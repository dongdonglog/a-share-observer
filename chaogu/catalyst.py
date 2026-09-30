#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""主线识别：按**真实涨停原因**把涨停股聚成主线，并给出对得上的催化原文。

为什么不用 theme_stock.tag
--------------------------
tag 是同花顺的**概念归属**（一带一路、专精特新、三胎概念……），一只票
同时属于几十个概念。按 tag 聚合再去取成分股的 reason_info，就会出现
"一带一路"下面挂着"地产贴息新政"、"机器人轴承"的催化 —— 原文和题材
完全对不上。

这里改用两条**来自个股本身**的证据：
  1. reason_type（涨停原因标签，如"固态电池+液冷服务器+水处理"）：
     按关键词归并成主线；
  2. reason_info 里的「行业原因」段落：同一段行业原因 = 同一个催化事件
     （如"工信部《新型电池产业发展十五五规划》"），直接作为主线的催化原文。
所以主线下展示的每一段原文，都是该主线成分股**自己的**涨停原因。
"""

from __future__ import annotations

import re
from collections import Counter, defaultdict

# 常见主线关键词（子串匹配到 reason_type 的任一标签即归入）。
# 顺序即优先级：越具体的放越前（"固态电池"先于"锂电"）。
CORE_KEYWORDS = [
    "固态电池", "钠离子电池", "锂电", "储能", "光伏", "风电", "核电", "氢能",
    "充电桩", "电力", "特高压", "电网",
    "机器人", "具身智能", "减速器", "人形", "工业母机",
    "算力", "液冷", "服务器", "CPO", "光模块", "PCB", "数据中心", "东数西算",
    "芯片", "半导体", "存储芯片", "光刻", "先进封装",
    "人工智能", "AI应用", "AI漫剧", "AI医疗", "大模型", "智能体", "AI眼镜",
    "AR/VR", "脑机",
    "华为", "鸿蒙", "昇腾", "信创", "国产软件", "网络安全", "数据要素",
    "低空经济", "无人机", "飞行汽车", "商业航天", "卫星", "军工", "大飞机",
    "汽车零部件", "汽车电子", "智能驾驶", "无人驾驶", "新能源汽车", "一体化压铸",
    "创新药", "CRO", "CXO", "医疗器械", "中药", "疫苗", "减肥药", "医美",
    "房地产", "地产", "物业", "建材", "家居", "装修",
    "消费", "白酒", "食品", "乳业", "零售", "免税", "旅游", "酒店", "预制菜",
    "宠物", "化妆品", "家电", "小家电", "体育",
    "证券", "券商", "保险", "银行", "金融科技", "跨境支付", "数字货币",
    "稀土", "有色", "黄金", "铜", "锂矿", "化工", "染料", "化肥", "磷化工",
    "钢铁", "煤炭", "石油", "天然气", "航运", "港口", "物流",
    "传媒", "游戏", "短剧", "影视", "出版", "AI漫剧",
    "农业", "种业", "猪肉", "养殖",
    "重组", "并购", "借壳", "股权转让", "控制权变更",
]

# 这些标签太泛，不代表主线（只作个股属性展示）
GENERIC = re.compile(
    r"(业绩|扭亏|预增|增长|国资|回购|增持|减持|分红|送转|半年报|年报|季报|"
    r"中报|摘帽|ST|次新|高送转|海外拓展|订单|项目量产|投资)")

# 关键词别名：归并到同一主线名
ALIAS = {
    "具身智能": "机器人", "人形": "机器人", "减速器": "机器人",
    "液冷": "算力", "服务器": "算力", "数据中心": "算力", "东数西算": "算力",
    "CPO": "算力", "光模块": "算力",
    "CXO": "CRO", "地产": "房地产", "物业": "房地产",
    "锂电": "锂电池", "钠离子电池": "固态电池",
    "大模型": "人工智能", "AI应用": "人工智能", "智能体": "人工智能",
    "AI漫剧": "传媒", "AI医疗": "医疗", "存储芯片": "芯片",
    "无人驾驶": "智能驾驶", "飞行汽车": "低空经济", "无人机": "低空经济",
    "券商": "证券", "小家电": "家电", "借壳": "重组", "并购": "重组",
    "控制权变更": "重组", "股权转让": "重组",
}

_SEG_IND = re.compile(r"行业原因[:：]\s*(.*?)(?=公司原因[:：]|$)", re.S)
_SEG_CO = re.compile(r"公司原因[:：]\s*(.*)$", re.S)
_DATE_MD = re.compile(r"(\d{1,2})月(\d{1,2})日")


def split_reason(info):
    """reason_info -> (行业原因, 公司原因)。无分段时整段视为公司原因。"""
    info = (info or "").strip()
    if not info:
        return "", ""
    m = _SEG_IND.search(info)
    ind = m.group(1).strip() if m else ""
    m2 = _SEG_CO.search(info)
    co = m2.group(1).strip() if m2 else ("" if m else info)
    return ind, co


def first_point(text, n=90):
    """取"1、……"第一条，截断到 n 字。"""
    t = (text or "").strip()
    parts = re.split(r"\n?\s*\d、", t)
    parts = [p.strip() for p in parts if p.strip()]
    s = parts[0] if parts else t
    return s if len(s) <= n else s[:n] + "…"


def _shingles(s, k=4):
    s = re.sub(r"\s+|\d、", "", s or "")[:220]
    return {s[i:i + k] for i in range(max(0, len(s) - k + 1))}


def _jacc(a, b):
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def _items(text):
    """行业原因按"1、2、"拆成条目，每条做 shingle。

    同一篇研报常被不同个股以**不同顺序**引用（ESMO 在前 / AI4S 在前），
    整段比对会把它们判成两个事件；逐条比对才能认出是同一催化。
    """
    parts = [p for p in re.split(r"\n?\s*\d、", text or "") if len(p.strip()) >= 10]
    return [_shingles(p) for p in (parts or [text])]


def _sim(a, b):
    return max((_jacc(x, y) for x in a for y in b), default=0.0)


def tokens(reason):
    return [t.strip() for t in re.split(r"[+＋]", reason or "") if t.strip()]


def keywords_of(reason):
    """reason_type -> 主线关键词集合（已归并别名）。"""
    out = []
    toks = tokens(reason)
    for t in toks:
        for kw in CORE_KEYWORDS:
            if kw in t:
                k = ALIAS.get(kw, kw)
                if k not in out:
                    out.append(k)
    return out


# ------------------------------------------------------------------ 取数

def stocks_of_day(conn, date):
    """当日涨停股 + 唯一的原因原文（theme_stock 里同一只股票的 reason_info 相同）。"""
    info = {}
    for r in conn.execute(
            "SELECT code, reason, reason_info FROM theme_stock "
            "WHERE trade_date=? AND reason_info IS NOT NULL", (date,)):
        if len(r["reason_info"] or "") > len(info.get(r["code"], ("", ""))[1]):
            info[r["code"]] = (r["reason"] or "", r["reason_info"] or "")
    rows = conn.execute(
        "SELECT code, name, height, first_seal, last_seal, open_num, "
        "seal_amount, turnover, float_mv, reason, lu_type, change_tag "
        "FROM limit_up WHERE trade_date=? ORDER BY height DESC, "
        "COALESCE(first_seal,'99') ASC", (date,)).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        rs, ri = info.get(d["code"], ("", ""))
        d["reason"] = d.get("reason") or rs
        d["reason_info"] = ri
        d["industry_reason"], d["company_reason"] = split_reason(ri)
        d["keywords"] = keywords_of(d["reason"])
        out.append(d)
    return out


# ------------------------------------------------------------------ 聚类

def _event_groups(stocks, thr=0.5):
    """按「行业原因」聚成催化事件（任一条目近似重复即算同一事件）。"""
    groups = []            # [{"text", "items", "codes"}]
    for s in stocks:
        ind = s["industry_reason"]
        if len(ind) < 12:
            continue
        it = _items(ind)
        for g in groups:
            if _sim(it, g["items"]) >= thr:
                g["codes"].append(s["code"])
                break
        else:
            groups.append({"text": ind, "items": it, "codes": [s["code"]]})
    return groups


def event_title(text, n=38):
    """从行业原因里提炼一句话标题：取第一句，去掉"据××报道"类前缀。"""
    t = re.sub(r"^\s*\d、", "", text.strip())
    t = re.split(r"[。；\n]", t)[0]
    t = re.sub(r"^(据[^，,]{0,20}[，,]|[^，,]{0,12}研报指出[，,]?)", "", t)
    return t if len(t) <= n else t[:n] + "…"


def event_date(text):
    m = _DATE_MD.search(text or "")
    return f"{int(m.group(1))}月{int(m.group(2))}日" if m else None


def mainlines(conn, date, min_n=2):
    """当日主线：[{name, stocks[...], catalyst{text,title,date,shared}, ...}]。

    构造顺序：
      1. 催化事件（同一段行业原因带动 ≥2 只）优先成线 —— 它自带原文；
      2. 再按 reason_type 关键词成线；
      3. 成员高度重合的线合并（固态电池 ≈ 新型电池规划事件）。
    每条线的催化原文取成分股里**共享最多**的那段行业原因，
    所以原文一定来自该线自己的成分股，不会张冠李戴。
    """
    stocks = stocks_of_day(conn, date)
    by_code = {s["code"]: s for s in stocks}
    evs = _event_groups(stocks)
    ev_of = {c: i for i, g in enumerate(evs) for c in g["codes"]}

    cands = []
    for i, g in enumerate(evs):
        if len(g["codes"]) < min_n:
            continue
        cnt = Counter(k for c in g["codes"] for k in by_code[c]["keywords"])
        name = None
        if cnt:
            k, n = cnt.most_common(1)[0]
            if n * 2 >= len(g["codes"]):
                name = k
        if not name:
            # 从原文里找主线词（ESMO 研报 -> "创新药"）
            for k0 in CORE_KEYWORDS:
                if k0 in g["text"]:
                    name = ALIAS.get(k0, k0)
                    break
        cands.append({"name": name or event_title(g["text"], 12),
                      "set": set(g["codes"]), "event": i, "alias": []})
    kw = defaultdict(set)
    for s in stocks:
        for k in s["keywords"]:
            kw[k].add(s["code"])
    for k, codes in kw.items():
        if len(codes) >= min_n:
            cands.append({"name": k, "set": set(codes), "event": None,
                          "alias": []})

    # 大的在前；同样大小时事件线优先（它有原文）
    cands.sort(key=lambda x: (-len(x["set"]), x["event"] is None))
    merged = []
    for c in cands:
        for m in merged:
            ov = len(c["set"] & m["set"]) / max(1, min(len(c["set"]),
                                                        len(m["set"])))
            if ov >= 0.75 or c["name"] == m["name"]:
                if c["name"] != m["name"] and c["name"] not in m["alias"]:
                    # 事件线若以研报标题命名，改用更短的关键词名
                    if m["event"] is not None and m["name"] not in kw \
                            and c["event"] is None:
                        m["alias"].append(m["name"])
                        m["name"] = c["name"]
                    else:
                        m["alias"].append(c["name"])
                m["set"] |= c["set"]
                if m["event"] is None:
                    m["event"] = c["event"]
                break
        else:
            merged.append(c)

    out = []
    for m in merged:
        mem = sorted((by_code[c] for c in m["set"]),
                     key=lambda s: (-(s["height"] or 1),
                                    s["first_seal"] or "99"))
        evc = Counter(ev_of[s["code"]] for s in mem if s["code"] in ev_of)
        cat = None
        if evc:
            ei, n = evc.most_common(1)[0]
            if n >= 2 or len(mem) == 1:
                t = evs[ei]["text"]
                cat = {"text": t, "title": event_title(t),
                       "date": event_date(t), "shared": n,
                       "codes": [c for c in evs[ei]["codes"] if c in m["set"]]}
        heights = Counter(s["height"] or 1 for s in mem)
        out.append({
            "name": m["name"],
            "alias": m["alias"],
            "n": len(mem),
            "max_height": mem[0]["height"] or 1,
            "echelon": {int(h): c for h, c in sorted(heights.items(),
                                                     reverse=True)},
            "leader": _brief(mem[0]),
            "catalyst": cat,
            "stocks": [_brief(s) for s in mem],
        })
    out.sort(key=lambda x: (-x["n"], -x["max_height"]))
    return out


def _brief(s):
    return {
        "code": s["code"], "name": s["name"], "height": s["height"] or 1,
        "first_seal": s["first_seal"], "open_num": s["open_num"],
        "seal_amount": s["seal_amount"], "turnover": s["turnover"],
        "float_mv": s["float_mv"], "reason": s["reason"],
        "lu_type": s.get("lu_type"),
        "industry_reason": s["industry_reason"],
        "company_point": first_point(s["company_reason"] or
                                     s["industry_reason"], 110),
        "keywords": s["keywords"],
    }


def events(conn, date, min_n=2):
    """当日催化事件（同一段行业原因带动 ≥min_n 只涨停）。"""
    stocks = stocks_of_day(conn, date)
    by_code = {s["code"]: s for s in stocks}
    out = []
    for g in _event_groups(stocks):
        if len(g["codes"]) < min_n:
            continue
        mem = sorted((by_code[c] for c in g["codes"]),
                     key=lambda s: -(s["height"] or 1))
        out.append({"title": event_title(g["text"]), "date": event_date(g["text"]),
                    "text": g["text"], "n": len(mem),
                    "stocks": [{"code": s["code"], "name": s["name"],
                                "height": s["height"] or 1} for s in mem]})
    out.sort(key=lambda x: -x["n"])
    return out


def keyword_series(conn, keyword, end, n=10):
    """某主线关键词近 n 日的涨停家数（按 reason_type 子串统计）。"""
    kws = [keyword] + [k for k, v in ALIAS.items() if v == keyword]
    dates = [r[0] for r in conn.execute(
        "SELECT DISTINCT trade_date FROM limit_up WHERE trade_date<=? "
        "ORDER BY trade_date DESC LIMIT ?", (end, n))]
    out = []
    for d in reversed(dates):
        c = 0
        h = 0
        for r in conn.execute("SELECT reason, height FROM limit_up "
                              "WHERE trade_date=?", (d,)):
            rs = r["reason"] or ""
            if any(k in t for t in tokens(rs) for k in kws):
                c += 1
                h = max(h, r["height"] or 1)
        out.append({"date": d, "n": c, "max_height": h})
    return out
