#!/usr/bin/env python3
"""信号留痕与回填：给看板装上"我上次说对了没有"的闭环。

为什么必须有这个模块
--------------------
用户目标是"提高胜率"。但**胜率只能靠留痕来改进**：

- 没有留痕，人只会记得赚钱的那几单，忘记亏的（选择性记忆）。
  这是散户最常见的自我欺骗方式，也是"感觉最近挺准"的来源。
- 看板每天给候选，如果不记录**当时**看到了什么，
  事后就无法区分"信号失效"和"我没照做"。

本模块把看板当时的判断写进 `signal_log`，之后用真实行情回填：
次日/5日/10日收益，以及最关键的一列 —— **次日是否涨停（买不进）**。

口径（与 seat.py 保持一致，不要改坏）
------------------------------------
- `signal_date` = 看板给出信号的日子，**收盘后**才披露/计算，
  因此真实可执行入场是 `signal_date` 的**次日**。
- `entry_close` 记的是 signal_date 收盘价，**这只是收益计算的基准，
  不是你能成交的价格**。真实成交价应接近次日开盘。
- `d1_ret` = 次日收盘 / signal_date 收盘 - 1。
- `d1_locked` = 次日是否又涨停（买不进）。**这一列是诚实性的关键**：
  本项目已证明席位信号"买不进"占 27.1%、追龙头占 37.7%，
  这些收益统计上归你、实际拿不到。
- 净收益展示时统一扣 `seat.total_cost()`（0.40%）。

用法
----
    python3 run.py track log        # 把今日看板候选记进 signal_log
    python3 run.py track fill       # 用真实行情回填未完成的行
    python3 run.py track report     # 看留痕战绩（含买不进比例）
"""

from __future__ import annotations

import statistics as st
from datetime import datetime

from . import config, db, seat


# ------------------------------------------------------------------ 留痕

def log_signals(conn, date=None, min_rep_prob=None, top=50):
    """把某日看板的跟随候选写入 signal_log。

    幂等：同一天重复调用不会产生重复行（主键 signal_date+code），
    已回填过的行不会被改回 NULL。
    """
    date = date or _latest_seat_date(conn)
    if not date:
        return {"ok": False, "reason": "无席位数据"}
    min_rep_prob = (config.SEAT_STRONG_REP_PROB if min_rep_prob is None
                    else min_rep_prob)
    try:
        sigs = seat.follow_signals(conn, date, min_rep_prob=min_rep_prob,
                                   exclude_limit_up=True, top=top)
    except Exception as exc:                                  # noqa: BLE001
        return {"ok": False, "reason": f"取候选失败：{exc}"}
    if not sigs:
        return {"ok": True, "date": date, "added": 0, "skipped": 0,
                "reason": "当日无候选"}

    now = datetime.now().isoformat(timespec="seconds")
    added = skipped = 0
    for s in sigs:
        code = s.get("code")
        if not code:
            continue
        # 已存在则只补 logged_at 之外的空字段，不覆盖已回填结果
        row = conn.execute(
            "SELECT 1 FROM signal_log WHERE signal_date=? AND code=?",
            (date, code)).fetchone()
        if row:
            skipped += 1
            continue
        conn.execute(
            "INSERT INTO signal_log (signal_date, code, name, seat, rep_prob,"
            " activity, reason, logged_at) VALUES (?,?,?,?,?,?,?,?)",
            (date, code, s.get("name"), s.get("seat"), s.get("rep_prob"),
             s.get("act"), (s.get("reason") or "")[:120], now))
        added += 1
    conn.commit()
    return {"ok": True, "date": date, "added": added,
            "skipped": skipped, "total": len(sigs),
            "min_rep_prob": min_rep_prob}


def _latest_seat_date(conn):
    try:
        return conn.execute("SELECT MAX(trade_date) FROM seat_trade"
                            ).fetchone()[0]
    except Exception:
        return None


# ------------------------------------------------------------------ 回填

def _limit_thr(code, name=None):
    """涨跌停阈值，与 seat.py 同口径（ST 是 5%，不是 9.5%）。"""
    try:
        return seat._limit_threshold(code, is_st=bool(name and "ST" in name))
    except Exception:
        from .collect import _limit_threshold
        return _limit_threshold(code, is_st=bool(name and "ST" in name))


def fill(conn, verbose=False):
    """用真实行情回填 signal_log 中尚未完成的行。

    只回填"未来已经发生"的部分：某行若还没到 D5，就只填 D1，
    剩下的等以后再来填。**绝不用未来数据伪装成当时就知道。**
    """
    dates = sorted({r[0] for r in conn.execute(
        "SELECT DISTINCT trade_date FROM quote_daily")})
    if not dates:
        return {"ok": False, "reason": "无行情数据"}
    di = {d: i for i, d in enumerate(dates)}
    px = {(r[0], r[1]): r[2] for r in conn.execute(
        "SELECT trade_date, code, close FROM quote_daily "
        "WHERE close IS NOT NULL")}
    lu = {(r[0], r[1]) for r in conn.execute(
        "SELECT trade_date, code FROM limit_up")}

    rows = conn.execute(
        "SELECT signal_date, code, name, entry_close, d1_ret, d5_ret, "
        "d10_ret, d1_locked FROM signal_log").fetchall()
    now = datetime.now().isoformat(timespec="seconds")
    n_d1 = n_d5 = n_d10 = n_locked = 0
    for r in rows:
        sd, code = r["signal_date"], r["code"]
        i = di.get(sd)
        if i is None:
            continue
        base = px.get((sd, code))
        if not base:
            continue
        upd, args = [], []
        # entry_close 只是收益基准，不是可成交价
        if r["entry_close"] is None:
            upd.append("entry_close=?")
            args.append(base)
        # D1
        if i + 1 < len(dates) and r["d1_ret"] is None:
            nd = dates[i + 1]
            c1 = px.get((nd, code))
            if c1:
                upd.append("d1_ret=?")
                args.append((c1 / base - 1) * 100)
                n_d1 += 1
        # d1_locked 单独判空：它可能已填而 d1_ret 未填（或反之）
        if i + 1 < len(dates) and r["d1_locked"] is None:
            nd = dates[i + 1]
            if px.get((nd, code)):
                upd.append("d1_locked=?")
                args.append(1 if (nd, code) in lu else 0)
                n_locked += 1
        # D5 / D10：只在"字段为空 且 未来确实已经发生"时才填。
        # 必须判空——否则每次 fill 都会把已有值重写一遍，
        # 既做无用功，又让"新填 N 条"的计数变成假的。
        for horizon, cnt_name, cur in ((5, "d5_ret", r["d5_ret"]),
                                       (10, "d10_ret", r["d10_ret"])):
            if cur is not None:
                continue
            j = i + horizon
            if j < len(dates):
                cc = px.get((dates[j], code))
                if cc:
                    upd.append(f"{cnt_name}=?")
                    args.append((cc / base - 1) * 100)
                    if horizon == 5:
                        n_d5 += 1
                    else:
                        n_d10 += 1
        if upd:
            upd.append("filled_at=?")
            args.append(now)
            args.extend([sd, code])
            conn.execute(
                f"UPDATE signal_log SET {', '.join(upd)} "
                f"WHERE signal_date=? AND code=?", args)
    conn.commit()
    return {"ok": True, "rows": len(rows), "filled_d1": n_d1,
            "filled_d5": n_d5, "filled_d10": n_d10,
            "locked_checked": n_locked}


# ------------------------------------------------------------------ 战绩

def track_record(conn, cost=None):
    """留痕战绩：看板历史候选的真实表现。

    分成三个口径，这是本模块存在的意义：
      · 全体（含买不进）
      · **剔除买不进**（真实可执行）
      · 净收益（扣成本）
    """
    cost = seat.total_cost() if cost is None else cost
    rows = [dict(r) for r in conn.execute(
        "SELECT * FROM signal_log ORDER BY signal_date DESC")]
    if not rows:
        return {"ok": True, "n": 0, "rows": []}

    def agg(sel, horizon):
        vals = [r[f"d{horizon}_ret"] for r in sel
                if r.get(f"d{horizon}_ret") is not None]
        if not vals:
            return None
        return {"n": len(vals), "mean": st.mean(vals),
                "median": st.median(vals),
                "win": sum(1 for v in vals if v > 0) / len(vals) * 100,
                "mean_net": st.mean(vals) - cost,
                "worst": min(vals), "best": max(vals)}

    filled = [r for r in rows if r.get("d1_ret") is not None]
    buyable = [r for r in filled if not r.get("d1_locked")]
    locked = [r for r in filled if r.get("d1_locked")]
    out = {
        "ok": True, "n": len(rows), "filled": len(filled),
        "locked_n": len(locked),
        "locked_share": (len(locked) / len(filled) * 100) if filled else None,
        "cost": cost, "rows": rows,
        "by_date": [],
    }
    for label, sel in (("all", filled), ("buyable", buyable)):
        out[label] = {f"d{h}": agg(sel, h) for h in (1, 5, 10)}
    # 按信号日聚合，看逐日是否稳定（单日重仓风险）
    for d in sorted({r["signal_date"] for r in filled}, reverse=True):
        g = [r for r in filled if r["signal_date"] == d]
        gb = [r for r in g if not r.get("d1_locked")]
        a = agg(gb, 1)
        out["by_date"].append({
            "date": d, "n": len(g), "locked": len(g) - len(gb),
            "d1_net": a["mean_net"] if a else None,
            "d1_win": a["win"] if a else None})
    return out