"""龙虎榜 / 席位数据的采集与入库。

设计取舍
--------
龙虎榜数据是**收盘后**披露的，所以它的用途不是"当日决策"，而是：
  1. 事后归因：某只票为什么涨/跌，谁在买
  2. 席位画像：哪些席位历史上真的赚钱（可样本外验证）
  3. 跟随信号：看到高品质席位买入后，次日跟随的期望收益

因此本模块不做"实时"采集，只做历史回填 + 每日增量。

关键字段（实测验证，勿臆测）
  billboard.D*_adj  上榜后 N 日涨幅，最近日期为 NULL
  seat_trade.rep_prob  东财的席位3日胜率；本地 46,923 条样本验证
                       与真实 D1 相关 +0.175，样本外秩相关 +0.504
                       注意：这个字段并非每个席位都有
"""

from __future__ import annotations

from . import db
from .sources import lhb

# 东财把"自然人/机构/沪股通"这类聚合标签混在席位列表里，
# 它们不是营业部，且实测都是强反向指标（D1 -4~-5%）。
# 聚合标签（不是具体营业部，而是把一类资金合并后的占位名）。
# 这些行没有"席位能力"可言，且历史表现显著为负，必须排除在排行与
# 跟随候选之外。经验：聚合标签的 D1 均值 -4% ~ -5%、胜率约 27%。
#
# ⚠️ 早期版本漏了 **机构专用**（本地数据中出现 6,400 次，是所有聚合
# 标签里最多的一个），导致它泄漏进报告 9.2 的净买入排行。
# 另补 沪深股通投资者（各 71 次）。
AGGREGATE_LABELS = {
    "自然人", "其他自然人", "中小投资者", "机构", "机构投资者",
    "机构专用",
    "沪股通", "深股通", "沪股通专用", "深股通专用",
    "沪股通投资者", "深股通投资者",
    "其他",
}


def is_aggregate(seat):
    """是否为聚合标签（而非具体营业部）。"""
    return (seat or "") in AGGREGATE_LABELS


def _f(v):
    return float(v) if v is not None else None


def step_billboard(conn, date, verbose=False):
    """采集某日龙虎榜个股明细。"""
    rows = lhb.fetch_billboard(date, date, max_pages=8)
    out = []
    for r in rows:
        out.append({
            "trade_date": (r.get("TRADE_DATE") or "")[:10],
            "code": r.get("SECURITY_CODE"),
            "name": r.get("SECURITY_NAME_ABBR"),
            "pct": _f(r.get("CHANGE_RATE")),
            "turnover": _f(r.get("TURNOVERRATE")),
            "reason": r.get("EXPLANATION"),
            "change_type": str(r.get("CHANGE_TYPE") or ""),
            "buy_amt": _f(r.get("BILLBOARD_BUY_AMT")),
            "sell_amt": _f(r.get("BILLBOARD_SELL_AMT")),
            "net_amt": _f(r.get("BILLBOARD_NET_AMT")),
            "explain": r.get("EXPLAIN"),
            "d1_adj": _f(r.get("D1_CLOSE_ADJCHRATE")),
            "d2_adj": _f(r.get("D2_CLOSE_ADJCHRATE")),
            "d5_adj": _f(r.get("D5_CLOSE_ADJCHRATE")),
            "d10_adj": _f(r.get("D10_CLOSE_ADJCHRATE")),
            "d20_adj": _f(r.get("D20_CLOSE_ADJCHRATE")),
            "d30_adj": _f(r.get("D30_CLOSE_ADJCHRATE")),
            "source": "eastmoney",
        })
    n = db.upsert(conn, "billboard", out, ["trade_date", "code", "reason"])
    if verbose:
        print(f"    龙虎榜 {len(out)} 条")
    return n


def step_seat_trade(conn, date, verbose=False):
    """采集某日席位买卖明细（买 + 卖）。"""
    out = []
    for side in ("buy", "sell"):
        try:
            rows = lhb.fetch_seat_detail(date, side, max_pages=6)
        except lhb.Throttled:
            if verbose:
                print(f"    席位 {side} 限流，跳过")
            continue
        for r in rows:
            out.append({
                "trade_date": (r.get("TRADE_DATE") or "")[:10],
                "seat": r.get("OPERATEDEPT_NAME"),
                "side": side,
                "code": r.get("SECURITY_CODE"),
                # 注意：RPT_BILLBOARD_DAILYDETAILSBUY/SELL 这两个报表
                # **不含股票名称字段**（实测字段只有 SECURITY_CODE /
                # SECUCODE 等）。这里保留读取以防接口后续补充，
                # 为空时由 _backfill_names() 从 billboard 回填。
                "name": r.get("SECURITY_NAME_ABBR"),
                "pct": _f(r.get("CHANGE_RATE")),
                "buy_amt": _f(r.get("BUY")),
                "sell_amt": _f(r.get("SELL")),
                "net_amt": _f(r.get("NET")),
                "rep_prob": _f(r.get("RISE_PROBABILITY_3DAY")),
                "rep_n": r.get("TOTAL_BUYER_SALESTIMES_3DAY"),
                "source": "eastmoney",
            })
    n = db.upsert(conn, "seat_trade", out,
                  ["trade_date", "seat", "side", "code"])
    _backfill_names(conn)
    if verbose:
        print(f"    席位明细 {len(out)} 条")
    return n


def _backfill_names(conn):
    """用 billboard（含名称）回填 seat_trade.name。

    席位明细接口不返回股票名称，所以名称必须从龙虎榜个股明细关联过来。
    放在这里而不是只在导入脚本里做，是为了让每日增量采集也能自动补全。
    """
    cur = conn.execute(
        "UPDATE seat_trade SET name = ("
        "  SELECT b.name FROM billboard b "
        "  WHERE b.code = seat_trade.code "
        "    AND b.trade_date = seat_trade.trade_date LIMIT 1) "
        "WHERE (name IS NULL OR name = '') "
        "  AND EXISTS (SELECT 1 FROM billboard b "
        "              WHERE b.code = seat_trade.code "
        "                AND b.trade_date = seat_trade.trade_date)")
    return cur.rowcount


def step_seat_daily(conn, date, verbose=False):
    """采集某日活跃席位榜。"""
    try:
        rows = lhb.fetch_active_seats(date, max_pages=4)
    except lhb.Throttled:
        if verbose:
            print("    活跃席位榜限流，跳过")
        return 0
    out = []
    for r in rows:
        pairs = lhb.parse_buy_stocks(r)
        out.append({
            "trade_date": (r.get("ONLIST_DATE") or "")[:10],
            "seat": r.get("OPERATEDEPT_NAME"),
            "appeared": (r.get("BUYER_APPEAR_NUM") or 0)
                        + (r.get("SELLER_APPEAR_NUM") or 0),
            "buyer_num": r.get("BUYER_APPEAR_NUM"),
            "seller_num": r.get("SELLER_APPEAR_NUM"),
            "buy_amt": _f(r.get("TOTAL_BUYAMT")),
            "sell_amt": _f(r.get("TOTAL_SELLAMT")),
            "net_amt": _f(r.get("TOTAL_NETAMT")),
            "buy_stocks": " ".join(c for c, _ in pairs),
            "buy_names": " ".join(n for _, n in pairs),
            "org_name": r.get("ORG_NAME_ABBR"),
            "source": "eastmoney",
        })
    n = db.upsert(conn, "seat_daily", out, ["trade_date", "seat"])
    if verbose:
        print(f"    活跃席位 {len(out)} 条")
    return n


def collect_day(conn, date, verbose=True):
    """采集某日全部龙虎榜数据。"""
    total = 0
    for name, fn in (("龙虎榜", step_billboard),
                     ("席位明细", step_seat_trade),
                     ("活跃席位", step_seat_daily)):
        try:
            total += fn(conn, date, verbose=verbose)
        except Exception as exc:
            if verbose:
                print(f"    {name} 失败: {exc}")
    return total


def backfill(conn, start, end, verbose=True):
    """按交易日回填。"""
    from .collect import trading_days
    # trading_days 的签名是 (end=None, count=30)，没有 (conn, start, end)。
    # 早期这里传了 3 个位置参数，导致 `run.py lhb --backfill` 必崩
    # （TypeError）。正确用法见 collect.backfill：用关键字调用。
    days = [r[0] for r in conn.execute(
        "SELECT DISTINCT trade_date FROM limit_up "
        "WHERE trade_date>=? AND trade_date<=? ORDER BY trade_date",
        (start, end))]
    if not days:
        days = trading_days(end=end,
                            count=max(1, (int(end[:4]) - int(start[:4]) + 1) * 250))
    done = 0
    for i, d in enumerate(days, 1):
        if verbose:
            print(f"  [{i}/{len(days)}] {d}", flush=True)
        done += collect_day(conn, d, verbose=False)
    return done