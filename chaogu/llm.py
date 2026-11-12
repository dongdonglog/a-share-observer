#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""LLM 客户端（纯标准库）：给 AI 看板、AI 解读用。

配置来源（前者优先）：
  1. 环境变量 CHAOGU_LLM_BASE / CHAOGU_LLM_KEY / CHAOGU_LLM_MODEL /
     CHAOGU_LLM_API（anthropic | openai）
  2. data/llm.json（`python3 run.py llm-setup` 或看板「⋯ → AI 设置」生成，权限 600）
  3. 环境变量 ANTHROPIC_BASE_URL + ANTHROPIC_AUTH_TOKEN / ANTHROPIC_API_KEY

为什么要落盘配置：看板由 launchd 开机自启时拿不到 shell 的环境变量，
只读环境变量的话自启后的看板永远是"AI 未配置"。

没有配置时所有调用返回 None，看板退回规则版（不会报错）。

**不要在界面上直接抛原始报文。** 网关的 401 长这样
`{"error":{"code":"","message":"Invalid token","type":"new_api_error"}}`——
用户看不懂，也看不出下一步该做什么。所以失败统一走 FAILURE_KINDS 归类成
「哪里断了 + 下一步做什么」，原始报文留在 detail 里供核对。
"""

from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.request

from . import config

CFG_PATH = config.DATA_DIR / "llm.json"
DEFAULT_MODEL = "claude-sonnet-5-5"

# 失败归类：(界面标题, 下一步该做什么)。key 是给程序看的稳定标识。
FAILURE_KINDS = {
    "unconfigured": ("AI 未配置", "打开看板右上「⋯ → AI 设置」填写接口地址与 Key"),
    "auth": ("AI 凭证无效或已过期", "到网关控制台重新签发 token，再回看板「⋯ → AI 设置」更新"),
    "model": ("网关不认识这个模型名", "在「⋯ → AI 设置」里核对模型名（以网关自己的模型列表为准）"),
    "not_found": ("接口地址不对", "检查 base 地址；anthropic 协议会自动拼 /v1/messages"),
    "bad_request": ("网关拒绝了这次请求", "核对模型名与 base 地址是否匹配这个网关"),
    "rate_limit": ("被网关限流", "稍后重试；持续出现请查网关的额度与分组设置"),
    "upstream": ("网关或上游模型报错", "稍后重试；持续出现请查网关日志"),
    "timeout": ("请求超时", "网络较慢或模型排队中，稍后重试"),
    "network": ("连不上网关", "检查 base 地址、本机代理与网络是否可达"),
    "incomplete": ("模型输出被截断", "重试一次；持续出现请调小输入或换模型"),
    "empty": ("模型返回了空内容", "重试一次"),
    "unknown": ("调用失败", "展开原始报文核对"),
}


def key_hint(key):
    """只回显够辨认的位数，永不回显明文。

    长度不够时整条打码——`key[:2] + "…" + key[-2:]` 在长度 ≤4 时会把原文原样拼回来，
    等于没打码。展示的位数也随长度收敛，避免短 key 被露掉一半。
    """
    key = str(key or "")
    if not key:
        return ""
    if len(key) < 12:
        return "…"
    head, tail = (8, 4) if len(key) >= 32 else (3, 2)
    return f"{key[:head]}…{key[-tail:]}"


def failure(kind, detail="", status=None):
    """把一次失败归一成结构化结果，界面直接拿 message / action 显示。"""
    msg, action = FAILURE_KINDS.get(kind, FAILURE_KINDS["unknown"])
    return {"kind": kind, "message": msg, "action": action, "status": status,
            "detail": str(detail or "")[:400]}


def failure_md():
    """把最近一次失败渲染成面板能直接显示的 Markdown（前端会转义，别塞 HTML）。"""
    f = last_failure or failure("unknown", last_error)
    detail = str(f.get("detail") or "").replace("`", "'").replace("\n", " ").strip()
    md = f"**AI 调用失败 · {f['message']}**\n\n{f['action']}。\n"
    if f.get("status"):
        md += f"\n来自网关的 HTTP {f['status']}。\n"
    if detail:
        md += f"\n原始报文：`{detail[:300]}`\n"
    return md


def _body(exc):
    """HTTPError 的响应体是失败原因的唯一线索，读不到就返回空串。"""
    try:
        return exc.read().decode("utf-8", "replace")[:400]
    except Exception:                                  # noqa: BLE001
        return ""


def _env_source():
    """配置实际来自哪里——环境变量优先级更高，会盖掉落盘文件。"""
    if os.environ.get("CHAOGU_LLM_BASE"):
        return "env"
    if CFG_PATH.exists():
        return "file"
    if os.environ.get("ANTHROPIC_BASE_URL") or os.environ.get("ANTHROPIC_API_KEY"):
        return "env"
    return "none"


def env_override():
    """环境变量是否压过落盘配置——压过的话写文件是白写的，设置面板要拦住。"""
    return _env_source() == "env"


def config_view():
    """给设置面板用的配置视图：**绝不包含 key 明文**。"""
    cfg = load_cfg() or {}
    source = _env_source()
    return {"base": cfg.get("base", ""), "api": cfg.get("api", "anthropic"),
            "model": cfg.get("model", ""), "has_key": bool(cfg.get("key")),
            "key_hint": key_hint(cfg.get("key")), "source": source,
            "available": bool(cfg.get("base")), "path": str(CFG_PATH),
            # 环境变量压过文件：这种情况下写盘是白写的，前端要明确提示。
            "writable": source != "env"}


def normalize_config(data, current=None):
    """校验并归一化设置表单，返回 (cfg, error)；不写盘。"""
    if current is None:
        current = load_cfg() or {}
    base = str(data.get("base") or "").strip().rstrip("/")
    if not base:
        return None, "接口地址（base）不能为空"
    if not base.lower().startswith(("http://", "https://")):
        return None, "接口地址必须以 http:// 或 https:// 开头"
    if len(base) > 300:
        return None, "接口地址过长"
    api = str(data.get("api") or "anthropic").strip().lower()
    if api not in ("anthropic", "openai"):
        return None, "协议只能是 anthropic 或 openai"
    model = str(data.get("model") or "").strip() or DEFAULT_MODEL
    if len(model) > 120 or any(c in model for c in "\r\n"):
        return None, "模型名不合法"
    # key 留空 = 沿用已保存的那把。否则每次改模型名都得重新粘一遍 key。
    key = data.get("key")
    if key is None or not str(key).strip():
        key = current.get("key", "")
    else:
        key = str(key).strip()
        if len(key) > 400 or any(c in key for c in "\r\n"):
            return None, "Key 不合法（含换行或过长）"
    return {"base": base, "api": api, "model": model, "key": key}, None


def load_cfg():
    c = {}
    try:
        c = json.loads(CFG_PATH.read_text("utf-8"))
    except Exception:
        c = {}
    env = os.environ
    if env.get("CHAOGU_LLM_BASE"):
        c = {"base": env["CHAOGU_LLM_BASE"], "key": env.get("CHAOGU_LLM_KEY", ""),
             "model": env.get("CHAOGU_LLM_MODEL") or c.get("model"),
             "api": env.get("CHAOGU_LLM_API") or "anthropic"}
    if not c.get("base") and env.get("ANTHROPIC_BASE_URL"):
        c = {"base": env["ANTHROPIC_BASE_URL"],
             "key": env.get("ANTHROPIC_AUTH_TOKEN") or env.get("ANTHROPIC_API_KEY", ""),
             "api": "anthropic"}
    if not c.get("base") and env.get("ANTHROPIC_API_KEY"):
        c = {"base": "https://api.anthropic.com", "key": env["ANTHROPIC_API_KEY"],
             "api": "anthropic"}
    if c.get("base"):
        c.setdefault("model", DEFAULT_MODEL)
        c.setdefault("api", "anthropic")
        c["model"] = c.get("model") or DEFAULT_MODEL
    return c if c.get("base") else None


def save_cfg(cfg):
    CFG_PATH.write_text(json.dumps(cfg, ensure_ascii=False, indent=1), "utf-8")
    os.chmod(CFG_PATH, 0o600)


def available():
    return load_cfg() is not None


def _endpoint(cfg):
    """按协议拼出真实请求地址。base 里已经带了路径前缀就不要再拼一层。"""
    base = cfg["base"].rstrip("/")
    if cfg["api"] == "openai":
        if base.endswith("/v1") or "/api/v3" in base:
            return base + "/chat/completions"
        return base + "/v1/chat/completions"
    return base + "/v1/messages"


def chat(system, user, max_tokens=2000, timeout=90, strict_complete=False, _cfg=None):
    """返回文本；未配置或失败返回 None（失败原因写入 last_error / last_failure）。"""
    global last_error, last_failure
    cfg = _cfg or load_cfg()
    if not cfg:
        last_error = "未配置 LLM"
        last_failure = failure("unconfigured")
        return None
    if cfg["api"] == "openai":
        body = {"model": cfg["model"], "max_tokens": max_tokens,
                "messages": [{"role": "system", "content": system},
                             {"role": "user", "content": user}]}
        hdr = {"Authorization": "Bearer " + cfg["key"]}
    else:
        body = {"model": cfg["model"], "max_tokens": max_tokens,
                "system": system, "messages": [{"role": "user", "content": user}]}
        hdr = {"x-api-key": cfg["key"],
               "Authorization": "Bearer " + cfg["key"],
               "anthropic-version": "2023-06-01"}
    req = urllib.request.Request(
        _endpoint(cfg), data=json.dumps(body).encode("utf-8"),
        headers={"content-type": "application/json", **hdr})

    # ---- 网络层：HTTPError 必须先于 URLError 捕获（它是 URLError 的子类）
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        detail = _body(exc)
        status = exc.code
        low = detail.lower()
        kind = ("auth" if status in (401, 403) else
                "rate_limit" if status == 429 else
                "not_found" if status == 404 else
                "upstream" if status >= 500 else
                "model" if status == 400 and "model" in low else
                "bad_request")
        last_failure = failure(kind, detail or exc.reason, status)
        last_error = f"HTTP Error {status}: {exc.reason} {detail}".strip()
        return None
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        reason = getattr(exc, "reason", exc)
        timed_out = isinstance(reason, TimeoutError) or "timed out" in str(reason).lower()
        last_failure = failure("timeout" if timed_out else "network", reason)
        last_error = f"{type(exc).__name__}: {reason}"
        return None
    except Exception as exc:                           # noqa: BLE001
        last_failure = failure("unknown", f"{type(exc).__name__}: {exc}")
        last_error = last_failure["detail"]
        return None

    # ---- 解析层：到这里已经是 200，出错说明网关返回了非预期结构
    try:
        j = json.loads(raw)
        if cfg["api"] == "openai":
            txt = j["choices"][0]["message"]["content"]
            stopped = j["choices"][0].get("finish_reason")
        else:
            txt = "".join(b.get("text", "") for b in j.get("content") or []
                          if b.get("type") == "text")
            stopped = j.get("stop_reason")
    except Exception as exc:                           # noqa: BLE001
        last_failure = failure("upstream", f"响应结构异常：{raw[:200]}")
        last_error = f"解析响应失败 {type(exc).__name__}: {exc}；原文 {raw[:200]}"
        return None

    if strict_complete and stopped != ("stop" if cfg["api"] == "openai" else "end_turn"):
        last_failure = failure("incomplete", f"stop_reason={stopped}")
        last_error = "模型输出未完整结束"
        return None
    if not txt.strip():
        last_failure = failure("empty")
        last_error = "模型返回了空内容"
        return None
    last_error, last_failure = None, None
    return txt.strip()


def test_connection(draft=None, timeout=30):
    """连通性自检：用草稿配置（缺的字段回落到当前配置）跑一次最小请求，不写盘。"""
    cur = load_cfg() or {}
    draft = draft or {}
    cfg = {"base": str(draft.get("base") or cur.get("base") or "").strip().rstrip("/"),
           "api": str(draft.get("api") or cur.get("api") or "anthropic").strip().lower(),
           "model": str(draft.get("model") or cur.get("model") or DEFAULT_MODEL).strip(),
           "key": str(draft.get("key") or cur.get("key") or "").strip()}
    if not cfg["base"]:
        return {"ok": False, **failure("unconfigured")}
    txt = chat("你是测试助手", "只回复两个字：正常", max_tokens=64,
               timeout=timeout, _cfg=cfg)
    if not txt:
        return {"ok": False, **(last_failure or failure("unknown", last_error))}
    return {"ok": True, "kind": "ok", "message": f"连通正常，模型回了「{txt[:40]}」",
            "action": "", "detail": "", "model": cfg["model"],
            "base": cfg["base"], "api": cfg["api"]}


last_error = None
last_failure = None


def extract_json(text):
    """从模型输出里抠出第一个 JSON 对象。"""
    if not text:
        return None
    m = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.S)
    raw = m.group(1) if m else text[text.find("{"): text.rfind("}") + 1]
    try:
        return json.loads(raw)
    except Exception:
        return None
