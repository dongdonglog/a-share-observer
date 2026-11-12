#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Public Eastmoney and Tonghuashun popularity rankings.

These are adapters for public web endpoints, not vendor-supported API
contracts. Callers must treat schema changes and partial responses as errors.
"""

from __future__ import annotations

import json
import random
import re
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request

EASTMONEY_URL = "https://emappdata.eastmoney.com/stockrank/getAllCurrentList"
THS_URL = "https://dq.10jqka.com.cn/fuyao/hot_list_data/out/hot_list/v1/stock"
EASTMONEY_REFERER = "https://guba.eastmoney.com/rank/"
THS_REFERER = "https://eq.10jqka.com.cn/frontend/thsTopRank/index.html#/"
PAGE_SIZE = 100
MAX_PAGES = 10
RETRY_ATTEMPTS = 3
RETRY_BACKOFF = (0.4, 1.0)
RETRYABLE_STATUS = {429, 500, 502, 503, 504}


class PopularityError(RuntimeError):
    pass


def _http_failure(exc):
    """把 HTTPError 变成带状态码和信息片段的 PopularityError。

    状态码是区分"限流/上游抖动"和"接口或鉴权变更"的唯一线索，只留异常类名会让
    阅读 source_status 的人无从判断该重试还是该改代码。
    """
    body = ""
    try:
        body = exc.read(200).decode("utf-8", "replace").strip()
    except Exception:  # noqa: BLE001
        body = ""
    message = f"榜单请求失败：HTTP {exc.code} {exc.reason}"
    if body:
        message += f" · {body[:160]}"
    return PopularityError(message)


def _attempt(url, body, headers, timeout):
    req = urllib.request.Request(url, data=body, headers=headers)
    with urllib.request.urlopen(req, timeout=timeout,
                               context=ssl.create_default_context()) as resp:
        raw = resp.read(2_000_001)
    if len(raw) > 2_000_000:
        raise PopularityError("榜单响应超过安全读取上限")
    result = json.loads(raw.decode("utf-8-sig"))
    if not isinstance(result, dict):
        raise PopularityError("榜单响应格式异常")
    return result


def _request(url, *, payload=None, referer, timeout=12):
    """请求公开榜单，对网络抖动做有界重试。

    这些接口经本机代理或公网直连时会出现偶发的 502/超时/截断响应（同一次运行里
    第一次 502、第二次成功）。重试只覆盖可能自愈的失败：网络错误、429 和 5xx。
    其余 4xx 视为确定性失败，直接上报，不浪费三次请求。三次都失败时按失败关闭
    处理——由调用方把该来源标为异常，而不是拿旧排名顶替。
    """
    body = None if payload is None else json.dumps(payload).encode("utf-8")
    headers = {
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                      "AppleWebKit/537.36 Chrome/130.0.0.0 Safari/537.36",
        "Accept": "application/json, text/plain, */*",
        "Referer": referer,
    }
    if body is not None:
        headers["Content-Type"] = "application/json;charset=UTF-8"
    last = None
    for index in range(RETRY_ATTEMPTS):
        try:
            return _attempt(url, body, headers, timeout)
        except PopularityError:
            raise
        except urllib.error.HTTPError as exc:
            last = _http_failure(exc)
            if exc.code not in RETRYABLE_STATUS:
                raise last from None
        except (OSError, UnicodeError, ValueError, urllib.error.URLError) as exc:
            detail = str(exc).strip()
            suffix = f"：{detail[:160]}" if detail else ""
            last = PopularityError(f"榜单请求或解析失败：{type(exc).__name__}{suffix}")
        if index < RETRY_ATTEMPTS - 1:
            time.sleep(RETRY_BACKOFF[index] + random.uniform(0, 0.3))
    raise last


def _code(value):
    value = str(value or "").strip().upper()
    m = re.fullmatch(r"(?:(?:SH|SZ|BJ)[.]?)?(\d{6})", value)
    return m.group(1) if m else None


def _valid_rows(rows, provider):
    if not isinstance(rows, list) or not rows:
        raise PopularityError(f"{provider} 未返回有效排名")
    out, seen_codes, seen_ranks = [], set(), set()
    for index, row in enumerate(rows, 1):
        if not isinstance(row, dict):
            continue
        code = _code(row.get("code") or row.get("stock_code") or row.get("sc"))
        rank = row.get("rank", row.get("rk", row.get("order")))
        if rank is None:
            rank = row.get("display_order")
        try:
            rank = int(rank)
        except (TypeError, ValueError):
            continue
        name = str(row.get("name") or row.get("stock_name") or row.get("ts_name") or "").strip()
        if not code or rank < 1 or not name or code in seen_codes or rank in seen_ranks:
            continue
        seen_codes.add(code)
        seen_ranks.add(rank)
        out.append({"code": code, "name": name[:40], "rank": rank})
    if not out:
        raise PopularityError(f"{provider} 返回字段无法识别")
    out.sort(key=lambda x: x["rank"])
    return out[:1000]


def fetch_eastmoney():
    """Fetch the Eastmoney popularity list.

    The public endpoint currently returns only the first 100 rows: paging past
    page 1 yields an empty array in practice, so the 1..100 window is the real
    observable depth. Paging is kept because it degrades to a no-op if the
    upstream ever widens.
    """
    rows = []
    for page in range(1, MAX_PAGES + 1):
        payload = {
            "appId": "appId01",
            "globalId": "786e4c21-70dc-435a-93bb-38",
            "marketType": "",
            "pageNo": page,
            "pageSize": PAGE_SIZE,
        }
        result = _request(EASTMONEY_URL, payload=payload, referer=EASTMONEY_REFERER)
        data = result.get("data")
        if isinstance(data, dict):
            data = data.get("list") or data.get("data")
        if data is None:
            raise PopularityError("东方财富榜单缺少 data 字段")
        if not isinstance(data, list):
            raise PopularityError("东方财富榜单 data 格式异常")
        if not data:
            break
        for row in data:
            if not isinstance(row, dict):
                continue
            rows.append({"code": row.get("sc"), "name": row.get("name") or row.get("stockName"),
                         "rank": row.get("rk")})
        if len(data) < PAGE_SIZE:
            break
        time.sleep(0.12)
    missing = [row for row in rows if not row.get("name")]
    if missing:
        names = _eastmoney_names([_code(row.get("code")) for row in missing])
        for row in missing:
            row["name"] = names.get(_code(row.get("code")))
    return _valid_rows(rows, "东方财富")


def _eastmoney_names(codes):
    """Resolve names for the rank endpoint, which often omits them."""
    found = {}
    codes = [code for code in dict.fromkeys(codes) if code]
    # 先尝试东财原生批量名称接口；部分网络环境会直接断开，失败后再切换
    # 到腾讯公开行情补名。排名来源始终保持东方财富。
    for start in range(0, len(codes), 100):
        chunk = codes[start:start + 100]
        secids = ",".join(("1." if code.startswith(("5", "6", "9")) else "0.") + code
                          for code in chunk)
        query = urllib.parse.urlencode({"fltt": 2, "fields": "f12,f14", "secids": secids})
        try:
            result = _request("https://push2.eastmoney.com/api/qt/ulist.np/get?" + query,
                              referer=EASTMONEY_REFERER)
            data = result.get("data")
            rows = data.get("diff") if isinstance(data, dict) else None
            if isinstance(rows, dict):
                rows = list(rows.values())
            if not isinstance(rows, list):
                raise PopularityError("东方财富证券名称补全失败")
            for row in rows:
                if not isinstance(row, dict):
                    continue
                code, name = _code(row.get("f12")), str(row.get("f14") or "").strip()
                if code and name:
                    found[code] = name[:40]
        except Exception:  # noqa: BLE001
            continue

    missing = [code for code in codes if code not in found]
    # ulist.np/get 失败时，改用腾讯公开批量行情补名。
    try:
        from . import tencent
        for start in range(0, len(missing), 50):
            for row in tencent.fetch_quotes(missing[start:start + 50]):
                code, name = _code(row.get("code")), str(row.get("name") or "").strip()
                if code and name and "�" not in name:
                    found[code] = name
    except Exception:  # noqa: BLE001
        pass
    missing = [code for code in codes if code not in found]
    if missing:
        # 最后逐只尝试东财 stock/get；它比批量名称接口更稳定，但只用于少量漏项。
        for code in missing[:20]:
            market = "1." if code.startswith(("5", "6", "9")) else "0."
            query = urllib.parse.urlencode({"secid": market + code, "fields": "f12,f58"})
            try:
                result = _request("https://push2.eastmoney.com/api/qt/stock/get?" + query,
                                  referer=EASTMONEY_REFERER)
                data = result.get("data") if isinstance(result, dict) else None
                name = str((data or {}).get("f58") or "").strip()
                if name:
                    found[code] = name[:40]
            except Exception:  # noqa: BLE001
                continue
    return found


def fetch_ths():
    """Fetch the public Tonghuashun A-share daily hot-stock list.

    Single request without paging; the endpoint returns the top 100 only.
    """
    query = urllib.parse.urlencode({"list_type": "normal", "stock_type": "a", "type": "day"})
    result = _request(THS_URL + "?" + query, referer=THS_REFERER)
    if str(result.get("status_code", "0")) not in ("0", "200"):
        code = result.get("status_code")
        message = str(result.get("status_msg") or result.get("message") or "").strip()
        raise PopularityError(f"同花顺榜单返回非成功状态：status_code={code}"
                              + (f" · {message[:160]}" if message else ""))
    data = result.get("data")
    rows = data.get("stock_list") if isinstance(data, dict) else None
    if rows is None and isinstance(data, list):
        rows = data
    normalized = []
    for index, row in enumerate(rows or [], 1):
        if not isinstance(row, dict):
            continue
        rank = row.get("rank", row.get("order", row.get("display_order")))
        if rank is None:
            # The web page orders this list directly and assigns rank by index.
            rank = index
        normalized.append({
            "code": row.get("code") or row.get("stock_code"),
            "name": row.get("name") or row.get("stock_name"),
            "rank": rank,
        })
    return _valid_rows(normalized, "同花顺")
