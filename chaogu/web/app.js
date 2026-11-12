/* 短线看板前端：Grafana 式栅格 + 一句话生成看板。无构建、无框架。 */
(() => {
"use strict";

const $ = (s, el = document) => el.querySelector(s);
const esc = s => String(s ?? "").replace(/[&<>"']/g, c =>
  ({"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"}[c]));
const api = async (url, opt) => {
  const r = await fetch(url, opt);
  const j = await r.json().catch(() => ({error: r.statusText}));
  if (!r.ok || j.error) throw new Error(j.error || r.statusText);
  return j;
};
const post = (url, body) => api(url, {method: "POST",
  headers: {"content-type": "application/json"}, body: JSON.stringify(body)});

const S = {
  meta: null, dashes: [], cur: null, date: "", edit: false,
  charts: new Map(), timers: [], seenNews: new Set(), catalog: {},
  analyzing: false, watchTab: "rules", watchUnread: null,
  // 服务绑到 0.0.0.0 之后，内网其他人打开的是同一个页面，但账户、盯盘、
  // AI 面板和所有写接口对他们都是 403。此时进入只读模式：先把会报错的
  // 入口收起来，而不是让人点一下才弹错。依据是 /api/meta 的 local 字段。
  readOnly: false,
};
const WORKSPACE_DASHES = new Set(["information", "learning", "portfolio", "archive"]);   // 受保护：不能删除
const EXAMPLES = ["明天重点关注哪些题材和龙头", "盯机器人这条线：龙头梯队、相关快讯、明天怎么做",
  "情绪周期和赚钱效应，现在适合打板吗", "盘中实时：涨停、炸板、快讯", "分析我的持仓、可用资金和退出补仓条件",
  "今日交易量排行（成交额、换手、量比）", "百万大单买入榜，看主力净买入",
  "自选股的逐笔大单成交，只要主动买"];
const LIVE_NEWS_DASHES = new Set(["information", "live"]);
const LS = {
  get(k, d) { try { return JSON.parse(localStorage.getItem(k)) ?? d; } catch { return d; } },
  set(k, v) { try { localStorage.setItem(k, JSON.stringify(v)); } catch {} },
};

function toast(msg, ms = 2600) {
  const t = $("#toast"); t.textContent = msg; t.classList.add("on");
  clearTimeout(t._h); t._h = setTimeout(() => t.classList.remove("on"), ms);
}

// ------------------------------------------------------------------ 格式
const fmt = (v, f) => {
  if (v === null || v === undefined || v === "") return "—";
  if (f === "pct") return (v * 100).toFixed(0) + "%";
  if (f === "pct1") return (v > 0 ? "+" : "") + Number(v).toFixed(2) + "%";
  if (f === "int") return Math.round(v).toLocaleString();
  return typeof v === "number" && !Number.isInteger(v) ? v.toFixed(2) : String(v);
};
const signCls = v => v > 0 ? "up" : v < 0 ? "down" : "";
const stk = (s, extra = "") =>
  `<span class="stk" data-code="${esc(s.code)}">${esc(s.name)}</span>` +
  (s.height > 1 ? `<span class="h">${s.height}板</span>` : "") + extra;
const trendTag = t => `<span class="tag ${{放量: "up", 缩量: "down", 新冒头: "warn", 持平: ""}[t] ?? ""}">${esc(t)}</span>`;
const stageCls = s => ({冰点: "down", 退潮: "down", 修复: "blue", 发酵: "warn", 高潮: "up", 分歧: "warn"}[s] || "");

// ------------------------------------------------------------------ 渲染器
const R = {};

R.stats = (el, d) => {
  el.innerHTML = `<div class="stats">${d.items.map(i => {
    let cls = i.color === "sign" ? signCls(i.value) : i.color === "up" ? "up" :
      i.color === "down" ? "down" : "";
    if (i.warn) cls = "warn";
    let dl = "";
    if (i.delta !== undefined && i.delta !== null) {
      const good = i.invert ? i.delta < 0 : i.delta > 0;
      const c = i.delta === 0 ? "faint" : good ? "up" : "down";
      const dv = Math.abs(i.delta) < 1 && i.delta !== 0 ? i.delta.toFixed(2) : Math.round(i.delta);
      dl = `<div class="d ${c}">${i.delta > 0 ? "▲" : i.delta < 0 ? "▼" : "="} ${Math.abs(dv)} <span class="faint">较昨</span></div>`;
    }
    return `<div class="stat"><div class="l">${esc(i.label)}</div>
      <div class="v ${cls}">${fmt(i.value, i.fmt)}</div>${dl}</div>`;
  }).join("")}</div>`;
};

R.verdict = (el, d) => {
  const ph = {pre: "今日", live: "盘中", post: "明日"}[d.phase] || "明日";
  const bt = d.backtest;
  el.innerHTML = `<div class="verdict">
    <div class="vcol"><h4>情绪周期 · ${esc(d.date)}</h4>
      <div class="stage"><span class="s ${stageCls(d.stage)}">${esc(d.stage || "—")}</span>
        <span class="tmp">温度 ${fmt(d.temperature)}</span></div>
      <div class="stance">${esc(d.stance || "")}</div></div>
    <div class="vcol"><h4>${ph}重点主线（按回测热度排序）</h4>
      <div class="tops">${d.top.map((t, i) => `<div class="topc">
        <div class="rk">No.${i + 1}</div>
        <div class="nm">${esc(t.name)}</div>
        <div class="ld">龙头 ${stk({code: t.leader_code, name: t.leader, height: t.height})}</div>
        <div class="meta"><span class="tag up">${t.n} 家涨停</span>
          <span class="tag blue">连续 ${t.days} 天</span>${trendTag(t.trend)}</div>
        ${t.catalyst ? `<div class="ct" title="${esc(t.catalyst)}">催化：${esc(t.catalyst)}</div>` : ""}
      </div>`).join("") || '<div class="empty">今日无成线主线</div>'}</div></div>
    <div class="vcol"><h4>风险提示</h4>
      ${d.risks.length ? `<ul class="risk">${d.risks.map(r => `<li>${esc(r)}</li>`).join("")}</ul>`
        : '<div class="dim">暂无明显风险信号</div>'}
      ${bt ? `<div class="bt">排序口径回测：每天前 ${bt.k} 条主线次日仍有 ≥3 家涨停的比例，
        近 ${bt.test_days} 个交易日样本外 <b class="warn">${(bt.test * 100).toFixed(0)}%</b>
        （全部主线基准 ${(bt.test_base * 100).toFixed(0)}%）。只说明"线还在不在"，不代表买龙头赚钱。</div>` : ""}
    </div></div>`;
};

function bars(prev, n) {
  const vals = [...prev].reverse().concat([n]);
  const mx = Math.max(...vals, 1);
  const lab = ["T-3", "T-2", "昨", "今"].slice(4 - vals.length);
  return `<div class="bars">${vals.map((v, i) => `<i class="${i === vals.length - 1 ? "cur" : ""}"
    style="height:${Math.max(3, v / mx * 30)}px"><span>${v}</span></i>`).join("")}</div>
    <div class="bars-l">${lab.map(l => `<span>${l}</span>`).join("")}</div>`;
}

function catBox(c, codes) {
  if (!c) return "";
  const id = "c" + Math.random().toString(36).slice(2, 8);
  return `<div class="catbox"><div><span class="ttl">${esc(c.title)}</span>
    ${c.date ? `<span class="tag blue">${esc(c.date)}</span>` : ""}
    <span class="tag">${c.shared}/${codes} 只共享此原因</span>
    <span class="more" data-toggle="${id}">原文 ▾</span></div>
    <div class="full" id="${id}" hidden>${esc(c.text)}</div></div>`;
}

R.outlook = (el, d) => {
  if (!d.picks.length) { el.innerHTML = '<div class="empty">今日没有 ≥2 家涨停的主线</div>'; return; }
  el.innerHTML = `<div class="picks">${d.picks.map((p, i) => {
    const r = p.roles;
    return `<div class="pick">
      <div><div class="dim" style="font-size:11px">No.${i + 1} · 热度 ${p.heat}</div>
        <div class="nm">${esc(p.name)}</div>
        <div class="heat"><span class="tag up">${p.n} 家</span>
          <span class="tag blue">连续 ${p.days_active} 天</span>${trendTag(p.trend)}
          ${p.broken?.length ? `<span class="tag warn">炸板 ${p.broken.length}</span>` : ""}</div>
        ${bars(p.prev, p.n)}</div>
      <div class="roles">
        <div class="role"><span class="k">龙头</span><span>${stk(r.leader)}
          <span class="faint">${esc((r.leader.first_seal || "").slice(0, 5))}</span></span></div>
        ${r.core ? `<div class="role"><span class="k">中军</span><span>${stk(r.core)}
          <span class="faint">流通 ${r.core.float_mv ? (r.core.float_mv / 1e8).toFixed(0) + "亿" : "—"}</span></span></div>` : ""}
        ${r.front.length ? `<div class="role"><span class="k">前排首板</span><span>${r.front.map(s => stk(s)).join("、")}</span></div>` : ""}
        <div class="role"><span class="k">全部</span><span class="dim" style="font-size:12px">${p.stocks.map(s => stk(s)).join(" ")}</span></div>
        ${catBox(p.catalyst, p.n)}
      </div>
      <div><div class="dim" style="font-size:12px;margin-bottom:3px">盯什么</div>
        <ul class="watch">${p.watch.map(w => `<li>${esc(w)}</li>`).join("")}</ul>
        ${p.news?.length ? `<div class="dim" style="font-size:12px;margin:8px 0 3px">相关快讯</div>
          <ul class="watch">${p.news.slice(0, 3).map(n => `<li><span class="faint">${esc(n.ts.slice(11, 16))}</span> ${esc(n.title)}</li>`).join("")}</ul>` : ""}
      </div></div>`;
  }).join("")}</div>
  ${d.others?.length ? `<div class="bt">其他主线：${d.others.map(o => `${esc(o.name)}(${o.n}家·${o.trend})`).join("　")}</div>` : ""}
  <div class="bt">这是观察清单，不是买入建议。排序 = 今日涨停家数 + 2×连续活跃天数（回测得出，"今天突然放量"的线历史上多为一日游）。</div>`;
};

R.mainlines = (el, d) => {
  if (!d.lines.length) { el.innerHTML = '<div class="empty">暂无主线</div>'; return; }
  el.innerHTML = `<div class="lines">${d.lines.map(l => `<div class="line">
    <div class="hd"><span class="nm">${esc(l.name)}</span><span class="n">${l.n} 家</span>
      <span class="sp" style="flex:1"></span>
      <div class="ech">${Object.entries(l.echelon).sort((a, b) => b[0] - a[0]).map(([h, c]) =>
        `<span class="tag ${h > 1 ? "up" : ""}">${h}板×${c}</span>`).join("")}</div></div>
    ${l.alias.length ? `<div class="faint" style="font-size:11px">含：${l.alias.map(esc).join("、")}</div>` : ""}
    <div class="stks">${l.stocks.map((s, i) => `<span class="sc ${i === 0 ? "lead" : ""}" data-code="${esc(s.code)}"
      title="${esc(s.reason)}">${esc(s.name)}${s.height > 1 ? `<span class="h">${s.height}</span>` : ""}</span>`).join("")}</div>
    ${l.catalyst ? catBox(l.catalyst, l.n) :
      `<div class="nocat">${l.stocks.slice(0, 4).map(s => `<div><b>${esc(s.name)}</b>：${esc(s.company_point)}</div>`).join("")}</div>`}
  </div>`).join("")}</div>`;
};

R.events = (el, d) => {
  if (!d.events.length) { el.innerHTML = '<div class="empty">今日没有带动 ≥2 只涨停的共同催化</div>'; return; }
  el.innerHTML = d.events.map(e => {
    const id = "e" + Math.random().toString(36).slice(2, 8);
    return `<div class="ev"><div><span class="tag up">${e.n} 只</span>
      ${e.date ? `<span class="tag blue">${esc(e.date)}</span>` : ""}</div>
      <div class="ttl" style="margin:4px 0">${esc(e.title)}</div>
      <div class="stks">${e.stocks.map(s => `<span class="sc" data-code="${esc(s.code)}">${esc(s.name)}${s.height > 1 ? `<span class="h">${s.height}</span>` : ""}</span>`).join("")}</div>
      <span class="more" data-toggle="${id}">原文 ▾</span><div class="pre" id="${id}" hidden style="margin-top:6px">${esc(e.text)}</div></div>`;
  }).join("");
};

R.ladder = (el, d) => {
  el.innerHTML = d.tiers.map(t => `<div class="tier"><div class="hh"><b>${t.height}板</b>
      <div>${t.stocks.length} 只${t.prev ? ` · 昨${t.height - 1}板 ${t.prev}` : ""}</div></div>
    <div class="stks">${t.stocks.map(s => `<span class="sc" data-code="${esc(s.code)}" title="${esc(s.reason)}">${esc(s.name)}
      <span class="faint" style="font-size:11px">${esc((s.reason || "").split("+")[0])}</span></span>`).join("")}</div></div>`).join("")
    || '<div class="empty">暂无涨停</div>';
};

R.news = (el, d, p) => {
  const first = !p._newsInit; p._newsInit = true;
  el.innerHTML = `${d.scope ? `<div class="news-scope">${esc(d.scope)}</div>` : ""}<div class="news">${d.items.map(n => {
    const isNew = !first && !S.seenNews.has(n.ts + n.title);
    S.seenNews.add(n.ts + n.title);
    return `<div class="it ${isNew ? "new" : ""}"><div><span class="tm">${esc((n.ts || "").slice(5, 16).replace("T", " "))}</span>
      <span class="tt">${esc(n.title)}</span></div>
      ${n.content ? `<div class="ct">${esc(n.content)}</div>` : ""}
      ${(n.lines.length || n.tags.length) ? `<div class="tg">${n.lines.map(x => `<span class="tag up">主线·${esc(x)}</span>`).join("")}
        ${n.tags.map(x => `<span class="tag">${esc(x)}</span>`).join("")}</div>` : ""}</div>`;
  }).join("") || '<div class="empty">没有匹配的快讯</div>'}</div>`;
};

R.news_impact = (el, d, p) => {
  if (p._impactTimer) clearTimeout(p._impactTimer);
  const report = d.report;
  const detail = x => `<dl class="impact-reason">${[
    ['传导机制', x.mechanism], ['验证信号', x.confirm], ['失效条件', x.invalidate],
    ['影响期限', x.horizon], ['资料缺口', x.gaps]
  ].map(([k,v])=>`<dt>${k}</dt><dd>${esc(v)}</dd>`).join('')}</dl>`;
  const link = n => /^https?:\/\//i.test(n.url || '') ? `<a href="${esc(n.url)}" target="_blank" rel="noopener noreferrer">查看原文 ↗</a>` : '原始链接未提供';
  const tone = x => x==='潜在利好'||x==='已验证'?'up':x==='潜在利空'||x==='已失效'?'down':x==='新闻直接提及'?'blue':x?.includes('传闻')||x==='待验证'?'warn':'';
  const sourceRows = n => (n.sources?.length ? n.sources : [{id:n.primary_news_id||n.id,ts:n.ts,source:n.source,title:n.title,url:n.url,rumor:n.rumor}])
    .map(s=>`<li><span>${esc((s.ts||'').replace('T',' '))}</span> <b>${esc(s.source||'来源未标注')}</b> · ${/^https?:\/\//i.test(s.url||'')?`<a href="${esc(s.url)}" target="_blank" rel="noopener noreferrer">${esc(s.title||'原文')}</a>`:esc(s.title||'原文')}${s.rumor?' <span class="tag warn">需核实</span>':''}</li>`).join('');
  const verify = n => {
    const v = n.verification; if (!v) return '';
    const checks = (v.checks||[]).map(c => `<article><header><span class='tag ${c.signal?'up':c.weak?'down':'warn'}'>${esc(c.horizon)}</span><b>${esc(c.date||'待数据')}</b></header>${c.available?`<p>均值 ${fmt(c.avg_pct,'pct1')} · 最高 ${fmt(c.max_pct,'pct1')} · 题材命中 ${esc(c.theme_count||0)} / 候选 ${esc(c.stock_count||0)}</p>${c.leaders?.length?`<p>${c.leaders.map(x=>`${esc(x.name||x.code)} ${fmt(x.pct,'pct1')}${x.theme?' · 题材':''}`).join('；')}</p>`:''}${c.theme_hits?.length?`<p class='dim'>${c.theme_hits.map(x=>`${esc(x.name||x.code)}：${esc(x.reason||'题材再出现')}`).join('；')}</p>`:''}`:`<p>${esc(c.summary||'等待后续交易日数据。')}</p>`}</article>`).join('');
    return `<section class='impact-verify'><div><span class='tag ${tone(v.status)}'>${esc(v.status)}</span> <b>事件回看</b><p>${esc(v.summary)}</p></div><div class='impact-checks'>${checks}</div></section>`;
  };
  const cards = d.events.map(n => {
    const a = report?.events.find(x=>x.id===n.id);
    return `<article class="impact-card"><div class="impact-state">
        <span class="tag ${tone(n.status)}">${esc(n.status || '待验证')}</span>
        <span class="tag ${tone(n.evidence_level)}">${esc(n.evidence_level || (n.rumor ? '传闻/辟谣' : '普通快讯'))}</span>
        <span class="tag">来源 ${esc(n.source_count || 1)}</span>
        <span class="impact-time">首次 ${esc((n.first_ts || n.ts || '').replace('T',' '))} · 最新 ${esc((n.ts || '').replace('T',' '))}</span>
      </div>
      <h3>${esc(n.title)}</h3>
      <div class="impact-meta">${esc(n.source)} · ${link(n)} ${n.rumor ? '<span class="tag warn">含传闻／否认内容，需核实</span>' : ''}</div>
      <details><summary>事实来源 · 查看合并快讯</summary><ul class="impact-sources">${sourceRows(n)}</ul><p>${esc(n.content || n.title)}</p></details>
      ${a ? `<section class="impact-ai"><p><span class="tag ${tone(a.direction)}">${esc(a.direction)}</span> <b>${esc(a.judgment)}</b></p>${detail(a)}</section>` : '<p class="dim">尚未生成影响判断；下列关联仅作为研究线索。</p>'}
      ${verify(n)}
      <div class="impact-stocks">${n.candidates.map(c=>{
        const sa=a?.stocks.find(x=>x.code===c.code), q=c.market;
        return `<details><summary>${stk(c)} <span class="tag">${esc(c.relation)}</span>${sa ? ` · ${esc(sa.direction)}`:''}</summary>
          <p class="dim">关联依据（${esc(c.evidence_date)}）：${esc(c.evidence)}</p>
          <p class="dim">${q ? `${esc(q.trade_date)} 日线涨跌 ${fmt(q.pct,'pct1')}，换手 ${fmt(q.turnover)}%；日线不能证明新闻因果或发布后反应。` : '缺少所选日期的日线，尚不能验证价格反应。'}</p>
          ${sa ? `<p><b>${esc(sa.judgment)}</b></p>${detail(sa)}`:''}</details>`;
      }).join('') || '<p class="dim">本地资料未找到有依据的相关个股，不凭新闻补出股票名单。</p>'}</div></article>`;
  }).join('');
  el.innerHTML = `<div class="impact"><div class="impact-toolbar"><div><b>事件中心 · 新闻影响</b><p class="dim">${esc(d.scope)}</p></div>
    ${S.readOnly ? "" : `<button class="btn primary" data-impact-generate ${d.job.status==='running' || !d.events.length || !d.llm_available ? 'disabled':''}>${d.job.status==='running'?'正在分析…':report?'更新新闻影响解析':'生成新闻影响解析'}</button>`}</div>
    <p class="dim">${d.latest ? "最新资讯日" : "复盘日"} ${esc(d.date)} · 原始快讯 ${d.scanned} 条 · 合并事件 ${d.unique} 个 · 本轮展示 ${d.events.length} 个。分析仅发送上述公开新闻和候选证券资料。</p>
    ${S.readOnly?'<p class="dim">只读模式：报告由看板维护者在本机生成，这里只展示已有结果。</p>':''}
    ${!d.llm_available?'<p class="warn">模型尚未配置，当前展示原文与关联线索。</p>':''}
    ${d.stale?'<p class="warn">新闻或市场资料已变化，以下为上一份完整报告，请更新。</p>':''}
    ${d.job.message?`<p class="${d.job.status==='failed'?'warn':'dim'}">${esc(d.job.message)}</p>`:''}
    ${report?`<p class="dim">报告生成于 ${esc(report.at.replace('T',' '))} · 判断为研究推论，不代表消息必然推动股价。</p>`:''}
    ${cards || '<div class="empty">该日期范围没有已采集新闻，无法构建证据链。</div>'}</div>`;
  // 只读模式不渲染这个按钮，所以要判空——否则绑事件会炸掉整个面板。
  const genBtn = $('[data-impact-generate]', el);
  if (genBtn) genBtn.onclick = async e => {
    e.target.disabled = true;
    try { const r = await post('/api/news-impact/analyze', {date:d.date});
      if (r.status === 'cached') toast('资料未变化，已复用完整报告');
      await reload(); }
    catch (err) { toast(err.message, 6000); e.target.disabled = false; }
  };
  async function reload(){
    if (!el.isConnected || (!LIVE_NEWS_DASHES.has(S.cur?.id) && S.date !== d.date)) return;
    try {
      const r=await api('/api/panel?'+new URLSearchParams({id:'news_impact',date:d.date,
        params:JSON.stringify(d.latest?{latest:true}:{})}));
      if (el.isConnected && (LIVE_NEWS_DASHES.has(S.cur?.id) || S.date === d.date)) R.news_impact(el,r.data,p);
    } catch(err){ if(el.isConnected) toast(err.message); }
  }
  if(d.job.status==='running') p._impactTimer=setTimeout(reload,3000);
};

R.event_archive = (el, d) => {
  const tone = x => x === "已验证" ? "up" : x === "已失效" ? "down" : x === "待验证" ? "warn" : x === "新闻直接提及" ? "blue" : "";
  const rows = (d.items || []).map(x => {
    const stocks = (x.candidates || []).map(c => '<span class="tag">' + esc(c.name || c.code) + '</span>').join("");
    const ai = x.ai_judgment ? '<p><span class="tag ' + tone(x.ai_direction) + '">' + esc(x.ai_direction) + '</span> ' + esc(x.ai_judgment) + '</p>' : '<p class="dim">尚无旧 AI 判断。</p>';
    return '<article class="event-archive-card"><div class="impact-state"><span class="tag ' + tone(x.status) + '">' + esc(x.status || "待验证") + '</span><span class="tag ' + tone(x.evidence_level) + '">' + esc(x.evidence_level || "普通快讯") + '</span><span class="tag">来源 ' + esc(x.source_count || 1) + '</span><span class="impact-time">' + esc(x.date) + ' · ' + esc((x.ts || "").replace("T"," ")) + '</span></div><h3>' + esc(x.title) + '</h3><p class="dim">' + esc(x.verification_summary || "暂无验证摘要") + '</p>' + ai + '<div class="event-archive-stocks">' + (stocks || '<span class="dim">无本地候选证券</span>') + '</div></article>';
  }).join("");
  el.innerHTML = '<div class="impact"><div class="impact-toolbar"><div><b>事件验证档案</b><p class="dim">' + esc(d.scope) + '</p></div></div>' + (rows || '<div class="empty">暂无可复盘事件。</div>') + '</div>';
};

const WATCH_PROVIDERS = {eastmoney: "东方财富", ths: "同花顺", tencent: "腾讯行情", eastmoney_flow: "资金流", eastmoney_ticks: "分时成交"};
const WATCH_FIELDS = {price: "价格（元）", pct: "涨跌幅（%）", ma20: "MA20", volume20_ratio: "成交量倍数"};
const WATCH_OPS = {gte: "达到或超过", lte: "达到或低于", cross_above: "上穿", cross_below: "下穿", above: "站上", below: "跌破"};
const watchTone = state => state === "ok" ? "up" : state === "error" ? "down" : state === "stale" ? "warn" : "";
const watchStateText = state => ({ok: "可用", error: "异常", stale: "报价过期", idle: "等待首次采集"}[state] || state || "等待数据");
function watchPopularityText(r) {
  const e = r.effective || {};
  const parts = [];
  if (e.enable_new_entry) parts.push(`新晋前 ${e.entry_max_rank}（昨日不在前 ${e.current_max_rank}）`);
  if (e.enable_jump) parts.push(`榜内跃升：昨日在第 ${e.previous_in_top} 名内且提升 ≥${e.min_improvement} 名`);
  const sources = (r.config.providers || []).map(x => WATCH_PROVIDERS[x]).join(" / ");
  const dual = e.require_all_providers ? "，需所选榜单同时出现" : "";
  const legacy = e.legacy_invalid ?
    `（原昨日门槛 ${r.config.previous_min_rank} 超出榜单深度 100，已停用；请编辑规则重建）` : "";
  return `${sources} · ${parts.join("；") || "未启用任何条件（请编辑）"}${dual}${legacy}`;
}
const WATCH_FLOW_METRICS = { main_net: "主力净额", main_ratio: "主力净占比" };
const WATCH_TICK_SCOPES = { watchlist: "自选股", position: "账户持仓", both: "自选股与持仓" };
function watchFlowText(r) {
  const c = r.config || {};
  const dir = c.direction === "in" ? "仅净流入" : c.direction === "out" ? "仅净流出" : "净流入或净流出";
  const thr = c.metric === "main_ratio" ? `主力净占比 ≥ ${c.min_ratio}%` : `主力净额 ≥ ${fmtFlow(Number(c.min_amount))}`;
  return `东方财富资金流 · ${dir} · ${thr}`;
}
function watchTickText(r) {
  const c = r.config || {};
  const side = c.side === "both" ? "主动买或主动卖" : "主动买";
  return `${WATCH_TICK_SCOPES[c.scope] || "自选股与持仓"} · ${side}单次成交 ≥ ${fmtFlow(Number(c.min_amount))} · 3 秒快照口径`;
}
const watchRuleText = r => r.kind === "price" ?
  `${r.config.scope === "position" ? "账户持仓" : "自选股"} · ${(r.config.conditions || []).map(c => `${WATCH_FIELDS[c.field]} ${WATCH_OPS[c.op]}${c.field === "ma20" ? "" : ` ${c.value}${c.field === "pct" ? "%" : c.field === "volume20_ratio" ? "×" : ""}`}`).join(r.config.logic === "all" ? "，且 " : "，或 ")}` :
  r.kind === "flow" ? watchFlowText(r) :
  r.kind === "tick" ? watchTickText(r) : watchPopularityText(r);

function watchEventSummary(event) {
  const o = event.observed || {};
  if (event.kind === "price") {
    return `价格 ${o.price == null ? "—" : `¥${Number(o.price).toFixed(2)}`} · 涨跌 ${o.pct == null ? "—" : `${Number(o.pct).toFixed(2)}%`} · MA20 ${o.ma20 == null ? "—" : `¥${Number(o.ma20).toFixed(2)}`} · 量比 ${o.volume20_ratio == null ? "—" : `${Number(o.volume20_ratio).toFixed(2)}×`}`;
  }
  if (event.kind === "flow") {
    const amt = o.main_net == null ? "—" : fmtFlow(Number(o.main_net));
    return `${o.direction === "out" ? "主力净流出" : "主力净流入"} ${amt} · 净占比 ${o.main_ratio == null ? "—" : `${Number(o.main_ratio).toFixed(2)}%`} · 涨跌 ${o.pct == null ? "—" : `${Number(o.pct).toFixed(2)}%`}`;
  }
  if (event.kind === "tick") {
    return `${o.side === "sell" ? "主动卖" : "主动买"} ${esc(o.time || "—")} · ${o.price == null ? "—" : `¥${Number(o.price).toFixed(2)}`} × ${o.lots ?? "—"} 手 = ${o.amount == null ? "—" : fmtFlow(Number(o.amount))}`;
  }
  return Object.entries(o).map(([provider, v]) => {
    const name = WATCH_PROVIDERS[provider] || provider;
    if (v.change === "new_entry") {
      return `${name} 昨日不在前 ${v.snapshot_depth ?? 100} 名 → 今日第 ${v.current_rank ?? "?"} 名（新晋）`;
    }
    return `${name} ${v.previous_rank ?? "?"} → ${v.current_rank ?? "?"}（提升 ${v.improvement ?? "—"}）`;
  }).join("；") || "缺少排名快照";
}

function watchSources(data) {
  return ["tencent", "eastmoney", "ths", "eastmoney_flow", "eastmoney_ticks"].map(id => {
    const s = data.sources?.[id] || {state: "idle"};
    const checked = s.checked_at ? ` · 更新 ${String(s.checked_at).slice(11, 19)}` : "";
    const detail = s.error ? ` · ${s.error}` : s.coverage ? ` · 覆盖前 ${s.coverage} 名` : s.rows ? ` · ${s.rows} 条` : "";
    return `<span class="watch-source"><b>${WATCH_PROVIDERS[id]}</b><i class="tag ${watchTone(s.state)}">${watchStateText(s.state)}</i><small>${esc(checked + detail)}</small></span>`;
  }).join("");
}

function watchEventRows(items, compact = false) {
  return items.map(item => `<article class="watch-event ${item.read ? "is-read" : ""}">
    <div class="watch-event-head"><b>${esc(item.name || item.code)} <small>${esc(item.code)}</small></b>
      <span class="tag ${item.kind === "popularity" ? "blue" : "warn"}">${({popularity: "人气异动", flow: "资金异动", tick: "大单成交", price: "行情触发"})[item.kind] || "行情触发"}</span>
      <time>${esc((item.triggered_at || "").replace("T", " "))}</time></div>
    <div>${esc(item.rule_name)}</div><p>${esc(watchEventSummary(item))}</p>
    ${!compact && item.source_at ? `<small>数据时间 ${esc(item.source_at.replace("T", " "))} · 触发时规则版本 ${esc(item.rule_snapshot?.revision || "—")}</small>` : ""}
    ${!item.read ? `<button class="btn watch-read-one" data-id="${esc(item.id)}">标为已读</button>` : ""}
  </article>`).join("") || '<div class="empty">暂无触发记录</div>';
}

R.watch_center = (el, d) => {
  S.watchData = d;
  const tabs = [["rules", "监控规则"], ["popularity", "人气异动"], ["events", `触发记录${d.unread ? ` · ${d.unread}` : ""}`]];
  const content = S.watchTab === "popularity" ? renderPopularity(d) : S.watchTab === "events" ?
    `<div class="watch-toolbar"><span class="dim">记录保存于本机；规则后续修改不会改变旧触发快照。</span><button class="btn" data-watch-open-alerts>完整记录</button></div>${watchEventRows(d.events || [])}` : renderWatchRules(d);
  el.innerHTML = `<section class="watch-center">
    <div class="watch-heading"><div><b>盯盘中心</b><span class="dim">规则只触发提醒，不连接交易操作</span></div>
      <span class="tag ${d.status?.service_running ? "up" : "warn"}">${d.status?.service_running ? "本机服务运行中" : "本机服务未运行"}</span></div>
    <div class="watch-sources">${watchSources(d)}</div>
    <nav class="watch-tabs" aria-label="盯盘视图">${tabs.map(([id,label]) => `<button class="${S.watchTab === id ? "on" : ""}" data-watch-tab="${id}">${label}</button>`).join("")}</nav>
    <div class="watch-content">${content}</div>
  </section>`;
  el.querySelectorAll("[data-watch-tab]").forEach(b => b.onclick = () => { S.watchTab = b.dataset.watchTab; R.watch_center(el, d); });
  el.querySelector("[data-watch-open-alerts]")?.addEventListener("click", openAlerts);
  el.querySelector("[data-watch-add-stock]")?.addEventListener("click", addWatchStock);
  el.querySelectorAll("[data-watch-delete-stock]").forEach(b => b.onclick = async () => {
    try { await api(`/api/watch/stocks?code=${encodeURIComponent(b.dataset.watchDeleteStock)}`, {method: "DELETE"}); await reloadWatchPanel(); }
    catch (e) { toast(e.message, 5000); }
  });
  el.querySelector("[data-watch-add-price]")?.addEventListener("click", () => editWatchRule("price", null, d));
  el.querySelector("[data-watch-add-popularity]")?.addEventListener("click", () => editWatchRule("popularity", null, d));
  el.querySelector("[data-watch-add-flow]")?.addEventListener("click", () => editWatchRule("flow", null, d));
  el.querySelector("[data-watch-add-tick]")?.addEventListener("click", () => editWatchRule("tick", null, d));
  el.querySelectorAll("[data-watch-edit]").forEach(b => b.onclick = () => editWatchRule(b.dataset.kind, d.rules.find(r => r.id === b.dataset.watchEdit), d));
  el.querySelectorAll("[data-watch-toggle]").forEach(b => b.onchange = async () => {
    const rule = d.rules.find(r => r.id === b.dataset.watchToggle);
    try { await post("/api/watch/rules", {...rule.config, id: rule.id, name: rule.name, kind: rule.kind, enabled: b.checked}); await reloadWatchPanel(); }
    catch (e) { b.checked = !b.checked; toast(e.message, 5000); }
  });
  el.querySelectorAll("[data-watch-delete-rule]").forEach(b => b.onclick = async () => {
    if (!confirm("删除规则？已有触发记录会保留。")) return;
    try { await api(`/api/watch/rules?id=${encodeURIComponent(b.dataset.watchDeleteRule)}`, {method: "DELETE"}); await reloadWatchPanel(); }
    catch (e) { toast(e.message, 5000); }
  });
  el.querySelectorAll(".watch-read-one").forEach(b => b.onclick = async () => {
    try { await post("/api/watch/read", {ids: [b.dataset.id]}); await reloadWatchPanel(); pollWatch(); }
    catch (e) { toast(e.message); }
  });
};

function renderWatchRules(d) {
  const stocks = d.stocks || [];
  const rules = d.rules || [];
  return `<div class="watch-toolbar"><div class="watch-add-stock"><input id="watchStockCode" inputmode="numeric" maxlength="6" placeholder="六位代码" aria-label="自选股代码"><input id="watchStockName" maxlength="40" placeholder="名称（可选）" aria-label="自选股名称"><button class="btn" data-watch-add-stock>加入自选</button></div>
      <div><button class="btn" data-watch-add-price>＋ 行情规则</button> <button class="btn" data-watch-add-popularity>＋ 人气规则</button> <button class="btn" data-watch-add-flow>＋ 资金规则</button> <button class="btn" data-watch-add-tick>＋ 大单成交</button></div></div>
    <div class="watch-list-heading"><b>自选股</b><span class="dim">账户持仓单独选择，不会自动加入自选</span></div>
    <div class="watch-stock-list">${stocks.map(s => `<div><span>${esc(s.name || s.code)} <small>${esc(s.code)}</small></span><button class="icon-btn" data-watch-delete-stock="${esc(s.code)}" title="从自选移除" aria-label="从自选移除 ${esc(s.name || s.code)}">×</button></div>`).join("") || '<span class="dim">暂无自选股，可直接新建行情规则。</span>'}</div>
    <div class="watch-list-heading"><b>规则</b><span class="dim">行情每 30 秒检查，大单成交每 60 秒，人气榜与资金流每 10 分钟；均仅交易时段</span></div>
    <div class="watch-rule-list">${rules.map(r => `<article class="watch-rule-row"><label class="watch-switch"><input type="checkbox" data-watch-toggle="${esc(r.id)}" ${r.enabled ? "checked" : ""}><span></span></label>
      <div class="watch-rule-main"><b>${esc(r.name)}</b><p>${esc(watchRuleText(r))}</p>${r.kind === "popularity" && r.effective?.legacy_invalid ? `<p class="warn">原昨日门槛 ${esc(r.config.previous_min_rank)} 超过榜单深度 100，规则已停止触发；请编辑规则重建新的阈值。</p>` : ""}<small>${r.kind === "price" ? `证券 ${esc(r.config.stock_name || r.config.code)} · ${esc(r.config.code)} · 修改于 ${esc(r.updated_at.slice(0,16).replace("T"," "))}` : `修改于 ${esc(r.updated_at.slice(0,16).replace("T"," "))}`}</small></div>
      <div class="watch-rule-actions"><button class="btn" data-watch-edit="${esc(r.id)}" data-kind="${esc(r.kind)}">编辑</button><button class="icon-btn" data-watch-delete-rule="${esc(r.id)}" title="删除规则" aria-label="删除规则">×</button></div></article>`).join("") || '<div class="empty">尚未创建规则</div>'}</div>`;
}

function renderPopularity(d) {
  return `<div class="watch-popularity-grid">${["eastmoney", "ths"].map(provider => {
    const p = d.popularity?.[provider] || {items: []};
    const status = d.sources?.[provider] || {state: "idle"};
    const items = status.state === "ok" ? (p.items || []) : [];
    return `<section class="watch-rank-source"><header><b>${WATCH_PROVIDERS[provider]}人气榜</b><span class="dim">基准 ${esc(p.baseline_date || "缺失")}${p.baseline_depth ? ` · 覆盖前 ${esc(p.baseline_depth)} 名` : ""}</span></header>
      ${status.state !== "ok" ? `<p class="watch-source-note ${status.state === "error" ? "warn" : "dim"}">${esc(watchStateText(status.state))}${status.error ? ` · ${esc(status.error)}` : ""}；不展示旧排名作为实时数据。</p>` : ""}
      ${!p.baseline_date ? '<p class="watch-source-note warn">尚无上一交易日收盘快照。有效基准只在 15:05–16:05 建立；若看板此时段后启动，将从下一交易日收盘建立，建立前不触发跃升提醒。</p>' : ""}
      ${items.length ? `<div class="watch-rank-table"><table><thead><tr><th>当前</th><th>股票</th><th>昨日</th><th>提升</th><th>数据时间</th></tr></thead><tbody>${items.map(x => `<tr><td class="num">${x.rank}</td><td>${esc(x.name)} <small>${esc(x.code)}</small></td><td class="num">${x.new_entry ? '<span class="tag blue">新晋</span>' : (x.previous_rank ?? "—")}</td><td class="num ${x.improvement > 0 ? "up" : ""}">${x.improvement == null ? "—" : `+${x.improvement}`}</td><td>${esc((x.source_at || "").slice(11,19) || "—")}</td></tr>`).join("")}</tbody></table></div>` : '<div class="empty">当前没有可用排名快照</div>'}
    </section>`;
  }).join("")}</div><p class="bt">两家榜单约每 10 分钟自动采集，页面每 30 秒刷新展示。东方财富与同花顺公开榜单都只提供前 100 名，因此无法监控 100 名以外的名次：「新晋」= 昨日不在前 100、今日进入；「榜内跃升」= 昨日已在前 N 名内且提升达到门槛。昨日名次只来自本机保存的完整收盘快照；快照缺失、覆盖不足或来源异常时不按排名上升处理。</p>`;
}

const fmtFlow = v => {
  if (v == null || !isFinite(v)) return "—";
  const a = Math.abs(v);
  if (a >= 1e8) return `${(v / 1e8).toFixed(2)} 亿`;
  if (a >= 1e4) return `${(v / 1e4).toFixed(1)} 万`;
  return v.toFixed(0);
};
const flowRow = x => `<tr><td><span class="stk" data-code="${esc(x.code)}">${esc(x.name || x.code)}</span> <small>${esc(x.code)}</small></td>
  <td class="num ${signCls(x.pct)}">${x.pct == null ? "—" : `${Number(x.pct) > 0 ? "+" : ""}${Number(x.pct).toFixed(2)}%`}</td>
  <td class="num ${(x.main_net || 0) > 0 ? "up" : "down"}">${fmtFlow(x.main_net)}</td>
  <td class="num ${(x.main_net || 0) > 0 ? "up" : "down"}">${x.main_ratio == null ? "—" : `${Number(x.main_ratio).toFixed(2)}%`}</td>
  <td class="num">${fmtFlow(x.amount)}</td></tr>`;

R.flow_center = (el, d) => {
  const s = d.sources?.eastmoney_flow || {state: "idle"};
  const board = (rows, hint) => {
    const list = rows || [];
    return `<section class="watch-rank-source"><header><b>${hint}</b><span class="dim">${d.source_at ? `数据 ${esc(String(d.source_at).slice(11, 19))}` : ""}</span></header>
      ${list.length ? `<div class="watch-rank-table"><table><thead><tr><th>股票</th><th>涨跌</th><th>主力净额</th><th>净占比</th><th>成交额</th></tr></thead><tbody>${list.map(flowRow).join("")}</tbody></table></div>` : '<div class="empty">暂无资金流数据</div>'}
    </section>`;
  };
  el.innerHTML = `<section class="watch-center">
    <div class="watch-heading"><div><b>资金异动</b><span class="dim">东方财富公开资金流 · 主力 = 超大单 + 大单净额</span></div>
      <span class="tag ${watchTone(s.state)}">${watchStateText(s.state)}</span></div>
    ${s.state !== "ok" ? `<p class="watch-source-note ${s.state === "error" ? "warn" : "dim"}">${esc(watchStateText(s.state))}${s.error ? ` · ${esc(s.error)}` : ""}；不展示旧数据当实时。</p>` : ""}
    <div class="watch-popularity-grid">${board(d.inflow, "主力净流入")}${board(d.outflow, "主力净流出")}</div>
    <p class="bt">覆盖全市场主力资金净流入前 300 名与净流出前 300 名的并集（约 600 只），包含大量人气榜 100 名以外的个股；净占比 = 主力净额 / 当日成交额，金额单位元。各平台口径有差异，属描述性数据，不预测次日，也不构成买卖建议。资金异动阈值提醒在「盯盘提醒与人气异动」的规则里新建。</p>
  </section>`;
};

function addWatchStock() {
  const code = $("#watchStockCode")?.value.trim();
  const name = $("#watchStockName")?.value.trim();
  if (!code) { toast("请输入六位证券代码"); return; }
  post("/api/watch/stocks", {code, name}).then(reloadWatchPanel).catch(e => toast(e.message, 5000));
}

function editWatchRule(kind, rule, data) {
  const cfg = rule?.config || {};
  // effective 是后端归一化后的口径（旧配置里超出榜单深度的门槛会在这里变成关闭）。
  const eff = rule?.effective || {};
  const jumpDefault = rule ? !!eff.enable_jump : true;
  const targets = scope => {
    const rows = scope === "position" ? (data.positions || []) : (data.stocks || []);
    const current = cfg.code || "";
    const options = rows.map(x => `<option value="${esc(x.code)}" ${x.code === current ? "selected" : ""}>${esc(x.name || x.code)} · ${esc(x.code)}</option>`).join("");
    const missing = current && !rows.some(x => x.code === current) ? `<option value="${esc(current)}" selected>${esc(cfg.stock_name || current)} · ${esc(current)}${scope === "position" ? "（当前未持有）" : "（已不在自选）"}</option>` : "";
    return `<option value="">请选择${scope === "position" ? "当前持仓" : "自选股"}</option>${missing}${options}`;
  };
  const conditionOptions = field => (field === "ma20" ? ["above", "below", "cross_above", "cross_below"] : field === "volume20_ratio" ? ["gte", "lte"] : ["gte", "lte", "cross_above", "cross_below"]);
  let conditions = structuredClone(cfg.conditions || [{field: "price", op: "cross_above", value: 10}]);
  if (kind === "price") {
    const priceFields = Object.entries(WATCH_FIELDS).map(([id,label]) => `<option value="${id}">${label}</option>`).join("");
    const drawConditions = () => {
      $("#watchConditions").innerHTML = conditions.map((c, i) => `<div class="watch-condition" data-index="${i}">
        <select class="wc-field" aria-label="条件类型">${priceFields}</select><select class="wc-op" aria-label="比较方式"></select>
        <input class="wc-value" type="number" step="any" aria-label="条件阈值"><span class="wc-unit"></span>
        <button type="button" class="icon-btn wc-remove" title="移除条件" aria-label="移除条件">×</button></div>`).join("");
      $("#watchConditions").querySelectorAll(".watch-condition").forEach(row => {
        const i = Number(row.dataset.index), field = row.querySelector(".wc-field"), op = row.querySelector(".wc-op"), value = row.querySelector(".wc-value"), unit = row.querySelector(".wc-unit");
        field.value = conditions[i].field; value.value = conditions[i].value ?? "";
        const sync = () => {
          const f = field.value, ops = conditionOptions(f);
          op.innerHTML = ops.map(v => `<option value="${v}">${WATCH_OPS[v]}</option>`).join("");
          op.value = ops.includes(conditions[i].op) ? conditions[i].op : ops[0];
          value.hidden = f === "ma20"; value.required = f !== "ma20";
          unit.textContent = f === "pct" ? "%" : f === "volume20_ratio" ? "×" : f === "price" ? "元" : "";
          conditions[i] = {field: f, op: op.value, ...(f === "ma20" ? {} : {value: value.value})};
        };
        sync();
        field.onchange = sync; op.onchange = sync; value.oninput = () => { conditions[i].value = value.value; };
        row.querySelector(".wc-remove").onclick = () => { conditions.splice(i, 1); drawConditions(); };
      });
    };
    modal(`<h3>${rule ? "编辑行情规则" : "新建行情规则"}</h3><form id="watchRuleForm" class="form watch-form">
      <label>规则名称</label><input name="name" maxlength="40" value="${esc(rule?.name || "")}" placeholder="例如：放量站上 MA20">
      <label>监控范围</label><select name="scope" id="watchScope"><option value="watchlist">自选股</option><option value="position">账户持仓</option></select>
      <label>证券</label><select name="code" id="watchTarget" required></select>
      <label>条件组合</label><select name="logic"><option value="all">全部满足</option><option value="any">任一满足</option></select>
      <div id="watchConditions"></div><button type="button" class="btn" id="watchAddCondition">＋ 添加条件</button>
      <label class="check"><input type="checkbox" name="enabled" ${rule?.enabled === false ? "" : "checked"}>启用规则</label>
      <p class="bt">量比 = 当前交易日累计成交量 ÷ 前 20 个完整交易日平均成交量；成交量来自同一行情单位。报价超过 120 秒或时间异常时不触发。</p>
      <div class="row"><button type="button" class="btn" id="watchCancel">取消</button><button type="submit" class="btn primary">保存规则</button></div></form>`);
    const scope = $("#watchScope"), target = $("#watchTarget");
    scope.value = cfg.scope || "watchlist";
    $("#watchRuleForm [name=logic]").value = cfg.logic || "all";
    const fillTargets = () => { target.innerHTML = targets(scope.value); };
    fillTargets(); scope.onchange = fillTargets;
    drawConditions();
    $("#watchAddCondition").onclick = () => { if (conditions.length >= 6) return toast("最多添加 6 个条件"); conditions.push({field:"price",op:"gte",value:10}); drawConditions(); };
    $("#watchCancel").onclick = closeModal;
    $("#watchRuleForm").onsubmit = async e => {
      e.preventDefault();
      const form = e.currentTarget;
      const parsed = [...form.querySelectorAll(".watch-condition")].map(row => {
        const field = row.querySelector(".wc-field").value, op = row.querySelector(".wc-op").value;
        return {field, op, ...(field === "ma20" ? {} : {value: Number(row.querySelector(".wc-value").value)})};
      });
      const fields = form.elements;
      try { await post("/api/watch/rules", {id: rule?.id, kind, name: fields.namedItem("name").value,
        enabled: fields.namedItem("enabled").checked,
        code: target.value, stock_name: target.selectedOptions[0]?.textContent.split(" · ")[0] || cfg.stock_name || "",
        scope: scope.value, logic: fields.namedItem("logic").value, conditions: parsed}); closeModal(); await reloadWatchPanel(); }
      catch (err) { toast(err.message, 5000); }
    };
    return;
  }
  if (kind === "flow") {
    modal(`<h3>${rule ? "编辑资金规则" : "新建资金规则"}</h3><form id="watchRuleForm" class="form watch-form">
      <label>规则名称</label><input name="name" maxlength="40" value="${esc(rule?.name || "资金异动")}">
      <label>方向</label><select name="direction"><option value="both">净流入或净流出</option><option value="in">仅净流入</option><option value="out">仅净流出</option></select>
      <label>触发指标</label><select name="metric"><option value="main_net">主力净额</option><option value="main_ratio">主力净占比</option></select>
      <label>主力净额阈值（万元）</label><input name="min_amount_wan" type="number" min="0" step="10" value="${((cfg.min_amount ?? 1e8) / 1e4)}" required>
      <label>主力净占比阈值（%）</label><input name="min_ratio" type="number" min="0" max="100" step="0.1" value="${cfg.min_ratio ?? 3}" required>
      <label class="check"><input type="checkbox" name="enabled" ${rule?.enabled === false ? "" : "checked"}>启用规则</label>
      <p class="bt">指标选「主力净额」时按净额阈值触发，选「净占比」时按占比阈值触发；两个阈值都会保存，可随时切换。数据来自东方财富公开资金流，覆盖面为全市场。同股同日同方向合并为一条记录。<b>注意阈值量级</b>：100 万元在整个市场并不稀有（实测一天约 1800 只个股满足），阈值设在百万量级会一天产生上千条记录；想少一点噪声就用「净占比」或把阈值提到千万以上。</p>
      <div class="row"><button type="button" class="btn" id="watchCancel">取消</button><button type="submit" class="btn primary">保存规则</button></div></form>`);
    $("#watchRuleForm [name=direction]").value = cfg.direction || "both";
    $("#watchRuleForm [name=metric]").value = cfg.metric || "main_net";
    $("#watchCancel").onclick = closeModal;
    $("#watchRuleForm").onsubmit = async e => {
      e.preventDefault(); const fields = e.currentTarget.elements;
      try { await post("/api/watch/rules", {id: rule?.id, kind: "flow", name: fields.namedItem("name").value,
        enabled: fields.namedItem("enabled").checked,
        direction: fields.namedItem("direction").value,
        metric: fields.namedItem("metric").value,
        min_amount: Number(fields.namedItem("min_amount_wan").value) * 1e4,
        min_ratio: Number(fields.namedItem("min_ratio").value)}); closeModal(); await reloadWatchPanel(); }
      catch (err) { toast(err.message, 5000); }
    };
    return;
  }
  if (kind === "tick") {
    modal(`<h3>${rule ? "编辑大单成交规则" : "新增大单成交规则"}</h3><form id="watchRuleForm" class="form watch-form">
      <label>规则名称</label><input name="name" maxlength="40" value="${esc(rule?.name || "大单成交提醒")}">
      <label>监控范围</label><select name="scope"><option value="both">自选股与账户持仓</option><option value="watchlist">仅自选股</option><option value="position">仅账户持仓</option></select>
      <label>成交方向</label><select name="side"><option value="buy">仅主动买盘</option><option value="both">主动买与主动卖</option></select>
      <label>单次成交额阈值（万元）</label><input name="min_amount_wan" type="number" min="0" step="10" value="${((cfg.min_amount ?? 1e6) / 1e4)}" required>
      <label class="check"><input type="checkbox" name="enabled" ${rule?.enabled === false ? "" : "checked"}>启用规则</label>
      <p class="bt">每 60 秒检查一次<b>自选股 + 账户持仓</b>的分时成交（全市场逐笔轮询不现实，所以范围只有这个小池子）。口径是<b>交易所免费行情的 3 秒快照</b>：判定「这 3 秒里主动买成交了 N 万元」，而不是某一笔委托 N 万元——真正的逐笔需要 Level-2 付费行情。只统计连续竞价时段，集合竞价没有主动买卖之分，会被排除。池子超过 30 只时只监控前 30 只。</p>
      <div class="row"><button type="button" class="btn" id="watchCancel">取消</button><button type="submit" class="btn primary">保存规则</button></div></form>`);
    $("#watchRuleForm [name=scope]").value = cfg.scope || "both";
    $("#watchRuleForm [name=side]").value = cfg.side || "buy";
    $("#watchCancel").onclick = closeModal;
    $("#watchRuleForm").onsubmit = async e => {
      e.preventDefault(); const fields = e.currentTarget.elements;
      try { await post("/api/watch/rules", {id: rule?.id, kind: "tick", name: fields.namedItem("name").value,
        enabled: fields.namedItem("enabled").checked,
        scope: fields.namedItem("scope").value,
        side: fields.namedItem("side").value,
        min_amount: Number(fields.namedItem("min_amount_wan").value) * 1e4}); closeModal(); await reloadWatchPanel(); }
      catch (err) { toast(err.message, 5000); }
    };
    return;
  }
  modal(`<h3>${rule ? "编辑人气规则" : "新建人气规则"}</h3><form id="watchRuleForm" class="form watch-form">
    <label>规则名称</label><input name="name" maxlength="40" value="${esc(rule?.name || "人气榜异动")}">
    <label>榜单来源</label><div class="watch-checks"><label class="check"><input type="checkbox" name="provider" value="eastmoney">东方财富</label><label class="check"><input type="checkbox" name="provider" value="ths">同花顺</label></div>
    <label>昨日榜外判定深度（前 N）</label><input name="current_max_rank" type="number" min="1" max="100" step="1" value="${cfg.current_max_rank ?? 100}" required>
    <label class="check"><input type="checkbox" name="enable_new_entry" ${eff.enable_new_entry === false ? "" : "checked"}>新晋：昨日不在上面这个名次以内、今日进入</label>
    <label>新晋需进入前</label><input name="entry_max_rank" type="number" min="1" max="100" step="1" value="${eff.entry_max_rank ?? 30}" required>
    <label class="check"><input type="checkbox" name="enable_jump" ${jumpDefault ? "checked" : ""}>榜内跃升：昨日已在前 N 名内且提升达到门槛</label>
    <label>榜内跃升起点：昨日名次不超过</label><input name="previous_in_top" type="number" min="1" max="100" step="1" value="${eff.previous_in_top ?? 100}">
    <label>名次至少提升</label><input name="min_improvement" type="number" min="1" max="999" step="1" value="${eff.min_improvement ?? 30}" required>
    <label class="check"><input type="checkbox" name="require_all_providers" ${eff.require_all_providers === false ? "" : "checked"}>要求所选榜单同时出现（东财 + 同花顺）</label>
    <label class="check"><input type="checkbox" name="enabled" ${rule?.enabled === false ? "" : "checked"}>启用规则</label>
    <p class="bt">东方财富与同花顺公开榜单都只提供前 100 名，无法监控 100 名以外。为避免日内换手刷屏，「新晋」默认只在证券进入<b>前 30</b> 时提醒，并要求所选榜单同时出现；把「新晋需进入前」与「昨日榜外判定深度」设成同样的 100 会回到最吵的口径。快照缺失或不完整时不推断跃升。</p>
    <div class="row"><button type="button" class="btn" id="watchCancel">取消</button><button type="submit" class="btn primary">保存规则</button></div></form>`);
  const jumpBox = $("#watchRuleForm [name=enable_jump]"), jumpFrom = $("#watchRuleForm [name=previous_in_top]");
  const syncJump = () => { jumpFrom.disabled = !jumpBox.checked; };
  jumpBox.onchange = syncJump;
  syncJump();
  $("#watchRuleForm").querySelectorAll('[name="provider"]').forEach(box => box.checked = cfg.providers?.includes(box.value) ?? true);
  $("#watchCancel").onclick = closeModal;
  $("#watchRuleForm").onsubmit = async e => {
    e.preventDefault(); const form = e.currentTarget;
    const fields = form.elements;
    const providers = [...form.querySelectorAll('[name="provider"]:checked')].map(x => x.value);
    const jump = fields.namedItem("enable_jump").checked;
    if (!jump && !fields.namedItem("enable_new_entry").checked) {
      toast("至少要启用「新晋」或「榜内跃升」之一", 4000); return;
    }
    try { await post("/api/watch/rules", {id: rule?.id, kind, name: fields.namedItem("name").value,
      enabled: fields.namedItem("enabled").checked, providers,
      enable_new_entry: fields.namedItem("enable_new_entry").checked,
      entry_max_rank: Number(fields.namedItem("entry_max_rank").value),
      require_all_providers: fields.namedItem("require_all_providers").checked,
      previous_in_top: jump ? Number(fields.namedItem("previous_in_top").value) : null,
      current_max_rank: Number(fields.namedItem("current_max_rank").value),
      min_improvement: Number(fields.namedItem("min_improvement").value)}); closeModal(); await reloadWatchPanel(); }
    catch (err) { toast(err.message, 5000); }
  };
}

async function reloadWatchPanel() {
  const panel = S.cur?.panels.find(p => p.id === "watch_center");
  if (panel) await loadPanel(panel);
  await pollWatch();
}

R.table = (el, d, p) => {
  const cols = d.columns;
  const sort = p._sort || null;
  let rows = [...d.rows];
  if (sort) rows.sort((a, b) => {
    const x = a[sort.k], y = b[sort.k];
    if (x === y) return 0; if (x === null || x === undefined) return 1; if (y === null || y === undefined) return -1;
    return (x > y ? 1 : -1) * sort.dir;
  });
  el.innerHTML = (d.note ? `<div class="faint" style="font-size:11px;padding:4px 0">${esc(d.note)}</div>` : "") +
    (rows.length ? `<table><thead><tr>${cols.map(([k, l]) =>
      `<th data-k="${k}">${esc(l)}${sort?.k === k ? (sort.dir > 0 ? " ▲" : " ▼") : ""}</th>`).join("")}</tr></thead>
    <tbody>${rows.map(r => `<tr title="${esc(r._detail || "")}">${cols.map(([k]) => {
      const v = r[k];
      if (k === "name") return `<td>${r.code ? `<span class="stk" data-code="${esc(r.code)}">${esc(v)}</span>` : esc(v)}</td>`;
      if (typeof v === "number") {
        const c = (k === "pct") ? signCls(v) : (k === "height" && v > 1) ? "up" : "";
        return `<td class="num ${c}">${fmt(v)}</td>`;
      }
      return `<td class="${k === "reason" || k === "seat" ? "wrap" : ""}">${esc(v ?? "—")}</td>`;
    }).join("")}</tr>`).join("")}</tbody></table>` : '<div class="empty">无数据</div>');
  el.querySelectorAll("th").forEach(th => th.onclick = () => {
    const k = th.dataset.k;
    p._sort = {k, dir: p._sort?.k === k ? -p._sort.dir : -1};
    R.table(el, d, p);
  });
};

function md(s) {
  const lines = String(s || "").split("\n");
  let out = "", list = null;
  const inline = t => esc(t).replace(/\*\*(.+?)\*\*/g, "<strong>$1</strong>")
    .replace(/`(.+?)`/g, "<code>$1</code>");
  const close = () => { if (list) { out += `</${list}>`; list = null; } };
  for (const raw of lines) {
    const l = raw.replace(/\s+$/, "");
    let m;
    if ((m = l.match(/^(#{1,4})\s+(.*)/))) { close(); out += `<h${Math.min(3, m[1].length + 1)}>${inline(m[2])}</h${Math.min(3, m[1].length + 1)}>`; }
    else if ((m = l.match(/^\s*[-*]\s+(.*)/))) { if (list !== "ul") { close(); out += "<ul>"; list = "ul"; } out += `<li>${inline(m[1])}</li>`; }
    else if ((m = l.match(/^\s*\d+[.、]\s+(.*)/))) { if (list !== "ol") { close(); out += "<ol>"; list = "ol"; } out += `<li>${inline(m[1])}</li>`; }
    else if (!l.trim()) { close(); }
    else { close(); out += `<p>${inline(l)}</p>`; }
  }
  close();
  return out;
}
R.markdown = (el, d) => {
  el.innerHTML = `<div class="md">${md(d.md)}</div>` +
    // 凭证/地址这类失败，光看文字还得自己去翻菜单；直接给一个入口。
    (d.failure ? '<div class="row" style="justify-content:flex-start;margin-top:10px">' +
      '<button class="btn" data-ai-settings>打开 AI 设置</button></div>' : "") +
    (d.ai ? `<div class="bt">由 AI 基于看板数据生成${d.at ? "（" + esc(d.at) + "）" : ""}，只作观察清单，不构成投资建议。</div>` : "");
};

// ---- 当前账户：现金由券商余额核对，已成交买卖再自动记账。
const money = v => v == null ? "—" : Number(v).toLocaleString("zh-CN", {minimumFractionDigits: 2, maximumFractionDigits: 2});
const fundPrice = v => v == null ? "—" : Number(v).toLocaleString("zh-CN", {minimumFractionDigits: 3, maximumFractionDigits: 4});
function allocationView(a) {
  if (!a?.items?.length) return "";
  return `<section class="asset-allocation"><div class="sec">我的钱分布在哪里</div>
    <div class="allocation-bar" aria-label="按总资产划分的资产比例">${a.items.filter(x => x.weight > 0).map(x =>
      `<span class="allocation-${esc(x.kind)}" style="width:${Math.min(100,x.weight)}%" title="${esc(x.label)} ${x.weight}%"></span>`).join("")}</div>
    <div class="allocation-grid">${a.items.map(x => `<div><span class="allocation-dot allocation-${esc(x.kind)}"></span>${esc(x.label)}
      <b>${money(x.value)} 元</b><small>${x.weight == null ? "—" : x.weight.toFixed(2) + "%"}</small></div>`).join("")}</div>
    ${a.checks.map(x => `<p class="bt warn">${esc(x)}</p>`).join("")}
    <details><summary class="bt">金额口径与覆盖范围</summary>${a.notes.map(x => `<p class="bt">${esc(x)}</p>`).join("")}</details></section>`;
}
R.portfolio = (el, d) => {
  const a = d.account, brokerMode = d.data_source === "eastmoney";
  el.innerHTML = `${brokerToolbar(d)}<div class="account-tools"><span class="dim">当前账户 · 不随历史日期切换</span><button class="btn primary" data-workbench-jump>研究工作台／图表</button>
    ${brokerMode ? '<button class="btn" data-account-action="limits">仓位上限设置</button>' : '<button class="btn" data-account-action="account">核对资金／设置</button>'}
    ${brokerMode ? "" : `
    <button class="btn" data-account-action="position">录入已有持仓</button>
    <button class="btn primary" data-account-action="trade">记录买入／卖出</button>`}</div>
    <div class="stats account-stats">${[["可用资金", a.cash], ["持仓市值", d.market_value], ["总资产", d.equity], ["浮动盈亏", d.pnl]].map(([label,v]) =>
      `<div class="stat"><div class="k">${label}（元）</div><div class="v ${label === "浮动盈亏" ? signCls(v) : ""}">${money(v)}</div></div>`).join("")}
      <div class="stat"><div class="k">${brokerMode ? "投资仓位（不含现金理财）" : "总仓位"}</div><div class="v">${d.exposure == null ? "—" : d.exposure.toFixed(1) + "%"}</div></div></div>
    ${allocationView(d.allocation)}
    ${brokerMode ? '<div class="bt">可用资金为券商购买额度，可能已包含天天宝可用额度；上方盈亏仅为已返回证券明细合计，不含未返回的理财收益。</div>' : ""}
    ${brokerMode ? `<div class="bt">东方财富账户快照 · 单股上限 ${a.max_single}% / 总仓位上限 ${a.max_total}%</div>` : `<div class="bt">${a.cash == null ? "先核对券商可用资金。录入已有持仓不会再次扣款。" : `资金最后核对／记账：${esc(a.updated_at || "—")} · 单股上限 ${a.max_single}% / 总仓位上限 ${a.max_total}%`}
      · ${a.monitoring ? "已开启公开行情监控（服务启用实时刷新时，交易时段约 30 秒）" : "公开行情监控未开启，使用已有本地快照"}</div>`}
    ${!d.complete ? '<div class="err">账户或行情数据不完整，总资产、总仓位和补仓资金空间暂不可计算。</div>' : ""}
    ${d.positions.length ? `<div class="account-table"><table><thead><tr><th>持仓</th><th>股数／可卖</th><th>成本／现价</th><th>浮盈亏</th><th>仓位</th><th>${brokerMode ? "同步时间与来源" : "行情时间与来源"}</th><th>操作</th></tr></thead><tbody>
      ${d.positions.map(p => `<tr><td>${stk(p)}<div class="faint">${esc(p.code)}</div></td>
        <td class="num">${p.quantity} / ${p.sellable}</td><td class="num">${money(p.cost)} / ${money(p.price)}</td>
        <td class="num ${signCls(p.pnl)}">${money(p.pnl)}<div>${fmt(p.pnl_pct, "pct1")}</div></td>
        <td class="num">${p.weight == null ? "—" : p.weight.toFixed(1) + "%"}</td>
        <td><span class="tag ${p.fresh ? "down" : "warn"}">${p.fresh ? (brokerMode ? "近期同步" : "近期报价") : "快照／待更新"}</span><div class="faint">${esc(p.quote_at || "—")}<br>${esc(p.source)}</div></td>
        <td><button class="btn" data-account-action="${brokerMode ? "observation" : "position"}" data-position-code="${esc(p.code)}">${brokerMode ? "观察条件" : "校正／条件"}</button>
          ${brokerMode ? "" : `<button class="btn" data-account-action="trade" data-position-code="${esc(p.code)}">记交易</button>`}</td></tr>`).join("")}</tbody></table></div>` :
      `<div class="empty">${brokerMode ? (d.complete ? '最近一次完整快照没有 A 股持仓。' : '尚未取得完整账户快照，请登录并同步。') : '还没有持仓。已有股票选「录入已有持仓」；新成交选「记录买入／卖出」。'}</div>`}
    ${brokerMode && d.fund_positions?.length ? `<div class="sec">基金／ETF 持仓</div><div class="account-table"><table><thead><tr><th>基金</th><th>份额／可卖</th><th>成本／现价</th><th>市值／占总资产</th><th>盈亏</th></tr></thead><tbody>
      ${d.fund_positions.map(p => `<tr><td>${esc(p.name)}<div class="faint">${esc(p.code)}</div></td><td>${p.quantity} / ${p.sellable}</td>
        <td>${fundPrice(p.cost)} / ${fundPrice(p.price)}</td><td>${money(p.value)}<div class="faint">${p.weight == null ? "—" : p.weight.toFixed(2) + "%"}</div></td>
        <td class="${signCls(p.pnl)}">${money(p.pnl)}</td></tr>`).join("")}</tbody></table></div><div class="bt">基金板块与条件分析见下方持仓观察。场外基金仅在券商接口返回时计入。</div>` : ""}
    ${brokerMode && d.extra_assets?.length ? `<div class="bt">天天宝／现金理财：${money(d.extra_assets.reduce((n,x) => n+x.value,0))} 元 · 用户确认的市值差额，已计入总资产；已核对的导入明细见工作台，自动产品查询尚未适配。</div>` : ""}
    ${brokerMode && d.other_positions?.length ? `<div class="bt"><b>其他证券 · 市值 ${money(d.other_market_value)} 元</b><br>已计入上方总资产、持仓市值和仓位，不计作可用现金；仅展示券商记录，不参与 A 股题材分析。数量单位以券商为准。</div>
      <div class="account-table"><table><thead><tr><th>证券</th><th>数量／可卖</th><th>成本／现价</th><th>市值</th><th>盈亏</th></tr></thead><tbody>
      ${d.other_positions.map(p => `<tr><td>${esc(p.name)}<div class="faint">${esc(p.code)}</div></td>
        <td class="num">${p.quantity} / ${p.sellable}</td><td class="num">${money(p.cost)} / ${money(p.price)}</td>
        <td class="num">${money(p.value)}</td><td class="num ${signCls(p.pnl)}">${money(p.pnl)}</td></tr>`).join("")}</tbody></table></div>` : ""}
    <div class="bt">${brokerMode ? `资金、股数和可卖数量来自券商完整快照，同代码多条有效持仓汇总展示。请与官方界面核对后开启自动同步。退出账户会连本机保存的快照一起清除，不会留到下次登录。` : "金额基于手动记录；分红、送转、费用或漏记交易后请核对。浮盈亏未扣未来卖出费用；本地历史价可能含复权。"}</div>
    ${!brokerMode && a.cash == null && !d.positions.length ? `<div class="bt">手动记账还是空的。如果你刚退出东方财富账户，本机保存的账户快照（可用资金、总资产、持仓、盈亏）与由此生成的资产历史和 AI 报告已一并清除，重新登录即可再次同步；手动记账数据不受影响。</div>` : ""}
    ${d.transactions.length ? `<details class="account-ledger"><summary>最近账户流水（${d.transactions.length} 条）</summary>
      <table><thead><tr><th>记录时间</th><th>类型／股票</th><th>数量 × 价格</th><th>费用</th><th>现金余额</th><th>已实现盈亏</th></tr></thead><tbody>${d.transactions.map(t =>
        `<tr><td>${esc(t.ts)}</td><td>${esc(({buy:"买入",sell:"卖出",cash_adjust:"核对资金",position_adjust:"校正持仓"})[t.kind])} ${esc(t.code || "")}</td>
        <td>${t.quantity == null ? "—" : `${t.quantity} × ${money(t.price)}`}</td><td>${money(t.fees)}</td><td>${money(t.cash_after)}</td><td class="${signCls(t.realized)}">${money(t.realized)}</td></tr>`).join("")}</tbody></table></details>` : ""}`;
  el.querySelectorAll("[data-account-action]").forEach(b => b.onclick = () => accountForm(b.dataset.accountAction, b.dataset.positionCode));
  $("[data-workbench-jump]",el).onclick = () => document.querySelector('[data-workbench]')?.scrollIntoView({behavior:'smooth',block:'start'});
  bindBrokerToolbar(el, d);
};

function brokerToolbar(d) {
  const b = d.broker || {}, isBroker = d.data_source === "eastmoney";
  const label = b.connected ? (b.auto_sync ? "已连接 · 每 60 秒同步" : "已连接 · 等待核对") :
    b.state === "unsupported" ? "接入受限 · 已断开" : "已断开";
  return `<div class="broker-box"><div class="account-tools"><b>账户来源</b>
    <button class="btn ${isBroker ? "" : "primary"}" data-source="manual">手动记账</button>
    <button class="btn ${isBroker ? "primary" : ""}" data-source="eastmoney">东方财富</button>
    <span class="tag ${b.connected ? "down" : "warn"}">${label}</span>
    ${b.connected ? `<button class="btn" data-broker="refresh">立即同步</button>` : `<button class="btn" data-broker="login">登录东方财富</button>`}
    <button class="btn" data-broker="logout">退出账户</button></div>
    ${isBroker ? `<div class="bt">最后成功同步：${esc(d.last_successful_sync || "尚未同步")} · 24 小时无用户操作自动退出 · 服务重启后需重新登录 · 退出账户即清除本机保存的账户数据</div>` : ""}
    ${isBroker && b.connected && !b.snapshot_current_session ? `<div class="err">${d.last_successful_sync ? '当前登录尚未取得完整快照，下方保留的是此前成功同步的数据。' : '登录已成功，但尚未取得第一份完整快照；下方空白不代表账户没有资金或持仓。'}</div>` : ""}
    ${b.query_error ? `<div class="err" role="alert">${esc(d.last_successful_sync ? b.query_error : b.query_error.replace('已保留上次快照', '尚未保存账户快照'))}</div>` : ""}
    ${b.query_diagnostics ? `<div class="bt">市值核对：券商汇总 ${money(b.query_diagnostics.asset_market_value)} 元 · 明细合计 ${money(b.query_diagnostics.detail_market_value)} 元 · 差额 ${money(b.query_diagnostics.difference)} 元 · 返回 ${b.query_diagnostics.row_count} 条持仓</div>` : ""}
    ${b.can_reconcile ? `<p class="bt">仅当你已与官方核对，确认上方全部差额属于天天宝／现金理财时，使用下方按钮。若是漏返基金或其他持仓，请勿确认。</p><button class="btn" data-broker="reconcile">已核对：差额全部是天天宝／现金理财</button>` : ""}
    ${isBroker && b.can_confirm && !b.auto_sync ? `<p class="bt">请先与东方财富官方界面逐项核对可用资金、总资产、持仓和可卖数量。</p><button class="btn primary" data-broker="confirm">已核对一致，启用 60 秒同步</button>` : ""}
    </div>`;
}

// 退出后必须回显"清掉了什么"。数据凭空消失比多一句提示难接受得多，
// 用户会以为是 bug 或者被误删，然后再也不敢用这个功能。
function logoutMessage(r) {
  const p = r.purged || {};
  const n = Object.values(p).reduce((a, v) => a + (typeof v === "number" ? v : 0), 0);
  return n ? `账户已退出，本机保存的 ${n} 条账户数据已清除`
           : "账户已退出（本机没有需要清除的账户数据）";
}

function bindBrokerToolbar(el, d) {
  el.querySelectorAll("[data-source]").forEach(b => b.onclick = async () => {
    try { await post("/api/portfolio/broker", {kind:"source", source:b.dataset.source}); reloadAccountPanels(); }
    catch (e) { toast(e.message); }
  });
  el.querySelectorAll("[data-broker]").forEach(b => b.onclick = async () => {
    const action = b.dataset.broker;
    if (action === "login") return brokerLogin();
    // 退出会删本机数据，属于不可逆动作，必须先讲清楚删什么、什么不删。
    if (action === "logout" && !confirm(
      "退出账户会删除本机保存的东方财富账户数据：\n\n" +
      "· 账户快照：可用资金、总资产、市值、盈亏与逐笔持仓\n" +
      "· 每次同步的资产历史与股数记录\n" +
      "· AI 账户报告与「持仓研究清单」\n" +
      "· 按持仓代码缓存的行情和该账户的观察条件\n\n" +
      "手动记账的账户、持仓与流水不受影响。\n\n确定退出？")) return;
    b.disabled = true;
    try {
      const result = await post("/api/eastmoney/" + action, {snapshot_version:d.broker.snapshot_version, diagnostic_version:d.broker.diagnostic_version});
      toast(action === "logout" ? logoutMessage(result)
        : (result.query_error || (action === "confirm" ? "自动同步已启用" : "同步请求已完成")), 6000);
    } catch (e) { toast(e.message, 5000); }
    finally { b.disabled = false; reloadAccountPanels(); }
  });
}

async function brokerLogin() {
  modal(`<h3>东方财富本机登录</h3><p>正在检查官方登录流程…</p><button class="btn" id="brokerClose">关闭</button>`);
  $("#brokerClose").onclick = closeModal;
  const card = $("#modalCard"), marker = card.firstElementChild;
  try {
    const init = await post("/api/eastmoney/init", {});
    if (card.firstElementChild !== marker) return;
    if (init.state !== "ready") {
      card.innerHTML = `<h3>东方财富接入受限</h3><div class="err">${esc(init.query_error || "无法初始化登录")}</div><p>当前尚未验证真实账户连通性。</p><a href="https://jywg.18.cn/Login" target="_blank" rel="noopener noreferrer">打开官方登录页</a><button class="btn" id="brokerClose">关闭</button>`;
      $("#brokerClose").onclick = closeModal; reloadAccountPanels(); return;
    }
    const rsa = new window.JSEncrypt(); rsa.setPublicKey(init.public_key);
    const encrypt = text => {
      if (!window.crypto?.getRandomValues) throw new Error("浏览器不支持安全随机数，请更新浏览器");
      const out = rsa.encrypt(text); if (!out) throw new Error("密码加密失败，请重新打开登录窗口"); return out;
    };
    let sms = false, autoAnalyze = true;
    const draw = (message = "") => {
      card.innerHTML = `<h3>东方财富本机登录${sms ? " · 手机验证" : ""}</h3>
        <p class="dim">凭证仅用于本机连接官方服务。提交后清空密码；券商会话可能早于 24 小时失效。</p>
        <form id="brokerForm" class="form" autocomplete="off">
        ${sms ? `<label for="brokerMobile">绑定手机末 4 位</label><input id="brokerMobile" inputmode="numeric" pattern="[0-9]{4}" maxlength="4" autocomplete="off">
        <label for="brokerSMS">短信验证码</label><input id="brokerSMS" inputmode="numeric" maxlength="8" autocomplete="off">` :
        `<label for="brokerAccount">12 位资金账号</label><input id="brokerAccount" inputmode="numeric" pattern="[0-9]{12}" maxlength="12" required autocomplete="off">
        <label for="brokerPassword">交易密码</label><input id="brokerPassword" type="password" maxlength="20" required autocomplete="new-password">`}
        <label for="brokerCaptcha">图形验证码</label><div class="row"><input id="brokerCaptcha" maxlength="4" autocomplete="off" ${sms ? "" : "required"}><img id="brokerCaptchaImage" alt="官方验证码"><button type="button" class="btn" id="brokerNewCaptcha">换一张</button></div>
        ${sms ? "" : `<label class="check"><input type="checkbox" id="brokerAutoAnalyze" ${autoAnalyze ? "checked" : ""}>登录后自动生成 AI 持仓分析</label><div class="bt">完整同步后，将资金、持仓、观察条件和盘面发送给当前模型服务，生成持有、退出及补仓的条件式研究清单。</div>`}
        <div class="err" id="brokerError" role="alert">${esc(message)}</div>
        <div class="row">${sms ? '<button type="button" class="btn" id="brokerSendSMS">发送短信验证码</button>' : ""}<button type="submit" class="btn primary">${sms ? "验证手机" : "登录并读取账户"}</button><button type="button" class="btn" id="brokerClose">关闭</button></div></form>
        <div class="bt">首次成功读取后，请核对资金和持仓，再启用自动同步。当前版本尚待真实账户验收。</div>`;
      const form = $("#brokerForm");
      const error = msg => { if (form.isConnected) $("#brokerError", form).textContent = msg; };
      const captcha = async () => {
        try { const r = await post("/api/eastmoney/captcha", {flow:init.flow}); if (form.isConnected) $("#brokerCaptchaImage", form).src = r.image; }
        catch (e) { error(e.message); }
      };
      $("#brokerClose").onclick = closeModal;
      $("#brokerNewCaptcha").onclick = captcha;
      if (sms) $("#brokerSendSMS").onclick = async e => {
        e.target.disabled = true;
        try {
          const r = await post("/api/eastmoney/sms-send", {flow:init.flow, encrypted_mobile:encrypt($("#brokerMobile",form).value), code:$("#brokerCaptcha",form).value});
          error(r.query_error || "短信已请求发送，请查看绑定手机。");
        } catch (ex) { error(ex.message); }
        finally { if (form.isConnected) setTimeout(() => { const b = $("#brokerSendSMS",form); if (b) b.disabled = false; }, 60000); }
      };
      form.onsubmit = async e => {
        e.preventDefault(); const button = $("[type=submit]", form); button.disabled = true;
        let data, terminal = false;
        try {
          if (sms) data = {flow:init.flow, encrypted_mobile:encrypt($("#brokerMobile",form).value), code:$("#brokerSMS",form).value};
          else {
            const password = $("#brokerPassword",form);
            autoAnalyze = $("#brokerAutoAnalyze",form).checked;
            try { data = {flow:init.flow, account:$("#brokerAccount",form).value, encrypted_password:encrypt(password.value), captcha:$("#brokerCaptcha",form).value, auto_analyze:autoAnalyze}; }
            finally { password.value = ""; }
          }
          const pending = post("/api/eastmoney/" + (sms ? "sms-verify" : "login"), data);
          Object.keys(data).forEach(k => delete data[k]); data = null;
          const result = await pending;
          if (!form.isConnected) { reloadAccountPanels(); return; }
          if (result.state === "connected") {
            await post("/api/portfolio/broker", {kind:"source", source:"eastmoney"});
            closeModal(); reloadAccountPanels(); toast(result.query_error || (result.analysis_queued ? "账户已读取，正在自动分析持仓；请核对后启用同步" : "账户已读取，请核对后启用自动同步"), 6000); return;
          }
          if (result.state === "sms_required") { sms = true; draw(result.query_error || "官方要求验证绑定手机，请由本人完成。"); return; }
          if (sms && result.state === "ready" && !result.query_error) { sms = false; draw("手机验证完成，请重新输入账号和密码登录。"); return; }
          if (["unsupported", "disconnected"].includes(result.state)) {
            terminal = true;
            error(result.query_error || "登录流程已失效，请关闭后重新打开");
            form.querySelectorAll("input,button:not(#brokerClose)").forEach(x => x.disabled = true); return;
          }
          error(result.query_error || "登录未通过，请核对后重试"); await captcha();
        } catch (ex) { error(ex.message); }
        finally { if (data) Object.keys(data).forEach(k => delete data[k]); if (form.isConnected && !terminal) button.disabled = false; reloadAccountPanels(); }
      };
      captcha();
    };
    draw();
  } catch (e) { if (card.firstElementChild === marker) card.innerHTML = `<h3>登录初始化失败</h3><div class="err">${esc(e.message)}</div>`; }
}

// 只记录真实、可见页面内的交互。定时刷新、鼠标悬停和服务端同步不续期。
let lastBrokerActivity = 0;
for (const eventName of ["pointerdown", "click", "keydown", "wheel", "touchstart"]) {
  document.addEventListener(eventName, event => {
    if (!event.isTrusted || document.visibilityState !== "visible" || Date.now()-lastBrokerActivity < 30000) return;
    lastBrokerActivity = Date.now();
    post("/api/eastmoney/activity", {}).then(r => { if (!r.connected && r.query_error) reloadAccountPanels(); }).catch(() => {});
  }, {passive:true});
}

R.portfolio_analysis = (el, d) => {
  const busy = S.analyzing || d.job?.state === "running";
  clearTimeout(S.analysisPoll);
  if (d.job?.state === "running") S.analysisPoll = setTimeout(reloadAccountPanels, 2000);
  el.innerHTML = `<div class="account-tools"><span class="dim">本地盘面 ${esc(d.market_date || "暂无")} + 当前账户</span>
    <button class="btn primary" data-analyze ${busy || !d.can_generate || !d.llm ? "disabled" : ""}>${busy ? "正在分析…" : "生成完整 AI 持仓分析"}</button></div>
    <p class="bt">登录自动分析或点击生成会把持仓、资金规划及研究资料发送给当前模型。普通刷新不调用模型。分析失败保留上一份完整报告。</p>
    ${d.job?.message ? `<p role="status">${esc(d.job.message)}</p>` : ""}<div data-workbench></div>`;
  if (d.workbench) window.AccountWorkbench.render($("[data-workbench]",el),d,{api,post,toast,reload:reloadAccountPanels});
  $("[data-analyze]", el).onclick = async () => {
    S.analyzing = true;
    el.querySelectorAll("[data-analyze]").forEach(b => { b.disabled = true; b.textContent = "正在分析…"; });
    try { await post("/api/portfolio/analyze", {}); toast("完整持仓分析已更新"); }
    catch (e) { toast(e.message, 6000); }
    finally { S.analyzing = false; reloadAccountPanels(); }
  };
};

function reloadAccountPanels() {
  document.querySelectorAll('[data-workbench]').forEach(el => delete el.dataset.editing);
  S.cur?.panels.filter(p => p.id.startsWith("portfolio_")).forEach(p => loadPanel(p));
}

async function accountForm(kind, code = "", name = "") {
  try {
    const s = await api("/api/portfolio");
    const a = s.account, p = s.positions.find(p => p.code === code) || {};
    if (s.data_source === "eastmoney" && !["observation", "limits"].includes(kind)) {
      throw new Error("当前是东方财富快照，请切换到手动记账后再录入；观察条件可在持仓行编辑。");
    }
    const field = (id, label, value = "", type = "number", extra = "") => `<label for="a_${id}">${esc(label)}</label>
      <input id="a_${id}" name="${id}" type="${type}" value="${esc(value ?? "")}" ${extra}>`;
    let fields = "", title, hint;
    if (kind === "limits") {
      title = "东方财富账户仓位上限"; hint = "这些是你自行设置的研究条件。";
      fields = field("max_single", "单股仓位上限（%）", a.max_single, "number", 'required min="0.1" max="100" step="0.1"') +
        field("max_total", "总仓位上限（%）", a.max_total, "number", 'required min="0.1" max="100" step="0.1"');
    } else if (kind === "observation") {
      title = "持仓观察条件"; hint = `${p.name || code} · 资金和股数随券商同步，下面填写你的研究条件。`;
      fields = field("code", "股票代码", code, "text", "readonly") +
        field("stop_price", "退出观察价（可留空）", p.stop_price, "number", 'min="0.0001" step="any"') +
        field("target_price", "止盈观察价（可留空）", p.target_price, "number", 'min="0.0001" step="any"') +
        field("add_price", "补仓观察价（可留空）", p.add_price, "number", 'min="0.0001" step="any"') +
        `<label for="a_thesis">买入逻辑与失效条件</label><textarea id="a_thesis" maxlength="1000">${esc(p.thesis || "")}</textarea>`;
    } else if (kind === "account") {
      title = "核对账户资金与仓位设置";
      hint = "填券商当前可用资金（已扣除已有持仓买入金额），不是初始本金或可取资金。这里保存会校正余额。";
      fields = field("cash", "可用资金（元）", a.cash, "number", 'required min="0" step="0.01"') +
        field("max_single", "单股仓位上限（%，自行设置）", a.max_single, "number", 'required min="0.1" max="100" step="0.1"') +
        field("max_total", "总仓位上限（%，自行设置）", a.max_total, "number", 'required min="0.1" max="100" step="0.1"') +
        `<label class="check"><input type="checkbox" id="a_monitoring" ${a.monitoring ? "checked" : ""}>开启持仓公开行情监控</label>
        <div class="bt">开启后，服务实时刷新启用时，工作日交易时段每 30 秒查询腾讯公开行情；休市无新报价时保留快照。未开启不会新增持仓行情请求。</div>`;
    } else if (kind === "position") {
      title = p.code ? "校正持仓与观察条件" : "录入已有持仓";
      hint = "这是已有持仓快照，保存不扣现金。以后新成交请用「记录买入／卖出」。可卖数量以券商为准。";
      fields = field("code", "六位股票代码", code, "text", `required pattern="[0-9]{6}" ${p.code ? "readonly" : ""}`) +
        field("name", "股票名称（用于匹配相关快讯）", p.name || name, "text") +
        field("quantity", "当前持仓股数", p.quantity, "number", 'required min="1" step="1"') +
        field("sellable", "当前可卖股数", p.sellable, "number", 'required min="0" step="1"') +
        field("cost", "券商持仓均价（含已计入费用，元）", p.cost, "number", 'required min="0.0001" step="any"') +
        field("stop_price", "退出观察价（可留空，元）", p.stop_price, "number", 'min="0.0001" step="any"') +
        field("target_price", "止盈观察价（可留空，元）", p.target_price, "number", 'min="0.0001" step="any"') +
        field("add_price", "补仓观察价（可留空，元；触及后仍需盘面确认）", p.add_price, "number", 'min="0.0001" step="any"') +
        `<label for="a_thesis">买入逻辑与失效条件（可选）</label><textarea id="a_thesis" maxlength="1000">${esc(p.thesis || "")}</textarea>`;
    } else {
      title = "记录已成交买入／卖出";
      hint = `这里只记录今天（${s.today}）在券商已完成的交易。买入扣现金并计入费用，卖出回补现金；当日买入股份按 T+1 锁定。历史漏记请用核对资金与持仓快照校正。`;
      fields = `<label for="a_side">方向</label><select id="a_side"><option value="buy">买入／补仓</option><option value="sell">卖出／减仓</option></select>` +
        field("code", "六位股票代码", code, "text", 'required pattern="[0-9]{6}"') +
        field("name", "股票名称（首次买入填写）", p.name || name, "text") +
        field("quantity", `成交股数${p.code ? `（当前可卖 ${p.sellable} 股）` : ""}`, "", "number", 'required min="1" step="1"') +
        field("price", "成交均价（元）", "", "number", 'required min="0.0001" step="any"') +
        field("fees", "此次交易总费用（佣金、印花税等，元）", "0", "number", 'required min="0" step="0.01"');
    }
    modal(`<h3>${title}</h3><p class="dim">${esc(hint)}</p><form id="accountForm" class="form">${fields}
      <div class="err" id="accountError" role="alert"></div><div class="row"><button type="button" class="btn" id="a_cancel">取消</button><button type="submit" class="btn primary">保存记录</button></div></form>`);
    $("#a_cancel").onclick = closeModal;
    $("#accountForm").onsubmit = async e => {
      e.preventDefault();
      const form = e.currentTarget, b = $("[type=submit]", form); b.disabled = true;
      const data = {kind, revision: a.revision};
      form.querySelectorAll("input,textarea,select").forEach(f => {
        const key = f.id.slice(2);
        data[key] = f.type === "checkbox" ? f.checked : f.value;
      });
      try { await post(["limits", "observation"].includes(kind) ? "/api/portfolio/broker" : "/api/portfolio", data); closeModal(); reloadAccountPanels(); toast("账户记录已保存"); }
      catch (err) { $("#accountError", form).textContent = err.message; b.disabled = false; }
    };
  } catch (e) { toast(e.message, 5000); }
}

// ---- ECharts
const axisStyle = {axisLine: {lineStyle: {color: "#2f333b"}}, axisLabel: {color: "#8e9099"},
  splitLine: {lineStyle: {color: "#1f2227"}}};
const PAL = ["#f2495c", "#5794f2", "#56c26a", "#ff9830", "#b877d9", "#fade2a"];
function chart(el, opt, p) {
  el.classList.add("chart");
  let c = S.charts.get(p.uid);
  if (!c || c.getDom() !== el) {
    if (c) c.dispose();
    el.innerHTML = "";
    c = echarts.init(el, null, {renderer: "canvas"});
    S.charts.set(p.uid, c);
  }
  c.setOption({backgroundColor: "transparent", color: PAL, textStyle: {fontFamily: "inherit"},
    tooltip: {trigger: "axis", backgroundColor: "#1c1f24", borderColor: "#2f333b", textStyle: {color: "#d8d9da"}},
    legend: {top: 2, textStyle: {color: "#8e9099"}, itemHeight: 8},
    grid: {left: 44, right: 44, top: 32, bottom: 26, containLabel: false}, ...opt}, true);
}
R.line = (el, d, p) => {
  const two = d.series.some(s => s.axis === 1);
  chart(el, {
    xAxis: {type: "category", data: d.x, ...axisStyle, boundaryGap: false},
    yAxis: [{type: "value", ...axisStyle}, ...(two ? [{type: "value", ...axisStyle, splitLine: {show: false}}] : [])],
    series: d.series.map(s => ({name: s.name, type: "line", data: s.data, yAxisIndex: s.axis || 0,
      smooth: true, showSymbol: false, lineStyle: {width: 2}, connectNulls: true})),
  }, p);
};
R.bar = (el, d, p) => {
  const cat = {type: "category", data: d.x, ...axisStyle};
  const val = {type: "value", ...axisStyle};
  chart(el, {
    grid: {left: d.horizontal ? 96 : 40, right: 20, top: 30, bottom: d.horizontal ? 20 : 40},
    xAxis: d.horizontal ? val : {...cat, axisLabel: {color: "#8e9099", rotate: d.x.length > 6 ? 30 : 0}},
    yAxis: d.horizontal ? cat : val,
    series: d.series.map((s, i) => ({name: s.name, type: "bar", data: s.data.map(v => d.signed ?
      {value: v, itemStyle: {color: v >= 0 ? "#f2495c" : "#56c26a"}} : v),
      barMaxWidth: 18, itemStyle: {borderRadius: 2},
      label: {show: d.signed, position: d.horizontal ? "right" : "top", color: "#8e9099",
        formatter: x => (x.value > 0 ? "+" : "") + Number(x.value).toFixed(2)}})),
  }, p);
};

// ------------------------------------------------------------------ 栅格布局
// 与后端 panels.py 同一套模型：24 列栅格，x 是列起点 0..23，y 是行起点，约束 x + w <= 24。
// 后端 _sanitize / _solve_layout 是权威实现，这里 mirror 一份用于拖动时的即时反馈；
// 保存时坐标会再经后端归一，所以前端算错也不会写出非法布局。
const GRID = {cols: 24, minW: 4, maxW: 24, minH: 3, maxH: 20};

function clampPanel(p) {
  return window.ChaoguLayout.clamp(p);
}

// 拖动/缩放开始时的坐标快照。每次指针移动都先把所有面板恢复到快照、再交给
// ChaoguLayout 求解——否则被推开的面板在指针移开后会留在下面回不来。
function snapshotLayout() {
  return S.cur.panels.map(p => ({uid: p.uid, x: p.x, y: p.y, w: p.w, h: p.h}));
}

function restoreLayout(snap) {
  const byUid = new Map(S.cur.panels.map(p => [p.uid, p]));
  snap.forEach(s => {
    const p = byUid.get(s.uid);
    if (p) { p.x = s.x; p.y = s.y; p.w = s.w; p.h = s.h; }
  });
}

// 与后端 _compact 同一套重力：按 (y, x) 顺序把每块面板上移到不与他人重叠的最小 y。
// 不改变 S.cur.panels 的数组顺序——顺序决定新面板的落点和导入导出，不是布局本身。
// 传了 activeUid 时，这块面板优先占住用户放的位置（拖上去不会再被弹回原位）。
function compactPanels(activeUid) {
  window.ChaoguLayout.resolve(S.cur.panels, activeUid || null);
}

// 新面板必须带坐标。只缺一块坐标，后端 _sanitize 就会判定整表缺坐标而按数组顺序重排，
// 把用户手动摆好的位置全部冲掉。所以新面板统一补到最后一行下面。
function placeNewPanel(p) {
  const ps = S.cur.panels;
  p.x = 0;
  p.y = ps.length ? Math.max(...ps.map(x => (Number(x.y) || 0) + (Number(x.h) || 0))) : 0;
  return clampPanel(p);
}

// 只把坐标写回已有 DOM，不重建面板。
// 拖动/缩放松手时若走 renderDash()，会 dispose 所有 ECharts 并逐个重新请求 /api/panel，
// 拖一下刷一遍全屏数据。这里就地改 style 即可，脚本装完重建才需要 renderDash()。
// resizeUid：只给这一块重算图表尺寸。拖动时传 null —— 位置变了尺寸没变，30 块面板
// 每帧都 resize 图表纯属浪费，还会让拖动发涩。
function applyPositions(resizeUid) {
  S.cur.panels.forEach(p => {
    const el = document.querySelector(`.panel[data-uid="${p.uid}"]`);
    if (!el) return;
    el.style.gridColumn = `${p.x + 1} / span ${p.w}`;
    el.style.gridRow = `${p.y + 1} / span ${p.h}`;
    if (resizeUid === undefined || p.uid === resizeUid) S.charts.get(p.uid)?.resize();
  });
}

// ------------------------------------------------------------------ 面板
function panelHTML(p) {
  const c = S.catalog[p.id] || {};
  const w = Math.max(GRID.minW, Math.min(GRID.cols, Math.round(p.w) || GRID.minW));
  const h = Math.max(GRID.minH, Math.round(p.h) || GRID.minH);
  const x = Math.max(0, Math.min(GRID.cols - w, Math.round(p.x) || 0));
  const y = Math.max(0, Math.round(p.y) || 0);
  // CSS Grid 从 1 计数，而后端的 x/y 从 0 计数，这里 +1 转换。
  return `<div class="panel" draggable="false" data-uid="${p.uid}"
      data-id="${esc(p.id)}"
      style="grid-column:${x + 1} / span ${w};grid-row:${y + 1} / span ${h}">
    <div class="ph"><span class="t">${esc(p.title)}</span>
      <span class="info" data-info="${esc(p.uid)}" title="口径与局限" role="button" aria-label="口径说明">ⓘ</span>
      ${c.trust_short ? `<span class="trust ${esc(c.trust_class)}" title="${esc(c.trust_label)}">${esc(c.trust_short)}</span>` : ""}
      ${c.live ? '<span class="badge live">实时</span>' : ""}
      ${p.id.startsWith("ai_") ? '<span class="badge ai">AI</span>' : ""}
      ${Object.keys(p.params || {}).length ? `<span class="tag">${esc(Object.values(p.params).flat().join(" ").slice(0, 24))}</span>` : ""}
      <span class="sp"></span><span class="upd"></span><span class="menu">⋮</span></div>
    <div class="pb"><div class="loading">加载中…</div></div>
    <div class="rz"></div></div>`;
}

async function loadPanel(p, force = false) {
  const el = document.querySelector(`.panel[data-uid="${p.uid}"]`);
  if (!el) return;
  const body = $(".pb", el);
  if (p.id.startsWith("ai_") && !body.dataset.loaded) body.innerHTML = '<div class="loading">AI 正在读取数据并撰写，约 20 秒…</div>';
  try {
    const params = {...(p.params || {})};
    if (LIVE_NEWS_DASHES.has(S.cur?.id) && (p.id === "news" || p.id === "news_impact")) {
      params.latest = true;
    }
    const q = new URLSearchParams({id: p.id, date: S.date, params: JSON.stringify(params)});
    if (force) q.set("force", "1");
    const r = await api("/api/panel?" + q);
    if (r.viz === "portfolio_analysis" && body.querySelector('[data-workbench][data-editing="true"]')) return;
    const fn = R[r.viz];
    if (!fn) throw new Error("未知可视化 " + r.viz);
    fn(body, r.data, p);
    body.dataset.loaded = "1";
    $(".upd", el).textContent = new Date().toTimeString().slice(0, 5);
  } catch (e) {
    body.innerHTML = `<div class="err">加载失败：${esc(e.message)}</div>`;
  }
}

function renderDash() {
  window.AccountWorkbench?.dispose();
  S.charts.forEach(c => c.dispose()); S.charts.clear();
  const g = $("#grid");
  g.classList.toggle("edit", S.edit);
  g.classList.toggle("account-workspace", S.cur?.id === "portfolio" && !S.edit);
  g.innerHTML = S.cur.panels.map(panelHTML).join("") ||
    '<div class="empty" style="grid-column:span 24">空看板：点「+ 添加面板」，或用对话描述需要的内容。</div>';
  S.cur.panels.forEach(p => loadPanel(p));
  bindGrid();
  renderTabs();
  renderBoardBar();
}

function refreshAll(onlyLive) {
  if (!S.cur) return;
  S.cur.panels.forEach(p => {
    if (p.id.startsWith("ai_")) return;          // AI 面板只在手动刷新时重算
    if (onlyLive && !S.catalog[p.id]?.live && p.id !== "verdict" && p.id !== "outlook") return;
    loadPanel(p);
  });
}

// 只读模式：收起所有会触发写接口的入口。判断依据是 /api/meta 的 local ——
// 服务绑 0.0.0.0 时，内网同事那边这个值是 false。
function applyReadOnly() {
  if (!S.readOnly) return;
  for (const sel of ["#btnNewBoard", "#btnAdd", "#btnEdit",
                     "#btnBoardMore", "#btnAlerts"]) {
    const el = $(sel);
    if (el) el.hidden = true;
  }
}

function renderTabs() {
  $("#tabs").innerHTML = S.dashes.map(d => {
    const on = d.id === S.cur?.id;
    const del = (S.readOnly || WORKSPACE_DASHES.has(d.id)) ? "" : `<span class="del" data-del="${esc(d.id)}" title="删除看板">✕</span>`;
    const hint = S.readOnly ? "点击切换看板" : "双击重命名 · 拖动排序 · 右键更多";
    return `<div class="tab${on ? " on" : ""}" data-id="${esc(d.id)}" role="tab"
      draggable="${S.readOnly ? "false" : "true"}"
      aria-selected="${on}" title="${hint}">
      <span class="tab-name">${esc(d.title)}</span>${del}</div>`;
  }).join("");
}

// 看板工具栏：只显示当前看板的名称和操作，与全局顶栏分开。
function renderBoardBar() {
  const d = S.cur;
  $("#boardTitle").textContent = d?.title || "";
  $("#boardMeta").textContent = (d ? `${d.panels.length} 个面板` : "") + (S.readOnly ? " · 只读" : "");
  $("#btnEdit").classList.toggle("on", S.edit);
  $("#btnEdit").textContent = S.edit ? "完成编辑" : "编辑布局";
}

function openDash(id) {
  S.cur = structuredClone(S.dashes.find(d => d.id === id) || S.dashes[0]);
  LS.set("dash", S.cur.id);
  renderDash();
}

function initialDashId() {
  const savedId = LS.get("dash", null);
  const saved = S.dashes.find(d => d.id === savedId);
  const migrated = LS.get("workspace_nav_v2", false);
  LS.set("workspace_nav_v2", true);
  if (!migrated && saved?.builtin && !WORKSPACE_DASHES.has(saved.id)) return "information";
  return saved?.id || S.dashes[0].id;
}

async function saveCur(quiet) {
  // 兜底：任何路径新增的面板（手动添加、AI 添加、导入）都要带上坐标，
  // 否则后端会把整表当成缺坐标而按数组顺序重排，冲掉用户摆好的布局。
  S.cur.panels.forEach(p => {
    if (p.x === undefined || p.y === undefined) placeNewPanel(p);
  });
  const saved = await post("/api/dashboards", S.cur);
  const i = S.dashes.findIndex(d => d.id === saved.id);
  if (i >= 0) S.dashes[i] = {...S.dashes[i], ...saved}; else S.dashes.push(saved);
  S.cur.panels = saved.panels;
  if (!quiet) toast("已保存");
  renderTabs();
  renderBoardBar();
}

// ------------------------------------------------------------------ 拖拽 & 缩放
// 用 pointer events + setPointerCapture，不用 HTML5 DnD：DnD 的指针会被原生拖影接管，
// 也没法把像素位移稳定换算成格坐标。面板内的交互元素上不劫持拖动。
const NO_DRAG = "button,a,input,select,textarea,[role=button],[data-code],[data-toggle]";

// 读实际渲染出来的列宽与行距，把像素位移换算成格数。
// clientWidth 含 .grid 的水平 padding，可用宽度还要再减掉 23 个列间距。
function gridMetrics() {
  const cs = getComputedStyle($("#grid"));
  const gap = parseFloat(cs.columnGap) || 10;
  const padX = (parseFloat(cs.paddingLeft) || 0) + (parseFloat(cs.paddingRight) || 0);
  const usable = $("#grid").clientWidth - padX - (GRID.cols - 1) * gap;
  return {colW: Math.max(1, usable / GRID.cols),
    rowH: (parseFloat(cs.gridAutoRows) || 38) + (parseFloat(cs.rowGap) || gap)};
}

function bindGrid() {
  const g = $("#grid");
  g.querySelectorAll(".panel").forEach(el => {
    const find = () => S.cur.panels.find(x => x.uid === el.dataset.uid);
    const ph = $(".ph", el), rz = $(".rz", el);

    // 拖标题栏整块移动。只在编辑态生效，否则读面板时轻轻一碰就把布局挪走了。
    ph.onpointerdown = e => {
      if (e.button !== 0 || !S.edit || e.target.closest(NO_DRAG)) return;
      const p = find();
      if (!p) return;
      e.preventDefault();                       // 拖动手柄不参与文本选中
      const {colW, rowH} = gridMetrics();
      const sx = e.clientX, sy = e.clientY, x0 = p.x, y0 = p.y;
      const snap = snapshotLayout();            // 拖动过程中别人要从这里"恢复再让位"
      let moved = false;
      ph.setPointerCapture(e.pointerId);
      const move = ev => {
        const dx = Math.round((ev.clientX - sx) / colW), dy = Math.round((ev.clientY - sy) / rowH);
        if (!moved && !dx && !dy) return;
        moved = true;
        el.classList.add("dragging");
        // 每次移动都从快照重算：先按指针给出拖动态的坐标，再让其它面板往下让开。
        // 指针移开时它们会回到原位，而不是被推下去留在下面。
        restoreLayout(snap);
        // 横向越界就地夹住：CSS Grid 会把超出的列当隐式列新建，整行跟着错位。
        p.x = Math.max(0, Math.min(GRID.cols - p.w, x0 + dx));
        p.y = Math.max(0, y0 + dy);
        window.ChaoguLayout.displace(S.cur.panels, p.uid);
        applyPositions(null);
      };
      const up = () => {
        ph.removeEventListener("pointermove", move);
        ph.removeEventListener("pointerup", up);
        ph.removeEventListener("pointercancel", up);
        el.classList.remove("dragging");
        if (!moved) return;
        // 松手后才做重力压实：拖动过程中压实会让面板一直往上跳，手感很糟。
        // 先把邻居恢复到拖动前、只留下这一块的落点，再压实——这样"谁在谁上面"
        // 按的是用户拖动前的排布，而不是被推开后的临时位置。
        const dropX = p.x, dropY = p.y;
        restoreLayout(snap);
        p.x = dropX; p.y = dropY;
        compactPanels(p.uid); applyPositions();
        saveCur(true).then(() => { compactPanels(); applyPositions(); });
      };
      ph.addEventListener("pointermove", move);
      ph.addEventListener("pointerup", up);
      ph.addEventListener("pointercancel", up);
    };

    // 手柄常显，所以点它本身就是「我要调布局」——顺手打开编辑态，不用先去顶栏找按钮。
    // 这里绝不能 renderDash()：重建 DOM 会让刚拿到的指针捕获立刻失效。
    rz.onpointerdown = e => {
      if (e.button !== 0) return;
      e.preventDefault(); e.stopPropagation();
      if (!S.edit) {
        S.edit = true;
        g.classList.add("edit");
        renderBoardBar();
      }
      const p = find();
      if (!p) return;
      const {colW, rowH} = gridMetrics();
      const sx = e.clientX, sy = e.clientY, w0 = p.w, h0 = p.h;
      const snap = snapshotLayout();
      rz.setPointerCapture(e.pointerId);
      const move = ev => {
        restoreLayout(snap);
        // 右边界是 24 列，所以宽度上限还要受列起点约束：x + w <= 24。
        p.w = Math.max(GRID.minW, Math.min(GRID.cols - p.x, Math.round(w0 + (ev.clientX - sx) / colW)));
        p.h = Math.max(GRID.minH, Math.min(GRID.maxH, Math.round(h0 + (ev.clientY - sy) / rowH)));
        // 撑大时邻居要立刻让开，否则会看起来被压在下面、连标题栏都点不到。
        window.ChaoguLayout.displace(S.cur.panels, p.uid);
        applyPositions(p.uid);
      };
      const up = () => {
        rz.removeEventListener("pointermove", move);
        rz.removeEventListener("pointerup", up);
        rz.removeEventListener("pointercancel", up);
        // 撑大可能盖住邻居，就地压实一次，和后端保存时的 _compact 保持同一结果。
        // 同样先恢复邻居、只留下这一块的新尺寸。
        const w = p.w, h = p.h;
        restoreLayout(snap);
        p.w = w; p.h = h;
        compactPanels(p.uid); applyPositions();
        saveCur(true).then(() => { compactPanels(); applyPositions(); });
      };
      rz.addEventListener("pointermove", move);
      rz.addEventListener("pointerup", up);
      rz.addEventListener("pointercancel", up);
    };
  });
}

// ------------------------------------------------------------------ 菜单 / 弹窗
function popMenu(x, y, items) {
  document.querySelectorAll(".menu-pop").forEach(m => m.remove());
  const m = document.createElement("div");
  m.className = "menu-pop";
  m.innerHTML = items.map((it, i) => it === "-" ? "<hr>" : `<div data-i="${i}">${esc(it[0])}</div>`).join("");
  document.body.appendChild(m);
  m.style.left = Math.min(x, innerWidth - m.offsetWidth - 8) + "px";
  m.style.top = Math.min(y, innerHeight - m.offsetHeight - 8) + "px";
  m.onclick = e => { const i = e.target.dataset.i; if (i !== undefined) { items[i][1](); m.remove(); } };
  setTimeout(() => addEventListener("click", () => m.remove(), {once: true}), 0);
}

function modal(html) { $("#modalCard").innerHTML = html; $("#modal").classList.add("on"); }
function closeModal() { $("#modal").classList.remove("on"); $("#modalCard").querySelectorAll("#brokerForm input").forEach(f => { f.value = ""; }); $("#modalCard").replaceChildren(); }
$("#modal").onclick = e => { if (e.target.id === "modal") closeModal(); };

function panelMenu(p, x, y) {
  // 只读模式（内网同事打开时）：这个菜单里除了刷新全是写操作，
  // 点了只会拿到 403，所以只留能用的那一项。
  if (S.readOnly) {
    popMenu(x, y, [["↻ 刷新", () => loadPanel(p, true)]]);
    return;
  }
  const ps = S.cur.panels, i = ps.indexOf(p);
  // activeUid 必须传：不传的话"移到顶部"会输给原来就在第 0 行的面板
  // （重力按 (y,x) 排序、并列时保持数组顺序），表现成点了没反应。
  const settle = activeUid => { compactPanels(activeUid); applyPositions(); saveCur(true); };
  popMenu(x, y, [
    ["↻ 刷新", () => loadPanel(p, true)],
    ["✎ 编辑标题/参数", () => editPanel(p)],
    // AI 面板报错时，最近的一步就是去改凭证/模型——放在这里省一次翻菜单。
    ...(p.id.startsWith("ai_") ? [["⚙ AI 设置（凭证 / 模型）", () => openAiSettings()], "-"] : []),
    ["宽度：整行", () => resize(p, 24)], ["宽度：半行", () => resize(p, 12)], ["宽度：1/3", () => resize(p, 8)],
    ["高度：小", () => resize(p, null, 6)], ["高度：中", () => resize(p, null, 10)], ["高度：大", () => resize(p, null, 16)],
    "-",
    ["↑ 移到顶部", () => { p.y = 0; settle(p.uid); }],
    ["↓ 移到底部", () => { p.y = Math.max(...ps.map(q => (q === p ? 0 : q.y + q.h))); settle(p.uid); }],
    "-",
    ["✕ 移除面板", () => { ps.splice(i, 1); renderDash(); saveCur(true); }],
  ]);
}

// w / h 传 null 表示只改一维，另一维持平。
function resize(p, w, h) {
  const t = S.cur.panels.find(x => x.uid === p.uid);
  if (!t) return;
  if (w != null) t.w = w;
  if (h != null) t.h = h;
  compactPanels(t.uid);            // 改尺寸会挤到邻居，顺手压实一次（这块优先占位）
  applyPositions();
  saveCur(true);
}

// 面板口径说明：可信度、数据来源、局限。移动端也能用，不依赖悬停。
function openPanelInfo(uid) {
  const p = S.cur.panels.find(x => x.uid === uid);
  const c = p && S.catalog[p.id];
  if (!c) return;
  const ks = Object.keys(c.params || {});
  modal(`<h3>${esc(p.title)}</h3>
    <div class="pi-meta"><span class="trust ${esc(c.trust_class)}">${esc(c.trust_label)}</span>
      <span class="faint">${esc(c.title)}</span></div>
    <p class="pi-desc">${esc(c.desc)}</p>
    <div class="sec">口径与局限</div>
    <p class="pi-basis">${esc(c.basis || "未填写")}</p>
    ${ks.length ? `<div class="sec">可配置参数</div>${ks.map(k => {
      const spec = c.param_types?.[k];
      const hint = spec?.type === "int" ? `（整数 ${spec.min}–${spec.max}）` :
        spec?.type === "list" ? "（逗号分隔，最多 " + spec.max + " 项）" : "";
      return `<div class="pi-param"><b>${esc(k)}</b> ${esc(c.params[k])}${esc(hint)}</div>`;
    }).join("")}` : ""}
    <div class="row"><button class="btn primary" id="piClose">关闭</button></div>`);
  $("#piClose").onclick = closeModal;
}

function editPanel(p) {
  const c = S.catalog[p.id];
  const ks = Object.keys(c.params || {});
  modal(`<h3>编辑面板 · ${esc(c.title)}</h3><div class="form">
    <label>标题</label><input id="f_title" value="${esc(p.title)}">
    ${ks.map(k => {
      const spec = c.param_types?.[k] || {type: "text"};
      const cur = Array.isArray(p.params?.[k]) ? p.params[k].join(",") : (p.params?.[k] ?? "");
      const hint = spec.type === "list" ? "（逗号分隔）" : spec.type === "int" ? `（${spec.min}–${spec.max}）` : "";
      // list 的 max 是项数而不是字符数，因此不设 maxlength；text 的 max 才是字符上限。
      const control = spec.type === "int" ?
        `<input type="number" min="${spec.min}" max="${spec.max}" step="1" id="f_${k}" value="${esc(cur)}">` :
        spec.type === "list" ? `<input id="f_${k}" value="${esc(cur)}">` :
        spec.max > 100 ? `<textarea id="f_${k}" maxlength="${spec.max}">${esc(cur)}</textarea>` :
        `<input id="f_${k}" maxlength="${spec.max || 200}" value="${esc(cur)}">`;
      return `<label>${esc(k)} — ${esc(c.params[k])}${esc(hint)}</label>${control}`;
    }).join("")}
    <div class="row"><button class="btn" id="f_cancel">取消</button><button class="btn primary" id="f_ok">保存</button></div></div>`);
  $("#f_cancel").onclick = closeModal;
  $("#f_ok").onclick = () => {
    // 保存响应会替换 S.cur.panels 中的对象，所以按 uid 重新定位，避免写进旧对象。
    const t = S.cur.panels.find(x => x.uid === p.uid);
    if (!t) { closeModal(); toast("面板已不在当前看板中", 3000); return; }
    t.title = $("#f_title").value.trim() || c.title;
    t.params = {};
    ks.forEach(k => {
      let v = $("#f_" + k).value.trim();
      if (!v) return;
      if (c.param_types?.[k]?.type === "list") v = v.split(/[,，\s]+/).filter(Boolean);
      if (c.param_types?.[k]?.type === "int") v = Number(v);
      t.params[k] = v;
    });
    closeModal(); renderDash(); saveCur(true);
  };
}

function addPanel() {
  modal(`<h3>添加面板 · ${esc(S.cur.title)}</h3><form class="askbox" id="addByAI"><span class="spark">✦</span>
    <input id="addPrompt" aria-label="用对话添加面板" placeholder="一句话描述要加的面板，例如：今日交易量排行" required>
    <button class="btn primary" type="submit">对话添加</button></form>
    <p class="dim">描述你需要的内容，面板将加入当前看板。${S.meta.llm ? "AI 从已有面板能力中挑选并排布，不编造数据。" : "AI 未配置，按关键词添加（运行 run.py llm-setup 开启 AI）。"} 也可从下方手动选择。</p>
    <div class="chips">${EXAMPLES.map((x, i) => `<span class="chip" data-i="${i}">${esc(x)}</span>`).join("")}</div>
    <div class="cat-list">${S.meta.catalog.map(c =>
    `<div class="cat-item" data-id="${c.id}"><b>${esc(c.title)} ${c.trust_short ? `<span class="trust ${esc(c.trust_class)}">${esc(c.trust_short)}</span>` : ""}</b><span>${esc(c.desc)}</span></div>`).join("")}</div>`);
  $("#addPrompt").focus();
  $("#modalCard").querySelectorAll(".chip").forEach(c => c.onclick = () => {
    $("#addPrompt").value = EXAMPLES[+c.dataset.i];
    $("#addByAI").requestSubmit();
  });
  $("#addByAI").onsubmit = async e => {
    e.preventDefault();
    const input = $("#addPrompt"), b = $("button", e.currentTarget);
    const prompt = input.value.trim();
    if (!prompt) return;
    // 请求期间即使用户切换标签，也加到发起请求的看板。
    const target = structuredClone(S.cur);
    b.disabled = true; b.textContent = "编排中…";
    try {
      const spec = await post("/api/ai/panels", {prompt, date: S.date});
      if (!spec.panels.length) throw new Error("没有匹配的面板，请具体描述想看的内容");
      // 合并期间保留当前已保存的布局编辑。
      const latest = S.dashes.find(d => d.id === target.id);
      if (!latest) throw new Error("原看板已被删除，请重新添加");
      target.panels = [...spec.panels, ...latest.panels];
      const saved = await post("/api/dashboards", target);
      const i = S.dashes.findIndex(d => d.id === saved.id);
      S.dashes[i] = saved;
      if (S.cur.id === saved.id) { S.cur = structuredClone(saved); renderDash(); }
      closeModal();
      toast(`已${spec.how === "ai" ? "由 AI" : "按关键词"}添加 ${spec.panels.length} 个面板到「${saved.title}」${spec.error ? `（AI 未成功：${spec.error}，已改用关键词规则）` : ""}`, spec.error ? 8000 : 4500);
    } catch (err) { toast(err.message, 5000); }
    finally { b.disabled = false; b.textContent = "对话添加"; }
  };
  $("#modalCard").querySelectorAll(".cat-item").forEach(el => el.onclick = () => {
    const c = S.catalog[el.dataset.id];
    const p = {uid: Math.random().toString(36).slice(2, 10), id: c.id, title: c.title, w: c.w, h: c.h, params: {}};
    placeNewPanel(p);
    S.cur.panels.unshift(p);
    closeModal(); renderDash(); saveCur(true);
    if (Object.keys(c.params || {}).length) editPanel(p);
  });
}

// ------------------------------------------------------------------ 个股抽屉
async function openStock(code) {
  $("#drawer").classList.add("on");
  $("#drawerTitle").textContent = code;
  $("#drawerBody").innerHTML = '<div class="loading">加载中…</div>';
  try {
    const s = await api(`/api/stock?code=${encodeURIComponent(code)}&date=${S.date}`);
    $("#drawerTitle").innerHTML = `${esc(s.name || code)} <span class="faint">${esc(code)}</span>`;
    const lu = s.limit_up || {};
    $("#drawerBody").innerHTML = `
      <div class="account-tools"><button class="btn primary" id="stockPosition">录入已有持仓</button><button class="btn" id="stockTrade">记录已成交交易</button></div>
      <div class="kv">
        <span class="k">高度</span><span class="up">${esc(lu.height_text || (lu.height ? lu.height + "板" : "—"))}</span>
        <span class="k">首封/末封</span><span>${esc(lu.first_seal || "—")} / ${esc(lu.last_seal || "—")}</span>
        <span class="k">开板</span><span>${esc(lu.open_num ?? "—")} 次</span>
        <span class="k">封单</span><span>${lu.seal_amount ? (lu.seal_amount / 1e8).toFixed(2) + " 亿" : "—"}</span>
        <span class="k">换手</span><span>${lu.turnover ? lu.turnover.toFixed(1) + "%" : "—"}</span>
        <span class="k">流通市值</span><span>${lu.float_mv ? (lu.float_mv / 1e8).toFixed(0) + " 亿" : "—"}</span>
        <span class="k">涨停原因</span><span>${esc(lu.reason || "—")}</span>
        <span class="k">所属主线</span><span>${(s.lines || []).map(x => `<span class="tag up">${esc(x)}</span>`).join(" ") || "—"}</span>
      </div>
      <div class="sec">催化原文（同花顺涨停原因）</div>
      <div class="pre">${esc(s.reason_info || "无")}</div>
      ${s.history?.length ? `<div class="sec">近期涨停记录</div>
        <table><tbody>${s.history.map(h => `<tr><td>${esc(h.trade_date)}</td><td class="num up">${h.height}板</td><td class="wrap">${esc(h.reason || "")}</td></tr>`).join("")}</tbody></table>` : ""}
      ${s.news?.length ? `<div class="sec">相关快讯</div>${s.news.map(n => `<div style="padding:5px 0;border-bottom:1px solid var(--line)"><span class="faint">${esc(n.ts.slice(5, 16).replace("T", " "))}</span> ${esc(n.title)}</div>`).join("")}` : ""}`;
    $("#stockPosition").onclick = () => accountForm("position", code, s.name);
    $("#stockTrade").onclick = () => accountForm("trade", code, s.name);
  } catch (e) { $("#drawerBody").innerHTML = `<div class="err">${esc(e.message)}</div>`; }
}
$("#drawerClose").onclick = () => $("#drawer").classList.remove("on");

// ------------------------------------------------------------------ 看板操作
const boardIndex = id => S.dashes.findIndex(d => d.id === id);
const newBoardId = () => "d" + Math.random().toString(36).slice(2, 10);

// 通用单行输入弹窗（用于改名、空白看板命名）。
function askText(title, value, onOk, hint = "") {
  modal(`<h3>${esc(title)}</h3><form class="form" id="askTextForm">
    <input id="askTextInput" maxlength="40" value="${esc(value)}" required>
    ${hint ? `<p class="dim">${esc(hint)}</p>` : ""}
    <div class="row"><button type="button" class="btn" id="askCancel">取消</button><button type="submit" class="btn primary">确定</button></div></form>`);
  const input = $("#askTextInput");
  input.focus(); input.select();
  $("#askCancel").onclick = closeModal;
  $("#askTextForm").onsubmit = e => {
    e.preventDefault();
    const v = input.value.trim();
    if (!v) return;
    closeModal(); onOk(v);
  };
}

// 新建看板：只建一个**空白**看板，面板由用户自己加（「+ 添加面板」，在那里可以用
// 一句话让 AI 挑面板）。这里刻意不再提供"一句话生成整个看板"——那会绕过用户的
// 选择、凭空造出一个装满面板的看板，而用户要的往往只是"加一个面板"。
function openNewBoard() {
  askText("新看板名称", `新看板 ${S.dashes.length + 1}`, createBlankBoard);
}

async function createBlankBoard(title) {
  try {
    const d = await post("/api/dashboards", {id: newBoardId(), title, panels: []});
    S.dashes.push(d);
    openDash(d.id);
    toast(`已新建「${d.title}」，点「+ 添加面板」开始搭建`, 3500);
  } catch (e) { toast("新建失败：" + e.message, 4000); }
}

function renameBoard(id) {
  const d = S.dashes.find(x => x.id === id);
  if (!d) return;
  askText("重命名看板", d.title, async name => {
    try {
      const base = id === S.cur?.id ? S.cur : d;
      const saved = await post("/api/dashboards", {...structuredClone(base), title: name});
      const i = boardIndex(id);
      if (i >= 0) S.dashes[i] = {...S.dashes[i], ...saved};
      if (S.cur?.id === id) S.cur.title = saved.title;
      renderTabs(); renderBoardBar();
      toast("已重命名");
    } catch (e) { toast("重命名失败：" + e.message, 4000); }
  });
}

async function duplicateBoard(id) {
  try {
    const copy = await post("/api/dashboards/duplicate", {id});
    S.dashes.splice(boardIndex(id) + 1, 0, copy);
    openDash(copy.id);
    toast(`已复制为「${copy.title}」`);
  } catch (e) { toast("复制失败：" + e.message, 4000); }
}

async function deleteBoard(id) {
  const d = S.dashes.find(x => x.id === id);
  if (!d || WORKSPACE_DASHES.has(id)) return;
  if (!confirm(`删除看板「${d.title}」？此操作不可撤销。`)) return;
  try {
    await api("/api/dashboards?id=" + encodeURIComponent(id), {method: "DELETE"});
    S.dashes = S.dashes.filter(x => x.id !== id);
    if (S.cur?.id === id) openDash(S.dashes[0].id); else renderTabs();
    toast("已删除看板");
  } catch (e) { toast("删除失败：" + e.message, 4000); }
}

// 导出：只包含布局、面板 id、标题、参数与提示语，不含行情或账户数据。
async function exportBoard(id) {
  try {
    const doc = await api("/api/dashboards/export?id=" + encodeURIComponent(id));
    const name = (doc.dashboard.title || "看板").replace(/[\\/:*?"<>|\s]+/g, "_").slice(0, 40);
    const url = URL.createObjectURL(new Blob([JSON.stringify(doc, null, 2)], {type: "application/json"}));
    const a = document.createElement("a");
    a.href = url; a.download = name + ".chaogu.json";
    document.body.append(a); a.click(); a.remove();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
    toast(`已导出「${doc.dashboard.title}」`);
  } catch (e) { toast("导出失败：" + e.message, 4000); }
}

// 导入：先校验并列出警告，用户确认后才保存为新看板。
function openImport() {
  let pending = null;
  modal(`<h3>导入看板</h3>
    <p class="dim">只接受本项目导出的看板文件（chaogu-dashboard）。导入的是布局、面板与参数，不包含行情或账户数据。</p>
    <div class="form">
      <label>选择文件</label><input type="file" id="impFile" accept=".json,application/json">
      <label>或粘贴 JSON 内容</label><textarea id="impText" rows="6" placeholder='{"format":"chaogu-dashboard", ...}'></textarea>
      <div class="err" id="impErr" role="alert"></div>
      <div id="impPreview"></div>
      <div class="row"><button type="button" class="btn" id="impCancel">取消</button>
        <button type="button" class="btn" id="impCheck">校验</button>
        <button type="button" class="btn primary" id="impSave" disabled>导入为新看板</button></div>
    </div>`);
  const reset = () => { pending = null; $("#impSave").disabled = true; $("#impPreview").innerHTML = ""; };
  $("#impFile").onchange = async e => {
    const f = e.target.files[0];
    if (!f) return;
    if (f.size > 256 * 1024) { $("#impErr").textContent = "文件过大（上限 256KB）"; return; }
    $("#impText").value = await f.text();
    reset(); $("#impErr").textContent = "";
  };
  $("#impText").oninput = () => { reset(); $("#impErr").textContent = ""; };
  $("#impCancel").onclick = closeModal;
  $("#impCheck").onclick = async () => {
    $("#impErr").textContent = "";
    let payload;
    try { payload = JSON.parse($("#impText").value); }
    catch { $("#impErr").textContent = "不是有效的 JSON，请检查粘贴内容或文件"; return; }
    try {
      const r = await post("/api/dashboards/import/preview", payload);
      pending = r.dashboard;
      const d = r.dashboard;
      $("#impPreview").innerHTML = `<div class="sec">预览</div>
        <div class="pi-meta"><b>${esc(d.title)}</b><span class="faint">${d.panels.length} 个面板</span></div>
        <div class="imp-panels">${d.panels.map(p => `<span class="tag">${esc(p.title)}</span>`).join("") || '<span class="faint">没有可用面板</span>'}</div>
        ${r.warnings.length ? `<div class="sec">注意（${r.warnings.length} 项）</div><ul class="imp-warn">${r.warnings.map(w => `<li>${esc(w)}</li>`).join("")}</ul>` : '<p class="dim">校验通过，没有需要注意的内容。</p>'}`;
      $("#impSave").disabled = false;
    } catch (e) { $("#impErr").textContent = e.message; }
  };
  $("#impSave").onclick = async () => {
    if (!pending) return;
    const b = $("#impSave"); b.disabled = true;
    try {
      const saved = await post("/api/dashboards", pending);
      S.dashes.push(saved);
      closeModal();
      openDash(saved.id);
      toast(`已导入「${saved.title}」`);
    } catch (e) { $("#impErr").textContent = "导入失败：" + e.message; b.disabled = false; }
  };
}

// 排序：先乐观更新界面，服务端失败时回滚。
async function applyOrder(ids) {
  const before = S.dashes;
  S.dashes = ids.map(id => before.find(d => d.id === id)).filter(Boolean);
  renderTabs();
  try {
    const r = await post("/api/dashboards/order", {ids});
    S.dashes = r.dashboards;
    renderTabs();
  } catch (e) {
    S.dashes = before; renderTabs();
    toast("调整顺序失败：" + e.message, 4000);
  }
}

function moveBoard(id, delta) {
  const ids = S.dashes.map(d => d.id);
  const i = ids.indexOf(id), j = i + delta;
  if (i < 0 || j < 0 || j >= ids.length) return;
  [ids[i], ids[j]] = [ids[j], ids[i]];
  applyOrder(ids);
}

function boardMenu(id, x, y) {
  const items = [
    ["重命名", () => renameBoard(id)],
    ["复制为新看板", () => duplicateBoard(id)],
    ["导出为 JSON", () => exportBoard(id)],
    "-",
    ["左移", () => moveBoard(id, -1)],
    ["右移", () => moveBoard(id, 1)],
  ];
  if (!WORKSPACE_DASHES.has(id)) items.push("-", ["删除看板", () => deleteBoard(id)]);
  popMenu(x, y, items);
}

// ------------------------------------------------------------------ 实时状态
async function pollLive() {
  try {
    const s = await api("/api/live");
    const el = $("#live");
    el.className = "live " + (s.session && s.enabled ? "on" : "");
    const market = `行情日 ${S.date}`;
    const news = s.enabled ? `快讯刷新 ${s.last.news || "等待中"}` : "快讯自动更新未开启";
    $("span", el).textContent = s.session && s.enabled ? `盘中实时 · ${market} · ${news}` :
      `${market} · ${news}`;
    el.title = Object.entries(s.last).map(([k, v]) => `${k} ${v}`).join("\n") +
      (Object.keys(s.errors).length ? "\n错误：\n" + Object.values(s.errors).join("\n") : "");
  } catch {}
}

async function loadAlertHistory() {
  const body = $("#drawerBody");
  body.innerHTML = '<div class="loading">正在读取触发记录…</div>';
  try {
    const data = await api("/api/watch/history?limit=200");
    body.innerHTML = `<div class="watch-alert-actions"><span class="dim">最近 ${data.items.length} 条</span>
      <button class="btn" data-alert-read-all>全部标为已读</button><button class="btn" data-alert-clear>清理已读</button></div>
      <div class="watch-event-list">${watchEventRows(data.items)}</div>`;
    body.querySelector("[data-alert-read-all]").onclick = async () => {
      try { await post("/api/watch/read", {all_unread: true}); await loadAlertHistory(); pollWatch(); }
      catch (e) { toast(e.message); }
    };
    body.querySelector("[data-alert-clear]").onclick = async () => {
      if (!confirm("清理所有已读触发记录？未读记录会保留。")) return;
      try { await post("/api/watch/history/clear", {read_only: true}); await loadAlertHistory(); }
      catch (e) { toast(e.message); }
    };
    body.querySelectorAll(".watch-read-one").forEach(b => b.onclick = async () => {
      try { await post("/api/watch/read", {ids: [b.dataset.id]}); await loadAlertHistory(); pollWatch(); }
      catch (e) { toast(e.message); }
    });
  } catch (e) { body.innerHTML = `<div class="err">${esc(e.message)}</div>`; }
}

function openAlerts() {
  $("#drawerTitle").textContent = "触发记录";
  $("#drawer").classList.add("on");
  loadAlertHistory();
}

// ---------------------------------------------------------------- AI 设置
// Grafana 式：属性放在右侧抽屉里改，不跳页、不弹居中弹窗。
// 两处刻意的设计：
//   1. key 只写不读——后端只回掩码，输入框永远不预填明文；留空 = 沿用已存的那把。
//   2. 保存后立刻自检一次，并把 AI 面板强制重算，否则用户会盯着旧报错以为没生效。
const AI_SOURCE_TEXT = {file: "来自 data/llm.json", env: "来自环境变量（优先级更高）",
  none: "尚未配置"};

async function openAiSettings() {
  $("#drawerTitle").textContent = "AI 设置";
  $("#drawer").classList.add("on");
  const body = $("#drawerBody");
  body.innerHTML = '<div class="loading">读取配置…</div>';
  let c;
  try { c = await api("/api/llm/config"); }
  catch (e) { body.innerHTML = `<div class="err">读取配置失败：${esc(e.message)}</div>`; return; }
  body.innerHTML = `<div class="ai-cfg">
    <div class="ai-cfg-state">
      <span class="trust ${c.available ? "trust-verified" : "trust-data"}">${c.available ? "已配置" : "未配置"}</span>
      <span class="faint">${esc(AI_SOURCE_TEXT[c.source] || c.source)}</span>
      ${c.has_key ? `<span class="faint">· Key ${esc(c.key_hint)}</span>` : '<span class="warn">· 没有 Key</span>'}
    </div>
    ${c.writable ? "" : `<p class="warn">检测到 CHAOGU_LLM_* 环境变量，它的优先级高于 data/llm.json——
      在这里保存不会生效。请先在启动看板的 shell 里 unset 这些变量。</p>`}
    <form class="form" id="aiForm">
      <label>接口地址（base）</label>
      <input id="aiBase" value="${esc(c.base)}" placeholder="https://llm-gateway.example.com" autocomplete="off" spellcheck="false">
      <label>协议</label>
      <select id="aiApi">
        <option value="anthropic">anthropic（自动拼 /v1/messages）</option>
        <option value="openai">openai 兼容（自动拼 /v1/chat/completions）</option>
      </select>
      <label>模型名</label>
      <input id="aiModel" value="${esc(c.model)}" placeholder="claude-sonnet-5-5" autocomplete="off" spellcheck="false">
      <label>API Key</label>
      <input id="aiKey" type="password" autocomplete="new-password"
        placeholder="${c.has_key ? "留空 = 沿用已保存的 Key（" + esc(c.key_hint) + "）" : "粘贴网关签发的 token"}">
      <p class="dim">Key 只写不读：保存后接口只返回掩码，页面不会拿到明文。写入本机
        <code>${esc(c.path)}</code>（权限 600）。</p>
      <div class="row"><button type="button" class="btn" id="aiTest">测试连通性</button>
        <button type="submit" class="btn primary" id="aiSave" ${c.writable ? "" : "disabled"}>保存</button></div>
    </form>
    <div id="aiResult"></div></div>`;
  $("#aiApi").value = c.api === "openai" ? "openai" : "anthropic";
  const draft = () => ({base: $("#aiBase").value.trim(), api: $("#aiApi").value,
    model: $("#aiModel").value.trim(), key: $("#aiKey").value});
  const show = html => { $("#aiResult").innerHTML = html; };
  const failBox = r => `<div class="ai-cfg-bad">❌ <b>${esc(r.message || "调用失败")}</b>` +
    (r.action ? `<br>${esc(r.action)}` : "") +
    (r.status ? `<br><span class="faint">来自网关的 HTTP ${esc(r.status)}</span>` : "") +
    (r.detail ? `<pre class="ai-cfg-raw">${esc(r.detail)}</pre>` : "") + "</div>";

  $("#aiTest").onclick = async e => {
    const b = e.currentTarget;
    b.disabled = true; b.textContent = "测试中…";
    show('<div class="loading">正在向网关发一次最小请求…</div>');
    try {
      const r = await post("/api/llm/test", draft());
      show(r.ok ? `<div class="ai-cfg-ok">✅ ${esc(r.message)}<br>
        <span class="faint">${esc(r.api)} · ${esc(r.model)} · ${esc(r.base)}</span></div>` : failBox(r));
    } catch (err) { show(`<div class="ai-cfg-bad">❌ ${esc(err.message)}</div>`); }
    finally { b.disabled = false; b.textContent = "测试连通性"; }
  };
  $("#aiForm").onsubmit = async e => {
    e.preventDefault();
    const b = $("#aiSave");
    b.disabled = true; b.textContent = "保存中…";
    try {
      const r = await post("/api/llm/config", draft());
      $("#aiKey").value = "";                                  // 不回填明文，也不留在 DOM 里
      toast("AI 配置已保存");
      S.meta.llm = r.config.available;
      $("#aiTest").click();                                    // 存完立刻自检，省一次点击
      refreshAiPanels();
    } catch (err) { show(`<div class="ai-cfg-bad">❌ ${esc(err.message)}</div>`); }
    finally { b.disabled = false; b.textContent = "保存"; }
  };
}

// 换凭证/换模型后，AI 面板的缓存结果就过期了，强制重算。
function refreshAiPanels() {
  const ps = (S.cur?.panels || []).filter(p => p.id.startsWith("ai_"));
  if (!ps.length) return;
  ps.forEach(p => loadPanel(p, true));
  toast(`已重新生成 ${ps.length} 个 AI 面板`, 4000);
}

async function pollWatch(announce = false) {
  // 只读模式没有 /api/watch 的权限，轮询只会每几秒刷一条 403。
  // 守卫放在函数入口，这样所有调用点（含交互回调）都覆盖到。
  if (S.readOnly) return;
  try {
    const s = await api("/api/watch/status");
    const badge = $("#unreadBadge");
    badge.textContent = s.unread > 99 ? "99+" : String(s.unread);
    badge.hidden = !s.unread;
    $("#btnAlerts").setAttribute("aria-label", s.unread ? `触发记录，${s.unread} 条未读` : "触发记录");
    if (S.watchUnread === null) {
      S.watchUnread = s.unread;
      if (announce && s.unread) toast(`有 ${s.unread} 条未读提醒`, 4500);
    } else if (s.unread > S.watchUnread) {
      toast(`收到 ${s.unread - S.watchUnread} 条新提醒`, 3500);
    }
    S.watchUnread = s.unread;
  } catch {}
}

function schedule() {
  S.timers.forEach(clearInterval); S.timers = [];
  const sec = +$("#refresh").value;
  LS.set("refresh", sec);
  if (sec) S.timers.push(setInterval(() => refreshAll(false), sec * 1000));
  S.timers.push(setInterval(pollLive, 15000));
  S.timers.push(setInterval(pollWatch, 15000));
  S.timers.push(setInterval(() => {
    if (S.cur?.id === "learning") {
      const panel = S.cur.panels.find(p => p.id === "watch_center");
      if (panel) loadPanel(panel);
    }
  }, 30000));
}

// ------------------------------------------------------------------ 事件绑定
document.addEventListener("click", e => {
  const t = e.target;
  const code = t.closest("[data-code]")?.dataset.code;
  if (code && !S.edit) { openStock(code); return; }
  const tg = t.dataset.toggle;
  if (tg) { const x = document.getElementById(tg); x.hidden = !x.hidden; t.textContent = x.hidden ? "原文 ▾" : "收起 ▴"; return; }
  if (t.classList.contains("menu")) {
    const uid = t.closest(".panel").dataset.uid;
    const r = t.getBoundingClientRect();
    panelMenu(S.cur.panels.find(p => p.uid === uid), r.left - 120, r.bottom + 4);
    e.stopPropagation(); return;
  }
  if (t.dataset.del) {
    e.stopPropagation();
    deleteBoard(t.dataset.del);
    return;
  }
  if (t.dataset.info) {
    e.stopPropagation();
    openPanelInfo(t.dataset.info);
    return;
  }
  if (t.closest("[data-ai-settings]")) {
    openAiSettings();
    return;
  }
  const tab = t.closest(".tab");
  if (tab && tab.dataset.id !== S.cur?.id) openDash(tab.dataset.id);
});

$("#btnNewBoard").onclick = openNewBoard;
$("#btnAdd").onclick = addPanel;
$("#btnAlerts").onclick = openAlerts;
$("#btnEdit").onclick = () => { S.edit = !S.edit; renderDash(); };
$("#btnBoardMore").onclick = e => {
  const r = e.currentTarget.getBoundingClientRect();
  boardMenu(S.cur.id, r.right - 160, r.bottom + 4);
};
$("#btnMore").onclick = e => popMenu(e.clientX - 150, e.clientY + 14, S.readOnly
  // 只读模式：AI 设置、导入、恢复默认看板全部是写操作，留着只会 403
  ? [["旧版看板", () => open("/legacy", "_blank")]]
  : [
      ["⚙ AI 设置…", () => openAiSettings()],
      "-",
      ["导入看板…", () => openImport()],
      ["恢复默认看板", async () => { if (!confirm("恢复默认会删除所有自定义看板，确定？")) return;
          S.dashes = (await post("/api/dashboards/reset", {})).dashboards; openDash(S.dashes[0].id); }],
      "-",
      ["旧版看板", () => open("/legacy", "_blank")],
    ]);

// 标签：双击改名、右键更多、拖动排序（都是写操作，只读模式下一律不接）
const tabsEl = $("#tabs");
tabsEl.addEventListener("dblclick", e => {
  if (S.readOnly) return;
  const tab = e.target.closest(".tab");
  if (tab && !e.target.closest(".del")) renameBoard(tab.dataset.id);
});
tabsEl.addEventListener("contextmenu", e => {
  if (S.readOnly) return;
  const tab = e.target.closest(".tab");
  if (!tab) return;
  e.preventDefault();
  boardMenu(tab.dataset.id, e.clientX, e.clientY);
});
let dragTabId = null;
tabsEl.addEventListener("dragstart", e => {
  if (S.readOnly) return;
  const tab = e.target.closest(".tab");
  if (!tab) return;
  dragTabId = tab.dataset.id;
  e.dataTransfer.effectAllowed = "move";
  e.dataTransfer.setData("text/plain", dragTabId);
  tab.classList.add("dragging");
});
tabsEl.addEventListener("dragend", () => {
  dragTabId = null;
  tabsEl.querySelectorAll(".tab").forEach(t => t.classList.remove("dragging", "drop-before", "drop-after"));
});
tabsEl.addEventListener("dragover", e => {
  const tab = e.target.closest(".tab");
  if (!tab || !dragTabId || tab.dataset.id === dragTabId) return;
  e.preventDefault();
  const r = tab.getBoundingClientRect();
  const after = e.clientX > r.left + r.width / 2;
  tabsEl.querySelectorAll(".tab").forEach(t => t.classList.remove("drop-before", "drop-after"));
  tab.classList.add(after ? "drop-after" : "drop-before");
});
tabsEl.addEventListener("drop", e => {
  const tab = e.target.closest(".tab");
  if (!tab || !dragTabId || tab.dataset.id === dragTabId) return;
  e.preventDefault();
  const after = tab.classList.contains("drop-after");
  const ids = S.dashes.map(d => d.id).filter(x => x !== dragTabId);
  ids.splice(ids.indexOf(tab.dataset.id) + (after ? 1 : 0), 0, dragTabId);
  dragTabId = null;
  applyOrder(ids);
});
$("#date").onchange = () => { S.date = $("#date").value; renderDash(); pollLive(); };
$("#refresh").onchange = schedule;
addEventListener("resize", () => S.charts.forEach(c => c.resize()));
addEventListener("keydown", e => { if (e.key === "Escape") { closeModal(); $("#drawer").classList.remove("on"); } });

// ------------------------------------------------------------------ 启动
(async () => {
  S.meta = await api("/api/meta");
  S.readOnly = S.meta.local === false;
  applyReadOnly();
  S.meta.catalog.forEach(c => S.catalog[c.id] = c);
  S.dashes = S.meta.dashboards;
  S.date = S.meta.latest;
  $("#date").innerHTML = S.meta.dates.map(d => `<option>${d}</option>`).join("");
  $("#refresh").value = String(LS.get("refresh", 60));
  openDash(initialDashId());
  pollLive(); schedule();
  if (S.readOnly) {
    // 只读模式（内网同事打开）：布局提示里说的「编辑布局」按钮已经收起，
    // 提醒角标走的 /api/watch 对他也是 403，两件事都不必做。
  } else if (!LS.get("layout_hint_v1", false)) {
    // 常显的缩放手柄不会自己说明用法，首次访问给一次性提示。
    // 未读提醒走的是同一个浮层，会比提示晚一步返回并把它顶掉，所以首次访问只刷角标不播报。
    LS.set("layout_hint_v1", true);
    pollWatch();
    toast("面板可自由摆放：点「编辑布局」后拖标题栏移动，拉右下角手柄改大小，右键面板有更多操作", 9000);
  } else {
    pollWatch(true);
  }
})().catch(e => { $("#grid").innerHTML = `<div class="err" style="grid-column:span 24">启动失败：${esc(e.message)}</div>`; });
})();
