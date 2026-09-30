#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""腾讯行情数据源（作为东财的备选通道）。

存在的理由：东财的 push2his / push2 在批量抓取后会对来源 IP 限流
（表现为 RemoteDisconnected / http=000），此时腾讯仍然可用。
多一个源，长任务就不会因为单点限流而卡死。
"""

from __future__ import annotations

from ..net import FetchError, get_json

KLINE = "https://web.ifzq.gtimg.cn/appstock/app/fqkline/get"
QUOTE = "https://qt.gtimg.cn/q="


def symbol(code, market=None):
    """构造腾讯代码。1=沪 sh，0=深 sz，北交所 bj。"""
    c = str(code)
    if c.startswith(("92", "8", "4")):
        return f"bj{c}"
    if market == 1 or (market is None and c.startswith(("6", "9", "5"))):
        return f"sh{c}"
    return f"sz{c}"


def fetch_kline(code, market=None, days=160):
    """日K（前复权）。

    腾讯的字段顺序是 [日期, 开, 收, 高, 低, 量]，
    与东财不同（东财是 开,收,高,低），注意别搞混。
    涨跌幅由相邻收盘价推算——前复权价算出来的日收益是正确的。
    """
    sym = symbol(code, market)
    url = f"{KLINE}?param={sym},day,,,{days},qfq"
    j = get_json(url, referer="https://gu.qq.com/")
    node = (j.get("data") or {}).get(sym) or {}
    rows = node.get("qfqday") or node.get("day") or []
    out = []
    prev_close = None
    for r in rows:
        if len(r) < 6:
            continue
        try:
            close = float(r[2])
            pct = None
            if prev_close:
                pct = round((close / prev_close - 1) * 100, 3)
            out.append({
                "date": r[0],
                "open": float(r[1]),
                "close": close,
                "high": float(r[3]),
                "low": float(r[4]),
                "volume": float(r[5]),
                "amount": None,
                "pct": pct,
                "turnover": None,
            })
            prev_close = close
        except (ValueError, TypeError):
            continue
    return out


def fetch_quotes(codes, markets=None):
    """批量实时行情（一次最多约 50 只），作为全市场快照的备用通道。"""
    if not codes:
        return []
    syms = [symbol(c, (markets or {}).get(c)) for c in codes]
    url = QUOTE + ",".join(syms)
    txt = None
    try:
        from ..net import get_text
        txt = get_text(url, referer="https://gu.qq.com/")
    except FetchError:
        return []
    out = []
    for line in (txt or "").split(";"):
        line = line.strip()
        if not line or "=" not in line:
            continue
        left, _, right = line.partition("=")
        code = left.replace("v_", "")[2:]
        parts = right.strip().strip('"').split("~")
        if len(parts) < 33:
            continue
        try:
            out.append({
                "code": code,
                "name": parts[1],
                "close": float(parts[3]) if parts[3] else None,
                "pct": float(parts[32]) if parts[32] else None,
                "turnover": float(parts[38]) if len(parts) > 38
                            and parts[38] else None,
                "amount": float(parts[37]) * 1e4 if len(parts) > 37
                          and parts[37] else None,
            })
        except (ValueError, IndexError):
            continue
    return out
