#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""LLM 客户端（纯标准库）：给 AI 看板、AI 解读用。

配置来源（前者优先）：
  1. 环境变量 CHAOGU_LLM_BASE / CHAOGU_LLM_KEY / CHAOGU_LLM_MODEL /
     CHAOGU_LLM_API（anthropic | openai）
  2. data/llm.json（`python3 run.py llm-setup` 生成，权限 600）
  3. 环境变量 ANTHROPIC_BASE_URL + ANTHROPIC_AUTH_TOKEN / ANTHROPIC_API_KEY

为什么要落盘配置：看板由 launchd 开机自启时拿不到 shell 的环境变量，
只读环境变量的话自启后的看板永远是"AI 未配置"。

没有配置时所有调用返回 None，看板退回规则版（不会报错）。
"""

from __future__ import annotations

import json
import os
import re
import urllib.request

from . import config

CFG_PATH = config.DATA_DIR / "llm.json"
DEFAULT_MODEL = "claude-sonnet-5-5"


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


def chat(system, user, max_tokens=2000, timeout=90):
    """返回文本；未配置或失败返回 None（失败原因写入 last_error）。"""
    global last_error
    cfg = load_cfg()
    if not cfg:
        last_error = "未配置 LLM"
        return None
    base = cfg["base"].rstrip("/")
    try:
        if cfg["api"] == "openai":
            url = base + ("/chat/completions" if base.endswith("/v1") or
                          "/api/v3" in base else "/v1/chat/completions")
            body = {"model": cfg["model"], "max_tokens": max_tokens,
                    "messages": [{"role": "system", "content": system},
                                 {"role": "user", "content": user}]}
            hdr = {"Authorization": "Bearer " + cfg["key"]}
        else:
            url = base + "/v1/messages"
            body = {"model": cfg["model"], "max_tokens": max_tokens,
                    "system": system,
                    "messages": [{"role": "user", "content": user}]}
            hdr = {"x-api-key": cfg["key"],
                   "Authorization": "Bearer " + cfg["key"],
                   "anthropic-version": "2023-06-01"}
        req = urllib.request.Request(
            url, data=json.dumps(body).encode("utf-8"),
            headers={"content-type": "application/json", **hdr})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            j = json.loads(r.read().decode("utf-8"))
        if cfg["api"] == "openai":
            txt = j["choices"][0]["message"]["content"]
        else:
            txt = "".join(b.get("text", "") for b in j.get("content") or []
                          if b.get("type") == "text")
        last_error = None
        return txt.strip() or None
    except Exception as exc:                          # noqa: BLE001
        detail = ""
        try:
            detail = exc.read().decode("utf-8")[:300]   # type: ignore[attr-defined]
        except Exception:
            pass
        last_error = f"{exc} {detail}".strip()
        return None


last_error = None


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
