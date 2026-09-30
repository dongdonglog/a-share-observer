#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""情绪周期指标计算。

核心思路：
  1. 每天算出一组原始指标（涨停家数、封板率、昨涨停溢价、晋级率…）
  2. 用**滚动分位数**把每个指标映射到 0-100（历史不足时退回固定阈值）
  3. 加权合成"情绪温度分"，再叠加退潮/分歧的覆盖规则定位周期阶段

最重要的指标是 **prev_zt_premium（昨日涨停股今日平均涨幅）**：
它直接度量"打板/接力的人赚不赚钱"，即赚钱效应。
涨停家数再多，如果昨日涨停今天全亏，那也是退潮。
"""

from __future__ import annotations

from datetime import datetime

from . import config, db


def _now():
    return datetime.now().isoformat(timespec="seconds")


# ------------------------------------------------------------------ 基础

def prev_trade_date(conn, date):
    r = conn.execute(
        "SELECT MAX(trade_date) FROM daily_market WHERE trade_date < ?",
        (date,)).fetchone()
    return r[0] if r and r[0] else None


def next_trade_date(conn, date):
    r = conn.execute(
        "SELECT MIN(trade_date) FROM daily_market WHERE trade_date > ?",
        (date,)).fetchone()
    return r[0] if r and r[0] else None


def _pct_lookup(conn, date, codes):
    """取指定股票在某日的涨跌幅 {code: pct}。

    优先用 quote_daily（当日采集或历史K线回填），
    缺失时用 limit_up 自身的涨幅兜底。
    """
    codes = list(codes)
    if not codes:
        return {}
    out = {}
    CH = 400
    for i in range(0, len(codes), CH):
        chunk = codes[i:i + CH]
        ph = ",".join("?" for _ in chunk)
        for r in conn.execute(
                f"SELECT code, pct FROM quote_daily "
                f"WHERE trade_date=? AND code IN ({ph})", (date, *chunk)):
            if r["pct"] is not None:
                out[r["code"]] = r["pct"]
        # 兜底：涨停股自身一定有涨幅
        for r in conn.execute(
                f"SELECT code, pct FROM limit_up "
                f"WHERE trade_date=? AND code IN ({ph})", (date, *chunk)):
            out.setdefault(r["code"], r["pct"])
    return out


def _prev_sets(conn, date):
    """前一日涨停股 / 连板股。"""
    p = prev_trade_date(conn, date)
    if not p:
        return None, set(), set()
    zt = {r[0] for r in conn.execute(
        "SELECT code FROM limit_up WHERE trade_date=?", (p,))}
    lb = {r[0] for r in conn.execute(
        "SELECT code FROM limit_up WHERE trade_date=? AND height>=2", (p,))}
    return p, zt, lb


# ------------------------------------------------------------------ 计分

def _band_score(v, lo, hi, invert=False):
    if v is None:
        return None
    if hi == lo:
        return 50.0
    x = (v - lo) / (hi - lo)
    x = max(0.0, min(1.0, x))
    return (1.0 - x) * 100 if invert else x * 100


def _percentile_score(hist, v):
    """v 在 hist 中的分位（0-100）。"""
    if v is None or not hist:
        return None
    n = len(hist)
    below = sum(1 for h in hist if h is not None and h < v)
    return round(below / n * 100, 2)


def _history(conn, metric, date, window=None):
    """取某指标在此前 window 个交易日的历史值。"""
    window = window or config.ROLLING_WINDOW
    col = {
        "zt_count": "m.zt_count", "dt_count": "m.dt_count",
        "max_height": "m.max_height", "lianban_count": "m.lianban_count",
        "seal_rate": "e.seal_rate", "premium": "e.prev_zt_premium",
        "promote_rate": "e.promote_rate",
    }[metric]
    sql = (f"SELECT {col} FROM emotion_daily e "
           f"JOIN daily_market m ON m.trade_date=e.trade_date "
           f"WHERE e.trade_date < ? ORDER BY e.trade_date DESC LIMIT ?")
    return [r[0] for r in conn.execute(sql, (date, window)) if r[0] is not None]


def compute_day(conn, date, save=True):
    """计算某日的情绪指标。返回 dict。

    只有真正采集过涨停数据的交易日才计算：
    daily_market 里有大量"仅指数"的行（指数日K回填产生），
    如果不过滤，会生成一堆涨停数=0 的垃圾情绪记录，
    并污染后续的分位数计算。
    """
    m = conn.execute("SELECT * FROM daily_market WHERE trade_date=?",
                     (date,)).fetchone()
    if not m or m["zt_count"] is None:
        return None

    zt = m["zt_count"] or 0
    zb = m["zb_count"] or 0
    dt = m["dt_count"] or 0
    seal_rate = round(zt / (zt + zb), 4) if (zt + zb) > 0 else None
    max_height = m["max_height"] or 0
    lianban = m["lianban_count"] or 0

    p_date, p_zt, p_lb = _prev_sets(conn, date)
    prev_zt_premium = prev_lb_premium = promote_rate = None
    if p_date and p_zt:
        pcts = _pct_lookup(conn, date, p_zt)
        vals = [pcts[c] for c in p_zt if c in pcts]
        if vals:
            prev_zt_premium = round(sum(vals) / len(vals), 3)
        # 晋级率：昨日涨停今天仍然涨停
        today_zt = {r[0] for r in conn.execute(
            "SELECT code FROM limit_up WHERE trade_date=?", (date,))}
        if p_zt:
            promote_rate = round(len(p_zt & today_zt) / len(p_zt), 4)
        # 昨日连板股今日表现
        if p_lb:
            pcts_lb = _pct_lookup(conn, date, p_lb)
            v2 = [pcts_lb[c] for c in p_lb if c in pcts_lb]
            if v2:
                prev_lb_premium = round(sum(v2) / len(v2), 3)

    up = m["up_count"] or 0
    dn = m["down_count"] or 0
    up_down_ratio = round(up / (up + dn), 4) if (up + dn) > 0 else None

    # ---- 温度分
    raw = {
        "zt_count": zt,
        "seal_rate": seal_rate,
        "premium": prev_zt_premium,
        "promote_rate": promote_rate,
        "max_height": max_height,
        "lianban_count": lianban,
        "dt_count": dt,
    }
    parts, detail = {}, {}
    for k, v in raw.items():
        hist = _history(conn, k, date)
        s = None
        if len(hist) >= config.MIN_HISTORY_FOR_QUANTILE:
            s = _percentile_score(hist, v)
            detail[k] = "分位"
        if s is None:
            fb = config.FALLBACK.get(k)
            if fb:
                s = _band_score(v, fb["lo"], fb["hi"],
                                invert=(k == "dt_count"))
                detail[k] = "阈值"
        if s is not None:
            parts[k] = s

    tw = sum(config.WEIGHTS.get(k, 0) for k in parts)
    temperature = (round(sum(parts[k] * config.WEIGHTS.get(k, 0)
                             for k in parts) / tw, 2) if tw else None)

    stage, reason = classify(temperature, seal_rate, prev_zt_premium,
                             max_height, dt, lianban)

    row = {
        "trade_date": date,
        "seal_rate": seal_rate,
        "prev_zt_premium": prev_zt_premium,
        "prev_lb_premium": prev_lb_premium,
        "promote_rate": promote_rate,
        "up_down_ratio": up_down_ratio,
        "temperature": temperature,
        "stage": stage,
        "stage_reason": reason,
        "prev_zt_n": len(p_zt) if p_zt else 0,
        "prev_lb_n": len(p_lb) if p_lb else 0,
        "computed_at": _now(),
    }
    if save:
        db.upsert(conn, "emotion_daily", [row], ["trade_date"])
    row["_parts"] = parts
    row["_detail"] = detail
    row["_raw"] = raw
    return row


# ------------------------------------------------------------------ 阶段

def classify(temp, seal_rate, premium, max_height, dt, lianban):
    """周期阶段判定。覆盖规则优先于分数分档。"""
    # 退潮：赚钱效应崩了
    if premium is not None and premium <= config.EBB_PREMIUM:
        return "退潮", f"昨涨停溢价 {premium:.2f}% ≤ {config.EBB_PREMIUM}%"
    if temp is None:
        return "未知", "样本不足"
    # 分歧：高位 + 封板质量崩
    if (seal_rate is not None and seal_rate < config.DIVERGE_SEAL_RATE
            and max_height >= 4):
        return "分歧", (f"最高 {max_height} 板但封板率仅 "
                        f"{seal_rate * 100:.0f}%（< "
                        f"{config.DIVERGE_SEAL_RATE * 100:.0f}%）")
    for lo, hi, name in config.STAGE_BANDS:
        if lo <= temp < hi:
            return name, f"情绪温度 {temp:.1f} ∈ [{lo},{hi})"
    return "高潮", f"情绪温度 {temp:.1f}"


def trend(conn, date):
    """温度的变化方向：升温 / 降温 / 持平。"""
    cur = conn.execute("SELECT temperature FROM emotion_daily WHERE trade_date=?",
                       (date,)).fetchone()
    p = prev_trade_date(conn, date)
    if not cur or cur[0] is None or not p:
        return None, None
    prv = conn.execute("SELECT temperature FROM emotion_daily WHERE trade_date=?",
                       (p,)).fetchone()
    if not prv or prv[0] is None:
        return None, None
    d = round(cur[0] - prv[0], 2)
    label = "升温" if d > 3 else ("降温" if d < -3 else "持平")
    return label, d


def compute_range(conn, start=None, end=None, save=True):
    """按时间顺序批量计算（分位数依赖历史，必须顺序推进）。"""
    sql = "SELECT trade_date FROM daily_market WHERE zt_count IS NOT NULL"
    args = []
    if start:
        sql += " AND trade_date >= ?"
        args.append(start)
    if end:
        sql += " AND trade_date <= ?"
        args.append(end)
    sql += " ORDER BY trade_date"
    dates = [r[0] for r in conn.execute(sql, args)]
    out = []
    for d in dates:
        r = compute_day(conn, d, save=save)
        if r:
            out.append(r)
    return out
