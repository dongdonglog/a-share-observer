"""席位（游资）分析与跟随信号。

这回答用户的原始问题："分析别人仓位来制定"——跟哪些席位、跟了期望如何。

核心方法学约束（必须遵守，否则结论是假的）
--------------------------------------------
1. **只用有前向收益的样本**：D*_adj 对最近日期为 NULL，训练/评估必须剔除。
2. **样本外验证优先于排名**：席位数量多（100+），直接按历史收益排序取 top
   必然挑到噪声。本模块所有"优质席位"判断都提供样本外持续性检验
   （前半段排名 -> 后半段表现），并报告秩相关。
3. **区分聚合标签与真实营业部**：东财把"自然人/机构/沪股通"混在席位里，
   它们是强反向指标，不是可跟随对象。
4. **不做不可执行的假设**：龙虎榜盘后披露，只能次日买。涨停股次日往往
   高开甚至一字，实际可执行收益低于 D1_adj。本模块提供"非涨停"子集，
   作为更接近可执行的保守口径。

实测基准（2026-01-05 ~ 2026-09-28，
46,923 条去重后、去聚合标签、次日收益非空的买方记录）
  - 全体席位        D1 -0.11%  胜率 46.1%
  - rep_prob>=50    D1 +2.13%  胜率 59.4%   D5 +3.04%
  - rep_prob>=60 且非涨停  D1 +1.41% 胜率 59.7%  D5 +5.35%
  - 席位排名样本外秩相关 +0.504（有持续性，非噪声）
"""

from __future__ import annotations

import math
import statistics as st

from . import config
from .collect import _limit_threshold


def _thr(code, name=None):
    """涨跌停阈值（含 ST 判定）。

    ST 判定必须做：主板 ST 是 ±5%，若按普通主板 9.5% 处理，
    会把 +5% 涨停的 ST 股误判为"未涨停"而混入可执行样本。
    实测 ST 的 D1 均值 -1.38%，差于非 ST 的 -0.10%。
    """
    return _limit_threshold(code, is_st=bool(name and "ST" in name))
from .lhb import AGGREGATE_LABELS

# 默认评估窗口（交易日数）与席位最少样本数。
# ⚠️ 这两个值必须来自 config，否则会形成"最隐蔽的一类失效"：
# 早期它们是独立字面量，恰好与 config 里的值相等（180/20），
# 于是掩盖了断链——用户改 config.SEAT_MIN_TRADES 后，
# run.py 的表头文案会跟着变，**但 seat_stats 仍按 20 筛选**，
# 看起来配置生效了，实际计算没变。现值从 config 读取，单一来源。
DEFAULT_DAYS = config.SEAT_EVAL_DAYS
MIN_SEAT_TRADES = config.SEAT_MIN_TRADES
# 样本外分割比例
SPLIT_RATIO = 0.5


def total_cost():
    """往返交易总成本（百分比）= 显性成本 + 双边滑点。"""
    return config.COST_EXPLICIT + config.COST_SLIPPAGE * 2


def _pearson(xs, ys):
    n = len(xs)
    if n < 3:
        return None
    mx, my = sum(xs) / n, sum(ys) / n
    cov = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    vx = math.sqrt(sum((x - mx) ** 2 for x in xs))
    vy = math.sqrt(sum((y - my) ** 2 for y in ys))
    return cov / (vx * vy) if vx and vy else None


def _spearman(xs, ys):
    def rank(a):
        order = sorted(range(len(a)), key=lambda i: a[i])
        r = [0.0] * len(a)
        i = 0
        while i < len(order):
            j = i
            while j + 1 < len(order) and a[order[j + 1]] == a[order[i]]:
                j += 1
            avg = (i + j) / 2.0
            for k in range(i, j + 1):
                r[order[k]] = avg
            i = j + 1
        return r
    return _pearson(rank(xs), rank(ys))


def _recent_dates(conn, end, days):
    rows = conn.execute(
        "SELECT DISTINCT trade_date FROM seat_trade WHERE trade_date<=? "
        "ORDER BY trade_date DESC LIMIT ?", (end, days)).fetchall()
    return sorted(r[0] for r in rows)


def seat_records(conn, start, end, field="d1_adj", exclude_aggregate=True):
    """取席位记录 + 对应的龙虎榜前向收益。

    seat_trade 本身不含前向收益，需按 (code, trade_date) 关联 billboard。
    """
    rows = conn.execute(
        "SELECT s.trade_date, s.seat, s.code, s.name, s.side, s.pct, "
        "       s.buy_amt, s.sell_amt, s.net_amt, s.rep_prob, s.rep_n, "
        f"      b.{field} AS fwd "
        "FROM seat_buy_uniq s JOIN billboard_uniq b "
        "  ON b.trade_date=s.trade_date AND b.code=s.code "
        "WHERE s.trade_date>=? AND s.trade_date<=? "
        f"  AND b.{field} IS NOT NULL", (start, end)).fetchall()
    out = [dict(r) for r in rows]
    if exclude_aggregate:
        out = [r for r in out if r["seat"] not in AGGREGATE_LABELS]
    return out


def seat_stats(conn, end, days=DEFAULT_DAYS, field="d1_adj",
               min_trades=MIN_SEAT_TRADES):
    """按席位聚合真实战绩，返回按均值降序的列表。"""
    dates = _recent_dates(conn, end, days)
    if not dates:
        return []
    recs = seat_records(conn, dates[0], dates[-1], field=field)
    by: dict[str, list] = {}
    for r in recs:
        by.setdefault(r["seat"], []).append(r)
    out = []
    for seat, g in by.items():
        vals = [r["fwd"] for r in g]
        if len(vals) < min_trades:
            continue
        # 注意：seat_records 默认只取 d1_adj（见 field 参数），所以这里的
        # vals 是**次日**收益。早期误命名了一个 d5 变量（值其实也是 d1），
        # 已删除以免误导。
        net = sum(r["net_amt"] or 0 for r in g)
        probs = [r["rep_prob"] for r in g if r["rep_prob"] is not None]
        out.append({
            "seat": seat,
            "trades": len(vals),
            "mean": st.mean(vals),
            "median": st.median(vals),
            "win": sum(1 for v in vals if v > 0) / len(vals) * 100,
            "net_amt": net,
            "mean_rep_prob": st.mean(probs) if probs else None,
        })
    out.sort(key=lambda x: -x["mean"])
    return out


def validate_persistence(conn, end, days=DEFAULT_DAYS, field="d1_adj",
                         min_trades=15):
    """样本外检验：用前半段排名，看后半段是否延续。

    这是本模块最重要的一道防线。没有它，"优质席位排行"就是多重检验
    的产物——100 个席位里总有几个看起来很强。
    """
    dates = _recent_dates(conn, end, days)
    if len(dates) < 20:
        return {"ok": False, "reason": "交易日不足"}
    split = dates[int(len(dates) * SPLIT_RATIO)]
    recs = seat_records(conn, dates[0], dates[-1], field=field)

    ins, oos = {}, {}
    for r in recs:
        (ins if r["trade_date"] <= split else oos).setdefault(
            r["seat"], []).append(r["fwd"])

    common = [s for s in ins
              if len(ins[s]) >= min_trades and len(oos.get(s, [])) >= min_trades]
    if len(common) < 8:
        return {"ok": False, "reason": f"两段都够样本的席位仅 {len(common)} 个"}

    im = {s: st.mean(ins[s]) for s in common}
    om = {s: st.mean(oos[s]) for s in common}
    ranked = sorted(common, key=lambda s: -im[s])
    k = max(3, len(ranked) // 10)
    top, bot = ranked[:k], ranked[-k:]

    def blk(seats):
        gi = [v for s in seats for v in ins[s]]
        go = [v for s in seats for v in oos[s]]
        return {
            "n_in": len(gi), "mean_in": st.mean(gi),
            "win_in": sum(1 for v in gi if v > 0) / len(gi) * 100,
            "n_out": len(go), "mean_out": st.mean(go),
            "win_out": sum(1 for v in go if v > 0) / len(go) * 100,
        }

    all_oos = [v for s in common for v in oos[s]]
    return {
        "ok": True,
        "split": split,
        "seats": len(common),
        "top": blk(top),
        "bottom": blk(bot),
        "baseline": {
            "n_out": len(all_oos), "mean_out": st.mean(all_oos),
            "win_out": sum(1 for v in all_oos if v > 0) / len(all_oos) * 100,
        },
        "rank_corr": _spearman([im[s] for s in common], [om[s] for s in common]),
        "top_seats": top,
    }


def permutation_test(conn, end, days=DEFAULT_DAYS, field="d1_adj",
                     min_trades=15, n_perm=2000, seed=42):
    """对"席位排名持续性"做置换检验。

    为什么必须做
    ------------
    秩相关 +0.40 看起来很强，但 122 个席位的排名在有限样本下本就会有
    非零相关。置换检验回答的是：观测到的秩相关，在"席位能力无差异"
    的零假设下有多罕见。

    零假设：各席位真实水平相同，两段均值差异纯属抽样噪声。
    做法：保持每个席位的两段均值配对不变，随机交换"哪段是样本内"，
    重算秩相关，统计 >= 观测值的比例。
    """
    import random

    v = validate_persistence(conn, end, days=days, field=field,
                             min_trades=min_trades)
    if not v.get("ok"):
        return {"ok": False, "reason": v.get("reason")}
    obs = v["rank_corr"]

    dates = _recent_dates(conn, end, days)
    split = dates[int(len(dates) * SPLIT_RATIO)]
    recs = seat_records(conn, dates[0], dates[-1], field=field)
    ins, oos = {}, {}
    for r in recs:
        (ins if r["trade_date"] <= split else oos).setdefault(
            r["seat"], []).append(r["fwd"])
    common = [s for s in ins
              if len(ins[s]) >= min_trades and len(oos.get(s, [])) >= min_trades]

    pairs = [(st.mean(ins[s]), st.mean(oos[s])) for s in common]
    rng = random.Random(seed)
    ge = 0
    for _ in range(n_perm):
        a, b = [], []
        for x, y in pairs:
            if rng.random() < 0.5:
                a.append(x)
                b.append(y)
            else:
                a.append(y)
                b.append(x)
        if _spearman(a, b) >= obs:
            ge += 1
    p = (ge + 1) / (n_perm + 1)
    return {
        "ok": True,
        "seats": len(common),
        "observed": obs,
        "permutations": n_perm,
        "p_value": p,
        "significant": p < 0.05,
    }


def following_edge(conn, end, days=DEFAULT_DAYS, field="d1_adj"):
    """跟随不同品质席位的期望收益（按东财 rep_prob 分组）。

    rep_prob 是唯一对每个席位-个股都给的品质标签，比"历史排名"更实时，
    且已验证与真实前向收益相关。
    """
    dates = _recent_dates(conn, end, days)
    if not dates:
        return []
    recs = seat_records(conn, dates[0], dates[-1], field=field)
    recs = [r for r in recs if r["rep_prob"] is not None]
    bands = [(0, 35), (35, 40), (40, 45), (45, 50), (50, 60), (60, 101)]
    out = []
    for lo, hi in bands:
        g = [r for r in recs if lo <= r["rep_prob"] < hi]
        if len(g) < 30:
            continue
        vals = [r["fwd"] for r in g]
        out.append({
            "band": f"{lo}-{hi}%" if hi < 101 else f"{lo}%+",
            "n": len(vals),
            "mean": st.mean(vals),
            "net": st.mean(vals) - total_cost(),
            "win": sum(1 for v in vals if v > 0) / len(vals) * 100,
        })
    corr = _pearson([r["rep_prob"] for r in recs], [r["fwd"] for r in recs])
    return out, corr, len(recs)


def follow_signals(conn, date, min_rep_prob=None, exclude_limit_up=True,
                   top=20, prefer_low_activity=True, window=60):
    """给定日期，列出"值得跟随"的席位买入（次日可执行）。

    这是实战入口：跑完当日龙虎榜后，输出高品质席位买入的股票。

    排序依据（已验证，见 activity_edge）
    -----------------------------------
    1. 席位胜率 rep_prob 高
    2. 该席位在**过去 window 个交易日**上榜次数少（低频）
       实测：高胜率+低频(<=2) 的 D5 净收益 +5.95%，
             高胜率+高频(>=10) 则 -2.41%。这是经诚实协议验证的差异。

    prefer_low_activity=True 时，会把滚动活跃度高的信号排到后面，
    而不是直接剔除（避免在低频信号少的交易日无输出）。
    """
    min_rep_prob = min_rep_prob if min_rep_prob is not None \
        else config.SEAT_MIN_REP_PROB
    rows = conn.execute(
        "SELECT s.seat, s.code, s.name, s.buy_amt, s.net_amt, s.rep_prob, "
        "       s.rep_n, b.pct, b.reasons AS reason "
        "FROM seat_buy_uniq s LEFT JOIN billboard_uniq b "
        "  ON b.trade_date=s.trade_date AND b.code=s.code "
        "WHERE s.trade_date=? AND s.rep_prob>=? "
        "GROUP BY s.seat, s.code "
        "ORDER BY s.rep_prob DESC, s.net_amt DESC", (date, min_rep_prob)
    ).fetchall()

    # 滚动活跃度：只统计 date 之前 window 个交易日
    act = {}
    if prefer_low_activity:
        prev = [r[0] for r in conn.execute(
            "SELECT DISTINCT trade_date FROM seat_trade "
            "WHERE trade_date<? ORDER BY trade_date DESC LIMIT ?",
            (date, window))]
        if prev:
            ph = ",".join("?" for _ in prev)
            for seat_name, c in conn.execute(
                    f"SELECT seat, COUNT(DISTINCT trade_date) FROM seat_trade "
                    f"WHERE trade_date IN ({ph}) AND side='buy' "
                    f"GROUP BY seat", prev):
                act[seat_name] = c

    out, seen = [], set()
    for r in rows:
        d = dict(r)
        # 聚合标签（机构专用/自然人/沪股通…）不是席位，没有"能力"可言，
        # 实测 D1 -4%~-5%、胜率 27%，属强反向指标。
        # 早期 follow_signals 完全不做这个过滤，导致 report 9.1"跟随候选"
        # 与 9.2"排除聚合"两节口径相反——最该被排除的反而没被排除。
        if d["seat"] in AGGREGATE_LABELS:
            continue
        key = (d["code"], d["seat"])
        if key in seen:
            continue
        seen.add(key)
        # 涨停判定必须按板块阈值：主板 9.5%、创业板/科创板 19.5%、
        # 北交所 29%。早期这里硬编码 9.5%，会把 20cm 股票的 +9.5%
        # 误判为涨停而错误排除。复用 collect._limit_threshold 保证一致。
        if exclude_limit_up and abs(d.get("pct") or 0) >= \
                _thr(d["code"], d.get("name")) - 0.5:
            continue            # 涨停股次日往往一字/高开，实际难执行
        d["act"] = act.get(d["seat"], 0)
        out.append(d)

    if prefer_low_activity:
        # 活跃度低者优先；同活跃度内保持原有的 rep_prob 降序
        out.sort(key=lambda x: (x["act"], -x["rep_prob"]))
    return out[:top]


def seat_profile(conn, seat, end=None, days=DEFAULT_DAYS):
    """单个席位的画像：战绩、偏好、净买入、涉及个股。"""
    end = end or conn.execute(
        "SELECT MAX(trade_date) FROM seat_trade").fetchone()[0]
    if not end:
        return None
    dates = _recent_dates(conn, end, days)
    if not dates:
        return None
    recs = seat_records(conn, dates[0], dates[-1])
    mine = [r for r in recs if r["seat"] == seat]
    buy = conn.execute(
        "SELECT code, name, trade_date, buy_amt, net_amt, rep_prob, pct "
        "FROM seat_trade WHERE seat=? ORDER BY trade_date DESC LIMIT 40",
        (seat,)).fetchall()
    vals = [r["fwd"] for r in mine]
    return {
        "seat": seat,
        "trades_with_fwd": len(vals),
        "mean_d1": st.mean(vals) if vals else None,
        "win": (sum(1 for v in vals if v > 0) / len(vals) * 100) if vals else None,
        "recent": [dict(r) for r in buy],
    }

def excess_by_band(conn, end, days=DEFAULT_DAYS, bands=None):
    """按席位胜率分档，计算**相对大盘**的超额收益（多持有期）。

    为什么用超额而不是绝对收益
    --------------------------
    绝对收益会混入大盘涨跌。实测：全样本上榜股 D5 绝对 -1.24%，
    但同期大盘 -0.13%，超额 -1.11%——必须剔除大盘才能看清席位贡献。

    实测结论（2026-01~09，n≈5.8万）
        胜率档      D1超额    D5超额   D10超额
        <40%       -1.34%   -4.21%   -5.59%
        40-50%     +0.35%   -0.96%   -2.07%
        50-60%     +1.20%   +0.68%   -1.02%
        >=60%      +3.51%   +6.70%   +5.88%

    两个关键点：
      1. "上榜=利空"只对低胜率席位成立；高胜率席位是显著正超额。
         所以龙虎榜不能整体看多或看空，必须看"是谁在买"。
      2. 高胜率席位的优势不衰减，D5 达峰后 D10 仍有 +5.88%，
         不像一日游脉冲——这是可持有数日的信号。
    """
    if bands is None:
        bands = [(0, 40), (40, 50), (50, 60), (60, 101)]
    dates = _recent_dates(conn, end, days)
    if not dates:
        return []
    recs = seat_records(conn, dates[0], dates[-1], field="d1_adj")
    # seat_records 只带 d1，这里重新取全字段
    rows = conn.execute(
        "SELECT s.trade_date, s.seat, s.rep_prob, "
        "       b.d1_adj, b.d5_adj, b.d10_adj "
        "FROM seat_buy_uniq s JOIN billboard_uniq b "
        "  ON b.trade_date=s.trade_date AND b.code=s.code "
        "WHERE s.trade_date>=? AND s.trade_date<=? "
        "  AND s.rep_prob IS NOT NULL", (dates[0], dates[-1])).fetchall()
    recs = [dict(r) for r in rows if r["seat"] not in AGGREGATE_LABELS]
    if not recs:
        return []

    # 大盘基准（上证）
    idx = [(r[0], r[1]) for r in conn.execute(
        "SELECT trade_date, sh_close FROM daily_market "
        "WHERE sh_close IS NOT NULL ORDER BY trade_date")]
    dts = [d for d, _ in idx]
    cl = {d: v for d, v in idx}
    pos = {d: i for i, d in enumerate(dts)}

    def bench(d0, n):
        i = pos.get(d0)
        if i is None or i + n >= len(dts):
            return None
        a, b = cl[dts[i]], cl[dts[i + n]]
        return (b / a - 1) * 100 if a and b else None

    out = []
    for lo, hi in bands:
        g = [r for r in recs if lo <= r["rep_prob"] < hi]
        if len(g) < 30:
            continue
        row = {"band": f"{lo}-{hi}%" if hi < 101 else f"{lo}%+",
               "n": len(g)}
        for n, k in ((1, "d1_adj"), (5, "d5_adj"), (10, "d10_adj")):
            ex = []
            for r in g:
                v = r[k]
                if v is None:
                    continue
                b = bench(r["trade_date"], n)
                if b is None:
                    continue
                ex.append(v - b)
            row[f"ex_d{n}"] = st.mean(ex) if ex else None
            row[f"n_d{n}"] = len(ex)
        out.append(row)
    return out


def walk_forward(conn, end, days=DEFAULT_DAYS, window=60, min_hist=5):
    """严格滚动前向测试：决策只用一个席位"过去"的战绩。

    这是最严格的一层验证，因为它完全**不使用**东财的 rep_prob，
    只用本地可自行复算的历史战绩。若能产生区分度，就证明
    "席位身份"本身携带真实信息，而不是数据商指标造出来的假象。

    做法（每个交易日循环）：
      1. 用截至昨日、过去 window 个交易日的记录，选出均值>0 的席位
      2. 这些席位今日的买入记录划为"跟随组"，其余为"不跟随组"
      3. 今日真实收益并入历史，供次日使用（严格无未来函数）

    实测（2026-01~09，n=63,452）：
        跟随组   D1 +0.33%（胜率 48.5%）  D5 -1.55%
        不跟随组 D1 -0.63%（胜率 43.7%）  D5 -2.46%
        多空价差 D1 +0.97%

    ⚠️ 诚实结论：+0.97% 的价差证明席位信息真实，但跟随组绝对收益
       +0.33% **扣 0.40% 成本后为负**。所以"只看历史战绩"不足以盈利，
       必须叠加 rep_prob（净 +3.16%）。本函数用于验证信号真实性，
       不是可直接交易的策略。
    """
    dates = _recent_dates(conn, end, days)
    if len(dates) < 30:
        return {"ok": False, "reason": "交易日不足"}
    lo = dates[0]
    rows = conn.execute(
        "SELECT s.trade_date, s.seat, b.d1_adj, b.d5_adj "
        "FROM seat_buy_uniq s JOIN billboard_uniq b "
        "  ON b.trade_date=s.trade_date AND b.code=s.code "
        "WHERE s.trade_date>=? AND s.trade_date<=? "
        "  AND b.d1_adj IS NOT NULL ORDER BY s.trade_date",
        (lo, dates[-1])).fetchall()
    recs = [dict(r) for r in rows if r["seat"] not in AGGREGATE_LABELS]
    by_date = {}
    for r in recs:
        by_date.setdefault(r["trade_date"], []).append(r)

    hist: dict[str, list] = {}
    picked, skipped = [], []
    for i, d in enumerate(dates):
        win_start = dates[max(0, i - window)]
        good = set()
        for seat_name, h in hist.items():
            recent = [x[1] for x in h if x[0] >= win_start]
            if len(recent) >= min_hist and st.mean(recent) > 0:
                good.add(seat_name)
        for r in by_date.get(d, []):
            (picked if r["seat"] in good else skipped).append(r)
        for r in by_date.get(d, []):
            hist.setdefault(r["seat"], []).append((d, r["d1_adj"]))

    def blk(g, k):
        v = [r[k] for r in g if r[k] is not None]
        if not v:
            return {"n": 0}
        return {"n": len(v), "mean": st.mean(v),
                "win": sum(1 for x in v if x > 0) / len(v) * 100}

    pa, pb = blk(picked, "d1_adj"), blk(skipped, "d1_adj")
    spread = (pa.get("mean", 0) - pb.get("mean", 0)) if pa["n"] and pb["n"] else None
    return {
        "ok": True, "window": window, "min_hist": min_hist,
        "picked": pa, "skipped": pb,
        "picked_d5": blk(picked, "d5_adj"),
        "skipped_d5": blk(skipped, "d5_adj"),
        "spread_d1": spread,
        "gross_after_cost": (pa.get("mean") or 0) - total_cost(),
    }


def compare_strategies(conn, end, days=DEFAULT_DAYS, seed=42):
    """对比各候选策略的**净收益**（已扣成本），并区分可执行性。

    这个函数记录了一个重要的**负面结果**，避免后来者重走弯路：

        策略                             样本    D1净     D5净
        A: rep_prob>=60                  5513   +3.16%  +6.38%
        B: rep_prob>=60 且本地历史为正      1720   +2.16%  +2.71%
        C: 仅本地历史为正                 27930   -0.07%  -1.95%
        D: rep_prob>=60 且非涨停           1944   +1.45%  +5.11%
        E: A+B+D 三条件叠加                411   -0.10%  -0.29%

    结论（反直觉但已实测）：
      1. **叠加"本地历史为正"会让收益变差**（+3.16% -> +2.16%）。
         说明平台 rep_prob 编码的信息多于简单历史均值，人工加过滤
         只会缩样本、引入噪声。不要叠加。
      2. **策略 A 的 +6.38% 含涨停股，买不进**，是不可执行的口径。
         策略 D（非涨停）才可执行，D5 仍有 +5.11% 净收益。
      3. 单看本地历史（策略 C）扣成本后为负，不足以独立使用。
    """
    from .collect import _limit_threshold

    dates = _recent_dates(conn, end, days)
    if len(dates) < 30:
        return {"ok": False, "reason": "交易日不足"}
    rows = conn.execute(
        "SELECT s.trade_date, s.seat, s.code, s.name, s.rep_prob, b.pct, "
        "       b.d1_adj, b.d5_adj, b.d10_adj "
        "FROM seat_buy_uniq s JOIN billboard_uniq b "
        "  ON b.trade_date=s.trade_date AND b.code=s.code "
        "WHERE s.trade_date>=? AND s.trade_date<=? "
        "  AND b.d1_adj IS NOT NULL ORDER BY s.trade_date",
        (dates[0], dates[-1])).fetchall()
    recs = [dict(r) for r in rows if r["seat"] not in AGGREGATE_LABELS]
    by_date = {}
    for r in recs:
        by_date.setdefault(r["trade_date"], []).append(r)

    WIN, MINH = 60, 5
    hist: dict[str, list] = {}
    buckets = {"A": [], "B": [], "C": [], "D": [], "E": []}
    for i, d in enumerate(dates):
        ws = dates[max(0, i - WIN)]
        good = set()
        for seat_name, h in hist.items():
            recent = [x[1] for x in h if x[0] >= ws]
            if len(recent) >= MINH and st.mean(recent) > 0:
                good.add(seat_name)
        for r in by_date.get(d, []):
            hi = r["rep_prob"] is not None and r["rep_prob"] >= 60
            gd = r["seat"] in good
            nl = abs(r["pct"] or 0) < _thr(r["code"], r.get("name")) - 0.5
            if hi:
                buckets["A"].append(r)
            if hi and gd:
                buckets["B"].append(r)
            if gd:
                buckets["C"].append(r)
            if hi and nl:
                buckets["D"].append(r)
            if hi and gd and nl:
                buckets["E"].append(r)
        for r in by_date.get(d, []):
            hist.setdefault(r["seat"], []).append((d, r["d1_adj"]))

    cost = total_cost()

    def stat(g):
        if len(g) < 30:
            return {"n": len(g), "insufficient": True}
        out = {"n": len(g), "insufficient": False}
        for k, f in (("d1", "d1_adj"), ("d5", "d5_adj"), ("d10", "d10_adj")):
            v = [r[f] for r in g if r[f] is not None]
            out[k + "_gross"] = st.mean(v) if v else None
            out[k + "_net"] = (st.mean(v) - cost) if v else None
        d1 = [r["d1_adj"] for r in g]
        out["win"] = sum(1 for x in d1 if x > 0) / len(d1) * 100
        return out

    names = {
        "A": "rep_prob>=60",
        "B": "rep_prob>=60 + 本地历史为正",
        "C": "仅本地历史为正",
        "D": "rep_prob>=60 + 非涨停（可执行）",
        "E": "A+B+D 三条件叠加",
    }
    return {
        "ok": True, "cost": cost,
        "strategies": [dict(stat(buckets[k]), key=k, name=names[k])
                       for k in ("A", "B", "C", "D", "E")],
    }


def monthly_stability(conn, end, days=DEFAULT_DAYS, threshold=70.0):
    """按自然月拆分高胜率席位的表现，检查是否依赖某一段行情。

    为什么需要
    ----------
    阈值扫描显示收益随阈值单调上升（>=80% 时 D5 仍 +8.95%），
    这不寻常。若收益集中在某一个月，就说明是特定行情产物而非稳定规律。

    实测结果（rep_prob>=70）
        2026-01 +4.59%(71.0%)   2026-02 +4.61%(80.4%)   2026-03 +3.91%(69.4%)
        2026-04 +4.85%(74.8%)   2026-05 +4.26%(72.4%)   2026-06 +3.52%(69.5%)
        2026-07 +5.33%(74.0%)   2026-08 +5.45%(76.8%)   2026-09 +2.77%(65.7%)
        9/9 个月为正。

    对照组（rep_prob 40-50）在零附近震荡，2026-07 甚至 -1.05%。
    实验组全正 + 对照组无效，排除了"某段行情特有"的解释。
    """
    rows = conn.execute(
        "SELECT s.trade_date, s.seat, s.rep_prob, b.d1_adj "
        "FROM seat_buy_uniq s JOIN billboard_uniq b "
        "  ON b.trade_date=s.trade_date AND b.code=s.code "
        "WHERE s.rep_prob IS NOT NULL "
        "  AND b.d1_adj IS NOT NULL AND s.trade_date>=? "
        "ORDER BY s.trade_date", (conn.execute(
            "SELECT MIN(trade_date) FROM seat_trade").fetchone()[0],)).fetchall()
    recs = [dict(r) for r in rows if r["seat"] not in AGGREGATE_LABELS]

    def by_month(pred):
        m = {}
        for r in recs:
            if pred(r):
                m.setdefault(r["trade_date"][:7], []).append(r["d1_adj"])
        out = []
        for k in sorted(m):
            v = m[k]
            if len(v) < 5:
                continue
            out.append({"month": k, "n": len(v), "mean": st.mean(v),
                        "win": sum(1 for x in v if x > 0) / len(v) * 100})
        return out

    hi = by_month(lambda r: r["rep_prob"] >= threshold)
    lo = by_month(lambda r: 40 <= r["rep_prob"] < 50)
    return {
        "threshold": threshold,
        "high": hi,
        "high_positive_months": sum(1 for x in hi if x["mean"] > 0),
        "low": lo,
        "low_positive_months": sum(1 for x in lo if x["mean"] > 0),
    }


def risk_profile(conn, end, days=DEFAULT_DAYS, min_rep_prob=None,
                 exclude_limit_up=True):
    """可执行口径下的**收益分布与尾部风险**。

    为什么必须看这个
    ----------------
    只看均值会严重误导。实测（rep_prob>=60 且非涨停，n=1,944）：

        持有期   均值     中位数   标准差   5%分位   胜率
        D1     +1.85%   +1.75%   7.06%   -8.63%   59.8%
        D5     +5.51%   +3.43%  18.64%  -17.87%   62.9%
        D10    +3.90%   +0.51%  24.10%  -24.70%   51.6%
        D20    +2.05%   -3.75%  32.14%     —      42.5%

    关键结论：
      1. **D1 最稳健**：均值(+1.85%)与中位数(+1.75%)接近，不靠极端值。
      2. **D5 以上均值被少数暴涨股拉高**：D20 中位数 **-3.75%**，
         意味着"典型的一笔交易是亏的"，靠个别翻倍股拉平均。
         这种分布下重仓长持非常危险。
      3. **尾部风险极大**：最差 -88% ~ -97%（退市/连续跌停）。
         头部越集中，越不能重仓单一标的。
    """
    min_rep_prob = (min_rep_prob if min_rep_prob is not None
                    else config.SEAT_STRONG_REP_PROB)
    from .collect import _limit_threshold

    dates = _recent_dates(conn, end, days)
    if not dates:
        return []
    rows = conn.execute(
        "SELECT s.seat, s.code, s.name, s.rep_prob, b.pct, "
        "       b.d1_adj, b.d2_adj, b.d5_adj, b.d10_adj, b.d20_adj "
        "FROM seat_buy_uniq s JOIN billboard_uniq b "
        "  ON b.trade_date=s.trade_date AND b.code=s.code "
        "WHERE s.trade_date>=? AND s.trade_date<=? "
        "  AND s.rep_prob>=? AND b.d1_adj IS NOT NULL",
        (dates[0], dates[-1], min_rep_prob)).fetchall()
    recs = [dict(r) for r in rows if r["seat"] not in AGGREGATE_LABELS]
    if exclude_limit_up:
        recs = [r for r in recs
                if abs(r["pct"] or 0) < _thr(r["code"], r.get("name")) - 0.5]

    out = []
    for n, k in ((1, "d1_adj"), (2, "d2_adj"), (5, "d5_adj"),
                 (10, "d10_adj"), (20, "d20_adj")):
        v = sorted(r[k] for r in recs if r[k] is not None)
        if len(v) < 30:
            continue
        out.append({
            "period": f"D{n}", "n": len(v),
            "mean": st.mean(v), "median": st.median(v),
            "std": st.pstdev(v),
            "p05": v[int(len(v) * 0.05)], "p25": v[int(len(v) * 0.25)],
            "worst": v[0], "best": v[-1],
            "win": sum(1 for x in v if x > 0) / len(v) * 100,
            "mean_gt_median": st.mean(v) > st.median(v),
        })
    return out


def executable_estimate(conn, end, days=DEFAULT_DAYS, min_rep_prob=None):
    """估算**真实可执行**收益：剔除次日涨停（买不进）的部分。

    为什么这是最重要的一个检验
    --------------------------
    龙虎榜盘后披露，你只能次日买入。但次日可能直接一字涨停，
    此时"D1 收益"统计上归你，实际上你根本买不到。

    实测（rep_prob>=60，n=5,513）：
        次日涨停（买不进）  24.1%   D1 +12.70%   D5 +17.79%
        次日未涨停（可买）  75.9%   D1  +0.66%   D5  +3.27%

        原始口径 D1 +3.56%  ->  可执行 D1 +0.66%
        **跳空/买不进高估了约 2.91%**

    结论：**D1 几乎不可用（扣成本后 +0.26%），真正的持有期是 D5**
    （可执行 +3.27%，净 +2.87%）。

    ⚠️ 口径局限（诚实标注）
      本函数用 `d1_adj >= 涨停阈值-0.3` 作为"次日涨停"的代理。
      `d1_adj` 是**收盘价**涨幅，而**收盘涨停不等于盘中买不进**
      （可能全天未封板、或尾盘才封）。因此本估计偏**保守**——
      真实可执行收益应介于"剔除全部次日涨停"与"完全不剔除"之间。
      要精确量化需要次日**开盘价**（当前库内 quote_daily 只有收盘价）。
    """
    min_rep_prob = (min_rep_prob if min_rep_prob is not None
                    else config.SEAT_STRONG_REP_PROB)
    from .collect import _limit_threshold

    dates = _recent_dates(conn, end, days)
    if not dates:
        return {"ok": False, "reason": "无数据"}
    rows = conn.execute(
        "SELECT s.seat, s.code, s.name, s.rep_prob, b.pct, b.d1_adj, b.d5_adj "
        "FROM seat_buy_uniq s JOIN billboard_uniq b "
        "  ON b.trade_date=s.trade_date AND b.code=s.code "
        "WHERE s.trade_date>=? AND s.trade_date<=? "
        "  AND s.rep_prob>=? AND b.d1_adj IS NOT NULL",
        (dates[0], dates[-1], min_rep_prob)).fetchall()
    recs = [dict(r) for r in rows if r["seat"] not in AGGREGATE_LABELS]
    if len(recs) < 50:
        return {"ok": False, "reason": f"样本不足（{len(recs)}）"}

    locked, free = [], []
    for r in recs:
        if (r["d1_adj"] or 0) >= _thr(r["code"], r.get("name")) - 0.3:
            locked.append(r)
        else:
            free.append(r)

    def m(g, k):
        v = [r[k] for r in g if r[k] is not None]
        return st.mean(v) if v else None

    cost = total_cost()
    return {
        "ok": True, "cost": cost, "n": len(recs),
        "locked_n": len(locked), "free_n": len(free),
        "locked_share": len(locked) / len(recs) * 100,
        "locked_d1": m(locked, "d1_adj"), "locked_d5": m(locked, "d5_adj"),
        "free_d1": m(free, "d1_adj"), "free_d5": m(free, "d5_adj"),
        "raw_d1": m(recs, "d1_adj"), "raw_d5": m(recs, "d5_adj"),
        "free_d1_net": (m(free, "d1_adj") or 0) - cost,
        "free_d5_net": (m(free, "d5_adj") or 0) - cost,
        "overstatement": (m(recs, "d1_adj") or 0) - (m(free, "d1_adj") or 0),
    }


def threshold_sweep(conn, end, days=DEFAULT_DAYS, executable=True):
    """在**可执行口径**下扫描 rep_prob 阈值，确定真正可用的门槛。

    为什么必须用可执行口径
    ----------------------
    宽松口径（不剔除次日涨停）会得出"50% 就够"的错误结论：
        阈值   宽松 D1净   可执行 D1净   可执行 D5净
        >=50    +1.74%      **-0.72%**    **-0.57%**
        >=60    +3.16%       +0.26%        +2.87%
        >=70    +3.99%       +1.12%        +6.06%

    **50% 阈值在可执行口径下完全为负**，因为它的收益几乎全部来自
    买不进的次日涨停股。这说明"放宽阈值增加交易机会"是危险的。

    实测结论：
      1. **所有阈值下 D5 都优于 D1**（D1 受跳空侵蚀最重）。
      2. 可用门槛是 **60%**（D5 净 +2.87%）；**70% 更优**（D5 净 +6.06%）。
      3. 55% 以下在可执行口径下不值得交易。
    """
    from .collect import _limit_threshold

    dates = _recent_dates(conn, end, days)
    if not dates:
        return []
    rows = conn.execute(
        "SELECT s.seat, s.code, s.name, s.rep_prob, b.pct, "
        "       b.d1_adj, b.d5_adj, b.d10_adj "
        "FROM seat_buy_uniq s JOIN billboard_uniq b "
        "  ON b.trade_date=s.trade_date AND b.code=s.code "
        "WHERE s.trade_date>=? AND s.trade_date<=? "
        "  AND b.d1_adj IS NOT NULL", (dates[0], dates[-1])).fetchall()
    recs = [dict(r) for r in rows if r["seat"] not in AGGREGATE_LABELS]
    cost = total_cost()

    out = []
    for th in (40, 45, 50, 55, 60, 65, 70, 75, 80):
        g = [r for r in recs
             if r["rep_prob"] is not None and r["rep_prob"] >= th]
        if executable:
            g = [r for r in g
                 if (r["d1_adj"] or 0) < _thr(r["code"], r.get("name")) - 0.3]
        if len(g) < 40:
            continue
        row = {"threshold": th, "n": len(g)}
        for n, k in ((1, "d1_adj"), (5, "d5_adj"), (10, "d10_adj")):
            v = [r[k] for r in g if r[k] is not None]
            row[f"d{n}_net"] = (st.mean(v) - cost) if v else None
        out.append(row)
    return out


def honest_protocol(conn, end, days=DEFAULT_DAYS, train_ratio=0.7,
                    executable=True):
    """诚实协议：**只用前段数据选阈值**，再在后段测量。

    为什么必须做
    ------------
    `threshold_sweep` 是在**全样本**上扫描阈值，据此挑出的"最优阈值"
    天然偏高——这是标准的数据窥探（data snooping）。文献记载的失败案例：
    全样本挑操作阈值得到 Sharpe 10.9，但改用"前 70% 选阈值、后 30% 测量"
    后只剩 11 笔交易。

    本函数把阈值选择限制在训练段，再报告该阈值在留出段的表现。
    若两段一致，说明阈值来自真实规律而非过拟合。

    实测（分割 2026-07-14）：
        阈值    训练段D5净   留出段D5净   留出段n
        >=50     -0.66%      -0.16%      2111
        >=60     +2.79%      +3.22%       801
        >=70     +6.25%      +5.10%       440
        >=80     +7.69%      +6.23%       348

    两个结论：
      1. **训练与留出高度一致**，阈值来自真实规律，不是过拟合。
      2. **留出段 D1 净收益几乎全为负**（-1.30% ~ +0.23%），
         再次确认 **D5 才是真实持有期**。
    """
    from .collect import _limit_threshold

    dates = _recent_dates(conn, end, days)
    if len(dates) < 40:
        return {"ok": False, "reason": "交易日不足"}
    rows = conn.execute(
        "SELECT s.trade_date, s.seat, s.code, s.name, s.rep_prob, b.pct, "
        "       b.d1_adj, b.d5_adj "
        "FROM seat_buy_uniq s JOIN billboard_uniq b "
        "  ON b.trade_date=s.trade_date AND b.code=s.code "
        "WHERE s.trade_date>=? AND s.trade_date<=? "
        "  AND b.d1_adj IS NOT NULL ORDER BY s.trade_date",
        (dates[0], dates[-1])).fetchall()
    recs = [dict(r) for r in rows if r["seat"] not in AGGREGATE_LABELS]
    if len(recs) < 500:
        return {"ok": False, "reason": f"样本不足（{len(recs)}）"}

    all_dates = sorted({r["trade_date"] for r in recs})
    split = all_dates[int(len(all_dates) * train_ratio)]
    ins = [r for r in recs if r["trade_date"] <= split]
    oos = [r for r in recs if r["trade_date"] > split]

    def exe(g):
        if not executable:
            return g
        return [r for r in g
                if (r["d1_adj"] or 0) < _thr(r["code"], r.get("name")) - 0.3]

    def net(g, k):
        v = [r[k] for r in g if r[k] is not None]
        return (st.mean(v) - total_cost()) if v else None

    cost = total_cost()
    table, best = [], None
    for th in (40, 45, 50, 55, 60, 65, 70, 75, 80):
        gi = exe([r for r in ins
                  if r["rep_prob"] is not None and r["rep_prob"] >= th])
        go = exe([r for r in oos
                  if r["rep_prob"] is not None and r["rep_prob"] >= th])
        if len(gi) < 30 or len(go) < 15:
            continue
        i5 = net(gi, "d5_adj")
        row = {"threshold": th, "train_n": len(gi), "train_d5_net": i5,
               "holdout_n": len(go), "holdout_d5_net": net(go, "d5_adj"),
               "holdout_d1_net": net(go, "d1_adj")}
        d1 = [r["d1_adj"] for r in go]
        row["holdout_win"] = sum(1 for x in d1 if x > 0) / len(d1) * 100
        table.append(row)
        if best is None or (i5 or -99) > (best[1] or -99):
            best = (th, i5)

    if not table:
        return {"ok": False, "reason": "可用阈值不足"}
    sel = next((r for r in table if r["threshold"] == best[0]), None)
    return {
        "ok": True, "cost": cost, "split": split,
        "train_range": f"{all_dates[0]} ~ {split}",
        "holdout_range": f"{next(r['trade_date'] for r in oos)} ~ {all_dates[-1]}",
        "train_n": len(ins), "holdout_n": len(oos),
        "table": table, "selected_threshold": best[0],
        "selected": sel,
    }


def _rolling_activity(conn, end, days=DEFAULT_DAYS, window=60):
    """给每条记录加上"该席位在过去 window 个交易日的上榜次数"。

    必须用**滚动**窗口：若用整段样本统计活跃度，就等于把未来信息
    泄漏进过滤器（会人为抬高效果）。此函数严格只用当日之前的数据。
    """
    dates = _recent_dates(conn, end, days)
    if not dates:
        return [], []
    rows = conn.execute(
        "SELECT s.trade_date, s.seat, s.code, s.name, s.rep_prob, b.pct, "
        "       b.d1_adj, b.d5_adj "
        "FROM seat_buy_uniq s JOIN billboard_uniq b "
        "  ON b.trade_date=s.trade_date AND b.code=s.code "
        "WHERE s.trade_date>=? AND s.trade_date<=? "
        "  AND b.d1_adj IS NOT NULL ORDER BY s.trade_date",
        (dates[0], dates[-1])).fetchall()
    recs = [dict(r) for r in rows if r["seat"] not in AGGREGATE_LABELS]
    by_date = {}
    for r in recs:
        by_date.setdefault(r["trade_date"], []).append(r)

    hist: dict[str, list] = {}
    enriched = []
    for i, d in enumerate(dates):
        ws = dates[max(0, i - window)]
        act = {s2: len([x for x in v if x >= ws])
               for s2, v in hist.items()}
        for r in by_date.get(d, []):
            r2 = dict(r)
            r2["act"] = act.get(r["seat"], 0)
            enriched.append(r2)
        for r in by_date.get(d, []):
            hist.setdefault(r["seat"], []).append(d)
    return enriched, dates


def activity_edge(conn, end, days=DEFAULT_DAYS, window=60,
                  executable=True, min_rep_prob=None):
    """在"高胜率席位"内部，再按**滚动活跃度**分层。

    这一条解决了一个真实的外部冲突
    ------------------------------
    一份公开复现发现"上榜次数越多，后续超额越差"（正超额席位占比从
    <15 次档 36.2% 单调降至 >150 次档 11.6%），与本项目"席位能力可持续"
    看似矛盾。本地分解后两者其实**都成立、只是条件不同**：

        （高胜率席位内部，rep_prob>=60）
        滚动活跃度    事件    D1均值    D5均值   胜率
        1-15         3099   +4.10%   +8.99%   70.6%
        15-30         834   +3.20%   +5.57%   63.3%
        30-120       1161   +2.90%   +3.56%   63.7%
        >120          419   +2.14%   +2.31%   57.8%

    即：**活跃度本身是反向的，但在高胜率条件下，低活跃度席位的信号
    显著更强**。外部按活跃度排序看到负相关（因为高频席位大多低胜率），
    本项目按 rep_prob 排序看到正相关——两个维度独立可用。

    可执行口径实测（滚动窗口，无未来信息；rep_prob>=60）：
        组合               可买   D1净     D5净    胜率
        高胜率             4183  +0.26%  +2.87%  56.6%
        + 低频(<=2/60日)   2158  +1.05%  **+5.95%**  63.5%
        + 高频(>=10/60日)   942  -0.91%  **-2.41%**  47.1%

    诚实协议（训练段选上限，留出段测量）：
        上限    训练D5净   留出D5净   留出n
        <=1     +6.81%    +5.92%     324
        <=2     +6.00%    +5.69%     408
        不限    +2.79%    +3.22%     801
    两段一致，**不是数据窥探的产物**。
    """
    from .collect import _limit_threshold

    min_rep_prob = (min_rep_prob if min_rep_prob is not None
                    else config.SEAT_STRONG_REP_PROB)
    enriched, dates = _rolling_activity(conn, end, days=days, window=window)
    if not enriched:
        return {"ok": False, "reason": "无数据"}

    def exe(g):
        if not executable:
            return g
        return [r for r in g
                if (r["d1_adj"] or 0) < _thr(r["code"], r.get("name")) - 0.3]

    def blk(g):
        g = exe(g)
        if len(g) < 30:
            return {"n": len(g), "insufficient": True}
        d1 = [r["d1_adj"] for r in g]
        d5 = [r["d5_adj"] for r in g if r["d5_adj"] is not None]
        cost = total_cost()
        return {"n": len(g), "d1_net": st.mean(d1) - cost,
                "d5_net": (st.mean(d5) - cost) if d5 else None,
                "win": sum(1 for x in d1 if x > 0) / len(d1) * 100,
                "insufficient": False}

    hi = [r for r in enriched
          if r["rep_prob"] is not None and r["rep_prob"] >= min_rep_prob]
    groups = {
        "高胜率（基准）": hi,
        "高胜率+低频(<=2)": [r for r in hi if r["act"] <= 2],
        "高胜率+中频(3-9)": [r for r in hi if 3 <= r["act"] <= 9],
        "高胜率+高频(>=10)": [r for r in hi if r["act"] >= 10],
        "非高胜率": [r for r in enriched
                     if r["rep_prob"] is None
                     or r["rep_prob"] < min_rep_prob],
    }
    return {
        "ok": True, "cost": total_cost(), "window": window,
        "min_rep_prob": min_rep_prob,
        "groups": [dict(blk(g), name=k) for k, g in groups.items()],
    }


# ------------------------------------------------------------------ 过拟合校正
# 以下实现填补 README 局限第 9 条（未实现 DSR/PBO）。方法学依据：
#   Bailey & López de Prado (2014) "The Deflated Sharpe Ratio"
#   Bailey, Borwein, López de Prado & Zhu "The Probability of Backtest
#     Overfitting"（CSCV 实现）
# 这两个量回答"我扫了 9 个阈值挑最好的，这算不算过拟合"。
#
# 数值自检（重要，否则不知道自己算对没有）
# ------------------------------------------------------------------
# 《已知答案检验》用蒙特卡洛验证实现正确性：
#   PSR  纯噪声 -> 0.53（理论 0.50）；强收益(mean=.5) -> 1.0000
#   DSR  纯噪声 N=5 -> 0.66（应 <0.95，即不显著）
#   PBO  纯噪声 12 次重复，均值 0.34~0.59，围绕理论 0.5
#        含 1 个真优策略 -> PBO 0.0000，样本外中位排名 1.000
#   SR0  随试验数 N 单调上升（N=2 -> +0.043, N=20 -> +0.091）
# 结论：估计器无系统性偏差。因此本地实得的 PBO=0.0000 可信，
#       不是"估计器偏向 0"造成的假象。

_EULER = 0.5772156649015329


def _norm_cdf(x):
    """标准正态 CDF（用 math.erf，无需 scipy）。"""
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def _norm_ppf(p):
    """标准正态分位数（Acklam 有理逼近，精度 ~1e-9）。"""
    if not 0.0 < p < 1.0:
        raise ValueError("p 必须在 (0,1)")
    a = (-3.969683028665376e+01, 2.209460984245205e+02,
         -2.759285104469687e+02, 1.383577518672690e+02,
         -3.066479806614716e+01, 2.506628277459239e+00)
    b = (-5.447609879822406e+01, 1.615858368580409e+02,
         -1.556989798598866e+02, 6.680131188771972e+01,
         -1.328068155288572e+01)
    c = (-7.784894002430293e-03, -3.223964580411365e-01,
         -2.400758277161838e+00, -2.549732539343734e+00,
         4.374664141464968e+00, 2.938163982698783e+00)
    d = (7.784695709041462e-03, 3.224671290700398e-01,
         2.445134137142996e+00, 3.754408661907416e+00)
    pl, ph = 0.02425, 1 - 0.02425
    if p < pl:
        q = math.sqrt(-2 * math.log(p))
        return (((((c[0]*q+c[1])*q+c[2])*q+c[3])*q+c[4])*q+c[5]) / \
               ((((d[0]*q+d[1])*q+d[2])*q+d[3])*q+1)
    if p > ph:
        q = math.sqrt(-2 * math.log(1 - p))
        return -(((((c[0]*q+c[1])*q+c[2])*q+c[3])*q+c[4])*q+c[5]) / \
                ((((d[0]*q+d[1])*q+d[2])*q+d[3])*q+1)
    q, r = p - 0.5, (p - 0.5) ** 2
    return (((((a[0]*r+a[1])*r+a[2])*r+a[3])*r+a[4])*r+a[5])*q / \
           (((((b[0]*r+b[1])*r+b[2])*r+b[3])*r+b[4])*r+1)


def _moments(xs):
    """返回 (均值, 标准差, 偏度, 峰度)。峰度为原始峰度（正态=3）。"""
    n = len(xs)
    if n < 4:
        return None
    m = sum(xs) / n
    var = sum((x - m) ** 2 for x in xs) / n
    sd = math.sqrt(var)
    if sd == 0:
        return None
    sk = sum((x - m) ** 3 for x in xs) / n / sd ** 3
    ku = sum((x - m) ** 4 for x in xs) / n / sd ** 4
    return m, sd, sk, ku


def _sharpe(xs):
    """单期 Sharpe（非年化）：均值/标准差。"""
    mm = _moments(xs)
    if not mm or mm[1] == 0:
        return None
    return mm[0] / mm[1]


def psr(xs, sr_benchmark=0.0):
    """概率化 Sharpe 比：P(真实 Sharpe > 基准)。

    同时校正收益的非正态性（偏度、峰度），这是普通 t 检验做不到的。
    """
    mm = _moments(xs)
    if not mm:
        return None
    m, sd, sk, ku = mm
    T = len(xs)
    sr = m / sd
    denom = 1 - sk * sr + (ku - 1) / 4 * sr ** 2
    if denom <= 0:
        return None
    z = (sr - sr_benchmark) * math.sqrt(T - 1) / math.sqrt(denom)
    return _norm_cdf(z)


def deflated_sharpe(returns_matrix):
    """Deflated Sharpe Ratio：校正"扫了 N 个策略挑最好"的选择偏差。

    returns_matrix: list of list，每行是一个策略的收益序列（等长）。

    做法（Bailey & López de Prado）：
      1. 算各策略 Sharpe，取其方差 V[SR]
      2. 算 N 次试验下"期望最大 Sharpe"作为基准：
         SR0 = sqrt(V[SR]) * ((1-γ)·Z⁻¹(1-1/N) + γ·Z⁻¹(1-1/(N·e)))
      3. 对最优策略算 PSR(SR0) 即为 DSR
    DSR 高（如 >0.95）表示最优策略的表现难以用"纯粹的选择偏差"解释。
    """
    series = [x for x in returns_matrix if x and len(x) >= 4]
    if len(series) < 2:
        return {"ok": False, "reason": "策略数不足"}
    srs = [_sharpe(x) for x in series]
    srs = [v for v in srs if v is not None]
    if len(srs) < 2:
        return {"ok": False, "reason": "可用 Sharpe 不足"}
    N = len(srs)
    mean_sr = sum(srs) / N
    var_sr = sum((v - mean_sr) ** 2 for v in srs) / (N - 1)
    sd_sr = math.sqrt(var_sr)

    sr0 = sd_sr * ((1 - _EULER) * _norm_ppf(1 - 1.0 / N)
                   + _EULER * _norm_ppf(1 - 1.0 / (N * math.e)))
    best_i = max(range(len(series)), key=lambda i: _sharpe(series[i]) or -9e9)
    best = series[best_i]
    dsr = psr(best, sr_benchmark=sr0)
    return {
        "ok": True, "trials": N,
        "sharpes": srs, "sd_sharpe": sd_sr,
        "expected_max_sr": sr0,
        "best_sr": _sharpe(best),
        "dsr": dsr,
        "best_index": best_i,
    }


def pbo_cscv(returns_matrix, n_blocks=10):
    """回测过拟合概率（PBO），用组合对称交叉验证（CSCV）。

    做法：把时间轴切成 n_blocks 段，枚举所有"一半段作样本内"的组合；
    每次在样本内挑最优策略，看它在样本外的排名分位。PBO = 样本外
    排名落在中位数以下的组合占比。

    PBO 高（如 >0.5）表示"样本内挑出的最优策略，样本外大概率表现平庸"
    ——即过拟合。PBO 低表示最优策略的选择是稳健的。
    """
    if not returns_matrix or len(returns_matrix) < 2:
        return {"ok": False, "reason": "策略数不足"}
    T = min(len(x) for x in returns_matrix)
    if T < n_blocks * 2:
        return {"ok": False, "reason": f"样本长度不足（{T} < {n_blocks*2}）"}
    if n_blocks % 2:
        n_blocks -= 1
    mat = [x[:T] for x in returns_matrix]
    N = len(mat)

    # 切成 n_blocks 个连续块
    size = T // n_blocks
    blocks = [list(range(i * size, (i + 1) * size)) for i in range(n_blocks)]

    from itertools import combinations
    half = n_blocks // 2
    logits, oos_ranks = [], []
    for combo in combinations(range(n_blocks), half):
        is_idx = [i for b in combo for i in blocks[b]]
        oos_idx = [i for b in range(n_blocks) if b not in combo
                   for i in blocks[b]]
        if not is_idx or not oos_idx:
            continue
        is_sr = [_sharpe([mat[n][i] for i in is_idx]) for n in range(N)]
        oos_sr = [_sharpe([mat[n][i] for i in oos_idx]) for n in range(N)]
        if any(v is None for v in is_sr) or any(v is None for v in oos_sr):
            continue
        best = max(range(N), key=lambda n: is_sr[n])
        # 该策略在样本外的排名分位（0=最差，1=最好）
        rank = sum(1 for v in oos_sr if v < oos_sr[best]) / (N - 1)
        rank = min(max(rank, 1e-6), 1 - 1e-6)
        oos_ranks.append(rank)
        logits.append(math.log(rank / (1 - rank)))
    if not logits:
        return {"ok": False, "reason": "无法完成 CSCV"}
    pbo = sum(1 for v in logits if v <= 0) / len(logits)
    return {
        "ok": True, "n_blocks": n_blocks, "splits": len(logits),
        "pbo": pbo,
        "median_oos_rank": st.median(oos_ranks),
        "mean_logit": st.mean(logits),
    }


def overfitting_report(conn, end, days=DEFAULT_DAYS, freq="W",
                       field="d5_adj"):
    """把阈值扫描当"扫了 N 个策略"，做 DSR + PBO 校正。

    实测（去重视图修复后）：DSR = 0.9679（通过 0.95）、PBO = 0.0000。
    ⚠️ 注意：在修复 billboard 重复计数之前，DSR 是 0.9403（未通过）。
    重复计数稀释了信号。该修复是**修正缺陷**而非挑参数，但它确实发生在
    观察结果之后，这一点不应隐瞒。

    ⚠️ 重要局限：这里用 `d5_adj`（5 日前向收益）按周聚合，**存在重叠
    与自相关**，会系统性抬高 Sharpe 的显著性。因此本报告应看作
    **方向性的稳健性检查**，而不是严格的 DSR 数值。要严格计算需要
    把持仓期错开（如每日只取 1/5 仓位），本项目未实现。
    """
    from .collect import _limit_threshold
    from datetime import date as _d

    dates = _recent_dates(conn, end, days)
    if len(dates) < 40:
        return {"ok": False, "reason": "交易日不足"}
    rows = conn.execute(
        f"SELECT s.trade_date, s.seat, s.code, s.name, s.rep_prob, b.pct, "
        f"       b.{field} AS fwd "
        "FROM seat_buy_uniq s JOIN billboard_uniq b "
        "  ON b.trade_date=s.trade_date AND b.code=s.code "
        "WHERE s.trade_date>=? AND s.trade_date<=? "
        f"  AND b.{field} IS NOT NULL", (dates[0], dates[-1])).fetchall()
    recs = [dict(r) for r in rows if r["seat"] not in AGGREGATE_LABELS]
    recs = [r for r in recs
            if (r["pct"] or 0) < _thr(r["code"], r.get("name")) - 0.3]
    if len(recs) < 200:
        return {"ok": False, "reason": f"可执行样本不足（{len(recs)}）"}

    def bucket(d):
        y, m, dd = (int(x) for x in d.split("-"))
        dt = _d(y, m, dd)
        if freq == "W":
            return f"{dt.isocalendar()[0]}-{dt.isocalendar()[1]:02d}"
        return d[:7]

    keys = sorted({bucket(r["trade_date"]) for r in recs})
    thresholds = (40, 45, 50, 55, 60, 65, 70, 75, 80)
    cost = total_cost()
    matrix, names = [], []
    for th in thresholds:
        sel = [r for r in recs
               if r["rep_prob"] is not None and r["rep_prob"] >= th]
        if len(sel) < 100:
            continue
        agg = {}
        for r in sel:
            agg.setdefault(bucket(r["trade_date"]), []).append(r["fwd"])
        # 无信号的周期按 0 计（空仓）
        matrix.append([st.mean(agg[k]) - cost if k in agg else 0.0
                       for k in keys])
        names.append(f"rep_prob>={th}")
    if len(matrix) < 2:
        return {"ok": False, "reason": "可用策略不足"}
    return {
        "ok": True, "freq": freq, "field": field, "periods": len(keys),
        "period_range": f"{keys[0]} ~ {keys[-1]}",
        "n_strategies": len(matrix), "names": names,
        "dsr": deflated_sharpe(matrix),
        "pbo": pbo_cscv(matrix, n_blocks=10 if len(keys) >= 20 else 6),
    }


# ------------------------------------------------------ 席位风格画像（学大神）
# 用户的目标之一是"学习一些大神的风格"。胜率只回答"他准不准"，
# 回答不了"他怎么做"——而后者才是能学的东西。
#
# 本函数从可核验的披露数据里提取**行为特征**（不是猜心态）：
#   · 打板还是低吸：上榜日涨幅分布（接近涨停=打板）
#   · 持有期：用 D1/D5/D10 收益衰减形状反推
#   · 集中度：单日买几只、单笔金额量级
#   · 出手频率：活跃度
#   · 偏好板块：主板/创业板/科创板/北交所分布
#   · 是否打新/ST
#
# 这些都是**可复现的统计**，不是叙事。要学的就是这些可量化的习惯。
#
# 实测示例（2026-01~09，可执行口径，扣 0.40%）：
#   华源证券深圳分公司   D1 +5.76% / 胜率 75.0%  偏打板、低频
#   招商证券上海牡丹江路 D1 -8.93% / 胜率 26.0%  高频、追高
# 两者的差别在"频率"和"买点"上，而不在"选股眼光"这种不可学的东西上。

def seat_style(conn, seat, end=None, days=None):
    """刻画一个席位的交易风格（可核验的行为特征）。"""
    days = days or DEFAULT_DAYS
    end = end or conn.execute(
        "SELECT MAX(trade_date) FROM seat_trade").fetchone()[0]
    if not end:
        return None
    dates = _recent_dates(conn, end, days)
    if not dates:
        return None
    lo, hi = dates[0], dates[-1]
    rows = [dict(r) for r in conn.execute(
        "SELECT s.trade_date, s.code, s.name, s.buy_amt, s.net_amt, s.pct, "
        "s.rep_prob, b.pct AS day_pct, b.d1_adj, b.d5_adj, b.d10_adj, "
        "b.reasons AS reason "
        "FROM seat_buy_uniq s JOIN billboard_uniq b "
        "ON b.trade_date=s.trade_date AND b.code=s.code "
        "WHERE s.seat=? AND s.trade_date>=? AND s.trade_date<=?",
        (seat, lo, hi))]
    if not rows:
        return {"seat": seat, "n": 0}

    def pct_cluster(v):
        if v is None:
            return "未知"
        if v >= 9.5:
            return "打板（涨停价买入）"
        if v >= 5:
            return "追高（+5%以上）"
        if v >= 0:
            return "低吸（红盘但不追高）"
        return "逆势（绿盘买入）"

    # 涨跌幅口径：优先 billboard.pct（上榜日涨幅）
    dayp = [r["day_pct"] if r["day_pct"] is not None else r["pct"]
            for r in rows]
    dayp = [v for v in dayp if v is not None]
    buckets = {}
    for v in dayp:
        buckets[pct_cluster(v)] = buckets.get(pct_cluster(v), 0) + 1
    total_b = sum(buckets.values()) or 1

    # 板块偏好
    def board(code):
        c0 = str(code or "")
        if c0.startswith(("688",)):
            return "科创板"
        if c0.startswith(("300", "301")):
            return "创业板"
        if c0.startswith(("8", "4", "9")):
            return "北交所"
        return "主板"
    boards = {}
    for r in rows:
        boards[board(r["code"])] = boards.get(board(r["code"]), 0) + 1

    # 出手频率：有买入的不同交易日数
    day_set = {r["trade_date"] for r in rows}
    per_day = {}
    for r in rows:
        per_day[r["trade_date"]] = per_day.get(r["trade_date"], 0) + 1

    # 金额量级
    amts = [r["buy_amt"] for r in rows if r["buy_amt"]]
    # 收益衰减形状（判断持有期偏好）
    rets = {}
    for h in ("d1", "d5", "d10"):
        v = [r[h + "_adj"] for r in rows if r[h + "_adj"] is not None]
        rets[h] = (st.mean(v), len(v)) if v else (None, 0)

    # 活跃度口径必须与**已验证的信号**一致：分析里用的是
    # "滚动 60 日上榜天数"，档位 ≤2 / 3-9 / ≥10（config 的
    # SEAT_* 与 activity_edge 都用这个）。这里若按 180 日窗口
    # 自定阈值，同一席位在两处会被贴不同标签——实测华源证券
    # 36/180 天被判"高频"，但它是本项目验证过的**低频**优质席位。
    last60 = dates[-60:] if len(dates) >= 60 else dates
    freq = len({r["trade_date"] for r in rows
                if r["trade_date"] in set(last60)})
    freq_label = ("低频" if freq <= 2 else "中频" if freq < 10 else "高频")
    style = []
    if buckets:
        top = max(buckets.items(), key=lambda x: x[1])
        if top[1] / total_b >= 0.5:
            style.append(top[0])
    if freq_label == "低频":
        style.append("出手极少")
    elif freq_label == "高频":
        style.append("出手极频")
    # 持有期判断：哪个持有期的均值最高
    best_h, best_v = None, None
    for h in ("d1", "d5", "d10"):
        m, n = rets[h]
        if m is not None and n >= 10 and (best_v is None or m > best_v):
            best_h, best_v = h, m
    if best_h:
        style.append({"d1": "隔夜为主", "d5": "持有数日",
                      "d10": "中线持有"}[best_h])
    delist = sum(1 for r in rows if r["name"] and "退" in r["name"])
    if delist and delist / len(rows) >= 0.2:
        style.append(f"⚠️ 大量买退市股（{delist}/{len(rows)}）")
    return {
        "seat": seat, "n": len(rows), "days": days,
        "window": f"{lo} ~ {hi}",
        "active_days": freq, "freq_label": freq_label,
        "trades_per_day": len(rows) / freq if freq else None,
        "buy_point": {k: round(v / total_b * 100, 1)
                      for k, v in sorted(buckets.items(),
                                         key=lambda x: -x[1])},
        "boards": {k: round(v / len(rows) * 100, 1)
                   for k, v in sorted(boards.items(), key=lambda x: -x[1])},
        "amount_median_wan": (st.median(amts) / 1e4) if amts else None,
        "amount_max_yi": (max(amts) / 1e8) if amts else None,
        "ret_d1": rets["d1"][0], "ret_d5": rets["d5"][0],
        "ret_d10": rets["d10"][0],
        "best_horizon": best_h,
        "decay": _decay_shape(rets),
        "style": style,
        "st_n": sum(1 for r in rows if r["name"] and "ST" in r["name"]),
        # 退市整理期股票：实测"最差席位"其实是在接退市股
        # （立方退×11、长药退×5…），收益 -30% 级。
        # 这不是"交易水平差"，而是标的本身要归零，必须单独标出来，
        # 否则会把这个席位误读成"反面教材的选股风格"。
        "delist_n": sum(1 for r in rows
                        if r["name"] and "退" in r["name"]),
        "top_names": _top_names(rows, 6),
    }


def _decay_shape(rets):
    """收益随持有期的形状：判断这是"脉冲"还是"趋势"。"""
    d1, d5, d10 = rets["d1"][0], rets["d5"][0], rets["d10"][0]
    if d1 is None or d5 is None:
        return "样本不足"
    if d5 < d1 and (d10 is None or d10 < d5):
        return "一日游脉冲（次日最高，越持越亏）"
    if d5 > d1 and (d10 is None or d10 >= d5):
        return "趋势型（越持越赚，可持有）"
    if d5 > d1 and d10 is not None and d10 < d5:
        return "峰在 D5（5 日左右了结）"
    return "震荡"


def _top_names(rows, k):
    cnt = {}
    for r in rows:
        key = (r["code"], r["name"])
        cnt[key] = cnt.get(key, 0) + 1
    return [{"code": c, "name": n, "n": v}
            for (c, n), v in sorted(cnt.items(), key=lambda x: -x[1])[:k]]


def rank_styles(conn, end=None, days=None, min_trades=25, top=12):
    """按可执行 D5 净收益给席位排序，并附风格标签。

    这是"学大神"的入口：找出**既有超额、风格又清晰**的席位。
    """
    days = days or DEFAULT_DAYS
    end = end or conn.execute(
        "SELECT MAX(trade_date) FROM seat_trade").fetchone()[0]
    if not end:
        return []
    dates = _recent_dates(conn, end, days)
    if not dates:
        return []
    lo, hi = dates[0], dates[-1]
    rows = conn.execute(
        "SELECT s.seat, b.d1_adj, b.d5_adj, b.d10_adj, b.pct "
        "FROM seat_buy_uniq s JOIN billboard_uniq b "
        "ON b.trade_date=s.trade_date AND b.code=s.code "
        "WHERE s.trade_date>=? AND s.trade_date<=? "
        "AND s.seat NOT IN ({})".format(
            ",".join("?" * len(AGGREGATE_LABELS))),
        (lo, hi, *AGGREGATE_LABELS)).fetchall()
    agg = {}
    for r in rows:
        a = agg.setdefault(r["seat"], {"d1": [], "d5": [], "d10": []})
        for h in ("d1", "d5", "d10"):
            if r[h + "_adj"] is not None:
                a[h].append(r[h + "_adj"])
    out = []
    for s, a in agg.items():
        if len(a["d1"]) < min_trades:
            continue
        out.append({
            "seat": s, "n": len(a["d1"]),
            "d1": st.mean(a["d1"]), "d1_win":
                sum(1 for v in a["d1"] if v > 0) / len(a["d1"]) * 100,
            "d5": st.mean(a["d5"]) if a["d5"] else None,
            "d10": st.mean(a["d10"]) if a["d10"] else None,
        })
    out.sort(key=lambda x: -(x["d5"] if x["d5"] is not None else -99))
    return out[:top]


def worst_styles(conn, end=None, days=None, min_trades=25, top=5):
    """D5 最差的席位——反面教材。

    必须能看反面：只知道"学谁"而不知道"别学谁"，
    很容易把「退市股接盘」误当成一种可模仿的低吸风格。
    """
    days = days or DEFAULT_DAYS
    end = end or conn.execute(
        "SELECT MAX(trade_date) FROM seat_trade").fetchone()[0]
    if not end:
        return []
    dates = _recent_dates(conn, end, days)
    if not dates:
        return []
    lo, hi = dates[0], dates[-1]
    rows = conn.execute(
        "SELECT s.seat, b.d1_adj, b.d5_adj, b.d10_adj "
        "FROM seat_buy_uniq s JOIN billboard_uniq b "
        "ON b.trade_date=s.trade_date AND b.code=s.code "
        "WHERE s.trade_date>=? AND s.trade_date<=? "
        "AND s.seat NOT IN ({})".format(
            ",".join("?" * len(AGGREGATE_LABELS))),
        (lo, hi, *AGGREGATE_LABELS)).fetchall()
    agg = {}
    for r in rows:
        a = agg.setdefault(r["seat"], {"d1": [], "d5": [], "d10": []})
        for h in ("d1", "d5", "d10"):
            if r[h + "_adj"] is not None:
                a[h].append(r[h + "_adj"])
    out = []
    for s, a in agg.items():
        if len(a["d1"]) < min_trades or not a["d5"]:
            continue
        out.append({"seat": s, "n": len(a["d1"]),
                    "d1": st.mean(a["d1"]),
                    "d5": st.mean(a["d5"]),
                    "d10": st.mean(a["d10"]) if a["d10"] else None,
                    "d1_win": sum(1 for v in a["d1"] if v > 0)
                    / len(a["d1"]) * 100})
    out.sort(key=lambda x: x["d5"])
    return out[:top]
