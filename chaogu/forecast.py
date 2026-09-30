#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""预测：历史相似周期匹配。

方法（刻意做得可解释、可回验，而不是黑箱）：
  1. 把每个交易日表示成一个特征向量（涨停家数、封板率、最高板、
     连板家数、昨涨停溢价、跌停家数、涨跌比）
  2. z-score 标准化后，在历史里找与"今天"最相似的 K 天
  3. 看那 K 天的**次日**实际发生了什么（次日溢价、次日涨停家数变化）
  4. 用邻居的加权平均作为预测，并给出置信度

关键点：预测的目标是"明天的情绪"，不是"明天哪只股票涨"。
后者在这个框架里无法可靠预测，不要假装可以。

每次预测都写入 forecast_log，事后可回填实际值做命中率统计。
这是这个模块存在的意义：让预测可被证伪。
"""

from __future__ import annotations

import json
import math
from datetime import datetime

from . import config, db


def _now():
    return datetime.now().isoformat(timespec="seconds")


def _feature_rows(conn, before=None):
    sql = ("SELECT m.trade_date, m.zt_count, m.max_height, m.lianban_count, "
           "m.dt_count, e.seal_rate, e.prev_zt_premium, e.promote_rate, "
           "e.up_down_ratio, e.temperature, e.stage "
           "FROM emotion_daily e JOIN daily_market m "
           "ON m.trade_date = e.trade_date WHERE 1=1")
    args = []
    if before:
        sql += " AND m.trade_date < ?"
        args.append(before)
    sql += " ORDER BY m.trade_date"
    return [dict(r) for r in conn.execute(sql, args)]


def _median(vals):
    v = sorted(x for x in vals if x is not None)
    if not v:
        return None
    n = len(v)
    return v[n // 2] if n % 2 else (v[n // 2 - 1] + v[n // 2]) / 2


def _medians(rows):
    return {f: _median([r.get(f) for r in rows])
            for f in config.FORECAST_FEATURES}


def _usable_features(rows, med=None):
    """筛出在样本中真正有数据的特征。

    若某特征在全部样本里都缺失（例如"涨跌家数"只有采集到全市场
    快照的日子才有），它的中位数也是 None。此时应**整维剔除**，
    否则每一行都会被判为无效，预测直接瘫痪。
    """
    med = med or _medians(rows)
    return [f for f in config.FORECAST_FEATURES if med.get(f) is not None]


def _vec(row, feats, med, max_missing=2):
    """按 feats 构造特征向量，缺失值用中位数填充。"""
    v, missing = [], 0
    for f in feats:
        x = row.get(f)
        if x is None:
            missing += 1
            x = med.get(f)
            if x is None:
                return None
        v.append(float(x))
    if missing > max_missing:
        return None
    return v


def _standardize(rows):
    """按列算均值/标准差。"""
    n = len(rows)
    if n == 0:
        return [], []
    dim = len(rows[0])
    mu = [sum(r[i] for r in rows) / n for i in range(dim)]
    sd = []
    for i in range(dim):
        var = sum((r[i] - mu[i]) ** 2 for r in rows) / n
        sd.append(math.sqrt(var) or 1.0)
    return mu, sd


def _dist(a, b, mu, sd):
    return math.sqrt(sum(((a[i] - b[i]) / sd[i]) ** 2
                         for i in range(len(a))))


def forecast(conn, date, k=None, horizon=None, save=True):
    """对 date 的下一交易日做情绪预测。"""
    k = k or config.FORECAST_NEIGHBORS
    horizon = horizon or 1               # 目前只预测次日

    hist = _feature_rows(conn, before=date)
    if len(hist) < 12:
        return {"ok": False, "reason": f"历史样本不足（{len(hist)} 天，"
                                       f"至少需要 12 天）"}

    today = conn.execute(
        "SELECT m.trade_date, m.zt_count, m.max_height, m.lianban_count, "
        "m.dt_count, e.seal_rate, e.prev_zt_premium, e.promote_rate, "
        "e.up_down_ratio, e.temperature, e.stage "
        "FROM emotion_daily e JOIN daily_market m "
        "ON m.trade_date = e.trade_date WHERE m.trade_date=?",
        (date,)).fetchone()
    if not today:
        return {"ok": False, "reason": f"{date} 无指标数据"}
    today = dict(today)

    # 先算各特征中位数，用于填充历史缺失字段
    med = _medians(hist)
    feats = _usable_features(hist, med)
    if len(feats) < 3:
        return {"ok": False, "reason": f"可用特征太少({len(feats)})"}

    # 为每个历史日构造 (特征, 次日结果)
    samples = []
    for i in range(len(hist) - horizon):
        fv = _vec(hist[i], feats, med)
        nxt = hist[i + horizon]
        if fv is None:
            continue
        if nxt.get("prev_zt_premium") is None:
            continue          # 次日溢价缺失（不是交易日或数据不全）
        samples.append({
            "date": hist[i]["trade_date"],
            "next_date": nxt["trade_date"],
            "feat": fv,
            "next_premium": nxt["prev_zt_premium"],
            "next_zt": nxt["zt_count"],
            "next_temp": nxt.get("temperature"),
            "next_stage": nxt.get("stage"),
            "cur_zt": hist[i]["zt_count"],
        })
    if len(samples) < 10:
        return {"ok": False,
                "reason": f"可用样本不足（{len(samples)} 条）"}

    mu, sd = _standardize([s["feat"] for s in samples])
    tv = _vec(today, feats, med)
    if tv is None:
        return {"ok": False, "reason": f"{date} 特征不完整，无法匹配"}

    for s in samples:
        s["dist"] = _dist(s["feat"], tv, mu, sd)
    samples.sort(key=lambda s: s["dist"])
    nb = samples[:k]

    # 距离倒数加权
    eps = 1e-6
    ws = [1.0 / (s["dist"] + eps) for s in nb]
    sw = sum(ws)
    pred_premium = sum(s["next_premium"] * w for s, w in zip(nb, ws)) / sw
    pred_zt = sum(s["next_zt"] * w for s, w in zip(nb, ws)) / sw

    # 命中率：邻居里次日溢价为正的比例（即打板赚钱的概率）
    win = sum(1 for s in nb if s["next_premium"] > 0)
    win_rate = win / len(nb)

    avg_dist = sum(s["dist"] for s in nb) / len(nb)
    # 置信度：邻居越近、越一致、样本越多，越高
    spread = (sum((s["next_premium"] - pred_premium) ** 2 for s in nb)
              / len(nb)) ** 0.5
    conf = 1.0 / (1.0 + avg_dist * 0.35) * 1.0 / (1.0 + spread * 0.30)
    conf = max(0.05, min(0.95, conf))

    if pred_premium > 1.0:
        bias = "偏暖"
    elif pred_premium < -1.0:
        bias = "偏冷"
    else:
        bias = "中性"

    basis = {
        "k": len(nb),
        "neighbors": [
            {"date": s["date"], "next": s["next_date"],
             "dist": round(s["dist"], 3),
             "next_premium": s["next_premium"],
             "next_zt": s["next_zt"],
             "next_stage": s["next_stage"]}
            for s in nb
        ],
        "win_rate": round(win_rate, 3),
        "spread": round(spread, 3),
        "avg_dist": round(avg_dist, 3),
        "today_stage": today.get("stage"),
        "today_temp": today.get("temperature"),
        "today_zt": today.get("zt_count"),
    }

    res = {
        "ok": True,
        "date": date,
        "target_date": nb[0]["next_date"] if nb else None,
        "bias": bias,
        "confidence": round(conf, 3),
        "pred_premium": round(pred_premium, 3),
        "pred_zt_count": round(pred_zt, 1),
        "zt_delta": round(pred_zt - (today.get("zt_count") or 0), 1),
        "win_rate": round(win_rate, 3),
        "neighbors": basis["neighbors"],
        "basis": basis,
    }

    if save:
        targets = conn.execute(
            "SELECT MIN(trade_date) FROM daily_market WHERE trade_date > ?",
            (date,)).fetchone()
        row = {
            "trade_date": date,
            "target_date": targets[0] if targets and targets[0] else None,
            "bias": bias,
            "confidence": round(conf, 3),
            "pred_zt_count": round(pred_zt, 1),
            "pred_premium": round(pred_premium, 3),
            "basis": json.dumps(basis, ensure_ascii=False),
            "created_at": _now(),
        }
        db.upsert(conn, "forecast_log", [row], ["trade_date"])
    return res


def verify_forecasts(conn):
    """回填实际值并计算命中率。"""
    rows = conn.execute(
        "SELECT f.* FROM forecast_log f WHERE f.actual_premium IS NULL "
        "AND f.target_date IS NOT NULL").fetchall()
    n = 0
    for r in rows:
        e = conn.execute(
            "SELECT prev_zt_premium FROM emotion_daily WHERE trade_date=?",
            (r["target_date"],)).fetchone()
        m = conn.execute("SELECT zt_count FROM daily_market WHERE trade_date=?",
                         (r["target_date"],)).fetchone()
        if not e or e[0] is None:
            continue
        actual = e[0]
        hit = None
        if r["pred_premium"] is not None:
            hit = 1 if (r["pred_premium"] > 0) == (actual > 0) else 0
        with conn:
            conn.execute(
                "UPDATE forecast_log SET actual_premium=?, actual_zt_count=?, "
                "hit=? WHERE trade_date=?",
                (actual, m[0] if m else None, hit, r["trade_date"]))
        n += 1
    return n


def accuracy(conn):
    """预测质量评估。

    **必须和基准率对比**，否则命中率会骗人：
    如果历史上 84% 的交易日溢价为正，那么"永远猜偏暖"也能有 84% 命中率。
    只有 hit_rate 显著高于 base_rate 才算有真实预测能力。
    """
    r = conn.execute(
        "SELECT COUNT(*) n, SUM(hit) h, "
        "AVG(ABS(pred_premium-actual_premium)) mae, "
        "AVG(pred_premium) avg_pred, AVG(actual_premium) avg_actual "
        "FROM forecast_log WHERE hit IS NOT NULL").fetchone()
    if not r or not r["n"]:
        return {"n": 0}

    hit_rate = (r["h"] or 0) / r["n"]
    # 基准率：历史所有交易日里溢价为正的比例
    base = conn.execute(
        "SELECT COUNT(*) n, SUM(CASE WHEN prev_zt_premium > 0 THEN 1 ELSE 0 END) p "
        "FROM emotion_daily WHERE prev_zt_premium IS NOT NULL").fetchone()
    base_rate = (base["p"] or 0) / base["n"] if base and base["n"] else None

    # 预测值与实际值的相关（有没有真的"跟着变化"，而不只是猜方向）
    pairs = conn.execute(
        "SELECT pred_premium p, actual_premium a FROM forecast_log "
        "WHERE pred_premium IS NOT NULL AND actual_premium IS NOT NULL"
    ).fetchall()
    corr = _pearson([x["p"] for x in pairs], [x["a"] for x in pairs])

    # 偏差方向统计：预测偏高还是偏低
    bias_dir = None
    if r["avg_pred"] is not None and r["avg_actual"] is not None:
        d = r["avg_pred"] - r["avg_actual"]
        bias_dir = ("系统性高估" if d > 0.5 else
                    "系统性低估" if d < -0.5 else "基本无偏")

    return {
        "n": r["n"],
        "hit_rate": round(hit_rate, 3),
        "base_rate": round(base_rate, 3) if base_rate is not None else None,
        "edge": (round(hit_rate - base_rate, 3)
                 if base_rate is not None else None),
        "mae": round(r["mae"], 3) if r["mae"] is not None else None,
        "corr": round(corr, 3) if corr is not None else None,
        "avg_pred": round(r["avg_pred"], 3) if r["avg_pred"] is not None else None,
        "avg_actual": round(r["avg_actual"], 3) if r["avg_actual"] is not None else None,
        "bias": bias_dir,
        "verdict": _verdict(hit_rate, base_rate, corr),
    }


def _verdict(hit, base, corr):
    """给一个直白的结论，避免用户被虚高的命中率误导。"""
    if base is None:
        return "样本不足，无法评估"
    edge = hit - base
    useful = corr is not None and corr > 0.2
    if edge < 0.03 and not useful:
        return ("几乎没有预测能力：命中率与基准率基本持平，"
                "且预测值与实际值相关性低。不要据此下单。")
    if edge < 0.08 or (corr is not None and corr <= 0.35):
        return ("只有很弱的参考价值：略高于基准率，"
                "但差距不足以覆盖交易成本，只能当辅助参考。")
    return "有一定参考价值，但仍需结合其他判断，且样本量有限。"


def diagnostics(conn):
    """预测有效性检验。

    这是整个预测模块里**最重要**的部分：在做任何预测之前，
    先问"这些特征到底有没有信息量"。方法：
      1. 溢价的滞后自相关——昨日溢价能预测今日吗
      2. 各特征与次日溢价的相关系数
      3. 按今日状态分组，看次日溢价的均值与胜率有无差异
    """
    rows = [dict(r) for r in conn.execute(
        "SELECT e.trade_date d, e.prev_zt_premium p, m.zt_count z, "
        "m.max_height h, m.lianban_count lb, m.dt_count dt, "
        "e.temperature t, e.seal_rate sr "
        "FROM emotion_daily e JOIN daily_market m ON m.trade_date=e.trade_date "
        "WHERE e.prev_zt_premium IS NOT NULL ORDER BY e.trade_date")]
    n = len(rows)
    if n < 10:
        return {"ok": False, "reason": f"样本不足（{n} 天）"}
    P = [r["p"] for r in rows]

    auto1 = _pearson(P[:-1], P[1:])
    auto2 = _pearson(P[:-2], P[2:])

    feats = {"涨停家数": "z", "最高板": "h", "连板家数": "lb",
             "跌停家数": "dt", "温度": "t", "封板率": "sr"}
    corr_next = {}
    for label, key in feats.items():
        xs, ys = [], []
        for i in range(n - 1):
            v = rows[i].get(key)
            if v is None:
                continue
            xs.append(float(v))
            ys.append(rows[i + 1]["p"])
        c = _pearson(xs, ys)
        if c is not None:
            corr_next[label] = round(c, 3)

    # 按今日温度分组的次日表现
    groups = {}
    for i in range(n - 1):
        t = rows[i].get("t")
        if t is None:
            continue
        k = ("<25 冰点" if t < 25 else "25-45 修复" if t < 45
             else "45-70 发酵" if t < 70 else ">=70 高潮")
        groups.setdefault(k, []).append(rows[i + 1]["p"])
    band = {}
    for k in ("<25 冰点", "25-45 修复", "45-70 发酵", ">=70 高潮"):
        v = groups.get(k) or []
        if v:
            band[k] = {
                "n": len(v),
                "mean": round(sum(v) / len(v), 2),
                "win": round(sum(1 for x in v if x > 0) / len(v), 3),
            }

    pos = sum(1 for x in P if x > 0)
    return {
        "ok": True,
        "n": n,
        "mean": round(sum(P) / n, 2),
        "std": round((sum((x - sum(P) / n) ** 2 for x in P) / n) ** 0.5, 2),
        "base_rate": round(pos / n, 3),
        "autocorr_1": round(auto1, 3) if auto1 is not None else None,
        "autocorr_2": round(auto2, 3) if auto2 is not None else None,
        "corr_next": corr_next,
        "bands": band,
        "conclusion": _diagnose(auto1, corr_next),
    }


def _diagnose(auto1, corr_next):
    strong = {k: v for k, v in (corr_next or {}).items() if abs(v) >= 0.25}
    if auto1 is not None and abs(auto1) < 0.2 and not strong:
        return ("次日溢价与今日各项情绪指标几乎不相关"
                "（自相关与各特征相关系数均接近 0）。"
                "也就是说，用当前指标预测次日赚钱效应，"
                "在本样本内没有可靠的信息量。"
                "结论：预测模块只能当作情景参考，不要当成信号。")
    if strong:
        items = "、".join(f"{k}(r={v:+.2f})" for k, v in strong.items())
        return (f"发现弱相关信息：{items}。"
                f"相关系数绝对值 >= 0.25 才值得关注，"
                f"但仍需注意样本量小、可能是巧合。")
    return "相关性弱，参考价值有限。"


def _pearson(xs, ys):
    n = len(xs)
    if n < 3:
        return None
    mx, my = sum(xs) / n, sum(ys) / n
    num = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    dx = sum((x - mx) ** 2 for x in xs) ** 0.5
    dy = sum((y - my) ** 2 for y in ys) ** 0.5
    if dx == 0 or dy == 0:
        return None
    return num / (dx * dy)
