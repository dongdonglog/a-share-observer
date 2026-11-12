"""按持仓代码获取公开研究资料；认证与账户数据不进入网络请求。"""
from __future__ import annotations
import html
import json
import math
import re
import ssl
import threading
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime

_LOCK = threading.Lock()
_STATE = {"state": "idle"}


def fetch(url):
    op = urllib.request.build_opener(urllib.request.ProxyHandler({}),
        urllib.request.HTTPSHandler(context=ssl.create_default_context()))
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0", "Referer": "https://fund.eastmoney.com/"})
    with op.open(req, timeout=12) as response:
        raw = response.read(4_000_001)
        if len(raw) > 4_000_000: raise ValueError("研究源返回过大")
        return raw.decode("utf-8-sig")


def plain(s):
    return html.unescape(re.sub(r"<[^>]+>", "", s)).strip()


def parse_profile(code, text):
    def field(label):
        m = re.search(re.escape(label)+r"</th>\s*<td[^>]*>(.*?)</td>",text,re.S)
        return plain(m[1]) if m else None
    if code not in (field("基金代码") or ""): raise ValueError("基金资料代码不匹配")
    name = field("基金全称")
    if not name: raise ValueError("基金全名缺失")
    return {"name":name,"type":field("基金类型"),"index":field("跟踪标的"),
            "source":f"https://fundf10.eastmoney.com/jbgk_{code}.html"}


def parse_nav(code, text):
    m = re.search(r'var\s+fS_code\s*=\s*"(\d+)"',text)
    if not m or m[1]!=code: raise ValueError("净值代码不匹配")
    m = re.search(r'var\s+Data_netWorthTrend\s*=\s*(\[.*?\]);',text,re.S)
    if not m: raise ValueError("未提供单位净值序列（货币类产品可能使用收益口径）")
    from .portfolio import TZ
    points = []
    for p in json.loads(m[1]):
        v=float(p["y"])
        if not math.isfinite(v) or v<=0: continue
        points.append({"date":datetime.fromtimestamp(p["x"]/1000,TZ).strftime("%Y-%m-%d"),"close":v})
    if not points: raise ValueError("净值序列为空")
    return {"rows":points[-250:],"basis":"单位净值（未含分红再投资，不等于总回报）","currency":"CNY",
            "source":f"https://fund.eastmoney.com/pingzhongdata/{code}.js","date":points[-1]["date"]}


def parse_constituents(text):
    # 仅解析首个披露期，绝不拼接多期前十大。
    match = re.search(r"截止至：.*?(\d{4}-\d{2}-\d{2}).*?<tbody>(.*?)</tbody>",text,re.S)
    if not match: raise ValueError("未取得带披露日期的成分表")
    rows=[]
    for tr in re.findall(r"<tr>(.*?)</tr>",match[2],re.S):
        cells=[plain(x) for x in re.findall(r"<td[^>]*>(.*?)</td>",tr,re.S)]
        if len(cells)<7 or not re.fullmatch(r"\d{5,6}",cells[1]) or not re.fullmatch(r"\d+(?:\.\d+)?%",cells[6]):
            raise ValueError("披露成分表字段变化")
        rows.append({"code":cells[1],"name":cells[2],"weight":float(cells[6][:-1])})
    total=sum(r["weight"] for r in rows)
    if not rows or total>100.1 or len({r['code'] for r in rows})!=len(rows): raise ValueError("披露权重异常")
    return {"date":match[1],"rows":rows,"known_pct":round(total,2),"unknown_pct":round(max(0,100-total),2),
            "note":"仅披露部分，行业分类尚未核验；剩余权重未知。"}


def parse_industries(code, data):
    from . import portfolio
    body=data.get("Data") or {}
    if str(body.get("FundCode"))!=code:raise ValueError("行业配置基金代码不匹配")
    quarters=body.get("QuarterInfos") or []
    if not quarters:raise ValueError("未取得行业披露")
    q=max(quarters,key=lambda p:p.get("JZRQ", ""));date=q.get("JZRQ", "")
    datetime.strptime(date,"%Y-%m-%d")
    if date>portfolio.now().strftime("%Y-%m-%d"):raise ValueError("行业披露日期异常")
    rows=[]
    for p in q.get("HYPZInfo") or []:
        weight=float(p["ZJZBL"])
        if p.get("BZDM")!=code or p.get("FSRQ")!=date or not math.isfinite(weight) or not 0<=weight<=100:
            raise ValueError("行业披露字段异常")
        rows.append({"name":str(p["HYMC"]),"weight":weight})
    total=sum(p["weight"] for p in rows)
    if not rows or total>100.1 or len({p['name'] for p in rows})!=len(rows):raise ValueError("行业披露权重异常")
    return {"date":date,"rows":rows,"known_pct":round(total,2),"unknown_pct":round(max(0,100-total),2),
            "source":f"https://api.fund.eastmoney.com/f10/HYPZ/?fundCode={code}&year="}


def quote_history(code, index=False):
    from .sources.tencent import symbol
    sym=symbol(code,1 if index else None)
    url=f"https://web.ifzq.gtimg.cn/appstock/app/fqkline/get?param={sym},day,,,250,qfq"
    data=json.loads(fetch(url)).get("data",{}).get(sym,{})
    basis="前复权价格" if data.get("qfqday") else "不复权价格"
    rows=[]
    for p in data.get("qfqday") or data.get("day") or []:
        v=float(p[2]);volume=float(p[5])
        if not math.isfinite(v) or v<=0 or not math.isfinite(volume): raise ValueError("价格序列异常")
        rows.append({"date":p[0],"close":v,"volume":volume})
    if not rows:raise ValueError("价格序列为空")
    return {"rows":rows,"basis":"指数点位" if index else basis,"date":rows[-1]["date"],"currency":"CNY","source":url}


def announcements(code):
    url=f"https://np-anotice-stock.eastmoney.com/api/security/ann?sr=-1&page_size=5&page_index=1&ann_type=A&stock_list={code}"
    data=json.loads(fetch(url))
    rows=(data.get("data") or {}).get("list")
    if not isinstance(rows,list):raise ValueError("公告返回不完整")
    return {"rows":[{"title":str(p.get("title") or "")[:300],"date":str(p.get("notice_date") or "")[:10]} for p in rows],"source":url}


def cached(codes):
    from . import portfolio
    c=portfolio.connect()
    try:
        result={}
        for code in codes:
            r=c.execute("SELECT payload,fetched_at,error FROM wb_research WHERE code=?",(code,)).fetchone()
            result[code]={**(json.loads(r[0]) if r else {}),"fetched_at":r[1] if r else None,
                          "error":r[2] if r else "尚未更新公开研究资料"}
        return result
    finally:c.close()


def status():
    with _LOCK:return dict(_STATE)


def refresh(snapshot):
    """有限并发；每个子来源独立保留上次成功数据和日期。调用者可同步等待。"""
    from . import portfolio, workbench
    with _LOCK:
        if _STATE["state"]=="running":return dict(_STATE)
        _STATE.update(state="running",message="正在更新持仓研究资料")
    try:
        holdings=snapshot['positions']+snapshot.get('fund_positions',[])
        imports=workbench.imported_products(snapshot)
        for group in imports:
            if group['valid']:holdings+=group['products']
        funds={p['code'] for p in snapshot.get('fund_positions',[])}|{p['code'] for g in imports if g['valid'] for p in g['products']}
        codes=list(dict.fromkeys([p['code'] for p in holdings]+['benchmark:000300']))
        previous=cached(codes)
        def one(key):
            old=previous[key]; now=portfolio.now(); at=now.isoformat(timespec='seconds')
            # 失败也限速，重试不清空成功缓存；休市仍显示实际资料日期。
            if old.get('fetched_at') and (now-datetime.fromisoformat(old['fetched_at'])).total_seconds()<300:
                return key,old,False
            code=key.split(':')[-1]
            if not re.fullmatch(r'\d{6}',code):return key,old,False
            data={k:v for k,v in old.items() if k not in ('fetched_at','error')}
            calls={'prices':lambda:quote_history(code,key.startswith('benchmark:'))}
            if code in funds:
                calls.update(industries=lambda:parse_industries(code,json.loads(fetch(f'https://api.fund.eastmoney.com/f10/HYPZ/?fundCode={code}&year='))),
                    profile=lambda:parse_profile(code,fetch(f'https://fundf10.eastmoney.com/jbgk_{code}.html')),
                    nav=lambda:parse_nav(code,fetch(f'https://fund.eastmoney.com/pingzhongdata/{code}.js')),
                    constituents=lambda:parse_constituents(fetch(f'https://fundf10.eastmoney.com/FundArchivesDatas.aspx?type=jjcc&code={code}&topline=10&year=&month=')))
                if code not in {p['code'] for p in snapshot.get('fund_positions',[])}:calls.pop('prices')
            if not key.startswith('benchmark:'):calls['announcements']=lambda:announcements(code)
            errors=dict(data.get('errors',{}))
            for name,fn in calls.items():
                part=data.get(name,{})
                if part.get('fetched_at','')[:10]==at[:10] and name!='prices':continue
                # 日线每天缓存；交易时段可五分钟更新，休市不重抓同日数据。
                if name=='prices' and part.get('fetched_at','')[:10]==at[:10] and not (now.weekday()<5 and '09:30'<=now.strftime('%H:%M')<='15:05'):continue
                try:
                    data[name]={**fn(),'fetched_at':at}
                    errors.pop(name,None)
                except Exception:
                    errors[name]='来源读取失败或字段不完整，保留上次成功数据'
            data['errors']=errors
            c=portfolio.connect()
            try:
                with c:c.execute('INSERT OR REPLACE INTO wb_research VALUES(?,?,?,?)',
                    (key,json.dumps(data,ensure_ascii=False),at,'；'.join(errors) or None))
            finally:c.close()
            return key,data,True
        with ThreadPoolExecutor(max_workers=3) as pool:results=list(pool.map(one,codes))
        with _LOCK:_STATE.update(state='complete',message='研究更新已结束；失败来源单列，旧资料保留',at=portfolio.now().isoformat(timespec='seconds'))
    except Exception:
        with _LOCK:_STATE.update(state='failed',message='研究更新未完成，已保留原资料')
    return status()


def start(snapshot):
    t=threading.Thread(target=refresh,args=(snapshot,),daemon=True,name='portfolio-research')
    t.start()
    return {'state':'running','message':'研究更新已提交'}


def refresh_connected():
    from . import db, portfolio, eastmoney
    state=eastmoney.session.status()
    if not state.get("connected") or not state.get("snapshot_current_session"):return
    c=db.init()
    try:
        snap=portfolio.snapshot(c)
        if snap["data_source"]=="eastmoney":start(snap)
    finally:c.close()
