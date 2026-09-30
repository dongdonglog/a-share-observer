#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""历史价格回填。

为什么需要它：
  quote_daily 只有"当天采集"才有数据，而计算
  「昨日涨停今日表现（赚钱效应）」需要知道昨日涨停股**今天**的涨跌幅。
  要回看历史周期，就必须把历史日K补齐。

策略：
  1. 从东财取全市场股票名录（约 5900 只）
  2. 逐只拉日K（一次请求返回整段历史），写入 quote_daily
  3. 有了它，溢价/晋级率/跌停家数/涨跌家数都能算历史

全量回填较慢（数千次请求），因此支持后台运行与断点续传：
已回填过的股票会跳过（记录在 stock_basic.price_synced）。
"""

from __future__ import annotations

import time

from . import config, db
from .net import FetchError, get_json
from .sources import eastmoney as em
from .sources import sina as sn
from .sources import tencent as tc

MARKET_FS = em.MARKET_FS


def derive_market(code):
    """按代码前缀推导市场：1=沪，0=深/北。

    注意 920xxx 是北交所（属 0），不能因为以 9 开头就判成沪市。
    """
    c = str(code)
    if c.startswith(("92", "8", "4")):
        return 0                      # 北交所
    if c.startswith(("6", "9", "5")):
        return 1                      # 沪市（含 900 B股、5xx 基金）
    return 0                          # 深市


def fetch_universe(pages=70):
    """全市场股票名录 -> [{code, name, market}]。

    必须**按代码排序**（fid=f12, po=0）。早先按涨跌幅排序，
    盘中价格变动会让股票在页间漂移，造成大量重复与遗漏
    （实测漏掉 1000+ 只）。
    """
    out, page = [], 1
    while page <= pages:
        url = (f"{em.PUSH2}?pn={page}&pz={em.PAGE_SIZE}&po=0&np=1&fltt=2"
               f"&invt=2&fid=f12&fs={MARKET_FS}&fields=f12,f13,f14")
        diff = None
        for attempt in range(4):
            try:
                j = get_json(url, referer="https://quote.eastmoney.com/",
                             retry=1)
                data = j.get("data")
                if data is None:
                    time.sleep(0.6 * (attempt + 1))
                    continue
                diff = data.get("diff") or []
                break
            except FetchError:
                time.sleep(0.5 * (attempt + 1))
        if diff is None:
            print(f"    名录第 {page} 页多次失败，跳过")
            page += 1
            continue
        if not diff:
            break
        for d in diff:
            code = d.get("f12")
            if not code:
                continue
            out.append({"code": code, "name": d.get("f14"),
                        "market": d.get("f13")})
        page += 1
        time.sleep(0.12)
    # 去重
    seen, uniq = set(), []
    for r in out:
        if r["code"] not in seen:
            seen.add(r["code"])
            uniq.append(r)
    return uniq


def secid(code, market=None):
    """构造东财 secid。market: 1=沪 0=深/北。"""
    if market in (0, 1):
        return f"{market}.{code}"
    return f"{derive_market(code)}.{code}"


def save_universe(conn, rows):
    """写入/更新名录。不覆盖已有的非空 name/market。"""
    n = 0
    with conn:
        for r in rows:
            conn.execute(
                "INSERT INTO stock_basic (code, name, market) VALUES (?,?,?) "
                "ON CONFLICT(code) DO UPDATE SET "
                "name=COALESCE(excluded.name, stock_basic.name), "
                "market=COALESCE(excluded.market, stock_basic.market)",
                (r["code"], r.get("name"), r.get("market")))
            n += 1
    return n


# 熔断器：某个源连续失败若干次后，本轮跳过它。
# 东财/腾讯在批量抓取后会限流，没有熔断的话每只股票都要白等重试。
_SRC_FAILS = {}
_SRC_DISABLED = set()
SRC_FAIL_LIMIT = 4


def _src_ok(name):
    return name not in _SRC_DISABLED


def _src_result(name, success):
    if success:
        _SRC_FAILS[name] = 0
        return
    _SRC_FAILS[name] = _SRC_FAILS.get(name, 0) + 1
    if _SRC_FAILS[name] >= SRC_FAIL_LIMIT and name not in _SRC_DISABLED:
        _SRC_DISABLED.add(name)
        print(f"    [熔断] {name} 连续失败 {SRC_FAIL_LIMIT} 次，本轮跳过")


def backfill_one(conn, code, market, days=160, verbose=False, force=False):
    """拉一只股票的日K并写入 quote_daily。返回写入行数。

    多源回退：东财 → 腾讯 → 新浪。
    三个源的口径已交叉验证一致（新浪与腾讯前复权涨跌幅 36 个点零差异）。
    """
    sid = secid(code, market)
    kl, src = [], None
    sources = [
        ("eastmoney", lambda: em.fetch_kline(sid, limit=days)),
        ("tencent", lambda: tc.fetch_kline(code, market, days=days)),
        ("sina", lambda: sn.fetch_kline(code, market, days=days)),
    ]
    for name, fn in sources:
        if not _src_ok(name):
            continue
        try:
            kl = fn() or []
        except FetchError:
            kl = []
        except Exception:                              # noqa: BLE001
            kl = []
        _src_result(name, bool(kl))
        if kl:
            src = name
            break
    if not kl:
        if verbose:
            print(f"      {code} 所有源都失败")
        return 0
    rows = [{
        "trade_date": k["date"], "code": code, "name": None,
        "pct": k.get("pct"), "turnover": k.get("turnover"),
        "amount": k.get("amount"), "close": k.get("close"),
    } for k in kl if k.get("pct") is not None or k.get("close") is not None]
    n = db.upsert_quotes(conn, rows)
    with conn:
        conn.execute("UPDATE stock_basic SET price_synced=1, "
                     "price_source=? WHERE code=?", (src, code))
    return n


def backfill_all(days=160, limit=None, only_codes=None, verbose=True,
                 progress_every=100):
    """全量/指定范围回填历史价格。"""
    conn = db.init()
    try:
        if only_codes:
            # 不依赖名录：缺的直接补进 stock_basic，市场按代码前缀推导
            rows = []
            for c in only_codes:
                rows.append({"code": c, "name": None,
                             "market": derive_market(c)})
            save_universe(conn, rows)
            ph = ",".join("?" for _ in only_codes)
            rows = conn.execute(
                f"SELECT code, market FROM stock_basic "
                f"WHERE COALESCE(price_synced,0)=0 AND code IN ({ph}) "
                f"ORDER BY code", list(only_codes)).fetchall()
        else:
            cnt = conn.execute("SELECT COUNT(*) FROM stock_basic").fetchone()[0]
            if cnt < 4000:
                if verbose:
                    print(f"  名录只有 {cnt} 只，重新拉取…")
                uni = fetch_universe()
                save_universe(conn, uni)
                if verbose:
                    print(f"  名录 {len(uni)} 只")
            rows = conn.execute(
                "SELECT code, market FROM stock_basic "
                "WHERE COALESCE(price_synced,0)=0 ORDER BY code").fetchall()
        if limit:
            rows = rows[:limit]
        total = len(rows)
        if verbose:
            print(f"  待回填 {total} 只（每次 {days} 根日K）")
        done = saved = 0
        for r in rows:
            n = backfill_one(conn, r["code"], r["market"], days=days)
            saved += n
            done += 1
            if verbose and done % progress_every == 0:
                print(f"    {done}/{total} 已处理，累计写入 {saved} 行")
        if verbose:
            print(f"  完成：{done} 只，写入 {saved} 行")
        return done, saved
    finally:
        conn.close()
