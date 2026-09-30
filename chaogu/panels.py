#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""看板面板目录：每个面板 = 一个数据查询 + 一种默认可视化。

前端只认 viz 类型（stats / verdict / outlook / mainlines / events / ladder /
line / bar / table / news / markdown），数据全部由这里按 id 取。
AI 生成看板 = 让模型从 CATALOG 里挑面板、填参数、排布局，
所以模型**不能编造数据**，只能组织已有的查询。
"""

from __future__ import annotations

import json
import re
import threading
import time
import uuid
from datetime import datetime

from . import catalyst as cat
from . import config, llm, outlook

# ------------------------------------------------------------------ 目录

CATALOG = [
    {"id": "verdict", "title": "今日结论", "viz": "verdict", "w": 24, "h": 6,
     "desc": "一屏结论：情绪阶段与操作姿态、明日首选主线及龙头、风险提示"},
    {"id": "market_stats", "title": "市场温度计", "viz": "stats", "w": 24, "h": 3,
     "live": True,
     "desc": "涨停/跌停/炸板/封板率/最高板/连板数/昨涨停溢价/晋级率/成交额/指数"},
    {"id": "outlook", "title": "明日关注", "viz": "outlook", "w": 24, "h": 14,
     "desc": "明日重点主线（回测排序）、每条线的龙头/中军/前排首板、观察信号、历史命中率",
     "params": {"top": "主线条数，默认 5"}},
    {"id": "mainlines", "title": "今日主线 · 催化原文", "viz": "mainlines",
     "w": 14, "h": 12, "live": True,
     "desc": "按真实涨停原因聚类的主线，每条附成分股自己的催化原文、梯队"},
    {"id": "events", "title": "催化事件", "viz": "events", "w": 10, "h": 12,
     "desc": "同一段行业原因带动多只涨停的催化事件（政策/涨价/研报）"},
    {"id": "ladder", "title": "连板天梯", "viz": "ladder", "w": 10, "h": 10,
     "live": True, "desc": "按连板高度排列的涨停股及其涨停原因"},
    {"id": "emotion_trend", "title": "情绪周期走势", "viz": "line", "w": 14, "h": 8,
     "desc": "情绪温度、涨停家数、跌停家数、昨涨停溢价的历史走势",
     "params": {"days": "天数，默认 30"}},
    {"id": "height_trend", "title": "空间高度走势", "viz": "line", "w": 12, "h": 8,
     "desc": "最高连板、连板家数、封板率的历史走势", "params": {"days": "天数"}},
    {"id": "line_trend", "title": "主线热度走势", "viz": "line", "w": 12, "h": 8,
     "desc": "指定主线关键词近 N 日的涨停家数（不填则取今日前 4 条主线）",
     "params": {"keywords": "主线关键词列表，如 [\"机器人\",\"固态电池\"]",
                "days": "天数，默认 15"}},
    {"id": "limitup", "title": "涨停明细", "viz": "table", "w": 24, "h": 10,
     "live": True, "desc": "全部涨停股：高度、首封时间、开板次数、封单、换手、涨停原因",
     "params": {"keyword": "只看原因含该关键词的，如 机器人"}},
    {"id": "broken", "title": "炸板池", "viz": "table", "w": 12, "h": 8,
     "live": True, "desc": "当日炸板股及原因（分歧信号）"},
    {"id": "blocks", "title": "题材板块强度", "viz": "bar", "w": 12, "h": 8,
     "live": True, "desc": "同花顺题材板块按涨停家数排行（含板块涨幅）"},
    {"id": "news", "title": "实时快讯", "viz": "news", "w": 10, "h": 12,
     "live": True, "desc": "新浪 7x24 快讯，自动标注命中的主线",
     "params": {"keyword": "只看含该关键词的快讯", "limit": "条数，默认 40"}},
    {"id": "global", "title": "外围市场", "viz": "bar", "w": 12, "h": 7,
     "desc": "纳指/道指/标普/恒生/日经/美元/黄金/原油涨跌"},
    {"id": "seats", "title": "席位跟随候选", "viz": "table", "w": 12, "h": 9,
     "desc": "龙虎榜高胜率席位买入的个股（本项目唯一通过样本外检验的信号）"},
    {"id": "ai_brief", "title": "AI 明日作战计划", "viz": "markdown", "w": 14, "h": 12,
     "desc": "AI 基于当日数据写的明日计划：市场定性、重点题材、龙头、观察信号、风险"},
    {"id": "ai_answer", "title": "AI 问答", "viz": "markdown", "w": 12, "h": 10,
     "desc": "针对一个具体问题，AI 只根据看板数据作答",
     "params": {"question": "要回答的问题"}},
]
BY_ID = {p["id"]: p for p in CATALOG}


# ------------------------------------------------------------------ 工具

def latest_date(conn):
    r = conn.execute("SELECT MAX(trade_date) FROM limit_up").fetchone()
    return r[0] if r else None


def dates(conn, n=60):
    return [r[0] for r in conn.execute(
        "SELECT DISTINCT trade_date FROM limit_up ORDER BY trade_date DESC "
        "LIMIT ?", (n,))]


def phase(date):
    """当前视角：盘前 / 盘中 / 盘后。决定"明日关注"指的是哪一天。"""
    now = datetime.now()
    today = now.strftime("%Y-%m-%d")
    if date != today:
        return "pre" if now.weekday() < 5 and now.strftime("%H:%M") < "15:00" \
            else "post"
    return "live" if now.strftime("%H:%M") < "15:00" else "post"


def _yi(v):
    return None if v is None else round(v / 1e8, 2)


_CACHE, _CLOCK = {}, threading.Lock()


def _cached(key, ttl, fn):
    now = time.time()
    with _CLOCK:
        hit = _CACHE.get(key)
        if hit and now - hit[0] < ttl:
            return hit[1]
    val = fn()
    with _CLOCK:
        _CACHE[key] = (now, val)
        if len(_CACHE) > 200:
            for k in sorted(_CACHE, key=lambda k: _CACHE[k][0])[:100]:
                _CACHE.pop(k, None)
    return val


def _sig(conn, date):
    """数据签名：盘中数据一变，缓存就失效。"""
    a = conn.execute("SELECT COUNT(*), MAX(first_seal) FROM limit_up "
                     "WHERE trade_date=?", (date,)).fetchone()
    b = conn.execute("SELECT MAX(ts) FROM news").fetchone()
    return (a[0], a[1], (b[0] or "")[:15])


# ------------------------------------------------------------------ 查询

def q_market_stats(conn, date, p):
    m = conn.execute("SELECT * FROM daily_market WHERE trade_date=?",
                     (date,)).fetchone()
    e = conn.execute("SELECT * FROM emotion_daily WHERE trade_date=?",
                     (date,)).fetchone()
    zt = conn.execute("SELECT COUNT(*), MAX(height), SUM(height>=2) FROM limit_up "
                      "WHERE trade_date=?", (date,)).fetchone()
    zb = conn.execute("SELECT COUNT(*) FROM broken_board WHERE trade_date=?",
                      (date,)).fetchone()[0]
    dt = conn.execute("SELECT COUNT(*) FROM limit_down WHERE trade_date=?",
                      (date,)).fetchone()[0]
    prev = conn.execute(
        "SELECT m.* FROM daily_market m WHERE trade_date<? "
        "ORDER BY trade_date DESC LIMIT 1", (date,)).fetchone()
    n_zt = zt[0] or 0
    seal = n_zt / (n_zt + zb) if (n_zt + zb) else None

    def d(k, v):
        return None if not prev or prev[k] is None or v is None else v - prev[k]
    items = [
        {"label": "涨停", "value": n_zt, "delta": d("zt_count", n_zt),
         "color": "up"},
        {"label": "跌停", "value": dt or (m["dt_count"] if m else None),
         "delta": d("dt_count", dt), "color": "down", "invert": True},
        {"label": "炸板", "value": zb, "invert": True},
        {"label": "封板率", "value": seal, "fmt": "pct",
         "warn": seal is not None and seal < config.SEAL_RATE_WARN},
        {"label": "最高板", "value": zt[1], "delta": d("max_height", zt[1])},
        {"label": "连板家数", "value": zt[2] or 0,
         "delta": d("lianban_count", zt[2] or 0)},
        {"label": "昨涨停溢价", "value": e["prev_zt_premium"] if e else None,
         "fmt": "pct1", "color": "sign"},
        {"label": "晋级率", "value": e["promote_rate"] if e else None,
         "fmt": "pct"},
        # 成交额不给"较昨"：历史日的成交额由不完整的行情快照汇总
        # （约 1/3 个股，1300~2000 亿），和当日新浪全市场口径（1.4 万亿）不可比。
        {"label": "成交额(亿)", "value": _yi(m["amount"]) if m and m["amount"] else None,
         "fmt": "int"},
    ]
    from . import live
    idx = (live.STATE.get("index") or {}).get("data") or {}
    if not idx and m:
        idx = {"上证指数": {"pct": m["sh_pct"]}, "创业板指": {"pct": m["cyb_pct"]}}
    for k in ("上证指数", "创业板指", "科创50"):
        if k in idx and idx[k].get("pct") is not None:
            items.append({"label": k, "value": idx[k]["pct"], "fmt": "pct1",
                          "color": "sign"})
    return {"items": items}


def q_verdict(conn, date, p):
    o = _outlook(conn, date)
    st = o["stance"]
    ms = q_market_stats(conn, date, {})["items"]
    get = {i["label"]: i["value"] for i in ms}
    risks = []
    if (get.get("跌停") or 0) >= 10:
        risks.append(f"跌停 {get['跌停']} 家，亏钱效应明显")
    if get.get("封板率") is not None and get["封板率"] < config.SEAL_RATE_WARN:
        risks.append(f"封板率 {get['封板率']:.0%} 偏低，打板容错差")
    if st.get("premium") is not None and st["premium"] < 0:
        risks.append(f"昨涨停今日溢价 {st['premium']:.1f}%，接力亏钱")
    for pk in o["picks"][:3]:
        if pk["trend"] == "缩量":
            risks.append(f"{pk['name']} 家数 {pk['prev'][0]}→{pk['n']} 缩量")
    top = []
    for pk in o["picks"][:3]:
        ld = pk["roles"]["leader"]
        top.append({"name": pk["name"], "n": pk["n"], "days": pk["days_active"],
                    "trend": pk["trend"], "leader": ld["name"],
                    "leader_code": ld["code"], "height": ld["height"],
                    "catalyst": (pk.get("catalyst") or {}).get("title")})
    return {"date": date, "phase": phase(date), "stage": st.get("stage"),
            "temperature": st.get("temperature"), "stance": st.get("text"),
            "top": top, "risks": risks[:4], "backtest": o.get("backtest")}


def _outlook(conn, date):
    return _cached(("outlook", date, _sig(conn, date)), 60,
                   lambda: outlook.tomorrow(conn, date, top=8))


def q_outlook(conn, date, p):
    o = dict(_outlook(conn, date))
    top = int(p.get("top") or 5)
    o["picks"] = o["picks"][:top]
    o["phase"] = phase(date)
    return o


def q_mainlines(conn, date, p):
    return {"lines": _cached(("ml", date, _sig(conn, date)), 60,
                             lambda: cat.mainlines(conn, date))}


def q_events(conn, date, p):
    return {"events": cat.events(conn, date)}


def q_ladder(conn, date, p):
    rows = conn.execute(
        "SELECT code, name, height, first_seal, open_num, reason, lu_type "
        "FROM limit_up WHERE trade_date=? ORDER BY height DESC, "
        "COALESCE(first_seal,'99') ASC", (date,)).fetchall()
    tiers = {}
    for r in rows:
        tiers.setdefault(r["height"] or 1, []).append(dict(r))
    # 昨日晋级情况：昨天 N 板的有几只今天到了 N+1
    prev = conn.execute("SELECT MAX(trade_date) FROM limit_up WHERE trade_date<?",
                        (date,)).fetchone()[0]
    prom = {}
    if prev:
        for h, c in conn.execute("SELECT height, COUNT(*) FROM limit_up "
                                 "WHERE trade_date=? GROUP BY height", (prev,)):
            prom[(h or 1) + 1] = c
    return {"tiers": [{"height": h, "stocks": v, "prev": prom.get(h)}
                      for h, v in sorted(tiers.items(), reverse=True)]}


def _series(conn, date, days, cols):
    rows = conn.execute(
        "SELECT e.trade_date d, e.temperature, e.prev_zt_premium, e.seal_rate, "
        "e.promote_rate, m.zt_count, m.dt_count, m.max_height, m.lianban_count "
        "FROM emotion_daily e LEFT JOIN daily_market m USING (trade_date) "
        "WHERE e.trade_date<=? ORDER BY e.trade_date DESC LIMIT ?",
        (date, days)).fetchall()
    rows = list(reversed(rows))
    return {"x": [r["d"][5:] for r in rows],
            "series": [{"name": n, "data": [r[k] if r[k] is None or k not in
                                            ("seal_rate", "promote_rate")
                                            else round(r[k] * 100, 1)
                                            for r in rows], "axis": ax}
                       for n, k, ax in cols]}


def q_emotion_trend(conn, date, p):
    return _series(conn, date, int(p.get("days") or 30), [
        ("情绪温度", "temperature", 0), ("涨停", "zt_count", 0),
        ("跌停", "dt_count", 0), ("昨涨停溢价%", "prev_zt_premium", 1)])


def q_height_trend(conn, date, p):
    return _series(conn, date, int(p.get("days") or 30), [
        ("最高板", "max_height", 0), ("连板家数", "lianban_count", 0),
        ("封板率%", "seal_rate", 1)])


def q_line_trend(conn, date, p):
    kws = p.get("keywords")
    if isinstance(kws, str):
        kws = [k for k in re.split(r"[,，\s]+", kws) if k]
    if not kws:
        kws = [m["name"] for m in q_mainlines(conn, date, {})["lines"][:4]]
    days = int(p.get("days") or 15)
    series, x = [], []
    for k in kws[:6]:
        s = cat.keyword_series(conn, k, date, days)
        x = [r["date"][5:] for r in s]
        series.append({"name": k, "data": [r["n"] for r in s], "axis": 0})
    return {"x": x, "series": series}


def q_limitup(conn, date, p):
    kw = (p.get("keyword") or "").strip()
    rows = cat.stocks_of_day(conn, date)
    if kw:
        rows = [r for r in rows if kw in (r["reason"] or "") or
                kw in (r["reason_info"] or "") or kw in r["keywords"]]
    cols = [("name", "名称"), ("height", "板"), ("first_seal", "首封"),
            ("open_num", "开板"), ("seal_amount", "封单(亿)"),
            ("turnover", "换手%"), ("float_mv", "流通(亿)"), ("reason", "涨停原因")]
    out = []
    for r in rows:
        out.append({"code": r["code"], "name": r["name"], "height": r["height"],
                    "first_seal": (r["first_seal"] or "")[:5],
                    "open_num": r["open_num"],
                    "seal_amount": _yi(r["seal_amount"]),
                    "turnover": round(r["turnover"], 1) if r["turnover"] else None,
                    "float_mv": _yi(r["float_mv"]), "reason": r["reason"],
                    "_detail": cat.first_point(r["company_reason"] or
                                               r["industry_reason"], 160)})
    return {"columns": cols, "rows": out}


def q_broken(conn, date, p):
    rows = conn.execute(
        "SELECT code, name, pct, open_num, first_seal, reason FROM broken_board "
        "WHERE trade_date=? ORDER BY pct DESC", (date,)).fetchall()
    return {"columns": [("name", "名称"), ("pct", "涨幅%"), ("first_seal", "首封"),
                        ("reason", "原因")],
            "rows": [{**dict(r), "pct": round(r["pct"], 2) if r["pct"] else None,
                      "first_seal": (r["first_seal"] or "")[:5]} for r in rows]}


def q_blocks(conn, date, p):
    rows = conn.execute(
        "SELECT name, limit_up_num, continuous_num, change_pct, high FROM theme_block "
        "WHERE trade_date=? ORDER BY limit_up_num DESC, change_pct DESC LIMIT 15",
        (date,)).fetchall()
    rows = list(reversed(rows))
    return {"horizontal": True, "x": [r["name"] for r in rows],
            "series": [{"name": "涨停家数", "data": [r["limit_up_num"] for r in rows]},
                       {"name": "板块涨幅%", "data": [
                           round((r["change_pct"] or 0), 2) for r in rows]}]}


def q_news(conn, date, p):
    kw = (p.get("keyword") or "").strip()
    lim = int(p.get("limit") or 40)
    sql = "SELECT ts, title, content, tags FROM news "
    args = []
    if kw:
        sql += "WHERE title LIKE ? OR content LIKE ? "
        args = [f"%{kw}%", f"%{kw}%"]
    rows = conn.execute(sql + "ORDER BY ts DESC LIMIT ?", (*args, lim)).fetchall()
    lines = [m["name"] for m in q_mainlines(conn, date, {})["lines"][:10]]
    out = []
    for r in rows:
        t = (r["title"] or "") + (r["content"] or "")
        out.append({"ts": r["ts"], "title": r["title"],
                    "content": (r["content"] or "")[:220] if r["content"] != r["title"]
                    else "",
                    "tags": [x for x in (r["tags"] or "").split(",") if x][:4],
                    "lines": [n for n in lines if n in t]})
    return {"items": out}


def q_global(conn, date, p):
    d = conn.execute("SELECT MAX(trade_date) FROM global_market WHERE trade_date<=?",
                     (date,)).fetchone()[0]
    rows = conn.execute("SELECT name, pct FROM global_market WHERE trade_date=?",
                        (d,)).fetchall()
    return {"x": [r["name"] for r in rows],
            "series": [{"name": "涨跌%", "data": [r["pct"] for r in rows]}],
            "signed": True, "date": d}


def q_seats(conn, date, p):
    from . import seat
    sd = conn.execute("SELECT MAX(trade_date) FROM seat_trade WHERE trade_date<=?",
                      (date,)).fetchone()[0]
    try:
        sig = _cached(("seats", sd), 600, lambda: seat.follow_signals(
            conn, sd, min_rep_prob=config.SEAT_STRONG_REP_PROB,
            exclude_limit_up=True, top=20))
    except Exception:
        sig = []
    rows = [{"name": s["name"], "code": s["code"],
             "rep_prob": s.get("rep_prob"), "net": _yi(s.get("net_amt")),
             "pct": round(s["pct"], 2) if s.get("pct") is not None else None,
             "seat": (s.get("seat") or "").replace("证券股份有限公司", "")
             .replace("证券有限责任公司", "").replace("证券有限公司", "")[:22]}
            for s in sig]
    return {"columns": [("name", "个股"), ("pct", "当日%"), ("rep_prob", "席位胜率"),
                        ("net", "净买(亿)"), ("seat", "席位")],
            "rows": rows, "note": f"龙虎榜日期 {sd}" if sd != date else ""}


# ------------------------------------------------------------------ AI

def snapshot(conn, date):
    """喂给 LLM 的紧凑数据快照（只含事实，模型不得编造）。"""
    o = _outlook(conn, date)
    ms = {i["label"]: i["value"] for i in q_market_stats(conn, date, {})["items"]}
    picks = []
    for pk in o["picks"][:6]:
        r = pk["roles"]
        picks.append({
            "主线": pk["name"], "今日涨停": pk["n"], "近3日家数(昨,前,大前)": pk["prev"],
            "连续活跃天数": pk["days_active"], "趋势": pk["trend"],
            "梯队": pk["echelon"], "炸板": pk.get("broken"),
            "龙头": f"{r['leader']['name']}({r['leader']['code']}) {r['leader']['height']}板"
                  f" 首封{r['leader']['first_seal']}",
            "中军": r["core"]["name"] if r["core"] else None,
            "前排首板": [x["name"] for x in r["front"]],
            "催化原文": ((pk.get("catalyst") or {}).get("text") or "")[:300],
            "个股原因": [f"{s['name']}:{s['reason']}" for s in pk["stocks"][:8]],
            "相关快讯": [n["title"] for n in pk.get("news") or []][:3],
        })
    news = [dict(r) for r in conn.execute(
        "SELECT ts, title FROM news ORDER BY ts DESC LIMIT 25")]
    glob = q_global(conn, date, {})
    return {
        "日期": date, "视角": {"pre": "盘前", "live": "盘中", "post": "盘后"}[phase(date)],
        "市场": ms, "情绪": o["stance"],
        "主线(按回测热度排序)": picks,
        "其他主线": o["others"][:8],
        "高度龙头": [f"{r['name']} {r['height']}板 {r['reason']}" for r in conn.execute(
            "SELECT name, height, reason FROM limit_up WHERE trade_date=? "
            "ORDER BY height DESC LIMIT 6", (date,))],
        "外围": dict(zip(glob["x"], glob["series"][0]["data"])),
        "快讯": [f"{n['ts'][5:16]} {n['title']}" for n in news],
        "主线排序回测": o.get("backtest"),
    }


SYS_ANALYST = (
    "你是 A 股短线复盘助手。只能使用用户给的 JSON 数据，不得编造数据中没有的股票、"
    "数字或新闻；数据不足就直说。用中文，结论先行，短句，少形容词。"
    "你给的是观察清单而不是买卖指令：提到个股时必须同时给出'什么信号出现才成立/什么情况放弃'。"
    "注意本项目的实证：追高位龙头剔除买不进后收益为负；情绪指标描述现状有效、预测明天无效；"
    "主线'明天还在不在'的回测命中率见数据里的'主线排序回测'。")


def q_ai_brief(conn, date, p):
    if not llm.available():
        return {"md": "**AI 未配置。** 运行 `python3 run.py llm-setup` 配置后刷新。\n\n"
                      "下方「明日关注」面板是不依赖 AI 的规则版结果。", "ai": False}
    snap = snapshot(conn, date)
    key = ("brief", date, _sig(conn, date))

    def gen():
        user = ("数据：\n```json\n" + json.dumps(snap, ensure_ascii=False, default=str)
                + "\n```\n请写「明日作战计划」，Markdown，严格按以下结构，总长 400 字内：\n"
                "## 一句话定性\n(情绪阶段 + 明天整体仓位姿态)\n"
                "## 重点题材（最多3个）\n每个：题材名 — 为什么（引用催化原文要点/持续天数）"
                " — 龙头/前排个股 — 成立信号 / 放弃信号\n"
                "## 竞价与盘中观察\n3 条以内\n## 风险\n2 条以内")
        return llm.chat(SYS_ANALYST, user, max_tokens=1800)
    md = _cached(key, 900, gen)
    if not md:
        _CACHE.pop(key, None)
        return {"md": f"AI 调用失败：{llm.last_error}", "ai": False}
    return {"md": md, "ai": True,
            "at": datetime.now().strftime("%H:%M")}


def q_ai_answer(conn, date, p):
    q = (p.get("question") or "").strip()
    if not q:
        return {"md": "在面板参数里填写问题。"}
    if not llm.available():
        return {"md": "**AI 未配置。** 运行 `python3 run.py llm-setup`。"}
    snap = snapshot(conn, date)
    key = ("ans", date, q, _sig(conn, date))

    def gen():
        return llm.chat(SYS_ANALYST, "数据：\n```json\n" + json.dumps(
            snap, ensure_ascii=False, default=str) + f"\n```\n问题：{q}\n"
            "用 Markdown 回答，300 字内，先给结论。", max_tokens=1500)
    md = _cached(key, 900, gen)
    if not md:
        _CACHE.pop(key, None)
        return {"md": f"AI 调用失败：{llm.last_error}"}
    return {"md": md, "ai": True}


QUERIES = {
    "verdict": q_verdict, "market_stats": q_market_stats, "outlook": q_outlook,
    "mainlines": q_mainlines, "events": q_events, "ladder": q_ladder,
    "emotion_trend": q_emotion_trend, "height_trend": q_height_trend,
    "line_trend": q_line_trend, "limitup": q_limitup, "broken": q_broken,
    "blocks": q_blocks, "news": q_news, "global": q_global, "seats": q_seats,
    "ai_brief": q_ai_brief, "ai_answer": q_ai_answer,
}


def run(conn, pid, date=None, params=None):
    date = date or latest_date(conn)
    fn = QUERIES.get(pid)
    if not fn:
        raise KeyError(pid)
    return {"id": pid, "viz": BY_ID[pid]["viz"], "date": date,
            "data": fn(conn, date, params or {})}


# ------------------------------------------------------------------ 看板存储

DASH_PATH = config.DATA_DIR / "dashboards.json"


def _p(pid, **kw):
    b = BY_ID[pid]
    return {"uid": uuid.uuid4().hex[:8], "id": pid,
            "title": kw.pop("title", b["title"]),
            "w": kw.pop("w", b["w"]), "h": kw.pop("h", b["h"]),
            "params": kw}


def default_dashboards():
    return [
        {"id": "review", "title": "盘后复盘 · 明日作战", "builtin": True, "panels": [
            _p("verdict"), _p("market_stats"), _p("outlook"),
            _p("mainlines"), _p("events"),
            _p("ai_brief"), _p("news"),
            _p("line_trend"), _p("emotion_trend", w=12),
            _p("ladder", w=12), _p("broken"),
        ]},
        {"id": "live", "title": "盘中实时", "builtin": True, "panels": [
            _p("market_stats"), _p("mainlines"), _p("news"),
            _p("ladder", w=12), _p("blocks"), _p("limitup"),
        ]},
        {"id": "emotion", "title": "情绪周期", "builtin": True, "panels": [
            _p("market_stats"), _p("emotion_trend", w=12),
            _p("height_trend"), _p("global"), _p("seats"),
        ]},
    ]


_DLOCK = threading.Lock()


def load_dashboards():
    with _DLOCK:
        try:
            d = json.loads(DASH_PATH.read_text("utf-8"))
            if d.get("dashboards"):
                return d["dashboards"]
        except Exception:
            pass
        ds = default_dashboards()
        DASH_PATH.write_text(json.dumps({"dashboards": ds}, ensure_ascii=False,
                                        indent=1), "utf-8")
        return ds


def save_dashboard(dash):
    ds = load_dashboards()
    dash = _sanitize(dash)
    with _DLOCK:
        ds = [x for x in ds if x["id"] != dash["id"]] + [dash]
        DASH_PATH.write_text(json.dumps({"dashboards": ds}, ensure_ascii=False,
                                        indent=1), "utf-8")
    return dash


def delete_dashboard(did):
    ds = load_dashboards()
    with _DLOCK:
        ds = [x for x in ds if x["id"] != did]
        DASH_PATH.write_text(json.dumps({"dashboards": ds}, ensure_ascii=False,
                                        indent=1), "utf-8")


def reset_dashboards():
    with _DLOCK:
        DASH_PATH.unlink(missing_ok=True)
    return load_dashboards()


def _sanitize(dash):
    panels = []
    for p in dash.get("panels") or []:
        if p.get("id") not in BY_ID:
            continue
        b = BY_ID[p["id"]]
        panels.append({
            "uid": p.get("uid") or uuid.uuid4().hex[:8], "id": p["id"],
            "title": str(p.get("title") or b["title"])[:40],
            "w": max(4, min(24, int(p.get("w") or b["w"]))),
            "h": max(3, min(20, int(p.get("h") or b["h"]))),
            "params": p.get("params") if isinstance(p.get("params"), dict) else {},
        })
    return {"id": str(dash.get("id") or "d" + uuid.uuid4().hex[:6])[:40],
            "title": str(dash.get("title") or "未命名看板")[:40],
            "prompt": dash.get("prompt"), "builtin": bool(dash.get("builtin")),
            "panels": panels}


# ------------------------------------------------------------------ AI 生成看板

SYS_BUILDER = (
    "你是看板编排器。用户用一句话描述想看的 A 股短线看板，你从面板目录中挑选面板并排布。"
    "只能使用目录里的 id；可以改 title；params 只能用目录中列出的参数名。"
    "布局是 24 列栅格，w 为宽度(4-24)，h 为高度(3-20，单位约 40px)，按阅读顺序输出，"
    "同一行的 w 之和尽量为 24。最重要的结论放最上面。一般 4-9 个面板。"
    "只输出 JSON：{\"title\":\"看板名\",\"panels\":[{\"id\":..,\"title\":..,\"w\":..,"
    "\"h\":..,\"params\":{..}}]}")


def build_from_prompt(prompt, conn=None, date=None):
    """一句话 -> 看板。有 LLM 用 LLM，没有或失败用关键词规则。"""
    prompt = (prompt or "").strip()
    catalog = [{"id": p["id"], "title": p["title"], "desc": p["desc"],
                "params": p.get("params") or {}, "default_w": p["w"],
                "default_h": p["h"]} for p in CATALOG]
    ctx = ""
    if conn is not None:
        try:
            ls = [m["name"] for m in q_mainlines(conn, date or latest_date(conn),
                                                 {})["lines"][:10]]
            ctx = f"\n今日主线关键词（可用于 keyword/keywords 参数）：{ls}"
        except Exception:
            pass
    spec, how = None, "rule"
    if llm.available():
        txt = llm.chat(SYS_BUILDER, "面板目录：\n" + json.dumps(
            catalog, ensure_ascii=False) + ctx + f"\n\n用户需求：{prompt}",
            max_tokens=1500, timeout=60)
        spec = llm.extract_json(txt)
        if spec and spec.get("panels"):
            how = "ai"
        else:
            spec = None
    if not spec:
        spec = _rule_build(prompt)
    spec["prompt"] = prompt
    spec["id"] = "d" + uuid.uuid4().hex[:6]
    out = _sanitize(spec)
    out["how"] = how
    out["error"] = llm.last_error if how == "rule" and llm.available() else None
    return out


_RULES = [
    (r"明天|明日|推荐|关注|机会|计划|买", ["verdict", "outlook", "ai_brief"]),
    (r"主线|题材|板块|催化|原因", ["mainlines", "events", "line_trend", "blocks"]),
    (r"龙头|连板|高度|天梯|梯队", ["ladder", "height_trend"]),
    (r"情绪|周期|温度|赚钱效应|亏钱", ["market_stats", "emotion_trend", "height_trend"]),
    (r"实时|盘中|快讯|新闻|消息", ["market_stats", "news", "limitup"]),
    (r"炸板|分歧|风险", ["broken", "market_stats"]),
    (r"外围|美股|港股|隔夜", ["global", "news"]),
    (r"席位|龙虎榜|游资|大神", ["seats"]),
]


def _rule_build(prompt):
    ids = []
    for pat, ps in _RULES:
        if re.search(pat, prompt):
            ids += [x for x in ps if x not in ids]
    if not ids:
        ids = ["verdict", "outlook", "mainlines", "news"]
    kw = None
    for k in cat.CORE_KEYWORDS:
        if k in prompt:
            kw = cat.ALIAS.get(k, k)
            break
    panels = []
    for i in ids:
        b = BY_ID[i]
        params = {}
        if kw and i in ("limitup", "news"):
            params["keyword"] = kw
        if kw and i == "line_trend":
            params["keywords"] = [kw]
        panels.append({"id": i, "w": b["w"], "h": b["h"], "params": params})
    if kw and "limitup" not in ids:
        panels.append({"id": "limitup", "title": f"{kw} 涨停明细",
                       "params": {"keyword": kw}})
    return {"title": (prompt[:16] or "自定义看板"), "panels": panels}
