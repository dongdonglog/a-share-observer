#!/usr/bin/env python3
"""量能榜 / 大单买入榜的离线测试：单位换算、排序口径、阈值过滤、异常兜底。

全部用假数据，不联网、不读写用户真实的 data/。

为什么单独测这两项
------------------
这两个面板把元换算成亿/万、把手换算成万手，还要在东财字段之间切换排序口径。
换算错一位就是 100 倍误差，而界面上看起来完全正常（"成交额 1.23 亿" 和
"成交额 123.00 亿" 都长得像正常数字），所以必须用已知答案锁住。

用法:
    python3 tools/test_rank_panels.py
退出码 0 表示全部通过。
"""

from __future__ import annotations

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from chaogu import panels                      # noqa: E402
from chaogu.sources import fundflow            # noqa: E402

FAILED = []
DATE = "2026-10-16"


def check(name, cond, detail=""):
    tag = "✅" if cond else "❌"
    print(f"  {tag} {name}" + (f"  ({detail})" if detail else ""))
    if not cond:
        FAILED.append(name)


def rows_for(fid):
    """已知答案的三行假数据：成交额/成交量/量比都错开，便于验证排序口径。"""
    base = [
        {"code": "600000", "name": "甲", "price": 10.0, "pct": 1.0, "amount": 1.0e9,
         "volume": 1.0e5, "turnover": 1.0, "volume_ratio": 3.0,
         "main_net": 5.0e7, "super_net": 4.0e7, "large_net": 1.0e7, "main_ratio": 5.0},
        {"code": "600001", "name": "乙", "price": 20.0, "pct": -2.0, "amount": 2.0e10,
         "volume": 2.0e7, "turnover": 2.0, "volume_ratio": 1.0,
         "main_net": 2.0e8, "super_net": 1.0e8, "large_net": 1.0e8, "main_ratio": 1.0},
        {"code": "600002", "name": "丙", "price": 30.0, "pct": 3.0, "amount": 3.0e8,
         "volume": 3.0e4, "turnover": 3.0, "volume_ratio": 9.0,
         "main_net": 2.0e6, "super_net": 1.0e6, "large_net": 1.0e6, "main_ratio": 0.7},
    ]
    key = {"f6": "amount", "f5": "volume", "f10": "volume_ratio"}[fid]
    return sorted(base, key=lambda r: r[key], reverse=True)


def patch(volume_fid_seen, fail=False):
    """替换数据源。记录实际请求的 fid，用来验证 sort 参数真的换了字段。"""
    volume_fid_seen.clear()          # 每条用例只看自己这一次请求
    def fake_board(fid="f6", direction="in", limit=40, page_size=100):
        volume_fid_seen.append(fid)
        if fail:
            raise RuntimeError("模拟限流")
        return rows_for(fid)[:limit]

    def fake_rank(fid="f62", direction="in", limit=400, page_size=100):
        if fail:
            raise RuntimeError("模拟限流")
        return sorted(rows_for("f6"), key=lambda r: r["main_net"], reverse=True)[:limit]

    fundflow.fetch_volume_board = fake_board
    fundflow.fetch_rank = fake_rank
    panels._CACHE.clear()


def main():
    real_board, real_rank = fundflow.fetch_volume_board, fundflow.fetch_rank

    print("量能榜")
    seen = []
    patch(seen)
    d = panels.q_volume_rank(None, DATE, {})
    check("默认按成交额取 f6", seen == ["f6"], f"实际请求 {seen}")
    check("默认取第一条是成交额最大的乙", d["rows"][0]["name"] == "乙")
    check("成交额 2e10 元 → 200.0 亿", d["rows"][0]["amount"] == 200.0,
          str(d["rows"][0]["amount"]))
    check("成交量 2e7 手 → 2000.0 万手", d["rows"][0]["volume"] == 2000.0,
          str(d["rows"][0]["volume"]))
    check("量比原样透传", d["rows"][0]["volume_ratio"] == 1.0)
    check("列名与数据键一一对应",
          all(k in d["rows"][0] for k, _ in d["columns"]),
          str([k for k, _ in d["columns"]]))
    check("note 带口径与来源", "成交额降序" in d["note"] and "东方财富" in d["note"],
          d["note"])

    patch(seen)
    d = panels.q_volume_rank(None, DATE, {"sort": "ratio", "limit": 2})
    check("sort=ratio 改用 f10", seen == ["f10"], f"实际请求 {seen}")
    check("量比降序第一条是丙", d["rows"][0]["name"] == "丙", d["rows"][0]["name"])
    check("limit 生效", len(d["rows"]) == 2, str(len(d["rows"])))

    patch(seen)
    d = panels.q_volume_rank(None, DATE, {"sort": "不存在的口径"})
    check("未知口径回落到成交额", seen == ["f6"], f"实际请求 {seen}")

    patch(seen)
    d = panels.q_volume_rank(None, DATE, {"min_yi": 100})
    check("成交额下限能筛掉小票（100 亿只剩乙）",
          [r["name"] for r in d["rows"]] == ["乙"], str([r["name"] for r in d["rows"]]))

    patch(seen, fail=True)
    d = panels.q_volume_rank(None, DATE, {})
    check("数据源异常时不抛错、rows 为空", d["rows"] == [])
    check("数据源异常写进 note 而不是谎报无数据", "数据源异常" in d["note"], d["note"])

    print("\n大单买入榜")
    seen = []
    patch(seen)
    d = panels.q_big_order(None, DATE, {})
    check("默认阈值 100 万，三只都过阈值",
          [r["name"] for r in d["rows"]] == ["乙", "甲", "丙"],
          str([r["name"] for r in d["rows"]]))
    check("主力净额 2e8 元 → 20000.0 万", d["rows"][0]["main_wan"] == 20000.0,
          str(d["rows"][0]["main_wan"]))
    check("超大单/大单拆分各自换万",
          d["rows"][0]["super_wan"] == 10000.0 and d["rows"][0]["large_wan"] == 10000.0,
          f"{d['rows'][0]['super_wan']} / {d['rows'][0]['large_wan']}")
    check("净占比与成交额（亿）换算正确",
          d["rows"][0]["main_ratio"] == 1.0 and d["rows"][0]["amount"] == 200.0,
          f"{d['rows'][0]['main_ratio']} / {d['rows'][0]['amount']}")

    patch(seen)
    d = panels.q_big_order(None, DATE, {"min_wan": 10000})
    check("阈值 10000 万筛掉 5000 万的甲",
          [r["name"] for r in d["rows"]] == ["乙"], str([r["name"] for r in d["rows"]]))
    check("note 报出窗口内命中数", "命中 1 只" in d["note"], d["note"])

    patch(seen)
    d = panels.q_big_order(None, DATE, {"min_wan": 0, "limit": 2})
    check("阈值 0 + limit 2 只显示前两名", len(d["rows"]) == 2, str(len(d["rows"])))

    patch(seen, fail=True)
    d = panels.q_big_order(None, DATE, {})
    check("数据源异常时不抛错、note 提示", d["rows"] == [] and "数据源异常" in d["note"],
          d["note"])

    print("\n面板注册与编排")
    check("CATALOG 收下了两个新面板",
          "volume_rank" in panels.BY_ID and "big_order" in panels.BY_ID)
    check("两个面板都有可信度登记（trust/basis）",
          all(panels.BY_ID[i].get("trust") and panels.BY_ID[i].get("basis")
              for i in ("volume_rank", "big_order")))
    check("查询函数已挂进 QUERIES",
          "volume_rank" in panels.QUERIES and "big_order" in panels.QUERIES)
    # 新面板是"目录里的面板"，不被塞进内置看板：内置看板保持原有编排，
    # 用户用「+ 添加面板」自己加（这就是产品方向：面板可加，看板由用户搭）。
    default_ids = {p["id"] for d in panels.default_dashboards() for p in d["panels"]}
    check("新面板没有被自动塞进内置看板",
          not ({"volume_rank", "big_order", "tick_watch"} & default_ids),
          str(sorted(default_ids & {"volume_rank", "big_order", "tick_watch"})))
    # 一句话生成要认用户自己的说法：「交易量」和「成交量」都得命中。
    for prompt in ("A 股市场交易量", "今日交易量", "A股交易量排行", "今日成交额和量比排行"):
        built = {p["id"] for p in panels._rule_build(prompt)["panels"]}
        check(f"一句话「{prompt}」→ 量能榜", built == {"volume_rank"}, str(built))
    built = {p["id"] for p in panels._rule_build("百万大单买入榜")["panels"]}
    check("一句话「百万大单买入榜」→ 大单榜", "big_order" in built, str(built))
    check("具体诉求不再被通用建议面板灌水（不塞 verdict/outlook）",
          not ({"verdict", "outlook", "ai_brief"} & built), str(built))
    built = {p["id"] for p in panels._rule_build("自选股的逐笔大单成交")["panels"]}
    check("说「逐笔」时只给大单成交监控，不给聚合净额榜",
          built == {"tick_watch"}, str(built))
    built = {p["id"] for p in panels._rule_build("明天重点关注哪些题材和龙头")["panels"]}
    check("没有具体诉求时才给通用建议面板（明日关注/主线）",
          "mainlines" in built, str(built))
    for pid in ("volume_rank", "big_order"):
        params, dropped = panels.clean_params(pid, {"limit": 20, "sort": "ratio",
                                                    "min_yi": 0, "min_wan": 100,
                                                    "bogus": 1})
        check(f"{pid} 参数清洗丢弃未知键、保留合法键",
              "bogus" in dropped and params, f"{params} 丢弃 {dropped}")

    print("\n量能榜字段常量")
    fundflow.fetch_volume_board, fundflow.fetch_rank = real_board, real_rank
    check("RANK_FIELDS 含成交量 f5/成交额 f6/换手 f8/量比 f10",
          all(x in fundflow.RANK_FIELDS.split(",") for x in ("f5", "f6", "f8", "f10")),
          fundflow.RANK_FIELDS)
    check("fetch_volume_board 默认走 f6 成交额",
          fundflow.fetch_volume_board.__defaults__[0] == "f6",
          str(fundflow.fetch_volume_board.__defaults__))
    try:
        fundflow.fetch_volume_board(fid="f62")
        check("非法 fid 被拒绝", False, "没有抛 ValueError")
    except ValueError:
        check("非法 fid 被拒绝", True)
    except Exception as exc:                       # noqa: BLE001
        check("非法 fid 被拒绝", False, f"抛了 {type(exc).__name__}")

    fundflow.fetch_volume_board, fundflow.fetch_rank = real_board, real_rank
    print()
    if FAILED:
        print(f"❌ {len(FAILED)} 项失败: {FAILED}")
        return 1
    print("✅ 全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
