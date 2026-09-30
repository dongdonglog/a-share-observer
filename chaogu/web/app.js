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
};
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
  el.innerHTML = `<div class="news">${d.items.map(n => {
    const isNew = !first && !S.seenNews.has(n.ts + n.title);
    S.seenNews.add(n.ts + n.title);
    return `<div class="it ${isNew ? "new" : ""}"><div><span class="tm">${esc((n.ts || "").slice(5, 16).replace("T", " "))}</span>
      <span class="tt">${esc(n.title)}</span></div>
      ${n.content ? `<div class="ct">${esc(n.content)}</div>` : ""}
      ${(n.lines.length || n.tags.length) ? `<div class="tg">${n.lines.map(x => `<span class="tag up">主线·${esc(x)}</span>`).join("")}
        ${n.tags.map(x => `<span class="tag">${esc(x)}</span>`).join("")}</div>` : ""}</div>`;
  }).join("") || '<div class="empty">没有匹配的快讯</div>'}</div>`;
};

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
    (d.ai ? `<div class="bt">由 AI 基于看板数据生成${d.at ? "（" + esc(d.at) + "）" : ""}，只作观察清单，不构成投资建议。</div>` : "");
};

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

// ------------------------------------------------------------------ 面板
function panelHTML(p) {
  const c = S.catalog[p.id] || {};
  return `<div class="panel" draggable="false" data-uid="${p.uid}"
      style="grid-column:span ${p.w};grid-row:span ${p.h}">
    <div class="ph"><span class="t">${esc(p.title)}</span>
      <span class="info" title="${esc(c.desc || "")}">ⓘ</span>
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
    const q = new URLSearchParams({id: p.id, date: S.date, params: JSON.stringify(p.params || {})});
    if (force) q.set("force", "1");
    const r = await api("/api/panel?" + q);
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
  S.charts.forEach(c => c.dispose()); S.charts.clear();
  const g = $("#grid");
  g.classList.toggle("edit", S.edit);
  g.innerHTML = S.cur.panels.map(panelHTML).join("") ||
    '<div class="empty" style="grid-column:span 24">空看板：点右上「+ 面板」，或在上方输入一句话生成。</div>';
  S.cur.panels.forEach(p => loadPanel(p));
  bindGrid();
  renderTabs();
}

function refreshAll(onlyLive) {
  if (!S.cur) return;
  S.cur.panels.forEach(p => {
    if (p.id.startsWith("ai_")) return;          // AI 面板只在手动刷新时重算
    if (onlyLive && !S.catalog[p.id]?.live && p.id !== "verdict" && p.id !== "outlook") return;
    loadPanel(p);
  });
}

function renderTabs() {
  $("#tabs").innerHTML = S.dashes.map(d => `<div class="tab ${d.id === S.cur?.id ? "on" : ""}" data-id="${esc(d.id)}">
    ${esc(d.title)}${d.builtin ? "" : `<span class="del" data-del="${esc(d.id)}" title="删除看板">✕</span>`}</div>`).join("");
}

function openDash(id) {
  S.cur = structuredClone(S.dashes.find(d => d.id === id) || S.dashes[0]);
  LS.set("dash", S.cur.id);
  renderDash();
}

async function saveCur(quiet) {
  const saved = await post("/api/dashboards", S.cur);
  const i = S.dashes.findIndex(d => d.id === saved.id);
  if (i >= 0) S.dashes[i] = {...S.dashes[i], ...saved}; else S.dashes.push(saved);
  S.cur.panels = saved.panels;
  if (!quiet) toast("已保存");
  renderTabs();
}

// ------------------------------------------------------------------ 拖拽 & 缩放
function bindGrid() {
  const g = $("#grid");
  g.querySelectorAll(".panel").forEach(el => {
    el.draggable = S.edit;
    el.ondragstart = e => { if (!S.edit) return; el.classList.add("dragging"); e.dataTransfer.setData("text", el.dataset.uid); };
    el.ondragend = () => el.classList.remove("dragging");
    el.ondragover = e => { if (S.edit) { e.preventDefault(); el.classList.add("drag-over"); } };
    el.ondragleave = () => el.classList.remove("drag-over");
    el.ondrop = e => {
      e.preventDefault(); el.classList.remove("drag-over");
      const from = e.dataTransfer.getData("text"), to = el.dataset.uid;
      if (from === to) return;
      const ps = S.cur.panels, a = ps.findIndex(x => x.uid === from), b = ps.findIndex(x => x.uid === to);
      const [m] = ps.splice(a, 1); ps.splice(b, 0, m);
      renderDash(); saveCur(true);
    };
    const rz = $(".rz", el);
    rz.onpointerdown = e => {
      e.preventDefault(); e.stopPropagation();
      const p = S.cur.panels.find(x => x.uid === el.dataset.uid);
      const colW = (g.clientWidth - 32) / 24, rowH = 48;
      const sx = e.clientX, sy = e.clientY, w0 = p.w, h0 = p.h;
      const move = ev => {
        p.w = Math.max(4, Math.min(24, Math.round(w0 + (ev.clientX - sx) / colW)));
        p.h = Math.max(3, Math.min(20, Math.round(h0 + (ev.clientY - sy) / rowH)));
        el.style.gridColumn = `span ${p.w}`; el.style.gridRow = `span ${p.h}`;
        S.charts.get(p.uid)?.resize();
      };
      const up = () => { removeEventListener("pointermove", move); removeEventListener("pointerup", up); saveCur(true); };
      addEventListener("pointermove", move); addEventListener("pointerup", up);
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
function closeModal() { $("#modal").classList.remove("on"); }
$("#modal").onclick = e => { if (e.target.id === "modal") closeModal(); };

function panelMenu(p, x, y) {
  const ps = S.cur.panels, i = ps.indexOf(p);
  popMenu(x, y, [
    ["↻ 刷新", () => loadPanel(p, true)],
    ["✎ 编辑标题/参数", () => editPanel(p)],
    "-",
    ["宽度：整行", () => resize(p, 24)], ["宽度：半行", () => resize(p, 12)], ["宽度：1/3", () => resize(p, 8)],
    ["↑ 上移", () => { if (i > 0) { [ps[i - 1], ps[i]] = [ps[i], ps[i - 1]]; renderDash(); saveCur(true); } }],
    ["↓ 下移", () => { if (i < ps.length - 1) { [ps[i + 1], ps[i]] = [ps[i], ps[i + 1]]; renderDash(); saveCur(true); } }],
    "-",
    ["✕ 移除面板", () => { ps.splice(i, 1); renderDash(); saveCur(true); }],
  ]);
}
function resize(p, w) { p.w = w; renderDash(); saveCur(true); }

function editPanel(p) {
  const c = S.catalog[p.id];
  const ks = Object.keys(c.params || {});
  modal(`<h3>编辑面板 · ${esc(c.title)}</h3><div class="form">
    <label>标题</label><input id="f_title" value="${esc(p.title)}">
    ${ks.map(k => `<label>${esc(k)} — ${esc(c.params[k])}</label>
      ${k === "question" ? `<textarea id="f_${k}">${esc(p.params?.[k] ?? "")}</textarea>` :
        `<input id="f_${k}" value="${esc(Array.isArray(p.params?.[k]) ? p.params[k].join(",") : p.params?.[k] ?? "")}">`}`).join("")}
    <div class="row"><button class="btn" id="f_cancel">取消</button><button class="btn primary" id="f_ok">保存</button></div></div>`);
  $("#f_cancel").onclick = closeModal;
  $("#f_ok").onclick = () => {
    p.title = $("#f_title").value.trim() || c.title;
    p.params = {};
    ks.forEach(k => {
      let v = $("#f_" + k).value.trim();
      if (!v) return;
      if (k === "keywords") v = v.split(/[,，\s]+/).filter(Boolean);
      p.params[k] = v;
    });
    closeModal(); renderDash(); saveCur(true);
  };
}

function addPanel() {
  modal(`<h3>添加面板</h3><div class="cat-list">${S.meta.catalog.map(c =>
    `<div class="cat-item" data-id="${c.id}"><b>${esc(c.title)}</b><span>${esc(c.desc)}</span></div>`).join("")}</div>`);
  $("#modalCard").querySelectorAll(".cat-item").forEach(el => el.onclick = () => {
    const c = S.catalog[el.dataset.id];
    const p = {uid: Math.random().toString(36).slice(2, 10), id: c.id, title: c.title, w: c.w, h: c.h, params: {}};
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
  } catch (e) { $("#drawerBody").innerHTML = `<div class="err">${esc(e.message)}</div>`; }
}
$("#drawerClose").onclick = () => $("#drawer").classList.remove("on");

// ------------------------------------------------------------------ AI 生成
async function generate(prompt) {
  prompt = (prompt || $("#prompt").value).trim();
  if (!prompt) { $("#prompt").focus(); return; }
  const b = $("#btnGen"); b.disabled = true; b.textContent = "生成中…";
  try {
    const d = await post("/api/ai/build", {prompt});
    S.dashes.push(d); S.cur = structuredClone(d);
    LS.set("dash", d.id);
    renderDash();
    toast(d.how === "ai" ? `AI 已生成「${d.title}」，可拖动调整` :
      `已按关键词生成「${d.title}」${d.error ? "（AI 调用失败：" + d.error.slice(0, 60) + "）" : "（AI 未配置）"}`, 4000);
    $("#prompt").value = "";
  } catch (e) { toast("生成失败：" + e.message, 4000); }
  finally { b.disabled = false; b.textContent = "生成看板"; }
}

// ------------------------------------------------------------------ 实时状态
async function pollLive() {
  try {
    const s = await api("/api/live");
    const el = $("#live");
    const today = s.now.slice(0, 10);
    el.className = "live " + (s.session && s.enabled ? "on" : S.date !== today ? "stale" : "");
    const last = s.last.limit || s.last.news || "";
    $("span", el).textContent = s.session && s.enabled ? `盘中实时 · ${last}` :
      S.date !== today ? `数据日 ${S.date}` : `已收盘 · 快讯 ${s.last.news || "—"}`;
    el.title = Object.entries(s.last).map(([k, v]) => `${k} ${v}`).join("\n") +
      (Object.keys(s.errors).length ? "\n错误：\n" + Object.values(s.errors).join("\n") : "");
  } catch {}
}

function schedule() {
  S.timers.forEach(clearInterval); S.timers = [];
  const sec = +$("#refresh").value;
  LS.set("refresh", sec);
  if (sec) S.timers.push(setInterval(() => refreshAll(false), sec * 1000));
  S.timers.push(setInterval(pollLive, 15000));
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
    if (!confirm("删除这个看板？")) return;
    api("/api/dashboards?id=" + encodeURIComponent(t.dataset.del), {method: "DELETE"}).then(() => {
      S.dashes = S.dashes.filter(d => d.id !== t.dataset.del);
      if (S.cur.id === t.dataset.del) openDash(S.dashes[0].id); else renderTabs();
    });
    return;
  }
  const tab = t.closest(".tab");
  if (tab) openDash(tab.dataset.id);
});

$("#btnGen").onclick = () => generate();
$("#prompt").onkeydown = e => { if (e.key === "Enter" && !e.isComposing) generate(); };
$("#btnAdd").onclick = addPanel;
$("#btnEdit").onclick = () => {
  S.edit = !S.edit; $("#btnEdit").classList.toggle("on", S.edit);
  $("#btnEdit").textContent = S.edit ? "完成" : "编辑";
  renderDash();
};
$("#btnMore").onclick = e => popMenu(e.clientX - 150, e.clientY + 14, [
  ["重命名当前看板", () => { const n = prompt("看板名称", S.cur.title); if (n) { S.cur.title = n; saveCur(); } }],
  ["另存为新看板", () => { S.cur = {...structuredClone(S.cur), id: "d" + Date.now().toString(36), builtin: false,
      title: S.cur.title + " 副本"}; saveCur(); }],
  ["恢复默认看板", async () => { if (!confirm("恢复默认会删除所有自定义看板，确定？")) return;
      S.dashes = (await post("/api/dashboards/reset", {})).dashboards; openDash(S.dashes[0].id); }],
  "-",
  ["旧版看板", () => open("/legacy", "_blank")],
]);
$("#date").onchange = () => { S.date = $("#date").value; renderDash(); pollLive(); };
$("#refresh").onchange = schedule;
addEventListener("resize", () => S.charts.forEach(c => c.resize()));
addEventListener("keydown", e => { if (e.key === "Escape") { closeModal(); $("#drawer").classList.remove("on"); } });

// ------------------------------------------------------------------ 启动
(async () => {
  S.meta = await api("/api/meta");
  S.meta.catalog.forEach(c => S.catalog[c.id] = c);
  S.dashes = S.meta.dashboards;
  S.date = S.meta.latest;
  $("#date").innerHTML = S.meta.dates.map(d => `<option>${d}</option>`).join("");
  $("#refresh").value = String(LS.get("refresh", 60));
  const ex = ["明天重点关注哪些题材和龙头", "盯机器人这条线：龙头梯队、相关快讯、明天怎么做",
    "情绪周期和赚钱效应，现在适合打板吗", "盘中实时：涨停、炸板、快讯", "今天的催化事件和对应个股"];
  $("#examples").innerHTML = ex.map(x => `<span class="chip">${esc(x)}</span>`).join("") +
    (S.meta.llm ? "" : '<span class="chip" style="color:var(--warn)">AI 未配置：按关键词生成（运行 run.py llm-setup 开启 AI）</span>');
  $("#examples").querySelectorAll(".chip").forEach((c, i) => { if (i < ex.length) c.onclick = () => generate(ex[i]); });
  openDash(LS.get("dash", null) || S.dashes[0].id);
  pollLive(); schedule();
})().catch(e => { $("#grid").innerHTML = `<div class="err" style="grid-column:span 24">启动失败：${esc(e.message)}</div>`; });
})();
