#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""HTTP 工具：连接复用 + 重试 + 限速 + 磁盘缓存。

为什么要自己做连接复用：
  东财的接口在 **每次请求都新建 TLS 连接** 时会大幅限流
  （表现为 RemoteDisconnected）。复用 keep-alive 连接后
  同样的分页抓取失败率从 ~25% 降到接近 0。
"""

from __future__ import annotations

import hashlib
import http.client
import json
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from . import config

_CTX = ssl.create_default_context()
_CTX.check_hostname = False
_CTX.verify_mode = ssl.CERT_NONE

_conns: dict[str, http.client.HTTPSConnection] = {}
_last_call = [0.0]


class FetchError(RuntimeError):
    pass


def _throttle():
    gap = time.time() - _last_call[0]
    if gap < config.HTTP_SLEEP:
        time.sleep(config.HTTP_SLEEP - gap)
    _last_call[0] = time.time()


def _conn(host):
    c = _conns.get(host)
    if c is None:
        c = http.client.HTTPSConnection(host, timeout=config.HTTP_TIMEOUT,
                                        context=_CTX)
        _conns[host] = c
    return c


def _drop(host):
    c = _conns.pop(host, None)
    if c is not None:
        try:
            c.close()
        except Exception:                              # noqa: BLE001
            pass


def _request(url, referer=None, timeout=None):
    parts = urllib.parse.urlsplit(url)
    host = parts.netloc
    path = parts.path + (("?" + parts.query) if parts.query else "")
    headers = {
        "User-Agent": config.UA,
        "Accept": "*/*",
        "Accept-Language": "zh-CN,zh;q=0.9",
        "Accept-Encoding": "identity",
        "Host": host,
    }
    if referer:
        headers["Referer"] = referer
    conn = _conn(host)
    if timeout:
        conn.timeout = timeout
    conn.request("GET", path, headers=headers)
    resp = conn.getresponse()
    body = resp.read().decode("utf-8", "replace")
    if resp.status != 200:
        raise FetchError(f"HTTP {resp.status} {url[:100]}")
    return body


def get_text(url, referer=None, timeout=None, retry=None, cache=False,
             cache_key=None):
    """GET 返回文本。失败抛 FetchError。"""
    timeout = timeout or config.HTTP_TIMEOUT
    retry = retry if retry is not None else config.HTTP_RETRY

    if cache:
        p = _cache_path(cache_key or url)
        if p.exists() and p.stat().st_size > 0:
            return p.read_text(encoding="utf-8")

    host = urllib.parse.urlsplit(url).netloc
    last_err = None
    for attempt in range(retry):
        _throttle()
        try:
            body = _request(url, referer=referer, timeout=timeout)
            if cache:
                _cache_path(cache_key or url).write_text(body, encoding="utf-8")
            return body
        except Exception as e:                         # noqa: BLE001
            last_err = e
            _drop(host)                # 连接可能已坏，丢弃重建
            if attempt < retry - 1:
                time.sleep(0.4 * (attempt + 1))
    raise FetchError(f"GET 失败({retry}次) {url[:100]} :: "
                     f"{type(last_err).__name__}: {last_err}")


def get_json(url, referer=None, timeout=None, retry=None, cache=False,
             cache_key=None):
    txt = get_text(url, referer=referer, timeout=timeout, retry=retry,
                   cache=cache, cache_key=cache_key)
    try:
        return json.loads(txt)
    except json.JSONDecodeError as e:
        raise FetchError(f"JSON 解析失败 {url[:100]} :: {txt[:160]!r}") from e


def _cache_path(key: str) -> Path:
    h = hashlib.sha256(key.encode("utf-8")).hexdigest()[:20]
    return config.CACHE_DIR / f"{h}.txt"


def qs(base: str, **params) -> str:
    return base + "?" + urllib.parse.urlencode(params)


def close_all():
    for h in list(_conns):
        _drop(h)
