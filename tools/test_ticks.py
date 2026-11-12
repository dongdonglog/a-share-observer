#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Offline regression tests for the tick-by-tick big-order monitor."""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from chaogu import watch                                    # noqa: E402
from chaogu.sources import ticks                            # noqa: E402


def row(time, price, lots, side="2"):
    """构造一行分时成交明细字符串（第 4 个字段是不用的 f54）。"""
    return f"{time},{price},{lots},{lots},{side}"


class TickSourceTests(unittest.TestCase):
    """解析层：金额换算是错一位就 100 倍误差的地方，必须锁住。"""

    def test_secid_market_mapping(self):
        self.assertEqual(ticks.secid("600519"), "1.600519")
        self.assertEqual(ticks.secid("688825"), "1.688825")
        self.assertEqual(ticks.secid("000725"), "0.000725")
        self.assertEqual(ticks.secid("300308"), "0.300308")
        # 北交所走 0 前缀：实测 0.920690 能返回数据，1.920690 拿不到。
        self.assertEqual(ticks.secid("920690"), "0.920690")
        self.assertEqual(ticks.secid("920690", market=0), "0.920690")

    def test_amount_is_price_times_lots_times_100(self):
        got = ticks.parse_details([row("13:57:56", "1317.50", 6)])
        self.assertEqual(len(got), 1)
        t = got[0]
        self.assertEqual(t["time"], "13:57:56")
        self.assertAlmostEqual(t["price"], 1317.5)
        self.assertEqual(t["lots"], 6)
        # 6 手 = 600 股 → 1317.5 × 600
        self.assertAlmostEqual(t["amount"], 1317.5 * 600)
        self.assertEqual(t["seconds"], 13 * 3600 + 57 * 60 + 56)

    def test_side_mapping(self):
        got = ticks.parse_details([row("10:00:00", 10, 1, "1"),
                                   row("10:00:03", 10, 1, "2"),
                                   row("10:00:06", 10, 1, "4"),
                                   row("10:00:09", 10, 1, "9")])
        self.assertEqual([t["side"] for t in got],
                         ["sell", "buy", "auction", "neutral"])

    def test_malformed_rows_are_skipped_not_guessed(self):
        got = ticks.parse_details([
            "10:00:00,10,1,1,2",      # 正常
            "10:00:00,10,1,1",        # 字段不足
            "bad-time,10,1,1,2",      # 时间不可解析
            "10:00:00,,1,1,2",        # 缺价格
            "10:00:00,0,1,1,2",       # 价格非正
            "10:00:00,10,-5,1,2",     # 手数为负
            "25:99:99,10,1,1,2",      # 时间越界
            "",
        ])
        self.assertEqual(len(got), 1)
        self.assertEqual(got[0]["time"], "10:00:00")

    def test_big_buys_threshold_side_and_lookback(self):
        got = ticks.parse_details([
            row("10:00:00", 10.0, 2000, "2"),    # 200万 主动买
            row("10:00:03", 10.0, 500, "2"),     # 50万  主动买
            row("10:00:06", 10.0, 3000, "1"),    # 300万 主动卖
            row("09:25:00", 10.0, 9000, "2"),    # 900万 但属于集合竞价时段
        ])
        # 默认只看主动买；集合竞价那笔 900万 即使金额达标也被档掉
        hits = ticks.big_buys(got, 1e6)
        self.assertEqual([h["time"] for h in hits], ["10:00:00"])
        both = ticks.big_buys(got, 1e6, side="both")
        self.assertEqual([h["time"] for h in both], ["10:00:00", "10:00:06"])
        # 显式关掉时段过滤才拿得到集合竞价那一笔，证明过滤确实起作用
        raw = ticks.big_buys(got, 1e6, session_only=False)
        self.assertIn("09:25:00", [h["time"] for h in raw])
        only_buy = [h["time"] for h in ticks.big_buys(got, 1e6, side="buy")]
        self.assertEqual(only_buy, ["10:00:00"])
        late = [h["time"] for h in ticks.big_buys(got, 1e6, side="both",
                                                  since_seconds=36005)]
        self.assertEqual(late, ["10:00:06"])

    def test_session_window_excludes_call_auctions(self):
        def sec(h, m, s):
            return h * 3600 + m * 60 + s
        self.assertFalse(ticks.in_session_hms(sec(9, 15, 2)))    # 开盘集合竞价
        self.assertFalse(ticks.in_session_hms(sec(9, 25, 0)))    # 开盘竞价成交
        self.assertTrue(ticks.in_session_hms(sec(9, 30, 0)))
        self.assertTrue(ticks.in_session_hms(sec(11, 29, 59)))
        self.assertFalse(ticks.in_session_hms(sec(11, 30, 0)))   # 午休
        self.assertFalse(ticks.in_session_hms(sec(12, 30, 0)))
        self.assertTrue(ticks.in_session_hms(sec(13, 0, 0)))
        self.assertTrue(ticks.in_session_hms(sec(14, 59, 59)))
        self.assertFalse(ticks.in_session_hms(sec(15, 0, 0)))    # 收盘竞价成交


class TickMonitorTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "watch.db"
        # 池子固定：000001 在自选，600000 是持仓。position_targets 读的是真实
        # portfolio.db，测试里必须替换掉，否则结果取决于用户本机有没有持仓。
        self.positions = patch.object(watch, "position_targets",
                                      return_value=[{"code": "600000", "name": "持仓股",
                                                     "quantity": 100}])
        self.positions.start()
        watch.add_stock("000001", "自选股", path=self.path)
        self.at = watch.now().replace(hour=10, minute=0, second=0, microsecond=0)

    def tearDown(self):
        self.positions.stop()
        self.tmp.cleanup()

    def feed(self, mapping, calls=None):
        """mapping: 代码 -> 明细行列表 或 Exception。"""
        def fetcher(code, **kw):
            if calls is not None:
                calls.append(code)
            got = mapping.get(code, [])
            if isinstance(got, Exception):
                raise got
            return ticks.parse_details(got)
        return fetcher

    def store(self, rule=None, mapping=None, at=None, **kw):
        mapping = mapping if mapping is not None else {
            "000001": [row("09:59:30", 10.0, 2000, "2"),    # 200万 主动买
                       row("09:54:00", 10.0, 5000, "2"),    # 500万 但早于 5 分钟窗口
                       row("09:59:40", 10.0, 3000, "1")],   # 300万 主动卖
            "600000": [row("09:25:00", 20.0, 9000, "2")],   # 集合竞价，应被排除
        }
        return watch.monitor_ticks(path=self.path, fetcher=self.feed(mapping, kw.pop("calls", None)),
                                   at=at or self.at, **kw)

    def test_pool_merges_watchlist_and_positions(self):
        items, total, mode = watch.tick_pool(path=self.path)
        self.assertEqual(mode, "user")
        self.assertEqual(total, 2)
        self.assertEqual({i["code"] for i in items}, {"000001", "600000"})
        scopes = {i["code"]: i["scope"] for i in items}
        self.assertEqual(scopes["000001"], "watchlist")
        self.assertEqual(scopes["600000"], "position")

    def test_pool_caps_and_reports_total(self):
        for i in range(5):
            watch.add_stock(f"60000{i}", f"股{i}", path=self.path)
        with patch.object(watch, "TICK_MAX_CODES", 3):
            items, total, mode = watch.tick_pool(path=self.path)
        self.assertEqual(mode, "user")
        self.assertEqual(len(items), 3)
        # 000001 + 600000..600004 = 6 只；600000 同时是持仓，按代码去重只算一次
        self.assertEqual(total, 6)

    def test_monitor_stores_only_session_snapshots_above_floor(self):
        res = self.store()
        self.assertEqual(res["state"], "ok")
        self.assertEqual(res["codes"], 2)
        # 09:59:30 的 200万买入 + 09:59:40 的 300万卖出入库；09:54 超出 5 分钟窗口
        # （窗口含边界，即"不早于"）；600000 的集合竞价被排除。
        self.assertEqual(res["rows"], 2)
        board = watch.tick_board(limit=10, path=self.path)
        self.assertEqual(board["stored"], 2)
        self.assertEqual(board["matched"], 1)       # 只看主动买
        self.assertEqual(board["rows"][0]["code"], "000001")

    def test_rule_threshold_and_side_filter(self):
        self.store()                       # 先在没有规则时采一轮：只入库、不提醒
        rule = watch.save_rule({"kind": "tick", "name": "大单", "scope": "both",
                                "side": "buy", "min_amount": 1e6}, path=self.path)
        c = watch.connect(self.path)
        try:
            n = c.execute("SELECT COUNT(*) FROM alert_event WHERE rule_id=?",
                          (rule["id"],)).fetchone()[0]
        finally:
            c.close()
        self.assertEqual(n, 0, "建规则之前不该有它的事件")
        res = self.store()
        self.assertTrue(res["events"], "已有规则时监控应触发事件")
        c = watch.connect(self.path)
        try:
            rows = [dict(r) for r in c.execute(
                "SELECT code,name,observed,kind FROM alert_event WHERE rule_id=?",
                (rule["id"],))]
        finally:
            c.close()
        self.assertEqual(len(rows), 1)               # 只有主动买那条，主动卖被 side 过滤
        self.assertEqual(rows[0]["code"], "000001")
        self.assertEqual(rows[0]["name"], "自选股")
        self.assertEqual(rows[0]["kind"], "tick")

    def test_second_run_does_not_duplicate_the_same_tick(self):
        watch.save_rule({"kind": "tick", "name": "大单", "side": "buy",
                         "min_amount": 1e6}, path=self.path)
        first = self.store()
        second = self.store()
        self.assertTrue(first["events"])
        # 第二次仍然是同一笔快照：events 会返回同一条记录 id（去重命中），
        # 关键是数据库里没有多出第二条。
        self.assertEqual(set(second["events"]), set(first["events"]))
        c = watch.connect(self.path)
        try:
            n = c.execute("SELECT COUNT(*) FROM alert_event").fetchone()[0]
        finally:
            c.close()
        self.assertEqual(n, 1)

    def test_scope_filter_respects_watchlist_and_position(self):
        rule = watch.save_rule({"kind": "tick", "name": "只看持仓", "scope": "position",
                                "side": "buy", "min_amount": 1e6}, path=self.path)
        self.store(mapping={"000001": [row("09:59:30", 10.0, 2000, "2")],
                            "600000": [row("09:59:30", 20.0, 1000, "2")]})
        c = watch.connect(self.path)
        try:
            codes = [r["code"] for r in c.execute(
                "SELECT code FROM alert_event WHERE rule_id=?", (rule["id"],))]
        finally:
            c.close()
        self.assertEqual(codes, ["600000"])          # 持仓的 200万 命中，自选的不命中

    def test_one_failed_code_does_not_fail_the_round(self):
        res = self.store(mapping={
            "000001": [row("09:59:30", 10.0, 2000, "2")],
            "600000": RuntimeError("模拟限流"),
        })
        self.assertEqual(res["state"], "ok")
        self.assertEqual(list(res["failed"]), ["600000"])
        self.assertEqual(res["rows"], 1)             # 成功的仍然入库
        c = watch.connect(self.path)
        try:
            item = dict(c.execute("SELECT * FROM source_status WHERE provider=?",
                                  (watch.TICK_PROVIDER,)).fetchone())
        finally:
            c.close()
        self.assertEqual(item["state"], "ok")
        self.assertEqual(item["coverage"], 1)        # 覆盖 1/2 只

    def test_all_codes_failing_marks_source_error(self):
        res = self.store(mapping={"000001": RuntimeError("挂"), "600000": RuntimeError("挂")})
        self.assertEqual(res["state"], "error")
        c = watch.connect(self.path)
        try:
            item = dict(c.execute("SELECT * FROM source_status WHERE provider=?",
                                  (watch.TICK_PROVIDER,)).fetchone())
        finally:
            c.close()
        self.assertEqual(item["state"], "error")

    def test_pool_pool_falls_back_to_market_when_user_pool_empty(self):
        """自选股和持仓都为空时，回退到全市场成交额榜，面板才不会永远是空的。"""
        c = watch.connect(self.path)
        try:
            with c:
                c.execute("DELETE FROM watch_stock")
            self.positions.stop()
            self.positions = patch.object(watch, "position_targets", return_value=[])
            self.positions.start()
            fake = [{"code": "600519", "name": "贵州茅台", "amount": 1e10},
                    {"code": "000725", "name": "京东方Ａ", "amount": 9e9}]
            with patch.object(watch, "_market_pool",
                              return_value=([{"code": r["code"], "name": r["name"],
                                              "scope": "market"} for r in fake], 2, "market")):
                items, total, mode = watch.tick_pool(path=self.path)
                self.assertEqual((mode, total, [i["scope"] for i in items]),
                                 ("market", 2, ["market", "market"]))
                # 回退池只用于看：规则不能拿它触发提醒（规则的适用范围写的是自选股/持仓）
                rule = watch.save_rule({"kind": "tick", "name": "只看自选", "scope": "both",
                                        "side": "buy", "min_amount": 1e6}, path=self.path)
                res = watch.monitor_ticks(path=self.path, at=self.at,
                                          fetcher=self.feed({"600519": [row("09:59:30", 10.0, 2000, "2")],
                                                             "000725": []}))
            self.assertEqual(res["pool"], "market")
            self.assertEqual(res["rows"], 1)          # 展示数据照常入库
            self.assertEqual(res["events"], [])       # 但不触发提醒
            c2 = watch.connect(self.path)
            try:
                n = c2.execute("SELECT COUNT(*) FROM alert_event WHERE rule_id=?",
                               (rule["id"],)).fetchone()[0]
                board = watch.tick_board(path=self.path)
            finally:
                c2.close()
            self.assertEqual(n, 0)
            self.assertEqual(board["pool"], "market")
            self.assertEqual([r["code"] for r in board["rows"]], ["600519"])
        finally:
            c.close()

    def test_empty_pool_and_no_fallback_is_idle(self):
        c = watch.connect(self.path)
        try:
            with c:
                c.execute("DELETE FROM watch_stock")
            self.positions.stop()
            self.positions = patch.object(watch, "position_targets", return_value=[])
            self.positions.start()
            with patch.object(watch, "_market_pool", return_value=([], 0, "empty")):
                res = self.store(mapping={})
        finally:
            c.close()
        self.assertEqual(res["state"], "idle")
        self.assertEqual(res["codes"], 0)

    def test_board_threshold_and_ordering(self):
        self.store(mapping={
            "000001": [row("09:59:30", 10.0, 2000, "2"),     # 200万
                       row("09:59:33", 10.0, 9000, "2")],    # 900万
            "600000": [],
        })
        board = watch.tick_board(limit=10, min_wan=500, path=self.path)
        self.assertEqual([r["lots"] for r in board["rows"]], [9000])   # 降序 + 阈值
        self.assertEqual(board["matched"], 1)
        self.assertEqual(board["stored"], 2)
        self.assertEqual(board["monitored"], 2)

    def test_rule_config_keeps_tick_and_flow_apart(self):
        tick_cfg = watch._rule_config({"providers": [watch.TICK_PROVIDER], "scope": "both",
                                       "side": "buy", "min_amount": 1e6})
        self.assertEqual(tick_cfg["side"], "buy")
        self.assertNotIn("metric", tick_cfg)
        flow_cfg = watch._rule_config({"providers": [watch.FLOW_PROVIDER],
                                       "direction": "in", "metric": "main_net",
                                       "min_amount": 1e8, "min_ratio": 3})
        self.assertEqual(flow_cfg["metric"], "main_net")
        self.assertNotIn("side", flow_cfg)     # tick 的默认值不能污染资金流规则

    def test_normalize_rule_rejects_bad_tick_config(self):
        for bad in ({"scope": "x"}, {"side": "up"}):
            with self.assertRaises(ValueError):
                watch._normalize_rule({"kind": "tick", **bad})
        self.assertIn("tick", ("price", "popularity", "flow", "tick"))

    def test_panel_query_wired(self):
        from chaogu import panels
        self.assertIn("tick_watch", panels.QUERIES)
        self.assertIn("tick_watch", panels.BY_ID)
        data = panels.q_tick_watch(None, "2026-10-16", {})
        self.assertEqual([k for k, _ in data["columns"]],
                         ["name", "time", "price", "lots", "amount_wan"])
        self.assertTrue(data["note"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
