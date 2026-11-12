"""资产分布及基金板块研究；仅使用券商快照与已有本地市场数据。"""
from __future__ import annotations

import re


def is_fund(code, name=""):
    # 沪深场内基金号段；不将名称不明的六位证券猜成场外基金。
    return bool(re.fullmatch(r"(?:1[568]\d{4}|5\d{5})", code)) or "基金" in name or "ETF" in name.upper() or "LOF" in name.upper()


def allocation(snapshot):
    equity = snapshot.get("equity")
    if not snapshot.get("complete") or equity is None:
        return {"items": [], "notes": ["尚无完整账户快照，不能计算资金分布。"], "checks": []}
    stock = sum(p["value"] for p in snapshot["positions"])
    funds = sum(p["value"] for p in snapshot.get("fund_positions", []))
    other = sum(p["value"] for p in snapshot.get("other_positions", []))
    cash_management = sum(p["value"] for p in snapshot.get("extra_assets", []))
    remainder = round(equity-stock-funds-other-cash_management, 2)
    parts = [("stocks", "股票", stock), ("funds", "基金／ETF", funds),
             ("cash_management", "天天宝／现金理财", cash_management),
             ("other", "其他证券", other), ("balance", "未拆分净资产（非可用现金）", remainder)]
    items = [{"kind": k, "label": label, "value": round(v, 2),
              "weight": round(v/equity*100, 2) if equity > 0 else None} for k, label, v in parts]
    notes = ["分类金额按总资产拆分；可用资金是券商给出的购买能力，可能已含天天宝额度，不能与分类金额再次相加。",
             "现金及其他净资产为总资产减已列资产，可能包含冻结、交收等项目，不等于可取资金。",
             "基金统计仅含本次券商接口返回的持仓；天天基金等其他账户的场外基金不会自动包含。"]
    if cash_management:
        notes.append("天天宝／现金理财按用户核对的市值差额列示；未取得产品明细、收益率或赎回到账时间。")
    checks = []
    invested = stock+funds+other
    if equity > 0 and invested/equity*100 > snapshot["account"]["max_total"]:
        checks.append("股票、基金及其他证券合计超过你设定的投资仓位上限；核对是否需要降低风险暴露。")
    if snapshot["positions"] and equity > 0:
        top = max(snapshot["positions"], key=lambda p:p["value"])
        if top["value"]/equity*100 > snapshot["account"]["max_single"]:
            checks.append(f"{top['name']}超过你设定的单股仓位上限；新增同板块基金也可能加重集中。")
    if funds:
        checks.append("持有多只基金不一定分散风险；先核对跟踪指数和最新成分，再判断是否与股票重复押注同一板块。")
    if cash_management:
        checks.append("理财产品与风险尚未核实；计划追加投资前先核对产品类型、券商可用额度、赎回规则和到账时间。")
    if remainder < -.1:
        checks.append("分类资产超过总资产，资产口径需重新核对；暂停使用补仓预算。")
    return {"items": items, "notes": notes, "checks": checks}


SECTORS = (
    ("半导体", ("半导体", "芯片")), ("医药医疗", ("医药", "医疗", "创新药", "生物")),
    ("证券", ("证券", "券商")), ("银行", ("银行",)), ("军工", ("军工", "国防", "航空")),
    ("新能源", ("新能源", "电池", "锂电")), ("光伏", ("光伏",)),
    ("机器人", ("机器人",)), ("人工智能", ("人工智能", "AI", "算力")),
    ("软件", ("软件", "计算机")), ("消费", ("消费", "白酒", "食品", "家电")),
    ("有色金属", ("有色", "稀土")), ("黄金", ("黄金",)),
    ("红利", ("红利",)), ("宽基指数", ("沪深300", "中证500", "中证1000", "A500", "上证50", "创业板", "科创50")),
    ("海外市场", ("纳指", "纳斯达克", "标普", "日经", "海外")),
    ("港股", ("恒生", "恒科", "港股",)), ("债券", ("债券", "国债", "政金债", "信用债")),
    ("货币", ("货币", "现金",)))


def enrich_funds(market, snapshot):
    date = market.execute("SELECT MAX(trade_date) FROM sector_daily WHERE trade_date<=?",
                          (snapshot["today"],)).fetchone()[0]
    blocks = [dict(r) for r in market.execute(
        "SELECT trade_date,name,pct FROM sector_daily WHERE trade_date=?", (date,))] if date else []
    for p in snapshot.get("fund_positions", []):
        name = p["name"].upper()
        matches = [(label, terms) for label, terms in SECTORS if any(t in name for t in terms)]
        p["sectors"] = [label for label, _ in matches]
        p["sector_basis"] = "券商证券名称关键词推断，未核验基金合同、跟踪指数及成分权重"
        terms = {t for _, words in matches for t in words}
        p["sector_evidence"] = [b for b in blocks if any(t in (b["name"] or "") for t in terms)][:6]
        p["fund_risks"] = ["未取得基金净值、折溢价、费率及成分权重，不能据此计算精确行业敞口。"]
        if any(s in p["sectors"] for s in ("海外市场", "港股")):
            p["sector_evidence"] = []
            p["fund_risks"].append("跨市场基金需核对汇率、交易时差、额度及折溢价；A 股盘面不能替代其标的市场。")
        if not matches:
            p["fund_risks"].append("名称不足以确定板块，请以基金合同与官方跟踪指数为准。")


def fund_observations(snapshot):
    out = []
    for p in snapshot.get("fund_positions", []):
        alerts = list(p.get("fund_risks", []))
        if not p.get("fresh"):
            alerts.append("账户或报价为旧快照，需同步确认。")
        if snapshot.get("market_date") != snapshot["today"]:
            alerts.append("板块盘面不是今日数据。")
        if any(b["trade_date"] != snapshot["today"] for b in p.get("sector_evidence", [])):
            alerts.append("所引用基金板块行情为历史数据，不代表今日走势。")
        facts = ["板块线索：" + ("、".join(p.get("sectors", [])) or "待核实"), p.get("sector_basis", "")]
        for b in p.get("sector_evidence", []):
            facts.append(f"{b['trade_date']} 本地板块 {b['name']} 涨跌幅 {b['pct']:+.2f}%" if b['pct'] is not None else f"{b['trade_date']} {b['name']} 涨跌幅缺失")
        if not p.get("sector_evidence"):
            facts.append("暂无可对应的本地板块行情，不能判定板块当前强弱。")
        out.append({"code":p["code"], "name":p["name"], "asset_type":"fund", "alerts":alerts, "facts":facts,
            "hold":"继续观察的前提：基金投向与原计划一致，相关板块逻辑未失效，且账户集中度仍在承受范围。",
            "exit":f"退出观察：核对板块逻辑转弱、基金偏离预期及集中度；券商当前返回可卖 {p['sellable']:g} 份，交易或赎回规则以该产品为准。",
            "add":"追加观察：先核对跟踪指数、净值折溢价及与现有持仓的重叠；仅有板块名称或价格下跌不足以支持追加。",
            "budget":None})
    return out
