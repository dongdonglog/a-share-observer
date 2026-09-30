"""东方财富 龙虎榜 / 营业部席位数据源。

与 sources/eastmoney.py 的 push2 行情接口不同，这里走 datacenter-web 域，
实测未被限流、历史深度到 2015 年，且自带前向收益字段（D1/D5/D10/D30），
是做"席位跟随"实证检验的关键数据。

已知约束（实测）：
  - pageSize 上限 500（传 1000/5000 会被截断为 500）
  - result 为 null 时表示被限流或报表名错误，需区分重试与放弃
  - D 系列前向收益对最近日期为 None（未来尚未发生），历史日期有值
"""

from __future__ import annotations

import json

from .. import net

BASE = "https://datacenter-web.eastmoney.com/api/data/v1/get"
REFERER = "https://data.eastmoney.com/"

# pageSize 实测上限
MAX_PAGE = 500

# 龙虎榜个股明细报表（含 D1/D2/D5/D10/D20/D30 前向涨幅）
RPT_DAILY = "RPT_DAILYBILLBOARD_DETAILSNEW"
# 每日活跃席位榜
RPT_ACTIVE_SEAT = "RPT_OPERATEDEPT_ACTIVE"
# 席位-个股买入明细（含 RISE_PROBABILITY_3DAY）
RPT_SEAT_BUY = "RPT_BILLBOARD_DAILYDETAILSBUY"
RPT_SEAT_SELL = "RPT_BILLBOARD_DAILYDETAILSSELL"
# 营业部交易（BUY_AMT_REAL 为真实金额）
RPT_SEAT_TRADE = "RPT_OPERATEDEPT_TRADE"


class ReportMissing(Exception):
    """报表名不存在（不可重试）。"""


class Throttled(Exception):
    """result 为 null，疑似限流（可重试）。"""


def _query(report: str, *, columns: str = "ALL", filter_: str | None = None,
           page: int = 1, size: int = MAX_PAGE, sort: str | None = None,
           sort_type: int = -1, retry: int = 2) -> dict:
    """查询一个报表页，返回 {count, pages, data}。

    区分三种失败：报表不存在 / 限流 / 网络错误。
    """
    size = min(size, MAX_PAGE)
    url = net.qs(
        BASE,
        reportName=report,
        columns=columns,
        pageNumber=page,
        pageSize=size,
        source="WEB",
        client="WEB",
    )
    if filter_:
        url += "&filter=" + filter_
    if sort:
        url += f"&sortColumns={sort}&sortTypes={sort_type}"

    raw = net.get_text(url, referer=REFERER, retry=retry)
    try:
        doc = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise Throttled(f"{report} 返回非 JSON（可能限流）: {raw[:120]}") from exc

    if not doc.get("success"):
        msg = doc.get("message") or ""
        if "报表配置不存在" in msg:
            raise ReportMissing(f"{report}: {msg}")
        raise Throttled(f"{report}: {msg or doc.get('code')}")

    res = doc.get("result")
    if res is None:
        # 合法空集与限流都表现为 null；用 count 无法区分，交给调用方决定
        return {"count": 0, "pages": 0, "data": []}
    return {
        "count": res.get("count") or 0,
        "pages": res.get("pages") or 0,
        "data": res.get("data") or [],
    }


def _date_filter(start: str, end: str | None = None,
                 field: str = "TRADE_DATE") -> str:
    end = end or start
    return f"({field}%3E%3D%27{start}%27)({field}%3C%3D%27{end}%27)"


def fetch_billboard(start: str, end: str | None = None,
                    max_pages: int = 60) -> list[dict]:
    """龙虎榜个股明细（含前向收益 D1/D5/D10/D30）。

    返回字段中 D1_CLOSE_ADJCHRATE 等即"上榜后 N 日涨幅"，
    是检验"上榜是否有效"的现成标签。最近日期的 D 值为 None。
    """
    return _paged(RPT_DAILY, _date_filter(start, end), max_pages,
                  sort="TRADE_DATE")


def fetch_active_seats(date: str, max_pages: int = 5,
                       sort: str = "TOTAL_NETAMT") -> list[dict]:
    """某日活跃席位榜（按净买入排序）。

    BUY_STOCK / SECURITY_NAME_ABBR 是该席位当日买入的股票，
    空格分隔且与代码一一对应。
    """
    return _paged(RPT_ACTIVE_SEAT, _date_filter(date, field="ONLIST_DATE"),
                  max_pages, sort=sort)


def fetch_seat_detail(date: str, side: str = "buy",
                      max_pages: int = 20) -> list[dict]:
    """席位-个股明细。side='buy'/'sell'。

    buy 表含 RISE_PROBABILITY_3DAY（该席位历史 3 日上涨概率）与
    TOTAL_BUYER_SALESTIMES_3DAY（3 日操作次数）。
    """
    report = RPT_SEAT_BUY if side == "buy" else RPT_SEAT_SELL
    return _paged(report, _date_filter(date), max_pages)


def _paged(report: str, filter_: str, max_pages: int,
           sort: str | None = None) -> list[dict]:
    out: list[dict] = []
    page = 1
    while page <= max_pages:
        r = _query(report, filter_=filter_, page=page, sort=sort)
        rows = r["data"]
        if not rows:
            break
        out.extend(rows)
        if page >= (r["pages"] or 1):
            break
        page += 1
    return out


def parse_buy_stocks(row: dict) -> list[tuple[str, str]]:
    """把活跃席位行的 BUY_STOCK / SECURITY_NAME_ABBR 配对成 [(code, name)]。"""
    codes = (row.get("BUY_STOCK") or "").split()
    names = (row.get("SECURITY_NAME_ABBR") or "").split()
    out = []
    for i, c in enumerate(codes):
        code = c.split(".")[0]
        out.append((code, names[i] if i < len(names) else ""))
    return out