#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""新浪行情数据源（K 线第三备选通道）。

东财、腾讯同时被限流时用这个。字段是 不复权 价，
因此涨跌幅由相邻收盘价推算，在除权日会有偏差——
但对于"昨日涨停今日表现"这类短窗口统计，影响可接受，
且调用方会优先使用复权源。
"""

from __future__ import annotations

from ..net import FetchError, get_json

KLINE = ("https://money.finance.sina.com.cn/quotes_service/api/json_v2.php/"
         "CN_MarketData.getKLineData")


def symbol(code, market=None):
    c = str(code)
    if c.startswith(("92", "8", "4")):
        return f"bj{c}"
    if market == 1 or (market is None and c.startswith(("6", "9", "5"))):
        return f"sh{c}"
    return f"sz{c}"


def fetch_kline(code, market=None, days=160):
    sym = symbol(code, market)
    url = f"{KLINE}?symbol={sym}&scale=240&ma=no&datalen={days}"
    j = get_json(url, referer="https://finance.sina.com.cn/")
    if not isinstance(j, list):
        return []
    out = []
    prev_close = None
    for r in j:
        try:
            close = float(r["close"])
            pct = None
            if prev_close:
                pct = round((close / prev_close - 1) * 100, 3)
            out.append({
                "date": (r.get("day") or "")[:10],
                "open": float(r["open"]),
                "close": close,
                "high": float(r["high"]),
                "low": float(r["low"]),
                "volume": float(r.get("volume") or 0),
                "amount": None,
                "pct": pct,
                "turnover": None,
            })
            prev_close = close
        except (KeyError, ValueError, TypeError):
            continue
    return out


INDEX_SPOT = "https://hq.sinajs.cn/list="
# 两市成交额 = 上证指数 + 深证综指（深证综指覆盖全部深市个股，
# 深证成指只覆盖 500 只，不能用来算两市总额）
DEFAULT_INDEXES = ("sh000001", "sz399106", "sz399001", "sz399006")


def fetch_index_spot(codes=DEFAULT_INDEXES):
    """指数实时行情。返回 {code: {name, close, prev_close, amount, pct}}。

    注意：新浪该接口返回 **GBK** 编码，而 net.get_text 按 UTF-8 解码，
    因此中文名会是乱码。数字字段是 ASCII 不受影响，
    所以调用方应**按代码而不是名称**取值。
    """
    from ..net import FetchError, get_text
    url = INDEX_SPOT + ",".join(codes)
    try:
        txt = get_text(url, referer="https://finance.sina.com.cn/")
    except FetchError:
        return {}
    out = {}
    for line in (txt or "").splitlines():
        if "hq_str_" not in line or "=" not in line:
            continue
        left, _, right = line.partition("=")
        code = left.strip().replace("var hq_str_", "")
        parts = right.strip().strip(';').strip('"').split(",")
        if len(parts) < 11:
            continue
        try:
            prev_close = float(parts[2])
            close = float(parts[3])
            amount = float(parts[9])          # 成交额（元）
        except (ValueError, IndexError):
            continue
        pct = round((close / prev_close - 1) * 100, 3) if prev_close else None
        out[code] = {
            "name": parts[0],
            "close": close,
            "prev_close": prev_close,
            "amount": amount,
            "pct": pct,
        }
    return out


def fetch_market_turnover():
    """两市成交额（元）。上证指数 + 深证综指。"""
    spot = fetch_index_spot()
    sh = spot.get("sh000001")
    sz = spot.get("sz399106") or spot.get("sz399001")
    if not sh or not sz:
        return None
    return sh["amount"] + sz["amount"]
