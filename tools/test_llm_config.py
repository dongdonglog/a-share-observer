#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""AI 设置的离线测试：配置视图脱敏、表单校验、失败归类、HTTP 路由、本机专属登记。

不读也不写用户真实的 data/llm.json——全程用临时路径 + 摘掉相关环境变量。

用法:
    python3 tools/test_llm_config.py
退出码 0 表示全部通过。
"""

from __future__ import annotations

import json
import os
import pathlib
import re
import sys
import tempfile
import unittest
import urllib.error
from unittest import mock

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from chaogu import dashboard, llm, webapi  # noqa: E402

REAL_KEY = "sk-0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcd"
ENV_NAMES = ("CHAOGU_LLM_BASE", "CHAOGU_LLM_KEY", "CHAOGU_LLM_MODEL", "CHAOGU_LLM_API",
             "ANTHROPIC_BASE_URL", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_API_KEY")


class _FakeResponse:
    """够 urlopen 上下文管理器用的最小替身。"""

    def __init__(self, text):
        self._body = text.encode("utf-8")

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class HttpFailure(urllib.error.HTTPError):
    """HTTPError 的 read() 要能吐出响应体，标准构造器喂不进去，这里包一层。"""

    def __init__(self, status, body):
        super().__init__("https://gw/v1/messages", status, "reason", {}, None)
        self._body = body.encode("utf-8")

    def read(self, *a):
        return self._body


class LlmEnvMixin:
    """临时配置路径 + 干净环境变量，避免碰用户真实的 data/llm.json。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._old_path = llm.CFG_PATH
        llm.CFG_PATH = pathlib.Path(self._tmp.name) / "llm.json"
        llm.last_error, llm.last_failure = None, None
        self._env = mock.patch.dict(os.environ, {}, clear=False)
        self._env.start()
        for n in ENV_NAMES:
            os.environ.pop(n, None)

    def tearDown(self):
        self._env.stop()
        llm.CFG_PATH = self._old_path
        self._tmp.cleanup()

    def _write(self, **over):
        cfg = {"base": "https://llm-gateway.example.com", "api": "anthropic",
               "model": "claude-sonnet-5-5", "key": REAL_KEY}
        cfg.update(over)
        llm.save_cfg(cfg)
        return cfg

    def _cfg(self, **over):
        cfg = {"base": "https://gw.example.com", "api": "anthropic", "model": "m", "key": "k"}
        cfg.update(over)
        return cfg


class LlmConfigTest(LlmEnvMixin, unittest.TestCase):
    # ------------------------------------------------------------ 脱敏
    def test_config_view_never_exposes_the_key(self):
        self._write()
        view = llm.config_view()
        blob = json.dumps(view, ensure_ascii=False)
        self.assertNotIn(REAL_KEY, blob)
        self.assertNotIn(REAL_KEY[:20], blob)          # 长前缀也不行
        self.assertTrue(view["has_key"])
        self.assertEqual(view["key_hint"], "sk-01234…abcd")
        self.assertEqual(view["key_hint"], llm.key_hint(REAL_KEY))
        self.assertTrue(view["available"])
        self.assertTrue(view["writable"])

    def test_key_hint_on_short_key_still_masks_the_middle(self):
        self.assertEqual(llm.key_hint("abc"), "…")
        self.assertEqual(llm.key_hint("a" * 11), "…")
        self.assertEqual(llm.key_hint("a" * 12), "aaa…aa")
        self.assertEqual(llm.key_hint("a" * 31), "aaa…aa")
        self.assertEqual(llm.key_hint("a" * 32), "aaaaaaaa…aaaa")
        self.assertEqual(llm.key_hint(""), "")

    def test_key_hint_never_shows_the_whole_key(self):
        """长度 ≤4 时前缀和后缀会重叠，是最容易写错成"全露"的地方。"""
        for n in (1, 2, 3, 4, 5, 6, 8, 11, 12, 20, 31, 32, 40, 67, 200):
            hint = llm.key_hint("k" * n)
            self.assertLess(len(hint.replace("…", "")), n,
                            f"长度 {n} 的 key 打码后仍被露全")

    def test_config_view_marks_env_override_as_not_writable(self):
        self._write()
        os.environ["CHAOGU_LLM_BASE"] = "https://from-env.example.com"
        view = llm.config_view()
        self.assertEqual(view["source"], "env")
        self.assertFalse(view["writable"])
        self.assertTrue(llm.env_override())

    def test_config_view_on_missing_file_is_honest(self):
        view = llm.config_view()
        self.assertFalse(view["available"])
        self.assertEqual(view["base"], "")
        self.assertEqual(view["key_hint"], "")

    # ------------------------------------------------------------ 表单校验
    def test_normalize_rejects_bad_input(self):
        for name, data in [
            ("空 base", {"base": ""}),
            ("非 http", {"base": "ftp://x.com"}),
            ("协议非法", {"base": "https://a.com", "api": "grpc"}),
            ("模型名带换行", {"base": "https://a.com", "model": "a\nb"}),
            ("key 带换行", {"base": "https://a.com", "key": "a\nb"}),
        ]:
            with self.subTest(name):
                cfg, err = llm.normalize_config(data)
                self.assertIsNone(cfg)
                self.assertTrue(err, name)

    def test_normalize_tidies_base_and_defaults_model(self):
        cfg, err = llm.normalize_config({"base": "  https://a.com/  "})
        self.assertIsNone(err)
        self.assertEqual(cfg["base"], "https://a.com")     # 去空白、去尾斜杠
        self.assertEqual(cfg["model"], llm.DEFAULT_MODEL)
        self.assertEqual(cfg["api"], "anthropic")

    def test_blank_key_keeps_the_saved_one(self):
        self._write()
        cfg, err = llm.normalize_config({"base": "https://new.example.com", "key": ""},
                                        current={"key": REAL_KEY})
        self.assertIsNone(err)
        self.assertEqual(cfg["key"], REAL_KEY)
        # key 字段整个不给，也应该沿用
        cfg2, _ = llm.normalize_config({"base": "https://new.example.com"},
                                       current={"key": REAL_KEY})
        self.assertEqual(cfg2["key"], REAL_KEY)

    def test_new_key_overrides_and_is_stripped(self):
        cfg, err = llm.normalize_config({"base": "https://a.com", "key": "  sk-new  "},
                                        current={"key": REAL_KEY})
        self.assertIsNone(err)
        self.assertEqual(cfg["key"], "sk-new")

    # ------------------------------------------------------------ 失败归类
    def test_http_status_is_classified(self):
        cases = [(401, "auth"), (403, "auth"), (404, "not_found"), (429, "rate_limit"),
                 (500, "upstream"), (502, "upstream")]
        for status, kind in cases:
            with self.subTest(status):
                with mock.patch("urllib.request.urlopen", side_effect=HttpFailure(status, "{}")):
                    self.assertIsNone(llm.chat("s", "u", _cfg=self._cfg()))
                self.assertEqual(llm.last_failure["kind"], kind)
                self.assertEqual(llm.last_failure["status"], status)

    def test_400_with_model_in_body_is_a_model_problem(self):
        body = '{"error":{"message":"model not found"}}'
        with mock.patch("urllib.request.urlopen", side_effect=HttpFailure(400, body)):
            llm.chat("s", "u", _cfg=self._cfg())
        self.assertEqual(llm.last_failure["kind"], "model")

    def test_400_without_model_is_a_generic_bad_request(self):
        with mock.patch("urllib.request.urlopen", side_effect=HttpFailure(400, '{"error":{}}')):
            llm.chat("s", "u", _cfg=self._cfg())
        self.assertEqual(llm.last_failure["kind"], "bad_request")

    def test_network_and_timeout_are_told_apart(self):
        with mock.patch("urllib.request.urlopen",
                        side_effect=urllib.error.URLError("nodename nor servname provided")):
            llm.chat("s", "u", _cfg=self._cfg())
        self.assertEqual(llm.last_failure["kind"], "network")

        with mock.patch("urllib.request.urlopen", side_effect=TimeoutError("timed out")):
            llm.chat("s", "u", _cfg=self._cfg())
        self.assertEqual(llm.last_failure["kind"], "timeout")

    def test_success_clears_the_failure(self):
        body = '{"content":[{"type":"text","text":"正常"}],"stop_reason":"end_turn"}'
        with mock.patch("urllib.request.urlopen", return_value=_FakeResponse(body)):
            self.assertEqual(llm.chat("s", "u", _cfg=self._cfg()), "正常")
        self.assertIsNone(llm.last_error)
        self.assertIsNone(llm.last_failure)

    def test_truncated_output_and_empty_output_are_distinguished(self):
        body = '{"content":[{"type":"text","text":"半句"}],"stop_reason":"max_tokens"}'
        with mock.patch("urllib.request.urlopen", return_value=_FakeResponse(body)):
            self.assertIsNone(llm.chat("s", "u", _cfg=self._cfg(), strict_complete=True))
        self.assertEqual(llm.last_failure["kind"], "incomplete")

        body = '{"content":[{"type":"text","text":"  "}],"stop_reason":"end_turn"}'
        with mock.patch("urllib.request.urlopen", return_value=_FakeResponse(body)):
            self.assertIsNone(llm.chat("s", "u", _cfg=self._cfg()))
        self.assertEqual(llm.last_failure["kind"], "empty")

    def test_openai_protocol_finish_reason_controls_strictness(self):
        body = '{"choices":[{"message":{"content":"好"},"finish_reason":"length"}]}'
        with mock.patch("urllib.request.urlopen", return_value=_FakeResponse(body)):
            self.assertIsNone(llm.chat("s", "u", _cfg=self._cfg(api="openai"),
                                       strict_complete=True))
        self.assertEqual(llm.last_failure["kind"], "incomplete")

    def test_unexpected_shape_is_reported_with_the_body(self):
        with mock.patch("urllib.request.urlopen", return_value=_FakeResponse("not json")):
            self.assertIsNone(llm.chat("s", "u", _cfg=self._cfg()))
        self.assertEqual(llm.last_failure["kind"], "upstream")
        self.assertIn("not json", llm.last_failure["detail"])

    def test_unconfigured_is_its_own_kind(self):
        self.assertIsNone(llm.chat("s", "u"))
        self.assertEqual(llm.last_failure["kind"], "unconfigured")

    def test_endpoint_is_built_per_protocol(self):
        seen = {}

        def grab(req, timeout=None):
            seen["url"] = req.full_url
            return _FakeResponse('{"content":[{"type":"text","text":"ok"}],"stop_reason":"end_turn"}')

        with mock.patch("urllib.request.urlopen", side_effect=grab):
            llm.chat("s", "u", _cfg=self._cfg(base="https://gw.example.com", api="anthropic"))
            self.assertEqual(seen["url"], "https://gw.example.com/v1/messages")
            llm.chat("s", "u", _cfg=self._cfg(base="https://gw.example.com", api="openai"))
            self.assertEqual(seen["url"], "https://gw.example.com/v1/chat/completions")
            # base 自带路径前缀时不要再拼一层
            llm.chat("s", "u", _cfg=self._cfg(base="https://gw.example.com/v1", api="openai"))
            self.assertEqual(seen["url"], "https://gw.example.com/v1/chat/completions")

    # ------------------------------------------------------------ 失败渲染
    def test_failure_md_gives_the_next_step_and_the_raw_body(self):
        with mock.patch("urllib.request.urlopen", side_effect=HttpFailure(401, '{"a":1}')):
            llm.chat("s", "u", _cfg=self._cfg())
        md = llm.failure_md()
        self.assertIn("AI 凭证无效或已过期", md)
        self.assertIn("重新签发 token", md)
        self.assertIn("HTTP 401", md)
        self.assertIn('{"a":1}', md)                 # 原始报文仍可核对

    def test_failure_md_neutralizes_backticks_so_markdown_survives(self):
        llm.last_failure = llm.failure("auth", "报文里有 ` 反引号 ` 会破坏行内代码")
        md = llm.failure_md()
        self.assertEqual(md.count("`"), 2)           # 只剩包裹用的那一对

    def test_failure_md_works_without_any_recorded_failure(self):
        llm.last_error, llm.last_failure = None, None
        self.assertIn("调用失败", llm.failure_md())

    # ------------------------------------------------------------ 连通性自检
    def test_test_connection_ok_shape(self):
        self._write()
        body = '{"content":[{"type":"text","text":"正常"}],"stop_reason":"end_turn"}'
        with mock.patch("urllib.request.urlopen", return_value=_FakeResponse(body)):
            r = llm.test_connection(timeout=5)
        self.assertTrue(r["ok"])
        self.assertIn("连通正常", r["message"])
        self.assertEqual(r["base"], "https://llm-gateway.example.com")

    def test_test_connection_reports_classified_failure(self):
        self._write()
        with mock.patch("urllib.request.urlopen", side_effect=HttpFailure(401, "{}")):
            r = llm.test_connection(timeout=5)
        self.assertFalse(r["ok"])
        self.assertEqual(r["kind"], "auth")
        self.assertEqual(r["status"], 401)
        self.assertTrue(r["action"])

    def test_test_connection_uses_draft_over_saved(self):
        self._write()
        seen = {}

        def grab(req, timeout=None):
            seen["url"] = req.full_url
            seen["auth"] = req.get_header("Authorization")
            return _FakeResponse('{"content":[{"type":"text","text":"正常"}],"stop_reason":"end_turn"}')

        with mock.patch("urllib.request.urlopen", side_effect=grab):
            llm.test_connection({"base": "https://draft.example.com/", "api": "openai",
                                 "model": "gpt-x", "key": "sk-draft"}, timeout=5)
        self.assertEqual(seen["url"], "https://draft.example.com/v1/chat/completions")
        self.assertEqual(seen["auth"], "Bearer sk-draft")

    def test_test_connection_without_any_base_says_unconfigured(self):
        r = llm.test_connection()
        self.assertFalse(r["ok"])
        self.assertEqual(r["kind"], "unconfigured")


class LlmRoutesTest(LlmEnvMixin, unittest.TestCase):
    """HTTP 路由层：真的不把 key 送出去，也不把草稿写进文件。"""

    def _get(self, path):
        code, _ctype, body = webapi.handle_get(path, "")
        return code, json.loads(body)

    def _post(self, path, payload):
        code, _ctype, body = webapi.handle_post(path, json.dumps(payload))
        return code, json.loads(body)

    def test_get_config_returns_masks_only(self):
        self._write()
        code, d = self._get("/api/llm/config")
        self.assertEqual(code, 200)
        self.assertNotIn(REAL_KEY, json.dumps(d, ensure_ascii=False))
        self.assertEqual(d["key_hint"], "sk-01234…abcd")

    def test_post_config_saves_and_locks_down_permissions(self):
        code, d = self._post("/api/llm/config", {
            "base": "https://new.example.com/", "api": "openai",
            "model": "gpt-4o", "key": "sk-brand-new"})
        self.assertEqual(code, 200)
        self.assertTrue(d["ok"])
        self.assertEqual(d["config"]["base"], "https://new.example.com")
        self.assertNotIn("sk-brand-new", json.dumps(d, ensure_ascii=False))
        on_disk = json.loads(llm.CFG_PATH.read_text("utf-8"))
        self.assertEqual(on_disk["key"], "sk-brand-new")
        self.assertEqual(on_disk["api"], "openai")
        if os.name != "nt":
            self.assertEqual(llm.CFG_PATH.stat().st_mode & 0o777, 0o600)

    def test_post_config_blank_key_keeps_the_stored_one(self):
        self._write()
        code, _ = self._post("/api/llm/config", {
            "base": "https://moved.example.com", "api": "anthropic",
            "model": "claude-sonnet-5-5", "key": ""})
        self.assertEqual(code, 200)
        on_disk = json.loads(llm.CFG_PATH.read_text("utf-8"))
        self.assertEqual(on_disk["key"], REAL_KEY)          # key 没被清空
        self.assertEqual(on_disk["base"], "https://moved.example.com")

    def test_post_config_rejects_invalid_input_without_touching_the_file(self):
        self._write()
        before = llm.CFG_PATH.read_text("utf-8")
        code, d = self._post("/api/llm/config", {"base": "not-a-url"})
        self.assertEqual(code, 400)
        self.assertIn("http", d["error"])
        self.assertEqual(llm.CFG_PATH.read_text("utf-8"), before)

    def test_post_config_refuses_when_env_would_win(self):
        self._write()
        os.environ["CHAOGU_LLM_BASE"] = "https://from-env.example.com"
        code, d = self._post("/api/llm/config", {"base": "https://file.example.com"})
        self.assertEqual(code, 409)
        self.assertIn("环境变量", d["error"])

    def test_post_config_rejects_non_json_object_body(self):
        code, _ctype, _body = webapi.handle_post("/api/llm/config", "{oops")
        self.assertEqual(code, 400)
        code, _ctype, _body = webapi.handle_post("/api/llm/config", "[1,2]")
        self.assertEqual(code, 400)

    def test_post_test_route_returns_the_classification(self):
        self._write()
        with mock.patch("urllib.request.urlopen", side_effect=HttpFailure(401, "{}")):
            code, d = self._post("/api/llm/test", {"base": "https://gw.example.com"})
        self.assertEqual(code, 200)
        self.assertFalse(d["ok"])
        self.assertEqual(d["kind"], "auth")

    def test_post_test_route_does_not_write_the_draft(self):
        self._write()
        body = '{"content":[{"type":"text","text":"正常"}],"stop_reason":"end_turn"}'
        with mock.patch("urllib.request.urlopen", return_value=_FakeResponse(body)):
            code, d = self._post("/api/llm/test", {"base": "https://draft.example.com",
                                                   "model": "draft-model"})
        self.assertEqual(code, 200)
        self.assertTrue(d["ok"])
        on_disk = json.loads(llm.CFG_PATH.read_text("utf-8"))
        self.assertEqual(on_disk["base"], "https://llm-gateway.example.com")   # 草稿没落盘


class PrivateApiGuardTest(unittest.TestCase):
    """能读本机敏感数据、或能改写本机文件的接口，都必须过同一道门。

    背景：PRIVATE_API_PREFIXES 是黑名单，没登记就默认放行。2026-10-13 实测
    POST /api/dashboards/reset 带着内网 Host 调用返回 200，一次误触就把用户
    的自定义看板全删了。所以这里不只是列出几条断言，而是**扫源码**兜住
    "新写了 POST/DELETE 接口却忘了登记"这件事。
    """

    ROOT = pathlib.Path(__file__).resolve().parent.parent

    def test_llm_routes_are_registered_as_local_only(self):
        for p in ("/api/llm/config", "/api/llm/test", "/api/llm/anything"):
            with self.subTest(p):
                self.assertTrue(dashboard._is_private_api(p), f"{p} 没登记，会裸奔")

    def test_existing_private_routes_still_registered(self):
        for p in ("/api/eastmoney/status", "/api/portfolio", "/api/news-impact/x",
                  "/api/watch/status", "/api/panel?id=watch_center",
                  "/api/panel?id=portfolio_account"):
            with self.subTest(p):
                self.assertTrue(dashboard._is_private_api(p))

    def test_dashboard_and_ai_write_routes_are_registered(self):
        """看板写接口能删改本机配置，AI 编排接口能烧用户的模型额度。"""
        for p in ("/api/dashboards", "/api/dashboards/reset",
                  "/api/dashboards/order", "/api/dashboards/duplicate",
                  "/api/dashboards/import/preview", "/api/dashboards/export?id=x",
                  "/api/ai/build", "/api/ai/panels"):
            with self.subTest(p):
                self.assertTrue(dashboard._is_private_api(p),
                                f"{p} 没登记，任何能连上端口的人都能调用")

    def test_every_write_route_is_registered_as_local_only(self):
        """扫 webapi.py 的 handle_post / handle_delete，逐条要求已登记。

        新增写接口忘了加进 PRIVATE_API_PREFIXES 时，这个用例会先红，
        不必靠人记得去看注释。
        """
        src = (self.ROOT / "chaogu" / "webapi.py").read_text(encoding="utf-8")
        bodies = []
        for fn in ("handle_post", "handle_delete"):
            m = re.search(rf"^def {fn}\(.*?\):\n(.*?)(?=^def |\Z)", src, re.S | re.M)
            self.assertIsNotNone(m, f"在 webapi.py 里找不到 {fn}，正则需要更新")
            bodies.append(m.group(1))
        routes = set()
        for chunk in bodies:
            routes |= set(re.findall(r'"(/api/[^"]*)"', chunk))
        # 正则失效会扫出 0 条，那样这个用例就成了空断言，先把这种情况断掉
        self.assertGreater(len(routes), 5, f"只扫到 {routes}，扫描逻辑可能已失效")
        for p in sorted(routes):
            with self.subTest(p):
                self.assertTrue(
                    dashboard._is_private_api(p),
                    f"{p} 是写接口但没登记进 PRIVATE_API_PREFIXES，"
                    f"任何能连上端口的人都能调用")

    def test_ai_panels_are_local_only(self):
        """AI 面板内容是市场数据，但打开一次就真实调用一次模型、花用户的钱。

        绑 0.0.0.0 之后这个尤其要紧：别人只要点一下标签页就在消耗你的额度。
        """
        for p in ("/api/panel?id=ai_brief", "/api/panel?id=ai_answer"):
            with self.subTest(p):
                self.assertTrue(dashboard._is_private_api(p),
                                f"{p} 没登记，别人打开看板就会烧你的模型额度")
        # 行情类面板是公开的，别连带挡掉
        for p in ("/api/panel?id=market_stats", "/api/panel?id=verdict",
                  "/api/panel?id=news"):
            with self.subTest(p):
                self.assertFalse(dashboard._is_private_api(p))

    def test_public_routes_are_not_dragged_in(self):
        for p in ("/api/meta", "/api/panel?id=news", "/api/stock?code=1",
                  "/api/data", "/static/app.js", "/"):
            with self.subTest(p):
                self.assertFalse(dashboard._is_private_api(p),
                                 f"{p} 被误判成本机专属，会连带挡掉正常访问")


if __name__ == "__main__":
    unittest.main(verbosity=2)
