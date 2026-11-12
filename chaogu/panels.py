#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""看板面板目录：每个面板 = 一个数据查询 + 一种默认可视化。

前端只认 viz 类型（stats / verdict / outlook / mainlines / events / ladder /
line / bar / table / news / markdown），数据全部由这里按 id 取。
AI 生成看板 = 让模型从 CATALOG 里挑面板、填参数、排布局，
所以模型**不能编造数据**，只能组织已有的查询。
"""

from __future__ import annotations

import copy
import json
import re
import threading
import time
import uuid
from datetime import datetime

from . import catalyst as cat
from . import config, llm, outlook, news_impact

# ------------------------------------------------------------------ 栅格常量
# 24 列栅格。行高由前端 CSS 变量 --grid-rows 决定（当前 38px 行高 + 10px 间距 = 48px/行）。
# 这里的上下限是权威值：后端 _sanitize 和前端拖拽都按它夹取，不要再各自写死数字。
GRID_COLUMNS = 24
MIN_PANEL_W, MAX_PANEL_W = 4, 24
MIN_PANEL_H, MAX_PANEL_H = 3, 20

# ------------------------------------------------------------------ 目录

CATALOG = [
    {"id": "portfolio_account", "title": "账户与持仓", "viz": "portfolio",
     "w": 24, "h": 10, "live": True,
     "desc": "当前可用现金、总资产、仓位、持仓盈亏；可切换手动记账与东方财富账户快照，不受历史日期筛选影响"},
    {"id": "portfolio_analysis", "title": "账户研究与资金规划工作台", "viz": "portfolio_analysis",
     "w": 24, "h": 14, "live": True,
     "desc": "结合最新盘面与当前账户，逐股核对继续持有、减仓退出、补仓条件；AI 分析需主动点击"},
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
    {"id": "news_impact", "title": "事件中心 · 新闻影响", "viz": "news_impact", "w": 24, "h": 15,
     "desc": "把近三日快讯合并为稳定事件，展示来源、证据等级、生命周期、行业传导、相关个股与验证条件；主动点击生成 AI 解读"},
    {"id": "watch_center", "title": "盯盘提醒与人气异动", "viz": "watch_center", "w": 24, "h": 13,
     "live": True, "desc": "管理行情预警与公开人气榜规则，查看数据源状态、触发记录和未读提醒"},
    {"id": "flow_center", "title": "资金异动", "viz": "flow_center", "w": 24, "h": 13,
     "live": True, "desc": "全市场主力资金净流入/净流出榜，覆盖人气榜 100 名以外的个股",
     "params": {"limit": "榜单条数，默认 30"}},
    {"id": "volume_rank", "title": "今日量能榜", "viz": "table", "w": 12, "h": 10,
     "live": True,
     "desc": "今日交易活跃度排行：按成交额/成交量/量比排序，列出价格、涨跌幅、成交额、成交量、换手率、量比",
     "params": {"limit": "榜单条数，默认 20",
                "sort": "排序口径：amount=成交额（默认）、volume=成交量、ratio=量比",
                "min_yi": "只看成交额不低于该值（亿元）的个股，默认 0 不过滤"}},
    {"id": "big_order", "title": "大单买入榜", "viz": "table", "w": 12, "h": 10,
     "live": True,
     "desc": "全市场按主力净买入（超大单+大单）从大到小排列，列出超大单/大单拆分、净占比与成交额；可设阈值只看大额买入",
     "params": {"limit": "榜单条数，默认 20",
                "min_wan": "净买入下限（万元），默认 100"}},
    {"id": "tick_watch", "title": "大单成交监控", "viz": "table", "w": 12, "h": 10,
     "live": True,
     "desc": "自选股 + 账户持仓的逐笔大单：连续竞价时段里单次主动买成交达到门槛的成交，按金额降序，含成交时间、价格、手数",
     "params": {"limit": "榜单条数，默认 20",
                "min_wan": "单次成交额下限（万元），默认 100"}},
    {"id": "event_archive", "title": "事件验证档案", "viz": "event_archive", "w": 24, "h": 12,
     "desc": "按历史日期汇总事件状态、旧 AI 判断、候选证券与 T+回看摘要，用于复盘事件是否验证或失效"},
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

# ------------------------------------------------------------------ 面板元数据
# 可信度说明的是"这个面板展示的东西能支持什么样的判断"，不是收益承诺。
# 依据见 README 第五节的实证结论；新增面板必须在这里登记，否则导入时报错。
TRUST = {
    # key: (完整说明, 面板角标, 角标样式)
    "verified":    ("站内样本外验证通过（仍有局限）", "已验证", "trust-verified"),
    "limited":     ("有条件回测，结论依赖口径", "回测", "trust-limited"),
    "descriptive": ("描述当前状态，不预测次日", "描述", "trust-descriptive"),
    "data":        ("原始数据或事件整理，需核对原文", "原始", "trust-data"),
    "watch":       ("用户自定义规则，效果取决于设置", "规则", "trust-watch"),
    "account":     ("本机账户数据，不连接券商下单", "账户", "trust-account"),
    "ai":          ("AI 生成，需对照数据核对", "AI", "trust-ai"),
}

# 参数规格：int 带范围；list 为逗号分隔的字符串列表；text 为限长文本。
# 未声明的参数在保存时被丢弃（运行时参数如 latest 不写入看板配置）。
META = {
    "portfolio_account": {"trust": "account", "basis":
        "账户来自本机手动记账或东方财富只读快照，只展示不下单。浮盈亏未扣未来卖出费用；"
        "本地历史行情可能含复权，需与券商价格核对。"},
    "portfolio_analysis": {"trust": "account", "basis":
        "依据最新盘面与你设定的条件，列出继续持有、减仓退出、补仓的研究清单，"
        "不推算买卖时点，不构成个性化建议。AI 分析仅在主动生成时调用。"},
    "verdict": {"trust": "descriptive", "basis":
        "情绪阶段的描述经验证有效；但次日赚钱效应与当日各项情绪指标几乎不相关。"
        "因此'明天怎么做'只作为风险姿态参考，不是预测。"},
    "market_stats": {"trust": "descriptive", "basis":
        "当日涨跌停、炸板、封板率、昨涨停溢价等统计。昨涨停溢价是赚钱效应的核心度量，"
        "描述当前状态，不预测次日。"},
    "outlook": {"trust": "limited", "basis":
        "排序 = 今日涨停家数 + 2×连续活跃天数。回测：连续活跃 3 天的主线次日续存 74%；"
        "每天取前 3 的续存率样本外约 60%（全部主线基准约 34%）。回答'明天这条线还在不在'，"
        "不是'买龙头能赚钱'。", "param_types": {"top": {"type": "int", "min": 1, "max": 10}}},
    "mainlines": {"trust": "descriptive", "basis":
        "按个股真实涨停原因聚类，每条原文来自其自身成分股。描述当日题材状态，不预测延续；"
        "题材延续性评分经检验无效，本面板不使用它。"},
    "news_impact": {"trust": "data", "basis":
        "快讯按稳定事件 ID 合并，证据等级、生命周期由规则给出。AI 解读需主动生成；"
        "事件与个股的关联只是线索，需打开原文核对。"},
    "watch_center": {"trust": "watch", "basis":
        "按你设置的价格与量能规则触发提醒，并显示数据源状态。提醒只是信息提示，"
        "不代表标的有利可图。"},
    "flow_center": {"trust": "data", "basis":
        "东方财富公开资金流：主力净额 = 超大单 + 大单净额，净占比 = 净额 / 成交额。"
        "金额单位元；各平台口径有差异，属描述性数据，不预测次日，也不构成买卖建议。",
        "param_types": {"limit": {"type": "int", "min": 5, "max": 100}}},
    "volume_rank": {"trust": "data", "basis":
        "东方财富公开行情快照，当日累计值：成交额单位为元，成交量单位为手，"
        "换手率 = 成交量 ÷ 流通股本，量比与换手率取东财口径（量比含多日同时段均量概念，"
        "与站内「成交量倍数」不是同一算法，不要互相换算）。只说明当下活跃度，不预测次日。"
        "量比口径下若不设成交额下限，小市值个股会占据前几名，属于正常现象而非异常放量信号。",
        "param_types": {"limit": {"type": "int", "min": 5, "max": 100},
                        "sort": {"type": "text", "max": 8},
                        "min_yi": {"type": "int", "min": 0, "max": 1000}}},
    "big_order": {"trust": "data", "basis":
        "东方财富公开资金流（净流入榜内筛选）：主力净额 = 超大单 + 大单净额，单位为元。"
        "这是按成交金额分档的**聚合**口径，不是逐笔单笔成交，无法指出某一笔具体委托；"
        "100 万元在整个市场并不稀有——实测全市场主力净买入 ≥100 万元的个股约 1800 只"
        "（约三分之一），所以阈值只用来过滤，相对强弱要看榜单里的金额大小。"
        "该来源覆盖全市场净流入/净流出两端各数百名，榜单只在这段范围内排序取前 N。"
        "各平台口径有差异，属描述性数据，不预测次日，也不构成买卖建议。",
        "param_types": {"limit": {"type": "int", "min": 5, "max": 100},
                        "min_wan": {"type": "int", "min": 0, "max": 1000000}}},
    "tick_watch": {"trust": "data", "basis":
        "东方财富公开分时成交明细，**只覆盖自选股 + 账户持仓**（每只一次请求，全市场"
        "逐笔轮询不现实，池子超过 30 只时只监控前 30 只并在面板上标明）。"
        "口径：交易所对外免费分发的是 3 秒快照，所以这里是"
        "「这 3 秒里主动买成交了 N 万元」，**不是某一笔委托 N 万元**——真正的逐笔需要 "
        "Level-2 付费行情；东财自己的大单统计同样按这类快照分档，故与业界口径一致。"
        "只统计连续竞价时段：集合竞价没有主动买卖之分，接口却会给它标方向，"
        "实测贵州茅台当天有 115 条 ≥100 万的竞价快照，排除后不会误报。"
        "方向由相邻快照的价差均值验证（主动买均值为正、主动卖为负）。"
        "属描述性数据，不预测次日，也不构成买卖建议。",
        "param_types": {"limit": {"type": "int", "min": 5, "max": 100},
                        "min_wan": {"type": "int", "min": 0, "max": 1000000}}},
    "event_archive": {"trust": "data", "basis":
        "按历史日期汇总事件状态、旧 AI 判断与 T+ 回看摘要，用于复盘事件是否验证或失效。"},
    "events": {"trust": "data", "basis":
        "同一段行业原因带动多只涨停时聚合为一个催化事件，来源为同花顺涨停原因，属于描述性归类。"},
    "ladder": {"trust": "descriptive", "basis":
        "按连板高度排列。回测显示：高板个股中可买入部分的净超额为负，买不进的比例随高度上升。"
        "可以看，不要据此追高。"},
    "emotion_trend": {"trust": "descriptive", "basis":
        "情绪温度、涨跌停家数与昨涨停溢价的历史走势，用于判断周期位置，不预测明日。",
        "param_types": {"days": {"type": "int", "min": 5, "max": 400}}},
    "height_trend": {"trust": "descriptive", "basis":
        "最高连板、连板家数、封板率的历史走势，描述空间高度的变化。",
        "param_types": {"days": {"type": "int", "min": 5, "max": 400}}},
    "line_trend": {"trust": "descriptive", "basis":
        "指定关键词的涨停家数历史，描述主线热度。热度高不等于接下来仍会延续。",
        "param_types": {"keywords": {"type": "list", "max": 8, "item_max": 12},
                        "days": {"type": "int", "min": 5, "max": 120}}},
    "limitup": {"trust": "data", "basis":
        "当日全部涨停股明细：高度、首封时间、开板次数、封单、换手与涨停原因，为原始记录。",
        "param_types": {"keyword": {"type": "text", "max": 20}}},
    "broken": {"trust": "data", "basis":
        "当日炸板股原始记录，是分歧的直接信号，但不能单独判断后续走势。"},
    "blocks": {"trust": "data", "basis":
        "同花顺题材板块按涨停家数排行并附板块涨幅，原始数据。"},
    "news": {"trust": "data", "basis":
        "新浪 7×24 快讯原文，自动标注命中的主线关键词。快讯不等于利好，需核对原文。",
        "param_types": {"keyword": {"type": "text", "max": 20},
                        "limit": {"type": "int", "min": 1, "max": 200}}},
    "global": {"trust": "data", "basis":
        "纳指、道指、标普、恒生、日经、美元、黄金、原油的涨跌，原始行情。"},
    "seats": {"trust": "verified", "basis":
        "龙虎榜高胜率席位（胜率≥50%）买入的个股。站内样本外秩相关 +0.40，置换检验 p=0.0015，"
        "逐月 9/9 为正。局限：该结论尚未被外部公开资料独立复现；次日买入仍有约 27% 买不进。"},
    "ai_brief": {"trust": "ai", "basis":
        "AI 基于当日看板数据撰写的明日计划，可能出错。每条结论都应对照上方数据核对，"
        "成立与放弃信号需要你自己确认。"},
    "ai_answer": {"trust": "ai", "basis":
        "AI 只依据看板数据作答，回答不构成投资建议。",
        "param_types": {"question": {"type": "text", "max": 500}}},
}

for _p in CATALOG:
    if _p["id"] not in META:
        raise RuntimeError(f"面板 {_p['id']} 缺少元数据（trust/basis），请在 panels.META 中登记")
    _m = META[_p["id"]]
    if _m["trust"] not in TRUST:
        raise RuntimeError(f"面板 {_p['id']} 的可信度 {_m['trust']} 不在 TRUST 中")
    _p["trust"] = _m["trust"]
    _p["basis"] = _m["basis"]
    _p["trust_label"], _p["trust_short"], _p["trust_class"] = TRUST[_m["trust"]]
    _p["param_types"] = _m.get("param_types", {})
    _undeclared = set(_p["param_types"]) - set(_p.get("params") or {})
    if _undeclared:
        raise RuntimeError(f"面板 {_p['id']} 的参数规格 {_undeclared} 未在 params 中声明")

WORKSPACE_IDS = ("information", "learning", "portfolio", "archive")


def clean_params(pid, params):
    """按面板参数规格清洗参数。返回 (清洗后的参数, 被丢弃的参数名)。"""
    if not isinstance(params, dict):
        return {}, []
    spec = BY_ID[pid].get("param_types") or {}
    allowed = BY_ID[pid].get("params") or {}
    out, dropped = {}, []
    for k, v in params.items():
        if k not in allowed:
            dropped.append(k)
            continue
        s = spec.get(k, {"type": "text", "max": 200})
        try:
            if s["type"] == "int":
                if isinstance(v, bool):
                    raise ValueError
                out[k] = max(s["min"], min(s["max"], int(v)))
            elif s["type"] == "list":
                items = v.split(",") if isinstance(v, str) else v
                if not isinstance(items, list):
                    raise ValueError
                items = [str(x).strip()[: s["item_max"]] for x in items if str(x).strip()][: s["max"]]
                if not items:
                    raise ValueError
                out[k] = items
            else:
                text = str(v).strip()[: s["max"]]
                if not text:
                    raise ValueError
                out[k] = text
        except (TypeError, ValueError, KeyError):
            dropped.append(k)
    return out, dropped


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
    latest = p.get("latest") is True
    cutoff = datetime.now().isoformat() if latest else news_impact.bounds(date)[1]
    sql = "SELECT ts, title, content, tags FROM news WHERE replace(ts,' ','T')<? "
    args = [cutoff]
    if kw:
        sql += "AND (title LIKE ? OR content LIKE ?) "
        args.extend([f"%{kw}%", f"%{kw}%"])
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
    scope = (f"最新 7×24 快讯 · 最新发布 {rows[0]['ts'].replace('T', ' ') if rows else '暂无已采集快讯'}" if latest
             else f"历史快讯 · 截至 {date}")
    return {"items": out, "scope": scope}


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
        "SELECT ts, title FROM news WHERE replace(ts,' ','T')<? ORDER BY ts DESC LIMIT 25",
        (news_impact.bounds(date)[1],))]
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
        return {"md": "**AI 未配置。** 打开看板右上「⋯ → AI 设置」填写接口地址与 Key"
                      "（或运行 `python3 run.py llm-setup`）后刷新。\n\n"
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
        # 直接把网关的 401 报文甩到界面上没人看得懂，归类成"哪里断了 + 下一步做什么"。
        return {"md": llm.failure_md(), "ai": False, "failure": llm.last_failure}
    return {"md": md, "ai": True,
            "at": datetime.now().strftime("%H:%M")}


def q_ai_answer(conn, date, p):
    q = (p.get("question") or "").strip()
    if not q:
        return {"md": "在面板参数里填写问题。"}
    if not llm.available():
        return {"md": "**AI 未配置。** 打开看板右上「⋯ → AI 设置」填写接口地址与 Key。"}
    snap = snapshot(conn, date)
    key = ("ans", date, q, _sig(conn, date))

    def gen():
        return llm.chat(SYS_ANALYST, "数据：\n```json\n" + json.dumps(
            snap, ensure_ascii=False, default=str) + f"\n```\n问题：{q}\n"
            "用 Markdown 回答，300 字内，先给结论。", max_tokens=1500)
    md = _cached(key, 900, gen)
    if not md:
        _CACHE.pop(key, None)
        return {"md": llm.failure_md(), "ai": False, "failure": llm.last_failure}
    return {"md": md, "ai": True}


QUERIES = {
    "verdict": q_verdict, "market_stats": q_market_stats, "outlook": q_outlook,
    "mainlines": q_mainlines, "events": q_events, "ladder": q_ladder,
    "emotion_trend": q_emotion_trend, "height_trend": q_height_trend,
    "line_trend": q_line_trend, "limitup": q_limitup, "broken": q_broken,
    "blocks": q_blocks, "news": q_news, "global": q_global, "seats": q_seats,
    "news_impact": lambda conn,date,p: news_impact.view(
        conn, datetime.now().strftime("%Y-%m-%d") if p.get("latest") is True else date,
        latest=p.get("latest") is True),
    "event_archive": lambda conn,date,p: news_impact.archive(conn,date),
    "ai_brief": q_ai_brief, "ai_answer": q_ai_answer,
}


def q_portfolio(conn, date, p):
    from . import portfolio, workbench
    return workbench.account_view(portfolio.snapshot(conn))


def q_portfolio_analysis(conn, date, p):
    from . import portfolio
    return portfolio.analysis(conn)


QUERIES.update(portfolio_account=q_portfolio, portfolio_analysis=q_portfolio_analysis)


def q_watch_center(conn, date, p):
    from . import watch
    data = watch.dashboard()
    data["positions"] = watch.position_targets()
    data["status"] = watch.status()
    return data


QUERIES["watch_center"] = q_watch_center


def q_flow_center(conn, date, p):
    from . import watch
    limit = int((p or {}).get("limit") or 30)
    data = watch.flow_board(limit=limit)
    data["sources"] = {"eastmoney_flow": watch.source_status().get("eastmoney_flow")}
    data["rules"] = [r for r in watch.list_rules(kind="flow")]
    return data


QUERIES["flow_center"] = q_flow_center

# 量能榜的排序口径 -> (东财字段, 中文名)。字段含义见 sources/fundflow.RANK_FIELDS。
_VOLUME_SORTS = {"amount": ("f6", "成交额"), "volume": ("f5", "成交量"),
                 "ratio": ("f10", "量比")}


def q_volume_rank(conn, date, p):
    """今日量能榜：全市场按成交额/成交量/量比排序。"""
    from .sources import fundflow
    p = p or {}
    limit = int(p.get("limit") or 20)
    fid, label = _VOLUME_SORTS.get(str(p.get("sort") or "").strip().lower(),
                                   _VOLUME_SORTS["amount"])
    min_yi = int(p.get("min_yi") or 0)
    floor = min_yi * 1e8
    # 设了成交额下限就要多取一段再筛，否则筛完可能不够 limit 条。
    window = limit if floor <= 0 else min(300, max(limit * 3, 60))

    def fetch():
        try:
            return fundflow.fetch_volume_board(fid=fid, limit=window), None, \
                datetime.now().strftime("%H:%M:%S")
        except Exception as exc:                       # noqa: BLE001
            return [], str(exc)[:160], None

    rows, err, stamp = _cached(("volume_rank", fid, window, floor), 60, fetch)
    if floor > 0:
        rows = [r for r in rows if (r.get("amount") or 0) >= floor]
    out = [{
        "code": r["code"], "name": r["name"],
        "pct": round(r["pct"], 2) if r["pct"] is not None else None,
        "amount": _yi(r["amount"]),
        "volume": round(r["volume"] / 1e4, 1) if r["volume"] is not None else None,
        "turnover": round(r["turnover"], 2) if r["turnover"] is not None else None,
        "volume_ratio": round(r["volume_ratio"], 2) if r["volume_ratio"] is not None else None,
    } for r in rows[:limit]]
    note = (f"数据源异常，本榜暂不可用：{err}" if err else
            f"东方财富公开行情 · 全市场按{label}降序"
            + (f" · 成交额≥{min_yi}亿" if min_yi > 0 else "")
            + (f" · 更新 {stamp}" if stamp else ""))
    return {"columns": [("name", "名称"), ("pct", "涨跌%"), ("amount", "成交额(亿)"),
                        ("volume", "成交量(万手)"), ("turnover", "换手%"),
                        ("volume_ratio", "量比")],
            "rows": out, "note": note}


def q_big_order(conn, date, p):
    """大单买入榜：主力净买入（超大单+大单）降序，阈值只做过滤。

    榜单从全市场净流入第一名开始取，所以阈值设在很低的量级（例如 100 万）时，
    它几乎不起筛选作用——这是数据本身的性质，不是 bug。面板口径里已写明。
    """
    from .sources import fundflow
    p = p or {}
    limit = int(p.get("limit") or 20)
    min_wan = 100 if p.get("min_wan") is None else int(p["min_wan"])
    floor = max(0, min_wan) * 1e4
    window = min(300, max(limit * 3, 100))

    def fetch():
        try:
            return fundflow.fetch_rank(fid="f62", direction="in", limit=window), None, \
                datetime.now().strftime("%H:%M:%S")
        except Exception as exc:                       # noqa: BLE001
            return [], str(exc)[:160], None

    rows, err, stamp = _cached(("big_order", window, floor), 60, fetch)
    hit = [r for r in rows if (r.get("main_net") or 0) >= floor]

    def wan(v):
        return round(v / 1e4, 1) if v is not None else None

    out = [{
        "code": r["code"], "name": r["name"],
        "pct": round(r["pct"], 2) if r["pct"] is not None else None,
        "main_wan": wan(r.get("main_net")), "super_wan": wan(r.get("super_net")),
        "large_wan": wan(r.get("large_net")),
        "main_ratio": round(r["main_ratio"], 2) if r.get("main_ratio") is not None else None,
        "amount": _yi(r.get("amount")),
    } for r in hit[:limit]]
    note = (f"数据源异常，本榜暂不可用：{err}" if err else
            f"东方财富公开资金流 · 主力净买入 ≥{min_wan}万元 · 净流入前 {window} 名内命中 {len(hit)} 只，"
            f"按净额降序显示前 {len(out)} 只"
            + (f" · 更新 {stamp}" if stamp else ""))
    return {"columns": [("name", "名称"), ("pct", "涨跌%"), ("main_wan", "主力净额(万)"),
                        ("super_wan", "超大单(万)"), ("large_wan", "大单(万)"),
                        ("main_ratio", "净占比%"), ("amount", "成交额(亿)")],
            "rows": out, "note": note}


QUERIES["volume_rank"] = q_volume_rank
QUERIES["big_order"] = q_big_order


def q_tick_watch(conn, date, p):
    """大单成交监控：自选股 + 持仓的逐笔大单（3 秒快照口径）。"""
    from . import watch
    p = p or {}
    limit = int(p.get("limit") or 20)
    min_wan = 100 if p.get("min_wan") is None else int(p["min_wan"])
    board = watch.tick_board(limit=limit, min_wan=min_wan)
    src = (watch.source_status().get("eastmoney_ticks") or {})
    rows = [{
        "code": r["code"], "name": r["name"] or r["code"],
        "time": (r["ts"] or "")[:8],
        "price": round(r["price"], 2) if r["price"] is not None else None,
        "lots": r["lots"],
        "amount_wan": round(r["amount"] / 1e4, 1) if r["amount"] is not None else None,
    } for r in board["rows"]]
    mode = board.get("pool") or "user"
    if not board["pool_total"]:
        note = ("暂无监控对象：自选股与账户持仓都是 0 只，回退用的全市场成交额榜也没取到。"
                "加入自选股后本面板会自动切到你自己的票。")
    elif src.get("state") == "error":
        note = f"数据源异常，本榜暂不可用：{src.get('error') or '分时成交请求失败'}"
    else:
        pool_text = ("未设自选股与持仓，正在监控**全市场成交额前 "
                     f"{board['monitored']} 名**" if mode == "market" else
                     f"监控 {board['monitored']}/{board['pool_total']} 只（自选股+持仓）")
        note = (f"{pool_text} · 主动买单次成交 ≥{min_wan}万 · "
                f"今日命中 {board['matched']} 条"
                + (f" · 数据 {str(board['source_at'])[11:19]}" if board["source_at"] else "")
                + " · 3 秒快照口径，不是逐笔委托")
    return {"columns": [("name", "股票"), ("time", "成交时间"), ("price", "价格"),
                        ("lots", "手数"), ("amount_wan", "成交额(万)")],
            "rows": rows, "note": note, "pool": mode}


QUERIES["tick_watch"] = q_tick_watch


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
    # 直接带上坐标：新建或恢复默认时文件一次写到位，不必等下一条读取再迁移。
    return _ensure_layout([
        {"id": "information", "title": "信息中心", "builtin": True, "panels": [
            _p("market_stats"), _p("news_impact"), _p("news"),
            _p("mainlines"), _p("events"), _p("global"),
        ]},
        {"id": "learning", "title": "短线学习", "builtin": True, "panels": [
            _p("verdict"), _p("outlook"), _p("watch_center"), _p("flow_center"),
            _p("mainlines"), _p("ladder", w=12), _p("seats", w=12), _p("limitup"),
            _p("broken"), _p("line_trend"), _p("height_trend"),
        ]},
        portfolio_dashboard(),
        {"id": "archive", "title": "复盘档案", "builtin": True, "panels": [
            _p("event_archive"), _p("news_impact"), _p("events"), _p("line_trend"),
            _p("emotion_trend", w=12), _p("ai_brief"), _p("global"),
        ]},
    ])


def portfolio_dashboard():
    return {"id": "portfolio", "title": "长期理财", "builtin": True, "panels": [
        _p("portfolio_account"), _p("portfolio_analysis"),
    ]}


def _ensure_builtin_dashboards(ds):
    """补齐缺失的内置看板并修正旧布局中的面板。不覆盖用户改过的标题。"""
    defaults = default_dashboards()
    existing = {d.get("id") for d in ds}
    for dash in defaults:
        if dash["id"] not in existing:
            ds.append(dash)
            existing.add(dash["id"])
    return ds


def _migrate_legacy_panels(ds):
    """仅对旧版文件执行一次的面板迁移。之后用户删除的面板不会被自动补回。"""
    for dash in ds:
        if dash.get("id") == "learning" and dash.get("builtin") and not any(
                p.get("id") == "watch_center" for p in dash.get("panels", [])):
            dash["panels"].insert(min(2, len(dash["panels"])), _p("watch_center"))
        if dash.get("id") == "review" and not any(p["id"] == "news_impact" for p in dash.get("panels", [])):
            dash["panels"].insert(3, _p("news_impact"))
        if dash.get("id") == "archive" and not any(p["id"] == "event_archive" for p in dash.get("panels", [])):
            dash["panels"].insert(0, _p("event_archive"))
        if dash.get("id") == "portfolio" and dash.get("builtin"):
            dash["panels"] = [p for p in dash.get("panels", [])
                              if p.get("id") not in {"market_stats", "mainlines", "news"}]
    return ds


# 标签顺序版本：文件里 tab_order == 2 时，列表顺序即标签栏顺序。
# 旧文件没有该字段，首次读取时把工作区固定到前面，与旧版显示一致。
# 3：面板增加 x/y 坐标，读取时补齐并写回（见 _ensure_layout / _backup_before_layout）。
TAB_ORDER_VERSION = 3
_DLOCK = threading.Lock()


def _legacy_order(ds):
    by_id = {d.get("id"): d for d in ds}
    head = [by_id[i] for i in WORKSPACE_IDS if i in by_id]
    return head + [d for d in ds if d.get("id") not in WORKSPACE_IDS]


def _write(ds):
    """原子写入：先写临时文件再替换，避免写到一半时丢失看板。"""
    tmp = DASH_PATH.with_name(DASH_PATH.name + ".tmp")
    tmp.write_text(json.dumps({"tab_order": TAB_ORDER_VERSION, "dashboards": ds},
                              ensure_ascii=False, indent=1), "utf-8")
    tmp.replace(DASH_PATH)


def _read():
    """读取看板列表。调用方必须持有 _DLOCK。"""
    try:
        raw = json.loads(DASH_PATH.read_text("utf-8"))
    except FileNotFoundError:
        raw = None
    except Exception:
        # 文件损坏时先备份原文件，再恢复默认，绝不静默覆盖用户看板。
        bad = DASH_PATH.with_name(f"dashboards.json.broken-{int(time.time())}")
        try:
            DASH_PATH.replace(bad)
        except OSError:
            pass
        raw = None
    ds = raw.get("dashboards") if isinstance(raw, dict) else None
    if not isinstance(ds, list) or not ds:
        ds = default_dashboards()
        _write(ds)
        return ds
    if raw.get("tab_order") != TAB_ORDER_VERSION:
        # 旧版文件：一次性迁移，并写回版本号，之后不再重复执行。
        # 面板级迁移（把 watch_center / news_impact / event_archive 补回去）只对真正
        # 没有版本号的旧文件执行一次：已有版本号的文件不能被重新补面板，否则用户
        # 主动删掉的面板会自己回来。这里只是多补了一次坐标，不动面板集合。
        if raw.get("tab_order") is None:
            ds = _migrate_legacy_panels(_legacy_order(_ensure_builtin_dashboards(ds)))
        else:
            ds = _ensure_builtin_dashboards(ds)
        try:
            ds = _ensure_layout(ds)
        except (AttributeError, KeyError, TypeError, ValueError):
            # 布局求解失败时保留原文件不动，绝不写入半成品，也不用默认看板顶替。
            return ds
        _backup_before_layout()
        _write(ds)
        return ds
    return _ensure_layout(_ensure_builtin_dashboards(ds))


def load_dashboards():
    with _DLOCK:
        return _read()


def save_dashboard(dash):
    """保存看板。已存在则原地替换（保持标签位置），否则追加到末尾。"""
    dash = _sanitize(dash)
    with _DLOCK:
        ds = _read()
        i = next((k for k, x in enumerate(ds) if x["id"] == dash["id"]), None)
        if i is None:
            dash["builtin"] = False
            ds.append(dash)
        else:
            dash["builtin"] = bool(ds[i].get("builtin"))
            ds[i] = dash
        _write(ds)
    return dash


def reorder_dashboards(ids):
    """按给定 id 顺序重排标签。未提及的看板依原顺序放到末尾，未知 id 忽略。"""
    if not isinstance(ids, list):
        raise ValueError("ids 必须是列表")
    with _DLOCK:
        ds = _read()
        by_id = {d["id"]: d for d in ds}
        seen = []
        for did in ids:
            if did in by_id and did not in seen:
                seen.append(did)
        ds = [by_id[i] for i in seen] + [d for d in ds if d["id"] not in seen]
        _write(ds)
    return ds


def duplicate_dashboard(did):
    """复制看板到源看板右侧，副本为自定义看板，面板 uid 重新生成。"""
    with _DLOCK:
        ds = _read()
        i = next((k for k, x in enumerate(ds) if x["id"] == did), None)
        if i is None:
            raise ValueError("看板不存在")
        src = ds[i]
        copy = _sanitize({
            "id": "d" + uuid.uuid4().hex[:6],
            "title": f"{src.get('title') or '看板'} 副本",
            "prompt": src.get("prompt"),
            "panels": [{**p, "uid": None} for p in src.get("panels") or []],
        })
        ds.insert(i + 1, copy)
        _write(ds)
    return copy


# ------------------------------------------------------------------ 导入导出
# 导出文件只含布局、面板 id、标题、参数和生成时的提示语，不含行情、账户或本机路径。
# 导入时只接受已知面板，参数经过 clean_params，并返回警告，由用户确认后才保存。
EXPORT_FORMAT = "chaogu-dashboard"
# 2：面板带上 x/y 坐标。v1 文件仍可导入，坐标由 _solve_layout 按顺序补。
EXPORT_VERSION = 2
MAX_IMPORT_PANELS = 60


def export_dashboard(did):
    with _DLOCK:
        d = next((x for x in _read() if x["id"] == did), None)
    if d is None:
        raise ValueError("看板不存在")
    return {
        "format": EXPORT_FORMAT,
        "schema_version": EXPORT_VERSION,
        "exported_at": datetime.now().isoformat(timespec="seconds"),
        "dashboard": {
            "title": d.get("title") or "看板",
            "prompt": d.get("prompt") if isinstance(d.get("prompt"), str) else None,
            "panels": [{"id": p["id"], "title": p.get("title"), "w": p.get("w"),
                        "h": p.get("h"), "x": p.get("x"), "y": p.get("y"),
                        "params": p.get("params") or {}}
                       for p in d.get("panels") or []],
        },
    }


def preview_import(payload):
    """校验导入内容，不写入文件。返回可保存的看板草稿（新 id、非内置）与警告列表。"""
    if not isinstance(payload, dict):
        raise ValueError("导入内容必须是 JSON 对象")
    if payload.get("format") != EXPORT_FORMAT:
        raise ValueError("这不是看板导出文件（format 不是 chaogu-dashboard）")
    ver = payload.get("schema_version")
    if not isinstance(ver, int) or isinstance(ver, bool) or ver < 1:
        raise ValueError("缺少有效的 schema_version")
    if ver > EXPORT_VERSION:
        raise ValueError(f"文件版本 {ver} 高于当前支持的 {EXPORT_VERSION}，请升级后再导入")
    body = payload.get("dashboard")
    if not isinstance(body, dict):
        raise ValueError("缺少 dashboard 内容")
    raw_panels = body.get("panels")
    if not isinstance(raw_panels, list):
        raise ValueError("dashboard.panels 必须是列表")
    if len(raw_panels) > MAX_IMPORT_PANELS:
        raise ValueError(f"面板数量超过上限 {MAX_IMPORT_PANELS}")
    title = str(body.get("title") or "").strip()
    if not title:
        raise ValueError("看板名称为空")

    warnings, kept, skipped = [], [], 0
    for i, rp in enumerate(raw_panels, 1):
        if not isinstance(rp, dict):
            skipped += 1
            warnings.append(f"第 {i} 个面板格式错误，已跳过")
            continue
        pid = rp.get("id")
        known = pid in BY_ID
        label = str(rp.get("title") or (BY_ID[pid]["title"] if known else pid) or f"第 {i} 个")[:40]
        if not known:
            skipped += 1
            warnings.append(f"面板「{label}」在当前版本中不存在（{pid}），已跳过")
            continue
        raw = rp.get("params") if isinstance(rp.get("params"), dict) else {}
        params, dropped = clean_params(pid, raw)
        for k in dropped:
            warnings.append(f"「{label}」的参数 {k} 不被支持或取值无效，已忽略")
        spec = BY_ID[pid].get("param_types") or {}
        for k, v in params.items():
            if spec.get(k, {}).get("type") == "int":
                try:
                    if int(raw.get(k)) != v:
                        warnings.append(f"「{label}」的参数 {k} 超出范围，已调整为 {v}")
                except (TypeError, ValueError):
                    pass
        entry = {"id": pid, "title": rp.get("title"), "w": rp.get("w"),
                 "h": rp.get("h"), "params": params}
        # v1 导出文件没有坐标，交给 _sanitize → _solve_layout 按顺序排布。
        if rp.get("x") is not None and rp.get("y") is not None:
            entry["x"], entry["y"] = rp["x"], rp["y"]
        kept.append(entry)

    prompt = body.get("prompt")
    draft = _sanitize({
        "id": "d" + uuid.uuid4().hex[:6],
        "title": title,
        "prompt": prompt[:2000] if isinstance(prompt, str) else None,
        "panels": kept,
    })
    draft["builtin"] = False
    return {"dashboard": draft, "warnings": warnings, "skipped": skipped}


def delete_dashboard(did):
    if did in WORKSPACE_IDS:
        raise ValueError("工作区看板不能删除，可以重命名或复制")
    with _DLOCK:
        ds = [x for x in _read() if x["id"] != did]
        _write(ds)


def reset_dashboards():
    with _DLOCK:
        DASH_PATH.unlink(missing_ok=True)
        return _read()


# ------------------------------------------------------------------ 布局求解
# 面板位置用 24 列栅格上的 (x, y)：x 是列起点 0..23，y 是行起点。约束 x + w <= 24。
# 求解逻辑在后端是权威实现，前端 mirror 同一套算法（app.js 的 compactPanels），
# 保存时再经 _sanitize 归一，所以前端算错也不会写出非法布局。

def _flow_layout(panels):
    """按数组顺序做行优先排布，复刻改动前 grid-auto-flow:row 的视觉。"""
    x = y = row_h = 0
    for p in panels:
        w, h = int(p["w"]), int(p["h"])
        if x and x + w > GRID_COLUMNS:
            x, y, row_h = 0, y + row_h, 0
        p["x"], p["y"] = x, y
        x += w
        row_h = max(row_h, h)
    return panels


def _compact(panels):
    """Grafana 式重力：按 (y, x) 顺序把每块面板上移到不与他人重叠的最小 y。"""
    placed = []
    for p in sorted(panels, key=lambda a: (int(a["y"]), int(a["x"]))):
        px, py, pw, ph = int(p["x"]), int(p["y"]), int(p["w"]), int(p["h"])
        y = 0
        while True:
            hit = [q for q in placed
                   if q["y"] < y + ph and y < q["y"] + int(q["h"])
                   and q["x"] < px + pw and px < q["x"] + int(q["w"])]
            if not hit:
                break
            y = min(q["y"] + int(q["h"]) for q in hit)
        p["y"] = y
        placed.append(p)
    return panels


def _solve_layout(panels):
    """夹取尺寸与坐标、补齐缺失坐标、消除重叠。就地修改并返回同一列表。

    只要有任何一块面板缺少 x/y（旧文件、导入文件、AI 生成、新建面板），
    就整表按数组顺序重排——这样调用方完全不必关心坐标，顺序即布局意图。
    """
    if not panels:
        return panels
    missing = any("x" not in p or "y" not in p for p in panels)
    for p in panels:
        p["w"] = max(MIN_PANEL_W, min(MAX_PANEL_W, int(p.get("w") or MIN_PANEL_W)))
        p["h"] = max(MIN_PANEL_H, min(MAX_PANEL_H, int(p.get("h") or MIN_PANEL_H)))
    if missing:
        _flow_layout(panels)
    for p in panels:
        p["x"] = max(0, min(GRID_COLUMNS - int(p["w"]), int(p.get("x") or 0)))
        p["y"] = max(0, int(p.get("y") or 0))
    return _compact(panels)


def _ensure_layout(ds):
    """给所有看板补齐布局。只补不改：已有的坐标、顺序、参数一律保留。"""
    for dash in ds:
        panels = dash.get("panels")
        if isinstance(panels, list):
            _solve_layout(panels)
    return ds


# ------------------------------------------------------------------ 对外可见范围
# 服务绑到 0.0.0.0 之后，内网其他人也能拿到 /api/meta 和 /api/panel。
# 这个名单决定他们看不到什么：
#   portfolio_  账户与持仓、资金规划工作台
#   watch_      盯盘中心
#   ai_         AI 面板 —— 内容是市场数据不涉及隐私，但每次打开都会真实调用
#               模型、花用户的钱，不能白送给能连上端口的人
# 判定按前缀，所以以后新增面板只要沿用这几类前缀就自动被挡住。
PRIVATE_PANEL_PREFIXES = ("portfolio_", "watch_", "ai_")


def is_private_panel(pid):
    """面板是否只给本机看。"""
    return str(pid).startswith(PRIVATE_PANEL_PREFIXES)


def visible_catalog(is_local=True):
    """面板目录按来源裁剪，和 public_dashboards 用同一套可见范围。

    非本机连"存在账户与持仓面板"这件事都不该知道，所以目录也要裁——
    否则对方能在添加面板的选择器里看到它的标题和说明。
    """
    if is_local:
        return CATALOG
    return [c for c in CATALOG if not is_private_panel(c.get("id", ""))]


def public_dashboards(dashboards, is_local=True):
    """按访问来源裁剪看板列表，供 /api/meta 使用。

    非本机访问时不裁会有两个问题：对方会看到「长期理财」这个标签、点进去只有
    一行 403 报文（看起来像坏了），而且等于告诉他你有这个看板。裁掉面板后坐标
    要重新压紧，否则原本的位置会留下一片空洞。
    """
    if is_local:
        return dashboards
    out = []
    for dash in dashboards:
        keep = [copy.deepcopy(p) for p in (dash.get("panels") or [])
                if not is_private_panel(p.get("id", ""))]
        if not keep:
            continue                        # 整块都私密的看板干脆不出现
        _compact(keep)
        item = copy.deepcopy(dash)
        item["panels"] = keep
        out.append(item)
    return out


def _backup_before_layout():
    """布局迁移写回前，把现有看板文件另存一份。

    命名沿用项目已有的 dashboards.json.before-* 约定。这是 JSON 文件而非 SQLite，
    直接复制即可（docs/ARCHITECTURE.md 里那条 SQLite backup API 要求不适用）。
    """
    if not DASH_PATH.exists():
        return None
    backup = DASH_PATH.with_name(
        "dashboards.json.before-layout-" + datetime.now().strftime("%Y%m%d-%H%M%S") + ".bak")
    if backup.exists():
        return backup
    try:
        backup.write_text(DASH_PATH.read_text("utf-8"), "utf-8")
    except OSError:
        return None
    return backup


def _sanitize(dash):
    panels = []
    for p in dash.get("panels") or []:
        if p.get("id") not in BY_ID:
            continue
        b = BY_ID[p["id"]]
        entry = {
            "uid": p.get("uid") or uuid.uuid4().hex[:8], "id": p["id"],
            "title": str(p.get("title") or b["title"])[:40],
            "w": max(MIN_PANEL_W, min(MAX_PANEL_W, int(p.get("w") or b["w"]))),
            "h": max(MIN_PANEL_H, min(MAX_PANEL_H, int(p.get("h") or b["h"]))),
            "params": clean_params(p["id"], p.get("params"))[0],
        }
        # 只有前端明确给了坐标才沿用；缺失时让 _solve_layout 按顺序排。
        if p.get("x") is not None and p.get("y") is not None:
            entry["x"], entry["y"] = p["x"], p["y"]
        panels.append(entry)
    _solve_layout(panels)
    return {"id": str(dash.get("id") or "d" + uuid.uuid4().hex[:6])[:40],
            "title": str(dash.get("title") or "未命名看板")[:40],
            "prompt": dash.get("prompt"), "builtin": bool(dash.get("builtin")),
            "panels": panels}


# ------------------------------------------------------------------ AI 生成看板

SYS_BUILDER = (
    "你是看板编排器。用户用一句话描述想看的 A 股短线看板，你从面板目录中挑选面板并排布。"
    "只能使用目录里的 id；可以改 title；params 只能用目录中列出的参数名。"
    "每个面板带 trust 字段（已验证/回测/描述/原始/规则/账户/AI）：描述、原始数据类面板"
    "只用于说明状态，标题和布局里不得把它们写成预测或买入建议。"
    "布局是 24 列栅格，w 为宽度(4-24)，h 为高度(3-20，单位约 48px：38px 行高 + 10px 间距)，"
    "按阅读顺序输出，同一行的 w 之和尽量为 24。不需要输出坐标，位置由阅读顺序决定。"
    "最重要的结论放最上面。一般 4-9 个面板。"
    "只输出 JSON：{\"title\":\"看板名\",\"panels\":[{\"id\":..,\"title\":..,\"w\":..,"
    "\"h\":..,\"params\":{..}}]}")


def build_from_prompt(prompt, conn=None, date=None):
    """一句话 -> 看板。有 LLM 用 LLM，没有或失败用关键词规则。"""
    if not isinstance(prompt, str) or not prompt.strip():
        raise ValueError("请描述需要的面板内容")
    prompt = prompt.strip()[:2000]
    catalog = [{"id": p["id"], "title": p["title"], "desc": p["desc"],
                "params": p.get("params") or {}, "default_w": p["w"],
                "default_h": p["h"], "trust": p["trust_short"]} for p in CATALOG]
    ctx = ""
    if conn is not None:
        try:
            ls = [m["name"] for m in q_mainlines(conn, date or latest_date(conn),
                                                 {})["lines"][:10]]
            ctx = f"\n今日主线关键词（可用于 keyword/keywords 参数）：{ls}"
        except Exception:
            pass
    spec, how, build_error = None, "rule", None
    if llm.available():
        txt = llm.chat(SYS_BUILDER, "面板目录：\n" + json.dumps(
            catalog, ensure_ascii=False) + ctx + f"\n\n用户需求：{prompt}",
            max_tokens=1500, timeout=60)
        spec = llm.extract_json(txt)
        try:
            valid = _sanitize(spec) if isinstance(spec, dict) else None
        except (AttributeError, TypeError, ValueError):
            valid = None
        if valid and valid["panels"]:
            spec, how = valid, "ai"
        else:
            spec = None
            build_error = ((llm.last_failure or {}).get("message") or llm.last_error
                           or "AI 未返回有效面板，已按关键词编排")
    if not spec:
        spec = _rule_build(prompt)
    spec["prompt"] = prompt
    spec["id"] = "d" + uuid.uuid4().hex[:6]
    out = _sanitize(spec)
    out["how"] = how
    out["error"] = build_error if how == "rule" else None
    return out


# 具体数据面板的规则：命中这些时**不再**追加"明天/买什么"那一类通用建议面板。
# 原因：一句"百万大单买入"同时命中"买"，于是会顺带塞进今日结论、明日关注、AI 计划
# 三块，把用户真正要的那块挤到后面——一句话生成的意义就是不塞无关面板。
_SPECIFIC_RULES = [
    # 逐笔/单笔要排在"大单"前面：说"逐笔大单成交"时，用户要的是分时成交明细，
    # 不是聚合净额榜，所以下面那条大单规则用 exclude 把自己排除掉。
    (r"逐笔|单笔|主动买|大单成交|盘口成交", ["tick_watch"]),
    # 量能：用户口语里"交易量"和"成交量"都会说，两个都要收。
    # 这条一开始漏了"交易量"，导致"今日交易量""A 股市场交易量"匹配不到任何面板，
    # 只能回落到默认那四块，看起来就像"方向不对"。
    (r"成交额|成交量|交易量|量能|放量|量比|活跃度|活跃|市值排行",
     ["volume_rank"]),
    (r"大单|超大单|百万|净买入|大额买入|资金流入|主力买入|资金异动|主力资金",
     ["big_order", "flow_center"], r"逐笔|单笔"),
    (r"持仓|账户|余额|补仓|加仓|减仓|卖出|继续持有",
     ["portfolio_account", "portfolio_analysis"]),
    (r"主线|题材|板块|催化|原因", ["mainlines", "events", "line_trend", "blocks"]),
    (r"龙头|连板|高度|天梯|梯队", ["ladder", "height_trend"]),
    (r"情绪|周期|温度|赚钱效应|亏钱", ["market_stats", "emotion_trend", "height_trend"]),
    (r"实时|盘中|快讯|新闻|消息", ["market_stats", "news_impact", "news", "limitup"]),
    (r"炸板|分歧|风险", ["broken", "market_stats"]),
    (r"外围|美股|港股|隔夜", ["global", "news"]),
    (r"席位|龙虎榜|游资|大神", ["seats"]),
]

# 通用建议面板：只有在没有命中任何具体诉求时才用。
_GENERIC_RULES = [
    (r"明天|明日|推荐|关注|机会|计划|买|怎么看|策略|操作",
     ["verdict", "outlook", "ai_brief"]),
]

_RULES = _SPECIFIC_RULES + _GENERIC_RULES


def _rule_build(prompt):
    ids = []
    for rule in _SPECIFIC_RULES:
        pat, ps = rule[0], rule[1]
        # 第三条元素是"排除词"：命中它说明这条规则不是用户想要的（例如说了"逐笔"
        # 就不该再给聚合净额榜）。
        if len(rule) > 2 and re.search(rule[2], prompt):
            continue
        if re.search(pat, prompt):
            ids += [x for x in ps if x not in ids]
    if not ids:
        # 没有具体诉求（例如"明天怎么做"）才给通用建议面板。
        for pat, ps in _GENERIC_RULES:
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
