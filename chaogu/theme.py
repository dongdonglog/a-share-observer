#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""题材分析：新题材侦测、延续性评分、龙头识别。

数据来源：
  - theme_block  同花顺概念板块（带涨停家数/连板数/最高板/活跃天数）
  - theme_stock  板块成分涨停股（带 reason_type 题材归因、reason_info 催化原文）

"新题材"的定义（可调，见 config）：
  今日出现、且此前 THEME_LOOKBACK 个交易日内没有出现过、
  且今日涨停家数 >= NEW_THEME_MIN_ZT。
另有一类"异动启动"：老题材但家数从低位突然跳到 EMERGING_ZT_JUMP 以上。
"""

from __future__ import annotations

import json
import statistics as st
from datetime import datetime

from . import config, db


def _j(v):
    return json.dumps(v, ensure_ascii=False)


def _recent_dates(conn, date, n):
    rows = conn.execute(
        "SELECT DISTINCT trade_date FROM theme_block WHERE trade_date < ? "
        "ORDER BY trade_date DESC LIMIT ?", (date, n)).fetchall()
    return [r[0] for r in rows]


# ------------------------------------------------------------------ 汇总

def build_daily(conn, date):
    """把主题材板块 + 成分股汇总进 theme_daily。"""
    blocks = conn.execute(
        "SELECT * FROM theme_block WHERE trade_date=?", (date,)).fetchall()
    out = []
    for b in blocks:
        tag = b["name"]
        stocks = conn.execute(
            "SELECT * FROM theme_stock WHERE trade_date=? AND tag=?",
            (date, tag)).fetchall()
        if not stocks:
            continue
        heights = [s["continue_num"] or 1 for s in stocks]
        max_h = max(heights)
        echelon = {}
        for h in heights:
            echelon[str(h)] = echelon.get(str(h), 0) + 1
        # 龙头：高度优先，其次首封早，再次涨幅
        def key(s):
            return (-(s["continue_num"] or 1),
                    s["first_seal"] or "99:99:99",
                    -(s["pct"] or 0))
        leader = sorted(stocks, key=key)[0]
        # 注意：同花顺题材接口不返回换手率，theme_stock 无 turnover 字段。
        # 这里统计的是成分股平均涨跌幅，字段名历史上误用了 avg_turnover。
        # 现统一按真实口径命名，避免下游把涨跌幅当换手率使用。
        pcts = [s["pct"] for s in stocks if s["pct"] is not None]
        lianban = sum(1 for h in heights if h >= 2)
        out.append({
            "trade_date": date,
            "tag": tag,
            "zt_count": len(stocks),
            "max_height": max_h,
            "leader_code": leader["code"],
            "leader_name": leader["name"],
            "leader_height": leader["continue_num"] or 1,
            "first_seal": leader["first_seal"],
            "echelon": _j(echelon),
            # 真实口径是平均涨跌幅（avg_pct）。保留 avg_turnover 列名以兼容
            # 已有数据库，但值语义已在此更正，禁止当作换手率使用。
            "avg_turnover": round(sum(pcts) / len(pcts), 3) if pcts else None,
            "seal_quality": round(lianban / len(stocks), 4),
        })
    n = db.upsert(conn, "theme_daily", out, ["trade_date", "tag"])
    return n


def build_range(conn, start=None, end=None):
    sql = "SELECT DISTINCT trade_date FROM theme_block"
    args = []
    if start:
        sql += " WHERE trade_date >= ?"
        args.append(start)
    sql += " ORDER BY trade_date"
    dates = [r[0] for r in conn.execute(sql, args)]
    if end:
        dates = [d for d in dates if d <= end]
    total = 0
    for d in dates:
        total += build_daily(conn, d)
    return total


# ------------------------------------------------------------------ 新题材

def detect_new(conn, date, lookback=None):
    """侦测新题材 / 异动启动题材。"""
    lookback = lookback or config.THEME_LOOKBACK
    prev_dates = _recent_dates(conn, date, lookback)

    today = {r["tag"]: r for r in conn.execute(
        "SELECT * FROM theme_daily WHERE trade_date=?", (date,))}
    if not today:
        return []

    hist = {}
    if prev_dates:
        ph = ",".join("?" for _ in prev_dates)
        for r in conn.execute(
                f"SELECT tag, trade_date, zt_count FROM theme_daily "
                f"WHERE trade_date IN ({ph})", prev_dates):
            hist.setdefault(r["tag"], {})[r["trade_date"]] = r["zt_count"]

    out = []
    for tag, row in today.items():
        zt = row["zt_count"] or 0
        h = hist.get(tag)
        if not h:
            if zt >= config.NEW_THEME_MIN_ZT:
                out.append({
                    "tag": tag, "kind": "新题材", "zt_count": zt,
                    "prev_max": 0, "days": 1,
                    "max_height": row["max_height"],
                    "leader": row["leader_name"],
                    "note": f"近{lookback}个交易日首次出现，"
                            f"今日涨停 {zt} 只",
                })
        else:
            prev_max = max(h.values()) if h else 0
            if zt >= config.EMERGING_ZT_JUMP and prev_max <= 1:
                out.append({
                    "tag": tag, "kind": "异动启动", "zt_count": zt,
                    "prev_max": prev_max, "days": len(h) + 1,
                    "max_height": row["max_height"],
                    "leader": row["leader_name"],
                    "note": f"家数由 {prev_max} 跳增至 {zt}",
                })
            elif zt >= config.NEW_THEME_MIN_ZT and prev_max == 0:
                out.append({
                    "tag": tag, "kind": "新题材", "zt_count": zt,
                    "prev_max": 0, "days": 1,
                    "max_height": row["max_height"],
                    "leader": row["leader_name"],
                    "note": f"此前 {lookback} 日无涨停，今日 {zt} 只",
                })
    out.sort(key=lambda x: (-x["zt_count"], -x["max_height"]))
    return out


# ------------------------------------------------------------------ 延续性

def _series(conn, tag, date, n):
    """取某题材最近 n 个交易日的 (date, zt_count, max_height)，按时间正序。"""
    rows = conn.execute(
        "SELECT trade_date, zt_count, max_height FROM theme_daily "
        "WHERE tag=? AND trade_date<=? ORDER BY trade_date DESC LIMIT ?",
        (tag, date, n)).fetchall()
    return list(reversed([dict(r) for r in rows]))


def score_persistence(conn, date, tag, lookback=None):
    """题材延续性评分 0-100，附各分项。"""
    lookback = lookback or config.THEME_LOOKBACK
    ser = _series(conn, tag, date, lookback)
    if not ser:
        return None
    today = ser[-1]
    zt = today["zt_count"] or 0
    mh = today["max_height"] or 0
    prev = ser[-2]["zt_count"] if len(ser) >= 2 else 0
    prev = prev or 0

    row = conn.execute("SELECT * FROM theme_daily WHERE trade_date=? AND tag=?",
                       (date, tag)).fetchone()
    blk = conn.execute("SELECT * FROM theme_block WHERE trade_date=? AND name=?",
                       (date, tag)).fetchone()

    # 1) 广度：涨停家数（8 家以上给满分）
    breadth = min(zt / 8.0, 1.0) * 100
    # 2) 趋势：相对昨日的变化
    if prev == 0:
        trend = 70.0 if zt >= 2 else 40.0
    else:
        ratio = zt / prev
        trend = max(0.0, min(1.0, (ratio - 0.4) / 1.1)) * 100
    # 3) 龙头高度：>=4 板给满分
    leader = min((row["leader_height"] or 1) / 4.0, 1.0) * 100 if row else 0
    # 4) 梯队：高度层数（>=3 层满分），且有连板
    ech = json.loads(row["echelon"]) if row and row["echelon"] else {}
    layers = len([k for k in ech if int(k) >= 2])
    echelon = min(layers / 3.0, 1.0) * 100
    # 5) 封板质量：连板占比
    seal = (row["seal_quality"] or 0) * 100 if row else 0
    # 6) 板块指数表现
    sect = 50.0
    if blk and blk["change_pct"] is not None:
        c = blk["change_pct"]
        c = c * 100 if abs(c) < 1 else c          # 兼容小数/百分数
        sect = max(0.0, min(1.0, (c + 2) / 7)) * 100

    parts = {"breadth": breadth, "trend": trend, "leader": leader,
             "echelon": echelon, "seal": seal, "sector": sect}
    w = config.THEME_SCORE_WEIGHTS
    total = sum(parts[k] * w[k] for k in parts)
    tw = sum(w.values())
    return {
        "tag": tag,
        "score": round(total / tw, 2),
        "parts": {k: round(v, 1) for k, v in parts.items()},
        "zt_count": zt,
        "prev_zt": prev,
        "max_height": mh,
        "days": len(ser),
        "leader": row["leader_name"] if row else None,
        "leader_height": row["leader_height"] if row else None,
        "ladder": ech,
    }


def rank_persistence(conn, date, min_zt=2, top=15):
    """按延续性给当日题材排序。"""
    tags = [r["tag"] for r in conn.execute(
        "SELECT tag FROM theme_daily WHERE trade_date=? AND zt_count>=?",
        (date, min_zt))]
    scored = []
    for t in tags:
        s = score_persistence(conn, date, t)
        if s:
            scored.append(s)
    scored.sort(key=lambda x: -x["score"])
    return scored[:top]


# ------------------------------------------------------------------ 龙头

def market_leaders(conn, date, top=15):
    """全市场龙头：高度优先，其次封板时间与封单额。"""
    rows = conn.execute(
        "SELECT * FROM limit_up WHERE trade_date=? ORDER BY height DESC, "
        "COALESCE(first_seal,'99:99:99') ASC, COALESCE(seal_amount,0) DESC "
        "LIMIT ?", (date, top)).fetchall()
    return [dict(r) for r in rows]


def theme_leaders(conn, date, top=15):
    """每个题材的龙头（按题材延续性排序后取龙头）。"""
    out = []
    for s in rank_persistence(conn, date, min_zt=2, top=top):
        row = conn.execute(
            "SELECT * FROM theme_stock WHERE trade_date=? AND tag=? "
            "ORDER BY continue_num DESC, COALESCE(first_seal,'99:99:99') ASC",
            (date, s["tag"])).fetchall()
        out.append({
            "tag": s["tag"],
            "score": s["score"],
            "zt_count": s["zt_count"],
            "stocks": [dict(r) for r in row],
        })
    return out


def theme_history(conn, tag, date, n=5):
    """题材最近 n 个交易日的涨停家数序列（按时间正序）。

    这是判断"发酵还是衰减"最直观的东西：
    3 → 5 → 8 → 12 是发酵；8 → 5 → 3 是衰减。
    """
    rows = conn.execute(
        "SELECT trade_date, zt_count, max_height, leader_name "
        "FROM theme_daily WHERE tag=? AND trade_date<=? "
        "ORDER BY trade_date DESC LIMIT ?", (tag, date, n)).fetchall()
    return list(reversed([dict(r) for r in rows]))


def sparkline(values):
    """把家数序列画成简易条形，用于终端/markdown 展示。"""
    if not values:
        return ""
    chars = "▁▂▃▄▅▆▇█"
    mx = max(values) or 1
    return "".join(chars[min(len(chars) - 1,
                             int(v / mx * (len(chars) - 1)))]
                   for v in values)


def lifecycle(conn, date, min_zt=2, top=12):
    """题材生命周期：家数序列 + 阶段判断（启动/发酵/高潮/衰减）。"""
    out = []
    for s in rank_persistence(conn, date, min_zt=min_zt, top=top):
        ser = theme_history(conn, s["tag"], date, n=5)
        vals = [x["zt_count"] or 0 for x in ser]
        if len(vals) >= 2:
            a, b = vals[-2], vals[-1]
            if b > a:
                phase = "发酵"
            elif b == a:
                phase = "持平"
            else:
                phase = "衰减"
        else:
            phase = "启动"
        s["series"] = vals
        s["spark"] = sparkline(vals)
        s["phase"] = phase
        out.append(s)
    return out


def stock_reason_map(conn, date):
    """code -> 该股的题材归因（优先取连板最高的那条记录）。"""
    out = {}
    for r in conn.execute(
            "SELECT code, name, reason, reason_info, continue_num, tag "
            "FROM theme_stock WHERE trade_date=? "
            "ORDER BY continue_num ASC", (date,)):
        # 后写入的连板更高，覆盖前面的；为空则不覆盖已有
        reason = (r["reason"] or "").strip()
        if reason:
            out[r["code"]] = reason
        else:
            out.setdefault(r["code"], r["tag"])
    return out


def stock_catalyst(conn, date, code):
    """取某只股票的催化原因原文（最详细的一条）。"""
    r = conn.execute(
        "SELECT reason_info, reason FROM theme_stock "
        "WHERE trade_date=? AND code=? AND reason_info IS NOT NULL "
        "ORDER BY continue_num DESC LIMIT 1", (date, code)).fetchone()
    return dict(r) if r else None


def theme_catalyst(conn, date, tag, limit=3):
    """题材的催化原因（取该题材下连板最高的几只的 reason_info）。"""
    rows = conn.execute(
        "SELECT code, name, continue_num, reason, reason_info FROM theme_stock "
        "WHERE trade_date=? AND tag=? ORDER BY continue_num DESC LIMIT ?",
        (date, tag, limit)).fetchall()
    return [dict(r) for r in rows if r["reason_info"] or r["reason"]]


def _series_leaders(conn, date, tag, limit=3):
    """题材内领涨个股（按连板高度、首封时间排序）。"""
    rows = conn.execute(
        "SELECT * FROM theme_stock WHERE trade_date=? AND tag=? "
        "ORDER BY continue_num DESC, COALESCE(first_seal,'99:99:99') ASC "
        "LIMIT ?", (date, tag, limit)).fetchall()
    return [dict(r) for r in rows]


# ---------------------------------------------------- 延续性评分的实证检验
# 以下函数回答一个此前**从未被验证**的问题：这套 6 项加权评分
# （权重在 config.THEME_SCORE_WEIGHTS 手工设定）到底能不能预测"能不能继续做"？
#
# 实测结论（2026-05-28 ~ 2026-09-30，88 个交易日）：
#
# 1) 题材层面（该题材未来 1~3 日是否还有涨停）：
#      评分 0-29 -> 持续率 55.4%
#      评分 70-79 -> 71.0%，但 80-100 回落到 67.7%  => 非单调
#      复合评分 Spearman 仅 +0.055
#    **而单一特征"昨日涨停家数"的 Spearman 是 +0.328，是复合评分的 6 倍。**
#    各分项单独表现：
#      echelon +0.154 / breadth +0.138 / leader +0.122 / seal +0.067
#      **trend -0.076 / sector -0.115（方向为负！）**
#    也就是说，手工权重把有效信号稀释了，还混进了两个反向分项。
#
# 2) 个股层面（持有的这只票次日是否继续涨停）——**这才是能不能赚钱的实质**：
#      全样本 Spearman = -0.019（无预测力）
#      训练段 -0.016 / 留出段 -0.029（两段都略负）
#      1 板股：评分 0-49 续板率 18.4%  vs  评分 70-100 只有 13.2%（**反向**）
#    控制连板高度后依然如此，说明不是"高板更容易续"的混淆造成的。
#
# **结论：这套评分不能用来判断个股"能不能继续做"。**
# 它在题材层面有一点点排序能力，但不如直接看涨停家数；
# 在个股层面无效甚至反向。报告中的评分应视为"题材热度的描述"，
# 而不是"延续性的预测"。真正的可用信号在 seat（席位跟随）。
# 详见 README 第三、五节。

def validate_persistence_score(conn, min_zt=2, split=0.7):
    """检验题材延续性评分是否真的能预测延续。

    返回题材层面与个股层面的分档持续率、相关系数，以及样本外分割结果。
    这是"我们的评分到底有没有用"的可复现答案。
    """
    from . import seat as _seat

    dates = sorted({r[0] for r in conn.execute(
        "SELECT DISTINCT trade_date FROM limit_up")})
    if len(dates) < 20:
        return {"ok": False, "reason": "交易日不足"}
    di = {d: i for i, d in enumerate(dates)}

    # 次日仍涨停的股票集合
    ladder = {}
    for r in conn.execute("SELECT trade_date, code FROM limit_up"):
        ladder.setdefault(r["code"], set()).add(r["trade_date"])

    theme_rows, stock_rows = [], []
    for i, d in enumerate(dates[:-1]):
        nd = dates[di[d] + 1]
        # 题材层面
        nxt_tags = set()
        for nd2 in dates[i + 1:i + 4]:
            for (t,) in conn.execute(
                    "SELECT DISTINCT tag FROM theme_daily "
                    "WHERE trade_date=? AND zt_count>0", (nd2,)):
                nxt_tags.add(t)
        for (t,) in conn.execute(
                "SELECT tag FROM theme_daily WHERE trade_date=? AND zt_count>=?",
                (d, min_zt)):
            s = score_persistence(conn, d, t)
            if not s:
                continue
            theme_rows.append({
                "date": d, "score": s["score"], "zt": s["zt_count"],
                "prev": s["prev_zt"], "parts": s["parts"],
                "persist": 1 if t in nxt_tags else 0})
        # 个股层面
        for r in conn.execute(
                "SELECT code, height FROM limit_up WHERE trade_date=?", (d,)):
            best = None
            for (t,) in conn.execute(
                    "SELECT DISTINCT tag FROM theme_stock WHERE code=? LIMIT 6",
                    (r["code"],)):
                s = score_persistence(conn, d, t)
                if s and (best is None or s["score"] > best):
                    best = s["score"]
            if best is None:
                continue
            stock_rows.append({
                "date": d, "score": best, "h": r["height"] or 1,
                "cont": 1 if nd in ladder.get(r["code"], ()) else 0})

    def band(rows, key, cuts):
        out = []
        for lo, hi in cuts:
            g = [r for r in rows if lo <= r[key] < hi]
            if len(g) < 25:
                continue
            out.append({"lo": lo, "hi": hi - 1, "n": len(g),
                        "rate": sum(r["persist" if key == "score" and
                                             "persist" in r else "cont"]
                                    for r in g) / len(g) * 100})
        return out

    res = {"ok": True, "theme_n": len(theme_rows), "stock_n": len(stock_rows)}
    if theme_rows:
        res["theme_base"] = sum(r["persist"] for r in theme_rows) / \
            len(theme_rows) * 100
        res["theme_corr"] = _seat._spearman(
            [r["score"] for r in theme_rows],
            [r["persist"] for r in theme_rows])
        res["prev_zt_corr"] = _seat._spearman(
            [r["prev"] for r in theme_rows],
            [r["persist"] for r in theme_rows])
        res["parts_corr"] = {
            k: _seat._spearman([r["parts"][k] for r in theme_rows],
                               [r["persist"] for r in theme_rows])
            for k in ("breadth", "trend", "leader",
                      "echelon", "seal", "sector")}
    if stock_rows:
        res["stock_base"] = sum(r["cont"] for r in stock_rows) / \
            len(stock_rows) * 100
        res["stock_corr"] = _seat._spearman(
            [r["score"] for r in stock_rows],
            [r["cont"] for r in stock_rows])
        stock_rows.sort(key=lambda x: x["date"])
        cut = stock_rows[int(len(stock_rows) * split)]["date"]
        for label, g in (("train", [r for r in stock_rows if r["date"] < cut]),
                         ("holdout", [r for r in stock_rows
                                      if r["date"] >= cut])):
            if len(g) < 50:
                continue
            res[f"stock_{label}_n"] = len(g)
            res[f"stock_{label}_corr"] = _seat._spearman(
                [r["score"] for r in g], [r["cont"] for r in g])
        # 1 板股分层（最能说明问题）
        one = [r for r in stock_rows if r["h"] == 1]
        if len(one) >= 50:
            res["one_board"] = []
            for lo, hi in ((0, 50), (50, 60), (60, 70), (70, 101)):
                g = [r for r in one if lo <= r["score"] < hi]
                if len(g) < 25:
                    continue
                res["one_board"].append({
                    "lo": lo, "hi": hi - 1, "n": len(g),
                    "rate": sum(r["cont"] for r in g) / len(g) * 100})
    return res


# ---------------------------------------------------- 龙头效应的实证检验
# 用户原始问题之一是"龙头是谁"。本函数回答：**跟着买龙头能赚钱吗？**
#
# 实测结论（2026-05-28 ~ 2026-09-30，6,507 个涨停事件）：
#
#   按连板高度分档，"次日买入→次日收盘"的原始收益单调上升：
#     1板 +1.06% / 2板 +2.23% / 3板 +2.19% / 4板 +2.54% / 5板 +3.50%
#   控制市场 beta 后依然单调（超额 +1.17% / +2.18% / +2.19% / +2.57% / +3.53%）。
#   表面看"越高越强"，非常符合"追龙头"的直觉。
#
# **但这是前视偏差的产物。** 涨停股次日往往一字或大幅高开，你买不进：
#
#   板数   买不进占比   买不进者收益   可买者收益
#   1板      13.7%        +10.48%       -0.43%
#   2板      25.5%        +10.35%       -0.56%
#   3板      30.8%        +10.29%       -1.41%
#   4板      32.9%        +10.21%       -1.22%
#   5板      37.7%        +10.00%       -0.43%
#
# **板数越高，"买不进"的比例越高**（13.7% → 37.7%），
# 而那些买不进的恰恰贡献了 +10% 的收益。
# 剔除买不进、控制 beta、扣 0.40% 成本后，**全部档位转负**：
#   1板 -0.68% / 2板 -0.94% / 3板 -1.70% / >=4板 -1.10%
#
# 样本外一致：>=4板可买净超额 训练 -1.19%、留出 -0.96%。
#
# **结论：追龙头（高板）不是一个正期望策略。**
# 它看起来赚钱，是因为统计里混入了你根本买不到的样本。
# 这条与席位信号是**同一类偏差**，但在龙头身上更严重
# （席位买不进 27.1%，5板龙头买不进 37.7%）。

def validate_leader_effect(conn, cost=None, split=0.7):
    """检验"追龙头（高连板）"是否真的有正期望。

    用次日涨停作为"买不进"的代理，剔除后计算超额净收益。
    返回各档位原始收益、买不进占比、可买净超额，以及样本外分割结果。
    """
    from . import seat as _seat

    cost = _seat.total_cost() if cost is None else cost
    dates = sorted({r[0] for r in conn.execute(
        "SELECT DISTINCT trade_date FROM quote_daily")})
    if len(dates) < 20:
        return {"ok": False, "reason": "行情数据不足"}
    di = {d: i for i, d in enumerate(dates)}
    px = {(r[0], r[1]): r[2] for r in conn.execute(
        "SELECT trade_date, code, close FROM quote_daily WHERE close IS NOT NULL")}
    lu = {(r[0], r[1]) for r in conn.execute(
        "SELECT trade_date, code FROM limit_up")}
    mkt = {d: conn.execute(
        "SELECT AVG(pct) FROM quote_daily WHERE trade_date=?",
        (d,)).fetchone()[0] for d in dates}

    recs = []
    for r in conn.execute(
            "SELECT trade_date, code, height FROM limit_up "
            "WHERE height IS NOT NULL"):
        i = di.get(r["trade_date"])
        if i is None or i + 1 >= len(dates):
            continue
        nd = dates[i + 1]
        a = px.get((r["trade_date"], r["code"]))
        b = px.get((nd, r["code"]))
        if not a or not b:
            continue
        recs.append({"h": r["height"], "ret": (b / a - 1) * 100,
                     "date": r["trade_date"], "mkt": mkt.get(nd),
                     "locked": (nd, r["code"]) in lu})
    if len(recs) < 200:
        return {"ok": False, "reason": f"样本不足（{len(recs)}）"}

    def band(rows, lo, hi):
        g = [x for x in rows if lo <= x["h"] <= hi]
        buy = [x for x in g if not x["locked"] and x["mkt"] is not None]
        if len(g) < 25 or len(buy) < 20:
            return None
        return {
            "label": f"{lo}板" if lo == hi else f">={lo}板",
            "n": len(g),
            "raw": st.mean([x["ret"] for x in g]),
            "locked_share": sum(1 for x in g if x["locked"]) / len(g) * 100,
            "locked_ret": (st.mean([x["ret"] for x in g if x["locked"]])
                           if any(x["locked"] for x in g) else None),
            "buy_n": len(buy),
            "buy_net": st.mean([x["ret"] - x["mkt"] - cost for x in buy]),
            "buy_win": sum(1 for x in buy
                           if x["ret"] - x["mkt"] - cost > 0) / len(buy) * 100,
        }

    out = {"ok": True, "n": len(recs), "cost": cost}
    out["bands"] = [b for b in (band(recs, h, h) for h in (1, 2, 3, 4, 5))
                    if b]
    out["leader"] = band(recs, 4, 99)
    recs.sort(key=lambda x: x["date"])
    cut = recs[int(len(recs) * split)]["date"]
    for lab, g in (("train", [x for x in recs if x["date"] < cut]),
                   ("holdout", [x for x in recs if x["date"] >= cut])):
        b = band(g, 4, 99)
        if b:
            out[f"{lab}_leader"] = b
    return out
