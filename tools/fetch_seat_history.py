"""席位历史战绩：连接席位买入明细与龙虎榜前向收益，独立验证席位胜率。

要回答的问题
------------
东财在席位明细里给了一个 RISE_PROBABILITY_3DAY 字段（看起来是"该席位3日胜率"）。
本脚本用龙虎榜自带的前向收益做独立验证：这个数字是不是真战绩？

若为真战绩，用它排序选出"厉害的席位"应当能带来正的跟随收益；
若它与真实前向收益无关，那它只是数据商的历史回看统计，不能用来跟。

增量落盘：每 10 个交易日写一次 /tmp/seat_history.json，中断可续。
"""

from __future__ import annotations

import json
import pathlib
import sys
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from chaogu import db, net
from chaogu.sources import lhb

OUT = pathlib.Path("/tmp/seat_history.json")
LHB = pathlib.Path("/tmp/lhb_2026.json")


def main():
    rows = json.loads(LHB.read_text())
    ret = {}
    for r in rows:
        k = (r["SECURITY_CODE"], r["TRADE_DATE"][:10])
        ret.setdefault(k, (
            r.get("D1_CLOSE_ADJCHRATE"),
            r.get("D5_CLOSE_ADJCHRATE"),
            r.get("D10_CLOSE_ADJCHRATE"),
        ))
    print(f"龙虎榜前向收益索引: {len(ret)} 个 (代码,日期)", flush=True)

    conn = db.init()
    dates = [r[0] for r in conn.execute(
        "SELECT DISTINCT trade_date FROM daily_market "
        "WHERE trade_date>='2026-01-01' AND trade_date<='2026-09-30' "
        "ORDER BY trade_date")]
    print(f"交易日 {len(dates)} 天", flush=True)

    out = []
    done = set()
    if OUT.exists():
        try:
            out = json.loads(OUT.read_text())
            done = {x["d"] for x in out}
            print(f"续跑：已有 {len(done)} 天 / {len(out)} 条", flush=True)
        except Exception:
            out, done = [], set()

    for i, d in enumerate(dates):
        if d in done:
            continue
        try:
            recs = lhb.fetch_seat_detail(d, "buy", max_pages=3)
        except Exception as exc:
            print(f"  {d} 失败: {exc}", flush=True)
            time.sleep(1.0)
            continue
        for r in recs:
            code = r.get("SECURITY_CODE")
            f = ret.get((code, d))
            if not f or f[0] is None:
                continue
            out.append({
                "d": d,
                "seat": r.get("OPERATEDEPT_NAME"),
                "code": code,
                "buy": r.get("BUY"),
                "sell": r.get("SELL"),
                "net": r.get("NET"),
                "rep_prob": r.get("RISE_PROBABILITY_3DAY"),
                "rep_n": r.get("TOTAL_BUYER_SALESTIMES_3DAY"),
                "d1": f[0], "d5": f[1], "d10": f[2],
                "chg": r.get("CHANGE_RATE"),
            })
        if i % 10 == 0:
            OUT.write_text(json.dumps(out, ensure_ascii=False))
            print(f"  {i}/{len(dates)} 累计 {len(out)} 条", flush=True)

    OUT.write_text(json.dumps(out, ensure_ascii=False))
    net.close_all()
    conn.close()
    print(f"完成 {len(out)} 条 -> {OUT}", flush=True)


if __name__ == "__main__":
    main()
