#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""明日关注：哪些主线明天还值得盯、各自的龙头/中军/前排是谁、看什么信号。

排序口径是**回测出来的**，不是手拍权重（手拍权重的题材评分在本项目
已被证伪，见 theme.validate_persistence_score）：

    热度分 = 今日涨停家数 + 2 × 连续活跃天数（近 3 日内家数 ≥2 的连续天数）

回测目标：主线次日仍有 ≥3 家涨停（"还在"）。88 个交易日实测：
  - 连续活跃 3 天的主线次日续存 74%，当日才冒头的只有 18%；
  - "今天突然放量"反而是负面信号（一日游居多）；
  - 每天取前 3，样本外（后 30%）命中 60%，基准 34%。
backtest() 每天重算这组数字并展示在看板上，数字会随数据更新。

注意：这回答的是"明天这条线还在不在"，**不是**"明天买龙头能赚钱"。
后者本项目实测为负（追 ≥4 板剔除买不进后 -1.10%）。
"""

from __future__ import annotations

from datetime import datetime, timedelta

from . import catalyst as cat

STREAK_W = 2


def _dates(conn, end, n):
    return [r[0] for r in conn.execute(
        "SELECT DISTINCT trade_date FROM limit_up WHERE trade_date<=? "
        "ORDER BY trade_date DESC LIMIT ?", (end, n))]


def _kw_counts(conn, date):
    out = {}
    for r in conn.execute("SELECT reason, height FROM limit_up "
                          "WHERE trade_date=?", (date,)):
        for k in cat.keywords_of(r["reason"]):
            o = out.setdefault(k, [0, 0])
            o[0] += 1
            o[1] = max(o[1], r["height"] or 1)
    return out


def _line_count(kc, line):
    names = [line["name"]] + list(line.get("alias") or [])
    return max((kc.get(n, [0, 0])[0] for n in names), default=0)


def _enrich(conn, date, lines, prev_dates):
    """给主线补上：近几日家数、连续天数、热度分。"""
    kcs = [_kw_counts(conn, d) for d in prev_dates]
    for ln in lines:
        prev = [_line_count(kc, ln) for kc in kcs]
        streak = 0
        for v in prev:
            if v >= 2:
                streak += 1
            else:
                break
        ln["prev"] = prev                       # [昨, 前, 大前]
        ln["streak"] = streak
        ln["days_active"] = streak + 1
        ln["heat"] = ln["n"] + STREAK_W * streak
        ln["trend"] = ("新冒头" if streak == 0 else
                       "放量" if ln["n"] > (prev[0] or 0) else
                       "持平" if ln["n"] == prev[0] else "缩量")
    return lines


def _broken_by_line(conn, date, lines):
    rows = conn.execute("SELECT name, reason FROM broken_board "
                        "WHERE trade_date=?", (date,)).fetchall()
    for ln in lines:
        names = [ln["name"]] + list(ln.get("alias") or [])
        hit = [r["name"] for r in rows
               if set(cat.keywords_of(r["reason"])) & set(names)]
        ln["broken"] = hit


def _news_by_line(conn, date, lines, hours=40):
    since = (datetime.strptime(date, "%Y-%m-%d") + timedelta(hours=15)
             - timedelta(hours=hours)).isoformat()
    news = conn.execute("SELECT ts, title, content FROM news WHERE ts>=? "
                        "ORDER BY ts DESC", (since,)).fetchall()
    for ln in lines:
        names = [ln["name"]] + [a for a in ln.get("alias") or []
                                if len(a) <= 6]
        hit = []
        for r in news:
            t = (r["title"] or "") + (r["content"] or "")
            if any(n in t for n in names):
                hit.append({"ts": r["ts"], "title": r["title"]})
        ln["news"] = hit[:5]


def _roles(ln):
    """龙头 / 中军 / 前排首板。"""
    st = ln["stocks"]
    leader = st[0]
    mid = max(st, key=lambda s: s.get("float_mv") or 0)
    first = [s for s in st if (s["height"] or 1) == 1 and
             s["code"] not in (leader["code"], mid["code"])]
    first.sort(key=lambda s: (s.get("open_num") or 0,
                              s.get("first_seal") or "99"))
    return {"leader": leader,
            "core": mid if mid["code"] != leader["code"] else None,
            "front": first[:3]}


def _watch(ln, roles):
    """明天盯什么：由数据直接生成的观察点（不是买卖建议）。"""
    w = []
    ld = roles["leader"]
    h = ld["height"] or 1
    if h >= 2:
        w.append(f"龙头 {ld['name']} {h}板：明日能否晋级 {h + 1} 板；"
                 f"断板/低开则整条线转分歧")
    else:
        w.append(f"前排 {ld['name']} 首板：明日有无 2 板出现，决定是否成线")
    if ln["trend"] == "放量" and ln["streak"] >= 1:
        w.append(f"家数 {ln['prev'][0]}→{ln['n']} 放量发酵，关注是否高潮一致")
    elif ln["trend"] == "缩量":
        w.append(f"家数 {ln['prev'][0]}→{ln['n']} 缩量，注意退潮")
    elif ln["trend"] == "新冒头":
        w.append("今日才冒头：历史上这类线次日续存率低，先看是否有 2 日延续")
    if ln.get("broken"):
        w.append(f"炸板 {len(ln['broken'])} 家（{'、'.join(ln['broken'][:3])}），分歧信号")
    c = ln.get("catalyst")
    if c and c.get("date"):
        w.append(f"催化日期 {c['date']}：看后续有无新的政策/订单跟进")
    if ln.get("news"):
        w.append(f"盘后有 {len(ln['news'])} 条相关快讯，留意竞价强弱")
    return w


def stance(conn, date):
    """市场整体姿态：来自情绪周期（描述状态，不预测）。"""
    e = conn.execute("SELECT * FROM emotion_daily WHERE trade_date=?",
                     (date,)).fetchone()
    m = conn.execute("SELECT * FROM daily_market WHERE trade_date=?",
                     (date,)).fetchone()
    if not e:
        return {"stage": None, "text": "情绪数据缺失"}
    stg = e["stage"]
    txt = {
        "冰点": "冰点：只看最强的一条线的首个 2 板，右侧确认再动",
        "修复": "修复：主线前排可以跟，仓位别满",
        "发酵": "发酵：主线龙头+前排都有机会，别追后排",
        "高潮": "高潮：追高风险最大，只做分歧转一致",
        "分歧": "分歧：只留最强龙头，后排全部回避",
        "退潮": "退潮：空仓等冰点，别抄底",
    }.get(stg, stg or "")
    return {"stage": stg, "temperature": e["temperature"],
            "premium": e["prev_zt_premium"], "seal_rate": e["seal_rate"],
            "promote_rate": e["promote_rate"],
            "zt": m["zt_count"] if m else None,
            "dt": m["dt_count"] if m else None,
            "max_height": m["max_height"] if m else None,
            "amount": m["amount"] if m else None,
            "text": txt}


def tomorrow(conn, date, top=5):
    """明日关注清单。"""
    lines = cat.mainlines(conn, date)
    prev_dates = [d for d in _dates(conn, date, 4) if d < date][:3]
    _enrich(conn, date, lines, prev_dates)
    _broken_by_line(conn, date, lines)
    _news_by_line(conn, date, lines)
    lines.sort(key=lambda x: (-x["heat"], -x["max_height"]))
    picks = []
    for ln in lines[:top]:
        roles = _roles(ln)
        picks.append({**ln, "roles": roles, "watch": _watch(ln, roles)})
    return {"date": date, "stance": stance(conn, date), "picks": picks,
            "others": [{"name": x["name"], "n": x["n"], "heat": x["heat"],
                        "trend": x["trend"]} for x in lines[top:]],
            "backtest": backtest(conn, date)}


_BT = {}


def backtest(conn, end, k=3, lookback=120):
    """回测"每天前 k 条主线次日仍 ≥3 家涨停"的命中率（按日缓存）。"""
    if end in _BT:
        return _BT[end]
    dates = sorted(_dates(conn, end, lookback))
    if len(dates) < 12:
        return None
    kcs = {d: _kw_counts(conn, d) for d in dates}
    rows = []
    for i in range(3, len(dates) - 1):
        d, nxt = dates[i], dates[i + 1]
        lines = cat.mainlines(conn, d)
        for ln in lines:
            prev = [_line_count(kcs[dates[i - j]], ln) for j in (1, 2, 3)]
            s = 0
            for v in prev:
                if v >= 2:
                    s += 1
                else:
                    break
            rows.append((d, ln["n"] + STREAK_W * s,
                         _line_count(kcs[nxt], ln) >= 3))
    if not rows:
        return None
    ds = sorted({r[0] for r in rows})
    cut = ds[int(len(ds) * 0.7)]

    def hit(cond):
        h = []
        for d in ds:
            if not cond(d):
                continue
            rr = sorted((r for r in rows if r[0] == d), key=lambda r: -r[1])[:k]
            h += [r[2] for r in rr]
        base = [r[2] for r in rows if cond(r[0])]
        return (sum(h) / len(h) if h else None,
                sum(base) / len(base) if base else None, len(h) // k)

    tr, trb, trn = hit(lambda d: d < cut)
    te, teb, ten = hit(lambda d: d >= cut)
    out = {"k": k, "train": tr, "train_base": trb, "train_days": trn,
           "test": te, "test_base": teb, "test_days": ten,
           "from": ds[0], "to": ds[-1]}
    if len(_BT) > 8:
        _BT.clear()
    _BT[end] = out
    return out
