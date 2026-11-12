#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""东方财富当日分时成交明细：公开行情能拿到的最细粒度成交。

口径（先读这一段，别把它当成逐笔委托）
--------------------------------------
交易所对外免费分发的是**3 秒快照**，不是逐笔委托。这个接口每一行是
"一个 3 秒窗口内、某个方向的成交聚合"，所以它能回答的是
「这 3 秒里主动买成交了 N 万元」，而不是「某一笔委托 N 万元」。
真正的逐笔（每一笔委托的价与量）需要 Level-2 付费行情。

这不是本模块的将就，而是公开口径的边界：东财自己的"大单/超大单"统计同样
是按这类快照分档得出的，所以用快照金额做"大单"判定，与业界口径一致。

字段
----
响应里 details 的每一行是逗号分隔的字符串：
    "HH:MM:SS,价格,成交量(手),f54,方向"
  方向 f55：1=主动卖盘，2=主动买盘，4=集合竞价（其他值按中性处理）。
  该映射不是照抄文档，而是用相邻快照的价差均值验证过：方向 2 的价差均值为正
  （+0.17），方向 1 为负（-0.17），符合"主动买推高价格"的市场机制。
  第 4 个字段 f54 的语义未能确认（既不是笔数也不是金额：全天下来的分布里
  它有时为 0、有时大于手数），因此本模块**不解析、不使用**它——宁可少一个
  字段，也不把猜测写进判定里。

成交量单位是手，金额由 价格 × 手数 × 100 得出（1 手 = 100 股）。
"""

from __future__ import annotations

import math
import time

from ..net import FetchError, get_json

DETAILS = "https://push2.eastmoney.com/api/qt/stock/details/get"
REFERER = "https://quote.eastmoney.com/"
# pos 取负数表示"最后 N 条"。默认只取最近 300 条，而不是一整天：
#   1) 逐笔监控只需要"上次轮询之后"的那一段，300 条对最活跃的个股也覆盖
#      十几分钟，远大于 60 秒的轮询间隔；
#   2) 一次拉一整天（实测贵州茅台约 3000 行、京东方约 3700 行）既慢又容易
#      触发东财限流，被限流时返回的 data:null 和"停牌无成交"无法区分。
# 需要看全天明细时显式传更大的 limit。
DEFAULT_ROWS = 300
SIDES = {"1": "sell", "2": "buy", "4": "auction"}
BUY, SELL, AUCTION = "buy", "sell", "auction"


def secid(code, market=None):
    """东财 secid。0=深市与北交所，1=沪市。

    北交所必须排在前面判断：920xxx 以 9 开头，但它走的是**0 前缀**，不是沪市。
    照抄"9 开头算沪市"的常见写法（沪 B 900xxx 确实是 1 前缀）会把 920xxx 解析错，
    而接口对错误 secid 返回 data:null，表现成"这只票一直没有成交"。
    实测 0.920690 有数据、1.920690 拿不到。
    """
    c = str(code)
    if market is not None:
        return f"{int(market)}.{c}"
    if c.startswith(("92", "8", "4")):        # 北交所
        return f"0.{c}"
    if c.startswith(("6", "9", "5")):         # 沪市（含 900xxx 沪 B）
        return f"1.{c}"
    return f"0.{c}"


def _num(value):
    if isinstance(value, bool):
        return None
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


def _hms_seconds(text):
    """'HH:MM:SS' -> 当日秒数；无法解析时返回 None。"""
    parts = str(text or "").split(":")
    if len(parts) != 3:
        return None
    try:
        h, m, s = (int(x) for x in parts)
    except ValueError:
        return None
    if not (0 <= h < 24 and 0 <= m < 60 and 0 <= s < 60):
        return None
    return h * 3600 + m * 60 + s


def parse_details(rows):
    """把 details 字符串数组解析成快照列表（按时间升序，接口原本就是升序）。

    残缺行直接跳过：把 schema 变化当成错误处理，而不是当成"没有成交"。
    """
    out = []
    for raw in rows or []:
        parts = str(raw).split(",")
        if len(parts) < 5:
            continue
        seconds = _hms_seconds(parts[0])
        price = _num(parts[1])
        lots = _num(parts[2])
        if seconds is None or price is None or lots is None or price <= 0 or lots < 0:
            continue
        side = SIDES.get(parts[4].strip(), "neutral")
        out.append({"time": parts[0].strip(), "seconds": seconds, "price": price,
                    "lots": int(lots), "amount": price * int(lots) * 100.0,
                    "side": side})
    return out


def fetch_details(code, market=None, limit=DEFAULT_ROWS, attempts=3):
    """拉当日分时成交。返回按时间升序的快照列表（可能为空：当日无成交）。

    东财限流时的表现是 data:null，和"停牌/当日无成交"长得一样，所以这里退避
    重试几次再判定：重试后仍为 null 就当失败抛 FetchError，由调用方按"来源异常"
    处理。把失败静默当成"今天没有大单"，是这类监控最容易骗人的地方。
    代价是遇到真的停牌股会白等约 2 秒——相比误报，这个代价可以接受。
    """
    url = (f"{DETAILS}?secid={secid(code, market)}&fields1=f1,f2,f3,f4"
           f"&fields2=f51,f52,f53,f54,f55&fltt=2&invt=2&pos=-{int(limit)}")
    last = None
    for attempt in range(max(1, attempts)):
        try:
            j = get_json(url, referer=REFERER, retry=2)
        except FetchError as exc:
            last = exc
            time.sleep(0.8 * (attempt + 1))
            continue
        data = j.get("data") if isinstance(j, dict) else None
        if data is None:
            time.sleep(0.8 * (attempt + 1))
            continue
        if str(data.get("code") or "").strip() != str(code):
            raise FetchError(f"{code} 分时成交返回了别的证券：{data.get('code')}")
        details = data.get("details")
        if details is None:
            return []                     # 当日无成交（停牌/未开盘）
        if not isinstance(details, list):
            raise FetchError(f"{code} 分时成交缺少 details 字段")
        return parse_details(details)
    if last is not None:
        raise last
    raise FetchError(f"{code} 分时成交连续返回空数据（限流或当日无成交）")


def big_buys(ticks, min_amount, *, side=BUY, since_seconds=None, session_only=True):
    """筛出达到金额门槛的大额快照。

    session_only 默认为真：集合竞价的成交没有主动买/主动卖之分，接口却会给它标
    方向，因此在这里就挡掉，避免任何调用方漏掉这一步。实测贵州茅台当天有 115 条
    ≥100 万的集合竞价快照，不挡就会全被当成"主动买大单"。
    since_seconds 只保留不早于该当日秒数的快照（含边界），用来看"最近 N 分钟"，
    避免首次启用时把一整天的历史大单一次性推成提醒。
    """
    out = []
    for t in ticks or []:
        if t.get("amount") is None or t["amount"] < min_amount:
            continue
        if side != "both" and t.get("side") != side:
            continue
        if session_only and not in_session_hms(t.get("seconds") or 0):
            continue
        if since_seconds is not None and (t.get("seconds") or 0) < since_seconds:
            continue
        out.append(t)
    return out


def in_session_hms(seconds):
    """该秒数是否落在连续竞价时段（09:30:00-11:29:59 / 13:00:00-14:59:59）。

    两端都取开区间，是为了把集合竞价的成交排除掉：开盘集合竞价（09:15-09:25）
    与收盘集合竞价（深市 14:57 起、成交在 15:00:00 打印）没有"主动买/主动卖"
    的区分，接口却会给它们标上方向。不排除的话，贵州茅台开盘竞价那一笔
    4394 万会被当成"主动买大单"推给你。
    """
    return (34200 <= seconds < 41400) or (46800 <= seconds < 54000)
