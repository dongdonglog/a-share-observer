"""账户历史、资金边界、导入核对和确定性资金方案。无交易能力。"""
from __future__ import annotations

import csv
import hashlib
import io
import json
import math
from datetime import datetime, timedelta

SCHEMA = """
CREATE TABLE IF NOT EXISTS wb_profile (id INTEGER PRIMARY KEY CHECK(id=1), payload TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS wb_history (
 source TEXT NOT NULL, at TEXT NOT NULL, payload TEXT NOT NULL, quantities TEXT NOT NULL,
 PRIMARY KEY(source,at));
CREATE TABLE IF NOT EXISTS wb_research (
 code TEXT PRIMARY KEY, payload TEXT NOT NULL, fetched_at TEXT NOT NULL, error TEXT);
CREATE TABLE IF NOT EXISTS wb_imports (
 scope TEXT PRIMARY KEY, at TEXT NOT NULL, payload TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS wb_reports (
 source TEXT PRIMARY KEY, at TEXT NOT NULL, signature TEXT NOT NULL, payload TEXT NOT NULL);
"""


def _connect():
    from . import portfolio
    return portfolio.connect()


def _number(value, minimum=0, maximum=1e12):
    if isinstance(value, bool):
        raise ValueError("金额或比例必须是数字")
    try:
        if isinstance(value,str) and "," in value:
            import re
            if not re.fullmatch(r"-?\d{1,3}(?:,\d{3})+(?:\.\d+)?",value):
                raise ValueError("金额千分位格式错误")
            value=value.replace(",", "")
        n = float(value)
    except (TypeError, ValueError):
        raise ValueError("金额或比例格式错误") from None
    if not math.isfinite(n) or not minimum <= n <= maximum:
        raise ValueError("金额或比例超出有效范围")
    return n


def validate_profile(data):
    clean = {"reserve": _number(data.get("reserve")),
             "horizon_months": _number(data.get("horizon_months"), 1, 600),
             "max_drawdown": _number(data.get("max_drawdown"), .1, 100),
             "long_term_pct": _number(data.get("long_term_pct"), 0, 100),
             "new_money": _number(data.get("new_money", 0)),
             "batches": _number(data.get("batches", 3), 1, 24)}
    if any(not clean[k].is_integer() for k in ("horizon_months", "batches")):
        raise ValueError("期限与分批次数必须是整数")
    return clean


def profile(data=None):
    c = _connect()
    try:
        if data is not None:
            clean = validate_profile(data)
            with c:
                c.execute("INSERT OR REPLACE INTO wb_profile VALUES(1,?)", (json.dumps(clean),))
            return clean
        r = c.execute("SELECT payload FROM wb_profile WHERE id=1").fetchone()
        return json.loads(r[0]) if r else None
    finally:
        c.close()


def record_snapshot(c, payload, at, source="eastmoney"):
    quantities = json.dumps(sorted((p["code"], float(p["quantity"])) for p in payload["positions"]))
    last = c.execute("SELECT at,quantities FROM wb_history WHERE source=? ORDER BY at DESC LIMIT 1", (source,)).fetchone()
    if last and last[1] == quantities and last[0][:10] == at[:10]:
        current = datetime.fromisoformat(at).timestamp()
        previous = datetime.fromisoformat(last[0]).timestamp()
        if current >= previous and int(current // 300) == int(previous // 300):
            # 保留五分钟桶的最新值；份额变化时则另留记录。
            c.execute("DELETE FROM wb_history WHERE source=? AND at=?", (source, last[0]))
    c.execute("INSERT OR REPLACE INTO wb_history VALUES(?,?,?,?)",
              (source, at, json.dumps(payload, ensure_ascii=False), quantities))


def history(source="eastmoney", days=90):
    from . import portfolio
    cutoff = (portfolio.now()-timedelta(days=max(1, min(3650, int(days))))).isoformat()
    c = _connect()
    try:
        rows = c.execute("SELECT at,payload FROM wb_history WHERE source=? AND at>=? ORDER BY at", (source,cutoff)).fetchall()
        points = []
        from .eastmoney import is_a_share
        from .portfolio_research import is_fund
        for at, raw in rows:
            p = json.loads(raw)
            ps = p.get("positions", [])
            stock = sum(x["value"] for x in ps if is_a_share(x["code"]))
            fund = sum(x["value"] for x in ps if is_fund(x["code"],x["name"]) and not is_a_share(x["code"]))
            points.append({"at":at,"equity":p["equity"], "stocks":stock if ps else None,
                           "funds":fund if ps else None, "other":p["equity"]-stock-fund if ps else None})
        flow = c.execute("SELECT payload FROM wb_imports WHERE scope=?", ("cashflows:"+source,)).fetchone()
        flows = json.loads(flow[0]) if flow else None
        complete = bool(points and flows and flows.get("complete") and
                        all(p["at"][11:19] == "23:59:59" for p in points) and
                        flows["start"] <= points[0]["at"][:10] and flows["end"] >= points[-1]["at"][:10])
        first = points[0] if points else None
        for p in points:
            net = sum(x["amount"] for x in flows["rows"] if first["at"][:10] < x["date"] <= p["at"][:10]) if complete else None
            p["net_flow"] = net
            p["profit"] = round(p["equity"]-first["equity"]-net,2) if complete else None
        return {"points":points, "cashflows_complete":complete,
                "note":"仅展示资产变化；外部资金流水未完整核对，不能视为投资收益。" if not complete else
                       "收益金额=资产变化−已确认完整的外部净流入；不作为时间加权收益率。"}
    finally:
        c.close()


def validate_import(data):
    """严格列映射后只保存财务白名单。预览和提交使用相同规范化结果。"""
    kind = data.get("kind")
    if kind not in ("products", "history", "cashflows"):
        raise ValueError("请选择产品明细、历史资产或外部资金流水")
    scope = data.get("scope", "cash_management")
    if scope not in ("cash_management", "unresolved"):
        raise ValueError("请选择产品对应的账户汇总范围")
    raw = str(data.get("csv", ""))
    if len(raw) > 1_000_000:
        raise ValueError("文件过大，请分段导入")
    reader = csv.DictReader(io.StringIO(raw.lstrip("\ufeff")))
    aliases = {"基金代码":"code","基金名称":"name","证券代码":"code","证券名称":"name",
               "产品类型":"type","持有份额":"quantity","份额":"quantity","市值":"value",
               "资产金额":"value","持有收益":"pnl","净值日期":"nav_date","净值":"nav",
               "赎回状态":"redemption","日期":"date","总资产":"equity","净流入":"amount"}
    rows = []
    for i, row in enumerate(reader,1):
        r = {aliases.get(k.strip(),k.strip()):v.strip() for k,v in row.items() if isinstance(k,str) and isinstance(v,str)}
        if kind == "products":
            import re
            if not re.fullmatch(r"[A-Za-z0-9]{6,20}",r.get("code","")) or not r.get("name"):
                raise ValueError(f"第 {i} 行产品代码或名称无效")
            t = r.get("type","unknown")
            type_map = {"货币基金":"money_market","债券基金":"bond","股票基金":"equity","混合基金":"mixed","未知":"unknown"}
            t = type_map.get(t,t)
            if t not in ("money_market","bond","equity","mixed","unknown"):
                raise ValueError(f"第 {i} 行产品类型无法识别")
            p = {"code":r["code"],"name":r["name"][:80],"type":t,"value":_number(r.get("value")),
                 "quantity":_number(r["quantity"]) if r.get("quantity") else None,
                 "pnl":_number(r["pnl"],-1e12) if r.get("pnl") else None,
                 "nav":_number(r["nav"]) if r.get("nav") else None,
                 "nav_date":r.get("nav_date") or None,"redemption":r.get("redemption", "未知")[:80]}
            if p["nav_date"]:
                datetime.strptime(p["nav_date"],"%Y-%m-%d")
        else:
            datetime.strptime(r.get("date",""),"%Y-%m-%d")
            key = "equity" if kind == "history" else "amount"
            p = {"date":r["date"],key:_number(r.get(key), -1e12 if kind == "cashflows" else 0)}
        rows.append(p)
        if len(rows)>5000:
            raise ValueError("最多导入 5000 行")
    if not rows and kind != "cashflows":
        raise ValueError("没有可导入的记录，请核对列名")
    identities = [r.get("code",r.get("date")) for r in rows]
    if len(set(identities)) != len(identities):
        raise ValueError("存在重复产品或日期；资金流水请先按日合并净流入")
    clean = {"kind":kind,"scope":scope,"rows":rows}
    if kind == "cashflows":
        for k in ("start","end"):
            datetime.strptime(str(data.get(k,"")),"%Y-%m-%d")
        if data["start"]>data["end"] or any(not data["start"]<=r["date"]<=data["end"] for r in rows):
            raise ValueError("流水日期不在核对区间内")
        clean.update(start=data["start"],end=data["end"],complete=data.get("complete") is True)
    clean["fingerprint"] = hashlib.sha256(json.dumps(clean,sort_keys=True).encode()).hexdigest()
    return clean


def import_data(data, snapshot):
    from . import portfolio
    clean = validate_import(data)
    today = portfolio.now().strftime("%Y-%m-%d")
    for row in clean["rows"]:
        if (row.get("date") or row.get("nav_date") or "") > today:
            raise ValueError("不能导入未来日期资料")
    if clean["kind"] == "products":
        holdings = snapshot["positions"] + snapshot.get("fund_positions",[]) + snapshot.get("other_positions",[])
        imported_codes={p["code"] for g in imported_products(snapshot) if g["valid"] and g["scope"]!=clean["scope"] for p in g["products"]}
        if any(p["code"] in imported_codes for p in clean["rows"]):
            raise ValueError("产品已出现在另一汇总的导入明细中，不能重复计数")
        if any(p["code"] in {x["code"] for x in holdings} for p in clean["rows"]):
            raise ValueError("产品已在证券持仓中，不能重复导入")
        expected = sum(e["value"] for e in snapshot.get("extra_assets",[])) if clean["scope"] == "cash_management" else (snapshot.get("equity") or 0)-(snapshot.get("market_value") or 0)
        as_of = str(data.get("as_of") or "")
        datetime.strptime(as_of, "%Y-%m-%d")
        snap_at = snapshot.get("last_successful_sync") or snapshot["account"].get("updated_at") or ""
        if as_of != snap_at[:10]:
            raise ValueError("产品明细日期必须与账户快照日期一致")
        clean["as_of"] = as_of
        if not snapshot.get("complete") or abs(sum(p["value"] for p in clean["rows"])-expected) > .1:
            raise ValueError("导入明细金额与所选账户汇总不一致，请核对日期与覆盖范围")
    clean.pop("fingerprint", None)
    clean["fingerprint"] = hashlib.sha256(json.dumps(clean,sort_keys=True).encode()).hexdigest()
    if data.get("commit") is not True:
        return {**clean,"preview":True}
    if data.get("fingerprint") != clean["fingerprint"]:
        raise ValueError("导入内容已变化，请重新预览")
    c = _connect()
    try:
        source = snapshot.get("data_source","manual")
        with c:
            if clean["kind"] == "history":
                for p in clean["rows"]:
                    if p["date"] >= today:
                        raise ValueError("仅可导入已经结束日期的日终资产")
                    at=p["date"]+"T23:59:59+08:00"
                    existing=c.execute("SELECT at,payload FROM wb_history WHERE source=? AND substr(at,1,10)=?", (source,p["date"])).fetchall()
                    if existing:
                        if len(existing)==1 and existing[0][0]==at and json.loads(existing[0][1])["equity"]==p["equity"]:continue
                        raise ValueError("该日期已有资产记录，请先核对冲突；不会覆盖真实历史")
                    c.execute("INSERT OR IGNORE INTO wb_history VALUES(?,?,?,?)",
                              (source,at,json.dumps({"equity":p["equity"],"positions":[]}),"[]"))
            else:
                key = clean["scope"] if clean["kind"] == "products" else "cashflows"
                c.execute("INSERT OR REPLACE INTO wb_imports VALUES(?,?,?)",
                          (key+":"+source,portfolio.now().isoformat(timespec="seconds"),json.dumps(clean,ensure_ascii=False)))
        return {"ok":True,"rows":len(clean["rows"])}
    finally:
        c.close()


def imported_products(snapshot):
    c = _connect()
    result = []
    try:
        for scope in ("cash_management","unresolved"):
            r=c.execute("SELECT at,payload FROM wb_imports WHERE scope=?",(scope+":"+snapshot["data_source"],)).fetchone()
            if not r:continue
            p=json.loads(r[1])
            expected=sum(x["value"] for x in snapshot.get("extra_assets",[])) if scope=="cash_management" else (snapshot.get("equity") or 0)-(snapshot.get("market_value") or 0)
            snap_at=snapshot.get("last_successful_sync") or snapshot["account"].get("updated_at") or ""
            valid=abs(sum(x["value"] for x in p["rows"])-expected)<=.1 and p.get("as_of")==snap_at[:10]
            result.append({"scope":scope,"at":r[0],"valid":valid,"products":p["rows"],"source":"用户导入并核对的官方明细","as_of":p.get("as_of")})
        return result
    finally:c.close()


def scenarios(snapshot, settings=None):
    from . import portfolio
    settings = settings or profile()
    if not settings or not snapshot.get("complete"):
        return {"ready":False,"reason":"先设置资金边界，并取得完整账户快照。","items":[]}
    # 空账户必须在这里挡住。没有持仓时 complete 会"真空成立"（所有 0 个持仓的价值
    # 都不为 None），但 equity 是 None；放过去就会在下面 equity - invested_now
    # 直接 TypeError。手动记账为空、或刚退出券商账户都会落到这个状态。
    if snapshot.get("equity") is None:
        return {"ready":False,
                "reason":"还没有可规划的资金：手动记账为空，或券商账户已退出、本机数据已清除。",
                "items":[]}
    equity = snapshot["equity"]
    reserve = settings["reserve"]
    new = settings["new_money"]
    available = snapshot["account"].get("cash") or 0
    # 未到账的新增资金仅参与未来情景，不参加立即可执行预算。
    fresh = snapshot.get("data_source") != "eastmoney" or (
        snapshot.get("broker", {}).get("connected") and
        snapshot.get("broker", {}).get("snapshot_current_session") and
        not snapshot.get("broker", {}).get("query_error") and
        snapshot.get("last_successful_sync") and
        0 <= (portfolio.now() - datetime.fromisoformat(snapshot["last_successful_sync"])).total_seconds() <= 180)
    invested_now = sum(p["value"] for p in snapshot["positions"] + snapshot.get("fund_positions", []) + snapshot.get("other_positions", []))
    # 未拆分资产的风险未知，不计入安全资产；只有已确认空余仓位可形成预算。
    unknown_risk = max(0, equity - invested_now - available)
    headroom = max(0, equity * snapshot["account"]["max_total"] / 100 - invested_now - unknown_risk)
    now_budget=round(max(0,min(available-reserve,headroom)),2) if fresh and settings["horizon_months"] >= 12 else 0
    total=round(equity+new,2)
    target_invested=min(max(0,total-reserve),total*snapshot["account"]["max_total"]/100)
    long_target=round(target_invested*settings["long_term_pct"]/100,2)
    short_target=round(target_invested-long_target,2)
    stock=sum(x["value"] for x in snapshot["positions"])
    fund=sum(x["value"] for x in snapshot.get("fund_positions",[]))
    fund += sum(p["value"] for g in imported_products(snapshot) if g["valid"] for p in g["products"] if p.get("type") != "unknown")
    current={"long_term":fund,"tactical":stock,"unassigned":round(total-stock-fund,2)}
    target={"long_term":long_target,"tactical":short_target,"unassigned":round(total-target_invested,2)}
    cap=min(equity*snapshot["account"]["max_single"]/100,(stock+fund)/3) if stock+fund else 0
    reductions=[]
    for p in sorted(snapshot["positions"],key=lambda x:-x["value"]):
        excess=max(0,p["value"]-cap)
        if excess>0:
            qty=min(p["sellable"],int(excess/p["price"])//100*100) if p.get("price") else 0
            reductions.append({"code":p["code"],"name":p["name"],"excess":round(excess,2),
                               "theoretical_sellable_quantity":qty,"estimated_value":round(qty*(p.get("price") or 0),2)})
    cash_freed=round(sum(p["estimated_value"] for p in reductions),2)
    lower={"long_term":fund,"tactical":round(stock-cash_freed,2),"unassigned":round(total-stock-fund+cash_freed,2)}
    deferred={"long_term":fund,"tactical":stock,"unassigned":round(total-stock-fund,2)}
    batches=int(settings["batches"])
    per=int(now_budget*100)//batches
    batch_amounts=[per/100]*(batches-1)+[round(now_budget-per/100*(batches-1),2)]
    constraints=["长期暂以基金、短线暂以股票归组，这是模拟分组，不推断你的实际持有期限。",
                 "金额为研究预算，未含未知费用；卖出与赎回预计到账款不计入当前可用资金。",
                 "情景不预测收益；可接受回撤是风险边界，不能保证实际回撤不超过该值。"]
    if settings["horizon_months"]<12:
        constraints.append("用钱期限不足一年：长期方案暂缓执行，优先核对流动性及到账规则。")
    if reserve>available:
        constraints.append("当前可用资金不足预留现金目标，暂不安排新的买入预算。")
    if not fresh:
        constraints.append("账户已断开或快照过期：当前可执行预算为零，目标仅供比较。")
    if unknown_risk > 0:
        constraints.append("未分类资产按未知风险占用仓位空间，核对前不释放新增预算。")
    return {"ready":True,"total":total,"immediate_budget":now_budget,"future_contribution":new,
            "constraints":constraints,"fresh":bool(fresh),
            "stress": [{"shock_pct":n,"loss":round((stock+fund)*n/100,2),
                        "account_loss_pct":round((stock+fund)*n/equity,2) if equity else None,
                        "exceeds_limit":bool(equity and (stock+fund)*n/equity>settings["max_drawdown"])} for n in (10,20,30)],
            "items":[
        {"id":"maintain","name":"维持当前","target":current,"delta":{k:0 for k in current},
         "condition":"当前配置仍符合资金用途且没有新增风险证据时继续观察。","invalid":"资金用途变化、风险超限或持有逻辑被证伪。"},
        {"id":"concentration","name":"降低集中度","target":lower,"delta":{k:round(lower[k]-current[k],2) for k in current},
         "reductions":reductions,"condition":"比较单项低于投资资产三分之一及用户单股限额的情景；成交和费用需确认。",
         "invalid":"交易不可执行、流动性或费用不明；不能将估计卖出款立即再投入。"},
        {"id":"staged","name":"分批调整","target":target,"delta":{k:round(target[k]-current[k],2) for k in current},
         "batch_amounts":batch_amounts,
         "funding":{"needed":round(sum(max(0,target[k]-current[k]) for k in ("long_term","tactical")),2),
                    "available_now":now_budget,"new_money_not_received":new,
                    "sale_proceeds_not_available":cash_freed},
         "sequence":["核对产品归属和资金期限", "留足预留现金，确认费用和交易限制", "如需卖出或赎回，先核对可卖份额与到账", "到账后重算共享预算，再按批次评估"],
         "target_ranges":{"long_term":[round(max(0,long_target-target_invested*.05),2),round(min(target_invested,long_target+target_invested*.05),2)],
                          "tactical":[round(max(0,short_target-target_invested*.05),2),round(min(target_invested,short_target+target_invested*.05),2)]},
         "condition":"先满足预留现金；按设定长期比例分配投资目标，逐批核对市场证据及产品规则。",
         "invalid":"资金未到账、用钱期限过短、触及风险边界或市场证据转弱；停止后续批次。"},
        {"id":"defer","name":"暂缓投入","target":deferred,"delta":{k:round(deferred[k]-current[k],2) for k in current},
         "funding":{"needed":0,"available_now":0,"new_money_not_received":new,
                    "sale_proceeds_not_available":cash_freed},
         "sequence":["停止新增买入", "优先同步账户和产品明细", "补齐费用、赎回和到账规则", "资金边界恢复后重新计算方案"],
         "condition":"账户快照过期、预留现金不足、用钱期限过短或资料缺口影响判断时，暂停新增投入。",
         "invalid":"账户重新同步、预留现金达标、产品规则明确且风险边界未被触发后，再恢复分批或再平衡评估。"}]}


def metrics(rows):
    closes=[p["close"] for p in rows if p.get("close") and p["close"]>0]
    if len(closes)<2:return {"samples":len(closes)}
    peak=closes[0]; dd=0
    for v in closes:
        peak=max(peak,v);dd=min(dd,v/peak-1)
    out={"samples":len(closes),"last":closes[-1],"max_drawdown_pct":round(dd*100,2)}
    for n in (20,60):
        if len(closes)>=n+1:
            out[f"return_{n}d_pct"]=round((closes[-1]/closes[-n-1]-1)*100,2)
            out[f"ma{n}"]=round(sum(closes[-n:])/n,4)
    if len(closes)>=21:
        returns=[math.log(closes[i]/closes[i-1]) for i in range(len(closes)-20,len(closes))]
        mean=sum(returns)/len(returns)
        out["volatility_20d_pct"]=round(math.sqrt(sum((x-mean)**2 for x in returns)/19)*math.sqrt(252)*100,2)
    return out


def build(snapshot, days=90, settings=None):
    """图表、AI 和文本共享此财务事实；GET 不联网、不调用模型。"""
    from . import portfolio_research, research_data
    allocation = portfolio_research.allocation(snapshot)
    imports = imported_products(snapshot)
    products = [dict(p, category='stocks') for p in snapshot['positions']]
    products += [dict(p, category='funds') for p in snapshot.get('fund_positions', [])]
    products += [dict(p, category='other') for p in snapshot.get('other_positions', [])]
    for group in imports:
        if not group['valid']: continue
        category = 'cash_management' if group['scope']=='cash_management' else 'balance'
        # 导入只替换相应汇总的明细，不再次把价值加入总资产。
        imported_funds=sum(p['value'] for p in group['products'] if p.get('type') != 'unknown')
        for part in allocation['items']:
            if part['kind']==category:part['value']=round(part['value']-imported_funds,2)
            if part['kind']=='funds':part['value']=round(part['value']+imported_funds,2)
            part['weight']=round(part['value']/snapshot['equity']*100,2) if snapshot['equity'] else None
        products += [dict(p, category='funds' if p.get('type')!='unknown' else category, source=group['source'], import_at=group['at']) for p in group['products']]
    equity = snapshot.get('equity') or 0
    research = research_data.cached([p['code'] for p in products]+['benchmark:000300'])
    benchmark = research.get('benchmark:000300',{}).get('prices',{})
    exposure = {}; industries={}; overlaps=[]; known_fund=0; known_industries=0; total_fund=0
    stock_codes={p['code'] for p in snapshot['positions']}
    for p in products:
        p['weight']=round(p['value']/equity*100,2) if equity else None
        r=research.get(p['code'],{})
        series=r.get('prices') or r.get('nav') or {}
        p['research']=r
        p['metrics']=metrics(series.get('rows',[]))
        p['market_date']=series.get('date')
        if r.get('profile'):p['full_name']=r['profile'].get('name')
        # 相同起止日的相对宽基表现；不将宽基声称为基金跟踪指数。
        bm={x['date']:x['close'] for x in benchmark.get('rows',[])}
        common=[x for x in series.get('rows',[]) if x['date'] in bm]
        if len(common)>=21:
            a,b=common[-21],common[-1]
            p['metrics']['relative_20d_pct']=round(((b['close']/a['close']-1)-(bm[b['date']]/bm[a['date']]-1))*100,2)
            p['metrics']['relative_dates']=[a['date'],b['date']]
        volumes=[x['volume'] for x in series.get('rows',[]) if x.get('volume') is not None]
        if len(volumes)>=21 and sum(volumes[-21:-1])>0:
            p['metrics']['volume_ratio_20d']=round(volumes[-1]/(sum(volumes[-21:-1])/20),2)
        c=r.get('constituents',{})
        if p['category']=='funds' or p.get('type') in ('money_market','bond','equity','mixed','unknown'):
            total_fund+=p['value']
            known_fund+=p['value']*c.get('known_pct',0)/100
            industry=r.get('industries',{})
            known_industries+=p['value']*industry.get('known_pct',0)/100
            for row in industry.get('rows',[]):
                e=industries.setdefault(row['name'],{'name':row['name'],'value':0,'dates':[]})
                e['value']+=p['value']*row['weight']/100;e['dates'].append(industry['date'])
            for x in c.get('rows',[]):
                amount=p['value']*x['weight']/100
                e=exposure.setdefault(x['code'],{'code':x['code'],'name':x['name'],'value':0,'dates':[]})
                e['value']+=amount;e['dates'].append(c['date'])
                if x['code'] in stock_codes:overlaps.append({'fund':p['code'],'stock':x['code'],'value':round(amount,2),'date':c['date']})
    for e in list(exposure.values())+list(industries.values()):e['value']=round(e['value'],2)
    coverage=[{'name':'证券资金与持仓','at':snapshot.get('last_successful_sync') or snapshot['account'].get('updated_at'),
               'status':'已保存快照' if snapshot.get('complete') else '缺少完整快照'},
              {'name':'天天宝／现金理财产品及历史收益','at':None,
               'status':'官方查询入口需要有效会话；专项接口契约尚未核验。汇总不代表已识别具体基金。'},
              {'name':'证券账户内场外基金','at':None,
               'status':'当前证券持仓接口不能证明覆盖完整场外基金；可导入官方明细核对。'}]
    coverage += [{'name':'导入明细 · '+g['scope'],'at':g['at'],'status':'金额已核对（用户导入，需核对资料日期）' if g['valid'] else '汇总金额已变化，导入待重新核对'} for g in imports]
    profile_value=settings if settings is not None else profile()
    return {'source':snapshot['data_source'],'equity':snapshot.get('equity'),'allocation':allocation,
            'products':products,'coverage':coverage,'imports':imports,'profile':profile_value,
            'liquidity':{'available':snapshot['account'].get('cash'),'withdrawable':None,'frozen':None,'pending':None,
                         'note':'可用额度不额外计入资产；可取、冻结、待交收及赎回到账尚未验证，均不猜值。'},
            'planning':scenarios(snapshot,profile_value),'history':history(snapshot['data_source'],days),
            'research_job':research_data.status(),'benchmark':benchmark,
            'lookthrough':{'disclosed_value':round(known_fund,2),'unknown_value':round(total_fund-known_fund,2),
                           'industry_unknown_value':round(total_fund-known_industries,2),
                           'industries':sorted(industries.values(),key=lambda x:-x['value']),
                           'constituents':sorted(exposure.values(),key=lambda x:-x['value']), 'overlaps':overlaps,
                           'note':'按各基金已披露权重折算当前市值，披露期可能不同；行业与证券穿透分开计算，未披露部分保持未知。'},
            'limits':{k:snapshot['account'][k] for k in ('max_single','max_total')}}


def report_facts(wb):
    """白名单且精简输入，价格全序列不送模型；金额仍由程序渲染。"""
    rows=[]
    for p in wb['products']:
        r=p['research'];m=p['metrics'];evidence=[];gaps=[]
        if p.get('market_date'):evidence.append({'date':p['market_date'],'metrics':m,'basis':(r.get('prices') or r.get('nav') or {}).get('basis')})
        if r.get('profile'):evidence.append(r['profile'])
        if r.get('announcements'):evidence.append(r['announcements'])
        if r.get('industries'):evidence.append(r['industries'])
        if r.get('nav'):
            nav=r['nav'];evidence.append({'nav_date':nav.get('date'),'latest_unit_nav':nav.get('rows',[{}])[-1].get('close'), 'basis':nav.get('basis')})
        if r.get('constituents'):evidence.append(r['constituents'])
        if not r.get('constituents') and p['category']!='stocks':gaps.append('缺少可核验成分权重，不能给完整行业敞口')
        if not p.get('thesis'):gaps.append('用户尚未填写买入逻辑；不能据此认定持有逻辑失败')
        if m.get('samples',0)<21:gaps.append('价格样本不足，不能计算二十日趋势或成交活跃度')
        if r.get('error'):gaps.append(r['error'])
        rows.append({'code':p['code'],'name':p.get('full_name') or p['name'],'value':p['value'],
                     'weight':p['weight'],'sellable':p.get('sellable'),'pnl':p.get('pnl'),
                     'thesis':p.get('thesis'),'evidence':evidence,'gaps':gaps,
                     'conditions':{k:p.get(k) for k in ('stop_price','target_price','add_price')}})
    parts={p['kind']:p for p in wb['allocation']['items']}
    return {'schema_version':2,'account_metrics':{
                'stocks_weight_pct':parts.get('stocks',{}).get('weight'),
                'funds_weight_pct':parts.get('funds',{}).get('weight'),
                'largest_item':max(({'code':p['code'],'value':p['value'],'weight':p['weight']} for p in wb['products']), key=lambda p:p['value'] or 0,default=None)},
            'allocation':wb['allocation'],'liquidity':wb['liquidity'],'planning':wb['planning'],
            'coverage':wb['coverage'],'lookthrough':wb['lookthrough'],'products':rows}


def validate_report(raw, codes):
    if not isinstance(raw,dict) or raw.get('complete') is not True:raise ValueError('AI 报告未完成；保留上一份完整报告')
    fields=('judgment','evidence','trigger','invalid','gaps')
    for section in [raw.get('account')]+list(raw.get('holdings',[])):
        if not isinstance(section,dict) or any(not isinstance(section.get(k),str) or not section[k].strip() for k in fields):
            raise ValueError('AI 报告结构不完整；保留上一份完整报告')
    actual=[p.get('code') for p in raw.get('holdings',[])]
    if len(actual)!=len(codes) or set(actual)!=set(codes):raise ValueError('AI 未完整覆盖全部持仓；保留上一份完整报告')
    if not isinstance(raw.get('methods'),list) or len(raw['methods'])!=4 or any(not isinstance(x,str) or not x.strip() for x in raw['methods']):
        raise ValueError('AI 投入方法比较未完成；保留上一份完整报告')
    return {'complete':True,'account':{k:raw['account'][k][:4000] for k in fields},
            'holdings':[{**{k:p[k][:4000] for k in fields},'code':p['code']} for p in raw['holdings']],
            'methods':[x[:2000] for x in raw['methods']]}


def account_view(snapshot):
    result=build(snapshot)
    snapshot.update(allocation=result['allocation'],product_classification=[
        {k:p.get(k) for k in ('code','name','category','value','weight')} for p in result['products']],
        coverage=result['coverage'],liquidity=result['liquidity'])
    return snapshot
