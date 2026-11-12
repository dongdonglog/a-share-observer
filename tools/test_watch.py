#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Offline regression tests for local watch rules and rank snapshots."""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from datetime import timedelta
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from chaogu import panels, portfolio, watch, webapi
from chaogu.sources import popularity, tencent


class WatchTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "watch.db"
        self.at = watch.now().replace(hour=10, minute=0, second=0, microsecond=0)

    def tearDown(self):
        self.tmp.cleanup()

    def bars(self, at=None):
        at = at or self.at
        return [{"date": (at.date() - timedelta(days=i)).isoformat(),
                 "close": 10.0, "volume": 10.0}
                for i in range(20, 0, -1)]

    def quote(self, price, at=None, *, volume=25, pct=1):
        at = at or self.at
        return [{"code": "000001", "name": "测试股", "close": price,
                 "pct": pct, "volume": volume,
                 "quote_at": at.isoformat(timespec="seconds")}]

    def price_rule(self, **extra):
        data = {"kind": "price", "code": "000001", "stock_name": "测试股",
                "name": "站上 MA20 并放量", "logic": "all", "conditions": [
                    {"field": "price", "op": "cross_above", "value": 10},
                    {"field": "ma20", "op": "above"},
                    {"field": "volume20_ratio", "op": "gte", "value": 2},
                ]}
        data.update(extra)
        return watch.save_rule(data, path=self.path)

    def ranks(self, rank=100):
        rows = [{"code": f"{i:06d}", "name": f"股票{i}", "rank": i}
                for i in range(1, 101)]
        rows[-1] = {"code": "600001", "name": "测试股", "rank": rank}
        return rows

    def ranks_with(self, entry=None, drop=()):
        """1..100 的当日榜单。entry 按名次替换证券，drop 里的名次直接缺席。

        与 ranks() 不同，这里不会制造重复名次——真实榜单不会有两行同名次，
        而重复名次会被来源校验判为异常，那样测出来的并不是判定逻辑。
        """
        entry = entry or {}
        rows = []
        for rank in range(1, 101):
            if rank in drop:
                continue
            code, name = entry.get(rank, (f"{rank:06d}", f"股票{rank}"))
            rows.append({"code": code, "name": name, "rank": rank})
        return rows

    def seed_previous(self, provider, code="600001", rank=600):
        c = watch.connect(self.path)
        try:
            with c:
                c.execute("INSERT OR REPLACE INTO rank_daily VALUES(?,?,?,?,?,?)",
                          (provider, (self.at.date() - timedelta(days=1)).isoformat(), code,
                           "测试股", rank, (self.at - timedelta(days=1)).isoformat()))
        finally:
            c.close()

    def seed_snapshot(self, provider, entries, day=None, path=None):
        """写入一份收盘快照。entries 是 (名次, 代码, 名称) 的可迭代序列。"""
        day = day or (self.at.date() - timedelta(days=1)).isoformat()
        c = watch.connect(path or self.path)
        try:
            with c:
                for rank, code, name in entries:
                    c.execute("INSERT OR REPLACE INTO rank_daily VALUES(?,?,?,?,?,?)",
                              (provider, day, code, name, rank,
                               (self.at - timedelta(days=1)).isoformat()))
        finally:
            c.close()

    def seed_complete_previous(self, provider, *, depth=100, override=None, path=None):
        """完整覆盖 1..depth 的收盘快照；override 按名次替换证券。

        只有快照完整时，"不在榜上"才等价于"名次在 depth 之外"。真实数据源只返回前 100
        名，所以测试也必须构造完整快照，否则等于在验证缺页场景。
        """
        override = override or {}
        entries = []
        for rank in range(1, depth + 1):
            code, name = override.get(rank, (f"{rank:06d}", f"股票{rank}"))
            entries.append((rank, code, name))
        self.seed_snapshot(provider, entries, path=path)

    def popularity_rule(self, path=None, **extra):
        data = {"kind": "popularity", "providers": ["eastmoney"]}
        data.update(extra)
        return watch.save_rule(data, path=path or self.path)

    def test_price_crossing_volume_and_ma20_only_trigger_on_transition(self):
        self.price_rule()
        history = lambda code: self.bars()
        first = watch.monitor_prices(path=self.path, at=self.at,
                                     quote_fetch=lambda codes: self.quote(9.9),
                                     history_fetch=history)
        self.assertEqual(first["events"], [])
        fired = watch.monitor_prices(path=self.path, at=self.at + timedelta(seconds=30),
                                     quote_fetch=lambda codes: self.quote(10.1, self.at + timedelta(seconds=30)),
                                     history_fetch=history)
        self.assertEqual(len(fired["events"]), 1)
        repeated = watch.monitor_prices(path=self.path, at=self.at + timedelta(seconds=60),
                                       quote_fetch=lambda codes: self.quote(10.2, self.at + timedelta(seconds=60)),
                                       history_fetch=history)
        self.assertEqual(repeated["events"], [])
        watch.monitor_prices(path=self.path, at=self.at + timedelta(seconds=90),
                             quote_fetch=lambda codes: self.quote(9.8, self.at + timedelta(seconds=90)),
                             history_fetch=history)
        second = watch.monitor_prices(path=self.path, at=self.at + timedelta(seconds=120),
                                      quote_fetch=lambda codes: self.quote(10.2, self.at + timedelta(seconds=120)),
                                      history_fetch=history)
        self.assertEqual(len(second["events"]), 1)
        records = watch.history(path=self.path)
        self.assertEqual(len(records), 2)
        self.assertEqual(records[0]["observed"]["volume20_ratio"], 2.5)
        self.assertEqual(records[0]["observed"]["ma20"], 10.0)

    def test_missing_volume_and_stale_quote_do_not_advance_rule_state(self):
        self.price_rule(conditions=[{"field": "volume20_ratio", "op": "gte", "value": 2}])
        history = lambda code: self.bars()
        watch.monitor_prices(path=self.path, at=self.at,
                             quote_fetch=lambda codes: self.quote(10, volume=10),
                             history_fetch=history)
        missing_at = self.at + timedelta(seconds=30)
        missing = watch.monitor_prices(path=self.path, at=missing_at,
                                       quote_fetch=lambda codes: self.quote(10.2, missing_at, volume=None),
                                       history_fetch=history)
        self.assertEqual(missing["events"], [])
        fresh_at = self.at + timedelta(seconds=60)
        fired = watch.monitor_prices(path=self.path, at=fresh_at,
                                     quote_fetch=lambda codes: self.quote(10.3, fresh_at, volume=30),
                                     history_fetch=history)
        self.assertEqual(len(fired["events"]), 1)

    def test_old_and_future_quotes_never_trigger(self):
        self.price_rule(conditions=[{"field": "price", "op": "cross_above", "value": 10}])
        history = lambda code: self.bars()
        watch.monitor_prices(path=self.path, at=self.at,
                             quote_fetch=lambda codes: self.quote(9), history_fetch=history)
        stale_at = self.at + timedelta(seconds=30)
        stale = watch.monitor_prices(path=self.path, at=stale_at,
                                     quote_fetch=lambda codes: self.quote(11, stale_at - timedelta(seconds=121)),
                                     history_fetch=history)
        self.assertEqual(stale["events"], [])
        future = watch.monitor_prices(path=self.path, at=stale_at,
                                      quote_fetch=lambda codes: self.quote(11, stale_at + timedelta(seconds=6)),
                                      history_fetch=history)
        self.assertEqual(future["events"], [])
        fresh_at = stale_at + timedelta(seconds=30)
        fresh = watch.monitor_prices(path=self.path, at=fresh_at,
                                     quote_fetch=lambda codes: self.quote(11, fresh_at),
                                     history_fetch=history)
        self.assertEqual(len(fresh["events"]), 1)

    def test_any_logic_and_incomplete_indicators(self):
        cfg = {"logic": "any", "conditions": [
            {"field": "price", "op": "gte", "value": 10},
            {"field": "volume20_ratio", "op": "gte", "value": 2},
        ]}
        self.assertTrue(watch.evaluate_price_rule(cfg, {"price": 11, "volume20_ratio": 1}))
        self.assertIsNone(watch.evaluate_price_rule(cfg, {"price": 9, "volume20_ratio": None}))

    def test_popularity_merges_two_sources_and_deduplicates_same_day(self):
        rule = watch.save_rule({"kind": "popularity", "providers": ["eastmoney", "ths"],
                                "entry_max_rank": 100}, path=self.path)
        # 两家昨日快照都完整覆盖前 100 名且都不含 600001，因此 600001 今日进榜就是"新晋"。
        for provider in ("eastmoney", "ths"):
            self.seed_complete_previous(provider)
        rows = self.ranks(100)
        fetchers = {"eastmoney": lambda: rows, "ths": lambda: rows}
        first = watch.monitor_popularity(path=self.path, at=self.at, fetchers=fetchers)
        self.assertEqual(len(first["events"]), 1)
        second = watch.monitor_popularity(path=self.path, at=self.at + timedelta(minutes=10), fetchers=fetchers)
        self.assertEqual(len(second["events"]), 1)
        events = watch.history(path=self.path)
        self.assertEqual(len(events), 1)
        self.assertEqual(set(events[0]["providers"]), {"eastmoney", "ths"})
        observed = events[0]["observed"]["eastmoney"]
        self.assertEqual(observed["change"], "new_entry")
        self.assertIsNone(observed["previous_rank"])
        self.assertEqual(observed["current_rank"], 100)
        self.assertEqual(observed["snapshot_depth"], 100)
        watch.save_rule({"id": rule["id"], "kind": "popularity", "name": "调整后的规则",
                         "providers": ["eastmoney", "ths"], "entry_max_rank": 100}, path=self.path)
        watch.monitor_popularity(path=self.path, at=self.at + timedelta(minutes=20), fetchers=fetchers)
        unchanged = watch.history(path=self.path)[0]
        self.assertEqual(unchanged["rule_name"], rule["name"])
        self.assertEqual(unchanged["rule_snapshot"]["revision"], rule["revision"])

    def test_popularity_data_collects_for_display_without_alert_rules(self):
        at = watch.now()
        rows = self.ranks(100)
        result = watch.monitor_popularity(
            path=self.path, at=at,
            fetchers={"eastmoney": lambda: rows, "ths": lambda: rows})
        self.assertEqual(result["state"], "ok")
        self.assertEqual(result["events"], [])
        self.assertEqual(set(result["providers"]), {"eastmoney", "ths"})

        view = watch.dashboard(path=self.path)
        self.assertEqual(len(view["popularity"]["eastmoney"]["items"]), 100)
        self.assertEqual(len(view["popularity"]["ths"]["items"]), 100)
        self.assertIsNone(view["popularity"]["eastmoney"]["baseline_date"])

        captured = watch.capture_daily_ranks(path=self.path, at=at,
                                             providers=["eastmoney", "ths"])
        self.assertEqual(set(captured["captured_providers"]), {"eastmoney", "ths"})

    def test_popularity_needs_a_valid_previous_close_and_complete_current_coverage(self):
        watch.save_rule({"kind": "popularity", "providers": ["eastmoney"]}, path=self.path)
        rows = self.ranks(100)
        # 没有任何收盘快照时，"不在榜上"无从判断，新晋也不成立。
        no_baseline = watch.monitor_popularity(path=self.path, at=self.at,
                                               fetchers={"eastmoney": lambda: rows})
        self.assertEqual(no_baseline["events"], [])
        # 只有一行 = 不完整的快照。改造前这里是被 previous_min_rank=500 的门槛拦下，
        # 现在是被快照完整性拦下：不能把"快照里没有"当成"名次在榜外"。
        self.seed_previous("eastmoney", rank=450)
        incomplete_snapshot = watch.monitor_popularity(path=self.path, at=self.at + timedelta(minutes=10),
                                                       fetchers={"eastmoney": lambda: rows})
        self.assertEqual(incomplete_snapshot["events"], [])
        # 当日榜单本身缺一枚名次属于覆盖不足，该来源整体作废并清空实时快照。
        self.seed_previous("eastmoney", rank=600)
        failed = watch.monitor_popularity(path=self.path, at=self.at + timedelta(minutes=20),
                                          fetchers={"eastmoney": lambda: rows[:-1]})
        self.assertEqual(failed["events"], [])
        c = watch.connect(self.path)
        try:
            self.assertEqual(c.execute("SELECT COUNT(*) FROM rank_current WHERE provider='eastmoney'").fetchone()[0], 0)
        finally:
            c.close()

    def test_new_entry_triggers_when_stock_was_below_snapshot(self):
        self.popularity_rule(entry_max_rank=100)
        self.seed_complete_previous("eastmoney")
        fired = watch.monitor_popularity(
            path=self.path, at=self.at,
            fetchers={"eastmoney": lambda: self.ranks_with({50: ("600001", "测试股")})})
        self.assertEqual(len(fired["events"]), 1)
        event = watch.history(path=self.path)[0]
        self.assertEqual(event["code"], "600001")
        observed = event["observed"]["eastmoney"]
        self.assertEqual(observed["change"], "new_entry")
        self.assertIsNone(observed["previous_rank"])
        self.assertEqual(observed["current_rank"], 50)
        self.assertEqual(observed["snapshot_depth"], 100)

    def test_new_entry_blocked_by_incomplete_previous_snapshot(self):
        self.popularity_rule(entry_max_rank=100)
        # 快照缺第 100 名，因此不能断定 600001 昨日在榜外。
        self.seed_snapshot("eastmoney",
                           [(rank, f"{rank:06d}", f"股票{rank}") for rank in range(1, 100)])
        blocked = watch.monitor_popularity(
            path=self.path, at=self.at,
            fetchers={"eastmoney": lambda: self.ranks_with({50: ("600001", "测试股")})})
        self.assertEqual(blocked["events"], [])

    def test_new_entry_requires_snapshot_depth_to_cover_current_rank(self):
        self.popularity_rule(entry_max_rank=100)
        # 快照完整但只到第 50 名；今日榜单是按前 100 名判定的，深度不够就不能下结论。
        self.seed_complete_previous("eastmoney", depth=50)
        blocked = watch.monitor_popularity(path=self.path, at=self.at,
                                          fetchers={"eastmoney": lambda: self.ranks_with({100: ("600001", "测试股")})})
        self.assertEqual(blocked["events"], [])

    def test_jump_threshold_triggers_only_when_improvement_is_enough(self):
        fired_path = Path(self.tmp.name) / "jump-fire.db"
        self.popularity_rule(path=fired_path, previous_in_top=100, min_improvement=30,
                             entry_max_rank=100)
        self.seed_complete_previous("eastmoney", override={100: ("600001", "测试股")},
                                    path=fired_path)
        # 昨日第 100 名 → 今日第 20 名，提升 80 ≥ 30，且昨日名次在起点门槛之内。
        # 今日把 000020 换到第 100 名，保证两天的证券集合一致——否则被挤掉的那只会
        # 变成额外的"新晋"，测出来的就不是跃升门槛了。
        fired = watch.monitor_popularity(
            path=fired_path, at=self.at,
            fetchers={"eastmoney": lambda: self.ranks_with(
                {20: ("600001", "测试股"), 100: ("000020", "股票20")})})
        self.assertEqual(len(fired["events"]), 1)
        observed = watch.history(path=fired_path)[0]["observed"]["eastmoney"]
        self.assertEqual(observed["change"], "jump")
        self.assertEqual(observed["previous_rank"], 100)
        self.assertEqual(observed["current_rank"], 20)
        self.assertEqual(observed["improvement"], 80)

        small_path = Path(self.tmp.name) / "jump-small.db"
        self.popularity_rule(path=small_path, previous_in_top=100, min_improvement=30,
                             entry_max_rank=100)
        self.seed_complete_previous("eastmoney", override={100: ("600001", "测试股")},
                                    path=small_path)
        # 昨日第 100 名 → 今日第 75 名，提升 25 < 30，不应触发。
        small = watch.monitor_popularity(
            path=small_path, at=self.at,
            fetchers={"eastmoney": lambda: self.ranks_with(
                {75: ("600001", "测试股"), 100: ("000075", "股票75")})})
        self.assertEqual(small["events"], [])

    def test_previous_in_top_null_disables_jump_but_keeps_new_entry(self):
        jump_path = Path(self.tmp.name) / "jump-off.db"
        self.popularity_rule(path=jump_path, previous_in_top=None, enable_new_entry=True)
        self.seed_complete_previous("eastmoney", override={100: ("600001", "测试股")},
                                    path=jump_path)
        # 600001 昨日在榜（第 100 名）且今日大幅上升，但榜内跃升已关闭 → 不触发。
        disabled = watch.monitor_popularity(
            path=jump_path, at=self.at,
            fetchers={"eastmoney": lambda: self.ranks_with(
                {20: ("600001", "测试股"), 100: ("000020", "股票20")})})
        self.assertEqual(disabled["events"], [])

        entry_path = Path(self.tmp.name) / "entry-on.db"
        self.popularity_rule(path=entry_path, previous_in_top=None, enable_new_entry=True)
        # 昨日快照完整但不含 600001 → 关掉榜内跃升不影响新晋。
        self.seed_complete_previous("eastmoney", path=entry_path)
        fired = watch.monitor_popularity(
            path=entry_path, at=self.at,
            fetchers={"eastmoney": lambda: self.ranks_with({30: ("600001", "测试股")})})
        self.assertEqual(len(fired["events"]), 1)
        self.assertEqual(watch.history(path=entry_path)[0]["observed"]["eastmoney"]["change"],
                         "new_entry")

    def test_legacy_unfulfillable_threshold_disables_rule_without_rewriting_database(self):
        legacy = {"providers": ["eastmoney"], "previous_min_rank": 500,
                  "current_max_rank": 100, "min_improvement": 400}
        c = watch.connect(self.path)
        try:
            with c:
                c.execute("INSERT INTO watch_rule VALUES(?,?,?,?,?,?,?,?)",
                          ("legacy01", "人气跃升至前 100", "popularity", 1,
                           json.dumps(legacy, ensure_ascii=False), 1,
                           self.at.isoformat(timespec="seconds"),
                           self.at.isoformat(timespec="seconds")))
        finally:
            c.close()
        stored = watch.list_rules(path=self.path, kind="popularity")[0]
        # 旧门槛 500 超出榜单深度 100，无法满足：不再静默降级成新晋，规则整体关闭，
        # 原值保留供界面提示用户重建（旧实现用 `or not enable_jump` 兜底导致刷屏）。
        self.assertFalse(stored["effective"]["enable_jump"])
        self.assertIsNone(stored["effective"]["previous_in_top"])
        self.assertFalse(stored["effective"]["enable_new_entry"])
        self.assertTrue(stored["effective"]["legacy_invalid"])
        self.assertEqual(stored["config"]["previous_min_rank"], 500)

        self.seed_complete_previous("eastmoney")
        fired = watch.monitor_popularity(path=self.path, at=self.at,
                                         fetchers={"eastmoney": lambda: self.ranks(100)})
        self.assertEqual(fired["events"], [])

        c = watch.connect(self.path)
        try:
            after = c.execute("SELECT config FROM watch_rule WHERE id='legacy01'").fetchone()[0]
        finally:
            c.close()
        self.assertEqual(json.loads(after), legacy)

    def test_new_entry_gated_to_entry_max_rank(self):
        # 进入前 100 但未进入 entry_max_rank(=30) 的证券不提醒；这是修掉刷屏的核心门槛。
        fired = Path(self.tmp.name) / "gate-fire.db"
        blocked = Path(self.tmp.name) / "gate-block.db"
        self.popularity_rule(path=fired, previous_in_top=None, enable_new_entry=True,
                             entry_max_rank=30)
        self.popularity_rule(path=blocked, previous_in_top=None, enable_new_entry=True,
                             entry_max_rank=30)
        for p in (fired, blocked):
            self.seed_complete_previous("eastmoney", path=p)
        hit = watch.monitor_popularity(
            path=fired, at=self.at,
            fetchers={"eastmoney": lambda: self.ranks_with({25: ("600001", "测试股")})})
        self.assertEqual(len(hit["events"]), 1)
        miss = watch.monitor_popularity(
            path=blocked, at=self.at,
            fetchers={"eastmoney": lambda: self.ranks_with({80: ("600002", "榜外股")})})
        self.assertEqual(miss["events"], [])

    def test_require_all_providers_blocks_single_source_hit(self):
        self.save_two_provider_rule(self.path)
        self.seed_complete_previous("eastmoney")
        self.seed_complete_previous("ths")
        rows = self.ranks_with({25: ("600001", "测试股")})
        baseline = self.ranks_with()
        # 只有东方财富出现新晋，同花顺没有 → 双榜口径下不提醒。
        watched = watch.monitor_popularity(
            path=self.path, at=self.at,
            fetchers={"eastmoney": lambda: rows, "ths": lambda: baseline})
        self.assertEqual(watched["events"], [])
        # 两家都出现 → 提醒。
        both = watch.monitor_popularity(
            path=self.path, at=self.at + timedelta(minutes=10),
            fetchers={"eastmoney": lambda: rows, "ths": lambda: rows})
        self.assertEqual(len(both["events"]), 1)

    def save_two_provider_rule(self, path):
        return watch.save_rule({"kind": "popularity", "providers": ["eastmoney", "ths"],
                                "previous_in_top": None, "enable_new_entry": True,
                                "require_all_providers": True, "entry_max_rank": 30},
                               path=path)

    def test_new_entry_and_jump_coexist_in_one_run(self):
        self.popularity_rule(previous_in_top=100, min_improvement=30, enable_new_entry=True,
                             entry_max_rank=100)
        self.seed_complete_previous("eastmoney", override={100: ("600001", "测试股")})
        fired = watch.monitor_popularity(
            path=self.path, at=self.at,
            fetchers={"eastmoney": lambda: self.ranks_with(
                {20: ("600001", "测试股"), 100: ("600002", "新晋股")})})
        self.assertEqual(len(fired["events"]), 2)
        by_code = {e["code"]: e["observed"]["eastmoney"]["change"]
                   for e in watch.history(path=self.path)}
        self.assertEqual(by_code, {"600001": "jump", "600002": "new_entry"})

    def test_close_snapshot_requires_recent_successful_source(self):
        watch.save_rule({"kind": "popularity", "providers": ["eastmoney"]}, path=self.path)
        close_at = self.at.replace(hour=15, minute=5)
        checked_at = close_at - timedelta(minutes=5)
        rows = self.ranks(100)
        watch.monitor_popularity(path=self.path, at=checked_at,
                                 fetchers={"eastmoney": lambda: rows})
        captured = watch.capture_daily_ranks(path=self.path, at=close_at)
        self.assertEqual(captured["rows"], 100)
        watch.monitor_popularity(path=self.path, at=close_at + timedelta(minutes=10),
                                 fetchers={"eastmoney": lambda: []})
        rejected = watch.capture_daily_ranks(path=self.path, at=close_at + timedelta(minutes=11))
        self.assertEqual(rejected["rows"], 0)

    def test_close_snapshot_retries_each_provider_independently(self):
        watch.save_rule({"kind": "popularity", "providers": ["eastmoney", "ths"]}, path=self.path)
        close_at = self.at.replace(hour=15, minute=5)
        rows = self.ranks(100)
        first = watch.monitor_popularity(
            path=self.path, at=close_at - timedelta(minutes=5),
            fetchers={"eastmoney": lambda: rows,
                      "ths": lambda: (_ for _ in ()).throw(ValueError("临时不可用"))})
        self.assertEqual(first["providers"]["eastmoney"]["state"], "ok")
        self.assertEqual(first["providers"]["ths"]["state"], "error")

        partial = watch.capture_daily_ranks(path=self.path, at=close_at,
                                            providers=["eastmoney", "ths"])
        self.assertEqual(partial["captured_providers"], ["eastmoney"])
        self.assertFalse(partial["providers"]["ths"]["captured"])

        recovered_at = close_at + timedelta(minutes=10)
        watch.monitor_popularity(path=self.path, at=recovered_at,
                                 fetchers={"eastmoney": lambda: rows, "ths": lambda: rows})
        recovered = watch.capture_daily_ranks(path=self.path, at=recovered_at,
                                              providers=["ths"])
        self.assertEqual(recovered["captured_providers"], ["ths"])
        c = watch.connect(self.path)
        try:
            for provider in ("eastmoney", "ths"):
                ranks = [r[0] for r in c.execute(
                    "SELECT rank FROM rank_daily WHERE provider=? AND trade_date=? ORDER BY rank",
                    (provider, close_at.strftime("%Y-%m-%d")))]
                self.assertEqual(ranks, list(range(1, 101)))
        finally:
            c.close()

    def test_close_snapshot_rejects_incomplete_coverage(self):
        watch.save_rule({"kind": "popularity", "providers": ["eastmoney"]}, path=self.path)
        close_at = self.at.replace(hour=15, minute=5)
        rows = self.ranks(100)[:-1]
        watch.monitor_popularity(path=self.path, at=close_at - timedelta(minutes=5),
                                 fetchers={"eastmoney": lambda: rows})
        result = watch.capture_daily_ranks(path=self.path, at=close_at,
                                           providers=["eastmoney"])
        self.assertEqual(result["rows"], 0)
        self.assertFalse(result["providers"]["eastmoney"]["captured"])

    def test_close_snapshot_retry_window_is_bounded(self):
        close_start = self.at.replace(hour=15, minute=5)
        close_end = self.at.replace(hour=16, minute=5)
        self.assertFalse(watch._in_close_capture_window(close_start - timedelta(minutes=1)))
        self.assertTrue(watch._in_close_capture_window(close_start))
        self.assertTrue(watch._in_close_capture_window(close_end))
        self.assertFalse(watch._in_close_capture_window(close_end + timedelta(minutes=1)))
        weekend = close_start + timedelta(days=(5 - close_start.weekday()) % 7 or 7)
        self.assertFalse(watch._in_close_capture_window(weekend))

    def test_rule_edits_do_not_rewrite_trigger_snapshots_and_read_cleanup(self):
        rule = self.price_rule(conditions=[{"field": "price", "op": "gte", "value": 10}], logic="all")
        at = self.at
        watch.monitor_prices(path=self.path, at=at, quote_fetch=lambda codes: self.quote(9), history_fetch=lambda code: self.bars())
        watch.monitor_prices(path=self.path, at=at + timedelta(seconds=30), quote_fetch=lambda codes: self.quote(10.1, at + timedelta(seconds=30)), history_fetch=lambda code: self.bars())
        changed = watch.save_rule({"id": rule["id"], "kind": "price", "code": "000001",
                                   "name": "新规则名", "conditions": [{"field": "price", "op": "gte", "value": 12}]}, path=self.path)
        self.assertGreater(changed["revision"], rule["revision"])
        old = watch.history(path=self.path)[0]
        self.assertEqual(old["rule_name"], rule["name"])
        self.assertEqual(old["rule_snapshot"]["revision"], rule["revision"])
        watch.mark_read([old["id"]], path=self.path)
        self.assertEqual(watch.status(path=self.path)["unread"], 0)
        watch.clear_history(read_only=True, path=self.path)
        self.assertEqual(watch.history(path=self.path), [])

    def test_position_rule_is_separate_from_watchlist(self):
        watch.save_rule({"kind": "price", "code": "000001", "scope": "position",
                         "conditions": [{"field": "price", "op": "gte", "value": 10}]}, path=self.path)
        self.assertEqual(watch.list_stocks(path=self.path), [])

    def test_empty_position_target_lookup_does_not_create_account_database(self):
        account_db = Path(self.tmp.name) / "account.db"
        with patch.object(portfolio, "PATH", account_db):
            self.assertEqual(watch.position_targets(), [])
        self.assertFalse(account_db.exists())

    def test_watch_api_validation_and_workspace_entry(self):
        with patch.object(watch, "PATH", self.path):
            status = webapi.handle_get("/api/watch/status", "")
            self.assertEqual(json.loads(status[2])["unread"], 0)
            bad = webapi.handle_get("/api/watch/history", "limit=oops")
            self.assertEqual(bad[0], 400)
            saved = webapi.handle_post("/api/watch/rules", json.dumps({
                "kind": "price", "code": "000001", "enabled": True,
                "conditions": [{"field": "price", "op": "gte", "value": 10}],
            }).encode())
            self.assertEqual(saved[0], 200)
        learning = next(d for d in panels.default_dashboards() if d["id"] == "learning")
        self.assertIn("watch_center", [p["id"] for p in learning["panels"]])

    def test_public_adapters_normalize_rank_names_and_quote_volume(self):
        with patch.object(popularity, "_request", side_effect=[
                {"data": [{"sc": "600001", "rk": 1}]},
                {"data": {"diff": [{"f12": "600001", "f14": "测试股"}]}}]) as request:
            rows = popularity.fetch_eastmoney()
        self.assertEqual(rows, [{"code": "600001", "name": "测试股", "rank": 1}])
        self.assertEqual(request.call_count, 2)

        fields = [""] * 39
        for i, value in {1: "测试股", 3: "10", 6: "1234", 30: "20261008100000", 32: "1"}.items():
            fields[i] = value
        with patch("chaogu.net.get_text", return_value='v_sz000001="' + "~".join(fields) + '";'):
            quote = tencent.fetch_quotes(["000001"])[0]
        self.assertEqual(quote["volume"], 1234)


    def test_flow_rule_triggers_in_and_out_and_builds_board(self):
        watch.save_rule({"kind": "flow", "name": "资金异动", "direction": "both",
                         "metric": "main_net", "min_amount": 1e8, "min_ratio": 3.0},
                        path=self.path)
        rows = [
            {"code": "600519", "name": "贵州茅台", "price": 1300, "pct": 1.6,
             "amount": 2.8e9, "main_net": 4.1e8, "main_ratio": 14.6},
            {"code": "000001", "name": "平安银行", "price": 10, "pct": -2.0,
             "amount": 1.5e9, "main_net": -2.5e8, "main_ratio": -16.0},
            {"code": "000002", "name": "万科A", "price": 8, "pct": 0.3,
             "amount": 5e7, "main_net": 3e6, "main_ratio": 6.0},
        ]
        result = watch.monitor_flow(path=self.path, fetcher=lambda: rows, at=self.at)
        self.assertEqual(result["state"], "ok")
        events = {e["code"]: e["observed"]["direction"] for e in watch.history(path=self.path)}
        self.assertEqual(events, {"600519": "in", "000001": "out"})
        board = watch.flow_board(path=self.path)
        self.assertEqual([r["code"] for r in board["inflow"]], ["600519", "000002", "000001"])
        self.assertEqual([r["code"] for r in board["outflow"]], ["000001", "000002", "600519"])

    def test_flow_rule_ratio_metric_and_direction_filter(self):
        watch.save_rule({"kind": "flow", "name": "仅净流入", "direction": "in",
                         "metric": "main_ratio", "min_amount": 1e9, "min_ratio": 5.0},
                        path=self.path)
        rows = [
            {"code": "600519", "name": "贵州茅台", "main_net": 1e8, "main_ratio": 10.0},
            {"code": "000001", "name": "平安银行", "main_net": -5e8, "main_ratio": -30.0},
            {"code": "000002", "name": "万科A", "main_net": 5e8, "main_ratio": 2.0},
        ]
        watch.monitor_flow(path=self.path, fetcher=lambda: rows, at=self.at)
        codes = {e["code"] for e in watch.history(path=self.path)}
        # 仅净流入 + 按占比阈值：只有 600519 命中（000002 占比不足，000001 是流出）。
        self.assertEqual(codes, {"600519"})

    def test_flow_rule_requires_valid_config(self):
        with self.assertRaises(ValueError):
            watch.save_rule({"kind": "flow", "direction": "sideways"}, path=self.path)


if __name__ == "__main__":
    unittest.main(verbosity=2)
