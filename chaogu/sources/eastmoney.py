#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""东方财富数据源：三池 + 全市场 + 板块 + 指数 + 外围。

东财的涨停/炸板/跌停池只提供**当天**数据（date 参数无效，
请求历史日期会回落到当天），因此它用于实时/当日采集；
历史回溯交给同花顺。
"""

from __future__ import annotations

import time
from datetime import datetime

from .. import config
from ..net import FetchError, get_json

PUSH2 = "https://push2.eastmoney.com/api/qt/clist/get"
PUSH2EX = "https://push2ex.eastmoney.com"
PUSH2HIS = "https://push2his.eastmoney.com/api/qt/stock/kline/get"

# 全市场股票池（沪深主板/创业板/科创板/北交所）
MARKET_FS = "m:0+t:6,m:0+t:80,m:1+t:2,m:1+t:23,m:0+t:81+s:2048"
PAGE_SIZE = 100


def _compact(date):
    """'2026-09-30' -> '20260930'。"""
    return str(date).replace("-", "")


def _hhmmss(v):
    """东财时间字段 92500 -> '09:25:00'。"""
    if v in (None, "", 0):
        return None
    s = str(int(v)).zfill(6)
    if len(s) != 6:
        return None
    return f"{s[:2]}:{s[2:4]}:{s[4:]}"


def _pool(endpoint, date):
    url = (f"{PUSH2EX}/{endpoint}?ut={config.EM_UT}&dpt=wz.ztzt"
           f"&Pageindex=0&pagesize=500&sort=fbt%3Aasc&date={_compact(date)}")
    if endpoint == "getTopicDTPool":
        url = url.replace("sort=fbt%3Aasc", "sort=fund%3Aasc")
    j = get_json(url, referer="https://quote.eastmoney.com/")
    data = j.get("data") or {}
    return data.get("pool") or [], data.get("qdate")


def fetch_limit_up(date):
    """东财涨停池。"""
    pool, qdate = _pool("getTopicZTPool", date)
    rows = []
    for p in pool:
        zttj = p.get("zttj") or {}
        rows.append({
            "trade_date": str(qdate or date),
            "code": p.get("c"),
            "name": p.get("n"),
            "market": p.get("m"),
            "price": round(p.get("p", 0) / 1000.0, 3),
            "pct": round(p.get("zdp", 0.0), 3),
            "turnover": round(p.get("hs") or 0.0, 3),
            "float_mv": p.get("ltsz"),
            "total_mv": p.get("tshare"),
            "height": p.get("lbc") or 1,
            "days": zttj.get("days"),
            "height_text": f"{zttj.get('days')}天{p.get('lbc')}板"
                           if (zttj.get("days") or 1) > 1 else "首板",
            "lu_type": None,
            "change_tag": None,
            "open_num": p.get("zbc") or 0,
            "seal_amount": p.get("fund"),
            "first_seal": _hhmmss(p.get("fbt")),
            "last_seal": _hhmmss(p.get("lbt")),
            "industry": p.get("hybk"),
            "reason": None,
            "is_new": None,
            "source": "eastmoney",
        })
    return rows, qdate


def fetch_broken(date):
    """东财炸板池。"""
    pool, qdate = _pool("getTopicZBPool", date)
    rows = []
    for p in pool:
        rows.append({
            "trade_date": str(qdate or date),
            "code": p.get("c"),
            "name": p.get("n"),
            "market": p.get("m"),
            "price": round(p.get("p", 0) / 1000.0, 3),
            "pct": round(p.get("zdp", 0.0), 3),
            "turnover": round(p.get("hs") or 0.0, 3),
            "zt_price": round(p.get("ztp", 0) / 1000.0, 3),
            "open_num": p.get("zbc") or 0,
            "amplitude": p.get("zf"),
            "industry": p.get("hybk"),
            "reason": None,
            "first_seal": _hhmmss(p.get("fbt")),
            "source": "eastmoney",
        })
    return rows, qdate


def fetch_limit_down(date):
    """东财跌停池。"""
    pool, qdate = _pool("getTopicDTPool", date)
    rows = []
    for p in pool:
        rows.append({
            "trade_date": str(qdate or date),
            "code": p.get("c"),
            "name": p.get("n"),
            "market": p.get("m"),
            "price": round(p.get("p", 0) / 1000.0, 3),
            "pct": round(p.get("zdp", 0.0), 3),
            "turnover": round(p.get("hs") or 0.0, 3),
            "seal_amount": p.get("fba") or p.get("fund"),
            "industry": p.get("hybk"),
            "source": "eastmoney",
        })
    return rows, qdate


def fetch_all_quotes(progress=None, max_pages=80, catchup_rounds=3):
    """全市场行情快照，分页拉全（约 59 页）。

    服务端在连续请求下会偶发 RemoteDisconnected。策略：
      主循环 + 若干轮"失败页补抓"。补抓轮之间留间隔让连接冷却。
    """
    import time as _t
    out, total, failed = [], None, []
    seen_codes = set()

    def fetch_page(page):
        url = (f"{PUSH2}?pn={page}&pz={PAGE_SIZE}&po=1&np=1&fltt=2&invt=2"
               f"&fid=f3&fs={MARKET_FS}"
               f"&fields=f2,f3,f6,f8,f12,f14,f20,f21")
        for attempt in range(5):
            try:
                j = get_json(url, referer="https://quote.eastmoney.com/",
                             retry=1)
                data = j.get("data") or {}
                return (data.get("diff") or []), data.get("total")
            except FetchError:
                _t.sleep(0.5 * (attempt + 1))
        return None, None

    def absorb(diff):
        added = 0
        for d in diff:
            pct = d.get("f3")
            if not isinstance(pct, (int, float)):
                continue          # 停牌等
            code = d.get("f12")
            if code in seen_codes:
                continue
            seen_codes.add(code)
            out.append({
                "trade_date": None,          # 由调用方填
                "code": code,
                "name": d.get("f14"),
                "pct": round(pct, 3),
                "turnover": d.get("f8") if isinstance(d.get("f8"), (int, float)) else None,
                "amount": d.get("f6") if isinstance(d.get("f6"), (int, float)) else None,
                "close": d.get("f2") if isinstance(d.get("f2"), (int, float)) else None,
            })
            added += 1
        return added

    page = 1
    consec_fail = 0
    while page <= max_pages:
        diff, tot = fetch_page(page)
        if tot and total is None:
            total = tot
        if diff is None:
            failed.append(page)
            consec_fail += 1
            # 前几页连续失败说明来源已被限流，没必要再耗 59 页的时间
            if consec_fail >= 3 and page <= 5:
                print(f"    !! 源头疑似限流（前 {page} 页均失败），"
                      f"放弃本轮全市场快照")
                return []
        elif not diff:
            break                          # 翻到最后一页
        else:
            consec_fail = 0
            absorb(diff)
            if progress:
                progress(page, total, len(out))
            if total and page * PAGE_SIZE > total + PAGE_SIZE:
                break
        page += 1
        _t.sleep(0.12)

    # 失败页补抓（多轮，逐步冷却）
    for rnd in range(catchup_rounds):
        if not failed:
            break
        _t.sleep(1.5 + rnd * 2.0)
        still = []
        for pg in failed:
            diff, _tot = fetch_page(pg)
            if diff is None:
                still.append(pg)
            else:
                absorb(diff)
                _t.sleep(0.2)
        failed = still
        if failed:
            print(f"    (补抓第 {rnd + 1} 轮后仍有 {len(failed)} 页失败: "
                  f"{failed[:6]}{'...' if len(failed) > 6 else ''})")

    if failed:
        print(f"    !! 全市场快照最终缺失 {len(failed)} 页: {failed[:8]}")
    return out


def fetch_indexes():
    secids = ",".join(s for s, _ in config.A_INDEXES)
    url = (f"https://push2.eastmoney.com/api/qt/ulist.np/get?fltt=2"
           f"&secids={secids}&fields=f2,f3,f12,f14")
    j = get_json(url, referer="https://quote.eastmoney.com/")
    out = {}
    for d in (j.get("data") or {}).get("diff") or []:
        out[d.get("f14")] = {"close": d.get("f2"), "pct": d.get("f3")}
    return out


def fetch_sectors(kind="concept", pages=3):
    """概念/行业板块排行。kind: concept | industry。

    注意 clist 的 pz 上限为 100，超过会被服务端断开，必须分页。
    单页瞬时失败会重试，不静默丢弃。
    """
    import time as _t
    fs = "m:90+t:3" if kind == "concept" else "m:90+t:2"
    out, failed = [], []
    for pn in range(1, pages + 1):
        url = (f"{PUSH2}?pn={pn}&pz={PAGE_SIZE}&po=1&np=1&fltt=2&invt=2"
               f"&fid=f3&fs={fs}"
               f"&fields=f2,f3,f6,f8,f12,f14,f104,f105,f128,f140")
        diff = None
        for attempt in range(6):
            try:
                j = get_json(url, referer="https://quote.eastmoney.com/",
                             retry=1)
                data = j.get("data")
                if data is None:
                    # 东财限流时会返回 {"rc":0,...,"data":null}，
                    # 这不是"没有数据"，必须退避后重试。
                    _t.sleep(0.8 * (attempt + 1))
                    continue
                diff = data.get("diff") or []
                break
            except FetchError:
                _t.sleep(0.5 * (attempt + 1))
        if diff is None:
            failed.append(pn)
            if pn == 1:
                # 第一页就失败说明整个源被限流，不必再试后续页
                print(f"    !! 板块 {kind} 源头限流，跳过")
                break
            continue
        if not diff:
            break
        for d in diff:
            out.append({
                "code": d.get("f12"),
                "name": d.get("f14"),
                "kind": kind,
                "pct": d.get("f3"),
                "turnover": d.get("f8") if isinstance(d.get("f8"), (int, float)) else None,
                "amount": d.get("f6") if isinstance(d.get("f6"), (int, float)) else None,
                "up_count": d.get("f104"),
                "down_count": d.get("f105"),
                "leader_name": d.get("f128"),
                "leader_code": d.get("f140"),
            })
        _t.sleep(0.15)
    if failed:
        print(f"    (板块 {kind} 有 {len(failed)} 页失败: {failed})")
    return out


def fetch_kline(secid, limit=20):
    """日K。板块 secid 形如 90.BK1063，个股 0.002188 / 1.600825。

    字段顺序（fields2=f51..f61）：
      0日期 1开 2收 3高 4低 5量 6额 7振幅 8涨跌幅 9涨跌额 10换手率
    注意 f58 是振幅不是涨跌幅，这里踩过坑。
    """
    url = (f"{PUSH2HIS}?secid={secid}&fields1=f1,f2,f3"
           f"&fields2=f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61"
           f"&klt=101&fqt=1&end=20500101&lmt={limit}")
    j = get_json(url, referer="https://quote.eastmoney.com/")
    data = j.get("data") or {}
    out = []
    for line in data.get("klines") or []:
        p = line.split(",")
        if len(p) < 9:
            continue
        try:
            out.append({
                "date": p[0],
                "open": float(p[1]), "close": float(p[2]),
                "high": float(p[3]), "low": float(p[4]),
                "volume": float(p[5]), "amount": float(p[6]),
                "amplitude": float(p[7]),
                "pct": float(p[8]),
                "change": float(p[9]) if len(p) > 9 else None,
                "turnover": float(p[10]) if len(p) > 10 else None,
            })
        except ValueError:
            continue
    return out


# 兼容旧名
fetch_sector_kline = fetch_kline


def fetch_global():
    secids = ",".join(s for s, _ in config.GLOBAL_INDEXES)
    url = (f"https://push2.eastmoney.com/api/qt/ulist.np/get?fltt=2"
           f"&secids={secids}&fields=f2,f3,f4,f12,f14")
    j = get_json(url, referer="https://quote.eastmoney.com/")
    out = []
    for d in (j.get("data") or {}).get("diff") or []:
        out.append({
            "code": d.get("f12"),
            "name": d.get("f14"),
            "price": d.get("f2"),
            "pct": d.get("f3"),
        })
    return out
