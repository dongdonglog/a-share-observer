#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""全局配置：路径、数据源、阈值、权重。

所有阈值集中在这里，方便按自己的理解校准。
"""

from pathlib import Path

# ---------------------------------------------------------------- 路径
ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"
DB_PATH = DATA_DIR / "chaogu.db"
REPORT_DIR = ROOT / "reports"
CACHE_DIR = DATA_DIR / "cache"

for _d in (DATA_DIR, REPORT_DIR, CACHE_DIR):
    _d.mkdir(parents=True, exist_ok=True)

# ---------------------------------------------------------------- 网络
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")
HTTP_TIMEOUT = 20
HTTP_RETRY = 3
HTTP_SLEEP = 0.12          # 请求间隔，避免被限流

# ---------------------------------------------------------------- 数据源
EM_UT = "7eea3edcaed734bea9cbfc24409ed989"
THS_FIELDS = ("199112,10,9001,330323,330324,330325,9002,330329,133971,133970,"
              "1968584,3475914,513070,513071,3541450,3642053,3541451")

# 外围市场标的（东财 secid）
GLOBAL_INDEXES = [
    ("100.NDX", "纳斯达克"),
    ("100.DJIA", "道琼斯"),
    ("100.SPX", "标普500"),
    ("100.HSI", "恒生指数"),
    ("100.HSCEI", "国企指数"),
    ("100.N225", "日经225"),
    ("100.UDI", "美元指数"),
    ("101.GC00Y", "COMEX黄金"),
    ("101.CL00Y", "NYMEX原油"),
]

# A股主要指数
A_INDEXES = [
    ("1.000001", "上证指数"),
    ("0.399001", "深证成指"),
    ("0.399006", "创业板指"),
    ("1.000688", "科创50"),
]

# ---------------------------------------------------------------- 情绪周期阈值
# 冷启动（历史样本不足）时使用的绝对阈值；样本够了改用滚动分位数
FALLBACK = {
    "zt_count":     {"lo": 15, "hi": 80},    # 涨停家数
    "dt_count":     {"lo": 0,  "hi": 15},    # 跌停家数（反向）
    "seal_rate":    {"lo": 0.55, "hi": 0.90},  # 封板率
    "max_height":   {"lo": 2,  "hi": 7},     # 最高连板
    "lianban_count": {"lo": 2, "hi": 25},    # 连板家数
    "premium":      {"lo": -3.0, "hi": 4.0},  # 昨涨停今日溢价(%)
    "promote_rate": {"lo": 0.05, "hi": 0.35},  # 晋级率
}

# 情绪温度分权重（合计 1.0）
WEIGHTS = {
    "zt_count": 0.16,
    "seal_rate": 0.14,
    "premium": 0.24,        # 赚钱效应：权重最高
    "promote_rate": 0.16,
    "max_height": 0.14,
    "lianban_count": 0.10,
    "dt_count": 0.06,       # 反向指标
}

# 周期阶段判定阈值
STAGE_BANDS = [
    (0, 22, "冰点"),
    (22, 45, "修复"),
    (45, 70, "发酵"),
    (70, 101, "高潮"),
]

# 退潮/分歧的覆盖规则（优先级高于分数）
EBB_PREMIUM = -1.5          # 溢价低于此值 -> 退潮
DIVERGE_SEAL_RATE = 0.62    # 封板率低于此值且高度>=4 -> 分歧

# 封板率告警线（仅用于报告"需要留意"提示，不改判情绪阶段）。
# 早期版本在 report.py 里硬编码 0.7，与上面的 0.62 形成同指标双口径；
# 现统一由 config 管理，避免两处漂移。
SEAL_RATE_WARN = 0.70       # 封板率低于此值 -> 报告提示封板质量偏差

# 滚动分位窗口（交易日）
ROLLING_WINDOW = 60
MIN_HISTORY_FOR_QUANTILE = 15

# ---------------------------------------------------------------- 题材
# 新题材判定
NEW_THEME_MIN_ZT = 2        # 首次出现至少要有这么多涨停
EMERGING_ZT_JUMP = 3        # 家数从低位跳到这个数视为异动启动
THEME_LOOKBACK = 5          # 统计题材连续活跃的回看天数

# 延续性评分权重
THEME_SCORE_WEIGHTS = {
    "breadth": 0.22,        # 涨停家数
    "trend": 0.20,          # 家数趋势（放大/衰减）
    "leader": 0.22,         # 有无龙头且高度
    "echelon": 0.16,        # 有无梯队（多高度层）
    "seal": 0.12,           # 封板质量
    "sector": 0.08,         # 板块指数表现
}

# ---------------------------------------------------------------- 预测
FORECAST_NEIGHBORS = 8      # 相似日取前 N 个
FORECAST_HORIZON = 3        # 向后看几天
FORECAST_FEATURES = [
    "zt_count", "seal_rate", "max_height", "lianban_count",
    "prev_zt_premium", "dt_count", "up_down_ratio",
]

# ---------------------------------------------------------------- 杂项
EXCLUDE_ST = True           # 统计时是否剔除 ST
EXCLUDE_NEW = True          # 是否剔除新股/次新（is_new）
MARKET_LABEL = {0: "深", 1: "沪"}

# ---------------------------------------------------------------- 龙虎榜/席位
# 席位"3日胜率"（东财 RISE_PROBABILITY_3DAY）阈值。
# 实测依据：2026-01~09 共 46,923 条（去重、去聚合、次日收益非空）
# 席位记录，该字段与真实次日收益相关
# +0.175，且分组单调（<35% -> D1 -1.90%；>=60% -> D1 +3.49%）。
# 50 是"收益转正"的分界，60 是"高胜率"档。
SEAT_MIN_REP_PROB = 50.0
SEAT_STRONG_REP_PROB = 60.0
# 席位排名纳入门槛与评估窗口
SEAT_MIN_TRADES = 20
SEAT_EVAL_DAYS = 180

# 交易成本模型（用于把毛收益换算成净收益）
# 显性成本：印花税 0.05%（仅卖出）+ 佣金约 0.025%×2 + 过户费 0.001%×2
COST_EXPLICIT = 0.10        # 合计约 0.10%（百分比）
# 滑点：短线标的波动大，双边各按 0.15% 保守估计
COST_SLIPPAGE = 0.15        # 单边（百分比）

# 席位胜率分档（基于实测净收益，见 README 第五节）
#   >=50  D1净 +0.33% / D5净 +1.61%   勉强可用
#   >=60  D1净 +1.45% / D5净 +5.11%   可用（报告默认档，信号较多）
#   >=70  D1净 +2.30% / D5净 +8.04%   最强档（信号较少，逐月 9/9 为正）
SEAT_ELITE_REP_PROB = 70.0
