#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""SQLite 存储层。"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager

from . import config

SCHEMA = """
PRAGMA journal_mode=WAL;

-- 每日市场快照（情绪指标的原料）
CREATE TABLE IF NOT EXISTS daily_market (
    trade_date      TEXT PRIMARY KEY,
    zt_count        INTEGER,
    zb_count        INTEGER,
    dt_count        INTEGER,
    max_height      INTEGER,
    lianban_count   INTEGER,
    up_count        INTEGER,
    down_count      INTEGER,
    flat_count      INTEGER,
    amount          REAL,      -- 两市成交额（亿元）
    sh_close        REAL,
    sh_pct          REAL,
    cyb_close       REAL,
    cyb_pct         REAL,
    source          TEXT,
    collected_at    TEXT
);

-- 涨停股明细
CREATE TABLE IF NOT EXISTS limit_up (
    trade_date      TEXT NOT NULL,
    code            TEXT NOT NULL,
    name            TEXT,
    market          INTEGER,
    price           REAL,
    pct             REAL,
    turnover        REAL,      -- 换手率 %
    float_mv        REAL,
    total_mv        REAL,
    height          INTEGER,   -- 板数（N天M板中的M）
    days            INTEGER,   -- 统计天数
    height_text     TEXT,
    lu_type         TEXT,      -- 换手板/一字板/T字板
    change_tag      TEXT,      -- FIRST_LIMIT / LIMIT_BACK
    open_num        INTEGER,   -- 开板次数
    seal_amount     REAL,      -- 封单额（元）
    first_seal      TEXT,      -- HH:MM:SS
    last_seal       TEXT,
    industry        TEXT,      -- 东财行业
    reason          TEXT,      -- 同花顺题材归因（+ 分隔）
    is_new          INTEGER,
    source          TEXT,
    PRIMARY KEY (trade_date, code)
);
CREATE INDEX IF NOT EXISTS idx_lu_date ON limit_up(trade_date);

-- 炸板股
CREATE TABLE IF NOT EXISTS broken_board (
    trade_date      TEXT NOT NULL,
    code            TEXT NOT NULL,
    name            TEXT,
    market          INTEGER,
    price           REAL,
    pct             REAL,
    turnover        REAL,
    zt_price        REAL,      -- 涨停价
    open_num        INTEGER,
    amplitude       REAL,
    industry        TEXT,
    reason          TEXT,
    first_seal      TEXT,
    source          TEXT,
    PRIMARY KEY (trade_date, code)
);

-- 跌停股
CREATE TABLE IF NOT EXISTS limit_down (
    trade_date      TEXT NOT NULL,
    code            TEXT NOT NULL,
    name            TEXT,
    market          INTEGER,
    price           REAL,
    pct             REAL,
    turnover        REAL,
    seal_amount     REAL,
    industry        TEXT,
    source          TEXT,
    PRIMARY KEY (trade_date, code)
);

-- 股票名录（历史价格回填用）
CREATE TABLE IF NOT EXISTS stock_basic (
    code            TEXT PRIMARY KEY,
    name            TEXT,
    market          INTEGER,      -- 1=沪 0=深/北
    price_synced    INTEGER DEFAULT 0
);

-- 每日全市场行情（算昨日涨停溢价/晋级率用）
CREATE TABLE IF NOT EXISTS quote_daily (
    trade_date      TEXT NOT NULL,
    code            TEXT NOT NULL,
    name            TEXT,
    pct             REAL,
    turnover        REAL,
    amount          REAL,
    close           REAL,
    PRIMARY KEY (trade_date, code)
);

-- 题材板块（同花顺 block_top 板块层）
CREATE TABLE IF NOT EXISTS theme_block (
    trade_date      TEXT NOT NULL,
    code            TEXT NOT NULL,
    name            TEXT,
    change_pct      REAL,      -- 板块涨跌幅（小数，0.05 = 5%）
    limit_up_num    INTEGER,   -- 板块内涨停家数
    continuous_num  INTEGER,   -- 板块内连板家数
    high            TEXT,      -- 最高板文本
    days            INTEGER,   -- 活跃天数
    PRIMARY KEY (trade_date, code)
);

-- 题材成分股（带题材归因与催化原因）
CREATE TABLE IF NOT EXISTS theme_stock (
    trade_date      TEXT NOT NULL,
    tag             TEXT NOT NULL,
    code            TEXT NOT NULL,
    name            TEXT,
    pct             REAL,
    continue_num    INTEGER,   -- 该股连板数
    height_text     TEXT,
    change_tag      TEXT,
    reason          TEXT,      -- 题材归因，+ 分隔
    reason_info     TEXT,      -- 催化原因原文
    first_seal      TEXT,
    is_st           INTEGER,
    is_new          INTEGER,
    PRIMARY KEY (trade_date, tag, code)
);
CREATE INDEX IF NOT EXISTS idx_ts_date ON theme_stock(trade_date);
CREATE INDEX IF NOT EXISTS idx_ts_code ON theme_stock(code);

-- 题材标签日聚合
CREATE TABLE IF NOT EXISTS theme_daily (
    trade_date      TEXT NOT NULL,
    tag             TEXT NOT NULL,
    zt_count        INTEGER,
    max_height      INTEGER,
    leader_code     TEXT,
    leader_name     TEXT,
    leader_height   INTEGER,
    first_seal      TEXT,
    echelon         TEXT,      -- JSON: {"1":n,"2":n,...}
    avg_turnover    REAL,
    seal_quality    REAL,
    PRIMARY KEY (trade_date, tag)
);

-- 板块行情（东财概念/行业）
CREATE TABLE IF NOT EXISTS sector_daily (
    trade_date      TEXT NOT NULL,
    code            TEXT NOT NULL,
    name            TEXT,
    kind            TEXT,      -- concept / industry
    pct             REAL,
    turnover        REAL,
    amount          REAL,
    up_count        INTEGER,
    down_count      INTEGER,
    leader_name     TEXT,
    leader_code     TEXT,
    PRIMARY KEY (trade_date, code)
);

-- 新闻
CREATE TABLE IF NOT EXISTS news (
    id              TEXT PRIMARY KEY,
    ts              TEXT,
    title           TEXT,
    content         TEXT,
    source          TEXT,
    url             TEXT,
    tags            TEXT,
    collected_at    TEXT
);

-- 外围市场
CREATE TABLE IF NOT EXISTS global_market (
    trade_date      TEXT NOT NULL,
    code            TEXT NOT NULL,
    name            TEXT,
    price           REAL,
    pct             REAL,
    PRIMARY KEY (trade_date, code)
);

-- 计算后的情绪指标
CREATE TABLE IF NOT EXISTS emotion_daily (
    trade_date          TEXT PRIMARY KEY,
    seal_rate           REAL,
    prev_zt_premium     REAL,
    prev_lb_premium     REAL,
    promote_rate        REAL,
    up_down_ratio       REAL,
    temperature         REAL,
    stage               TEXT,
    stage_reason        TEXT,
    prev_zt_n           INTEGER,
    prev_lb_n           INTEGER,
    computed_at         TEXT
);

-- 预测记录（用于事后回验，这很重要）
CREATE TABLE IF NOT EXISTS forecast_log (
    trade_date      TEXT PRIMARY KEY,   -- 做出预测的日期
    target_date     TEXT,               -- 预测的目标日
    bias            TEXT,               -- 偏暖/中性/偏冷
    confidence      REAL,
    pred_zt_count   REAL,
    pred_premium    REAL,
    basis           TEXT,               -- JSON 依据
    created_at      TEXT,
    actual_zt_count REAL,               -- 事后回填
    actual_premium  REAL,
    hit             INTEGER
);

-- 采集日志
-- 看板信号留痕：每次看板给出"跟随候选"时把当时的判断记下来，
-- 之后用真实行情回填结果。这是"提高胜率"唯一的闭环——
-- 没有留痕就只能靠记忆，而记忆会自动过滤掉亏损的那些单子。
--
-- 为什么单独建表而不复用 seat_trade：seat_trade 是券商披露的
-- **事后**数据；本表记的是**当时**看板看到了什么，
-- 两者的时间戳含义不同，混在一起无法审计。
CREATE TABLE IF NOT EXISTS signal_log (
    signal_date TEXT,     -- 看板给出信号的日子（= 可执行入场日的前一交易日）
    code        TEXT,
    name        TEXT,
    seat        TEXT,
    rep_prob    REAL,     -- 当时平台给出的席位3日胜率
    activity    INTEGER,  -- 当时该席位 60 日活跃度
    reason      TEXT,     -- 看板给出的理由摘要
    logged_at   TEXT,
    -- 回填字段（NULL = 未来尚未发生，不是缺失）
    entry_close REAL,     -- 信号日收盘（口径基准，非可成交价）
    d1_ret      REAL,     -- 次日收盘涨幅%
    d5_ret      REAL,
    d10_ret     REAL,
    d1_locked   INTEGER,  -- 次日是否涨停（1=买不进）
    filled_at   TEXT,
    PRIMARY KEY (signal_date, code)
);

CREATE TABLE IF NOT EXISTS collect_log (
    trade_date TEXT,
    step       TEXT,
    status     TEXT,
    rows       INTEGER,
    message    TEXT,
    ts         TEXT,
    PRIMARY KEY (trade_date, step, ts)
);

-- ---------------------------------------------------------------- 龙虎榜
-- 龙虎榜个股明细。D*_adj 是"上榜后 N 个交易日涨幅"（东财自带，
-- 已复权）。最近日期的 D 值为 NULL——未来尚未发生，不是缺失。
CREATE TABLE IF NOT EXISTS billboard (
    trade_date  TEXT,
    code        TEXT,
    name        TEXT,
    pct         REAL,      -- 上榜日涨跌幅
    turnover    REAL,      -- 换手率
    reason      TEXT,      -- 上榜原因原文
    change_type TEXT,      -- 东财原因编码
    buy_amt     REAL,      -- 龙虎榜买入额（元）
    sell_amt    REAL,
    net_amt     REAL,
    explain     TEXT,      -- 东财机构解读
    d1_adj      REAL,      -- 上榜后1日涨幅(%)
    d2_adj      REAL,
    d5_adj      REAL,
    d10_adj     REAL,
    d20_adj     REAL,
    d30_adj     REAL,
    source      TEXT,
    PRIMARY KEY (trade_date, code, reason)
);

-- 席位-个股买入/卖出明细。rep_prob 是东财给出的该席位3日上涨概率，
-- 经本地 46,923 条（去重、去聚合）样本验证：与真实前向收益
-- 相关 +0.175，且样本外持续。
CREATE TABLE IF NOT EXISTS seat_trade (
    trade_date  TEXT,
    seat        TEXT,
    side        TEXT,      -- buy / sell
    code        TEXT,
    name        TEXT,
    pct         REAL,      -- 上榜日涨跌幅
    buy_amt     REAL,
    sell_amt    REAL,
    net_amt     REAL,
    rep_prob    REAL,      -- RISE_PROBABILITY_3DAY
    rep_n       INTEGER,   -- TOTAL_BUYER_SALESTIMES_3DAY
    source      TEXT,
    PRIMARY KEY (trade_date, seat, side, code)
);

-- 席位每日汇总（活跃席位榜）。BUY_STOCK 与 name 空格分隔一一对应。
CREATE TABLE IF NOT EXISTS seat_daily (
    trade_date  TEXT,
    seat        TEXT,
    appeared    INTEGER,   -- 上榜次数
    buyer_num   INTEGER,   -- 买方席位出现次数
    seller_num  INTEGER,
    buy_amt     REAL,
    sell_amt    REAL,
    net_amt     REAL,
    buy_stocks  TEXT,      -- "000001 000002"
    buy_names   TEXT,
    org_name    TEXT,
    source      TEXT,
    PRIMARY KEY (trade_date, seat)
);

-- 去重视图：billboard 主键含 reason，同一股票同日可能有多个上榜原因
-- （实测 13.4% 的 (trade_date,code) 有多行）。直接 JOIN billboard 会让
-- 这些股票被重复计数，实测把可执行样本的 D1 从 +1.880% 拉偏到 +1.768%。
-- 凡是"按股票事件"做的统计都应 JOIN 这个视图，而不是 billboard 原表。
CREATE VIEW IF NOT EXISTS billboard_uniq AS
SELECT trade_date, code,
       MIN(name)     AS name,
       MIN(pct)      AS pct,
       MIN(turnover) AS turnover,
       MIN(change_type) AS change_type,
       SUM(buy_amt)  AS buy_amt,
       SUM(sell_amt) AS sell_amt,
       SUM(net_amt)  AS net_amt,
       MIN(explain)  AS explain,
       -- 前向收益同股同日必然相同，取 MIN 即可（用 SUM 会翻倍）
       MIN(d1_adj)   AS d1_adj,
       MIN(d2_adj)   AS d2_adj,
       MIN(d5_adj)   AS d5_adj,
       MIN(d10_adj)  AS d10_adj,
       MIN(d20_adj)  AS d20_adj,
       MIN(d30_adj)  AS d30_adj,
       GROUP_CONCAT(DISTINCT reason) AS reasons,
       COUNT(*)      AS reason_n
FROM billboard
GROUP BY trade_date, code;

-- 席位买卖去重视图：seat_trade 主键含 side，同一席位同日同股可能
-- 同时出现在买卖两侧（虽罕见），按买方事件统计时需保证唯一。
CREATE VIEW IF NOT EXISTS seat_buy_uniq AS
SELECT trade_date, seat, code,
       MIN(name)     AS name,
       MIN(side)     AS side,
       MIN(pct)      AS pct,
       SUM(buy_amt)  AS buy_amt,
       SUM(sell_amt) AS sell_amt,
       SUM(net_amt)  AS net_amt,
       MIN(rep_prob) AS rep_prob,
       MIN(rep_n)    AS rep_n
FROM seat_trade
WHERE side = 'buy'
GROUP BY trade_date, seat, code;
"""


def connect(path=None) -> sqlite3.Connection:
    conn = sqlite3.connect(str(path or config.DB_PATH), timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


# 增量迁移：CREATE TABLE IF NOT EXISTS 不会给已存在的表加列
MIGRATIONS = [
    ("news", "tags", "TEXT"),
    ("stock_basic", "price_synced", "INTEGER DEFAULT 0"),
    ("stock_basic", "price_source", "TEXT"),
]


def _migrate(conn):
    for table, col, decl in MIGRATIONS:
        rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
        if not rows:
            continue                      # 表还不存在，交给 SCHEMA 创建
        if col not in {r[1] for r in rows}:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {decl}")


def init(path=None):
    conn = connect(path)
    with conn:
        conn.executescript(SCHEMA)
        _migrate(conn)
    return conn


@contextmanager
def session(path=None):
    conn = init(path)
    try:
        yield conn
    finally:
        conn.close()


def upsert_quotes(conn, rows):
    """写入 quote_daily，**保留已有的非空字段**。

    历史价格回填用腾讯/新浪，它们不返回成交额与换手率；
    如果直接覆盖，会把实时采集到的成交额清成 NULL
    （实测导致"两市成交额"从 8640 亿掉到 6519 亿）。
    """
    rows = [r for r in rows if r]
    if not rows:
        return 0
    sql = """
    INSERT INTO quote_daily
      (trade_date, code, name, pct, turnover, amount, close)
    VALUES (?,?,?,?,?,?,?)
    ON CONFLICT(trade_date, code) DO UPDATE SET
      name     = COALESCE(excluded.name,     quote_daily.name),
      pct      = COALESCE(excluded.pct,      quote_daily.pct),
      turnover = COALESCE(excluded.turnover, quote_daily.turnover),
      amount   = COALESCE(excluded.amount,   quote_daily.amount),
      close    = COALESCE(excluded.close,    quote_daily.close)
    """
    with conn:
        conn.executemany(sql, [
            (r.get("trade_date"), r.get("code"), r.get("name"), r.get("pct"),
             r.get("turnover"), r.get("amount"), r.get("close"))
            for r in rows])
    return len(rows)


def upsert(conn, table: str, rows, keys):
    """按 keys 做主键冲突更新。rows 为 dict 列表。"""
    rows = [r for r in rows if r]
    if not rows:
        return 0
    cols = list(rows[0].keys())
    placeholders = ",".join("?" for _ in cols)
    updates = ",".join(f"{c}=excluded.{c}" for c in cols if c not in keys)
    sql = (f"INSERT INTO {table} ({','.join(cols)}) VALUES ({placeholders}) "
           f"ON CONFLICT({','.join(keys)}) DO UPDATE SET {updates}")
    with conn:
        conn.executemany(sql, [tuple(r.get(c) for c in cols) for r in rows])
    return len(rows)


def log(conn, trade_date, step, status, rows=0, message="", ts=None):
    from datetime import datetime
    with conn:
        conn.execute(
            "INSERT OR REPLACE INTO collect_log "
            "(trade_date,step,status,rows,message,ts) VALUES (?,?,?,?,?,?)",
            (trade_date, step, status, rows, message,
             ts or datetime.now().isoformat(timespec="seconds")))


def dates(conn, table="daily_market", limit=250):
    cur = conn.execute(
        f"SELECT DISTINCT trade_date FROM {table} "
        f"ORDER BY trade_date DESC LIMIT ?", (limit,))
    return [r[0] for r in cur.fetchall()]


def latest_date(conn, table="daily_market"):
    d = dates(conn, table, 1)
    return d[0] if d else None
