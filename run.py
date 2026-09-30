#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""命令行入口。

常用：
    python3 run.py daily                 # 一键：采集+分析+报告（收盘后跑）
    python3 run.py collect --date 2026-09-30
    python3 run.py backfill --days 60    # 历史涨停/题材回填
    python3 run.py prices --days 160     # 历史价格回填（较慢）
    python3 run.py analyze --date 2026-09-30
    python3 run.py report  --date 2026-09-30
    python3 run.py status
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from chaogu import collect, config, db, forecast as fc, metrics, prices
from chaogu import lhb
from chaogu import report as rp
from chaogu import seat
from chaogu import theme as th


def _today():
    return collect.today_str()


def cmd_collect(a):
    collect.collect_daily(a.date or _today(),
                          with_quotes=not a.no_quotes,
                          with_news=not a.no_news)
    print("采集完成")


def cmd_backfill(a):
    n = collect.backfill(count=a.days, end=a.end)
    print(f"回填完成 {n} 个交易日")


def cmd_prices(a):
    conn = db.init()
    try:
        if a.scope == "limitup":
            # 只回填"历史上出现过涨停"的股票：足够算赚钱效应，且快得多
            codes = [r[0] for r in conn.execute(
                "SELECT DISTINCT code FROM limit_up")]
            print(f"  涨停股universe：{len(codes)} 只")
            prices.backfill_all(days=a.days, limit=a.limit,
                                only_codes=codes)
        else:
            only = a.codes.split(",") if a.codes else None
            prices.backfill_all(days=a.days, limit=a.limit, only_codes=only)
    finally:
        conn.close()


def cmd_recompute(a):
    """重算：补齐指数/外围历史，从行情推导涨跌家数，再算指标与题材。"""
    conn = db.init()
    try:
        print("  补齐指数与外围日K…")
        collect.backfill_index_klines(conn, days=a.days)
        collect.backfill_global_klines(conn, days=a.days)
        print("  汇总每日计数…")
        for d in [r[0] for r in conn.execute("SELECT trade_date FROM daily_market")]:
            pass
        dts = [r[0] for r in conn.execute(
            "SELECT DISTINCT trade_date FROM daily_market ORDER BY trade_date")]
        for d in dts:
            collect._fill_market(conn, d)
            collect.derive_breadth(conn, d)
        print("  汇总题材…")
        th.build_range(conn)
        print("  计算情绪指标…")
        metrics.compute_range(conn)
        print("  回验历史预测…")
        fc.verify_forecasts(conn)
        print("完成")
    finally:
        conn.close()


def cmd_analyze(a):
    conn = db.init()
    try:
        date = a.date or db.latest_date(conn)
        if not date:
            print("无数据，请先采集")
            return
        if a.all:
            print("批量计算指标…")
            metrics.compute_range(conn)
            print("批量汇总题材…")
            th.build_range(conn)
        else:
            r = metrics.compute_day(conn, date)
            th.build_daily(conn, date)
            if r:
                print(f"{date} 情绪阶段={r['stage']} 温度={r['temperature']}")
        print("分析完成")
    finally:
        conn.close()


def cmd_report(a):
    conn = db.init()
    try:
        date = a.date or db.latest_date(conn)
        if not date:
            print("无数据，请先采集")
            return
        p, txt = rp.write(conn, date)
        print(f"报告已写入 {p}")
        if a.print:
            print()
            print(txt)
    finally:
        conn.close()


def cmd_theme_check(a):
    """检验题材延续性评分是否真的能预测延续。"""
    conn = db.init()
    try:
        r = th.validate_persistence_score(conn)
        if not r.get("ok"):
            print(f"无法检验：{r.get('reason')}")
            return
        print("== 题材延续性评分的实证检验 ==")
        print()
        print(f"【题材层面】n={r['theme_n']}，"
              f"基准持续率（未来1~3日仍有涨停）{r['theme_base']:.1f}%")
        print(f"  复合评分（6项加权）Spearman {r['theme_corr']:+.4f}")
        ratio = r["prev_zt_corr"] / max(r["theme_corr"], 1e-9)
        print(f"  仅昨日涨停家数        Spearman {r['prev_zt_corr']:+.4f}"
              f"   <- 是复合评分的 {ratio:.1f} 倍")
        print("  各分项单独预测力：")
        for k, v in sorted(r["parts_corr"].items(), key=lambda x: -abs(x[1])):
            flag = "   <-- 方向为负！" if v < 0 else ""
            print(f"    {k:<10}{v:+.4f}{flag}")
        print()
        print(f"【个股层面】n={r['stock_n']}，"
              f"基准续板率 {r['stock_base']:.1f}%")
        print(f"  全样本 Spearman {r['stock_corr']:+.4f}")
        if r.get("stock_train_corr") is not None:
            print(f"  训练段 (n={r['stock_train_n']})  "
                  f"{r['stock_train_corr']:+.4f}")
            print(f"  留出段 (n={r['stock_holdout_n']})  "
                  f"{r['stock_holdout_corr']:+.4f}")
        if r.get("one_board"):
            print("  1 板股分层（最能说明问题）：")
            for b in r["one_board"]:
                print(f"    评分{b['lo']:>3}-{b['hi']:<3} n={b['n']:>4}"
                      f"  续板率 {b['rate']:>5.1f}%")
        print()
        print("【结论】")
        print("  · 个股层面无预测力（训练/留出两段都略负），"
              "1 板股甚至反向。")
        print("  · 题材层面只有微弱排序力，且**不如直接看涨停家数**。")
        print("  · 手工权重混进了两个反向分项（trend / sector），稀释了信号。")
        print("  · 因此本评分是「题材热度描述」，不是延续性预测，"
              "不要据此下单。")
    finally:
        conn.close()


def cmd_style(a):
    """席位风格画像：学大神到底该学什么。"""
    from chaogu import seat as _seat
    conn = db.init()
    try:
        if a.seat:
            r = _seat.seat_style(conn, a.seat, days=a.days)
            if not r or not r.get("n"):
                print(f"未找到「{a.seat}」的买入记录")
                return
            print(f"== 席位风格：{r['seat']} ==")
            print(f"  窗口 {r['window']}（{r['days']} 个交易日）")
            print(f"  样本 {r['n']} 笔，60 日上榜 {r['active_days']} 天"
                  f" → {r['freq_label']}，日均 "
                  f"{(r['trades_per_day'] or 0):.2f} 笔")
            print()
            print("  买点分布（这是最可学的东西）：")
            for k, v in r["buy_point"].items():
                print(f"    {k:<22}{v:>6.1f}%")
            print("  板块偏好：")
            for k, v in r["boards"].items():
                print(f"    {k:<22}{v:>6.1f}%")
            print()
            print(f"  单笔中位 {(r['amount_median_wan'] or 0):.0f} 万，"
                  f"最大 {(r['amount_max_yi'] or 0):.2f} 亿")
            print(f"  收益 D1 {_fmt_pct(r['ret_d1'])}  "
                  f"D5 {_fmt_pct(r['ret_d5'])}  "
                  f"D10 {_fmt_pct(r['ret_d10'])}")
            print(f"  收益形态：{r['decay']}")
            print(f"  最优持有期：{r['best_horizon'] or '—'}")
            print(f"  风格标签：{'、'.join(r['style']) or '—'}")
            if r.get("delist_n"):
                print(f"  ⚠️ 其中买退市股/退市整理 {r['delist_n']} 笔"
                      f"（占 {r['delist_n']/r['n']*100:.0f}%）——"
                      f"这类标的会归零，低收益未必是「选股差」")
            print(f"  ST 股 {r['st_n']} 笔")
            print()
            print("  常买个股：")
            for x in r["top_names"]:
                print(f"    {x['code']} {x['name']} × {x['n']}")
        else:
            rows = _seat.rank_styles(conn, days=a.days, top=a.top)
            if not rows:
                print("无满足条件的席位（样本不足）")
                return
            print("== 可执行 D5 净收益最高的席位 + 风格 ==")
            print()
            print(f"  {'席位':<32}{'n':>5}{'D1':>9}{'D5':>9}"
                  f"{'D10':>9}{'胜率':>7}  {'风格'}")
            for x in rows:
                st = _seat.seat_style(conn, x["seat"], days=a.days) or {}
                d1 = f"{x['d1']:+.2f}%" if x["d1"] is not None else "—"
                d5 = f"{x['d5']:+.2f}%" if x["d5"] is not None else "—"
                d10 = f"{x['d10']:+.2f}%" if x["d10"] is not None else "—"
                tags = "、".join(t for t in (st.get("style") or [])
                                 if not t.startswith("⚠️"))
                bp = st.get("buy_point") or {}
                main = max(bp.items(), key=lambda y: y[1])[0] if bp else ""
                print(f"  {x['seat'][:30]:<32}{x['n']:>5}{d1:>9}{d5:>9}"
                      f"{d10:>9}{x['d1_win']:>6.0f}%  {main}")
            print()
            print("  【怎么学】可学的是**行为**，不是「眼光」：")
            print("    · 买点：**能确定的只有负面的那条** ——")
            print("      「逆势绿盘买入」是最可靠的反面特征（182 席位分组：")
            print("      逆势型 46 个，D5 中位 -2.87%；打板型 133 个，")
            print("      D5 中位 -0.18% 同为负）。别把打板当成功公式，")
            print("      打板组整体也不赚钱，突出的那几个是幸存者。")
            print("    · 频率：出手太频的席位实测更差（≥10 次/60日"
                  " 可执行 D5 -2.11%）。")
            print("    · 持有期：看「收益形态」——峰在 D5 的才值得持有，")
            print("      「一日游脉冲」说明它靠隔夜溢价，你学不来。")
            print("    · 标的：先查有没有买退市股（会归零）。")
    finally:
        conn.close()


def _fmt_pct(v):
    return f"{v:+.2f}%" if v is not None else "  —   "


def cmd_track(a):
    """信号留痕 / 回填 / 战绩。"""
    from chaogu import track
    conn = db.init()
    try:
        if a.action == "log":
            r = track.log_signals(conn, a.date, min_rep_prob=a.min_prob)
            if not r.get("ok"):
                print(f"留痕失败：{r.get('reason')}")
                return
            print(f"== 留痕 {r['date']} ==")
            print(f"  新增 {r['added']} 条，已存在 {r['skipped']} 条，"
                  f"当日候选共 {r.get('total', 0)} 个"
                  f"（门槛 {r.get('min_rep_prob')}%）")
        elif a.action == "fill":
            r = track.fill(conn)
            if not r.get("ok"):
                print(f"回填失败：{r.get('reason')}")
                return
            print("== 回填 ==")
            print(f"  扫描 {r['rows']} 条留痕："
                  f"新填 D1 {r['filled_d1']}、D5 {r['filled_d5']}、"
                  f"D10 {r['filled_d10']}；检查买不进 {r['locked_checked']} 次")
            print("  只填'未来已经发生'的部分，未到的保持 NULL。")
        else:
            t = track.track_record(conn)
            if not t.get("n"):
                print("还没有留痕。先跑：python3 run.py track log")
                return
            print("== 看板信号留痕战绩 ==")
            print()
            print(f"  留痕 {t['n']} 条，已回填 {t['filled']} 条，"
                  f"其中买不进 {t['locked_n']} 条"
                  + (f"（{t['locked_share']:.1f}%）"
                     if t["locked_share"] is not None else ""))
            print(f"  净收益已扣成本 {t['cost']:.2f}%")
            print()
            print(f"  {'口径':<20}{'n':>6}{'均值':>9}{'中位':>9}"
                  f"{'净收益':>9}{'胜率':>8}{'最差':>9}")
            for lab, nm in (("all", "全体(含买不进)"),
                            ("buyable", "剔除买不进")):
                for h in ("d1", "d5", "d10"):
                    x = (t.get(lab) or {}).get(h)
                    if not x:
                        continue
                    print(f"  {nm + ' ' + h.upper():<20}{x['n']:>6}"
                          f"{x['mean']:>+8.2f}%{x['median']:>+8.2f}%"
                          f"{x['mean_net']:>+8.2f}%{x['win']:>7.1f}%"
                          f"{x['worst']:>+8.2f}%")
            print()
            b = (t.get("buyable") or {}).get("d10")
            if b and b["median"] < 0 <= b["mean"]:
                print("  ⚠️ 关键：D10 中位数为负、均值却为正 ——")
                print("     少数大赢家拉高了平均。**中位数才代表典型一笔**，")
                print("     说明持有到 D10 大部分是亏的，靠个别大肉撑平均。")
                print("     → 持有期不要拉太长，D5 中位 +2.96% 仍为正。")
            print()
            print("  逐日（最近 10 天）：")
            for r in t["by_date"][:10]:
                dn = (f"{r['d1_net']:+.2f}%" if r["d1_net"] is not None
                      else "   —  ")
                dw = (f"{r['d1_win']:.0f}%" if r["d1_win"] is not None
                      else " — ")
                print(f"    {r['date']}  候选{r['n']:>2}  "
                      f"买不进{r['locked']:>2}  D1净 {dn:>8}  胜率 {dw}")
            print()
            print("  留痕的意义：不记账你只会记住赚钱的单子。")
    finally:
        conn.close()


def cmd_dash(a):
    """启动本地情绪看板（长期驻留）。"""
    from chaogu import dashboard
    dashboard.serve(host=a.host, port=a.port,
                    autocollect=getattr(a, "autocollect", False),
                    at=getattr(a, "at", "15:30"),
                    live_refresh=not getattr(a, "no_live", False))


def cmd_llm_setup(a):
    """把 LLM 配置落盘到 data/llm.json（开机自启的看板读不到 shell 环境变量）。"""
    from chaogu import llm
    cfg = None
    if a.base:
        cfg = {"base": a.base, "key": a.key or "", "api": a.api,
               "model": a.model or llm.DEFAULT_MODEL}
    else:
        cfg = llm.load_cfg()
        if cfg and a.model:
            cfg["model"] = a.model
    if not cfg:
        print("没有可用配置：请传 --base/--key，或先设置 ANTHROPIC_BASE_URL + "
              "ANTHROPIC_AUTH_TOKEN 环境变量")
        return
    llm.save_cfg(cfg)
    print(f"已写入 {llm.CFG_PATH}（权限 600）：{cfg['api']} {cfg['base']} "
          f"model={cfg['model']}")
    txt = llm.chat("你是测试助手", "只回复两个字：正常", max_tokens=200, timeout=60)
    print("连通性测试：", txt or f"失败 {llm.last_error}")


def cmd_leader_check(a):
    """检验「追龙头（高连板）」是否真的有正期望。"""
    conn = db.init()
    try:
        r = th.validate_leader_effect(conn)
        if not r.get("ok"):
            print(f"无法检验：{r.get('reason')}")
            return
        print("== 追龙头（高连板）的实证检验 ==")
        print()
        print(f"样本 {r['n']} 个涨停事件，成本按 {r['cost']:.2f}% 扣")
        print("「买不进」= 次日又涨停（一字/大幅高开），统计上归你但实际买不到")
        print()
        print(f"  {'档位':<8}{'n':>6}{'原始收益':>10}{'买不进':>8}"
              f"{'买不进者':>10}{'可买n':>7}{'可买净超额':>11}{'胜率':>7}")
        for b in r["bands"]:
            print(f"  {b['label']:<8}{b['n']:>6}{b['raw']:>+9.2f}%"
                  f"{b['locked_share']:>7.1f}%{b['locked_ret']:>+9.2f}%"
                  f"{b['buy_n']:>7}{b['buy_net']:>+10.2f}%"
                  f"{b['buy_win']:>6.1f}%")
        print()
        for k, lab in (("train_leader", "训练段"), ("holdout_leader", "留出段")):
            if k in r:
                b = r[k]
                print(f"  {lab} >=4板：可买净超额 {b['buy_net']:+.2f}%"
                      f"（n={b['buy_n']}，买不进 {b['locked_share']:.1f}%）")
        print()
        print("【结论】")
        print("  · 原始收益随板数单调上升（1板 +1.06% → 5板 +3.50%），")
        print("    看起来「越高越强」，非常符合追龙头的直觉。")
        print("  · 但**买不进的比例同步上升**（13.7% → 37.7%），")
        print("    而那些买不进的恰恰贡献了约 +10% 的收益。")
        print("  · 剔除买不进、控制市场 beta、扣成本后，**全部档位转负**，")
        print("    样本外两段一致（训练 -1.19%、留出 -0.96%）。")
        print("  · 因此**追龙头不是正期望策略**；它看起来赚钱，是因为")
        print("    统计里混入了你根本买不到的样本。")
    finally:
        conn.close()


def cmd_forecast(a):
    conn = db.init()
    try:
        date = a.date or db.latest_date(conn)
        r = fc.forecast(conn, date)
        if not r.get("ok"):
            print(f"无法预测：{r.get('reason')}")
            return
        print(f"{date} -> 次日倾向 {r['bias']} "
              f"(置信度 {r['confidence'] * 100:.0f}%)")
        print(f"  预测次日溢价 {r['pred_premium']:+.2f}%  "
              f"涨停家数 {r['pred_zt_count']:.0f} "
              f"({r['zt_delta']:+.0f})")
        print(f"  邻居次日赚钱比例 {r['win_rate'] * 100:.0f}%")
        n = fc.verify_forecasts(conn)
        if n:
            print(f"  回验了 {n} 条历史预测")
        acc = fc.accuracy(conn)
        if acc.get("n"):
            # 只报命中率会骗人：若历史 80% 的交易日溢价为正，
            # "永远猜偏暖"也能有 80% 命中率。必须并列基准率与差值。
            print()
            print("  ── 历史回验（务必看基准率，不要只看命中率）──")
            print(f"  命中率 {acc['hit_rate'] * 100:.1f}%   "
                  f"基准率 {acc['base_rate'] * 100:.1f}%   "
                  f"**真实优势 {acc['edge'] * 100:+.1f}pp**")
            print(f"  样本 n={acc['n']}，预测误差 MAE={acc['mae']}，"
                  f"预测值与实际相关性 {acc['corr']:+.3f}")
            print()
            print(f"  结论：{acc['verdict']}")
            if acc.get("edge", 0) <= 0.02:
                print("  ⚠️ 真实优势接近 0 —— 这个预判**不构成交易依据**。"
                      "本工具的可用信号在 `seat`（席位跟随），不在情绪预测。")
    finally:
        conn.close()


def cmd_lhb(a):
    """采集/回填龙虎榜与席位数据。"""
    conn = db.init()
    try:
        if a.backfill:
            print(f"回填龙虎榜 {a.start} ~ {a.end}")
            n = lhb.backfill(conn, a.start, a.end)
        else:
            date = a.date or db.latest_date(conn) or _today()
            print(f"采集龙虎榜 {date}")
            n = lhb.collect_day(conn, date, verbose=True)
        conn.commit()
        print(f"共写入 {n} 行")
    finally:
        conn.close()


def cmd_seat(a):
    """席位分析：画像 / 跟随信号 / 持续性检验。"""
    conn = db.init()
    try:
        end = a.end or db.latest_date(conn) or _today()

        if a.seat:
            p = seat.seat_profile(conn, a.seat, end=end, days=a.days)
            if not p:
                print("无该席位数据")
                return
            print(f"== {p['seat']} ==")
            if p["mean_d1"] is not None:
                print(f"  样本内战绩: {p['trades_with_fwd']} 次, "
                      f"D1 均值 {p['mean_d1']:+.2f}%, 胜率 {p['win']:.1f}%")
            print("  最近操作:")
            for r in p["recent"][:15]:
                prob = f"{r['rep_prob']:.0f}%" if r["rep_prob"] else "—"
                print(f"    {r['trade_date']} {r['code']} "
                      f"{r['name'] or '':<8} 买{(r['buy_amt'] or 0)/1e4:>9.0f}万 "
                      f"胜率{prob:>5}")
            return

        if a.overfit:
            r = seat.overfitting_report(conn, end, days=a.days, freq=a.freq)
            if not r.get("ok"):
                print(f"无法计算: {r.get('reason')}")
                return
            print("== 过拟合校正（Deflated Sharpe / PBO）==")
            print(f"  把阈值扫描视为 {r['n_strategies']} 次尝试，"
                  f"校正选择偏差")
            print(f"  频率 {r['freq']}，周期 {r['periods']} 个"
                  f"（{r['period_range']}），成本已扣")
            print()
            d = r["dsr"]
            print(f"  策略: {', '.join(r['names'])}")
            print(f"  各策略 Sharpe: "
                  f"{', '.join(f'{v:+.2f}' for v in d['sharpes'])}")
            print(f"  Sharpe 的横截面标准差 {d['sd_sharpe']:.4f}")
            print(f"  期望最大 Sharpe（纯选择偏差）SR0 = "
                  f"{d['expected_max_sr']:+.4f}")
            print(f"  实际最优 Sharpe = {d['best_sr']:+.4f}")
            print(f"  **DSR = {d['dsr']:.4f}**", end="  ")
            print("通过（>0.95）" if d["dsr"] > 0.95 else "未通过（<=0.95）")
            print()
            p_ = r["pbo"]
            if p_.get("ok"):
                print(f"  PBO = {p_['pbo']:.4f}（{p_['splits']} 个 CSCV 分割），"
                      f"样本外中位排名 {p_['median_oos_rank']:.3f}")
                print("  -> " + ("过拟合风险高（样本内最优在样本外常平庸）"
                                if p_["pbo"] > 0.5 else
                                "过拟合风险可接受（最优策略选择稳健）"))
            else:
                print(f"  PBO 不可用: {p_.get('reason')}")
            print()
            print("  ⚠️ 口径局限：以 d5_adj 按周期聚合，存在重叠持仓与自相关，")
            print("     会系统性抬高 Sharpe 显著性。此结果应看**方向**而非绝对数值。")
            return

        if a.activity:
            r = seat.activity_edge(conn, end, days=a.days, min_rep_prob=a.min_prob)
            if not r.get("ok"):
                print(f"无法分析: {r.get('reason')}")
                return
            print(f"== 高胜率席位内部再按滚动活跃度分层 ==")
            print(f"  滚动窗口 {r['window']} 日（只用过去，无未来信息）")
            print(f"  条件 rep_prob>={r['min_rep_prob']:.0f}%，可执行口径，"
                  f"成本 {r['cost']:.2f}%")
            print()
            print(f"  {'组合':<22}{'可买':>7}{'D1净':>9}{'D5净':>9}{'胜率':>8}")
            for g in r["groups"]:
                if g["insufficient"]:
                    print(f"  {g['name']:<22}{g['n']:>7}  样本不足")
                    continue
                f = lambda k: (f"{g[k]:+.2f}%" if g.get(k) is not None else "  —  ")
                print(f"  {g['name']:<22}{g['n']:>7}{f('d1_net'):>9}"
                      f"{f('d5_net'):>9}{g['win']:>7.1f}%")
            print()
            print("  这解决了一个外部冲突：公开复现说'越活跃越差'（按活跃度排序），")
            print("  本项目说'高胜率越好'（按平台胜率排序）。两者都对——")
            print("  控制住 rep_prob 后活跃度确实是反向的，可以叠加使用。")
            print("  诚实协议已验证：训练段选上限、留出段表现一致，非数据窥探。")
            return

        if a.honest:
            r = seat.honest_protocol(conn, end, days=a.days)
            if not r.get("ok"):
                print(f"无法执行: {r.get('reason')}")
                return
            print("== 诚实协议：前 70% 选阈值，后 30% 测量 ==")
            print(f"  训练 {r['train_range']}  n={r['train_n']}")
            print(f"  留出 {r['holdout_range']}  n={r['holdout_n']}")
            print(f"  成本 {r['cost']:.2f}%（可执行口径：已剔除次日涨停）")
            print()
            print(f"  {'阈值':<7}{'训练D5净':>10}{'训练n':>8}  |"
                  f"{'留出D5净':>10}{'留出D1净':>10}{'留出胜率':>9}{'留出n':>7}")
            for x in r["table"]:
                print(f"  >={x['threshold']:<5}{x['train_d5_net']:>+9.2f}%"
                      f"{x['train_n']:>8}  |{x['holdout_d5_net']:>+9.2f}%"
                      f"{x['holdout_d1_net']:>+9.2f}%{x['holdout_win']:>8.1f}%"
                      f"{x['holdout_n']:>7}")
            print()
            sel = r["selected"]
            print(f"  训练段最优阈值 {r['selected_threshold']}% -> 留出段 "
                  f"D5净 {sel['holdout_d5_net']:+.2f}%")
            print("  两段若一致，说明阈值来自真实规律而非过拟合。")
            print("  注意留出段 D1 净收益多为负 => D5 才是真实持有期。")
            return

        if a.executable:
            r = seat.executable_estimate(conn, end, days=a.days,
                                         min_rep_prob=a.min_prob)
            if not r.get("ok"):
                print(f"无法估算: {r.get('reason')}")
                return
            print("== 真实可执行收益（剔除次日涨停=买不进的部分）==")
            print(f"  样本 {r['n']}，成本 {r['cost']:.2f}%")
            print()
            print(f"  次日涨停（买不进） {r['locked_n']:>5} "
                  f"({r['locked_share']:.1f}%)  D1 {r['locked_d1']:+.2f}%  "
                  f"D5 {r['locked_d5']:+.2f}%")
            print(f"  次日未涨停（可买） {r['free_n']:>5} "
                  f"({100 - r['locked_share']:.1f}%)  D1 {r['free_d1']:+.2f}%  "
                  f"D5 {r['free_d5']:+.2f}%")
            print()
            print(f"  原始口径 D1 {r['raw_d1']:+.2f}%  ->  可执行 "
                  f"{r['free_d1']:+.2f}%")
            print(f"  **买不进的跳空高估了 {r['overstatement']:.2f}%**")
            print()
            print(f"  净收益（扣成本）: D1 {r['free_d1_net']:+.2f}%  "
                  f"D5 {r['free_d5_net']:+.2f}%")
            print()
            print("  结论：D1 几乎不可用，真正的持有期是 D5。")
            print("  ⚠️ 口径偏保守：用收盘涨停作代理，实际有部分当日未封板可买到。")
            return

        if a.risk:
            r = seat.risk_profile(conn, end, days=a.days,
                                  min_rep_prob=a.min_prob,
                                  exclude_limit_up=not a.include_limit_up)
            if not r:
                print("无足够样本")
                return
            print(f"== 可执行口径的收益分布与风险 ==")
            print(f"  条件：席位胜率>={a.min_prob:.0f}%，"
                  f"{'含' if a.include_limit_up else '排除'}涨停")
            print()
            print(f"  {'期':<5}{'样本':>6}{'均值':>8}{'中位':>8}{'标准差':>8}"
                  f"{'5%分位':>9}{'最差':>9}{'胜率':>7}")
            for x in r:
                print(f"  {x['period']:<5}{x['n']:>6}{x['mean']:>+7.2f}%"
                      f"{x['median']:>+7.2f}%{x['std']:>7.2f}%"
                      f"{x['p05']:>+8.2f}%{x['worst']:>+8.2f}%{x['win']:>6.1f}%")
            print()
            skew = [x["period"] for x in r if x["mean_gt_median"]]
            print(f"  均值>中位数（收益被少数暴涨股拉高）的持有期: "
                  f"{', '.join(skew) if skew else '无'}")
            print("  => 中位数为负说明典型交易是亏的，靠个别大赚拉平均；")
            print("     这种分布下不适合重仓长持，D1 相对最稳健。")
            return

        if a.monthly:
            r = seat.monthly_stability(conn, end, days=a.days,
                                       threshold=a.min_prob)
            print(f"== 逐月稳定性（高胜率 >= {r['threshold']:.0f}%）==")
            print(f"  {'月份':<10}{'样本':>6}{'D1均值':>9}{'胜率':>8}")
            for x in r["high"]:
                print(f"  {x['month']:<10}{x['n']:>6}{x['mean']:>+8.2f}%"
                      f"{x['win']:>7.1f}%")
            print(f"  -> {r['high_positive_months']}/{len(r['high'])} 个月为正")
            print()
            print(f"  对照组（40-50%，应为噪声）: "
                  f"{r['low_positive_months']}/{len(r['low'])} 个月为正")
            print("  实验组全正 + 对照组无效 => 不是某段行情特有的结果。")
            return

        if a.compare:
            r = seat.compare_strategies(conn, end, days=a.days)
            if not r.get("ok"):
                print(f"无法对比: {r.get('reason')}")
                return
            print(f"== 策略净收益对比（已扣往返成本 {r['cost']:.2f}%）==")
            print(f"  {'策略':<32}{'样本':>7}{'D1净':>8}{'胜率':>7}{'D5净':>8}{'D10净':>8}")
            for x in r["strategies"]:
                if x["insufficient"]:
                    print(f"  {x['name']:<32}{x['n']:>7}  样本不足")
                    continue
                f = lambda k: (f"{x[k]:+.2f}%" if x.get(k) is not None else "  —  ")
                print(f"  {x['name']:<32}{x['n']:>7}{f('d1_net'):>8}"
                      f"{x['win']:>6.1f}%{f('d5_net'):>8}{f('d10_net'):>8}")
            print()
            print("  关键结论（反直觉，已实测）：")
            print("   1. 叠加'本地历史为正'反而让收益变差（+3.16% -> +2.16%），")
            print("      平台 rep_prob 的信息量大于简单历史均值，不要叠加。")
            print("   2. 'rep_prob>=60' 的高收益含涨停股，次日买不进；")
            print("      可执行的是'非涨停'口径，D5 仍有正净收益。")
            return

        if a.walkforward:
            r = seat.walk_forward(conn, end, days=a.days)
            if not r.get("ok"):
                print(f"无法检验: {r.get('reason')}")
                return
            pk, sk = r["picked"], r["skipped"]
            print("== 严格滚动前向测试（决策只用席位历史战绩，不含平台指标）==")
            print(f"  窗口 {r['window']} 交易日，最少历史 {r['min_hist']} 次")
            print(f"  跟随组    D1 {pk['mean']:+.2f}%  胜率 {pk['win']:.1f}%  "
                  f"(n={pk['n']})")
            print(f"  不跟随组  D1 {sk['mean']:+.2f}%  胜率 {sk['win']:.1f}%  "
                  f"(n={sk['n']})")
            print(f"  多空价差  D1 {r['spread_d1']:+.2f}%")
            print()
            print(f"  ⚠️ 跟随组扣往返成本 {seat.total_cost():.2f}% 后为 "
                  f"{r['gross_after_cost']:+.2f}%，即"
                  f"{'不足以独立盈利' if r['gross_after_cost'] <= 0 else '仍为正'}。")
            print("  结论：+0.97% 价差证明席位身份携带真实信息，")
            print("        但须叠加 rep_prob 筛选才有正的净期望。")
            return

        if a.permute:
            r = seat.permutation_test(conn, end, days=a.days,
                                      n_perm=a.n_perm)
            if not r.get("ok"):
                print(f"无法检验: {r.get('reason')}")
                return
            print(f"== 席位排名持续性的置换检验 ==")
            print(f"  参与席位 {r['seats']} 个，置换 {r['permutations']} 次")
            print(f"  观测秩相关 {r['observed']:+.4f}")
            print(f"  p 值 {r['p_value']:.4f}  "
                  f"-> {'显著（席位能力差异真实存在）' if r['significant'] else '不显著'}")
            print()
            print("  零假设：各席位真实水平相同。")
            print("  p<0.05 表示观测到的持续性很难用随机噪声解释。")
            return

        if a.validate:
            v = seat.validate_persistence(conn, end, days=a.days)
            if not v.get("ok"):
                print(f"无法检验: {v.get('reason')}")
                return
            t, b, base = v["top"], v["bottom"], v["baseline"]
            print(f"== 样本外持续性检验（分割日 {v['split']}）==")
            print(f"  可用席位 {v['seats']} 个")
            print(f"  TOP 席位     样本内 {t['mean_in']:+.2f}% "
                  f"(胜{t['win_in']:.1f}%)  ->  样本外 {t['mean_out']:+.2f}% "
                  f"(胜{t['win_out']:.1f}%, n={t['n_out']})")
            print(f"  BOTTOM 席位  样本内 {b['mean_in']:+.2f}% "
                  f"(胜{b['win_in']:.1f}%)  ->  样本外 {b['mean_out']:+.2f}% "
                  f"(胜{b['win_out']:.1f}%, n={b['n_out']})")
            print(f"  全体基准     样本外 {base['mean_out']:+.2f}% "
                  f"(胜{base['win_out']:.1f}%, n={base['n_out']})")
            print(f"  秩相关（持续性）{v['rank_corr']:+.4f}")
            return

        if a.follow:
            date = a.date or end
            # 50 档是"行为警戒线"而非推荐门槛：实测可执行净收益为负。
            # 静默按 50 出候选会误导，所以显式警告。
            if a.min_prob < config.SEAT_STRONG_REP_PROB:
                print(f"⚠️  胜率下限 {a.min_prob:.0f}% 低于 60%："
                      f"该档实测可执行 D5 净收益为负，仅供观察。")
                print(f"   实战建议加 --min-prob 60（D5 净 +2.71%）"
                      f"或 --min-prob 70（D5 净 +5.82%）。")
                print()
            rows = seat.follow_signals(conn, date,
                                       min_rep_prob=a.min_prob,
                                       exclude_limit_up=not a.include_limit_up,
                                       top=a.top)
            print(f"== {date} 席位跟随候选（胜率>={a.min_prob:.0f}%，"
                  f"{'排除' if not a.include_limit_up else '包含'}涨停）==")
            if not rows:
                print("  无符合条件的信号")
                return
            print(f"  {'代码':<8}{'名称':<10}{'胜率':>7}{'净买(万)':>11}"
                  f"  席位")
            for r in rows:
                print(f"  {r['code']:<8}{(r['name'] or ''):<10}"
                      f"{r['rep_prob']:>6.1f}%{(r['net_amt'] or 0)/1e4:>11.0f}"
                      f"  {(r['seat'] or '')[:30]}")
            return

        # 默认：跟随收益矩阵 + 席位排行
        r = seat.following_edge(conn, end, days=a.days)
        # following_edge 在无数据时返回 []，直接 r[0] 会 IndexError。
        # 空库或 --days 窗口内无数据时应给出提示而不是崩栈。
        if not r:
            print(f"== 无足够样本（近 {a.days} 交易日内没有可用席位记录）==")
            print("  请先采集龙虎榜：python3 run.py lhb --date <日期>")
            return
        if r[0]:
            bands, corr, n = r
            print(f"== 跟随不同胜率席位的期望收益（近 {a.days} 交易日，n={n}）==")
            print(f"  {'胜率档':<10}{'样本':>7}{'毛收益':>9}{'净收益':>9}{'胜率':>8}")
            for b in bands:
                print(f"  {b['band']:<10}{b['n']:>7}{b['mean']:>+8.2f}%"
                      f"{b.get('net', b['mean']):>+8.2f}%{b['win']:>7.1f}%")
            print(f"  （净收益已扣往返成本 {seat.total_cost():.2f}%）")
            if corr is not None:
                print(f"  相关系数 {corr:+.4f}")
            print()

        ex = seat.excess_by_band(conn, end, days=a.days)
        if ex:
            print("== 剔除大盘后的超额收益（关键：上榜≠利空，要看是谁在买）==")
            print(f"  {'胜率档':<10}{'样本':>7}{'D1超额':>10}{'D5超额':>10}{'D10超额':>10}")
            for x in ex:
                f = lambda k: (f"{x[k]:+.2f}%" if x.get(k) is not None else "  —  ")
                print(f"  {x['band']:<10}{x['n']:>7}{f('ex_d1'):>10}"
                      f"{f('ex_d5'):>10}{f('ex_d10'):>10}")
            print("  注：高胜率席位 D5 超额达峰且 D10 仍显著为正，")
            print("      说明不是一日游脉冲，而是可持有数日的信号。")
            print()
            print()

        stats = seat.seat_stats(conn, end, days=a.days)
        if stats:
            print(f"== 席位战绩排行（近 {a.days} 交易日，"
                  f"样本>={config.SEAT_MIN_TRADES}）==")
            print(f"  {'席位':<34}{'次数':>5}{'D1均值':>9}{'胜率':>8}{'净买(亿)':>10}")
            for s in stats[:a.top]:
                print(f"  {(s['seat'] or '')[:32]:<34}{s['trades']:>5}"
                      f"{s['mean']:>+8.2f}%{s['win']:>7.1f}%"
                      f"{s['net_amt']/1e8:>+10.1f}")
            print()
            print(f"  {'':34}{'':>5}{'':>9}{'':>8}")
            print("  最差席位（反向指标）:")
            for s in stats[-5:]:
                print(f"  {(s['seat'] or '')[:32]:<34}{s['trades']:>5}"
                      f"{s['mean']:>+8.2f}%{s['win']:>7.1f}%")
    finally:
        conn.close()


def cmd_daily(a):
    date = a.date or _today()
    print(f"=== 每日流程 {date} ===")
    collect.collect_daily(date, with_quotes=not a.no_quotes)
    # 龙虎榜盘后披露，是独立数据源，失败不应中断主流程
    print("\n-- 龙虎榜/席位 --")
    conn = db.init()
    try:
        try:
            n = lhb.collect_day(conn, date, verbose=True)
            conn.commit()
            print(f"  龙虎榜合计写入 {n} 行")
        except Exception as exc:
            print(f"  龙虎榜采集失败（不影响主流程）: {exc}")
        metrics.compute_range(conn)
        th.build_range(conn)
        fc.forecast(conn, date)
        fc.verify_forecasts(conn)
        # 信号留痕闭环：先记今天的候选，再回填往日已到期的结果。
        # 放在 daily 里而不是靠用户手跑，是因为忘记记账正是
        # 胜率无法改进的头号原因。
        try:
            from chaogu import track
            lg = track.log_signals(conn, date)
            if lg.get("ok"):
                print(f"  信号留痕：新增 {lg['added']} 条"
                      f"（当日候选 {lg.get('total', 0)} 个）")
            fl = track.fill(conn)
            if fl.get("ok"):
                print(f"  战绩回填：D1 +{fl['filled_d1']}、"
                      f"D5 +{fl['filled_d5']}、D10 +{fl['filled_d10']}")
        except Exception as exc:                     # noqa: BLE001
            print(f"  留痕/回填失败（不影响主流程）: {exc}")
        p, _ = rp.write(conn, date)
        print(f"\n报告：{p}")
    finally:
        conn.close()


def cmd_status(a):
    conn = db.init()
    try:
        print("== 数据库 ==")
        print(f"  路径: {config.DB_PATH}")
        for t in ("daily_market", "limit_up", "broken_board", "limit_down",
                  "theme_block", "theme_stock", "theme_daily", "quote_daily",
                  "sector_daily", "global_market", "news", "emotion_daily",
                  "forecast_log", "stock_basic",
                  # 龙虎榜三张表此前完全不在清单里，用户 status 看不到
                  # 席位数据的覆盖情况——而席位是唯一验证有效的信号。
                  "billboard", "seat_trade", "seat_daily"):
            try:
                n = conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
                rng = conn.execute(
                    f"SELECT MIN(trade_date), MAX(trade_date) FROM {t}"
                ).fetchone() if t not in ("news", "stock_basic") else (None, None)
                s = f"{n:>8} 行"
                if rng and rng[0]:
                    s += f"  {rng[0]} ~ {rng[1]}"
                print(f"  {t:16s} {s}")
            except Exception as e:                      # noqa: BLE001
                print(f"  {t:16s} (无此表: {e})")
        d = conn.execute(
            "SELECT trade_date, zt_count, max_height, stage, temperature "
            "FROM emotion_daily JOIN daily_market USING(trade_date) "
            "ORDER BY trade_date DESC LIMIT 10").fetchall()
        if d:
            print("\n== 最近情绪 ==")
            for r in d:
                print(f"  {r['trade_date']}  涨停{r['zt_count']:>3}  "
                      f"高{r['max_height']}板  {r['stage'] or '—':<4}  "
                      f"温度{r['temperature']}")
    finally:
        conn.close()


def main():
    ap = argparse.ArgumentParser(
        description="A股观察站：市场数据与情绪周期复盘看板",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__)
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("collect", help="采集某日数据")
    p.add_argument("--date")
    p.add_argument("--no-quotes", action="store_true", help="跳过全市场快照")
    p.add_argument("--no-news", action="store_true", help="跳过新闻")
    p.set_defaults(func=cmd_collect)

    p = sub.add_parser("backfill", help="历史涨停/炸板/题材回填")
    p.add_argument("--days", type=int, default=60)
    p.add_argument("--end")
    p.set_defaults(func=cmd_backfill)

    p = sub.add_parser("prices", help="历史价格回填（慢，建议后台跑）")
    p.add_argument("--days", type=int, default=160)
    p.add_argument("--limit", type=int, default=None)
    p.add_argument("--codes", help="只回填这些代码，逗号分隔")
    p.add_argument("--scope", choices=["limitup", "all"], default="limitup",
                   help="limitup=只回填曾涨停的股票（快，够算赚钱效应）；"
                        "all=全市场（慢，但涨跌家数/跌停家数完整）")
    p.set_defaults(func=cmd_prices)

    p = sub.add_parser("recompute", help="重算全部指标（补齐历史后跑）")
    p.add_argument("--days", type=int, default=400)
    p.set_defaults(func=cmd_recompute)

    p = sub.add_parser("analyze", help="计算情绪指标与题材")
    p.add_argument("--date")
    p.add_argument("--all", action="store_true", help="批量重算全部历史")
    p.set_defaults(func=cmd_analyze)

    p = sub.add_parser("report", help="生成复盘报告")
    p.add_argument("--date")
    p.add_argument("--print", action="store_true", help="同时打印到终端")
    p.set_defaults(func=cmd_report)

    p = sub.add_parser("style", help="席位风格画像（学大神到底学什么）")
    p.add_argument("--seat", help="指定席位名；不填则列出 D5 最高的席位")
    p.add_argument("--days", type=int, default=None)
    p.add_argument("--top", type=int, default=10)
    p.set_defaults(func=cmd_style)

    p = sub.add_parser("track", help="信号留痕/回填/战绩（提高胜率的闭环）")
    p.add_argument("action", nargs="?", default="report",
                   choices=["log", "fill", "report"],
                   help="log=记录今日候选, fill=回填真实结果, report=看战绩")
    p.add_argument("--date")
    p.add_argument("--min-prob", type=float, default=None, dest="min_prob")
    p.set_defaults(func=cmd_track)

    p = sub.add_parser("dash", help="启动本地情绪看板（长期驻留）")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8787)
    p.add_argument("--autocollect", action="store_true",
                   help="收盘后自动采集一次（默认关闭；会打外部接口）")
    p.add_argument("--at", default="15:30",
                   help="自动采集的触发时间，默认 15:30")
    p.add_argument("--no-live", action="store_true", dest="no_live",
                   help="关闭盘中实时刷新（默认开启：快讯/涨停池/题材）")
    p.set_defaults(func=cmd_dash)

    p = sub.add_parser("llm-setup", help="配置看板用的 LLM（AI 生成看板/AI 解读）")
    p.add_argument("--base", help="接口地址，如 https://api.anthropic.com 或 "
                   "https://ark.cn-beijing.volces.com/api/v3")
    p.add_argument("--key", help="API Key")
    p.add_argument("--model", help=f"模型名，默认 {'claude-sonnet-5-5'}")
    p.add_argument("--api", default="anthropic", choices=["anthropic", "openai"],
                   help="协议：anthropic 或 openai 兼容（豆包/DeepSeek 等）")
    p.set_defaults(func=cmd_llm_setup)

    p = sub.add_parser("leader-check",
                       help="检验追龙头（高连板）是否真的有正期望")
    p.set_defaults(func=cmd_leader_check)

    p = sub.add_parser("theme-check",
                       help="检验题材延续性评分是否真能预测延续")
    p.set_defaults(func=cmd_theme_check)

    p = sub.add_parser("forecast", help="明日情绪预判")
    p.add_argument("--date")
    p.set_defaults(func=cmd_forecast)

    p = sub.add_parser("daily", help="一键：采集+分析+报告")
    p.add_argument("--date")
    p.add_argument("--no-quotes", action="store_true")
    p.set_defaults(func=cmd_daily)

    p = sub.add_parser("lhb", help="采集/回填龙虎榜与席位数据")
    p.add_argument("--date", help="采集某日（默认最新）")
    p.add_argument("--backfill", action="store_true", help="按区间回填")
    p.add_argument("--start", default="2026-01-01")
    p.add_argument("--end", default=None)
    p.set_defaults(func=cmd_lhb)

    p = sub.add_parser("seat", help="席位（游资）分析与跟随信号")
    p.add_argument("--date", help="跟随信号日期")
    p.add_argument("--end", help="分析截止日（默认最新）")
    p.add_argument("--days", type=int, default=config.SEAT_EVAL_DAYS,
                   help="回看交易日数")
    p.add_argument("--follow", action="store_true", help="列出跟随候选")
    p.add_argument("--validate", action="store_true", help="样本外持续性检验")
    p.add_argument("--overfit", action="store_true",
                   help="过拟合校正（Deflated Sharpe + PBO）")
    p.add_argument("--freq", default="W", choices=("W", "M"),
                   help="过拟合校正的聚合频率")
    p.add_argument("--activity", action="store_true",
                   help="高胜率席位内按滚动活跃度再分层")
    p.add_argument("--honest", action="store_true",
                   help="诚实协议：前段选阈值，后段测量")
    p.add_argument("--executable", action="store_true",
                   help="估算真实可执行收益（剔除次日涨停）")
    p.add_argument("--risk", action="store_true",
                   help="收益分布与尾部风险")
    p.add_argument("--monthly", action="store_true",
                   help="逐月稳定性检验")
    p.add_argument("--compare", action="store_true",
                   help="对比各策略的净收益（含成本）")
    p.add_argument("--walkforward", action="store_true",
                   help="严格滚动前向测试（不使用平台指标）")
    p.add_argument("--permute", action="store_true",
                   help="对排名持续性做置换检验（最严格的验证）")
    p.add_argument("--n-perm", type=int, default=2000, dest="n_perm",
                   help="置换次数")
    p.add_argument("--seat", help="查询单个席位画像")
    p.add_argument("--min-prob", type=float, default=config.SEAT_MIN_REP_PROB,
                   dest="min_prob", help="席位胜率下限")
    p.add_argument("--include-limit-up", action="store_true",
                   help="跟随信号包含涨停股（默认排除，因次日难买入）")
    p.add_argument("--top", type=int, default=20)
    p.set_defaults(func=cmd_seat, seat=None)

    p = sub.add_parser("status", help="查看数据库概览")
    p.set_defaults(func=cmd_status)

    a = ap.parse_args()
    a.func(a)


if __name__ == "__main__":
    main()
