"use strict";
const NAV = [
  ["overview", "◈ Overview", "1"],
  ["fleet", "⊳ Fleet", "2"],
  ["contracts", "§ Contracts", "3"],
  ["markets", "$ Markets", "4"],
  ["automation", "⚙ Automation", "5"],
  ["analytics", "📈 Analytics", "6"],
];
const C = { cyan: "#22d3ee", gold: "#fbbf24", green: "#34d399", pink: "#e879f9",
  danger: "#f87171", muted: "#8590b8", border: "#283358" };

let view = "overview";
let state = null;      // /api/state snapshot
let stats = null;      // /api/stats
let focusShip = null;

const $ = (s, r = document) => r.querySelector(s);
const el = (tag, cls, txt) => { const e = document.createElement(tag); if (cls) e.className = cls; if (txt != null) e.textContent = txt; return e; };
const fmt = (n) => (n == null ? "—" : Number(n).toLocaleString());

async function getJSON(u) { const r = await fetch(u); return r.json(); }
async function postJSON(u, body) {
  const r = await fetch(u, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body || {}) });
  return r.json();
}

/* ---------- nav + shell ---------- */
function buildNav() {
  const nav = $("#nav"); nav.innerHTML = "";
  for (const [id, label, key] of NAV) {
    const it = el("div", "nav-item" + (id === view ? " active" : ""));
    it.innerHTML = `<span class="label-text">${label}</span><span class="key">${key}</span>`;
    it.onclick = () => { view = id; buildNav(); render(); };
    nav.appendChild(it);
  }
}
document.addEventListener("keydown", (e) => {
  const hit = NAV.find(n => n[2] === e.key);
  if (hit && e.target.tagName !== "INPUT") { view = hit[0]; buildNav(); render(); }
});

function updateChrome() {
  if (!state) return;
  const a = state.agent || {};
  $("#mini-credits").textContent = fmt(a.credits);
  $("#mini-hq").textContent = a.headquarters || "—";
  $("#mini-ships").textContent = a.shipCount ?? (state.ships || []).length;
  $("#sb-agent").textContent = a.symbol || "—";
  $("#sb-credits").textContent = fmt(a.credits) + " c";
  $("#sb-hq").textContent = a.headquarters || "—";
  const nbots = Object.keys(state.bots || {}).length + Object.keys((state.orchestrator || {}).roster || {}).length;
  $("#sb-bots").textContent = "bots " + nbots;
  const poll = $("#sb-poll");
  if (state.poll_ok) { poll.textContent = "● live " + state.last_poll; poll.className = "green"; }
  else { poll.textContent = "✕ " + (state.poll_err || "error"); poll.className = ""; poll.style.color = C.danger; }
}
setInterval(() => { $("#sb-clock").textContent = new Date().toLocaleTimeString(); }, 1000);

/* ---------- charts (canvas, no deps) ---------- */
function sizeCanvas(cv, h) {
  const dpr = window.devicePixelRatio || 1;
  const w = cv.clientWidth || 600;
  cv.width = w * dpr; cv.height = h * dpr;
  cv.style.height = h + "px";
  const ctx = cv.getContext("2d"); ctx.scale(dpr, dpr);
  return { ctx, w, h };
}
function lineChart(cv, vals, color) {
  const { ctx, w, h } = sizeCanvas(cv, cv.dataset.h ? +cv.dataset.h : 160);
  ctx.clearRect(0, 0, w, h);
  if (!vals || vals.length < 2) { ctx.fillStyle = C.muted; ctx.fillText("no data yet", 8, 20); return; }
  const lo = Math.min(...vals), hi = Math.max(...vals), span = (hi - lo) || 1;
  const pad = 6, x = i => pad + i * (w - 2 * pad) / (vals.length - 1);
  const y = v => h - pad - (v - lo) / span * (h - 2 * pad);
  // area
  ctx.beginPath(); ctx.moveTo(x(0), y(vals[0]));
  vals.forEach((v, i) => ctx.lineTo(x(i), y(v)));
  ctx.lineTo(x(vals.length - 1), h - pad); ctx.lineTo(x(0), h - pad); ctx.closePath();
  ctx.fillStyle = color + "22"; ctx.fill();
  // line
  ctx.beginPath(); ctx.moveTo(x(0), y(vals[0]));
  vals.forEach((v, i) => ctx.lineTo(x(i), y(v)));
  ctx.strokeStyle = color; ctx.lineWidth = 2; ctx.stroke();
  ctx.fillStyle = C.muted; ctx.font = "11px monospace";
  ctx.fillText(fmt(hi), 4, 12); ctx.fillText(fmt(lo), 4, h - 4);
}
function sparkline(cv, vals, color) {
  const { ctx, w, h } = sizeCanvas(cv, 22);
  ctx.clearRect(0, 0, w, h);
  if (!vals || vals.length < 2) return;
  const lo = Math.min(...vals), hi = Math.max(...vals), span = (hi - lo) || 1;
  ctx.beginPath();
  vals.forEach((v, i) => { const px = i * w / (vals.length - 1), py = h - 2 - (v - lo) / span * (h - 4); i ? ctx.lineTo(px, py) : ctx.moveTo(px, py); });
  ctx.strokeStyle = color; ctx.lineWidth = 1.5; ctx.stroke();
}

/* ---------- views ---------- */
function head(title, sub) { return `<h2 class="title">${title}</h2><p class="sub">${sub}</p>`; }

function vOverview() {
  const a = state.agent || {}, ships = state.ships || [];
  const fuelCap = ships.reduce((s, x) => s + (x.fuel?.capacity || 0), 0);
  const fuelCur = ships.reduce((s, x) => s + (x.fuel?.current || 0), 0);
  const m = $("#main");
  m.innerHTML = head("◈ AGENT OVERVIEW", "live ledger · holdings · fleet status") +
    `<div class="grid stat-grid">
      ${stat("CREDITS", fmt(a.credits), "green")}
      ${stat("SHIPS", a.shipCount ?? ships.length, "gold")}
      ${stat("FACTION", a.startingFaction || "—", "pink")}
      ${stat("HQ", a.headquarters || "—")}
      ${stat("CONTRACTS", (state.contracts || []).length)}
      ${stat("AVG FUEL", fuelCap ? Math.round(fuelCur / fuelCap * 100) + "%" : "—")}
     </div>
     <div class="panel"><div class="phead">RECENT EVENTS</div><div id="log"></div></div>`;
  refreshLog();
}
const stat = (l, v, cls) => `<div class="stat ${cls || ""}"><div class="label">${l}</div><div class="value">${v}</div></div>`;

function vFleet() {
  const ships = state.ships || [];
  const m = $("#main");
  m.innerHTML = head("⊳ FLEET COMMAND", "select a ship, then act on it");
  const grid = el("div", "grid card-grid");
  for (const s of ships) {
    const nav = s.nav || {}, fuel = s.fuel || {}, cargo = s.cargo || {};
    const card = el("div", "ship" + (s.symbol === focusShip ? " sel" : ""));
    card.onclick = () => { focusShip = s.symbol; render(); };
    card.innerHTML =
      `<div class="top"><span class="name">${s.symbol}</span><span class="pill ${nav.status}">${nav.status || "—"}</span></div>
       <div class="frame">${s.frame?.name || "?"} · ${(s.registration?.role || "").toLowerCase()}</div>
       ${gauge("FUEL", fuel.current, fuel.capacity, C.cyan)}
       ${gauge("CARGO", cargo.units, cargo.capacity, C.gold)}
       <div class="dim" style="font-size:12px;margin-top:6px">@ ${nav.waypointSymbol || "—"}</div>`;
    grid.appendChild(card);
  }
  m.appendChild(grid);
  if (focusShip) m.appendChild(fleetActions(focusShip));
}
function gauge(label, cur, cap, color) {
  const pct = cap ? Math.round((cur || 0) / cap * 100) : 0;
  const cls = color === C.gold ? "gauge cargo" : "gauge";
  return `<div class="glabel"><span>${label}</span><span>${cur ?? 0}/${cap ?? 0}</span></div><div class="${cls}"><i style="width:${pct}%"></i></div>`;
}
function fleetActions(ship) {
  const p = el("div", "panel");
  p.innerHTML = `<div class="phead">ACTIONS · ${ship}</div>`;
  const row = el("div", "row");
  const acts = [["orbit", "Orbit"], ["dock", "Dock"], ["refuel", "Refuel"], ["extract", "Extract"], ["sell", "Sell All"]];
  for (const [k, lbl] of acts) { const b = el("button", "", lbl); b.onclick = async () => { await postJSON("/api/fleet", { ship, action: k }); poll(); }; row.appendChild(b); }
  const wp = el("input"); wp.placeholder = "waypoint e.g. X1-N85-B9";
  const go = el("button", "btn primary", "Go →");
  go.onclick = async () => { await postJSON("/api/fleet", { ship, action: "navigate", waypoint: wp.value.trim() }); poll(); };
  row.appendChild(wp); row.appendChild(go);
  p.appendChild(row); return p;
}

function vContracts() {
  const cs = state.contracts || [];
  const m = $("#main");
  m.innerHTML = head("§ CONTRACTS", "accept procurements · deliver · bank the payout");
  if (!cs.length) { m.innerHTML += `<p class="muted">no contracts</p>`; return; }
  for (const c of cs) {
    const p = el("div", "panel");
    const terms = c.terms || {}, pay = terms.payment || {};
    const status = c.fulfilled ? "DONE" : (c.accepted ? "ACCEPTED" : "PENDING");
    let deliv = (terms.deliver || []).map(d =>
      `<tr><td>${d.tradeSymbol}</td><td>${d.unitsFulfilled}/${d.unitsRequired}</td><td class="dim">→ ${d.destinationSymbol}</td></tr>`).join("");
    p.innerHTML = `<div class="phead">${c.factionSymbol} · ${c.type} · <span class="muted">${status}</span></div>
      <div class="muted">accept <span class="pos">${fmt(pay.onAccepted)}c</span> · fulfill <span class="pos">${fmt(pay.onFulfilled)}c</span></div>
      <table>${deliv}</table>`;
    if (!c.accepted && !c.fulfilled) {
      const b = el("button", "btn primary", "Accept"); b.style.marginTop = "8px";
      b.onclick = async () => { await postJSON("/api/contract", { id: c.id, action: "accept" }); poll(); };
      p.appendChild(b);
    }
    m.appendChild(p);
  }
}

async function vMarkets() {
  const m = $("#main");
  m.innerHTML = head("$ MARKETS", "best known arbitrage routes · look up a market");
  const deals = await getJSON("/api/deals?min_profit=1&limit=20");
  const p = el("div", "panel"); p.innerHTML = `<div class="phead">TOP ROUTES</div>`;
  p.innerHTML += `<table><tr><th>Good</th><th>Buy @</th><th>Sell @</th><th>Profit</th><th>Hops</th></tr>` +
    (deals.length ? deals.map(r => `<tr><td>${r.good}</td><td class="dim">${r.buy_wp}</td><td class="dim">${r.sell_wp}</td><td class="pos">+${fmt(r.profit)}</td><td>${r.hops || 0}</td></tr>`).join("")
      : `<tr><td colspan="5" class="muted">no routes yet — run a trader/scout to gather prices</td></tr>`) + `</table>`;
  m.appendChild(p);
  const look = el("div", "panel");
  look.innerHTML = `<div class="phead">MARKET LOOKUP</div>`;
  const row = el("div", "row");
  const inp = el("input"); inp.placeholder = "waypoint e.g. X1-N85-A1";
  const b = el("button", "btn", "Fetch");
  const out = el("div"); out.style.marginTop = "8px";
  b.onclick = async () => {
    const d = await getJSON("/api/market/" + encodeURIComponent(inp.value.trim()));
    if (d.error) { out.innerHTML = `<span class="neg">${d.error}</span>`; return; }
    const goods = d.tradeGoods || [];
    out.innerHTML = `<table><tr><th>Good</th><th>Buy</th><th>Sell</th><th>Supply</th></tr>` +
      goods.map(g => `<tr><td>${g.symbol}</td><td>${fmt(g.purchasePrice)}</td><td class="dim">${fmt(g.sellPrice)}</td><td class="muted">${g.supply || ""}</td></tr>`).join("") + `</table>`;
  };
  row.appendChild(inp); row.appendChild(b); look.appendChild(row); look.appendChild(out);
  m.appendChild(look);
}

function vAutomation() {
  const m = $("#main");
  const orch = state.orchestrator || {};
  m.innerHTML = head("⚙ AUTOMATION", "orchestrate the fleet · per-ship bots · live log");
  const bar = el("div", "panel"); bar.innerHTML = `<div class="phead">FLEET ORCHESTRATOR</div>`;
  const row = el("div", "row");
  const btn = el("button", "btn " + (orch.running ? "danger" : "primary"), orch.running ? "■ Stop Orchestrator" : "🚀 Orchestrate Fleet");
  btn.onclick = async () => { await postJSON("/api/orchestrator", { action: orch.running ? "stop" : "start" }); poll(); };
  const status = el("span", "muted", orch.running ? `running · ${Object.keys(orch.roster || {}).length} deployed` : "idle");
  row.appendChild(btn); row.appendChild(status); bar.appendChild(row); m.appendChild(bar);

  const grid = el("div", "grid card-grid");
  for (const s of state.ships || []) {
    const sym = s.symbol;
    const bot = (state.bots || {})[sym];
    const role = orch.roster?.[sym];
    const card = el("div", "ship");
    card.innerHTML = `<div class="top"><span class="name">${sym}</span>` +
      (role ? `<span class="pill run">${role}</span>` : bot ? `<span class="pill run">${bot.role}</span>` : `<span class="pill">idle</span>`) + `</div>`;
    const r = el("div", "row"); r.style.marginTop = "8px";
    if (bot) { const b = el("button", "btn danger", "Stop"); b.onclick = async () => { await postJSON("/api/bot", { ship: sym, kind: "stop" }); poll(); }; r.appendChild(b); }
    else for (const [k, lbl, cls] of [["mine", "Mine", "primary"], ["trade", "Trade", "gold"], ["scout", "Scout", ""]]) {
      const b = el("button", "btn " + cls, lbl); b.onclick = async () => { await postJSON("/api/bot", { ship: sym, kind: k }); poll(); }; r.appendChild(b);
    }
    card.appendChild(r); grid.appendChild(card);
  }
  m.appendChild(grid);
  const logp = el("div", "panel"); logp.innerHTML = `<div class="phead">BOT CONSOLE</div><div id="log"></div>`;
  m.appendChild(logp); refreshLog();
}

async function vAnalytics() {
  const m = $("#main");
  m.innerHTML = head("📈 ANALYTICS", "net worth · realized P&L · price trends");
  stats = await getJSON("/api/stats");
  const pnl = stats.pnl || {};
  m.innerHTML += `<div class="grid stat-grid">
    ${stat("NET WORTH", fmt((state.agent || {}).credits), "green")}
    ${stat("REALIZED NET", (pnl.net >= 0 ? "+" : "") + fmt(pnl.net), "gold")}
    ${stat("TRADES", pnl.trades || 0)}
   </div>
   <div class="panel"><div class="phead">NET WORTH OVER TIME</div><canvas id="cw" data-h="180"></canvas></div>
   <div class="panel"><div class="phead">GOODS WATCHLIST</div><div id="watch"></div></div>
   <div class="panel"><div class="phead">REALIZED P&L BY GOOD</div><div id="pnlbars"></div></div>`;
  lineChart($("#cw"), stats.credits, C.green);
  // watchlist with sparklines
  const w = $("#watch");
  const tbl = el("table"); tbl.innerHTML = `<tr><th>Good</th><th>Sell</th><th>Δ</th><th>Trend</th></tr>`;
  for (const it of stats.watchlist || []) {
    const tr = el("tr");
    tr.innerHTML = `<td>${it.good}</td><td>${fmt(it.last)}</td><td class="${it.delta >= 0 ? "pos" : "neg"}">${it.delta >= 0 ? "+" : ""}${fmt(it.delta)}</td>`;
    const td = el("td"); const cv = el("canvas", "spark"); td.appendChild(cv); tr.appendChild(td); tbl.appendChild(tr);
    setTimeout(() => sparkline(cv, it.spark, it.delta >= 0 ? C.green : C.danger), 0);
  }
  w.appendChild(tbl);
  // pnl bars
  const rows = stats.pnl_by_good || [];
  const peak = Math.max(1, ...rows.map(r => Math.abs(r.net)));
  const pb = $("#pnlbars");
  for (const r of rows) {
    const line = el("div", "row"); line.style.margin = "4px 0";
    line.innerHTML = `<span style="width:150px" class="dim">${r.symbol}</span>
      <span class="bar ${r.net < 0 ? "neg" : ""}" style="width:${Math.round(Math.abs(r.net) / peak * 220)}px"></span>
      <span class="${r.net >= 0 ? "pos" : "neg"}">${r.net >= 0 ? "+" : ""}${fmt(r.net)}c</span>`;
    pb.appendChild(line);
  }
  if (!rows.length) pb.innerHTML = `<span class="muted">no trades recorded yet</span>`;
}

async function refreshLog() {
  const box = $("#log"); if (!box) return;
  const lines = await getJSON("/api/log?limit=100");
  box.innerHTML = lines.map(l => `<div class="line"><span class="t">${l.t}</span>${l.msg}</div>`).join("");
  box.scrollTop = box.scrollHeight;
}

function render() {
  if (!state) return;
  ({ overview: vOverview, fleet: vFleet, contracts: vContracts, markets: vMarkets,
     automation: vAutomation, analytics: vAnalytics }[view] || vOverview)();
}

/* ---------- poll loop ---------- */
async function poll() {
  try { state = await getJSON("/api/state"); updateChrome(); render(); }
  catch (e) { /* keep last */ }
}
buildNav();
poll();
setInterval(poll, 4000);
