#!/usr/bin/env python3
"""全部使用模拟响应、临时数据库和本机 HTTP；不调用券商或行情服务。"""
import copy
from portfolio_fixtures import complete_report
import io
import json
import pathlib
import sqlite3
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from contextlib import redirect_stdout, redirect_stderr, closing
from http.server import ThreadingHTTPServer
from unittest.mock import patch

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
from chaogu import config, db, eastmoney as em, portfolio, portfolio_jobs, webapi, dashboard

SNAP = {"cash": 5000, "equity": 6100, "market_value": 1100, "pnl": 100,
        "positions": [{"code": "000001", "name": "测试股票", "quantity": 100, "sellable": 60,
                       "cost": 10, "price": 11, "value": 1100, "pnl": 100}]}
SECRET = "DO_NOT_PERSIST_SESSION_SECRET"


class FakeClient:
    def __init__(self):
        self.data = copy.deepcopy(SNAP)
        self.result = "authenticated"
        self.login_error = self.query_error = self.init_error = self.logout_error = None
        self.queries = self.logins = self.logouts = 0
        self.closed = False
        self.account = SECRET

    def initialize(self):
        if self.init_error:
            raise self.init_error
        return {"public_key": "test-public-key"}

    def captcha(self):
        return "data:image/png;base64,AAAA"

    def login(self, *args):
        self.logins += 1
        if self.login_error:
            raise self.login_error
        return self.result

    def sms(self, *args):
        pass

    def query(self):
        self.queries += 1
        if self.query_error:
            raise self.query_error
        return copy.deepcopy(self.data)

    def logout(self):
        self.logouts += 1
        if self.logout_error:
            raise self.logout_error

    def close(self):
        self.closed = True
        self.account = ""


class BrokerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self.tmp.name)
        self.patches = [patch("chaogu.research_data.refresh", return_value={"state":"complete"}),
                        patch("chaogu.research_data.status", return_value={"state":"complete"}),
                        patch.object(portfolio, "PATH", self.root / "portfolio.db"),
                        patch.object(em, "CONFIRMATION_PATH", self.root / "asset-confirmation.json"),
                        patch.object(config, "DB_PATH", self.root / "market.db")]
        for p in self.patches:
            p.start()
        self.market = db.init()
        self.clock = [100000., 1000.]
        self.client = FakeClient()
        self.session = em.Session(lambda: self.client, wall=lambda: self.clock[0], monotonic=lambda: self.clock[1])
        self.session_patch = patch.object(em, "session", self.session)
        self.session_patch.start()

    def tearDown(self):
        self.session_patch.stop()
        self.market.close()
        for p in reversed(self.patches):
            p.stop()
        self.tmp.cleanup()

    def advance(self, seconds, sleep=False):
        self.clock[0] += seconds
        if not sleep:
            self.clock[1] += seconds

    def login(self):
        init = self.session.initialize()
        data = {"flow": init["flow"], "account": SECRET, "encrypted_password": SECRET, "captcha": SECRET}
        result = self.session.login(data)
        self.assertEqual(data, {})
        return result

    def test_login_snapshot_confirmation_and_polling(self):
        self.assertTrue(self.login()["connected"])
        self.assertFalse(self.session.auto)
        self.advance(61); self.session.tick()
        self.assertEqual(self.client.queries, 1)
        self.session.confirm(self.session.snapshot_version)
        self.advance(60); self.session.tick()
        self.assertEqual(self.client.queries, 2)
        s = portfolio.connect()
        self.assertEqual(json.loads(s.execute("SELECT payload FROM broker_snapshot").fetchone()[0]), SNAP)
        s.close()
        with self.assertRaises(em.BrokerError):
            self.session.confirm("obsolete-snapshot")

    def test_invalid_login_not_retried_and_captcha_can_refresh(self):
        self.client.login_error = em.BrokerError("登录未通过，请核对验证码")
        r = self.login()
        self.assertEqual(r["state"], "ready")
        self.assertEqual(self.session.captcha({"flow": self.session.nonce})["image"], "data:image/png;base64,AAAA")
        self.advance(120); self.session.tick()
        self.assertEqual(self.client.logins, 1)
        self.assertEqual(self.client.queries, 0)

    def test_sms_never_reuses_password(self):
        self.client.result = "sms_required"
        r = self.login()
        self.assertEqual(r["state"], "sms_required")
        self.session.sms({"flow": self.session.nonce, "encrypted_mobile": SECRET, "code": "123456"})
        self.assertEqual(self.session.state, "ready")
        self.assertEqual(self.client.logins, 1)
        self.assertEqual(self.client.queries, 0)

    def test_unsupported_control_stops_login(self):
        self.client.init_error = em.Unsupported("官方要求不支持的安全控件")
        r = self.session.initialize()
        self.assertEqual(r["state"], "unsupported")
        self.assertTrue(self.client.closed)
        self.assertEqual(self.client.logins, 0)

    def test_idle_polling_does_not_extend_session(self):
        self.login(); self.session.confirm(self.session.snapshot_version)
        last = self.session.last_wall
        self.advance(86399); self.session.tick()
        self.assertEqual(self.session.last_wall, last)
        self.advance(1)
        self.assertFalse(self.session.activity()["connected"])
        self.assertTrue(self.client.closed)
        self.assertEqual(self.client.logouts, 1)

    def test_real_activity_extends_idle_and_sleep_resume_expires(self):
        self.login(); self.advance(80000); self.session.activity()
        self.advance(80000); self.session.tick()
        self.assertTrue(self.session.status()["connected"])
        self.advance(86400, sleep=True); self.session.tick()
        self.assertFalse(self.session.status()["connected"])

    def test_expiry_during_query_does_not_commit_snapshot(self):
        self.login()
        self.advance(86390)
        self.session.auto = True
        original = self.client.query
        def slow_query():
            self.advance(20, sleep=True)
            return original()
        self.client.query = slow_query
        with patch.object(portfolio, "save_broker_snapshot") as save:
            self.session.tick()
            save.assert_not_called()
        self.assertFalse(self.session.status()["connected"])

    def test_upstream_expiry_stops_queries_and_purges_local_snapshot(self):
        """会话失效后本机不再保留账户数据。

        这里原来叫 ..._retains_snapshot，断言"保留了上次快照"。那个行为被用户
        明确否掉了：保留会留下"已断开但金额和持仓还挂在界面上"的中间态，
        而且 data/portfolio.db 里一直躺着一份完整账户数据，退出登录等于没退出。
        """
        self.login(); self.session.confirm(self.session.snapshot_version)
        self.client.query_error = em.Expired("券商会话已失效")
        self.session.refresh()
        self.assertFalse(self.session.status()["connected"])
        self.advance(120); self.session.tick()
        self.assertEqual(self.client.queries, 2)
        self.assertFalse(portfolio.has_broker_data())
        s = portfolio.snapshot(self.market)
        self.assertEqual(s["data_source"], "manual")
        self.assertEqual(s["positions"], [])
        self.assertNotIn("last_successful_sync", s)      # 手动视图里不该再有券商同步信息

    def test_logout_purges_every_local_trace_of_the_account(self):
        """退出登录要删掉本机**全部**账户派生数据，不只是快照那一条。

        券商数据会散落到 6 张表：只清 broker_snapshot，AI 账户报告正文、
        逐次资产历史、按持仓代码缓存的行情都还留着，足以反推出持仓和资产规模。
        """
        self.login()
        c = portfolio.connect()
        c.execute("INSERT OR REPLACE INTO broker_observations VALUES(?,?,?,?,?)",
                  ("000001", 9.5, 13.0, 9.0, "研究理由"))
        c.execute("INSERT OR REPLACE INTO wb_reports VALUES(?,?,?,?)",
                  ("eastmoney", "2026-10-02T10:00:00+08:00", "sig",
                   '{"account":{"judgment":"最大风险点占25.35%"}}'))
        c.execute("INSERT OR REPLACE INTO wb_research VALUES(?,?,?,?)",
                  ("000001", '{"prices":{"rows":[]}}', "2026-10-02T10:00:00+08:00", None))
        c.execute("INSERT OR REPLACE INTO wb_research VALUES(?,?,?,?)",
                  ("benchmark:000300", '{"prices":{"rows":[]}}', "2026-10-02T10:00:00+08:00", None))
        c.execute("INSERT OR REPLACE INTO analysis VALUES(1,?,?,?)",
                  ("sig", "# 持仓研究清单", "2026-10-02T10:00:00+08:00"))
        c.execute("UPDATE account SET cash=1234 WHERE id=1")
        c.commit()
        # 记下清除前的手动账/设置行数，清除后必须一模一样
        kept = {t: c.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
                for t in ("account", "positions", "transactions", "wb_profile")}
        c.close()

        r = self.session.logout()
        self.assertTrue(r["purged"], "退出响应必须回显清掉了什么，否则用户以为数据凭空消失")
        self.assertGreaterEqual(sum(r["purged"].values()), 6)

        c = portfolio.connect()
        for t in ("broker_snapshot", "broker_observations", "wb_reports", "wb_history", "analysis"):
            self.assertEqual(c.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0], 0, t)
        # 公开基准行情与账户无关，保留；账户持仓代码必须清掉
        self.assertEqual([x[0] for x in c.execute("SELECT code FROM wb_research")],
                         ["benchmark:000300"])
        self.assertEqual(c.execute("SELECT source FROM portfolio_source WHERE id=1").fetchone()[0],
                         "manual")
        # 手动记账与用户自己的偏好设置不能受牵连
        for t, n in kept.items():
            self.assertEqual(c.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0], n, t)
        self.assertEqual(c.execute("SELECT cash FROM account WHERE id=1").fetchone()[0], 1234)
        c.close()
        self.assertFalse(portfolio.has_broker_data())

    def test_idle_timeout_and_crash_restart_also_leave_no_account_data(self):
        """超时和进程重启同样是会话结束，磁盘上不该留下账户数据。

        正常停止走 serve() 的 finally -> logout()，但 SIGKILL / 崩溃时 finally
        不执行，所以启动期还要用 purge_orphaned() 兜一次。
        """
        self.login()
        self.assertTrue(portfolio.has_broker_data())
        self.advance(86400, sleep=True); self.session.tick()
        self.assertFalse(portfolio.has_broker_data())

        self.login()                                     # 模拟被强杀后库里还留着数据
        self.assertTrue(portfolio.has_broker_data())
        with redirect_stdout(io.StringIO()):             # 启动清除会打一行提示，别污染测试输出
            self.assertIsNotNone(em.purge_orphaned())
        self.assertFalse(portfolio.has_broker_data())
        self.assertEqual(portfolio.snapshot(self.market)["data_source"], "manual")

    def test_orphan_purge_skips_manual_only_install(self):
        """纯手动记账的库里没有券商数据，启动清除不该顺手删掉他的 analysis 缓存。"""
        c = portfolio.connect()
        c.execute("INSERT OR REPLACE INTO analysis VALUES(1,?,?,?)",
                  ("sig", "手动账户的分析", "2026-10-02T10:00:00+08:00"))
        c.commit(); c.close()
        with redirect_stdout(io.StringIO()):
            self.assertIsNone(em.purge_orphaned())
        c = portfolio.connect()
        self.assertEqual(c.execute("SELECT COUNT(*) FROM analysis").fetchone()[0], 1)
        c.close()

    def test_logout_failure_clears_memory_and_restart_is_disconnected(self):
        self.login()
        self.client.logout_error = OSError(SECRET)
        r = self.session.logout()
        self.assertNotIn(SECRET, json.dumps(r))
        self.assertIn("未确认", r["query_error"])
        self.assertTrue(self.client.closed)
        self.assertIsNone(self.session.client)
        restarted = em.Session()
        self.assertFalse(restarted.status()["connected"])
        self.assertFalse(restarted.auto)

    def test_partial_failed_or_zero_query_cannot_clear_holdings(self):
        self.login()
        for value in [None, {}, {**SNAP, "positions": []}, {**SNAP, "cash": None},
                      {**SNAP, "positions": [dict(SNAP["positions"][0], sellable=101)]}]:
            self.client.data = value
            self.session.refresh()
            self.assertTrue(self.session.error)
            c = portfolio.connect()
            saved = json.loads(c.execute("SELECT payload FROM broker_snapshot").fetchone()[0]); c.close()
            self.assertEqual(saved, SNAP)
        # 真正的完整零持仓允许更新。
        self.client.data = {"cash": 6100, "equity": 6100, "market_value": 0, "pnl": 0, "positions": []}
        self.session.refresh()
        self.assertFalse(self.session.error)

    def test_manual_account_isolation_and_observations(self):
        portfolio.mutate({"kind": "account", "revision": 0, "cash": 9999, "max_single":30, "max_total":70, "monitoring":False})
        self.login()
        portfolio.broker_mutate({"kind":"source", "source":"eastmoney"})
        s = portfolio.snapshot(self.market)
        self.assertEqual(s["account"]["cash"], 5000)
        with self.assertRaises(ValueError):
            portfolio.mutate({"kind":"trade", "revision":1})
        portfolio.broker_mutate({"kind":"observation", "code":"000001", "stop_price":9, "target_price":12, "thesis":"观察题材"})
        self.session.refresh(); self.session.refresh()
        s = portfolio.snapshot(self.market)
        self.assertEqual(s["account"]["cash"], 5000)
        self.assertEqual(s["positions"][0]["stop_price"], 9)
        portfolio.broker_mutate({"kind":"source", "source":"manual"})
        s = portfolio.snapshot(self.market)
        self.assertEqual(s["account"]["cash"], 9999)
        self.assertEqual(len(s["transactions"]), 1)

    def test_mixed_securities_preserve_assets_and_limit_stock_research(self):
        other = dict(SNAP["positions"][0], code="510300", name="测试ETF", value=2200,
                     quantity=200, sellable=200, pnl=200)
        self.client.data = dict(SNAP, equity=8300, market_value=3300, pnl=300,
                                positions=[*SNAP["positions"], other])
        self.assertTrue(self.login()["snapshot_current_session"])
        portfolio.broker_mutate({"kind":"source", "source":"eastmoney"})
        s = portfolio.context(self.market)
        self.assertEqual(s["market_value"], 3300)
        self.assertEqual(s["account"]["cash"], 5000)
        self.assertEqual([p["code"] for p in s["positions"]], ["000001"])
        self.assertEqual(s["other_positions"], [])
        self.assertEqual(s["fund_positions"][0]["value"], 2200)
        self.assertEqual(s["other_market_value"], 0)
        self.assertEqual(s["positions"][0]["add_budget"], round(max(0, min(
            5000, 8300*s["account"]["max_single"]/100-1100,
            8300*s["account"]["max_total"]/100-3300)), 2))
        self.assertEqual([p["code"] for p in portfolio.observations(s)], ["000001", "510300"])
        payload = portfolio.analysis_payload(s)
        self.assertEqual(payload["other_positions"], [])
        self.assertEqual(payload["fund_positions"][0]["value"], 2200)
        before = portfolio._signature(s)
        s["fund_positions"][0]["quantity"] += 1
        self.assertNotEqual(portfolio._signature(s), before)

    def test_bad_security_and_duplicate_rows_keep_snapshot_and_connection(self):
        self.login()
        for rows, expected in [
            ([dict(SNAP["positions"][0], code=SECRET)], "第 1 条"),
            ([dict(SNAP["positions"][0], name=" ")], "名称缺失"),
            (SNAP["positions"] * 2, "同代码多条")]:
            self.client.data = dict(SNAP, positions=rows)
            self.session.refresh()
            self.assertIn(expected, self.session.error)
            self.assertNotIn(SECRET, self.session.error)
            self.assertTrue(self.session.status()["connected"])
            self.assertFalse(self.client.closed)
            with closing(portfolio.connect()) as c:
                self.assertEqual(json.loads(c.execute("SELECT payload FROM broker_snapshot").fetchone()[0]), SNAP)
        self.client.data = copy.deepcopy(SNAP)
        self.session.refresh()
        self.assertFalse(self.session.error)

    def test_mismatch_diagnostics_are_financial_only_and_memory_only(self):
        raw = copy.deepcopy(SNAP)
        raw["market_value"] = 1200
        raw["account_number"] = SECRET
        raw["positions"][0].update(cookie=SECRET, shareholder=SECRET)
        self.client.data = raw
        state = self.login()
        diagnostic = state["query_diagnostics"]
        self.assertTrue(state["connected"])
        self.assertFalse(state["snapshot_current_session"])
        self.assertEqual(diagnostic["difference"], 100)
        self.assertEqual(diagnostic["row_count"], 1)
        self.assertEqual(diagnostic["positions"], [{"code":"000001", "quantity":100, "price":11, "value":1100}])
        self.assertNotIn(SECRET, json.dumps(diagnostic))
        with closing(portfolio.connect()) as c:
            self.assertIsNone(c.execute("SELECT * FROM broker_snapshot").fetchone())
        self.client.data = copy.deepcopy(SNAP)
        self.assertIsNone(self.session.refresh()["query_diagnostics"])
        self.client.data = raw
        self.session.refresh()
        self.assertIsNotNone(self.session.status()["query_diagnostics"])
        self.assertIsNone(self.session.logout()["query_diagnostics"])

    def test_other_securities_only_are_not_reported_as_empty_account(self):
        self.client.data["positions"][0].update(code="113001", name="测试转债")
        self.assertTrue(self.login()["snapshot_current_session"])
        portfolio.broker_mutate({"kind":"source", "source":"eastmoney"})
        s = portfolio.snapshot(self.market)
        self.assertTrue(s["complete"])
        self.assertEqual(s["positions"], [])
        self.assertEqual(len(s["other_positions"]), 1)
        self.assertEqual(s["market_value"], 1100)
        with patch("chaogu.llm.available", return_value=True), patch("chaogu.llm.chat") as chat:
            worker = portfolio_jobs.start(self.session, self.session.nonce); worker.join(3)
            self.assertEqual(portfolio_jobs.status()["state"], "empty")
            self.assertIn("暂无可分析", portfolio_jobs.status()["message"])
            chat.assert_not_called()

    def test_confirmed_cash_management_reconciles_without_double_counting(self):
        self.client.data = dict(SNAP, cash=5100, equity=6200, market_value=6100)
        state = self.login()
        self.assertTrue(state["can_reconcile"])
        with self.assertRaises(em.BrokerError):
            self.session.reconcile("stale-version")
        state = self.session.reconcile(state["diagnostic_version"])
        self.assertTrue(state["snapshot_current_session"])
        self.assertFalse(state["query_error"])
        portfolio.broker_mutate({"kind":"source", "source":"eastmoney"})
        s = portfolio.snapshot(self.market)
        parts = {x["kind"]:x["value"] for x in s["allocation"]["items"]}
        self.assertEqual(parts, {"stocks":1100,"funds":0,"cash_management":5000,"other":0,"balance":100})
        self.assertEqual(sum(parts.values()), 6200)
        self.assertEqual(s["account"]["cash"], 5100)
        self.assertAlmostEqual(s["exposure"], 1100/6200*100, places=2)
        self.assertEqual(s["positions"][0]["add_budget"], 760)
        with closing(portfolio.connect()) as c:
            saved = json.loads(c.execute("SELECT payload FROM broker_snapshot").fetchone()[0])
        self.assertEqual(em.validate_snapshot(saved), saved)
        self.assertEqual(saved["extra_assets"][0]["source"], "user_confirmed_difference")
        self.assertNotIn(SECRET, em.CONFIRMATION_PATH.read_text())
        # 只允许价格变化；金额变化或持仓遗漏必须再次核对。
        self.client.data = copy.deepcopy(self.client.data)
        self.client.data["positions"][0].update(price=12,value=1200)
        self.client.data.update(equity=6300,market_value=6200)
        self.assertFalse(self.session.refresh()["query_error"])
        self.client.data["positions"] = []
        self.assertTrue(self.session.refresh()["query_error"])
        self.assertEqual(len(portfolio.snapshot(self.market)["positions"]), 1)

    def test_new_difference_needs_confirmation_and_rechecks_latest_query(self):
        self.client.data = dict(SNAP, equity=7100,market_value=2100)
        state = self.login()
        self.client.data = dict(SNAP, equity=7200,market_value=2200)
        # 用户点按钮之前金额已经变化：本次不得发布旧候选。
        state = self.session.reconcile(state["diagnostic_version"])
        self.assertFalse(state["snapshot_current_session"])
        self.assertTrue(state["query_error"])
        state = self.session.reconcile(state["diagnostic_version"])
        self.assertTrue(state["snapshot_current_session"])
        self.assertIsNone(state["query_diagnostics"])

    def test_fund_only_sector_evidence_and_analysis(self):
        self.client.data["positions"][0].update(code="159999", name="半导体ETF")
        self.login(); portfolio.broker_mutate({"kind":"source","source":"eastmoney"})
        date = portfolio.now().strftime("%Y-%m-%d")
        self.market.execute("INSERT INTO daily_market(trade_date) VALUES(?)", (date,))
        self.market.execute("INSERT INTO sector_daily(trade_date,code,name,pct) VALUES(?,?,?,?)",
                            (date,"BK0001","半导体",2.5))
        self.market.commit()
        s = portfolio.context(self.market)
        self.assertEqual(s["positions"], [])
        self.assertTrue(portfolio.has_analyzable_assets(s))
        self.assertEqual(s["fund_positions"][0]["sectors"], ["半导体"])
        self.assertEqual(s["fund_positions"][0]["sector_evidence"][0]["pct"], 2.5)
        obs = portfolio.observations(s)[0]
        self.assertNotIn("T+1", obs["exit"])
        self.assertIn("基金合同", str(obs["facts"]))
        with patch("chaogu.llm.available", return_value=True), patch("chaogu.llm.chat", side_effect=complete_report) as chat:
            worker = portfolio_jobs.start(self.session,self.session.nonce); worker.join(3)
            self.assertEqual(portfolio_jobs.status()["state"], "complete")
            sent = json.loads(chat.call_args.args[1])
            self.assertEqual(sent["fund_positions"][0]["sectors"], ["半导体"])
            self.assertEqual(sent["allocation"]["items"][1]["value"], 1100)
        self.session.refresh()
        self.assertFalse(portfolio.analysis(self.market)["stale"])

    def test_cash_management_only_can_generate_allocation_analysis(self):
        self.client.data = {"cash":1000,"equity":1000,"market_value":1000,"pnl":0,"positions":[]}
        state = self.login()
        self.session.reconcile(state["diagnostic_version"])
        portfolio.broker_mutate({"kind":"source","source":"eastmoney"})
        with patch("chaogu.llm.available",return_value=True),patch("chaogu.llm.chat", side_effect=complete_report):
            result = portfolio.analysis(self.market,generate=True)
        self.assertTrue(result["can_generate"])
        self.assertEqual(result["observations"], [])
        self.assertEqual(result["allocation"]["items"][2]["value"],1000)

    def test_unknown_and_cross_border_fund_do_not_invent_sector_exposure(self):
        from chaogu.portfolio_research import enrich_funds
        today = portfolio.now().strftime("%Y-%m-%d")
        self.market.execute("INSERT INTO sector_daily(trade_date,code,name,pct) VALUES(?,?,?,?)",
                            (today,"BK0002","创新药",3))
        self.market.commit()
        for name, expected in [("未识别ETF", []), ("港股创新药ETF", ["医药医疗", "港股"])]:
            s = {"today":today,"fund_positions":[{"name":name}]}
            enrich_funds(self.market,s)
            fund = s["fund_positions"][0]
            self.assertEqual(fund["sectors"],expected)
            self.assertEqual(fund["sector_evidence"],[])
            self.assertTrue(fund["fund_risks"])

    def test_negative_mismatch_cannot_be_classified_as_cash_management(self):
        self.client.data = dict(SNAP,market_value=1000)
        state = self.login()
        self.assertFalse(state["can_reconcile"])
        with self.assertRaises(em.BrokerError):
            self.session.reconcile(state["diagnostic_version"])
        self.assertFalse(em.CONFIRMATION_PATH.exists())

    def test_migration_uses_sqlite_backup(self):
        target = self.root / "legacy.db"
        with closing(sqlite3.connect(target)) as c, c:
            c.executescript(portfolio.SCHEMA)
            c.execute("UPDATE account SET cash=1234 WHERE id=1")
        c = portfolio.connect(target); c.close()
        backups = list(self.root.glob("legacy.db.before-eastmoney-*.bak"))
        self.assertEqual(len(backups), 1)
        with closing(sqlite3.connect(backups[0])) as c:
            self.assertEqual(c.execute("SELECT cash FROM account").fetchone()[0], 1234)
            self.assertIsNone(c.execute("SELECT name FROM sqlite_master WHERE name='portfolio_source'").fetchone())
        c = portfolio.connect(target); c.close()
        self.assertEqual(len(list(self.root.glob("legacy.db.before-eastmoney-*.bak"))), 1)

    def test_credentials_never_in_database_logs_or_ai(self):
        self.client.data["account_number"] = SECRET
        self.client.data["positions"][0]["cookie"] = SECRET
        output = io.StringIO()
        with redirect_stdout(output), redirect_stderr(output):
            self.login()
            portfolio.broker_mutate({"kind":"source", "source":"eastmoney"})
            with patch("chaogu.llm.available", return_value=True), patch("chaogu.llm.chat", side_effect=complete_report) as chat:
                portfolio.analysis(self.market)
                chat.assert_not_called()
                portfolio.analysis(self.market, generate=True)
                self.assertNotIn(SECRET, str(chat.call_args))
        self.assertNotIn(SECRET, output.getvalue())
        for path in self.root.glob("portfolio.db*"):
            self.assertNotIn(SECRET.encode(), path.read_bytes())
        self.assertNotIn(SECRET, json.dumps(self.session.status()))

    def test_changed_snapshot_invalidates_ai(self):
        self.login(); portfolio.broker_mutate({"kind":"source", "source":"eastmoney"})
        with patch("chaogu.llm.available", return_value=True), patch("chaogu.llm.chat", side_effect=complete_report):
            portfolio.analysis(self.market, generate=True)
        self.client.data["cash"] = 4000
        self.client.data["equity"] = 5100
        self.session.refresh()
        self.assertTrue(portfolio.analysis(self.market)["stale"])

    def test_auto_analysis_is_once_after_first_complete_sync(self):
        init = self.session.initialize()
        self.client.query_error = em.BrokerError("模拟部分返回")
        with patch.object(portfolio_jobs, "start") as start:
            body = json.dumps({"flow":init["flow"], "auto_analyze":True})
            webapi.handle_post("/api/eastmoney/login", body)
            start.assert_not_called()
            self.client.query_error = None
            webapi.handle_post("/api/eastmoney/refresh", "{}")
            start.assert_called_once_with(self.session, self.session.nonce)
            webapi.handle_post("/api/eastmoney/refresh", "{}")
            webapi.handle_post("/api/eastmoney/activity", "{}")
            start.assert_called_once()
        self.assertEqual(portfolio.snapshot(self.market)["data_source"], "eastmoney")

    def test_auto_analysis_opt_out(self):
        init = self.session.initialize()
        with patch.object(portfolio_jobs, "start") as start:
            webapi.handle_post("/api/eastmoney/login", json.dumps({"flow":init["flow"], "auto_analyze":False}))
            start.assert_not_called()
        self.assertIsNone(self.session.claim_analysis())

    def test_auto_analysis_background_result_and_credentials_excluded(self):
        self.login()
        portfolio.broker_mutate({"kind":"source", "source":"eastmoney"})
        with patch("chaogu.llm.available", return_value=True), patch("chaogu.llm.chat", side_effect=complete_report) as chat:
            worker = portfolio_jobs.start(self.session, self.session.nonce)
            worker.join(3)
            self.assertFalse(worker.is_alive())
            self.assertEqual(portfolio_jobs.status()["state"], "complete")
            chat.assert_called_once()
            sent = str(chat.call_args)
            self.assertNotIn(SECRET, sent)
            self.assertNotIn(self.session.nonce, sent)
        self.assertTrue(portfolio.analysis(self.market)["report"]["complete"])
        self.session.refresh()
        self.assertFalse(portfolio.analysis(self.market)["stale"])

    def test_auto_analysis_does_not_publish_after_logout_or_source_switch(self):
        for change in (lambda:self.session.logout(), lambda:portfolio.broker_mutate({"kind":"source", "source":"manual"})):
            self.login()
            portfolio.broker_mutate({"kind":"source", "source":"eastmoney"})
            entered, finish = threading.Event(), threading.Event()
            def model(*args, **kwargs):
                entered.set()
                finish.wait(3)
                return complete_report(*args, **kwargs)
            with patch("chaogu.llm.available", return_value=True), patch("chaogu.llm.chat", side_effect=model):
                worker = portfolio_jobs.start(self.session, self.session.nonce)
                self.assertTrue(entered.wait(3))
                change()
                finish.set(); worker.join(3)
                self.assertFalse(worker.is_alive())
            c = portfolio.connect()
            self.assertIsNone(c.execute("SELECT * FROM wb_reports").fetchone()); c.close()
            self.assertEqual(portfolio_jobs.status()["state"], "cancelled")

    def test_auto_analysis_unconfigured_or_empty_skips_model(self):
        self.login(); portfolio.broker_mutate({"kind":"source", "source":"eastmoney"})
        with patch("chaogu.llm.available", return_value=False), patch("chaogu.llm.chat") as chat:
            worker = portfolio_jobs.start(self.session, self.session.nonce); worker.join(3)
            self.assertEqual(portfolio_jobs.status()["state"], "unavailable")
            chat.assert_not_called()
        self.client.data = {"cash":6100,"equity":6100,"market_value":0,"pnl":0,"positions":[]}
        self.session.refresh()
        with patch("chaogu.llm.available", return_value=True), patch("chaogu.llm.chat") as chat:
            worker = portfolio_jobs.start(self.session, self.session.nonce); worker.join(3)
            self.assertEqual(portfolio_jobs.status()["state"], "empty")
            chat.assert_not_called()

    def test_product_page_redirect_does_not_expire_valid_account(self):
        self.login()
        with patch.object(self.client, "product_capabilities", create=True,
                          side_effect=em.Redirected(False)):
            result=self.session.product_capabilities()
        self.assertFalse(result["supported"])
        self.assertTrue(self.session.status()["connected"])
        self.assertFalse(self.client.closed)

    def test_old_login_window_is_rejected(self):
        first = self.session.initialize()
        self.session.initialize()
        with self.assertRaises(em.BrokerError):
            self.session.login({"flow":first["flow"]})
        self.assertEqual(self.client.logins, 0)

    def test_http_same_origin_and_host_checks_before_reading_credentials(self):
        server = ThreadingHTTPServer(("127.0.0.1", 0), dashboard._Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
        origin = f"http://127.0.0.1:{server.server_port}"
        try:
            with patch.object(self.session, "login", return_value={"ok":True}) as login:
                for headers in [{}, {"Origin":"https://evil.example"}, {"Origin":origin,"Host":"evil.example"},
                                {"Origin":origin,"Content-Type":"text/plain"}]:
                    req = urllib.request.Request(origin+"/api/eastmoney/login", data=b'{}',
                                                 headers={"Content-Type":"application/json", **headers})
                    with self.assertRaises(urllib.error.HTTPError) as error:
                        urllib.request.urlopen(req)
                    self.assertEqual(error.exception.code, 403)
                login.assert_not_called()
                req = urllib.request.Request(origin+"/api/eastmoney/login", data=b'{}',
                                             headers={"Content-Type":"application/json", "Origin":origin})
                with urllib.request.urlopen(req) as response:
                    self.assertEqual(json.load(response), {"ok":True})
                login.assert_called_once()
        finally:
            server.shutdown(); server.server_close(); thread.join()


class SnapshotTests(unittest.TestCase):
    def test_normalize_codes_and_separate_missing_name_diagnostic(self):
        for value in ("000001", " 000001 ", "sz000001", "SZ.000001", "000001.sz", 1):
            raw = copy.deepcopy(SNAP)
            raw["positions"][0].update(code=value, name=" 测试股票 ")
            self.assertEqual(em.validate_snapshot(raw), SNAP)
        for value in (True, None, "1", "123456789012", "sh000001.sz", SECRET):
            raw = copy.deepcopy(SNAP)
            raw["positions"][0]["code"] = value
            with self.assertRaisesRegex(em.BrokerError, "第 1 条持仓的证券代码"):
                em.validate_snapshot(raw)

    def test_reconciled_multiple_lots_use_weighted_cost_and_are_idempotent(self):
        first = dict(SNAP["positions"][0], code="sz000001")
        second = dict(first, code="000001.SZ", quantity=200, sellable=100, cost=8, value=2200, pnl=600)
        raw = dict(SNAP, equity=8300, market_value=3300, pnl=700, positions=[first, second])
        clean = em.validate_snapshot(raw)
        p = clean["positions"][0]
        self.assertEqual(len(clean["positions"]), 1)
        self.assertEqual((p["quantity"], p["sellable"], p["value"], p["pnl"]), (300, 160, 3300, 700))
        self.assertAlmostEqual(p["cost"], (100*10 + 200*8)/300)
        self.assertEqual(em.validate_snapshot(clean), clean)
        for changes in ({"name":"冲突名称"}, {"price":12}, {"code":"000001.SH"}):
            bad = dict(raw, positions=[first, dict(second, **changes)])
            with self.assertRaisesRegex(em.BrokerError, "不一致|标识冲突"):
                em.validate_snapshot(bad)

    def test_never_guess_deduplication_to_make_asset_total_match(self):
        with self.assertRaisesRegex(em.BrokerError, "同代码多条"):
            em.validate_snapshot(dict(SNAP, positions=SNAP["positions"]*2))
        # 完全相同的两笔记录也必须各计一次，并与券商总额一致。
        raw = dict(SNAP, equity=7200, market_value=2200, pnl=200, positions=SNAP["positions"]*2)
        self.assertEqual(em.validate_snapshot(raw)["positions"][0]["quantity"], 200)

    def test_other_securities_are_retained_and_missing_fields_are_not_zero(self):
        for code in ("510300", "159915", "113001", "204001"):
            row = dict(SNAP["positions"][0], code=code)
            clean = em.validate_snapshot(dict(SNAP, positions=[row]))
            self.assertEqual(clean["positions"], [row])
            self.assertEqual(em.validate_snapshot(clean), clean)
            del row["sellable"]
            with self.assertRaisesRegex(em.BrokerError, code + ".*sellable"):
                em.validate_snapshot(dict(SNAP, positions=[row]))


class ProtocolTests(unittest.TestCase):
    def test_authentication_sms_control_and_expiry_mapping(self):
        c = em.OfficialClient(); c.rand = "0.1"
        encrypted = "a"*171 + "="
        for response, expected in [({"Status":0}, "authenticated"), ({"Status":-1,"ErrCode":-11}, "sms_required")]:
            c.rand = "0.1"
            with patch.object(c, "_json", return_value=response):
                self.assertEqual(c.login("123456789012", encrypted, "1234"), expected)
        with patch.object(c, "_json", return_value={"Status":-1,"Message":"需要安全控件"}):
            c.rand = "0.1"
            with self.assertRaises(em.Unsupported):
                c.login("123456789012", encrypted, "1234")
        with patch.object(c, "_json", return_value={"Status":-1,"ErrCode":-13}) as auth:
            c.rand = "0.1"
            with self.assertRaisesRegex(em.Unsupported, "开户手机号"):
                c.login("123456789012", encrypted, "1234")
            self.assertEqual(auth.call_args[0][0], "/Login/AuthenticationSec")
        with patch.object(c, "_request", return_value=(b'{"Status":-2}', "application/json")):
            with self.assertRaises(em.Expired):
                c._json("/Com/queryAssetAndPositionV1", {})

    def test_query_contract_and_missing_fields_fail_closed(self):
        c = em.OfficialClient()
        page = '<input value="local-test-token" id="em_validatekey"><script src="/JsBundles/Position"></script>'
        # 回归：官方页面/脚本没有 API 地址字面量时，仍应真正查询固定只读 API。
        with patch.object(c, "_text", return_value=page) as fetch_page:
            c._query_contract()
            fetch_page.assert_called_once_with("/Search/Position")
        a = {"Status":0,"Data":[{"Kyzj":5000,"Zzc":6100,"Zxsz":1100,"positions":[
            {"Zqdm":"000001","Zqmc":"测试股票","Zqsl":100,"Kysl":60,
             "Cbjg":10,"Zxjg":11,"Zxsz":1100,"Ljyk":100}]}]}
        with patch.object(c, "_json", return_value=a):
            self.assertEqual(c.query(), SNAP)
            a["Data"][0]["PositionCount"] = 2
            with self.assertRaises(em.BrokerError):
                c.query()
            del a["Data"][0]["PositionCount"]
            del a["Data"][0]["positions"][0]["Kysl"]
            with self.assertRaises(em.BrokerError):
                c.query()
        with patch.object(c, "_text", return_value=page.replace(' id="em_validatekey"', '')):
            with self.assertRaises(em.Unsupported):
                c._query_contract()

    def test_official_initialization_rejects_changed_script_and_required_control(self):
        import hashlib
        script = "reviewed official fixture"
        page = '<script src="/JsBundles/BaseJS"></script><script src="/JsBundles/LoginSec"></script><script>var __secStage = "2";</script>'
        c = em.OfficialClient()
        with patch.object(c, "_text", side_effect=[page, script]):
            with self.assertRaisesRegex(em.Unsupported, "已更新"):
                c.initialize()
        digest = hashlib.sha256(script.encode()).hexdigest()
        with patch.object(em, "LOGIN_SCRIPT_SHA256", digest), patch("chaogu.eastmoney.platform.system", return_value="Windows"), patch.object(c, "_text", side_effect=[page, script]):
            with self.assertRaisesRegex(em.Unsupported, "安全控件"):
                c.initialize()

    def test_sms_error_does_not_echo_upstream_credentials_and_logout_redirect(self):
        c = em.OfficialClient(); c.account = "123456789012"; c.rand = "0.1"
        encrypted = "a"*171 + "="
        with patch.object(c, "_json", return_value={"Status":-1,"Message":SECRET}):
            with self.assertRaises(em.BrokerError) as exc:
                c.sms(encrypted, "123456")
            self.assertNotIn(SECRET, str(exc.exception))
        with patch.object(c, "_request", side_effect=em.Redirected(False)):
            with self.assertRaises(em.BrokerError):
                c.logout()
        with patch.object(c, "_request", side_effect=em.Redirected(True)):
            c.logout()

    def test_no_transaction_or_arbitrary_url_requests_and_tls_validation(self):
        import ssl
        c = em.OfficialClient()
        https = next(h for h in c.opener.handlers if isinstance(h, urllib.request.HTTPSHandler))
        self.assertEqual(https._context.verify_mode, ssl.CERT_REQUIRED)
        self.assertTrue(https._context.check_hostname)
        with patch.object(c.opener, "open") as network:
            for path in ("/Trade/Submit", "/Trade/Revoke", "/Transfer", "https://evil.example", "/Login/../Trade/Submit"):
                with self.assertRaises(em.Unsupported):
                    c._request(path, {})
            network.assert_not_called()
        with self.assertRaises(em.Expired):
            em._NoRedirect().redirect_request(None, None, 302, "", {}, "https://evil.example")


if __name__ == "__main__":
    unittest.main(verbosity=2)
