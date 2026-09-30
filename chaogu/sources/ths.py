#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""同花顺数据源：涨停池 / 炸板池 / 连板梯队 / 题材板块。

关键优势：**支持历史日期回溯**（实测可到约 2026-04），
且带题材归因（reason_type）与个股催化原因（reason_info）。
东财只有当天，因此历史回填走这里。
"""

from __future__ import annotations

from datetime import datetime, timezone, timedelta

from .. import config
from ..net import get_json

BASE = "https://data.10jqka.com.cn/dataapi/limit_up"
REFERER = "https://data.10jqka.com.cn/"
CST = timezone(timedelta(hours=8))
MAX_LIMIT = 200          # 该接口 limit 上限


def _compact(date):
    """'2026-09-30' -> '20260930'（接口要求紧凑格式）。"""
    return str(date).replace("-", "")


def _ts_to_hms(ts):
    """unix 秒 -> 'HH:MM:SS'（北京时间）。"""
    try:
        v = int(ts)
        if v <= 0:
            return None
        return datetime.fromtimestamp(v, CST).strftime("%H:%M:%S")
    except (TypeError, ValueError, OSError):
        return None


def _pnl(j):
    d = j.get("data") or {}
    return d if isinstance(d, dict) else {}


def _fetch_paged(path, date, list_key="info"):
    """分页拉取，返回 info 列表。limit=200 上限。"""
    out, page = [], 1
    while True:
        url = (f"{BASE}/{path}?page={page}&limit={MAX_LIMIT}"
               f"&field={config.THS_FIELDS}&filter=HS,GEM2STAR"
               f"&order_field=330324&order_type=0&date={_compact(date)}")
        if path == "continuous_limit_up":
            url = f"{BASE}/{path}?page={page}&limit={MAX_LIMIT}&date={_compact(date)}"
        j = get_json(url, referer=REFERER)
        if j.get("status_code") != 0:
            return out
        data = j.get("data")
        if path == "continuous_limit_up":
            # 该接口直接返回高度分组列表
            return data or []
        info = (data or {}).get(list_key) or []
        if not info:
            return out
        out.extend(info)
        pageinfo = (data or {}).get("page") or {}
        total = pageinfo.get("total") or 0
        if len(out) >= total or page > 20:
            return out
        page += 1


def fetch_limit_up(date):
    """历史涨停池（同花顺）。"""
    rows = []
    for p in _fetch_paged("limit_up_pool", date):
        hdv = p.get("high_days_value") or 0
        boards = (hdv >> 16) or p.get("continue_num") or 1
        days = (hdv & 0xFFFF) or None
        rows.append({
            "trade_date": date,
            "code": p.get("code"),
            "name": p.get("name"),
            "market": p.get("market_id"),
            "price": p.get("latest"),
            "pct": p.get("change_rate"),
            "turnover": p.get("turnover_rate"),
            "float_mv": p.get("currency_value"),
            "total_mv": p.get("sum_market_value"),
            "height": boards,
            "days": days,
            "height_text": p.get("high_days"),
            "lu_type": p.get("limit_up_type"),
            "change_tag": p.get("change_tag"),
            "open_num": p.get("open_num"),
            "seal_amount": p.get("order_amount"),
            "first_seal": _ts_to_hms(p.get("first_limit_up_time")),
            "last_seal": _ts_to_hms(p.get("last_limit_up_time")),
            "industry": None,
            "reason": p.get("reason_type"),
            "is_new": p.get("is_new"),
            "source": "ths",
        })
    return rows


def fetch_broken(date):
    """历史炸板池（同花顺）。"""
    rows = []
    for p in _fetch_paged("open_limit_pool", date):
        hdv = p.get("high_days_value") or 0
        rows.append({
            "trade_date": date,
            "code": p.get("code"),
            "name": p.get("name"),
            "market": p.get("market_id"),
            "price": p.get("latest"),
            "pct": p.get("change_rate"),
            "turnover": p.get("turnover_rate"),
            "zt_price": p.get("limit_up_price") or p.get("zt_price"),
            "open_num": p.get("open_num"),
            "amplitude": p.get("amplitude"),
            "industry": None,
            "reason": p.get("reason_type"),
            "first_seal": _ts_to_hms(p.get("first_limit_up_time")),
            "source": "ths",
        })
    return rows


def fetch_ladder(date):
    """连板梯队：返回 {高度: [股票...]}。"""
    groups = _fetch_paged("continuous_limit_up", date)
    out = {}
    for g in groups or []:
        h = g.get("height")
        if h is None:
            continue
        out[int(h)] = g.get("code_list") or []
    return out


def fetch_theme_blocks(date, top=60):
    """题材板块（带涨停家数、连板数、最高板、成分股与催化原因）。"""
    url = f"{BASE}/block_top?page=1&limit={top}&date={_compact(date)}"
    j = get_json(url, referer=REFERER)
    if j.get("status_code") != 0:
        return []
    out = []
    for b in j.get("data") or []:
        stocks = []
        for s in b.get("stock_list") or []:
            hdv = s.get("high_days") or 0
            stocks.append({
                "code": s.get("code"),
                "name": s.get("name"),
                "price": s.get("latest"),
                "pct": s.get("change_rate"),
                "reason": s.get("reason_type"),
                "reason_info": s.get("reason_info"),
                "continue_num": s.get("continue_num") or (hdv >> 16) or 1,
                "height_text": s.get("high"),
                "change_tag": s.get("change_tag"),
                "is_st": s.get("is_st"),
                "is_new": s.get("is_new"),
                "market_type": s.get("market_type"),
                "first_seal": _ts_to_hms(s.get("first_limit_up_time")),
                "last_seal": _ts_to_hms(s.get("last_limit_up_time")),
            })
        out.append({
            "code": b.get("code"),
            "name": b.get("name"),
            "change": b.get("change"),
            "limit_up_num": b.get("limit_up_num"),
            "continuous_plate_num": b.get("continuous_plate_num"),
            "high": b.get("high"),
            "days": b.get("days"),
            "stock_list": stocks,
        })
    return out


def probe_date(date):
    """判断某日是否为交易日/有无数据。返回涨停家数或 None。"""
    url = (f"{BASE}/limit_up_pool?page=1&limit=1&field={config.THS_FIELDS}"
           f"&filter=HS,GEM2STAR&order_field=330324&order_type=0&date={_compact(date)}")
    try:
        j = get_json(url, referer=REFERER, retry=1)
    except Exception:                                  # noqa: BLE001
        return None
    if j.get("status_code") != 0:
        return None
    t = ((j.get("data") or {}).get("page") or {}).get("total")
    return t
