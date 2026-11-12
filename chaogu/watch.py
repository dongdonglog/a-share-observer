#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Local watchlist, configurable alerts, and popularity-rank history."""

from __future__ import annotations

import json
import math
import re
import sqlite3
import threading
import time
import uuid
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from . import config

PATH = config.DATA_DIR / "watch.db"
TZ = ZoneInfo("Asia/Shanghai")
_DB_LOCK = threading.Lock()

SCHEMA = """
PRAGMA journal_mode=WAL;
CREATE TABLE IF NOT EXISTS watch_stock (
 code TEXT PRIMARY KEY, name TEXT NOT NULL DEFAULT '', added_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS watch_rule (
 id TEXT PRIMARY KEY, name TEXT NOT NULL, kind TEXT NOT NULL, enabled INTEGER NOT NULL,
 config TEXT NOT NULL, revision INTEGER NOT NULL DEFAULT 1,
 created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS rule_state (
 rule_id TEXT NOT NULL, code TEXT NOT NULL, revision INTEGER NOT NULL,
 state TEXT NOT NULL, updated_at TEXT NOT NULL, PRIMARY KEY(rule_id, code)
);
CREATE TABLE IF NOT EXISTS price_reference (
 code TEXT PRIMARY KEY, ref_date TEXT, ma20 REAL, avg_volume20 REAL,
 fetched_at TEXT NOT NULL, error TEXT
);
CREATE TABLE IF NOT EXISTS rank_current (
 provider TEXT NOT NULL, code TEXT NOT NULL, name TEXT NOT NULL, rank INTEGER NOT NULL,
 source_at TEXT NOT NULL, PRIMARY KEY(provider, code)
);
CREATE TABLE IF NOT EXISTS rank_daily (
 provider TEXT NOT NULL, trade_date TEXT NOT NULL, code TEXT NOT NULL,
 name TEXT NOT NULL, rank INTEGER NOT NULL, source_at TEXT NOT NULL,
 PRIMARY KEY(provider, trade_date, code)
);
CREATE INDEX IF NOT EXISTS idx_rank_daily_code ON rank_daily(provider, code, trade_date);
CREATE TABLE IF NOT EXISTS source_status (
 provider TEXT PRIMARY KEY, state TEXT NOT NULL, checked_at TEXT,
 success_at TEXT, rows INTEGER NOT NULL DEFAULT 0, coverage INTEGER NOT NULL DEFAULT 0,
 error TEXT
);
CREATE TABLE IF NOT EXISTS alert_event (
 id TEXT PRIMARY KEY, rule_id TEXT, rule_name TEXT NOT NULL, rule_snapshot TEXT NOT NULL,
 code TEXT NOT NULL, name TEXT NOT NULL, kind TEXT NOT NULL,
 triggered_at TEXT NOT NULL, source_at TEXT, providers TEXT NOT NULL,
 observed TEXT NOT NULL, read_at TEXT, dedupe_key TEXT NOT NULL UNIQUE
);
CREATE INDEX IF NOT EXISTS idx_alert_event_time ON alert_event(triggered_at DESC);
CREATE TABLE IF NOT EXISTS flow_current (
 provider TEXT NOT NULL, code TEXT NOT NULL, name TEXT NOT NULL,
 price REAL, pct REAL, amount REAL, main_net REAL, main_ratio REAL,
 super_net REAL, large_net REAL, medium_net REAL, small_net REAL,
 source_at TEXT NOT NULL, PRIMARY KEY(provider, code)
);
CREATE TABLE IF NOT EXISTS flow_daily (
 provider TEXT NOT NULL, trade_date TEXT NOT NULL, code TEXT NOT NULL,
 name TEXT NOT NULL, pct REAL, amount REAL, main_net REAL, main_ratio REAL,
 source_at TEXT NOT NULL, PRIMARY KEY(provider, trade_date, code)
);
CREATE TABLE IF NOT EXISTS tick_big (
 provider TEXT NOT NULL, trade_date TEXT NOT NULL, code TEXT NOT NULL, name TEXT NOT NULL,
 ts TEXT NOT NULL, price REAL, lots INTEGER, amount REAL NOT NULL, side TEXT NOT NULL,
 source_at TEXT NOT NULL,
 PRIMARY KEY(provider, trade_date, code, ts, price, lots, side)
);
CREATE INDEX IF NOT EXISTS idx_tick_big_amount ON tick_big(trade_date, side, amount DESC);
"""

PRICE_FIELDS = {"price", "pct", "ma20", "volume20_ratio"}
PROVIDERS = {"eastmoney", "ths"}
FLOW_PROVIDER = "eastmoney_flow"
DEFAULT_FLOW_DIRECTION = "both"
DEFAULT_FLOW_METRIC = "main_net"
DEFAULT_FLOW_MIN_AMOUNT = 1e8   # 1 亿元
DEFAULT_FLOW_MIN_RATIO = 3.0    # 3%
# 大单成交（逐笔口径的公开替代）：交易所免费行情是 3 秒快照，所以判定的是
# "这 3 秒里主动买成交了 N 万元"，不是"某一笔委托 N 万元"。见 sources/ticks.py。
TICK_PROVIDER = "eastmoney_ticks"
TICK_MIN_AMOUNT = 1e6           # 100 万元
DEFAULT_TICK_SIDE = "buy"       # buy / both
DEFAULT_TICK_SCOPE = "both"     # watchlist / position / both
# 每次只看最近 5 分钟的成交：首次启用规则时不会把一整天的历史大单刷成提醒。
TICK_LOOKBACK_SECONDS = 300
TICK_FETCH_ROWS = 300           # 单只单次拉取的快照条数
# 每只股票一次请求，池子越大越容易被东财限流（实测连续请求会返回 data:null）。
# 30 只约 10-20 秒跑完一轮，超过这个量级应该先精简自选股。
TICK_MAX_CODES = 30
TICK_INTERVAL = 60              # 盘中轮询间隔（秒）
# 100 万对高价股是很低的门槛：30 只最活跃的票，一个 5 分钟窗口就能落下近千条快照
# （实测一轮 984 行），一天十万量级。这些数据里只有金额最大的那几十条会被展示，
# 所以按天保留一段时间就够回看，没必要长期堆着。
TICK_RETENTION_DAYS = 20
CLOSE_CAPTURE_START = "15:05"
CLOSE_CAPTURE_END = "16:05"
# 东方财富与同花顺的公开人气榜都只返回前 100 名（东财第 2 页为空、同花顺单次请求不分页）。
# 因此排名类规则的可观测范围就是 1..100：既不能用大于 100 的门槛，也不能把 100 名以外的
# 名次当作参考。"新晋"正是为这个深度上限设计的口径——昨日不在前 100、今日进入前 100。
RANK_LIST_DEPTH = 100
DEFAULT_CURRENT_MAX_RANK = RANK_LIST_DEPTH
DEFAULT_JUMP_FROM_RANK = RANK_LIST_DEPTH
DEFAULT_JUMP_IMPROVEMENT = 30
# "新晋"只在证券进入这个名次以内时才提醒。默认远小于榜单深度，避免日内换手把
# "昨日榜外→今日榜内"刷成上百条（实测一天可触发 100+ 条）。
DEFAULT_ENTRY_MAX_RANK = 30
# 收盘快照超过这些天数就不再作为"昨日"基准，避免用陈旧排名判断跃升。
SNAPSHOT_MAX_AGE_DAYS = 8
STATE = {"enabled": False, "started_at": None, "last": {}, "errors": {}}
_THREAD_LOCK = threading.Lock()


def now():
    return datetime.now(TZ)


def connect(path=None):
    target = Path(path or PATH)
    target.parent.mkdir(parents=True, exist_ok=True)
    existed = target.exists() and target.stat().st_size > 0
    with _DB_LOCK:
        conn = sqlite3.connect(str(target), timeout=30)
        conn.row_factory = sqlite3.Row
        try:
            conn.executescript(SCHEMA)
            if not existed:
                target.chmod(0o600)
            return conn
        except Exception:
            conn.close()
            raise


def _code(value):
    code = str(value or "").strip()
    if code.startswith(("SH", "SZ", "BJ")) and len(code) == 8:
        code = code[2:]
    if not re.fullmatch(r"\d{6}", code):
        raise ValueError("请输入六位证券代码")
    return code


def add_stock(code, name="", path=None):
    code = _code(code)
    name = str(name or "").strip()[:40]
    at = now().isoformat(timespec="seconds")
    c = connect(path)
    try:
        with c:
            c.execute("INSERT INTO watch_stock(code,name,added_at) VALUES(?,?,?) "
                      "ON CONFLICT(code) DO UPDATE SET name=CASE WHEN excluded.name!='' "
                      "THEN excluded.name ELSE watch_stock.name END", (code, name, at))
    finally:
        c.close()
    return {"code": code, "name": name or code, "added_at": at}


def list_stocks(path=None):
    c = connect(path)
    try:
        return [dict(r) for r in c.execute("SELECT * FROM watch_stock ORDER BY name,code")]
    finally:
        c.close()


def remove_stock(code, path=None):
    code = _code(code)
    c = connect(path)
    try:
        active = c.execute("SELECT 1 FROM watch_rule WHERE kind='price' "
                           "AND json_extract(config,'$.code')=? "
                           "AND COALESCE(json_extract(config,'$.scope'),'watchlist')='watchlist' LIMIT 1",
                           (code,)).fetchone()
        if active:
            raise ValueError("该股票仍被行情规则引用，请先删除规则")
        with c:
            c.execute("DELETE FROM watch_stock WHERE code=?", (code,))
        return {"ok": True}
    finally:
        c.close()


def _number(value, label, *, minimum=None, maximum=None):
    if isinstance(value, bool):
        raise ValueError(f"{label}必须是数字")
    try:
        v = float(value)
    except (TypeError, ValueError):
        raise ValueError(f"{label}必须是数字") from None
    if not math.isfinite(v) or (minimum is not None and v < minimum) or \
            (maximum is not None and v > maximum):
        raise ValueError(f"{label}超出允许范围")
    return v


def _integer(value, label, *, minimum, maximum):
    number = _number(value, label, minimum=minimum, maximum=maximum)
    if not number.is_integer():
        raise ValueError(f"{label}必须是整数")
    return int(number)


def _conditions(raw):
    if not isinstance(raw, list) or not 1 <= len(raw) <= 6:
        raise ValueError("每条行情规则需要 1 到 6 个条件")
    out = []
    for item in raw:
        if not isinstance(item, dict):
            raise ValueError("行情条件格式无效")
        field, op = item.get("field"), item.get("op")
        allowed = {
            "price": {"gte", "lte", "cross_above", "cross_below"},
            "pct": {"gte", "lte", "cross_above", "cross_below"},
            "ma20": {"above", "below", "cross_above", "cross_below"},
            "volume20_ratio": {"gte", "lte"},
        }
        if field not in allowed or op not in allowed[field]:
            raise ValueError("不支持的行情条件")
        row = {"field": field, "op": op}
        if field != "ma20":
            row["value"] = _number(item.get("value"), "条件阈值",
                                   minimum=0 if field in ("price", "volume20_ratio") else None)
        out.append(row)
    return out


def _normalize_rule(data):
    if not isinstance(data, dict):
        raise ValueError("规则格式无效")
    kind = data.get("kind")
    if kind not in ("price", "popularity", "flow", "tick"):
        raise ValueError("规则类型无效")
    name = str(data.get("name") or "").strip()[:40]
    config_data = {}
    if kind == "price":
        code = _code(data.get("code"))
        logic = data.get("logic", "all")
        if logic not in ("all", "any"):
            raise ValueError("条件组合只能选择全部满足或任一满足")
        scope = data.get("scope", "watchlist")
        if scope not in ("watchlist", "position"):
            raise ValueError("请选择自选股或账户持仓")
        config_data = {"code": code, "stock_name": str(data.get("stock_name") or "").strip()[:40],
                       "scope": scope, "logic": logic,
                       "conditions": _conditions(data.get("conditions"))}
        name = name or f"{config_data['stock_name'] or code} 行情提醒"
    elif kind == "popularity":
        providers = data.get("providers", sorted(PROVIDERS))
        if not isinstance(providers, list) or not providers or not set(providers) <= PROVIDERS:
            raise ValueError("至少选择一个有效人气榜来源")
        current_max = _integer(data.get("current_max_rank", DEFAULT_CURRENT_MAX_RANK),
                               "当前排名", minimum=1, maximum=RANK_LIST_DEPTH)
        entry_max = _integer(data.get("entry_max_rank", DEFAULT_ENTRY_MAX_RANK),
                             "新晋目标名次", minimum=1, maximum=RANK_LIST_DEPTH)
        if entry_max > current_max:
            raise ValueError("新晋目标名次不能大于当前排名门槛")
        raw_from = data.get("previous_in_top", DEFAULT_JUMP_FROM_RANK)
        previous_in_top = None if raw_from is None else _integer(
            raw_from, "榜内跃升起点名次", minimum=1, maximum=RANK_LIST_DEPTH)
        enable_new_entry = data.get("enable_new_entry", True)
        if not isinstance(enable_new_entry, bool):
            raise ValueError("enable_new_entry 必须是布尔值")
        require_all = data.get("require_all_providers", True)
        if not isinstance(require_all, bool):
            raise ValueError("require_all_providers 必须是布尔值")
        if previous_in_top is None and not enable_new_entry:
            raise ValueError("至少要启用「新晋前 N」或「榜内跃升」之一")
        if previous_in_top is not None and previous_in_top < current_max:
            raise ValueError("榜内跃升的起点名次不能小于当前排名门槛")
        config_data = {
            "providers": sorted(set(providers)),
            "current_max_rank": current_max,
            "entry_max_rank": entry_max,
            "enable_new_entry": enable_new_entry,
            "require_all_providers": require_all,
            "previous_in_top": previous_in_top,
            "min_improvement": _integer(data.get("min_improvement", DEFAULT_JUMP_IMPROVEMENT),
                                        "名次提升", minimum=1, maximum=999),
        }
        name = name or f"人气榜异动（前 {current_max}）"
    elif kind == "flow":
        direction = data.get("direction", DEFAULT_FLOW_DIRECTION)
        if direction not in ("in", "out", "both"):
            raise ValueError("资金流方向只能是净流入、净流出或二者之一")
        metric = data.get("metric", DEFAULT_FLOW_METRIC)
        if metric not in ("main_net", "main_ratio"):
            raise ValueError("资金流指标无效")
        config_data = {
            "providers": [FLOW_PROVIDER],
            "direction": direction,
            "metric": metric,
            "min_amount": _number(data.get("min_amount", DEFAULT_FLOW_MIN_AMOUNT),
                                  "主力净额阈值（元）", minimum=0),
            "min_ratio": _number(data.get("min_ratio", DEFAULT_FLOW_MIN_RATIO),
                                 "主力净占比阈值（%）", minimum=0, maximum=100),
        }
        name = name or "资金异动"
    elif kind == "tick":
        # 大单成交（3 秒快照口径），池子固定为自选股 + 账户持仓：全市场逐笔轮询
        # 不现实（每只一次请求），所以这个规则的作用范围就是这个小池子。
        scope = data.get("scope", DEFAULT_TICK_SCOPE)
        if scope not in ("watchlist", "position", "both"):
            raise ValueError("请选择自选股或账户持仓")
        side = data.get("side", DEFAULT_TICK_SIDE)
        if side not in ("buy", "both"):
            raise ValueError("大单成交方向只能是主动买盘或买卖都看")
        config_data = {
            "providers": [TICK_PROVIDER],
            "scope": scope,
            "side": side,
            "min_amount": _number(data.get("min_amount", TICK_MIN_AMOUNT),
                                  "大单成交额阈值（元）", minimum=0),
        }
        name = name or "大单成交提醒"
    enabled = data.get("enabled", True)
    if not isinstance(enabled, bool):
        raise ValueError("enabled 必须是布尔值")
    return {"name": name, "kind": kind, "enabled": enabled,
            "config": config_data}


def save_rule(data, path=None):
    normalized = _normalize_rule(data)
    rid = str(data.get("id") or uuid.uuid4().hex)
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,40}", rid):
        raise ValueError("规则编号无效")
    at = now().isoformat(timespec="seconds")
    c = connect(path)
    try:
        with c:
            old = c.execute("SELECT revision,created_at FROM watch_rule WHERE id=?", (rid,)).fetchone()
            if old:
                old_kind = c.execute("SELECT kind FROM watch_rule WHERE id=?", (rid,)).fetchone()[0]
                if old_kind != normalized["kind"]:
                    raise ValueError("规则类型不可修改，请新建另一种规则")
            revision = (old[0] + 1) if old else 1
            c.execute("INSERT INTO watch_rule VALUES(?,?,?,?,?,?,?,?) "
                      "ON CONFLICT(id) DO UPDATE SET name=excluded.name,kind=excluded.kind,"
                      "enabled=excluded.enabled,config=excluded.config,revision=excluded.revision,"
                      "updated_at=excluded.updated_at",
                      (rid, normalized["name"], normalized["kind"], int(normalized["enabled"]),
                       json.dumps(normalized["config"], ensure_ascii=False), revision,
                       old[1] if old else at, at))
            if normalized["kind"] == "price" and normalized["config"]["scope"] == "watchlist":
                _stock_name = normalized["config"]["stock_name"]
                c.execute("INSERT INTO watch_stock(code,name,added_at) VALUES(?,?,?) "
                          "ON CONFLICT(code) DO UPDATE SET name=CASE WHEN excluded.name!='' "
                          "THEN excluded.name ELSE watch_stock.name END",
                          (normalized["config"]["code"], _stock_name, at))
        return _public_rule(c.execute("SELECT * FROM watch_rule WHERE id=?", (rid,)).fetchone())
    finally:
        c.close()


def _rule_config(raw):
    """把规则配置归一化成实际参与判定的口径，并兼容改造前写入的旧配置。

    改造前的 popularity 配置只有 previous_min_rank，语义是"昨日名次不小于 N"。两家公开
    榜单都只返回前 100 名，N 大于 100 时该条件永远不可能满足。这里不再把这个不可满足的
    门槛"静默降级"成新晋条件——旧实现用 `or not enable_jump` 兜底，结果把一条安静的规则
    变成了每天上百条的刷屏。现在：不可满足的门槛一律关闭，且缺失 enable_new_entry 的旧
    配置视为关闭，规则停止触发，由界面提示用户重建，而不是替用户猜一个更吵的口径。
    """
    cfg = dict(raw) if isinstance(raw, dict) else {}
    if cfg.get("providers") == [TICK_PROVIDER]:
        # 大单成交规则：只补齐缺省值，不参与人气榜的名次归一化。
        # 放在资金流判断之前——它的配置里也有 min_amount，先按 providers 认领，
        # 否则会被当成资金流规则处理。
        cfg.setdefault("scope", DEFAULT_TICK_SCOPE)
        cfg.setdefault("side", DEFAULT_TICK_SIDE)
        cfg.setdefault("min_amount", TICK_MIN_AMOUNT)
        cfg["enable_jump"] = False
        cfg["enable_new_entry"] = False
        cfg["legacy_invalid"] = False
        return cfg
    if cfg.get("providers") == [FLOW_PROVIDER] or "metric" in cfg or "direction" in cfg:
        # 资金流规则不参与人气榜的名次归一化，只补齐缺省值。
        cfg.setdefault("providers", [FLOW_PROVIDER])
        cfg.setdefault("direction", DEFAULT_FLOW_DIRECTION)
        cfg.setdefault("metric", DEFAULT_FLOW_METRIC)
        cfg.setdefault("min_amount", DEFAULT_FLOW_MIN_AMOUNT)
        cfg.setdefault("min_ratio", DEFAULT_FLOW_MIN_RATIO)
        cfg["enable_jump"] = False
        cfg["enable_new_entry"] = False
        cfg["legacy_invalid"] = False
        return cfg
    providers = cfg.get("providers")
    if not isinstance(providers, list) or not providers or not set(providers) <= PROVIDERS:
        providers = sorted(PROVIDERS)
    cfg["providers"] = sorted(set(providers))
    try:
        current_max = int(cfg.get("current_max_rank") or DEFAULT_CURRENT_MAX_RANK)
    except (TypeError, ValueError):
        current_max = DEFAULT_CURRENT_MAX_RANK
    cfg["current_max_rank"] = min(RANK_LIST_DEPTH, max(1, current_max))

    raw_from = cfg["previous_in_top"] if "previous_in_top" in cfg else cfg.get("previous_min_rank")
    legacy_invalid = False
    try:
        raw_from = None if raw_from is None else int(raw_from)
    except (TypeError, ValueError):
        raw_from = None
    if raw_from is not None and not 1 <= raw_from <= cfg["current_max_rank"]:
        legacy_invalid = True
    cfg["previous_in_top"] = (raw_from if raw_from is not None and
                              1 <= raw_from <= cfg["current_max_rank"] else None)
    try:
        improvement = int(cfg.get("min_improvement") or DEFAULT_JUMP_IMPROVEMENT)
    except (TypeError, ValueError):
        improvement = DEFAULT_JUMP_IMPROVEMENT
    cfg["min_improvement"] = max(1, improvement)
    try:
        entry_max = int(cfg.get("entry_max_rank") or DEFAULT_ENTRY_MAX_RANK)
    except (TypeError, ValueError):
        entry_max = DEFAULT_ENTRY_MAX_RANK
    cfg["entry_max_rank"] = min(cfg["current_max_rank"], max(1, entry_max))
    cfg["require_all_providers"] = bool(cfg.get("require_all_providers", True))
    cfg["enable_jump"] = cfg["previous_in_top"] is not None
    # 缺失 key 视为关闭：旧配置不会因为 jump 被关掉而自动启用新晋。
    cfg["enable_new_entry"] = bool(cfg.get("enable_new_entry", False))
    cfg["legacy_invalid"] = legacy_invalid
    return cfg


def _public_rule(row):
    item = dict(row)
    item["enabled"] = bool(item["enabled"])
    item["config"] = json.loads(item["config"])
    item["effective"] = _rule_config(item["config"])
    return item


def list_rules(path=None, kind=None):
    c = connect(path)
    try:
        if kind:
            rows = c.execute("SELECT * FROM watch_rule WHERE kind=? ORDER BY created_at DESC", (kind,))
        else:
            rows = c.execute("SELECT * FROM watch_rule ORDER BY created_at DESC")
        return [_public_rule(r) for r in rows]
    finally:
        c.close()


def delete_rule(rule_id, path=None):
    c = connect(path)
    try:
        with c:
            c.execute("DELETE FROM watch_rule WHERE id=?", (str(rule_id),))
            c.execute("DELETE FROM rule_state WHERE rule_id=?", (str(rule_id),))
        return {"ok": True}
    finally:
        c.close()


def _event(row):
    item = dict(row)
    item["rule_snapshot"] = json.loads(item["rule_snapshot"])
    item["providers"] = json.loads(item["providers"])
    item["observed"] = json.loads(item["observed"])
    item["read"] = bool(item.pop("read_at"))
    return item


def history(*, unread=None, limit=100, path=None):
    limit = max(1, min(500, int(limit)))
    c = connect(path)
    try:
        if unread:
            rows = c.execute("SELECT * FROM alert_event WHERE read_at IS NULL "
                              "ORDER BY triggered_at DESC LIMIT ?", (limit,))
        else:
            rows = c.execute("SELECT * FROM alert_event ORDER BY triggered_at DESC LIMIT ?", (limit,))
        return [_event(r) for r in rows]
    finally:
        c.close()


def mark_read(ids=None, all_unread=False, path=None):
    at = now().isoformat(timespec="seconds")
    c = connect(path)
    try:
        with c:
            if all_unread:
                c.execute("UPDATE alert_event SET read_at=? WHERE read_at IS NULL", (at,))
            else:
                ids = ids if isinstance(ids, list) else []
                ids = [str(x)[:40] for x in ids[:200]]
                if ids:
                    c.executemany("UPDATE alert_event SET read_at=? WHERE id=? AND read_at IS NULL",
                                  [(at, x) for x in ids])
        return {"ok": True}
    finally:
        c.close()


def clear_history(read_only=True, path=None):
    c = connect(path)
    try:
        with c:
            if read_only:
                c.execute("DELETE FROM alert_event WHERE read_at IS NOT NULL")
            else:
                c.execute("DELETE FROM alert_event")
        return {"ok": True}
    finally:
        c.close()


def _rule_snapshot(rule):
    config_data = rule["config"] if isinstance(rule["config"], dict) else json.loads(rule["config"])
    return {"id": rule["id"], "name": rule["name"], "kind": rule["kind"],
            "config": config_data, "revision": rule["revision"]}


def _source_state(c, provider, state, *, rows=0, coverage=0, error=None, at=None):
    at = at or now().isoformat(timespec="seconds")
    previous = c.execute("SELECT success_at FROM source_status WHERE provider=?", (provider,)).fetchone()
    success = at if state == "ok" else (previous[0] if previous else None)
    c.execute("INSERT INTO source_status(provider,state,checked_at,success_at,rows,coverage,error) "
              "VALUES(?,?,?,?,?,?,?) ON CONFLICT(provider) DO UPDATE SET state=excluded.state,"
              "checked_at=excluded.checked_at,success_at=excluded.success_at,rows=excluded.rows,"
              "coverage=excluded.coverage,error=excluded.error",
              (provider, state, at, success, int(rows), int(coverage), str(error or "")[:200] or None))


def source_status(path=None):
    c = connect(path)
    try:
        result = {"tencent": {"provider": "tencent", "state": "idle"},
                  "eastmoney": {"provider": "eastmoney", "state": "idle"},
                  "ths": {"provider": "ths", "state": "idle"},
                  "eastmoney_flow": {"provider": "eastmoney_flow", "state": "idle"},
                  "eastmoney_ticks": {"provider": "eastmoney_ticks", "state": "idle"}}
        for row in c.execute("SELECT * FROM source_status"):
            item = dict(row)
            if row["provider"] == "tencent":
                stale_after = 120
            elif row["provider"] == "eastmoney_flow":
                stale_after = 3900   # 盘后每小时一次；65 分钟未更新才算过期
            elif row["provider"] == "eastmoney_ticks":
                stale_after = 180    # 盘中每 60 秒一轮；3 分钟没更新就是断了
            else:
                stale_after = 1200
            checked = _quote_time(row["checked_at"])
            age = (now() - checked).total_seconds() if checked else None
            if item["state"] == "ok" and (age is None or age < -5 or age > stale_after):
                item["state"] = "stale"
                item["error"] = "最近一次成功采集已过期"
            result[row["provider"]] = item
        return result
    finally:
        c.close()


def _quote_time(value):
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return dt.replace(tzinfo=TZ) if dt.tzinfo is None else dt.astimezone(TZ)
    except ValueError:
        return None


def _reference(code, trade_day, *, path=None, fetcher=None):
    trade_day = str(trade_day)
    c = connect(path)
    try:
        row = c.execute("SELECT * FROM price_reference WHERE code=?", (code,)).fetchone()
        if row and row["fetched_at"][:10] == trade_day and not row["error"]:
            return dict(row)
        if row and row["error"] and (now() - datetime.fromisoformat(row["fetched_at"])).total_seconds() < 300:
            return dict(row)
    finally:
        c.close()

    fetcher = fetcher or _fetch_history
    fetched = now().isoformat(timespec="seconds")
    try:
        bars = fetcher(code)
        completed = sorted([b for b in (bars or []) if b.get("date") and b["date"] < trade_day],
                           key=lambda b: b["date"])[-20:]
        if len(completed) < 20 or len({b["date"] for b in completed}) != 20:
            raise ValueError("可用完整日线少于 20 个交易日")
        closes = [float(b["close"]) for b in completed]
        volumes = [float(b["volume"]) for b in completed]
        if any(not math.isfinite(x) or x <= 0 for x in closes + volumes):
            raise ValueError("日线价格或成交量无效")
        ref = {"code": code, "ref_date": completed[-1]["date"],
               "ma20": sum(closes) / 20, "avg_volume20": sum(volumes) / 20,
               "fetched_at": fetched, "error": None}
    except Exception as exc:  # noqa: BLE001
        ref = {"code": code, "ref_date": None, "ma20": None, "avg_volume20": None,
               "fetched_at": fetched, "error": str(exc)[:160]}
    c = connect(path)
    try:
        with c:
            c.execute("INSERT INTO price_reference VALUES(?,?,?,?,?,?) ON CONFLICT(code) "
                      "DO UPDATE SET ref_date=excluded.ref_date,ma20=excluded.ma20,"
                      "avg_volume20=excluded.avg_volume20,fetched_at=excluded.fetched_at,error=excluded.error",
                      tuple(ref[k] for k in ("code", "ref_date", "ma20", "avg_volume20", "fetched_at", "error")))
    finally:
        c.close()
    return ref


def _fetch_history(code):
    from .sources import eastmoney
    market = "1" if code.startswith(("5", "6", "9")) else "0"
    return eastmoney.fetch_kline(f"{market}.{code}", limit=50, adjusted=False)


def _condition(condition, current, previous):
    field, op = condition["field"], condition["op"]
    if field == "ma20":
        price, ma = current.get("price"), current.get("ma20")
        old_price, old_ma = (previous or {}).get("price"), (previous or {}).get("ma20")
        if price is None or ma is None:
            return None
        above = price > ma
        if op == "above":
            return above
        if op == "below":
            return price < ma
        if old_price is None or old_ma is None:
            return False
        return (old_price <= old_ma and price > ma) if op == "cross_above" \
            else (old_price >= old_ma and price < ma)
    value = current.get(field)
    if value is None:
        return None
    target = condition.get("value")
    if op == "gte":
        return value >= target
    if op == "lte":
        return value <= target
    old = (previous or {}).get(field)
    if old is None:
        return False
    return (old < target <= value) if op == "cross_above" else (old > target >= value)


def evaluate_price_rule(rule_config, current, previous=None):
    results = [_condition(item, current, previous) for item in rule_config["conditions"]]
    available = [x for x in results if x is not None]
    if len(available) != len(results):
        return None
    return all(results) if rule_config.get("logic", "all") == "all" else any(results)


def _insert_event(c, rule, *, code, name, kind, triggered_at, source_at,
                  providers, observed, dedupe_key):
    snapshot = json.dumps(_rule_snapshot(rule), ensure_ascii=False)
    providers_json = json.dumps(sorted(set(providers)), ensure_ascii=False)
    observed_json = json.dumps(observed, ensure_ascii=False, sort_keys=True)
    existing = c.execute("SELECT id,providers,observed,rule_snapshot FROM alert_event WHERE dedupe_key=?",
                         (dedupe_key,)).fetchone()
    if existing:
        if json.loads(existing["rule_snapshot"]) != json.loads(snapshot):
            return existing["id"]
        merged = json.loads(existing["observed"])
        previous_providers = set(json.loads(existing["providers"]))
        added = set(providers) - previous_providers
        if added:
            if kind == "popularity":
                merged.update({key: value for key, value in observed.items() if key in added})
            merged_providers = sorted(previous_providers | set(providers))
            c.execute("UPDATE alert_event SET providers=?,observed=? WHERE id=?",
                      (json.dumps(merged_providers),
                       json.dumps(merged, ensure_ascii=False, sort_keys=True), existing["id"]))
        return existing["id"]
    eid = uuid.uuid4().hex
    c.execute("INSERT INTO alert_event VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
              (eid, rule["id"], rule["name"], snapshot, code, name, kind,
               triggered_at, source_at, providers_json, observed_json, None, dedupe_key))
    return eid


def monitor_prices(*, path=None, quote_fetch=None, history_fetch=None, at=None):
    at = at or now()
    if at.tzinfo is None:
        at = at.replace(tzinfo=TZ)
    rules = [r for r in list_rules(path=path, kind="price") if r["enabled"]]
    if not rules:
        return {"state": "idle", "rows": 0}
    held = _position_codes() if any(r["config"].get("scope") == "position" for r in rules) else set()
    position_rules = [r for r in rules if r["config"].get("scope") == "position"]
    rules = [r for r in rules if r["config"].get("scope", "watchlist") != "position"
             or r["config"]["code"] in held]
    codes = sorted({r["config"]["code"] for r in rules})
    reference_codes = {r["config"]["code"] for r in rules
                       if any(c["field"] in ("ma20", "volume20_ratio")
                              for c in r["config"]["conditions"])}
    if not codes:
        c = connect(path)
        try:
            with c:
                c.executemany("DELETE FROM rule_state WHERE rule_id=? AND code=?",
                              [(r["id"], r["config"]["code"]) for r in position_rules])
        finally:
            c.close()
        return {"state": "idle", "rows": 0}
    if quote_fetch is None:
        from .sources import tencent
        quote_fetch = tencent.fetch_quotes
    quotes = []
    try:
        for start in range(0, len(codes), 50):
            quotes.extend(quote_fetch(codes[start:start + 50]) or [])
    except Exception as exc:  # noqa: BLE001
        _record_source("tencent", "error", error=exc, path=path, at=at)
        raise
    by_code = {str(q.get("code")): q for q in quotes if q.get("code")}
    fired, valid = [], 0
    c = connect(path)
    try:
        with c:
            c.executemany("DELETE FROM rule_state WHERE rule_id=? AND code=?",
                          [(r["id"], r["config"]["code"]) for r in position_rules
                           if r["config"]["code"] not in held])
    finally:
        c.close()
    for code in codes:
        quote = by_code.get(code)
        if not quote:
            continue
        quote_at = _quote_time(quote.get("quote_at"))
        age = (at - quote_at).total_seconds() if quote_at else None
        if age is None or age > 120 or age < -5:
            continue
        try:
            price = float(quote.get("close"))
            pct = float(quote["pct"]) if quote.get("pct") is not None else None
            volume = float(quote["volume"]) if quote.get("volume") is not None else None
            if not math.isfinite(price) or price <= 0 or (volume is not None and (not math.isfinite(volume) or volume < 0)):
                continue
        except (TypeError, ValueError):
            continue
        ref = (_reference(code, at.strftime("%Y-%m-%d"), path=path, fetcher=history_fetch)
               if code in reference_codes else {"ma20": None, "avg_volume20": None,
                                                "ref_date": None, "error": None})
        current = {"price": price, "pct": pct, "ma20": ref.get("ma20"), "volume": volume,
                   "volume20_ratio": volume / ref["avg_volume20"]
                   if volume is not None and ref.get("avg_volume20") else None}
        valid += 1
        c = connect(path)
        try:
            for rule in rules:
                cfg = rule["config"]
                if cfg["code"] != code:
                    continue
                state_row = c.execute("SELECT revision,state FROM rule_state WHERE rule_id=? AND code=?",
                                      (rule["id"], code)).fetchone()
                previous_state = json.loads(state_row["state"]) if state_row else None
                if not state_row or state_row["revision"] != rule["revision"]:
                    match = evaluate_price_rule(cfg, current, None)
                    state = {"sample": current, "matched": bool(match), "transition": 0}
                    with c:
                        c.execute("INSERT INTO rule_state VALUES(?,?,?,?,?) ON CONFLICT(rule_id,code) "
                                  "DO UPDATE SET revision=excluded.revision,state=excluded.state,updated_at=excluded.updated_at",
                                  (rule["id"], code, rule["revision"], json.dumps(state), at.isoformat(timespec="seconds")))
                    continue
                previous = previous_state.get("sample") if previous_state else None
                matched = evaluate_price_rule(cfg, current, previous)
                if matched is None:
                    continue
                was_matched = bool(previous_state.get("matched"))
                transition = int(previous_state.get("transition", 0))
                if matched and not was_matched:
                    transition += 1
                    event_id = _insert_event(
                        c, rule, code=code,
                        name=cfg.get("stock_name") or quote.get("name") or code,
                        kind="price", triggered_at=at.isoformat(timespec="seconds"),
                        source_at=quote_at.isoformat(timespec="seconds"), providers=["tencent"],
                        observed={"price": price, "pct": pct, "ma20": current["ma20"],
                                  "volume20_ratio": current["volume20_ratio"],
                                  "ref_date": ref.get("ref_date"), "basis": "腾讯行情与未复权日线"},
                        dedupe_key=f"price:{rule['id']}:{rule['revision']}:{code}:{transition}")
                    fired.append(event_id)
                state = {"sample": current, "matched": bool(matched), "transition": transition}
                with c:
                    c.execute("INSERT INTO rule_state VALUES(?,?,?,?,?) ON CONFLICT(rule_id,code) "
                              "DO UPDATE SET revision=excluded.revision,state=excluded.state,updated_at=excluded.updated_at",
                              (rule["id"], code, rule["revision"], json.dumps(state), at.isoformat(timespec="seconds")))
        finally:
            c.close()
    _record_source("tencent", "ok" if valid else "stale", rows=valid,
                   error=None if valid else "未收到有效且足够新鲜的报价", path=path, at=at)
    return {"state": "ok" if valid else "stale", "rows": valid, "events": fired}


def _record_source(provider, state, *, rows=0, coverage=0, error=None, path=None, at=None):
    c = connect(path)
    try:
        with c:
            _source_state(c, provider, state, rows=rows, coverage=coverage, error=error, at=(at or now()).isoformat(timespec="seconds"))
    finally:
        c.close()


def _position_codes():
    return {str(row["code"]) for row in position_targets()}


def position_targets():
    """Read current account holdings from SQLite without opening a write connection."""
    try:
        from . import portfolio
        target = Path(portfolio.PATH)
        if not target.is_file():
            return []
        c = sqlite3.connect(target.as_uri() + "?mode=ro", uri=True, timeout=5)
        c.row_factory = sqlite3.Row
        try:
            tables = {r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            source = "manual"
            if "portfolio_source" in tables:
                row = c.execute("SELECT source FROM portfolio_source WHERE id=1").fetchone()
                source = row["source"] if row else "manual"
            if source == "eastmoney" and "broker_snapshot" in tables:
                row = c.execute("SELECT payload FROM broker_snapshot WHERE id=1").fetchone()
                holdings = json.loads(row["payload"]).get("positions", []) if row else []
            elif "positions" in tables:
                holdings = [dict(r) for r in c.execute(
                    "SELECT code,name,quantity FROM positions WHERE quantity>0")]
            else:
                holdings = []
            return [{"code": str(p["code"]), "name": str(p.get("name") or p["code"]),
                     "quantity": p.get("quantity", 0)} for p in holdings
                    if p.get("code") and p.get("quantity", 0) > 0]
        finally:
            c.close()
    except (OSError, sqlite3.Error, ValueError, TypeError, KeyError):
        return []


def _complete_snapshot(c, provider, before_day, *, days=SNAPSHOT_MAX_AGE_DAYS):
    """返回最近一份完整收盘快照的 (trade_date, 覆盖深度)；无可用快照时返回 (None, 0)。

    只有名次连续覆盖 1..N 时快照才算完整。缺页会让"不在榜上"和"名次在榜外"混淆，
    此时不能推断任何跃升——宁可漏报，也不把未知名次当成大幅提升。
    """
    last = c.execute("SELECT MAX(trade_date) FROM rank_daily WHERE provider=? AND trade_date<?",
                     (provider, before_day)).fetchone()[0]
    if not last:
        return None, 0
    try:
        gap = (date.fromisoformat(before_day) - date.fromisoformat(str(last))).days
    except ValueError:
        return None, 0
    if gap <= 0 or gap > days:
        return None, 0
    ranks = sorted(int(r["rank"]) for r in c.execute(
        "SELECT rank FROM rank_daily WHERE provider=? AND trade_date=?", (provider, last)))
    if not ranks or ranks != list(range(1, len(ranks) + 1)):
        return None, 0
    return str(last), ranks[-1]


def monitor_popularity(*, path=None, fetchers=None, at=None):
    at = at or now()
    if at.tzinfo is None:
        at = at.replace(tzinfo=TZ)
    rules = [r for r in list_rules(path=path, kind="popularity") if r["enabled"]]
    if fetchers is None:
        from .sources import popularity
        fetchers = {"eastmoney": popularity.fetch_eastmoney, "ths": popularity.fetch_ths}
    fresh = {}
    statuses = {}
    for provider in sorted(PROVIDERS):
        try:
            rows = fetchers[provider]()
            rows = sorted(rows, key=lambda x: int(x["rank"]))[:1000]
            configs = [_rule_config(r["config"]) for r in rules]
            needed = max((c["current_max_rank"] for c in configs
                          if provider in c["providers"]), default=RANK_LIST_DEPTH)
            ranks = [int(x["rank"]) for x in rows]
            if not rows or ranks[0] != 1:
                raise ValueError("排名不完整：缺少第 1 名")
            codes = [str(x["code"]) for x in rows]
            if len(ranks) != len(set(ranks)) or len(codes) != len(set(codes)):
                raise ValueError("排名存在重复名次或证券")
            if max(ranks) < needed or ranks[:needed] != list(range(1, needed + 1)):
                raise ValueError(f"榜单覆盖不足：当前规则至少需要完整前 {needed} 名")
            fresh[provider] = rows
            statuses[provider] = {"state": "ok", "rows": len(rows), "coverage": max(ranks), "error": None}
        except Exception as exc:  # noqa: BLE001
            statuses[provider] = {"state": "error", "rows": 0, "coverage": 0,
                                  "error": str(exc)[:180]}
    c = connect(path)
    fired = []
    today = at.strftime("%Y-%m-%d")
    try:
        with c:
            for provider, status in statuses.items():
                _source_state(c, provider, status["state"], rows=status["rows"],
                              coverage=status["coverage"], error=status["error"],
                              at=at.isoformat(timespec="seconds"))
                if provider not in fresh:
                    c.execute("DELETE FROM rank_current WHERE provider=?", (provider,))
                    continue
                c.execute("DELETE FROM rank_current WHERE provider=?", (provider,))
                c.executemany("INSERT INTO rank_current VALUES(?,?,?,?,?)",
                              [(provider, row["code"], row["name"], int(row["rank"]),
                                at.isoformat(timespec="seconds")) for row in fresh[provider]])
            for rule in rules:
                cfg = _rule_config(rule["config"])
                fresh_providers = set(fresh)
                candidates = {}
                for provider in cfg["providers"]:
                    rows = fresh.get(provider)
                    if not rows:
                        continue
                    snap_day, snap_depth = _complete_snapshot(c, provider, today)
                    for row in rows:
                        rank = int(row["rank"])
                        # 只在进入 entry_max_rank 以内时才可能提醒；否则一天上百只进出前 100
                        # 的股票会把提醒刷屏。
                        if rank > cfg["current_max_rank"] or rank > cfg["entry_max_rank"]:
                            continue
                        prev = c.execute("SELECT rank,trade_date,source_at FROM rank_daily WHERE provider=? "
                                         "AND code=? AND trade_date<? ORDER BY trade_date DESC LIMIT 1",
                                         (provider, row["code"], today)).fetchone()
                        fresh_prev = prev if (prev and
                                              (date.fromisoformat(today) -
                                               date.fromisoformat(prev["trade_date"])).days
                                              <= SNAPSHOT_MAX_AGE_DAYS) else None
                        if fresh_prev is not None:
                            if not cfg["enable_jump"]:
                                continue
                            previous_rank = int(fresh_prev["rank"])
                            improvement = previous_rank - rank
                            if previous_rank > cfg["previous_in_top"] or \
                                    improvement < cfg["min_improvement"]:
                                continue
                            change = {"change": "jump", "previous_rank": previous_rank,
                                      "previous_date": fresh_prev["trade_date"],
                                      "current_rank": rank, "improvement": improvement}
                        elif cfg["enable_new_entry"] and snap_day and \
                                snap_depth >= cfg["current_max_rank"]:
                            # 该证券不在最近一份完整收盘快照里，而这份快照完整覆盖了前 snap_depth 名，
                            # 所以"昨日不在榜单"等价于"昨日名次在 snap_depth 之外"。这正是人气从榜外
                            # 跃入榜内的情形。快照缺失或不完整时不做任何推断。
                            change = {"change": "new_entry", "previous_rank": None,
                                      "previous_date": snap_day, "snapshot_depth": snap_depth,
                                      "current_rank": rank, "improvement": None}
                        else:
                            continue
                        change["source_at"] = at.isoformat(timespec="seconds")
                        item = candidates.setdefault(row["code"], {"name": row["name"], "observed": {}})
                        item["observed"][provider] = change
                for code, candidate in candidates.items():
                    if cfg["require_all_providers"]:
                        # 要求所有"有新鲜数据的"所选平台都命中，任一平台没报就不提醒。
                        # 某来源当次采集失败时按可用来源判定，不因缺一路而全部吞掉。
                        required = set(cfg["providers"]) & fresh_providers
                        if required and not required <= set(candidate["observed"]):
                            continue
                    event_id = _insert_event(
                        c, rule, code=code, name=candidate["name"], kind="popularity",
                        triggered_at=at.isoformat(timespec="seconds"), source_at=at.isoformat(timespec="seconds"),
                        providers=list(candidate["observed"]), observed=candidate["observed"],
                        dedupe_key=f"rank:{rule['id']}:{code}:{today}")
                    fired.append(event_id)
    finally:
        c.close()
    return {"state": "ok" if fresh else "error", "providers": statuses, "events": sorted(set(fired))}


def _flow_threshold(cfg):
    """返回 (方向, 阈值是否命中) 判定函数；metric 决定用净额还是净占比。"""
    metric = cfg.get("metric", DEFAULT_FLOW_METRIC)
    amount = float(cfg.get("min_amount") or 0)
    ratio = float(cfg.get("min_ratio") or 0)

    def hit(row):
        net, pct = row.get("main_net"), row.get("main_ratio")
        if metric == "main_ratio":
            return pct is not None and abs(pct) >= ratio
        return net is not None and abs(net) >= amount
    return hit


def monitor_flow(*, path=None, fetcher=None, at=None):
    """拉全市场资金流，落 flow_current，并按资金异动规则触发提醒。"""
    at = at or now()
    if at.tzinfo is None:
        at = at.replace(tzinfo=TZ)
    rules = [r for r in list_rules(path=path, kind="flow") if r["enabled"]]
    if fetcher is None:
        from .sources import fundflow
        fetcher = fundflow.fetch_flow_all
    try:
        rows = fetcher()
    except Exception as exc:  # noqa: BLE001
        _record_source(FLOW_PROVIDER, "error", error=exc, path=path, at=at)
        raise
    if not rows:
        _record_source(FLOW_PROVIDER, "error", error="资金流来源返回空", path=path, at=at)
        return {"state": "error", "rows": 0, "events": []}
    stamp = at.isoformat(timespec="seconds")
    fired = []
    today = at.strftime("%Y-%m-%d")
    c = connect(path)
    try:
        with c:
            c.execute("DELETE FROM flow_current WHERE provider=?", (FLOW_PROVIDER,))
            c.executemany("INSERT INTO flow_current VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                          [(FLOW_PROVIDER, r["code"], r["name"], r.get("price"), r.get("pct"),
                            r.get("amount"), r.get("main_net"), r.get("main_ratio"),
                            r.get("super_net"), r.get("large_net"), r.get("medium_net"),
                            r.get("small_net"), stamp) for r in rows])
            for rule in rules:
                cfg = _rule_config(rule["config"])
                direction = cfg.get("direction", "both")
                hit = _flow_threshold(cfg)
                for row in rows:
                    net = row.get("main_net")
                    if net is None or net == 0:
                        continue
                    if direction == "in" and net <= 0:
                        continue
                    if direction == "out" and net >= 0:
                        continue
                    if not hit(row):
                        continue
                    sign = "in" if net > 0 else "out"
                    event_id = _insert_event(
                        c, rule, code=row["code"], name=row["name"], kind="flow",
                        triggered_at=stamp, source_at=stamp, providers=[FLOW_PROVIDER],
                        observed={"direction": sign, "main_net": net,
                                  "main_ratio": row.get("main_ratio"),
                                  "pct": row.get("pct"), "amount": row.get("amount")},
                        dedupe_key=f"flow:{rule['id']}:{row['code']}:{sign}:{today}")
                    fired.append(event_id)
            _source_state(c, FLOW_PROVIDER, "ok", rows=len(rows), coverage=len(rows), at=stamp)
    finally:
        c.close()
    return {"state": "ok", "rows": len(rows), "events": sorted(set(fired))}


def _day_seconds(at):
    return at.hour * 3600 + at.minute * 60 + at.second


def _pool_names(path=None):
    """代码 -> 名称。只取名称，不受 TICK_MAX_CODES 截断影响。"""
    names = {}
    for row in list_stocks(path=path):
        code = str(row.get("code") or "").strip()
        if code:
            names[code] = str(row.get("name") or "")
    for p in position_targets():
        code = str(p.get("code") or "").strip()
        if code:
            names.setdefault(code, str(p.get("name") or ""))
    return names


def tick_pool(path=None):
    """大单成交的监控范围，返回 (items, total, mode)。

    优先自选股 + 账户持仓（按代码去重）。两者都为空时**自动回退**到"全市场成交额
    前 TICK_MAX_CODES 名"——否则面板在用户还没建自选股之前永远是空的，看起来像坏了。
    回退口径只花一次请求（量能榜第一页），明确标成 market，面板上会写明"未设自选股，
    正在监控全市场成交额前 N 名"，不会让人误以为在盯自己的票。

    items 是实际会监控的 [{"code","name","scope"}]，total 是池子全部数量：超过
    TICK_MAX_CODES 时只取前若干只，并把 total 报出去，界面显示"监控 30/48 只"，
    而不是让人以为整个池子都在盯着。scope 取 watchlist / position / both / market。
    """
    items, index = [], {}
    for row in list_stocks(path=path):
        code = str(row.get("code") or "").strip()
        if code and code not in index:
            index[code] = len(items)
            items.append({"code": code, "name": str(row.get("name") or ""),
                          "scope": "watchlist"})
    for p in position_targets():
        code = str(p.get("code") or "").strip()
        if not code:
            continue
        if code in index:
            items[index[code]]["scope"] = "both"
        else:
            index[code] = len(items)
            items.append({"code": code, "name": str(p.get("name") or ""),
                          "scope": "position"})
    if items:
        return items[:TICK_MAX_CODES], len(items), "user"
    return _market_pool()


def _market_pool(limit=TICK_MAX_CODES):
    """回退池：全市场成交额前 limit 名（一次请求）。取不到就返回空池。"""
    try:
        from .sources import fundflow
        rows = fundflow.fetch_volume_board(fid="f6", limit=limit)
    except Exception:                                  # noqa: BLE001
        return [], 0, "empty"
    items = [{"code": r["code"], "name": r["name"], "scope": "market"}
             for r in rows if r.get("code")]
    return items[:TICK_MAX_CODES], len(items), "market" if items else "empty"


def monitor_ticks(*, path=None, fetcher=None, at=None, codes=None, min_amount=None,
                  lookback=TICK_LOOKBACK_SECONDS):
    """抓自选股+持仓的分时成交，落 tick_big，并按大单成交规则触发提醒。

    三条边界，都是为了不骗人：
      1. 只统计连续竞价时段。集合竞价没有"主动买/主动卖"之分，接口却会给它标方向，
         不排除的话茅台开盘那一笔 4394 万会被当成主动买大单（实测当天有 115 条这种）。
      2. 只看最近 lookback 秒的成交。首次启用规则时不会把一整天的历史大单刷成提醒。
      3. 单只失败不拖垮整轮：能拿到的照常入库，覆盖度写进来源状态，界面显示
         "成功 28/30 只"，而不是整轮报错或假装没有大单。
    """
    from .sources import ticks as tick_src
    at = at or now()
    if at.tzinfo is None:
        at = at.replace(tzinfo=TZ)
    rules = [r for r in list_rules(path=path, kind="tick") if r["enabled"]]
    if fetcher is None:
        fetcher = tick_src.fetch_details
    if codes is not None:
        # 显式指定代码（离线测试、单次检查用）：名称仍然从池子里补齐，
        # 否则提醒记录里只有代码、名称是空的。
        names = _pool_names(path=path)
        items = [{"code": str(c), "name": names.get(str(c), ""), "scope": "both"}
                 for c in codes]
        total, mode = len(items), "given"
    else:
        items, total, mode = tick_pool(path=path)
    # 入库门槛取默认值与所有规则阈值里最小的那个：规则把阈值调低也不会漏掉数据。
    floors = [TICK_MIN_AMOUNT if min_amount is None else float(min_amount)]
    floors += [float(_rule_config(r["config"]).get("min_amount") or 0) for r in rules]
    floor = min(floors)
    stamp = at.isoformat(timespec="seconds")
    today = at.strftime("%Y-%m-%d")
    cutoff = _day_seconds(at) - max(0, int(lookback))
    stored, fired, failed = [], [], {}
    for item in items:
        code = item["code"]
        try:
            ticks = fetcher(code)
        except Exception as exc:                       # noqa: BLE001
            failed[code] = str(exc)[:80]
            continue
        for t in tick_src.big_buys(ticks, floor, side="both"):
            if not tick_src.in_session_hms(t["seconds"]):
                continue
            if cutoff > 0 and t["seconds"] < cutoff:
                continue
            stored.append((TICK_PROVIDER, today, code, item["name"], t["time"],
                           t["price"], t["lots"], t["amount"], t["side"], stamp))
    if not items:
        _record_source(TICK_PROVIDER, "idle",
                       error="自选股/持仓为空，回退的全市场成交额榜也没取到，暂无监控对象",
                       path=path, at=at)
        return {"state": "idle", "codes": 0, "total": total, "rows": 0,
                "pool": mode, "failed": {}, "events": []}
    if len(failed) == len(items):
        _record_source(TICK_PROVIDER, "error", rows=0, coverage=0,
                       error="全部分时成交请求失败（多为一轮内请求过多被限流）",
                       path=path, at=at)
        return {"state": "error", "codes": len(items), "total": total, "rows": 0,
                "pool": mode, "failed": failed, "events": []}
    c = connect(path)
    try:
        with c:
            if stored:
                c.executemany("INSERT OR REPLACE INTO tick_big VALUES(?,?,?,?,?,?,?,?,?,?)",
                              stored)
                # 保留期限：一天十万量级，长期堆着没有意义（能展示的只有金额最大的几十条）。
                c.execute("DELETE FROM tick_big WHERE trade_date < ?",
                          ((at - timedelta(days=TICK_RETENTION_DAYS)).strftime("%Y-%m-%d"),))
            scopes = {it["code"]: it["scope"] for it in items}
            for rule in rules:
                cfg = _rule_config(rule["config"])
                threshold = float(cfg.get("min_amount") or 0)
                side = cfg.get("side", DEFAULT_TICK_SIDE)
                scope = cfg.get("scope", DEFAULT_TICK_SCOPE)
                for row in stored:
                    # 元组顺序与 tick_big 的列一致：provider, trade_date, code, name,
                    # ts, price, lots, amount, side, source_at。别按"业务字段"去数，
                    # 第一个是 provider——把 provider 当成 code 写进提醒，界面上会看到
                    # 一屏 "eastmoney_ticks"。
                    _provider, _day, r_code, name, ts, price, lots, amount, r_side, _stamp = row
                    # 回退池只用于"看"，不触发提醒：规则的适用范围写的是自选股/持仓，
                    # 拿全市场成交额榜去触发等于偷偷扩大了规则范围。
                    if scopes.get(r_code) == "market":
                        continue
                    if amount < threshold:
                        continue
                    if side != "both" and r_side != side:
                        continue
                    if scope != "both" and scopes.get(r_code, "both") not in (scope, "both"):
                        continue
                    event_id = _insert_event(
                        c, rule, code=r_code, name=name, kind="tick",
                        triggered_at=stamp, source_at=stamp, providers=[TICK_PROVIDER],
                        observed={"time": ts, "price": price, "lots": lots,
                                  "amount": amount, "side": r_side},
                        dedupe_key=(f"tick:{rule['id']}:{r_code}:{today}:"
                                    f"{ts}:{lots}:{price}:{r_side}"))
                    fired.append(event_id)
            _source_state(c, TICK_PROVIDER, "ok", rows=len(stored),
                          coverage=len(items) - len(failed), at=stamp)
    finally:
        c.close()
    return {"state": "ok", "codes": len(items), "total": total, "rows": len(stored),
            "pool": mode, "failed": failed, "events": sorted(set(fired))}


def tick_board(*, limit=30, min_wan=100, path=None):
    """大单成交面板数据：当日达到门槛的主动买快照，按金额降序。"""
    c = connect(path)
    try:
        day = c.execute("SELECT MAX(trade_date) FROM tick_big").fetchone()[0] \
            or now().strftime("%Y-%m-%d")
        floor = max(0.0, float(min_wan) * 1e4)
        rows = [dict(r) for r in c.execute(
            "SELECT code,name,ts,price,lots,amount,side,source_at FROM tick_big "
            "WHERE trade_date=? AND side='buy' AND amount>=? "
            "ORDER BY amount DESC LIMIT ?", (day, floor, max(1, int(limit))))]
        matched = c.execute("SELECT COUNT(*) FROM tick_big WHERE trade_date=? AND "
                            "side='buy' AND amount>=?", (day, floor)).fetchone()[0]
        stored = c.execute("SELECT COUNT(*) FROM tick_big WHERE trade_date=?",
                           (day,)).fetchone()[0]
        source_at = c.execute("SELECT MAX(source_at) FROM tick_big WHERE trade_date=?",
                              (day,)).fetchone()[0]
        items, total, mode = tick_pool(path=path)
        return {"rows": rows, "date": day, "min_amount": floor, "matched": matched,
                "stored": stored, "source_at": source_at, "pool": mode,
                "monitored": len(items), "pool_total": total}
    finally:
        c.close()


def capture_daily_flow(*, path=None, at=None):
    """把当前资金流快照落成当日收盘快照（供历史回看）。"""
    at = at or now()
    if at.tzinfo is None:
        at = at.replace(tzinfo=TZ)
    today = at.strftime("%Y-%m-%d")
    c = connect(path)
    try:
        rows = c.execute("SELECT * FROM flow_current WHERE provider=?", (FLOW_PROVIDER,)).fetchall()
        if not rows:
            return {"rows": 0, "captured": False}
        with c:
            c.executemany("INSERT OR REPLACE INTO flow_daily VALUES(?,?,?,?,?,?,?,?,?)",
                          [(FLOW_PROVIDER, today, r["code"], r["name"], r["pct"], r["amount"],
                            r["main_net"], r["main_ratio"], r["source_at"]) for r in rows])
        return {"rows": len(rows), "captured": True, "date": today}
    finally:
        c.close()


def flow_board(*, limit=50, path=None):
    """资金异动面板数据：主力净流入/净流出榜。"""
    c = connect(path)
    try:
        cols = ("code,name,price,pct,amount,main_net,main_ratio,source_at")
        inflow = [dict(r) for r in c.execute(
            f"SELECT {cols} FROM flow_current WHERE provider=? AND main_net IS NOT NULL "
            "ORDER BY main_net DESC LIMIT ?", (FLOW_PROVIDER, max(1, limit)))]
        outflow = [dict(r) for r in c.execute(
            f"SELECT {cols} FROM flow_current WHERE provider=? AND main_net IS NOT NULL "
            "ORDER BY main_net ASC LIMIT ?", (FLOW_PROVIDER, max(1, limit)))]
        source_at = inflow[0]["source_at"] if inflow else None
        return {"inflow": inflow, "outflow": outflow, "source_at": source_at,
                "limit": limit}
    finally:
        c.close()


def capture_daily_ranks(*, path=None, at=None, max_age_minutes=30, providers=None):
    at = at or now()
    if at.tzinfo is None:
        at = at.replace(tzinfo=TZ)
    today = at.strftime("%Y-%m-%d")
    cutoff = at - timedelta(minutes=max_age_minutes)
    c = connect(path)
    try:
        rules = [r for r in list_rules(path=path, kind="popularity") if r["enabled"]]
        required = {}
        for rule in rules:
            cfg = _rule_config(rule["config"])
            for provider in cfg["providers"]:
                required[provider] = max(required.get(provider, 0), cfg["current_max_rank"])
        targets = sorted(set(providers) if providers is not None else
                         (set(required) | {r[0] for r in c.execute(
                             "SELECT DISTINCT provider FROM rank_current")}))
        written = 0
        results = {}
        with c:
            for provider in targets:
                if provider not in PROVIDERS:
                    continue
                needed = max(1, required.get(provider, RANK_LIST_DEPTH))
                saved = c.execute("SELECT rank FROM rank_daily WHERE provider=? AND trade_date=?",
                                  (provider, today)).fetchall()
                saved_ranks = sorted(int(r["rank"]) for r in saved)
                if saved_ranks[:needed] == list(range(1, needed + 1)):
                    results[provider] = {"captured": True, "rows": len(saved_ranks),
                                         "error": None, "existing": True}
                    continue
                status = c.execute("SELECT state,checked_at FROM source_status WHERE provider=?",
                                   (provider,)).fetchone()
                checked_at = _quote_time(status["checked_at"]) if status else None
                if (not status or status["state"] != "ok" or not checked_at or
                        checked_at < cutoff or checked_at > at + timedelta(seconds=5)):
                    results[provider] = {"captured": False, "rows": 0,
                                         "error": "来源状态未成功或检查时间已过期"}
                    continue
                rows = c.execute("SELECT * FROM rank_current WHERE provider=?", (provider,)).fetchall()
                if not rows or any(_quote_time(r["source_at"]) is None or
                                   _quote_time(r["source_at"]) < cutoff or
                                   _quote_time(r["source_at"]) > at + timedelta(seconds=5)
                                   for r in rows):
                    results[provider] = {"captured": False, "rows": 0,
                                         "error": "实时快照时间已过期"}
                    continue
                ranks = sorted(int(r["rank"]) for r in rows)
                codes = [r["code"] for r in rows]
                if (len(ranks) != len(set(ranks)) or len(codes) != len(set(codes)) or
                        ranks[:needed] != list(range(1, needed + 1))):
                    results[provider] = {"captured": False, "rows": 0,
                                         "error": f"榜单未完整覆盖前 {needed} 名"}
                    continue
                c.executemany("INSERT OR REPLACE INTO rank_daily VALUES(?,?,?,?,?,?)",
                              [(provider, today, r["code"], r["name"], r["rank"], r["source_at"])
                               for r in rows])
                written += len(rows)
                results[provider] = {"captured": True, "rows": len(rows), "error": None}
        return {"rows": written, "date": today, "providers": results,
                "captured_providers": sorted(p for p, result in results.items()
                                              if result["captured"])}
    finally:
        c.close()


def dashboard(path=None):
    c = connect(path)
    try:
        stocks = [dict(r) for r in c.execute("SELECT * FROM watch_stock ORDER BY name,code")]
        rules = [_public_rule(r) for r in c.execute("SELECT * FROM watch_rule ORDER BY created_at DESC")]
        unread = c.execute("SELECT COUNT(*) FROM alert_event WHERE read_at IS NULL").fetchone()[0]
        events = [_event(r) for r in c.execute("SELECT * FROM alert_event ORDER BY triggered_at DESC LIMIT 20")]
        sources = source_status(path)
        popularity = {}
        previous_day = now().strftime("%Y-%m-%d")
        for provider in sorted(PROVIDERS):
            last = c.execute("SELECT MAX(trade_date) FROM rank_daily "
                             "WHERE provider=? AND trade_date<?",
                             (provider, previous_day)).fetchone()[0]
            baseline = {r["code"]: {"rank": r["rank"], "date": r["trade_date"]}
                        for r in c.execute("SELECT code,rank,trade_date FROM rank_daily "
                                           "WHERE provider=? AND trade_date=?", (provider, last))} if last else {}
            _, snap_depth = _complete_snapshot(c, provider, previous_day)
            current = []
            for row in c.execute("SELECT code,name,rank,source_at FROM rank_current WHERE provider=? "
                                 "ORDER BY rank LIMIT 100", (provider,)):
                old = baseline.get(row["code"])
                current.append({**dict(row), "previous_rank": old["rank"] if old else None,
                                "previous_date": old["date"] if old else None,
                                "improvement": old["rank"] - row["rank"] if old else None,
                                "new_entry": old is None and snap_depth >= int(row["rank"])})
            popularity[provider] = {"items": current, "baseline_date": last,
                                    "baseline_depth": snap_depth}
        flow = flow_board(limit=50, path=path)
        flow_rules = [r for r in rules if r["kind"] == "flow"]
        return {"stocks": stocks, "rules": rules, "events": events, "unread": unread,
                "sources": sources, "popularity": popularity, "flow": flow,
                "flow_rules": flow_rules}
    finally:
        c.close()


def status(path=None):
    c = connect(path)
    try:
        counts = {kind: c.execute("SELECT COUNT(*) FROM watch_rule WHERE enabled=1 AND kind=?", (kind,)).fetchone()[0]
                  for kind in ("price", "popularity", "flow", "tick")}
        sources = source_status(path)
        return {"enabled_rules": counts, "monitoring": any(counts.values()), "sources": sources,
                "unread": c.execute("SELECT COUNT(*) FROM alert_event WHERE read_at IS NULL").fetchone()[0],
                "service_running": STATE["enabled"], "last": dict(STATE["last"]),
                "errors": dict(STATE["errors"])}
    finally:
        c.close()


def _watch_loop(stop):
    captured_day = None
    captured_providers = set()
    flow_captured_day = None
    while not stop.is_set():
        at = now()
        stamp = time.monotonic()
        price_rules = [r for r in list_rules(kind="price") if r["enabled"]]
        popularity_rules = [r for r in list_rules(kind="popularity") if r["enabled"]]
        flow_rules = [r for r in list_rules(kind="flow") if r["enabled"]]
        if price_rules and _in_market_session(at) and stamp - STATE["last"].get("prices", 0) >= 30:
            try:
                result = monitor_prices(at=at)
                STATE["last"]["prices"] = stamp
                STATE["errors"].pop("prices", None)
                STATE["price_state"] = result["state"]
            except Exception as exc:  # noqa: BLE001
                STATE["last"]["prices"] = stamp
                STATE["errors"]["prices"] = str(exc)[:180]
        # 大单成交：池子只有自选股+账户持仓，每次约 30 个请求，所以间隔单独设 60 秒，
        # 不跟资金流（600 秒）绑在一起。没有规则时也照常采集，面板不要求先建规则。
        last_ticks = STATE["last"].get("ticks")
        if (_in_market_session(at) and
                (last_ticks is None or stamp - last_ticks >= TICK_INTERVAL)):
            try:
                result = monitor_ticks(at=at)
                STATE["last"]["ticks"] = stamp
                STATE["tick_state"] = result["state"]
                if result.get("failed"):
                    STATE["errors"]["ticks"] = (
                        f"成功 {result['codes'] - len(result['failed'])}/{result['codes']} 只；"
                        + "；".join(f"{k}: {v}" for k, v in list(result["failed"].items())[:2])
                    )[:180]
                else:
                    STATE["errors"].pop("ticks", None)
            except Exception as exc:  # noqa: BLE001
                STATE["last"]["ticks"] = stamp
                STATE["errors"]["ticks"] = str(exc)[:180]
        last_flow = STATE["last"].get("flow")
        flow_interval = 600 if _in_market_session(at) else 3600
        if (_in_popularity_window(at) and
                (last_flow is None or stamp - last_flow >= flow_interval)):
            try:
                monitor_flow(at=at)
                STATE["last"]["flow"] = stamp
                STATE["errors"].pop("flow", None)
            except Exception as exc:  # noqa: BLE001
                STATE["last"]["flow"] = stamp
                STATE["errors"]["flow"] = str(exc)[:180]
        last_popularity = STATE["last"].get("popularity")
        if (_in_popularity_window(at) and
                (last_popularity is None or stamp - last_popularity >= 600)):
            try:
                result = monitor_popularity(at=at)
                STATE["last"]["popularity"] = stamp
                STATE["errors"].pop("popularity", None)
                if result["state"] == "error":
                    STATE["errors"]["popularity"] = "全部已启用的人气榜来源均不可用"
            except Exception as exc:  # noqa: BLE001
                STATE["last"]["popularity"] = stamp
                STATE["errors"]["popularity"] = str(exc)[:180]
        day = at.strftime("%Y-%m-%d")
        if day != captured_day:
            captured_day = day
            captured_providers = set()
        close_providers = sorted(PROVIDERS | {p for rule in popularity_rules
                                               for p in rule["config"]["providers"]})
        pending_providers = [p for p in close_providers if p not in captured_providers]
        if pending_providers and _in_close_capture_window(at):
            try:
                result = capture_daily_ranks(at=at, providers=pending_providers)
                captured_providers.update(result["captured_providers"])
                if result["captured_providers"]:
                    STATE["last"]["rank_close"] = at.strftime("%H:%M:%S")
                missing = {p: v["error"] for p, v in result["providers"].items()
                           if not v["captured"]}
                if missing:
                    STATE["errors"]["rank_close"] = "；".join(
                        f"{p}: {error}" for p, error in missing.items())[:180]
                else:
                    STATE["errors"].pop("rank_close", None)
            except Exception as exc:  # noqa: BLE001
                STATE["errors"]["rank_close"] = str(exc)[:180]
        if flow_captured_day != day and _in_close_capture_window(at):
            try:
                result = capture_daily_flow(at=at)
                if result["captured"]:
                    flow_captured_day = day
                    STATE["last"]["flow_close"] = at.strftime("%H:%M:%S")
                    STATE["errors"].pop("flow_close", None)
            except Exception as exc:  # noqa: BLE001
                STATE["errors"]["flow_close"] = str(exc)[:180]
        stop.wait(10)
    STATE["enabled"] = False


def _in_market_session(at):
    if at.weekday() >= 5:
        return False
    hm = at.strftime("%H:%M")
    return ("09:30" <= hm <= "11:30") or ("13:00" <= hm <= "15:00")


def _in_popularity_window(at):
    return "08:00" <= at.strftime("%H:%M") <= "22:00"


def _in_close_capture_window(at):
    hm = at.strftime("%H:%M")
    return at.weekday() < 5 and CLOSE_CAPTURE_START <= hm <= CLOSE_CAPTURE_END


def start(stop):
    with _THREAD_LOCK:
        if STATE["enabled"]:
            return None
        STATE["enabled"] = True
        STATE["started_at"] = now().isoformat(timespec="seconds")
        thread = threading.Thread(target=_watch_loop, args=(stop,), daemon=True,
                                  name="chaogu-watch")
        thread.start()
        return thread
