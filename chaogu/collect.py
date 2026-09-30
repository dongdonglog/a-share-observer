#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""采集编排。

**重要设计约束（踩过坑）**：
  只有同花顺的涨停池/炸板池/题材板块支持历史日期。
  东财的涨停/炸板/跌停池、板块排行、全市场快照、指数、外围
  **全部只有实时数据**——请求历史日期会静默返回当天数据。
  因此：
    - collect_daily(date) 在 date 不是今天时，自动跳过实时步骤
    - backfill() 只跑可历史回溯的步骤
  否则会把"今天"的数据写到过去的日期上，造成数据污染。

历史的价格/指数/外围通过 **日K接口** 回填（K线本来就有历史）。
"""

from __future__ import annotations

from datetime import datetime, timedelta

from . import config, db
from .sources import eastmoney as em
from .sources import news as nw
from .sources import ths

# 仅实时可用的步骤
REALTIME_STEPS = ("indexes", "limit_down", "sectors", "global",
                  "quotes", "news")
# 可历史回溯的步骤
HISTORY_STEPS = ("limit_up", "broken_board", "theme")


def today_str():
    return datetime.now().strftime("%Y-%m-%d")


def _now():
    return datetime.now().isoformat(timespec="seconds")


def is_today(date):
    return str(date) == today_str()


# ------------------------------------------------------------------ 历史步骤

def step_limit_up(conn, date):
    rows = ths.fetch_limit_up(date)
    n = db.upsert(conn, "limit_up", rows, ["trade_date", "code"])
    db.log(conn, date, "limit_up", "ok", n)
    return n


def step_broken(conn, date):
    rows = ths.fetch_broken(date)
    n = db.upsert(conn, "broken_board", rows, ["trade_date", "code"])
    db.log(conn, date, "broken_board", "ok", n)
    return n


def step_theme(conn, date, top=60):
    blocks = ths.fetch_theme_blocks(date, top=top)
    brows, srows = [], []
    for b in blocks:
        brows.append({
            "trade_date": date,
            "code": b.get("code"),
            "name": b.get("name"),
            "change_pct": b.get("change"),
            "limit_up_num": b.get("limit_up_num"),
            "continuous_num": b.get("continuous_plate_num"),
            "high": b.get("high"),
            "days": b.get("days"),
        })
        for s in b.get("stock_list") or []:
            srows.append({
                "trade_date": date,
                "tag": b.get("name"),
                "code": s.get("code"),
                "name": s.get("name"),
                "pct": s.get("pct"),
                "continue_num": s.get("continue_num"),
                "height_text": s.get("height_text"),
                "change_tag": s.get("change_tag"),
                "reason": s.get("reason"),
                "reason_info": s.get("reason_info"),
                "first_seal": s.get("first_seal"),
                "is_st": s.get("is_st"),
                "is_new": s.get("is_new"),
            })
    nb = db.upsert(conn, "theme_block", brows, ["trade_date", "code"])
    ns = db.upsert(conn, "theme_stock", srows, ["trade_date", "tag", "code"])
    db.log(conn, date, "theme", "ok", ns, f"blocks={nb}")
    return nb, ns


# ------------------------------------------------------------------ 实时步骤

def step_limit_down(conn, date):
    rows, _q = em.fetch_limit_down(date)
    for r in rows:
        r["trade_date"] = date
    n = db.upsert(conn, "limit_down", rows, ["trade_date", "code"])
    db.log(conn, date, "limit_down", "ok", n)
    return n


def step_quotes(conn, date, progress=None):
    rows = em.fetch_all_quotes(progress=progress)
    for r in rows:
        r["trade_date"] = date
    n = db.upsert_quotes(conn, rows)
    db.log(conn, date, "quotes", "ok", n, f"{len(rows)} 只")
    return n


def step_sectors(conn, date):
    rows = []
    for kind in ("concept", "industry"):
        for s in em.fetch_sectors(kind):
            s["trade_date"] = date
            rows.append(s)
    n = db.upsert(conn, "sector_daily", rows, ["trade_date", "code"])
    db.log(conn, date, "sectors", "ok", n)
    return n


def step_global(conn, date):
    rows = []
    for g in em.fetch_global():
        g["trade_date"] = date
        rows.append(g)
    n = db.upsert(conn, "global_market", rows, ["trade_date", "code"])
    db.log(conn, date, "global", "ok", n)
    return n


def step_turnover(conn, date):
    """用新浪指数接口补两市成交额。

    push2 被限流时东财全市场快照会失败，但新浪的指数接口是另一个
    域名，通常仍然可用。成交额 = 上证指数 + 深证综指。
    """
    from .sources import sina as sn
    amt = sn.fetch_market_turnover()
    if not amt:
        raise RuntimeError("新浪指数接口未返回成交额")
    with conn:
        conn.execute(
            "INSERT INTO daily_market (trade_date, amount, collected_at) "
            "VALUES (?,?,?) ON CONFLICT(trade_date) DO UPDATE SET "
            "amount=excluded.amount",
            (date, amt, _now()))
    db.log(conn, date, "turnover", "ok", 1, f"{amt / 1e8:.0f}亿")
    return amt


def step_news(conn, date=None):
    rows = nw.fetch_news(page_size=50)
    for r in rows:
        r["collected_at"] = _now()
    n = db.upsert(conn, "news", rows, ["id"])
    db.log(conn, date or today_str(), "news", "ok", n)
    return n


def step_indexes(conn, date):
    idx = em.fetch_indexes()
    sh = idx.get("上证指数") or {}
    cyb = idx.get("创业板指") or {}
    with conn:
        conn.execute(
            "INSERT INTO daily_market (trade_date, sh_close, sh_pct, "
            "cyb_close, cyb_pct, collected_at) VALUES (?,?,?,?,?,?) "
            "ON CONFLICT(trade_date) DO UPDATE SET "
            "sh_close=excluded.sh_close, sh_pct=excluded.sh_pct, "
            "cyb_close=excluded.cyb_close, cyb_pct=excluded.cyb_pct",
            (date, sh.get("close"), sh.get("pct"),
             cyb.get("close"), cyb.get("pct"), _now()))
    db.log(conn, date, "indexes", "ok", len(idx))
    return idx


# ------------------------------------------------------------------ 日K回填

def backfill_index_klines(conn, days=200):
    """用日K补齐指数历史，并汇总两市成交额。

    两市成交额 = 上证指数成交额 + 深证综指成交额（0.399106）。
    注意不能用深证成指（0.399001），它只覆盖 500 只成分股。
    """
    mapping = {"1.000001": "sh", "0.399006": "cyb"}
    filled = 0
    amounts = {}
    for secid, key in mapping.items():
        try:
            kl = em.fetch_kline(secid, limit=days)
        except Exception as e:                             # noqa: BLE001
            print(f"    指数 {secid} 失败: {str(e)[:60]}")
            continue
        for k in kl:
            col_c, col_p = f"{key}_close", f"{key}_pct"
            with conn:
                conn.execute(
                    f"INSERT INTO daily_market (trade_date, {col_c}, {col_p}) "
                    f"VALUES (?,?,?) ON CONFLICT(trade_date) DO UPDATE SET "
                    f"{col_c}=excluded.{col_c}, {col_p}=excluded.{col_p}",
                    (k["date"], k["close"], k["pct"]))
            filled += 1
            if key == "sh" and k.get("amount"):
                amounts[k["date"]] = k["amount"]
    # 深证综指成交额
    try:
        for k in em.fetch_kline("0.399106", limit=days):
            if k.get("amount") and k["date"] in amounts:
                amounts[k["date"]] += k["amount"]
    except Exception:                                      # noqa: BLE001
        pass
    for d, amt in amounts.items():
        with conn:
            conn.execute("UPDATE daily_market SET amount=? WHERE trade_date=? "
                         "AND (amount IS NULL OR amount<=0)", (amt, d))
    db.log(conn, "*", "index_klines", "ok", filled)
    return filled


def backfill_global_klines(conn, days=60):
    """用日K补齐外围市场历史（每个品种 1 个请求）。"""
    filled = 0
    for secid, name in config.GLOBAL_INDEXES:
        try:
            kl = em.fetch_kline(secid, limit=days)
        except Exception:                                  # noqa: BLE001
            continue
        for k in kl:
            with conn:
                conn.execute(
                    "INSERT INTO global_market "
                    "(trade_date, code, name, price, pct) VALUES (?,?,?,?,?) "
                    "ON CONFLICT(trade_date, code) DO UPDATE SET "
                    "price=excluded.price, pct=excluded.pct",
                    (k["date"], secid.split(".")[-1], name,
                     k["close"], k["pct"]))
            filled += 1
    db.log(conn, "*", "global_klines", "ok", filled)
    return filled


# ------------------------------------------------------------------ 汇总

def _limit_threshold(code, is_st=False):
    """按板块给出涨跌停阈值。

    ST 股单独处理：主板 ST 是 ±5%，创业板/科创板 ST 仍是 ±20%，
    北交所 ST 仍是 ±30%。早期版本忽略 ST，导致主板 ST 的 +5% 涨停
    被判为"未涨停"，进而混入"可执行"样本（实测 ST 的 D1 均值
    -1.38%，显著差于非 ST 的 -0.10%，属实质性污染）。
    经验验证：ST 记录的涨幅峰值出现在 5.0%（主板）与 20.0%（创业科创）。
    """
    c = str(code)
    if c.startswith(("8", "4", "92")):
        return 29.0
    if c.startswith(("30", "68")):
        return 19.5
    return 4.8 if is_st else 9.5


def derive_breadth(conn, date, save=True):
    """从 quote_daily 推导涨跌家数与跌停家数。

    **只在行情覆盖完整时才写**：
      历史价格回填是分批进行的，某些日期只有几百上千只股票。
      用残缺样本算"涨跌家数"会得到严重偏小的错值
      （实测未加守卫时把 09-29 写成"涨797跌505"，真实是几千家）。

    跌停家数**优先采用东财跌停池**（权威口径），
    只有该日没有池数据时才用涨跌幅阈值近似。
    """
    rows = conn.execute(
        "SELECT code, pct FROM quote_daily WHERE trade_date=?", (date,)
    ).fetchall()
    if not rows:
        return {"skipped": True, "reason": "无行情"}
    MIN_COVERAGE = 3000
    if len(rows) < MIN_COVERAGE:
        return {"skipped": True, "reason": f"覆盖不足({len(rows)})"}

    up = dn = flat = dt = 0
    for r in rows:
        p = r["pct"]
        if p is None:
            continue
        if p > 0:
            up += 1
        elif p < 0:
            dn += 1
        else:
            flat += 1
        if p <= -_limit_threshold(r["code"]):
            dt += 1

    has_pool = conn.execute(
        "SELECT 1 FROM limit_down WHERE trade_date=? LIMIT 1", (date,)
    ).fetchone()

    if save:
        with conn:
            if has_pool or dt == 0:
                conn.execute(
                    "UPDATE daily_market SET up_count=?, down_count=?, "
                    "flat_count=? WHERE trade_date=?", (up, dn, flat, date))
            else:
                conn.execute(
                    "UPDATE daily_market SET up_count=?, down_count=?, "
                    "flat_count=?, dt_count=? WHERE trade_date=?",
                    (up, dn, flat, dt, date))
    return {"up": up, "down": dn, "flat": flat, "dt": dt,
            "dt_from_pool": bool(has_pool)}


def _fill_market(conn, date):
    """从已入库明细汇总 daily_market 的计数类字段。"""
    def one(sql, *a):
        r = conn.execute(sql, a).fetchone()
        return r[0] if r and r[0] is not None else 0

    zt = one("SELECT COUNT(*) FROM limit_up WHERE trade_date=?", date)
    zb = one("SELECT COUNT(*) FROM broken_board WHERE trade_date=?", date)
    dt = one("SELECT COUNT(*) FROM limit_down WHERE trade_date=?", date)
    mx = one("SELECT COALESCE(MAX(height),0) FROM limit_up WHERE trade_date=?", date)
    lb = one("SELECT COUNT(*) FROM limit_up WHERE trade_date=? AND height>=2", date)
    amt = one("SELECT COALESCE(SUM(amount),0) FROM quote_daily WHERE trade_date=?", date)
    with conn:
        conn.execute(
            "INSERT INTO daily_market (trade_date, zt_count, zb_count, dt_count,"
            " max_height, lianban_count, amount, source, collected_at) "
            "VALUES (?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(trade_date) DO UPDATE SET "
            "zt_count=excluded.zt_count, zb_count=excluded.zb_count, "
            "dt_count=CASE WHEN excluded.dt_count>0 THEN excluded.dt_count "
            "              ELSE daily_market.dt_count END, "
            "max_height=excluded.max_height, "
            "lianban_count=excluded.lianban_count, "
            "amount=CASE WHEN excluded.amount>0 THEN excluded.amount "
            "            ELSE daily_market.amount END",
            (date, zt, zb, dt, mx, lb, amt, "collect", _now()))
    return zt, zb, dt, mx, lb


# ------------------------------------------------------------------ 编排

def _safe(conn, date, name, fn, *a, **kw):
    try:
        return fn(conn, date, *a, **kw)
    except Exception as e:                              # noqa: BLE001
        db.log(conn, date, name, "error", 0, f"{type(e).__name__}: {e}")
        print(f"    !! {name} 失败: {type(e).__name__}: {str(e)[:140]}")
        return None


def collect_daily(date=None, with_quotes=True, with_news=True, verbose=True,
                  force_realtime=False):
    """采集单个交易日。

    date 不是今天时，默认只跑可历史回溯的步骤；
    force_realtime=True 可强制跑实时步骤（仅在确认安全时使用）。
    """
    date = date or today_str()
    realtime = is_today(date) or force_realtime
    conn = db.init()
    try:
        if verbose:
            print(f"  [采集] {date}"
                  f"{'' if realtime else '（历史日：仅跑可回溯步骤）'}")
        _safe(conn, date, "limit_up", step_limit_up)
        _safe(conn, date, "broken_board", step_broken)
        _safe(conn, date, "theme", step_theme)
        if realtime:
            _safe(conn, date, "indexes", step_indexes)
            _safe(conn, date, "limit_down", step_limit_down)
            _safe(conn, date, "sectors", step_sectors)
            _safe(conn, date, "global", step_global)
            _safe(conn, date, "turnover", step_turnover)
            if with_quotes:
                _safe(conn, date, "quotes", step_quotes)
            if with_news:
                _safe(conn, date, "news", lambda c, d: step_news(c, d))
        _fill_market(conn, date)
        if realtime and with_quotes:
            derive_breadth(conn, date)
        return True
    finally:
        conn.close()


def trading_days(end=None, count=30):
    """近似交易日历：跳过周末（节假日由数据探测兜底）。"""
    end = end or today_str()
    d = datetime.strptime(end, "%Y-%m-%d")
    out = []
    while len(out) < count:
        if d.weekday() < 5:
            out.append(d.strftime("%Y-%m-%d"))
        d -= timedelta(days=1)
    return list(reversed(out))


def backfill(count=60, verbose=True, end=None):
    """历史回填（仅可回溯步骤 + 指数/外围日K）。"""
    days = trading_days(end=end, count=count)
    print(f"  回填 {len(days)} 个候选交易日（自动跳过无数据日）")
    conn = db.init()
    done = 0
    try:
        print("  补齐指数与外围历史（日K）…")
        backfill_index_klines(conn, days=400)
        backfill_global_klines(conn, days=max(count, 30))
        for d in days:
            has = conn.execute(
                "SELECT 1 FROM limit_up WHERE trade_date=? LIMIT 1",
                (d,)).fetchone()
            if has:
                if verbose:
                    print(f"    {d} 已有数据，跳过")
                continue
            total = ths.probe_date(d)
            if not total:
                if verbose:
                    print(f"    {d} 非交易日/无数据，跳过")
                continue
            if verbose:
                print(f"    {d} 采集… 涨停 {total} 只")
            _safe(conn, d, "limit_up", step_limit_up)
            _safe(conn, d, "broken_board", step_broken)
            _safe(conn, d, "theme", step_theme)
            _fill_market(conn, d)
            done += 1
        return done
    finally:
        conn.close()
