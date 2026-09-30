#!/usr/bin/env python3
"""短线情绪看板：长期驻留本机，随时打开就能看当下情绪与候选。

设计原则（重要，别改坏）
------------------------
1. **只暴露已验证的信号为"可操作"，未验证的一律标注。**
   本项目实测：情绪预测无预测力（corr -0.207）、题材延续性评分
   预测不成立（个股层面 -0.021）、追龙头剔除买不进后为负
   （≥4板 -1.10%）。唯一通过样本外检验的是**席位跟随**。
   看板必须把这个区别摆在明面上，不能让用户以为都是信号。

2. **"想象性"不做成数字。** 想象性（题材叙事空间）无法从本地
   量价数据测出来；本项目已经证明"把直觉指标加权成评分"会失败
   （题材评分就是例子）。所以看板只陈列**原始证据**
   （催化原文、新颖度、广度、梯队），把判断留给用户。

3. **数据每天变，服务不重启。** 每次请求都重新查库，
   这样长期驻留的进程能自动反映当天新采集的数据。

用法：
    python3 run.py dash              # 默认 127.0.0.1:8787
    python3 run.py dash --port 9000
"""

from __future__ import annotations

import html
import json
import threading
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

from . import config, db, seat, theme

# 聚合标签：不是真实席位，展示与跟随都要排除
_AGG = None

# 采集数据按天变化，没必要每个请求都重算。
# 实测 _seats 单次约 500ms（占整页 560ms 的 90%），
# 串行化后 10 并发会把尾延迟推到 5.8s —— 对长期开着的看板不可接受。
# 用一个 15 秒 TTL 的缓存：手动刷新仍然"够新"，
# 但连点刷新不再重复付这笔钱。
_CACHE = {}
_CACHE_TTL = 15.0
# 容量上限：正常情况下只有 1 个日期的 4~6 个键，
# 给足余量以容忍跨日切换，同时保证长期运行不涨。
_CACHE_MAX = 48
_CACHE_KEEP = 24


def _agg():
    global _AGG
    if _AGG is None:
        from .lhb import AGGREGATE_LABELS
        _AGG = AGGREGATE_LABELS
    return _AGG


def _max_date(conn, table, col="trade_date"):
    try:
        return conn.execute(f"SELECT MAX({col}) FROM {table}").fetchone()[0]
    except Exception:
        return None


def _attempted_key(conn, key, date):
    try:
        return bool(conn.execute(
            "SELECT 1 FROM collect_log WHERE trade_date=? AND step=? LIMIT 1",
            (date, key)).fetchone())
    except Exception:
        return False


# 龙虎榜补采最多重试几次（每 5 分钟一次 tick）。
# 上界存在的理由和节假日重试风暴同源：必须有限，否则
# 一个休市日会从 18:00 打到午夜（~72 次），反复打外部接口。
_LHB_MAX_ATTEMPTS = 12


def _lhb_state(conn, date):
    """返回 (是否已成功采到, 今日已尝试次数)。"""
    try:
        rows = conn.execute(
            "SELECT status FROM collect_log WHERE trade_date=? AND step=?",
            (date, _LHB_KEY)).fetchall()
    except Exception:
        return False, 0
    ok = any(r[0] == "ok" for r in rows)
    return ok, len(rows)


def _mark_key(conn, key, date, note, status="ok"):
    """在 collect_log 里落一个"这天这件事已做过"的标记。

    复用 collect_log 而不新建表：它已经是"某天某步骤做过什么"的
    日志表，语义完全吻合。主键含 ts，所以重复标记不会冲突。
    """
    try:
        conn.execute(
            "INSERT INTO collect_log (trade_date, step, status, rows, message,"
            " ts) VALUES (?,?,?,?,?,?)",
            (date, key, status, 0, note,
             datetime.now().isoformat(timespec="microseconds")))
        conn.commit()
    except Exception:
        pass


def _latest(conn):
    """看板基准日 = 各数据源里最新的那一天。

    取 max 而不是优先某一源：情绪是用户最关心的（"捕捉短线的情绪"），
    但龙虎榜披露通常晚一天，取 max 能让情绪面板总是最新；
    席位面板在基准日无数据时会各自回退（见 _seats/_themes）。
    """
    cands = [d for d in (_max_date(conn, "emotion_daily"),
                         _max_date(conn, "seat_trade"),
                         _max_date(conn, "limit_up"),
                         _max_date(conn, "daily_market")) if d]
    return max(cands) if cands else None


# ------------------------------------------------------------------ 数据层

def _cached(key, ttl, fn):
    """极简 TTL 缓存。只缓存成功结果，异常不缓存（否则会固化故障）。

    必须**同时**做容量上限：缓存键含日期，而日期每天都会变，
    只按 TTL 淘汰的话旧键永远不会被访问、也就永远不会过期，
    长期开着的看板会一天漏一点内存（实测 9 天就攒了 36 个键）。
    """
    import time as _t
    now = _t.time()
    hit = _CACHE.get(key)
    if hit and now - hit[0] < ttl:
        return hit[1]
    val = fn()
    _CACHE[key] = (now, val)
    # 超出容量就丢掉最旧的一批（按写入时间）
    if len(_CACHE) > _CACHE_MAX:
        for k in sorted(_CACHE, key=lambda k: _CACHE[k][0]
                        )[:len(_CACHE) - _CACHE_KEEP]:
            _CACHE.pop(k, None)
    return val


def gather(conn, date=None, use_cache=True):
    """汇总看板所需的全部数据。

    数据按天变化，所以 15 秒缓存不会让你看到过期内容；
    但它能让连点刷新 / 多标签页 / API 轮询不再重复打重查询。
    """
    date = date or _latest(conn)
    data = {"date": date, "generated_at": datetime.now().strftime("%H:%M:%S")}
    if not date:
        data["empty"] = True
        return data

    def _get(name, fn):
        if not use_cache:
            return fn(conn, date)
        return _cached((name, date), _CACHE_TTL, lambda: fn(conn, date))

    data["emotion"] = _get("emotion", _emotion)
    data["themes"] = _get("themes", _themes)
    data["leaders"] = _get("leaders", _leaders)
    data["seats"] = _get("seats", _seats)
    # 看板自己记账：把当日候选写进 signal_log。
    # 这样"留痕"不依赖用户记得手动跑 track log —— 忘记记账正是
    # 胜率无法改进的头号原因。失败不影响渲染。
    #
    # 用日志表判断"今天是否已记过"，而不是每个请求都尝试 INSERT：
    # 虽然 log_signals 是幂等的，但每次请求都去查/写库没有意义。
    try:
        from . import track as _track_mod
        if not _attempted_key(conn, "signal_logged", date):
            _track_mod.log_signals(conn, date)
            _mark_key(conn, "signal_logged", date, "看板自动留痕")
    except Exception:
        pass
    data["outside"] = _outside(conn, date)
    data["freshness"] = _freshness(conn, date)
    data["track"] = _track(conn)
    return data


def _track(conn):
    """留痕战绩：看板过去到底说对了没有。"""
    try:
        from . import track as _track_mod
        t = _track_mod.track_record(conn)
        # 只保留渲染需要的字段，避免把上千行塞进每次请求
        t.pop("rows", None)
        return t
    except Exception:
        return {"ok": False}


def _freshness(conn, date):
    """数据新鲜度：长期驻留的看板必须让用户一眼看出数据是不是旧的。

    看板本身不会自动采集（采集要打外部接口，不适合无脑定时跑），
    所以"数据到今天了吗"必须显式暴露，否则用户会对着旧数据做决策。
    """
    today = datetime.now().strftime("%Y-%m-%d")
    out = {"today": today, "date": date, "is_today": date == today,
           "sources": []}
    for label, sql in (
            ("情绪指标", "SELECT MAX(trade_date) FROM emotion_daily"),
            ("涨停/题材", "SELECT MAX(trade_date) FROM limit_up"),
            ("龙虎榜席位", "SELECT MAX(trade_date) FROM seat_trade"),
            ("外围市场", "SELECT MAX(trade_date) FROM global_market")):
        try:
            d = conn.execute(sql).fetchone()[0]
        except Exception:
            d = None
        out["sources"].append({"label": label, "date": d,
                               "ok": d == today})
    try:
        last = conn.execute(
            "SELECT ts FROM collect_log ORDER BY rowid DESC LIMIT 1"
        ).fetchone()
        out["last_collect"] = last[0] if last else None
    except Exception:
        out["last_collect"] = None
    return out


def _emotion(conn, date):
    """情绪周期：温度、阶段、关键比率 + 近 5 日走势。"""
    cur = conn.execute(
        "SELECT * FROM emotion_daily WHERE trade_date=?", (date,)).fetchone()
    mkt = conn.execute(
        "SELECT * FROM daily_market WHERE trade_date=?", (date,)).fetchone()
    hist = conn.execute(
        "SELECT trade_date, temperature, stage, prev_zt_premium, zt_count "
        "FROM emotion_daily e LEFT JOIN daily_market m USING (trade_date) "
        "WHERE e.trade_date<=? ORDER BY e.trade_date DESC LIMIT 10",
        (date,)).fetchall()
    out = {"row": dict(cur) if cur else None,
           "market": dict(mkt) if mkt else None,
           "history": [dict(r) for r in reversed(hist)]}
    # 情绪温度分位（相对历史）
    temps = [r["temperature"] for r in conn.execute(
        "SELECT temperature FROM emotion_daily WHERE temperature IS NOT NULL")]
    if cur and temps:
        t = cur["temperature"]
        out["pctile"] = sum(1 for x in temps if x <= t) / len(temps) * 100
    return out


def _themes(conn, date):
    """新题材 + 延续性（附诚实标注）+ 想象性原始证据。"""
    new = []
    try:
        new = theme.detect_new(conn, date) or []
    except Exception:
        new = []
    ranked = []
    try:
        for s in theme.rank_persistence(conn, date, min_zt=2, top=12):
            s = dict(s)
            # 催化原文：这是判断"想象性"的核心材料，必须给出
            cat = theme.theme_catalyst(conn, date, s["tag"], limit=3)
            s["catalysts"] = cat
            hist = theme.sparkline(
                [r["zt_count"] for r in theme._series(
                    conn, s["tag"], date, config.THEME_LOOKBACK)])
            s["spark"] = hist
            ranked.append(s)
    except Exception:
        ranked = []
    return {"new": [dict(x) if not isinstance(x, dict) else x for x in new],
            "ranked": ranked}


def _leaders(conn, date):
    try:
        mk = [dict(r) for r in theme.market_leaders(conn, date, top=10)]
    except Exception:
        mk = []
    try:
        th = theme.theme_leaders(conn, date, top=8) or []
        th = [dict(r) if not isinstance(r, dict) else r for r in th]
    except Exception:
        th = []
    return {"market": mk, "theme": th}


def _seats(conn, date):
    """大神风格：跟随候选（已按活跃度排序）+ 高胜率席位榜。"""
    # 龙虎榜通常比情绪晚一天披露；基准日无席位数据时回退到最近有数据的一天，
    # 并在返回里标出实际日期，避免用户误以为看的是当天。
    sdate = date
    try:
        if not conn.execute("SELECT 1 FROM seat_trade WHERE trade_date=? "
                            "LIMIT 1", (date,)).fetchone():
            sdate = _max_date(conn, "seat_trade") or date
    except Exception:
        pass
    try:
        sig = seat.follow_signals(conn, sdate, min_rep_prob=60,
                                  exclude_limit_up=True, top=25)
    except Exception:
        sig = []
    # 高胜率席位榜（近 180 日，排除聚合标签）
    try:
        stats = seat.seat_stats(conn, date, days=config.SEAT_EVAL_DAYS)
    except Exception:
        stats = []
    rank = [{k: v for k, v in s.items() if k != "recent"}
            for s in stats if s["seat"] not in _agg()][:15]
    # 当日上榜但被排除的聚合标签（反向指标，仅展示警示）
    agg = conn.execute(
        "SELECT s.seat, COUNT(*) n FROM seat_buy_uniq s "
        "WHERE s.trade_date=? GROUP BY s.seat ORDER BY n DESC LIMIT 40",
        (date,)).fetchall()
    agg_rows = [dict(r) for r in agg if r["seat"] in _agg()]
    # 滚动活跃度（用于在候选上标 低频/高频）
    acts = {}
    try:
        prev = [r[0] for r in conn.execute(
            "SELECT DISTINCT trade_date FROM seat_trade "
            "WHERE trade_date<? ORDER BY trade_date DESC LIMIT 60", (date,))]
        if prev:
            ph = ",".join("?" for _ in prev)
            for n, c in conn.execute(
                    f"SELECT seat, COUNT(DISTINCT trade_date) FROM seat_trade "
                    f"WHERE trade_date IN ({ph}) AND side='buy' "
                    f"GROUP BY seat", prev):
                acts[n] = c
    except Exception:
        pass
    for s in sig:
        s["act"] = acts.get(s["seat"], 0)
    # 风格榜：学大神到底学什么（行为特征，不是胜率）
    styles = _cached(("styles", sdate), 300, lambda: _styles(conn, sdate))
    worst = _cached(("worst", sdate), 300,
                    lambda: _styles(conn, sdate, top=4, worst=True))
    return {"styles": styles, "worst": worst,
            "follow": sig, "rank": rank, "agg": agg_rows,
            "min_prob": config.SEAT_STRONG_REP_PROB,
            "date": sdate, "stale": sdate != date}


def _styles(conn, date, top=6, worst=False):
    """席位行为标签。worst=False 取 D5 最好，True 取最差（反面教材）。"""
    try:
        rows = (seat.worst_styles(conn, date, top=top) if worst
                else seat.rank_styles(conn, date, top=top))
    except Exception:
        return []
    out = []
    for x in rows:
        try:
            st = seat.seat_style(conn, x["seat"], end=date) or {}
        except Exception:
            st = {}
        bp = st.get("buy_point") or {}
        main = max(bp.items(), key=lambda y: y[1])[0] if bp else "—"
        out.append({**x, "buy_point": main,
                    "freq": st.get("freq_label"),
                    "decay": st.get("decay"),
                    "delist": st.get("delist_n") or 0,
                    "boards": st.get("boards") or {},
                    "best_h": st.get("best_horizon")})
    return out


def _outside(conn, date):
    try:
        gm = [dict(r) for r in conn.execute(
            "SELECT code,name,pct FROM global_market WHERE trade_date=? "
            "ORDER BY code", (date,))]
    except Exception:
        gm = []
    try:
        news = [dict(r) for r in conn.execute(
            "SELECT ts,title,source FROM news ORDER BY ts DESC LIMIT 15")]
    except Exception:
        news = []
    return {"global": gm, "news": news}


# ------------------------------------------------------------------ 视图层

CSS = """
*{box-sizing:border-box}
body{margin:0;background:#0f1216;color:#d7dde5;
 font:13px/1.55 -apple-system,"PingFang SC","Microsoft YaHei",sans-serif}
a{color:#5aa9e6;text-decoration:none}
header{position:sticky;top:0;z-index:9;background:#151a21;
 border-bottom:1px solid #232b36;padding:10px 18px;
 display:flex;align-items:baseline;gap:18px;flex-wrap:wrap}
h1{font-size:15px;margin:0;font-weight:600;letter-spacing:.4px}
.muted{color:#7b8798}
main{padding:16px 18px 60px;max-width:1680px;margin:0 auto}
.grid{display:grid;gap:14px}
.g2{grid-template-columns:repeat(auto-fit,minmax(420px,1fr))}
.g3{grid-template-columns:repeat(auto-fit,minmax(300px,1fr))}
.card{background:#151a21;border:1px solid #232b36;border-radius:8px;
 padding:13px 15px;margin-bottom:14px}
.card h2{font-size:12px;margin:0 0 10px;color:#9fb0c4;font-weight:600;
 letter-spacing:.6px;text-transform:uppercase}
.card h2 .tag{text-transform:none;letter-spacing:0;font-weight:400}
table{width:100%;border-collapse:collapse;font-size:12.5px}
th{text-align:left;color:#7b8798;font-weight:500;padding:5px 7px;
 border-bottom:1px solid #232b36;white-space:nowrap}
td{padding:5px 7px;border-bottom:1px solid #1b2129;vertical-align:top}
tr:last-child td{border-bottom:none}
tbody tr:hover{background:#1a2029}
.num{text-align:right;font-variant-numeric:tabular-nums;white-space:nowrap}
.pos{color:#f2545b}.neg{color:#3ecf8e}
.big{font-size:30px;font-weight:600;line-height:1.1}
.kv{display:flex;flex-wrap:wrap;gap:16px 26px}
.kv div{min-width:78px}
.kv .l{font-size:11px;color:#7b8798;margin-bottom:2px}
.kv .v{font-size:17px;font-weight:600;font-variant-numeric:tabular-nums}
.pill{display:inline-block;padding:1px 7px;border-radius:9px;
 font-size:11px;line-height:17px;white-space:nowrap}
.p-st{background:#2a2f3a;color:#c3ccd8}
.p-ok{background:#14382a;color:#5fd39b}
.p-warn{background:#3d3416;color:#e0be5c}
.p-bad{background:#3d1c1f;color:#f08388}
.p-low{background:#14382a;color:#5fd39b}
.p-high{background:#3d1c1f;color:#f08388}
.p-mid{background:#2a2f3a;color:#a9b6c6}
.spark{font-family:ui-monospace,Menlo,monospace;color:#5aa9e6;
 letter-spacing:-1px}
.ca{color:#93a3b6;font-size:11.5px;margin-top:3px}
.ca b{color:#c3ccd8;font-weight:500}
.note{background:#171d26;border-left:3px solid #3a4757;padding:8px 11px;
 border-radius:0 5px 5px 0;color:#93a3b6;font-size:12px;margin:9px 0 0}
.note.warn{border-left-color:#8a6d1f;color:#d6bd76}
.note.ok{border-left-color:#1f6f4a;color:#7fd6a8}
.bar{height:6px;background:#232b36;border-radius:3px;overflow:hidden;
 margin-top:5px}
.bar i{display:block;height:100%}
.empty{color:#7b8798;padding:12px 0}
.bar-wrap{min-width:90px}
"""


def _e(v):
    return html.escape(str(v if v is not None else ""))


def _n(v, nd=2, plus=False):
    if v is None or v == "":
        return "—"
    try:
        x = float(v)
    except (TypeError, ValueError):
        return _e(v)
    return f"{x:+.{nd}f}" if plus else f"{x:.{nd}f}"


def _cls(v):
    if v is None:
        return ""
    try:
        x = float(v)
    except (TypeError, ValueError):
        return ""
    return "pos" if x > 0 else ("neg" if x < 0 else "")


def _pct_cls(v):
    """A 股习惯：涨红跌绿。"""
    return _cls(v)


def _stage_pill(stage, temp):
    if not stage:
        return '<span class="pill p-st">未知</span>'
    k = {"冰点": "p-bad", "退潮": "p-bad", "修复": "p-warn",
         "发酵": "p-st", "高潮": "p-ok", "分歧": "p-warn"}.get(stage, "p-st")
    # 温度与阶段冲突时提示
    warn = ""
    if temp is not None:
        if stage == "高潮" and temp < 50:
            warn = " ⚠"
        elif stage in ("冰点", "退潮") and temp > 60:
            warn = " ⚠"
    return f'<span class="pill {k}">{_e(stage)}{warn}</span>'


def _spark(vals):
    """把一串数值画成 Unicode 火花线。"""
    if not vals:
        return ""
    blocks = "▁▂▃▄▅▆▇█"
    lo, hi = min(vals), max(vals)
    if hi == lo:
        return blocks[3] * len(vals)
    return "".join(blocks[min(7, int((v - lo) / (hi - lo) * 7.99))]
                   for v in vals)


def _brief(d):
    """今日要点：把"现在什么状态、该看什么"压缩成 4 条。"""
    em = d.get("emotion") or {}
    row = em.get("row") or {}
    st = d.get("seats") or {}
    tr = d.get("track") or {}
    fr = d.get("freshness") or {}
    th = d.get("themes") or {}

    temp = row.get("temperature")
    stage = row.get("stage") or "未知"
    # 阶段对应的一般性操作提示（**不是预测**，是风险偏好提示）
    hint = {"冰点": "情绪低位，容错高但需等右侧确认",
            "修复": "弱转强阶段，试错成本仍偏高",
            "发酵": "情绪上行，可正常参与但别追高板",
            "高潮": "情绪过热，**追高最危险**，减仓为主",
            "退潮": "亏钱效应扩散，**空仓等冰点**更优"}.get(
                stage, "状态不明，降低仓位")
    if row.get("prev_zt_premium") is not None and \
            row["prev_zt_premium"] < 0:
        hint += "；昨日涨停今天亏钱"

    follow = st.get("follow") or []
    low = [x for x in follow if (x.get("act") or 0) <= 2]
    themes = th.get("ranked") or []

    p = ["<div class='card' style='border-color:#2f3d4f'>"
         "<h2>今日要点 <span class='tag muted'>—— 3 秒看完"
         "</span></h2><div class='kv'>"]
    p.append(f"<div><div class='l'>情绪</div><div class='v'>"
             f"{_n(temp,0)} <span style='font-size:13px'>"
             f"{_stage_pill(stage, temp)}</span></div></div>")
    p.append(f"<div><div class='l'>打不打得动</div>"
             f"<div class='v' style='font-size:13px'>{_e(hint)}</div></div>")
    p.append(f"<div><div class='l'>跟随候选</div><div class='v'>"
             f"{len(follow)} <span style='font-size:12px' class='muted'>"
             f"个{'，其中低频 ' + str(len(low)) + ' 个' if low else ''}"
             f"</span></div></div>")
    if themes:
        t0 = themes[0]
        p.append(f"<div><div class='l'>最热题材</div>"
                 f"<div class='v' style='font-size:14px'>"
                 f"{_e(t0.get('tag'))} "
                 f"<span class='muted' style='font-size:12px'>"
                 f"昨 {_e(t0.get('prev_zt'))} 家</span></div></div>")
    b = (tr.get("buyable") or {}).get("d5") if tr.get("ok") else None
    if b:
        p.append(f"<div><div class='l'>留痕战绩 D5</div>"
                 f"<div class='v {_cls(b['mean_net'])}' "
                 f"style='font-size:15px'>{_n(b['mean_net'],1,True)}%"
                 f" <span class='muted' style='font-size:12px'>"
                 f"胜率 {_n(b['win'],0)}%</span></div></div>")
    p.append("</div>")
    if not fr.get("is_today", True):
        p.append("<div class='note warn' style='margin-top:8px'>"
                 "数据不是今天，下面的数字可能已过期。"
                 "收盘后跑 <code>python3 run.py daily</code>。</div>")
    p.append("<div class='note' style='margin-top:8px'>"
             "操作摘要（<b>不是操作建议</b>）：<b>打不打得动</b>只反映"
             "当前情绪的容错高低，<b>不预测明天</b>。"
             "本项目实测次日情绪无法预测（优势 -0.5pp）。"
             "唯一有统计优势的是 ④ 栏席位跟随。</div>")
    p.append("</div>")
    return "".join(p)


def render(d):
    """渲染整页 HTML。"""
    if d.get("empty"):
        return (f"<!doctype html><meta charset='utf-8'>"
                f"<body style='background:#0f1216;color:#d7dde5;"
                f"font-family:sans-serif;padding:40px'>"
                f"<h2>暂无数据</h2><p class='muted'>先采集："
                f"<code>python3 run.py daily</code></p>")

    date = d["date"]
    fr = d.get("freshness") or {}
    fresh = fr.get("is_today")
    dot = "#3ecf8e" if fresh else "#e0be5c"
    stale_txt = "" if fresh else " ⚠ 数据不是今天"
    src_txt = " · ".join(
        f"{s['label']} {_e(s['date'] or '无')}"
        + ("" if s["ok"] else "⚠")
        for s in (fr.get("sources") or []))
    p = [f"<!doctype html><html lang='zh-CN'><meta charset='utf-8'>"
         f"<meta name='viewport' content='width=device-width,initial-scale=1'>"
         f"<title>短线情绪看板 {_e(date)}</title>"
         f"<style>{CSS}</style>"
         f"<header><h1>短线情绪看板</h1>"
         f"<span class='muted'>"
         f"<span style='display:inline-block;width:8px;height:8px;"
         f"border-radius:50%;background:{dot};margin-right:6px'></span>"
         f"基准日 <b style='color:#d7dde5'>{_e(date)}</b>{stale_txt}"
         f" · 生成于 {_e(d['generated_at'])} · 每 60 秒自动刷新</span>"
         f"<span class='muted' style='font-size:11.5px'>{src_txt}</span>"
         f"<span style='margin-left:auto' class='muted'>"
         f"<a href='/'>刷新</a></span></header><main>"]
    # 数据陈旧时给出醒目提示与更新命令
    if fr and not fresh:
        last = (fr.get("last_collect") or "")[:16].replace("T", " ")
        p.append(
            f"<div class='card' style='border-color:#8a6d1f'>"
            f"<b style='color:#e0be5c'>⚠ 数据不是今天（今天 {_e(fr.get('today'))}，"
            f"看板基准日 {_e(date)}）</b>"
            f"<div class='muted' style='margin-top:5px'>"
            f"最后采集时间：{_e(last or '未知')}。"
            f"看板不会自动采集（采集要打外部接口）。"
            f"收盘后请手动跑一次：<code>python3 run.py daily</code>，"
            f"刷新本页即可看到当天数据。</div></div>")

    # ---------- 0. 今日要点（一屏结论）
    p.append(_brief(d))

    # ---------- 1. 情绪周期
    em = d.get("emotion") or {}
    row, mkt = em.get("row"), em.get("market")
    p.append("<div class='card'><h2>① 情绪周期 "
             "<span class='tag muted'>—— 描述当下状态，"
             "<b>不预测明天</b></span></h2>")
    if row:
        t = row.get("temperature")
        p.append("<div class='kv'>")
        p.append(f"<div><div class='l'>情绪温度</div>"
                 f"<div class='v'>{_n(t,1)}</div>"
                 f"<div class='muted' style='font-size:11px'>"
                 f"历史分位 {_n(em.get('pctile'),0)}%</div></div>")
        p.append(f"<div><div class='l'>阶段</div><div class='v'>"
                 f"{_stage_pill(row.get('stage'), t)}</div></div>")
        p.append(f"<div><div class='l'>封板率</div><div class='v'>"
                 f"{_n((row.get('seal_rate') or 0)*100,1)}%</div></div>")
        p.append(f"<div><div class='l'>昨涨停溢价</div>"
                 f"<div class='v {_cls(row.get('prev_zt_premium'))}'>"
                 f"{_n(row.get('prev_zt_premium'),2,True)}%</div></div>")
        p.append(f"<div><div class='l'>晋级率</div><div class='v'>"
                 f"{_n((row.get('promote_rate') or 0)*100,1)}%</div></div>")
        if mkt:
            p.append(f"<div><div class='l'>涨停家数</div><div class='v'>"
                     f"{_e(mkt.get('zt_count'))}</div></div>")
            p.append(f"<div><div class='l'>最高板</div><div class='v'>"
                     f"{_e(mkt.get('max_height'))}</div></div>")
            p.append(f"<div><div class='l'>跌停/炸板</div><div class='v'>"
                     f"{_e(mkt.get('dt_count'))}/{_e(mkt.get('zb_count'))}"
                     f"</div></div>")
            if mkt.get("sh_pct") is not None:
                p.append(f"<div><div class='l'>上证</div>"
                         f"<div class='v {_pct_cls(mkt.get('sh_pct'))}'>"
                         f"{_n(mkt.get('sh_pct'),2,True)}%</div></div>")
        p.append("</div>")
        # 情绪温度条
        tv = max(0, min(100, t or 0))
        col = ("#f2545b" if tv >= 70 else
               "#e0be5c" if tv >= 45 else "#3ecf8e")
        p.append(f"<div class='bar'><i style='width:{tv}%;background:{col}'>"
                 f"</i></div>")
        p.append(f"<div class='muted' style='font-size:11px;margin-top:4px'>"
                 f"{_e(row.get('stage_reason') or '')}</div>")
    else:
        p.append("<div class='empty'>当日情绪未计算，"
                 "先跑 <code>python3 run.py analyze</code></div>")

    # 近 10 日走势
    hist = em.get("history") or []
    if hist:
        p.append("<table style='margin-top:10px'><thead><tr>"
                 "<th>日期</th><th>阶段</th><th class='num'>温度</th>"
                 "<th class='num'>涨停</th><th class='num'>溢价</th>"
                 "<th>温度走势</th></tr></thead><tbody>")
        temps = [h.get("temperature") or 0 for h in hist]
        sp = _spark(temps)
        for i, h in enumerate(hist):
            p.append(
                f"<tr><td>{_e(h.get('trade_date'))}</td>"
                f"<td>{_stage_pill(h.get('stage'), h.get('temperature'))}"
                f"</td>"
                f"<td class='num'>{_n(h.get('temperature'),1)}</td>"
                f"<td class='num'>{_e(h.get('zt_count'))}</td>"
                f"<td class='num {_cls(h.get('prev_zt_premium'))}'>"
                f"{_n(h.get('prev_zt_premium'),2,True)}</td>"
                f"<td class='spark'>{sp[i]}</td></tr>")
        p.append("</tbody></table>")
    p.append("<div class='note'>情绪周期用于判断<b>现在处于哪个阶段</b>"
             "（有效）；但本工具实测<b>次日情绪无法预测</b>"
             "（命中率 80.0% vs 基准率 80.5%，优势 -0.5pp，"
             "相关系数 -0.207）。<b>不要据此择时。</b></div>")
    p.append("</div>")

    # ---------- 2. 新题材
    th = d.get("themes") or {}
    p.append("<div class='card'><h2>② 新题材 &amp; 延续性 "
             "<span class='tag muted'>—— 想象力看原文，判断在你自己"
             "</span></h2>")
    news = th.get("new") or []
    if news:
        p.append("<table><thead><tr><th>题材</th><th class='num'>涨停</th>"
                 "<th class='num'>最高板</th><th>催化（原文）</th>"
                 "</tr></thead><tbody>")
        for t in news[:10]:
            tag = t.get("tag") or t.get("name") or "—"
            cat = ""
            try:
                c = theme.theme_catalyst.__name__  # noqa: F841
            except Exception:
                pass
            p.append(f"<tr><td><b>{_e(tag)}</b></td>"
                     f"<td class='num'>{_e(t.get('zt_count') or t.get('limit_up_num'))}</td>"
                     f"<td class='num'>{_e(t.get('max_height') or t.get('high'))}</td>"
                     f"<td class='ca'>{_e(t.get('reason') or cat or '—')}</td></tr>")
        p.append("</tbody></table>")
    else:
        p.append("<div class='empty'>当日无新题材（或题材数据未采集）</div>")

    ranked = th.get("ranked") or []
    if ranked:
        # 按"真正有效的那个特征"重排。用 prev_zt（昨日家数）而不是
        # zt_count（当日家数）——因为前者才是 validate_persistence_score
        # 里被验证过的那个：prev_zt_corr = +0.332，而复合评分只有 +0.055。
        # （另测：当日家数 zt_count 对次日家数相关 +0.278，是另一个特征，
        #  别和 +0.332 混为一谈——这两个数字口径不同，都成立。）
        ranked = sorted(ranked, key=lambda x: -(x.get("prev_zt") or 0))
        p.append("<div style='margin-top:14px'>"
                 "<b>按「昨日涨停家数」排序</b>"
                 "<span class='muted'>（这是唯一实测有效的题材特征："
                 "与「未来 1~3 日该题材是否还有涨停」相关 <b>+0.332</b>；"
                 "6 项加权综合评分只有 <b>+0.055</b>，约 6 倍差距，"
                 "所以不用评分排序）</span></div>")
        p.append("<table><thead><tr><th>题材</th>"
                 "<th class='num'>昨家数<span class='muted'>"
                 "（有效）</span></th>"
                 "<th class='num'>当日家数</th>"
                 "<th class='num'>高度</th>"
                 "<th class='num'>评分<span class='muted'>"
                 "（无效）</span></th><th>走势</th>"
                 "<th>催化原文（判断想象力的材料）</th>"
                 "</tr></thead><tbody>")
        for s in ranked:
            cats = s.get("catalysts") or []
            txt = " ｜ ".join(
                (c.get("reason_info") or c.get("reason") or "")
                for c in cats if (c.get("reason_info") or c.get("reason")))
            p.append(
                f"<tr><td><b>{_e(s.get('tag'))}</b></td>"
                f"<td class='num'><b>{_e(s.get('prev_zt'))}</b></td>"
                f"<td class='num'>{_e(s.get('zt_count'))}</td>"
                f"<td class='num'>{_e(s.get('leader_height'))}</td>"
                f"<td class='num muted'>{_n(s.get('score'),1)}</td>"
                f"<td class='spark'>{_e(s.get('spark'))}</td>"
                f"<td class='ca'>{_e(txt[:190])}</td></tr>")
        p.append("</tbody></table>")
    p.append(
        "<div class='note'>"
        "<b>想象力 / 延续空间怎么判断（不给你打分，给你清单）：</b>"
        "题材的叙事空间无法从量价数据测出来，请对着催化原文逐条问——"
        "<br>① <b>可外推吗</b>：是单点事件（一次中标、一份订单）"
        "还是持续过程（产业趋势、政策周期）？"
        "<br>② <b>天花板在哪</b>：市场空间能否算得清、够不够大？"
        "算不清的（如「AI+」）通常比算得清的（如某笔订单）更容易反复炒。"
        "<br>③ <b>有对标吗</b>：海外/历史有没有同类行情？"
        "有对标的题材更容易形成共识。"
        "<br>④ <b>能证伪吗</b>：多久能被业绩证实或证伪？"
        "太快的（当期财报就能看到）炒不长，太慢的容易断。"
        "<br>⑤ <b>今天是第几天</b>：用上面「走势」列看家数变化，"
        "第 1-2 天与第 5 天之后的风险完全不同。"
        "<br>⑥ <b>谁在买</b>：去第 ④ 栏看该题材龙头是否上了龙虎榜、"
        "买的是高胜率低频席位还是聚合标签（后者是反向信号）。"
        "</div>")
    p.append("<div class='note warn'>"
             "<b>两个必须知道的事：</b><br>"
             "① <b>延续性评分不预测延续。</b>实测个股层面 Spearman "
             "<b>-0.021</b>（训练 -0.016 / 留出 -0.029），1 板股甚至反向"
             "（评分 0-49 续板率 18.4% → 70-100 只有 13.2%）。"
             "题材层面也只有 <b>+0.055</b>，"
             "<b>不如直接看昨日涨停家数（+0.332，约 6 倍）</b>"
             "——所以上表<b>按昨家数排序</b>，评分列已置灰。<br>"
             "② <b>想象力不做成数字。</b>题材的叙事空间无法从量价数据"
             "测出来；把直觉指标加权成评分已在本项目被证伪两次。"
             "请直接读<b>催化原文</b>自己判断——看它是"
             "「有没有后续催化空间、是否可外推、是否有产业趋势支撑」。"
             "</div>")
    p.append("</div>")

    # ---------- 3. 龙头
    ld = d.get("leaders") or {}
    p.append("<div class='card'><h2>③ 龙头 "
             "<span class='tag muted'>—— 看高度可以，"
             "<b>追它不行</b></span></h2>")
    mk = ld.get("market") or []
    if mk:
        p.append("<table><thead><tr><th>代码</th><th>名称</th>"
                 "<th class='num'>连板</th><th class='num'>涨幅</th>"
                 "<th class='num'>换手</th><th class='num'>封单(亿)</th>"
                 "<th>首封</th><th>题材</th></tr></thead><tbody>")
        for r in mk:
            sa = r.get("seal_amount")
            p.append(
                f"<tr><td>{_e(r.get('code'))}</td><td><b>{_e(r.get('name'))}</b></td>"
                f"<td class='num'>{_e(r.get('height'))}</td>"
                f"<td class='num {_pct_cls(r.get('pct'))}'>"
                f"{_n(r.get('pct'),2,True)}%</td>"
                f"<td class='num'>{_n(r.get('turnover'),1)}%</td>"
                f"<td class='num'>{_n((sa or 0)/1e8 if sa else None,2)}</td>"
                f"<td class='muted'>{_e(r.get('first_seal') or '—')}</td>"
                f"<td class='ca'>{_e((r.get('reason') or '')[:26])}</td></tr>")
        p.append("</tbody></table>")
    else:
        p.append("<div class='empty'>无龙头数据</div>")
    p.append("<div class='note warn'><b>不要追高板。</b>"
             "实测「次日买入高连板」原始收益确实随板数上升"
             "（1板 +1.06% → 5板 +3.50%），但<b>买不进的比例同步上升</b>"
             "（13.7% → 37.7%），那些买不进的恰好贡献约 +10%。"
             "剔除买不进、控 beta、扣成本后<b>全部转负</b>"
             "（≥4板 训练 -1.19%、留出 -0.96%）。"
             "<b>龙头可以看，不能追。</b></div>")
    p.append("</div>")

    # ---------- 4. 大神风格（席位）
    st = d.get("seats") or {}
    _sd = st.get("date")
    _stag = ("" if not st.get("stale") else
             f"<span class='pill p-warn'>龙虎榜 {_e(_sd)} 披露</span> ")
    p.append("<div class='card'><h2>④ 大神风格 · 席位跟随 "
             "<span class='tag p-ok' style='padding:1px 7px'>"
             "本项目唯一通过样本外检验的信号</span> "
             f"{_stag}</h2>")
    follow = st.get("follow") or []
    if follow:
        p.append(f"<div class='muted' style='margin-bottom:6px'>"
                 f"胜率 ≥{_n(st.get('min_prob'),0)}% 且非涨停，"
                 f"已按<b>席位活跃度升序</b>排列（低频优先）</div>")
        p.append("<table><thead><tr><th>代码</th><th>名称</th>"
                 "<th class='num'>席位胜率</th><th>活跃度</th>"
                 "<th class='num'>净买(万)</th><th>席位</th>"
                 "<th>上榜原因</th></tr></thead><tbody>")
        for r in follow:
            a = r.get("act") or 0
            alab = ("低频" if a <= 2 else "中频" if a < 10 else "高频")
            acls = ("p-low" if a <= 2 else "p-mid" if a < 10 else "p-high")
            rp_ = r.get("rep_prob") or 0
            star = " ⭐" if rp_ >= config.SEAT_ELITE_REP_PROB else ""
            p.append(
                f"<tr><td>{_e(r.get('code'))}</td>"
                f"<td><b>{_e(r.get('name') or '—')}</b></td>"
                f"<td class='num'>{_n(rp_,0)}%{star}</td>"
                f"<td><span class='pill {acls}'>{alab}({a})</span></td>"
                f"<td class='num'>{(r.get('net_amt') or 0)/1e4:+.0f}</td>"
                f"<td class='ca'>{_e((r.get('seat') or '')[:30])}</td>"
                f"<td class='ca'>{_e((r.get('reason') or '')[:30])}</td></tr>")
        p.append("</tbody></table>")
        p.append("<div class='note ok'>"
                 "高胜率 + <b>低频</b>（60 日上榜 ≤2 次）可执行 D5 净 "
                 "<b>+5.40%</b>、胜率 60.8%；高胜率 + <b>高频</b>（≥10 次）"
                 " 只有 <b>-2.11%</b>。"
                 "持有到 <b>D5</b>（D1 净仅 +0.17%）。"
                 "⭐ = 胜率 ≥70%。</div>")
    else:
        p.append("<div class='empty'>当日无符合条件的跟随候选</div>")

    rank = st.get("rank") or []
    if rank:
        p.append("<div style='margin-top:14px'><b>高胜率席位榜</b>"
                 "<span class='muted'>（近 180 交易日，已排除聚合标签）"
                 "</span></div>")
        p.append("<table><thead><tr><th>席位</th><th class='num'>样本</th>"
                 "<th class='num'>次日均值</th><th class='num'>胜率</th>"
                 "<th class='num'>平台胜率</th></tr></thead><tbody>")
        for s in rank[:10]:
            p.append(
                f"<tr><td>{_e((s.get('seat') or '')[:38])}</td>"
                f"<td class='num'>{_e(s.get('trades'))}</td>"
                f"<td class='num {_cls(s.get('mean'))}'>"
                f"{_n(s.get('mean'),2,True)}%</td>"
                f"<td class='num'>{_n(s.get('win'),1)}%</td>"
                f"<td class='num muted'>{_n(s.get('mean_rep_prob'),0)}</td>"
                f"</tr>")
        p.append("</tbody></table>")
    styles = st.get("styles") or []
    if styles:
        p.append("<div style='margin-top:16px'><b>怎么学大神：行为画像</b>"
                 "<span class='muted'>（按可执行 D5 净收益排序；"
                 "学的是<b>可复现的行为</b>，不是「眼光」）</span></div>")
        p.append("<table><thead><tr><th>席位</th><th class='num'>n</th>"
                 "<th class='num'>D1</th><th class='num'>D5</th>"
                 "<th class='num'>D10</th><th class='num'>胜率</th>"
                 "<th>主要买点</th><th>频率</th><th>收益形态</th>"
                 "<th class='num'>退市股</th></tr></thead><tbody>")
        for x in styles:
            dl = x.get("delist") or 0
            dl_cell = (f"<span class='pill p-bad'>{dl}</span>" if dl
                       else "<span class='muted'>0</span>")
            p.append(
                f"<tr><td>{_e(x['seat'][:28])}</td>"
                f"<td class='num'>{x['n']}</td>"
                f"<td class='num {_cls(x['d1'])}'>"
                f"{_n(x['d1'],2,True)}%</td>"
                f"<td class='num {_cls(x['d5'])}'>"
                f"{_n(x['d5'],2,True)}%</td>"
                f"<td class='num {_cls(x['d10'])}'>"
                f"{_n(x['d10'],2,True)}%</td>"
                f"<td class='num'>{_n(x['d1_win'],0)}%</td>"
                f"<td class='ca'>{_e(x.get('buy_point') or '—')}</td>"
                f"<td class='ca'>{_e(x.get('freq') or '—')}</td>"
                f"<td class='ca'>{_e((x.get('decay') or '—')[:18])}</td>"
                f"<td class='num'>{dl_cell}</td></tr>")
        p.append("</tbody></table>")
        worst = st.get("worst") or []
        if worst:
            p.append("<div style='margin-top:14px'>"
                     "<b>反面教材：D5 最差的席位</b>"
                     "<span class='muted'>（只看「学谁」不够，"
                     "还要知道「别学谁」）</span></div>")
            p.append("<table><thead><tr><th>席位</th>"
                     "<th class='num'>n</th><th class='num'>D1</th>"
                     "<th class='num'>D5</th><th class='num'>D10</th>"
                     "<th>主要买点</th><th class='num'>退市股</th>"
                     "</tr></thead><tbody>")
            for x in worst:
                dl = x.get("delist") or 0
                dc = (f"<span class='pill p-bad'>{dl}</span>" if dl
                      else "<span class='muted'>0</span>")
                p.append(
                    f"<tr><td>{_e(x['seat'][:28])}</td>"
                    f"<td class='num'>{x['n']}</td>"
                    f"<td class='num {_cls(x['d1'])}'>"
                    f"{_n(x['d1'],2,True)}%</td>"
                    f"<td class='num neg'>{_n(x['d5'],2,True)}%</td>"
                    f"<td class='num {_cls(x['d10'])}'>"
                    f"{_n(x['d10'],2,True)}%</td>"
                    f"<td class='ca'>{_e(x.get('buy_point') or '—')}</td>"
                    f"<td class='num'>{dc}</td></tr>")
            p.append("</tbody></table>")
        p.append(
            "<div class='note'>"
            "<b>可学的是行为，不是「眼光」：</b><br>"
            "① <b>买点</b>：能确定的<b>只有负面那条</b>——"
            "「<b>逆势绿盘买入</b>」是最可靠的反面特征"
            "（182 席分组：逆势型 46 个 D5 中位 <b>-2.87%</b>；"
            "打板型 133 个中位 <b>-0.18%</b>，<b>同为负</b>）。"
            "别把打板当成功公式，上表那几个是<b>幸存者</b>。<br>"
            "② <b>频率</b>：出手太频实测更差（60日≥10 次，"
            "可执行 D5 <b>-2.11%</b>）。<br>"
            "③ <b>持有期</b>：看「收益形态」——<b>峰在 D5</b> 的才值得持有；"
            "「一日游脉冲」说明它靠隔夜溢价，你学不来。<br>"
            "④ <b>标的</b>：先查「退市股」列。实测有个 -30% 的"
            "「最差席位」，45 笔里 <b>24 笔买的是退市整理股</b>"
            "（立方退/长药退）——低收益未必是选股差，是标的会归零。"
            "</div>")
    agg = st.get("agg") or []
    if agg:
        p.append("<div class='note warn'><b>当日上榜的聚合标签（反向指标，"
                 "已从候选中排除）：</b> "
                 + "、".join(_e(a["seat"]) for a in agg)
                 + "。它们不是真实席位，实测 D1 -4%~-5%、胜率约 27%。"
                 "</div>")
    p.append("</div>")

    # ---------- 4.5 留痕战绩
    tr = d.get("track") or {}
    p.append("<div class='card'><h2>④b 留痕战绩 "
             "<span class='tag muted'>—— 看板过去到底说对了没有"
             "</span></h2>")
    if tr.get("ok") and tr.get("filled"):
        p.append(f"<div class='muted' style='margin-bottom:6px'>"
                 f"留痕 {tr['n']} 条，已回填 {tr['filled']} 条，"
                 f"其中买不进 {tr['locked_n']} 条"
                 + (f"（{tr['locked_share']:.1f}%）"
                    if tr.get("locked_share") is not None else "")
                 + f" · 净收益已扣 {tr['cost']:.2f}%</div>")
        p.append("<table><thead><tr><th>口径</th>"
                 "<th class='num'>n</th><th class='num'>均值</th>"
                 "<th class='num'>中位</th><th class='num'>净收益</th>"
                 "<th class='num'>胜率</th><th class='num'>最差</th>"
                 "</tr></thead><tbody>")
        for lab, nm in (("all", "全体（含买不进）"),
                        ("buyable", "剔除买不进")):
            for h in ("d1", "d5", "d10"):
                x = (tr.get(lab) or {}).get(h)
                if not x:
                    continue
                p.append(
                    f"<tr><td>{nm} <b>{h.upper()}</b></td>"
                    f"<td class='num'>{x['n']}</td>"
                    f"<td class='num {_cls(x['mean'])}'>"
                    f"{_n(x['mean'],2,True)}%</td>"
                    f"<td class='num {_cls(x['median'])}'>"
                    f"{_n(x['median'],2,True)}%</td>"
                    f"<td class='num {_cls(x['mean_net'])}'>"
                    f"{_n(x['mean_net'],2,True)}%</td>"
                    f"<td class='num'>{_n(x['win'],1)}%</td>"
                    f"<td class='num neg'>{_n(x['worst'],2,True)}%</td>"
                    f"</tr>")
        p.append("</tbody></table>")
        bb = (tr.get("buyable") or {}).get("d10")
        if bb and bb["median"] < 0 <= bb["mean"]:
            p.append("<div class='note warn'>"
                     "<b>D10 中位数为负、均值却为正。</b>"
                     "中位数 -"
                     f"{abs(bb['median']):.2f}% 才是<b>典型一笔</b>的结果；"
                     f"均值 +{bb['mean']:.2f}% 是被少数大赢家拉起来的。"
                     "说明持有到 D10 大部分是亏的。"
                     "<b>不要拉长持有期</b>——D5 中位仍为正。</div>")
        bd = tr.get("by_date") or []
        if bd:
            p.append("<div style='margin-top:12px'><b>逐日表现</b>"
                     "<span class='muted'>（最近 12 天）</span></div>")
            p.append("<table><thead><tr><th>信号日</th>"
                     "<th class='num'>候选</th><th class='num'>买不进</th>"
                     "<th class='num'>D1净</th><th class='num'>胜率</th>"
                     "</tr></thead><tbody>")
            for r in bd[:12]:
                dn = (f"{r['d1_net']:+.2f}%" if r["d1_net"] is not None
                      else "—")
                dw = (f"{r['d1_win']:.0f}%" if r["d1_win"] is not None
                      else "—")
                p.append(
                    f"<tr><td>{_e(r['date'])}</td>"
                    f"<td class='num'>{r['n']}</td>"
                    f"<td class='num'>{r['locked'] or '—'}</td>"
                    f"<td class='num {_cls(r['d1_net'])}'>{dn}</td>"
                    f"<td class='num'>{dw}</td></tr>")
            p.append("</tbody></table>")
        p.append("<div class='note'>看板<b>每次渲染都会自动记账</b>"
                 "（写进 signal_log），不需要你记得手动留痕——"
                 "忘记记账正是胜率无法改进的头号原因。"
                 "回填：<code>python3 run.py track fill</code></div>")
    else:
        p.append("<div class='empty'>暂无留痕。"
                 "首次采集后看板会自动开始记账。</div>")
    p.append("</div>")

    # ---------- 5. 外围
    ou = d.get("outside") or {}
    p.append("<div class='grid g2'>")
    gm = ou.get("global") or []
    p.append("<div class='card'><h2>⑤ 外围市场</h2>")
    if gm:
        p.append("<div class='kv'>")
        for g in gm:
            p.append(f"<div><div class='l'>{_e(g.get('name'))}</div>"
                     f"<div class='v {_pct_cls(g.get('pct'))}'>"
                     f"{_n(g.get('pct'),2,True)}%</div></div>")
        p.append("</div>")
    else:
        p.append("<div class='empty'>无当日外围数据</div>")
    p.append("</div>")

    nw = ou.get("news") or []
    p.append("<div class='card'><h2>⑥ 快讯</h2>")
    if nw:
        p.append("<table><tbody>")
        for n in nw[:12]:
            p.append(f"<tr><td class='muted' style='white-space:nowrap'>"
                     f"{_e((n.get('ts') or '')[11:16])}</td>"
                     f"<td>{_e((n.get('title') or '')[:88])}</td></tr>")
        p.append("</tbody></table>")
    else:
        p.append("<div class='empty'>暂无快讯</div>")
    p.append("</div></div>")

    # ---------- 汇总口径
    p.append("<div class='card'><h2>操作口径速查</h2>"
             "<table><thead><tr><th>问题</th><th>结论</th>"
             "<th>证据强度</th></tr></thead><tbody>"
             "<tr><td>情绪周期有用吗</td>"
             "<td>描述当下状态有效；预测明天无效</td>"
             "<td><span class='pill p-ok'>已验证</span></td></tr>"
             "<tr><td>次日情绪可预测吗</td>"
             "<td>不可。命中率 80.0% vs 基准率 80.5%</td>"
             "<td><span class='pill p-bad'>已证伪</span></td></tr>"
             "<tr><td>题材延续性评分可信吗</td>"
             "<td>不可。个股层面 -0.021，不如看涨停家数</td>"
             "<td><span class='pill p-bad'>已证伪</span></td></tr>"
             "<tr><td>追龙头能赚吗</td>"
             "<td>不能。买不进占 37.7%，可买者净超额为负</td>"
             "<td><span class='pill p-bad'>已证伪</span></td></tr>"
             "<tr><td>席位跟随有用吗</td>"
             "<td>有。高胜率+低频 D5 净 +5.40%，样本外一致</td>"
             "<td><span class='pill p-ok'>已验证</span></td></tr>"
             "<tr><td>为什么以 D5 为持有期</td>"
             "<td>D1 净仅 +0.17%，被跳空吃掉；D5 才是真实持有期</td>"
             "<td><span class='pill p-ok'>已验证</span></td></tr>"
             "</tbody></table>"
             "<div class='note'>本看板所有数字来自本地 "
             "2026-01~09 样本，<b>仅 9 个月一轮行情</b>，"
             "未覆盖完整牛熊。核心发现<b>无外部独立复现</b>。"
             "这是统计优势，不是保证。</div></div>")

    p.append("</main>"
             "<script>setTimeout(()=>location.reload(),60000)</script>")
    return "".join(p)


# ------------------------------------------------------------------ 服务层

class _Handler(BaseHTTPRequestHandler):
    server_version = "chaogu-dash"
    _lock = threading.Lock()

    def log_message(self, *a):
        pass                      # 静默：长期驻留不刷日志

    def _send(self, body, ctype="text/html; charset=utf-8", code=200):
        raw = body.encode("utf-8") if isinstance(body, str) else body
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        try:
            self.wfile.write(raw)
        except BrokenPipeError:
            pass

    def _reply(self, res):
        code, ctype, body = res
        return self._send(body, ctype, code)

    def do_GET(self):
        u = urlparse(self.path)
        path = u.path
        if path == "/favicon.ico":
            return self._send(b"", "image/x-icon", 204)
        # 新版看板（Grafana 式 + AI 生成）；旧版挪到 /legacy
        from . import webapi
        res = webapi.handle_get(path, u.query)
        if res is not None:
            return self._reply(res)
        # 串行化查库，避免 sqlite 多线程问题
        with _Handler._lock:
            conn = db.init()
            try:
                if path == "/api/data":
                    return self._send(
                        json.dumps(gather(conn), ensure_ascii=False,
                                   default=str),
                        "application/json; charset=utf-8")
                html_doc = render(gather(conn))
            except Exception as exc:                     # noqa: BLE001
                return self._send(
                    f"<!doctype html><meta charset='utf-8'>"
                    f"<body style='background:#0f1216;color:#f08388;"
                    f"font-family:monospace;padding:30px'>"
                    f"<h3>看板渲染失败</h3><pre>{html.escape(str(exc))}"
                    f"</pre>", code=500)
            finally:
                conn.close()
        self._send(html_doc)

    def do_POST(self):
        from . import webapi
        u = urlparse(self.path)
        n = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(n) if n else b""
        res = webapi.handle_post(u.path, body)
        if res is None:
            return self._send('{"error":"not found"}',
                              "application/json; charset=utf-8", 404)
        return self._reply(res)

    def do_DELETE(self):
        from . import webapi
        u = urlparse(self.path)
        res = webapi.handle_delete(u.path, u.query)
        if res is None:
            return self._send('{"error":"not found"}',
                              "application/json; charset=utf-8", 404)
        return self._reply(res)


# ---------------------------------------------------------------- 自动采集
#
# 看板本身只读、不采集，所以数据会过期。这个可选后台线程负责在
# 收盘后自动跑一次每日流程，让长期驻留的看板保持最新。
#
# **为什么默认关闭**：采集会打外部接口（东财/新浪/腾讯等），
# 属于有副作用的网络行为，不该在看板一启动就悄悄发生。
# 必须显式 `--autocollect` 才启用。

def _is_trading_day_guess(conn, date):
    """粗判是否交易日：库里该日已有涨停数据，或该日是工作日。

    没有节假日日历，所以只能"工作日 + 有数据"双重证据。
    宁可少跑（漏一天用户可手跑），也不要在节假日空跑打接口。
    """
    try:
        if conn.execute("SELECT 1 FROM limit_up WHERE trade_date=? LIMIT 1",
                        (date,)).fetchone():
            return True
    except Exception:
        pass
    return datetime.strptime(date, "%Y-%m-%d").weekday() < 5


# 收盘时间。涨停/题材这些字段只有在收盘后才定稿：
# 14:58 抓的涨停名单，最后两分钟可能开板或新封，不是终值。
_CLOSE_HHMM = "15:00"


def _already_collected(conn, date, require_post_close=True):
    """当天是否已**在收盘后**成功采集。

    为什么必须看时刻，不能只看 status=='ok'
    ----------------------------------------
    实测踩到的坑：2026-09-30 的 limit_up/theme 是 14:58:47 采的
    （盘中），turnover 也是 14:58:55 采的、状态 ok。于是 15:30 的
    自动采集判定"今天已采过"→ **直接跳过**，看板一整天显示的都是
    盘中未定稿的数据，而且不会自愈。

    所以这里额外要求：那条 turnover 记录的 ts 时刻 >= 15:00。
    盘中采的记录一律视为"未采集"，收盘后的自动采集会重新抓一遍；
    重抓后 ts 已在收盘后，不会反复重试。
    """
    try:
        if require_post_close:
            r = conn.execute(
                "SELECT status, ts FROM collect_log WHERE trade_date=? "
                "AND step='turnover' ORDER BY rowid DESC LIMIT 1",
                (date,)).fetchone()
            if not r or r[0] != "ok" or not r[1]:
                return False
            # ts 形如 2026-09-30T15:15:10；取时刻部分比较
            hhmm = str(r[1])[11:16]
            return bool(hhmm) and hhmm >= _CLOSE_HHMM
        r = conn.execute("SELECT status FROM collect_log WHERE trade_date=? "
                         "AND step='turnover' ORDER BY rowid DESC LIMIT 1",
                         (date,)).fetchone()
        return bool(r and r[0] == "ok")
    except Exception:
        return False


# 自动采集的落盘标记：统一走 _mark_key/_attempted_key，
# 不再各写一份 SQL（重复实现正是本项目反复踩的坑）。
_AC_KEY = "autocollect_attempt"
_LHB_KEY = "lhb_catchup"

# 龙虎榜披露时间：沪深交易所在收盘后约 17:30 公布当日榜单。
# 15:30 那轮 daily 里也会试着采一次，但那时数据还没发布，
# 拿到 0 行且**不会重试** —— 结果当天的榜单永远缺，④栏一直停在前一天。
# 所以 17:30 之后要单独补采一次。
_LHB_HHMM = "18:00"


def _mark_attempted(conn, date, note):
    """标记"今天已尝试采集"。

    为什么必须落盘：节假日是工作日但无行情，采集成功却无数据，
    _already_collected 恒为 False。若不落盘，看板每次重启都会
    在节假日重新打一轮接口。落盘后跨重启也不会重复。
    """
    _mark_key(conn, _AC_KEY, date, note, status="skip")


def _attempted_today(conn, date):
    return _attempted_key(conn, _AC_KEY, date)


def _autocollect_loop(stop, at="15:30"):
    """收盘后自动跑一次 daily；同一天只跑一次。"""
    from . import collect as _collect  # noqa: F401
    import subprocess
    import sys as _sys

    print(f"[autocollect] 已启用，每个工作日 {at} 之后自动采集一次")
    # 内存里记录"今天已经尝试过"，无论成败。
    # 关键：节假日是工作日但无行情，采集会成功但无数据，
    # _already_collected 永远为 False；只靠它会导致每 5 分钟重试一整天，
    # 反复打外部接口。用 attempted 集合兜住这种情况。
    attempted = set()
    lhb_done = set()
    while not stop.is_set():
        now = datetime.now()
        today = now.strftime("%Y-%m-%d")
        hhmm = now.strftime("%H:%M")
        # ---- 晚间补采龙虎榜（17:30 披露，15:30 那轮必然拿不到）----
        if hhmm >= _LHB_HHMM and today not in lhb_done:
            conn = None
            try:
                conn = db.init()
                done_ok, tries = _lhb_state(conn, today)
                if done_ok or tries >= _LHB_MAX_ATTEMPTS:
                    lhb_done.add(today)
                    if tries >= _LHB_MAX_ATTEMPTS and not done_ok:
                        print(f"[autocollect] {today} 龙虎榜补采已试 "
                              f"{tries} 次仍无数据，今日放弃"
                              f"（休市或披露延迟）", flush=True)
                else:
                    have = conn.execute(
                        "SELECT COUNT(*) FROM billboard WHERE trade_date=?",
                        (today,)).fetchone()[0]
                    if have:
                        _mark_key(conn, _LHB_KEY, today, f"已有{have}行")
                        lhb_done.add(today)
                        print(f"[autocollect] {today} 龙虎榜已就绪"
                              f"（{have} 行），补采跳过", flush=True)
                    else:
                        lhb_done.add(today)
                        print(f"[autocollect] {today} 补采龙虎榜…",
                              flush=True)
                        root = str(__import__("pathlib").Path(__file__)
                                   .resolve().parent.parent)
                        r = subprocess.run(
                            [_sys.executable, "run.py", "lhb",
                             "--date", today],
                            cwd=root, capture_output=True, text=True,
                            timeout=900)
                        ok = r.returncode == 0
                        n2 = 0
                        try:
                            c3 = db.init()
                            n2 = c3.execute(
                                "SELECT COUNT(*) FROM billboard "
                                "WHERE trade_date=?", (today,)).fetchone()[0]
                            c3.close()
                        except Exception:
                            pass
                        print(f"[autocollect] {today} 龙虎榜补采"
                              f"{'完成' if ok else '失败'}，{n2} 行",
                              flush=True)
                        if not ok:
                            print((r.stderr or r.stdout or "")[-500:])
                        try:
                            c4 = db.init()
                            # 关键：只有真拿到行才算成功。0 行时记 empty，
                            # 下个 tick 继续试（受 _LHB_MAX_ATTEMPTS 约束）。
                            # 之前这里无条件写 "ok"，导致 15:41 试采拿到
                            # 0 行也标记完成，18:00 真正披露时不再补采。
                            st = ("ok" if (ok and n2 > 0)
                                  else "empty" if ok else "fail")
                            _mark_key(c4, _LHB_KEY, today,
                                      f"补采 {n2} 行", status=st)
                            c4.close()
                        except Exception:
                            pass
                        # 席位数据变了，重建指标与预测
                        if ok and n2:
                            try:
                                subprocess.run(
                                    [_sys.executable, "run.py",
                                     "report", "--date", today],
                                    cwd=root, capture_output=True,
                                    text=True, timeout=600)
                            except Exception:
                                pass
            except Exception as exc:
                print(f"[autocollect] 龙虎榜补采异常（不影响看板）：{exc}")
            finally:
                if conn is not None:
                    try:
                        conn.close()
                    except Exception:
                        pass

        if hhmm >= at and today not in attempted:
            conn = None
            try:
                conn = db.init()
                if _already_collected(conn, today) or _attempted_today(conn, today):
                    # 静默跳过是正确逻辑，但对一个要跑几个月的服务来说，
                    # "什么都没打印"和"卡死了"看起来一模一样。
                    # 所以每天留一行心跳，证明循环还活着、数据已是最新。
                    print(f"[autocollect] {today} 数据已是最新，跳过"
                          f"（{datetime.now().strftime('%H:%M:%S')} 心跳）",
                          flush=True)
                    attempted.add(today)
                elif not _is_trading_day_guess(conn, today):
                    _mark_attempted(conn, today, "非交易日")
                    attempted.add(today)
                else:
                    conn.close()
                    conn = None
                    # 先记账再采集：即使中途崩溃也不重复打接口
                    attempted.add(today)
                    print(f"[autocollect] {today} 开始采集…")
                    root = str(__import__("pathlib").Path(__file__)
                               .resolve().parent.parent)
                    r = subprocess.run(
                        [_sys.executable, "run.py", "daily", "--date", today],
                        cwd=root, capture_output=True, text=True, timeout=1800)
                    ok = r.returncode == 0
                    print(f"[autocollect] {today} 采集"
                          f"{'完成' if ok else '失败'}")
                    if not ok:
                        print((r.stderr or r.stdout or "")[-600:])
                    try:
                        c2 = db.init()
                        _mark_attempted(c2, today,
                                        "采集完成" if ok else "采集失败")
                        c2.close()
                    except Exception:
                        pass
            except Exception as exc:                     # noqa: BLE001
                print(f"[autocollect] 异常（不影响看板）：{exc}")
            finally:
                if conn is not None:
                    try:
                        conn.close()
                    except Exception:
                        pass
        # 每 5 分钟检查一次；用 wait 以便快速响应停止信号
        stop.wait(300)


def serve(host="127.0.0.1", port=8787, autocollect=False, at="15:30",
          live_refresh=True):
    httpd = ThreadingHTTPServer((host, port), _Handler)
    httpd.daemon_threads = True
    url = f"http://{host}:{port}/"
    print(f"看板已启动：{url}")
    print(f"  旧版：{url}legacy    API：{url}api/meta")

    stop = threading.Event()
    if live_refresh:
        from . import live
        live.start(stop)
        print("  实时刷新已启用：快讯 30s；交易时段涨停/炸板 60s、题材 3min")
    else:
        print("  实时刷新未启用（去掉 --no-live 可开启）")
    if autocollect:
        t = threading.Thread(target=_autocollect_loop, args=(stop, at),
                             daemon=True)
        t.start()
    else:
        print("  自动采集未启用（加 --autocollect 可在收盘后自动采集）")

    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止。")
    finally:
        stop.set()
        httpd.server_close()
