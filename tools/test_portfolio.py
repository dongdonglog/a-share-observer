#!/usr/bin/env python3
"""账户资金、T+1、并发校正、数据时效与 HTTP 接口自检；全部临时数据、无网络。"""
import json
from portfolio_fixtures import complete_report
import pathlib
import sys
import tempfile
import unittest
from datetime import datetime, timedelta
from unittest.mock import patch

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
from chaogu import config, db, llm, panels, portfolio, webapi
from chaogu.sources import tencent


class PortfolioTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = pathlib.Path(self.temp.name)
        self.patches = [patch.object(portfolio, "PATH", root / "portfolio.db"),
                        patch.object(config, "DB_PATH", root / "market.db"),
                        patch.object(panels, "DASH_PATH", root / "dashboards.json"),
                        patch.object(portfolio, "now", return_value=datetime(2026, 9, 30, 10, 0, tzinfo=portfolio.TZ)),
                        patch.object(llm, "available", return_value=False)]
        for p in self.patches:
            p.start()
        self.market = db.init()
        panels._CACHE.clear()

    def tearDown(self):
        self.market.close()
        for p in reversed(self.patches):
            p.stop()
        self.temp.cleanup()

    def snap(self):
        return portfolio.snapshot(self.market)

    def save(self, **data):
        data.setdefault("revision", self.snap()["account"]["revision"])
        return portfolio.mutate(data)

    def account(self, cash=10000, monitoring=False):
        self.save(kind="account", cash=cash, max_single=30, max_total=70, monitoring=monitoring)

    def position(self, **data):
        values = dict(kind="position", code="000001", name="测试股", quantity=100,
                      sellable=100, cost=10, stop_price=9, target_price=12, add_price=9.5)
        values.update(data)
        self.save(**values)

    def trade(self, **data):
        values = dict(kind="trade", code="000001", name="测试股", side="buy", quantity=100,
                      price=12, fees=5)
        values.update(data)
        self.save(**values)

    def quote(self, price=11, date="2026-09-30"):
        db.upsert_quotes(self.market, [dict(trade_date=date, code="000001", name="测试股", close=price)])

    def test_empty_account_unknown_cash(self):
        s = self.snap()
        self.assertIsNone(s["account"]["cash"])
        self.assertIsNone(s["equity"])
        self.assertEqual(s["positions"], [])
        self.assertEqual(portfolio.analysis(self.market)["observations"], [])

    def test_snapshot_does_not_double_charge_cash(self):
        self.account()
        self.position()
        self.quote()
        s = self.snap()
        self.assertEqual(s["account"]["cash"], 10000)
        self.assertEqual(s["equity"], 11100)
        self.assertEqual(s["pnl"], 100)
        self.assertFalse(s["positions"][0]["fresh"])

    def test_buy_cost_cash_and_t1_then_partial_sale(self):
        self.account()
        self.position()
        self.trade()
        s = self.snap()
        p = s["positions"][0]
        self.assertEqual(s["account"]["cash"], 8795)
        self.assertEqual(p["quantity"], 200)
        self.assertEqual(p["sellable"], 100)
        self.assertAlmostEqual(p["cost"], 11.025)
        self.trade(side="sell", quantity=100, price=13, fees=6)
        s = self.snap()
        self.assertEqual(s["account"]["cash"], 10089)
        self.assertEqual(s["positions"][0]["sellable"], 0)
        self.assertEqual(s["transactions"][0]["realized"], 191.5)
        with self.assertRaisesRegex(ValueError, "可卖"):
            self.trade(side="sell", quantity=1)
        with patch.object(portfolio, "now", return_value=portfolio.now()+timedelta(days=1)):
            self.assertEqual(self.snap()["positions"][0]["sellable"], 100)
            self.trade(side="sell", quantity=100, price=13, fees=6)
        self.assertEqual(self.snap()["positions"], [])

    def test_insufficient_cash_rolls_back_everything(self):
        self.account(cash=100)
        before = self.snap()
        with self.assertRaisesRegex(ValueError, "资金不足"):
            self.trade()
        self.assertEqual(self.snap(), before)

    def test_stale_revision_cannot_overwrite(self):
        self.account()
        before = self.snap()
        with self.assertRaisesRegex(ValueError, "其他页面"):
            self.save(kind="account", cash=2, max_single=30, max_total=70, monitoring=False, revision=0)
        self.assertEqual(self.snap(), before)

    def test_invalid_inputs_do_not_write(self):
        self.account()
        before = self.snap()
        for change in ({"quantity": 1.5}, {"cost": float("nan")}, {"cost": float("inf")},
                       {"quantity": -1}, {"sellable": 101}, {"code": "abc123"},
                       {"stop_price": 13}, {"quantity": True}):
            with self.subTest(change=change), self.assertRaises(ValueError):
                self.position(**change)
        self.assertEqual(self.snap(), before)

    def test_missing_price_disables_equity_and_budget(self):
        self.account()
        self.position()
        s = self.snap()
        self.assertFalse(s["complete"])
        self.assertIsNone(s["equity"])
        self.assertIsNone(s["positions"][0]["add_budget"])

    def test_budget_obeys_cash_and_concentration_limits(self):
        self.account(cash=1000)
        self.position(quantity=1000, sellable=1000)
        self.quote(price=10)
        self.assertEqual(self.snap()["positions"][0]["add_budget"], 0)
        self.position(quantity=100, sellable=100)
        self.account(cash=10000)
        self.assertEqual(self.snap()["positions"][0]["add_budget"], 2300)

    def test_monitor_requires_explicit_opt_in_and_preserves_source_time(self):
        self.position()
        with patch.object(tencent, "fetch_quotes") as fetch:
            portfolio.refresh_quotes()
            fetch.assert_not_called()
        self.account(monitoring=True)
        rows = [dict(code="000001", name="测试股", close=9, pct=-3, quote_at="2026-09-29T15:00:00")]
        with patch.object(tencent, "fetch_quotes", return_value=rows):
            portfolio.refresh_quotes()
        p = self.snap()["positions"][0]
        self.assertFalse(p["fresh"])
        self.assertEqual(p["quote_at"], "2026-09-29T15:00:00")
        self.quote(price=11)
        self.assertEqual(self.snap()["positions"][0]["price"], 11)
        with patch.object(tencent, "fetch_quotes", return_value=[]):
            with self.assertRaisesRegex(RuntimeError, "保留原快照"):
                portfolio.refresh_quotes()
        self.assertEqual(self.snap()["positions"][0]["price"], 11)

    def test_tencent_timestamp_is_source_time(self):
        fields = [""] * 39
        for i, v in {1:"测试股", 3:"10", 30:"20260930100000", 32:"1", 37:"2", 38:"3"}.items():
            fields[i] = v
        with patch("chaogu.net.get_text", return_value='v_sz000001="' + "~".join(fields) + '";'):
            q = tencent.fetch_quotes(["000001"])[0]
        self.assertEqual(q["quote_at"], "2026-09-30T10:00:00")
        self.assertEqual(q["close"], 10)

    def test_analysis_is_passive_and_invalidates_after_account_change(self):
        self.account()
        self.position()
        self.quote()
        with patch.object(llm, "chat") as chat:
            portfolio.analysis(self.market)
            chat.assert_not_called()
        with patch.object(llm, "available", return_value=True), patch.object(llm, "chat", side_effect=complete_report) as chat:
            r = portfolio.analysis(self.market, generate=True)
            self.assertFalse(r["stale"])
            payload = json.loads(chat.call_args.args[1])
            self.assertNotIn("transactions", payload)
            self.assertEqual(payload["account"]["cash"], 10000)
            self.account(cash=9999)
            self.assertTrue(portfolio.analysis(self.market)["stale"])

    def test_market_context_matches_held_stock_and_stale_date(self):
        self.account()
        self.position()
        self.quote()
        for code in ("000001", "000002"):
            db.upsert(self.market, "limit_up", [dict(code=code, trade_date="2026-09-29", name="测试股", height=1, reason="机器人")], ["trade_date", "code"])
        db.upsert(self.market, "theme_stock", [dict(code="000001", trade_date="2026-09-29", tag="机器人", name="测试股", reason="机器人", reason_info="催化原文")], ["trade_date", "tag", "code"])
        s = portfolio.context(self.market)
        self.assertEqual(s["market_date"], "2026-09-29")
        self.assertEqual(s["positions"][0]["mainlines"][0]["name"], "机器人")
        r = portfolio.observations(s)[0]
        self.assertIn("盘面不是今日数据", r["alerts"])
        self.assertTrue(any("机器人" in f for f in r["facts"]))

    def test_existing_dashboards_preserved_with_account_entry(self):
        existing = [{"id":"custom", "title":"我的布局", "panels":[], "builtin":False}]
        panels.DASH_PATH.write_text(json.dumps({"dashboards": existing}))
        ds = panels.load_dashboards()
        self.assertEqual([d["id"] for d in ds[:4]], ["information", "learning", "portfolio", "archive"])
        self.assertEqual(ds[4], existing[0])
        self.assertEqual(len(panels.load_dashboards()), 5)
        panels.DASH_PATH.write_text(json.dumps({"dashboards":[{"id":"archive","title":"旧复盘","builtin":True,"panels":[]}]}))
        archive = panels.load_dashboards()[3]
        self.assertEqual(archive["id"], "archive")
        self.assertIn("event_archive", [p["id"] for p in archive["panels"]])

    def test_http_validation_and_ai_panel_compilation_without_new_dashboard(self):
        r = webapi.handle_post("/api/portfolio", b"[]")
        self.assertEqual(r[0], 400)
        r = webapi.handle_post("/api/portfolio", b'{"kind":"trade","revision":0}')
        self.assertEqual(r[0], 400)
        r = webapi.handle_post("/api/ai/panels", json.dumps({"prompt":"分析我的持仓账户资金和补仓退出条件"}).encode())
        self.assertEqual(r[0], 200)
        result = json.loads(r[2])
        self.assertTrue({"portfolio_account", "portfolio_analysis"} <= {p["id"] for p in result["panels"]})
        self.assertFalse(panels.DASH_PATH.exists())
        for pid in ("portfolio_account", "portfolio_analysis"):
            r = webapi.handle_get("/api/panel", f"id={pid}&date=2020-01-01")
            self.assertEqual(r[0], 200)

    def test_invalid_ai_spec_falls_back_to_valid_catalog(self):
        with patch.object(llm, "available", return_value=True):
            for text in ('[]', '{"panels":[{"id":"invented"}]}',
                         '{"panels":[{"id":"portfolio_account","w":"oops"}]}'):
                with self.subTest(text=text), patch.object(llm, "chat", return_value=text):
                    s = panels.build_from_prompt("分析我的持仓")
                    self.assertEqual(s["how"], "rule")
                    self.assertTrue(s["error"])
                    self.assertTrue(all(p["id"] in panels.BY_ID for p in s["panels"]))


if __name__ == "__main__":
    unittest.main(verbosity=2)
