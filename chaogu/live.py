#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""盘中实时刷新：让看板在交易时段自己保持最新。

节奏（全部可在 INTERVALS 调）：
  - 快讯（新浪 7x24）：全天每 30 秒，写入 news 表；
  - 交易时段内：涨停池/炸板池（同花顺）每 60 秒，指数每 30 秒，
    题材板块+催化原文每 3 分钟，跌停池每 2 分钟；
    刷完后重算当日情绪指标，看板上的情绪/主线/明日关注跟着变。

盘中涨停池是"此刻"的快照：早上涨停、下午开板的票要从当日表里删掉，
所以当日数据是**整体替换**而不是 upsert 累加（否则炸板的票会一直算作涨停）。
只在新快照非空时替换，接口抽风返回空不会清空已有数据。

收盘后的完整采集（全市场行情、龙虎榜等）仍由 autocollect 负责。
"""

from __future__ import annotations

import threading
import time
from datetime import datetime

from . import db

INTERVALS = {"news": 30, "limit": 60, "index": 30, "theme": 180, "down": 120}

STATE = {
    "enabled": False, "session": False, "last": {}, "errors": {},
    "index": {}, "started_at": None, "ticks": 0,
}
_lock = threading.Lock()


def in_session(now=None):
    now = now or datetime.now()
    if now.weekday() >= 5:
        return False
    hm = now.strftime("%H:%M")
    return ("09:15" <= hm <= "11:31") or ("12:59" <= hm <= "15:02")


def _due(key, now):
    return now - STATE["last"].get(key, 0) >= INTERVALS[key]


def _run(key, fn):
    try:
        fn()
        STATE["last"][key] = time.time()
        STATE["errors"].pop(key, None)
    except Exception as exc:                          # noqa: BLE001
        # 失败也记时间，避免失败后每个 tick 都去打接口
        STATE["last"][key] = time.time()
        STATE["errors"][key] = f"{datetime.now():%H:%M:%S} {exc}"[:200]


def _replace_today(conn, table, rows, date):
    if not rows:
        return 0
    codes = [r["code"] for r in rows]
    ph = ",".join("?" for _ in codes)
    with conn:
        conn.execute(f"DELETE FROM {table} WHERE trade_date=? AND code NOT IN ({ph})",
                     (date, *codes))
    return db.upsert(conn, table, rows, ["trade_date", "code"])


def refresh_news():
    from . import collect
    conn = db.init()
    try:
        collect.step_news(conn)
    finally:
        conn.close()


def refresh_limit(date):
    from .sources import ths
    from . import collect, metrics
    conn = db.init()
    try:
        _replace_today(conn, "limit_up", ths.fetch_limit_up(date), date)
        _replace_today(conn, "broken_board", ths.fetch_broken(date), date)
        collect._fill_market(conn, date)
        try:
            metrics.compute_day(conn, date, save=True)
        except Exception:
            pass
    finally:
        conn.close()


def refresh_theme(date):
    from . import collect
    conn = db.init()
    try:
        collect.step_theme(conn, date)
    finally:
        conn.close()


def refresh_down(date):
    from . import collect
    conn = db.init()
    try:
        collect.step_limit_down(conn, date)
    finally:
        conn.close()


def refresh_index():
    from .sources import eastmoney as em
    STATE["index"] = {"at": datetime.now().strftime("%H:%M:%S"),
                      "data": em.fetch_indexes()}


def _loop(stop):
    while not stop.is_set():
        now = time.time()
        date = datetime.now().strftime("%Y-%m-%d")
        sess = in_session()
        STATE["session"] = sess
        STATE["ticks"] += 1
        with _lock:
            if _due("news", now):
                _run("news", refresh_news)
            if _due("index", now) and (sess or not STATE["index"]):
                _run("index", refresh_index)
            if sess:
                if _due("limit", now):
                    _run("limit", lambda: refresh_limit(date))
                if _due("theme", now):
                    _run("theme", lambda: refresh_theme(date))
                if _due("down", now):
                    _run("down", lambda: refresh_down(date))
        stop.wait(10)


def start(stop):
    STATE["enabled"] = True
    STATE["started_at"] = datetime.now().isoformat(timespec="seconds")
    t = threading.Thread(target=_loop, args=(stop,), daemon=True,
                         name="chaogu-live")
    t.start()
    return t


def status():
    return {
        "enabled": STATE["enabled"], "session": in_session(),
        "last": {k: datetime.fromtimestamp(v).strftime("%H:%M:%S")
                 for k, v in STATE["last"].items()},
        "errors": dict(STATE["errors"]),
        "index": STATE["index"],
        "now": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }
