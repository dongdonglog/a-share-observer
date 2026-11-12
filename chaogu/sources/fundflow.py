#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""东方财富全市场资金流向（主力/超大单/大单/中单/小单 净额与净占比）。

公开 web 接口，不是厂商支持的 API 契约；调用方必须把 schema 变化与残缺响应
当作错误处理，而不是当"没有资金流"。

口径说明（东财公开口径，仅供参考、各平台有差异）：
  主力净额 f62 = 超大单净额 f66 + 大单净额 f72
  净占比 = 该类资金净额 / 当日成交额
金额单位是元；占比是百分数（如 14.6 表示 14.6%）。
"""

from __future__ import annotations

import math
import time

from .eastmoney import MARKET_FS
from ..net import FetchError, get_json

PUSH2 = "https://push2.eastmoney.com/api/qt/clist/get"
REFERER = "https://quote.eastmoney.com/"
PAGE_SIZE = 100
MAX_PAGES = 60
FIELDS = "f2,f3,f6,f12,f14,f62,f66,f69,f72,f75,f78,f81,f84,f87,f184"
# 量能榜字段：f5 成交量（手）、f6 成交额（元）、f8 换手率（%）、f10 量比。
# 与 FIELDS 分开请求，避免为了一个榜把资金流那十几个字段也拉一遍。
RANK_FIELDS = "f2,f3,f5,f6,f8,f10,f12,f14"


def _num(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value) if math.isfinite(value) else None


def _parse(rows):
    out = []
    for d in rows or []:
        if not isinstance(d, dict):
            continue
        code = str(d.get("f12") or "").strip()
        if len(code) != 6 or not code.isdigit():
            continue
        out.append({
            "code": code,
            "name": str(d.get("f14") or "").strip()[:40],
            "price": _num(d.get("f2")),
            "pct": _num(d.get("f3")),
            "amount": _num(d.get("f6")),
            "main_net": _num(d.get("f62")),
            "main_ratio": _num(d.get("f184")),
            "super_net": _num(d.get("f66")),
            "super_ratio": _num(d.get("f69")),
            "large_net": _num(d.get("f72")),
            "large_ratio": _num(d.get("f75")),
            "medium_net": _num(d.get("f78")),
            "medium_ratio": _num(d.get("f81")),
            "small_net": _num(d.get("f84")),
            "small_ratio": _num(d.get("f87")),
        })
    return out


def _parse_rank(rows):
    """量能榜字段解析。成交量单位是手，成交额单位是元，换手率与量比是倍数/百分数。"""
    out = []
    for d in rows or []:
        if not isinstance(d, dict):
            continue
        code = str(d.get("f12") or "").strip()
        if len(code) != 6 or not code.isdigit():
            continue
        out.append({
            "code": code,
            "name": str(d.get("f14") or "").strip()[:40],
            "price": _num(d.get("f2")),
            "pct": _num(d.get("f3")),
            "volume": _num(d.get("f5")),
            "amount": _num(d.get("f6")),
            "turnover": _num(d.get("f8")),
            "volume_ratio": _num(d.get("f10")),
        })
    return out


def _page(pn, fid, po, pz=PAGE_SIZE, fields=FIELDS, parse=None):
    """拉第 pn 页。东财限流时会返回 data:null，必须退避重试而不是当成空榜。"""
    url = (f"{PUSH2}?pn={pn}&pz={pz}&po={po}&np=1&fltt=2&invt=2"
           f"&fid={fid}&fs={MARKET_FS}&fields={fields}")
    last = None
    for attempt in range(6):
        try:
            j = get_json(url, referer=REFERER, retry=2)
        except FetchError as exc:
            last = exc
            time.sleep(1.2 * (attempt + 1))
            continue
        data = j.get("data")
        if data is None:
            time.sleep(1.5 * (attempt + 1))
            continue
        diff = data.get("diff") if isinstance(data, dict) else None
        if isinstance(diff, dict):
            diff = list(diff.values())
        if not isinstance(diff, list):
            raise FetchError("资金流响应缺少 diff 字段")
        return (parse or _parse)(diff)
    if last is not None:
        raise last
    raise FetchError("资金流请求失败：连续限流")


def _pager(fid, direction, limit, *, page_size=PAGE_SIZE, fields=FIELDS, parse=None):
    """按字段翻页取一段榜。已经拿到的连续前几页是有效的；某页失败时保留已有部分
    继续，不整段作废。只有第一页就失败才抛错（由调用方把来源标为异常）。"""
    po = "1" if direction == "in" else "0"
    out, pn = [], 1
    while len(out) < limit and pn <= MAX_PAGES:
        try:
            rows = _page(pn, fid, po, pz=page_size, fields=fields, parse=parse)
        except FetchError:
            if pn == 1:
                raise
            break
        if not rows:
            break
        out.extend(rows)
        if len(rows) < page_size:
            break
        pn += 1
        time.sleep(0.35)
    return out[:limit]


def fetch_rank(fid="f62", direction="in", limit=400, page_size=PAGE_SIZE):
    """按任意字段取一段榜。f62 主力净额 / f6 成交额 / f3 涨跌幅。

    direction="in" 为降序（净流入/金额大在前），"out" 为升序（净流出在前）。
    """
    return _pager(fid, direction, limit, page_size=page_size)


def fetch_volume_board(fid="f6", direction="in", limit=40, page_size=PAGE_SIZE):
    """量能活跃榜。fid 用 f6 成交额（默认）/ f5 成交量 / f10 量比，均为降序。

    与 fetch_rank 的区别只在请求字段与解析器：这里返回成交量、换手率、量比，
    没有资金流字段（超大单/大单净额等），收益口径不要互相混用。
    """
    if fid not in ("f6", "f5", "f10"):
        raise ValueError("量能榜只支持 f6 成交额 / f5 成交量 / f10 量比")
    return _pager(fid, direction, limit, page_size=page_size,
                  fields=RANK_FIELDS, parse=_parse_rank)



# 全市场扫描只取两端各 N 名：任何 |主力净额| 达到阈值（如 1 亿）的个股都落在
# 这两段里，足以判定榜单与提醒；相比逐页拉满 60 页，对限流容忍度高得多。
FLOW_WINDOW = 300


def fetch_flow_all(half=FLOW_WINDOW):
    """净流入前 half 名 + 净流出前 half 名的并集（去重）。"""
    seen = {}
    for direction in ("in", "out"):
        for row in fetch_rank(fid="f62", direction=direction, limit=half):
            seen.setdefault(row["code"], row)
    return list(seen.values())


def fetch_hot(limit=400):
    """按成交额排序的热门/活跃池，用于把观测范围扩到人气榜 100 名以外。"""
    return fetch_rank(fid="f6", direction="in", limit=limit)
