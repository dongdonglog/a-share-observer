"""把 /tmp/seat_history.json 的验证样本导入数据库。

为什么用导入而不是重抓
----------------------
那份 JSON 已经包含 2026-01-05 ~ 2026-09-28 的 75,875 条席位记录，
且每条都关联了龙虎榜前向收益（d1/d5/d10）。重新抓一遍要 ~180 个交易日
× 3 类接口，既慢又可能触发限流，而数据是同一来源、同一时点。

导入后 seat_trade / billboard 即可支撑 seat.py 的样本外验证。
"""

from __future__ import annotations

import json
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from chaogu import db

SRC = pathlib.Path("/tmp/seat_history.json")
# 同时导入龙虎榜明细（含完整 D 系列）
LHB = pathlib.Path("/tmp/lhb_2026.json")


def main():
    conn = db.init()

    # ---- 1. 龙虎榜明细（含 D1/D5/D10 前向收益）
    if LHB.exists():
        rows = json.loads(LHB.read_text())
        out = []
        for r in rows:
            out.append({
                "trade_date": (r.get("TRADE_DATE") or "")[:10],
                "code": r.get("SECURITY_CODE"),
                "name": r.get("SECURITY_NAME_ABBR"),
                "pct": r.get("CHANGE_RATE"),
                "turnover": r.get("TURNOVERRATE"),
                "reason": r.get("EXPLANATION"),
                "change_type": str(r.get("CHANGE_TYPE") or ""),
                "buy_amt": r.get("BILLBOARD_BUY_AMT"),
                "sell_amt": r.get("BILLBOARD_SELL_AMT"),
                "net_amt": r.get("BILLBOARD_NET_AMT"),
                "explain": r.get("EXPLAIN"),
                "d1_adj": r.get("D1_CLOSE_ADJCHRATE"),
                "d2_adj": r.get("D2_CLOSE_ADJCHRATE"),
                "d5_adj": r.get("D5_CLOSE_ADJCHRATE"),
                "d10_adj": r.get("D10_CLOSE_ADJCHRATE"),
                "d20_adj": r.get("D20_CLOSE_ADJCHRATE"),
                "d30_adj": r.get("D30_CLOSE_ADJCHRATE"),
                "source": "eastmoney",
            })
        n = db.upsert(conn, "billboard", out, ["trade_date", "code", "reason"])
        conn.commit()
        print(f"billboard 导入 {n} 行 (源 {len(rows)})")

    # ---- 2. 席位明细（带 rep_prob，前向收益通过 billboard 关联）
    if SRC.exists():
        rows = json.loads(SRC.read_text())
        out = []
        seen = set()
        for r in rows:
            key = (r["d"], r.get("seat"), "buy", r.get("code"))
            if key in seen:
                continue
            seen.add(key)
            out.append({
                "trade_date": r["d"],
                "seat": r.get("seat"),
                "side": "buy",
                "code": r.get("code"),
                "name": None,
                "pct": r.get("chg"),
                "buy_amt": r.get("buy"),
                "sell_amt": r.get("sell"),
                "net_amt": r.get("net"),
                "rep_prob": r.get("rep_prob"),
                "rep_n": r.get("rep_n"),
                "source": "eastmoney",
            })
        n = db.upsert(conn, "seat_trade", out,
                      ["trade_date", "seat", "side", "code"])
        conn.commit()
        print(f"seat_trade 导入 {n} 行 (源 {len(rows)}, 去重后 {len(out)})")

    for t in ("billboard", "seat_trade", "seat_daily"):
        cnt = conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
        days = conn.execute(
            f"SELECT COUNT(DISTINCT trade_date) FROM {t}").fetchone()[0]
        print(f"  {t:<12} {cnt:>7} 行 / {days:>4} 天")
    conn.close()


if __name__ == "__main__":
    main()
