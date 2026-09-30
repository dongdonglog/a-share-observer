#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""复盘报告生成（Markdown）。"""

from __future__ import annotations

import json
from datetime import datetime

from . import config, db, forecast as fc, metrics, seat, theme as th


def _f(v, nd=2, dash="—"):
    if v is None:
        return dash
    if isinstance(v, float):
        return f"{v:.{nd}f}"
    return str(v)


def _pct(v, nd=2, dash="—"):
    return dash if v is None else f"{v:.{nd}f}%"


def _bar(score, width=20):
    if score is None:
        return ""
    n = int(round(score / 100 * width))
    return "█" * n + "·" * (width - n)


def _focus_section(conn, A, date, m, e):
    """今日焦点：把"该盯哪些板块、哪些龙头"结论提到最前面。

    这是整份报告里唯一"结论先行"的部分。前面所有计算都是为它服务的：
    短线交易者打开报告第一眼要知道的就是——今天的主线是什么、
    龙头是谁、有什么风险。
    """
    A("## 今日焦点")
    A("")

    # ---- 一句话状态
    stage = e.get("stage") or "—"
    temp = e.get("temperature")
    prem = e.get("prev_zt_premium")
    A(f"**情绪：{stage}**（温度 {_f(temp, 1)}）｜ "
      f"赚钱效应 **{_pct(prem)}** ｜ "
      f"涨停 {m.get('zt_count')} / 炸板 {m.get('zb_count')} / "
      f"最高 {m.get('max_height')} 板")
    A("")

    # ---- 空间板（全市场最高连板）—— 短线最该盯的一只
    top = conn.execute(
        "SELECT * FROM limit_up WHERE trade_date=? "
        "ORDER BY height DESC, COALESCE(first_seal,'99:99:99') ASC LIMIT 3",
        (date,)).fetchall()
    if top:
        t0 = dict(top[0])
        rmap = th.stock_reason_map(conn, date)
        A(f"**空间板：{t0['name']}（{t0['height']}板，{t0['code']}）** "
          f"｜题材：{rmap.get(t0['code']) or '—'}｜"
          f"换手 {_pct(t0.get('turnover'))}｜"
          f"首封 {t0.get('first_seal') or '—'}"
          f"{'｜一字板' if t0.get('lu_type') == '一字板' else ''}")
        if len(top) > 1:
            A("")
            A("次高标：" + "、".join(
                f"{r['name']}({r['height']}板)" for r in top[1:]))
        A("")

    # ---- 核心主线
    ranks = th.lifecycle(conn, date, min_zt=2, top=5)
    if ranks:
        A("### 核心主线（按延续性排序）")
        A("")
        A("| 题材 | 延续性 | 阶段 | 涨停 | 近5日家数 | 龙头 | 高度 |")
        A("|---|---|---|---|---|---|---|")
        for s in ranks:
            ser = "→".join(str(v) for v in s["series"])
            A(f"| **{s['tag']}** | {s['score']:.1f} | {s['phase']} | "
              f"{s['zt_count']} | {ser} | **{s['leader']}** | "
              f"{s['leader_height']}板 |")
        A("")

        # 龙头详情（含催化）
        A("**龙头与催化**")
        A("")
        seen = set()
        if top:
            seen.add(dict(top[0])["code"])       # 空间板已在上面单独讲
        for s in ranks[:5]:
            for st in th._series_leaders(conn, date, s["tag"], limit=1):
                if st["code"] in seen:
                    continue
                seen.add(st["code"])
                info = (st.get("reason_info") or "").split("（免责声明")[0]
                info = " ".join(info.split())[:150]
                A(f"- **{st['name']}（{st.get('continue_num')}板）** "
                  f"— {s['tag']}｜题材：{st.get('reason') or '—'}")
                if info:
                    A(f"  - {info}")
        A("")

    # ---- 新题材
    newt = th.detect_new(conn, date)
    if newt:
        A("### 新题材 / 异动启动")
        A("")
        for t in newt[:6]:
            A(f"- **{t['tag']}**（{t['kind']}）涨停 {t['zt_count']} 只，"
              f"最高 {t['max_height']} 板，龙头 **{t['leader']}**")
        A("")

    # ---- 风险
    risks = []
    dec = [s for s in ranks if s["phase"] == "衰减"]
    if dec:
        risks.append("延续性居前的题材中，**" +
                     "、".join(s["tag"] for s in dec) +
                     "** 的涨停家数在衰减")
    zb = m.get("zb_count") or 0
    if (m.get("zt_count") or 0) and zb:
        sr = (m["zt_count"] / (m["zt_count"] + zb))
        if sr < config.SEAL_RATE_WARN:
            risks.append(f"封板率仅 {sr * 100:.0f}%，封板质量偏差")
    if prem is not None and prem < 0:
        risks.append(f"赚钱效应为负（{prem:+.2f}%），接力意愿弱")
    hi = conn.execute(
        "SELECT name, height, open_num FROM limit_up WHERE trade_date=? "
        "AND height>=4 AND COALESCE(open_num,0)>=2 ORDER BY height DESC",
        (date,)).fetchall()
    if hi:
        risks.append("高位股出现开板：" + "、".join(
            f"{r['name']}({r['height']}板/开板{r['open_num']}次)" for r in hi))
    dan = conn.execute(
        "SELECT name, pct FROM broken_board WHERE trade_date=? "
        "AND pct >= 5 ORDER BY pct DESC LIMIT 5", (date,)).fetchall()
    if dan:
        risks.append("炸板（曾涨停未封住）：" + "、".join(
            f"{r['name']}({r['pct']:+.1f}%)" for r in dan))

    if risks:
        A("### 需要留意")
        A("")
        for r in risks:
            A(f"- ⚠️ {r}")
        A("")

    # ---- 席位信号（唯一经过样本外验证的正优势信号）
    try:
        sig = seat.follow_signals(
            conn, date, min_rep_prob=config.SEAT_STRONG_REP_PROB,
            exclude_limit_up=True, top=5)
    except Exception:
        sig = []
    if sig:
        A("### 席位跟随候选（已验证信号）")
        A("")
        A("> 这是本报告中**唯一通过样本外检验**的信号：席位胜率与次日收益"
          "相关 +0.19，TOP 席位样本外仍 +1.85%（对照基准 -0.26%）。"
          "详见第九节。")
        A("")
        A("| 代码 | 名称 | 席位胜率 | 席位活跃度 | 净买(万) | 席位 |")
        A("|---|---|---|---|---|---|")
        for r in sig:
            mark = " ⭐" if r["rep_prob"] >= config.SEAT_ELITE_REP_PROB else ""
            a = r.get("act")
            alab = "—" if a is None else ("低频" if a <= 2 else
                                         ("中频" if a < 10 else "**高频**"))
            A(f"| {r['code']} | {r['name'] or '—'} | "
              f"{r['rep_prob']:.0f}%{mark} | {alab}({a}) | "
              f"{(r['net_amt'] or 0) / 1e4:+.0f} | {(r['seat'] or '')[:22]} |")
        A("")
        A(f"> ⭐ = 席位胜率 ≥ {config.SEAT_ELITE_REP_PROB:.0f}%。"
          f"活跃度 = 该席位过去 60 交易日上榜次数。")
        A("> 已验证：高胜率 + **低频** 的 D5 净 +5.95%；"
          "高胜率 + **高频** 则 -2.41%（诚实协议验证，非过拟合）。")
        A("> 候选已按活跃度升序排列。")
        A("")

    # ---- 提醒
    f = fc.forecast(conn, date, save=False)
    if f.get("ok"):
        A(f"> 次日情绪统计倾向：**{f['bias']}**"
          f"（预测溢价 {f['pred_premium']:+.2f}%）。"
          f"但见第八节的检验——这个预测**没有超过基准率**，"
          f"只能当情景参考，不要当信号。")
        A("")
    A("---")
    A("")


def _seat_section(conn, A, date):
    """九、席位动向（龙虎榜）。

    这一节是整份报告里**唯一经过样本外验证仍有正优势**的信号，
    因此它比第八节的情绪预测更值得看。措辞上必须把"已验证"和
    "未验证"分清楚，不能让人误以为它是保证。
    """
    try:
        has = conn.execute(
            "SELECT COUNT(*) FROM billboard_uniq WHERE trade_date=?",
            (date,)).fetchone()[0]
    except Exception:
        return
    if not has:
        return

    A("## 九、席位动向（龙虎榜）")
    A("")

    # ---- 当日龙虎榜概览
    row = conn.execute(
        "SELECT COUNT(*) n, SUM(net_amt) net, "
        "SUM(CASE WHEN net_amt>0 THEN 1 ELSE 0 END) pos "
        # 用去重视图：同股多条上榜原因会重复计数（实测 13.4% 的
        # (trade_date,code) 有多行），直接把"上榜只数"和净额算大。
        "FROM billboard_uniq WHERE trade_date=?", (date,)).fetchone()
    if row and row["n"]:
        A(f"- 上榜 **{row['n']}** 只，净买入为正 "
          f"**{row['pos']}** 只，龙虎榜合计净额 "
          f"**{(row['net'] or 0) / 1e8:+.2f} 亿**")
        A("")

    # ---- 跟随候选（高品质席位 + 排除涨停，最接近可执行）
    try:
        sig = seat.follow_signals(
            conn, date, min_rep_prob=config.SEAT_STRONG_REP_PROB,
            exclude_limit_up=True, top=12)
    except Exception:
        sig = []
    if sig:
        A(f"### 9.1 跟随候选（席位胜率 ≥ "
          f"{config.SEAT_STRONG_REP_PROB:.0f}%，已排除涨停股）")
        A("")
        A("| 代码 | 名称 | 席位胜率 | 席位活跃度 | 净买(万) | 席位 |")
        A("|---|---|---|---|---|---|")
        for r in sig:
            mark = " ⭐" if r["rep_prob"] >= config.SEAT_ELITE_REP_PROB else ""
            a = r.get("act")
            alab = "—" if a is None else ("低频" if a <= 2 else
                                         ("中频" if a < 10 else "**高频**"))
            A(f"| {r['code']} | {r['name'] or '—'} | "
              f"{r['rep_prob']:.0f}%{mark} | {alab}({a}) | "
              f"{(r['net_amt'] or 0) / 1e4:+.0f} | {(r['seat'] or '')[:24]} |")
        A("")
        A(f"> ⭐ = 最强档（胜率 ≥ {config.SEAT_ELITE_REP_PROB:.0f}%）。"
          "已按席位活跃度升序排列——低频优先。")
        A("")
    else:
        A("### 9.1 跟随候选")
        A("")
        A("今日无满足条件（席位胜率≥"
          f"{config.SEAT_STRONG_REP_PROB:.0f}% 且非涨停）的信号。")
        A("")

    # ---- 净买入最大的席位（排除"自然人/机构/沪股通"这类聚合标签：
    #      它们不是营业部，实测是强反向指标，混排会误导）
    labels = ",".join("?" for _ in seat.AGGREGATE_LABELS)
    top = conn.execute(
        f"SELECT seat, SUM(net_amt) net, COUNT(DISTINCT code) n "
        f"FROM seat_trade WHERE trade_date=? AND side='buy' "
        f"AND seat NOT IN ({labels}) "
        f"GROUP BY seat ORDER BY net DESC LIMIT 8",
        (date, *sorted(seat.AGGREGATE_LABELS))).fetchall()
    if top:
        A("### 9.2 净买入最大的席位")
        A("")
        A("| 席位 | 净买(亿) | 涉及股数 |")
        A("|---|---|---|")
        for r in top:
            A(f"| {(r['seat'] or '')[:30]} | {(r['net'] or 0) / 1e8:+.2f} | "
              f"{r['n']} |")
        A("")

    # ---- 已验证的规律（固定文案，说明证据强度）
    A("### 9.3 这个信号有多可信")
    A("")
    A("- 席位胜率（东财 `RISE_PROBABILITY_3DAY`）与真实次日收益的相关性 "
      "**+0.19**（n≈4.7 万，2026-01~09，去重口径）")
    A("- 分档单调：胜率 <35% 的席位次日 **-1.84%**；≥60% 的 **+3.56%**"
      "（胜率 67%）")
    A("- **样本外检验通过**：用前半段排名选出的 TOP 席位，后半段仍有 "
      "+1.85%（对照全席位 -0.26%），秩相关 **+0.40**")
    A("- ⚠️ 龙虎榜盘后披露，只能**次日**买入，实际收益会低于上述统计"
      "（涨停股次日常高开）。本节的跟随候选已排除涨停股。")
    A("- **五层验证全部通过**：分档单调 / 控制涨幅后仍成立 / 样本外秩相关 "
      "+0.40 / 置换检验 p=0.0015 / 逐月 9-9 个月为正")
    A("- **诚实协议通过**：前 70% 选阈值、后 30% 测量，两段一致")
    A("- **席位活跃度是独立的第二个维度**：控制住胜率后，低频席位"
      "（过去 60 日上榜 ≤2 次）的 D5 净 +5.95%，高频（≥10 次）为 -2.41%")
    A("- **阈值分档（已扣 0.40% 成本，可执行的非涨停口径）**："
      "≥50% 时 D1净 +0.33%；≥60% 时 +1.45%；≥70% 时 **+2.30%**")
    A("- ⚠️ 样本期仅 2026 年 1~9 月，未覆盖完整牛熊。")
    A("")


def build(conn, date):
    L = []
    A = L.append

    m = conn.execute("SELECT * FROM daily_market WHERE trade_date=?",
                     (date,)).fetchone()
    e = conn.execute("SELECT * FROM emotion_daily WHERE trade_date=?",
                     (date,)).fetchone()
    if not m:
        return f"# {date} 无数据\n\n请先运行采集。\n"
    m = dict(m)
    e = dict(e) if e else {}

    tlabel, tdelta = metrics.trend(conn, date)

    # ---------------- 标题
    A(f"# A股短线复盘 · {date}")
    A("")
    A(f"> 生成时间 {datetime.now():%Y-%m-%d %H:%M:%S} ｜ "
      f"情绪阶段 **{e.get('stage', '—')}** ｜ "
      f"温度 **{_f(e.get('temperature'), 1)}**"
      f"{f'（{tlabel} {tdelta:+.1f}）' if tlabel else ''}")
    A("")

    # ---------------- 今日焦点（结论先行）
    _focus_section(conn, A, date, m, e)

    # ---------------- 一、市场概览
    A("## 一、市场概览")
    A("")
    A("| 指标 | 数值 |")
    A("|---|---|")
    A(f"| 上证指数 | {_f(m.get('sh_close'))} （{_pct(m.get('sh_pct'))}） |")
    A(f"| 创业板指 | {_f(m.get('cyb_close'))} （{_pct(m.get('cyb_pct'))}） |")
    amt = m.get("amount")
    A(f"| 两市成交额 | {_f(amt / 1e8, 0) + ' 亿' if amt else '—'} |")
    up, dn, flat = m.get("up_count"), m.get("down_count"), m.get("flat_count")
    if up is not None:
        A(f"| 涨跌家数 | 涨 {up} / 跌 {dn} / 平 {flat} |")
    A(f"| 涨停 / 炸板 / 跌停 | {m.get('zt_count')} / {m.get('zb_count')} / "
      f"{m.get('dt_count')} |")
    A(f"| 最高连板 | {m.get('max_height')} 板 |")
    A(f"| 连板家数 | {m.get('lianban_count')} |")
    A("")

    # ---------------- 二、情绪周期
    A("## 二、情绪周期定位")
    A("")
    A(f"**阶段：{e.get('stage', '—')}**")
    A("")
    A(f"- 判定依据：{e.get('stage_reason', '—')}")
    if e.get("seal_rate") is not None:
        A(f"- 封板率：{e['seal_rate'] * 100:.1f}%"
          f"（{m.get('zt_count')} 涨停 / {m.get('zb_count')} 炸板）")
    if e.get("prev_zt_premium") is not None:
        A(f"- **昨日涨停今日表现（赚钱效应）：{e['prev_zt_premium']:+.2f}%**"
          f"（样本 {e.get('prev_zt_n')} 只）")
    if e.get("prev_lb_premium") is not None:
        A(f"- 昨日连板今日表现：{e['prev_lb_premium']:+.2f}%"
          f"（样本 {e.get('prev_lb_n')} 只）")
    if e.get("promote_rate") is not None:
        A(f"- 晋级率（昨涨停→今再涨停）：{e['promote_rate'] * 100:.1f}%")
    if e.get("up_down_ratio") is not None:
        A(f"- 涨跌比：{e['up_down_ratio'] * 100:.1f}%")
    A("")

    # 温度分项
    detail = e.get("stage_reason")
    parts = _parts(conn, date)
    if parts:
        A("| 分项 | 得分 | |")
        A("|---|---|---|")
        names = {
            "zt_count": "涨停家数", "seal_rate": "封板率",
            "premium": "赚钱效应", "promote_rate": "晋级率",
            "max_height": "最高板", "lianban_count": "连板家数",
            "dt_count": "跌停(反向)",
        }
        for k, v in sorted(parts.items(), key=lambda x: -x[1]):
            A(f"| {names.get(k, k)} | {v:.1f} | `{_bar(v)}` |")
        A("")

    # ---------------- 三、连板梯队
    A("## 三、连板梯队")
    A("")
    rows = conn.execute(
        "SELECT height, COUNT(*) n FROM limit_up WHERE trade_date=? "
        "GROUP BY height ORDER BY height DESC", (date,)).fetchall()
    if rows:
        A("| 高度 | 家数 | 个股 |")
        A("|---|---|---|")
        for r in rows:
            h = r["height"]
            names = conn.execute(
                "SELECT name, code FROM limit_up WHERE trade_date=? AND height=? "
                "ORDER BY COALESCE(first_seal,'99:99:99')", (date, h)).fetchall()
            nm = "、".join(f"{x['name']}" for x in names[:10])
            if len(names) > 10:
                nm += f" 等{len(names)}只"
            A(f"| {h} 板 | {r['n']} | {nm} |")
        A("")

    # ---------------- 四、题材
    A("## 四、题材与新题材")
    A("")
    newt = th.detect_new(conn, date)
    if newt:
        A("### 4.1 新题材 / 异动启动")
        A("")
        A("| 题材 | 类型 | 涨停数 | 最高板 | 龙头 | 说明 |")
        A("|---|---|---|---|---|---|")
        for t in newt[:12]:
            A(f"| {t['tag']} | {t['kind']} | {t['zt_count']} | "
              f"{t['max_height']} | {t['leader']} | {t['note']} |")
        A("")
    else:
        A("_今日无新题材/异动启动。_")
        A("")

    ranks = th.lifecycle(conn, date, min_zt=2, top=12)
    if ranks:
        A("### 4.2 题材生命周期与延续性排行")
        A("")
        A("| 排名 | 题材 | 阶段 | 近5日家数 | 延续性 | 涨停 | 最高板 | 龙头 | 梯队 |")
        A("|---|---|---|---|---|---|---|---|---|")
        for i, s in enumerate(ranks, 1):
            ladder = " ".join(f"{k}板×{v}" for k, v in
                              sorted(s["ladder"].items(), key=lambda x: -int(x[0])))
            A(f"| {i} | {s['tag']} | {s['phase']} | "
              f"`{s['spark']}` {'→'.join(str(v) for v in s['series'])} | "
              f"**{s['score']:.1f}** | {s['zt_count']} | "
              f"{s['max_height']} | "
              f"{s['leader']}({s['leader_height']}板) | {ladder} |")
        A("")
        A("> `近5日家数` 是各交易日该题材的涨停家数，从左到右由远及近。"
          "3→5→8→12 是发酵，8→5→3 是衰减。")
        A("")
        # 延续性分项（取第一名）
        top = ranks[0]
        A(f"**{top['tag']} 的延续性分项**：" + "、".join(
            f"{k} {v:.0f}" for k, v in top["parts"].items()))
        A("")

        # 催化原因
        cat = th.theme_catalyst(conn, date, ranks[0]["tag"], limit=2)
        if cat:
            A("### 4.3 龙头催化原因（原文摘录）")
            A("")
            for c in cat:
                info = (c["reason_info"] or "").split("（免责声明")[0].strip()
                info = info.replace("\n", " ")[:220]
                A(f"- **{c['name']}（{c['continue_num']}板）** 题材："
                  f"{c['reason']}")
                if info:
                    A(f"  - {info}")
            A("")

    # ---------------- 五、龙头
    leaders = th.market_leaders(conn, date, top=10)
    if leaders:
        rmap = th.stock_reason_map(conn, date)
        A("## 五、全市场龙头")
        A("")
        A("| 代码 | 名称 | 高度 | 换手率 | 首封 | 开板 | 封单额 | 题材 |")
        A("|---|---|---|---|---|---|---|---|")
        for s in leaders:
            seal = (f"{s['seal_amount'] / 1e8:.2f}亿"
                    if s.get("seal_amount") else "—")
            opn = s.get("open_num")
            A(f"| {s['code']} | {s['name']} | {s['height']}板 | "
              f"{_pct(s.get('turnover'))} | {s.get('first_seal') or '—'} | "
              f"{'—' if opn is None else opn} | {seal} | "
              f"{rmap.get(s['code']) or s.get('industry') or '—'} |")
        A("")

    # ---------------- 六、外围市场
    g = conn.execute(
        "SELECT * FROM global_market WHERE trade_date=? ORDER BY code",
        (date,)).fetchall()
    if g:
        A("## 六、外围市场")
        A("")
        A("| 品种 | 最新 | 涨跌 |")
        A("|---|---|---|")
        for r in g:
            A(f"| {r['name']} | {_f(r['price'])} | {_pct(r['pct'])} |")
        A("")

    # ---------------- 七、新闻
    A("## 七、消息面")
    A("")
    _news_section(conn, A)

    # ---------------- 八、预测
    A("## 八、明日情绪预判")
    A("")
    f = fc.forecast(conn, date, save=False)
    if f.get("ok"):
        A(f"- **倾向：{f['bias']}**（置信度 {f['confidence'] * 100:.0f}%）")
        A(f"- 预测次日昨日涨停溢价：**{f['pred_premium']:+.2f}%**")
        A(f"- 预测次日涨停家数：{f['pred_zt_count']:.0f} "
          f"（较今日 {f['zt_delta']:+.0f}）")
        A(f"- 相似样本中次日赚钱比例：{f['win_rate'] * 100:.0f}%"
          f"（{len(f['neighbors'])} 个邻居）")
        A("")
        A("| 相似日 | 距离 | 次日溢价 | 次日涨停数 | 次日阶段 |")
        A("|---|---|---|---|---|")
        for nb in f["neighbors"][:8]:
            A(f"| {nb['date']} | {nb['dist']:.2f} | "
              f"{nb['next_premium']:+.2f}% | {nb['next_zt']} | "
              f"{nb.get('next_stage') or '—'} |")
        A("")
        A(f"> 方法：在历史上找与今日情绪特征最接近的 "
          f"{len(f['neighbors'])} 天，统计这些日子**次日**的实际表现。")
        A("> 这是统计参考，不是确定性预测。它描述的是"
          "「情绪处于类似位置时，历史上通常怎么走」。")
    else:
        A(f"_暂不可预测：{f.get('reason')}_")
    A("")

    acc = fc.accuracy(conn)
    if acc.get("n"):
        A("### 8.1 这个预测准不准（回验）")
        A("")
        A(f"- 历史回验样本：**{acc['n']} 次**")
        A(f"- 方向命中率：{acc['hit_rate'] * 100:.1f}%")
        if acc.get("base_rate") is not None:
            A(f"- **基准率**（历史上溢价为正的天数占比）："
              f"{acc['base_rate'] * 100:.1f}%")
            edge = acc["edge"] * 100
            flag = "✅ 有优势" if edge > 3 else ("⚠️ 优势微弱" if edge > 0
                                                else "❌ 无优势")
            A(f"- **相对基准率的优势：{edge:+.1f} 个百分点** {flag}")
        if acc.get("corr") is not None:
            A(f"- 预测值与实际值相关系数：{acc['corr']:+.3f}"
              f"（越接近 0 越说明没预测力）")
        A(f"- 平均绝对误差：{acc['mae']:.2f} 个百分点")
        A("")
        if acc.get("verdict"):
            A(f"> **结论**：{acc['verdict']}")
            A("")

    diag = fc.diagnostics(conn)
    if diag.get("ok"):
        A("### 8.2 特征有效性检验")
        A("")
        A(f"- 样本：{diag['n']} 个交易日；次日溢价均值 "
          f"{diag['mean']:+.2f}%，标准差 {diag['std']:.2f}，"
          f"为正的比例 {diag['base_rate'] * 100:.1f}%")
        A(f"- 溢价自相关：滞后1日 {diag['autocorr_1']:+.3f}，"
          f"滞后2日 {diag['autocorr_2']:+.3f}"
          f"（绝对值 < 0.2 基本等于没有延续性）")
        A("")
        A("| 今日特征 | 与次日溢价的相关系数 |")
        A("|---|---|")
        for k, v in sorted(diag["corr_next"].items(),
                          key=lambda x: -abs(x[1])):
            mark = " ⚠️" if abs(v) >= 0.25 else ""
            A(f"| {k} | {v:+.3f}{mark} |")
        A("")
        if diag.get("bands"):
            A("| 今日阶段 | 样本 | 次日溢价均值 | 次日为正比例 |")
            A("|---|---|---|---|")
            for k, v in diag["bands"].items():
                A(f"| {k} | {v['n']} | {v['mean']:+.2f}% | "
                  f"{v['win'] * 100:.1f}% |")
            A("")
        A(f"> **{diag['conclusion']}**")
        A("")

    # ---------------- 席位居中（龙虎榜）
    _seat_section(conn, A, date)

    # ---------------- 免责
    A("---")
    A("")
    A("**免责声明**：本报告由本地程序依据公开行情数据自动生成，"
      "仅用于交易复盘与个人研究，不构成任何投资建议。"
      "数据来源为第三方公开接口，可能存在延迟、缺失或错误，"
      "请以交易所及券商行情为准。短线交易风险极高，请自行承担决策后果。")
    A("")
    return "\n".join(L)


def _news_section(conn, A):
    """新闻按相关性排序：宏观/外围、产业催化优先，其余作快讯。"""
    from .sources.news import CATALYST_KEYWORDS, GLOBAL_KEYWORDS
    rows = conn.execute(
        "SELECT * FROM news ORDER BY ts DESC LIMIT 120").fetchall()
    if not rows:
        return
    macro, other = [], []
    for n in rows:
        text = (n["title"] or "") + (n["content"] or "")
        g = [k for k in GLOBAL_KEYWORDS if k in text]
        c = [k for k in CATALYST_KEYWORDS if k in text]
        rec = {"row": n, "g": g, "c": c, "score": len(g) * 2 + len(c)}
        (macro if g else other).append(rec)
    macro.sort(key=lambda x: (-x["score"], x["row"]["ts"] or ""))
    other.sort(key=lambda x: (-x["score"], x["row"]["ts"] or ""))

    if macro:
        A("### 7.1 外围与宏观要闻")
        A("")
        for r in macro[:10]:
            n = r["row"]
            ts = (n["ts"] or "")[5:16].replace("T", " ")
            A(f"- `{ts}` **{n['title']}** "
              f"`{'/'.join(r['g'][:3])}`")
        A("")

    if other:
        A("### 7.2 市场快讯")
        A("")
        for r in other[:10]:
            n = r["row"]
            ts = (n["ts"] or "")[5:16].replace("T", " ")
            tag = f" `{'/'.join(r['c'][:3])}`" if r["c"] else ""
            A(f"- `{ts}` {n['title']}{tag}")
        A("")


def _parts(conn, date):
    """重算分项得分（用于展示）。"""
    try:
        r = metrics.compute_day(conn, date, save=False)
        return r.get("_parts") if r else None
    except Exception:                                  # noqa: BLE001
        return None


def write(conn, date, path=None):
    txt = build(conn, date)
    path = path or (config.REPORT_DIR / f"{date}.md")
    path.write_text(txt, encoding="utf-8")
    return path, txt
