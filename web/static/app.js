"use strict";
const NAV = [
  ["overview", "◈ Overview", "1"],
  ["fleet", "⊳ Fleet", "2"],
  ["contracts", "§ Contracts", "3"],
  ["markets", "$ Markets", "4"],
  ["map", "✦ Map", "5"],
  ["automation", "⚙ Automation", "6"],
  ["analytics", "📈 Analytics", "7"],
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

function tip(html, x, y) {
  let t = $("#chart-tip");
  if (!t) { t = el("div"); t.id = "chart-tip"; document.body.appendChild(t); }
  if (html == null) { t.style.display = "none"; return; }
  t.innerHTML = html; t.style.display = "block";
  t.style.left = (x + 14) + "px"; t.style.top = (y + 12) + "px";
}
// series: [{name,color,points:[{t,v}]}]. Lines over a shared time/value domain
// with a hover crosshair + tooltip.
function interactiveChart(cv, series, h, yfmt) {
  yfmt = yfmt || fmt;
  const all = series.flatMap(s => s.points).filter(p => p && p.v != null);
  const draw = (hoverPx) => {
    const { ctx, w, h: H } = sizeCanvas(cv, h);
    ctx.clearRect(0, 0, w, H);
    if (all.length < 2) { ctx.fillStyle = C.muted; ctx.font = "12px monospace"; ctx.fillText("no data yet", 8, 20); return; }
    const ts = all.map(p => p.t), vs = all.map(p => p.v);
    const t0 = Math.min(...ts), t1 = Math.max(...ts), tspan = (t1 - t0) || 1;
    const lo = Math.min(...vs), hi = Math.max(...vs), vspan = (hi - lo) || 1;
    const pad = 8, X = t => pad + (t - t0) / tspan * (w - 2 * pad), Y = v => H - pad - (v - lo) / vspan * (H - 2 * pad);
    ctx.fillStyle = C.muted; ctx.font = "11px monospace";
    ctx.fillText(yfmt(hi), 4, 12); ctx.fillText(yfmt(lo), 4, H - 4);
    for (const s of series) {
      const pts = s.points.filter(p => p.v != null);
      if (pts.length < 2) continue;
      ctx.beginPath(); pts.forEach((p, i) => { const x = X(p.t), y = Y(p.v); i ? ctx.lineTo(x, y) : ctx.moveTo(x, y); });
      ctx.strokeStyle = s.color; ctx.lineWidth = 2; ctx.stroke();
    }
    if (hoverPx != null) {
      const th = t0 + (hoverPx - pad) / (w - 2 * pad) * tspan;
      ctx.strokeStyle = C.border; ctx.lineWidth = 1;
      ctx.beginPath(); ctx.moveTo(hoverPx, pad); ctx.lineTo(hoverPx, H - pad); ctx.stroke();
      let rows = "";
      for (const s of series) {
        const pts = s.points.filter(p => p.v != null); if (!pts.length) continue;
        let best = pts[0]; for (const p of pts) if (Math.abs(p.t - th) < Math.abs(best.t - th)) best = p;
        ctx.fillStyle = s.color; ctx.beginPath(); ctx.arc(X(best.t), Y(best.v), 3, 0, 7); ctx.fill();
        rows += `<div style="color:${s.color}">${s.name ? s.name + " " : ""}${yfmt(best.v)}</div>`;
        cv._when = best.t;
      }
      return { rows };
    }
  };
  draw();
  cv.onmousemove = (e) => {
    const r = cv.getBoundingClientRect();
    const info = draw(e.clientX - r.left);
    const when = cv._when ? new Date(cv._when * 1000).toLocaleString() : "";
    tip((info ? info.rows : "") + `<div class="dim">${when}</div>`, e.clientX, e.clientY);
  };
  cv.onmouseleave = () => { draw(); tip(null); };
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
  if (focusShip) {
    const ship = ships.find(s => s.symbol === focusShip);
    if (ship) m.appendChild(fleetDetail(ship));
    m.appendChild(fleetActions(focusShip));
  }
}
function fleetDetail(ship) {
  const p = el("div", "panel"), nav = ship.nav || {}, cargo = ship.cargo || {}, fuel = ship.fuel || {};
  const inv = cargo.inventory || [];
  const mounts = (ship.mounts || []).map(m => m.symbol || m.name).join(", ") || "—";
  let routeLine = "";
  if (nav.status === "IN_TRANSIT" && nav.route) {
    const secs = Math.max(0, Math.round((Date.parse(nav.route.arrival) - Date.now()) / 1000));
    routeLine = `<div class="muted">→ ${(nav.route.destination || {}).symbol || ""} · ETA ${secs}s</div>`;
  }
  p.innerHTML = `<div class="phead">${ship.symbol} · DETAIL</div>
    <div class="muted">${(ship.frame || {}).name || "?"} · ${((ship.registration || {}).role || "").toLowerCase()} · ${nav.flightMode || ""}</div>
    <div class="muted">${nav.status || ""} @ ${nav.waypointSymbol || ""}</div>${routeLine}
    ${gauge("FUEL", fuel.current, fuel.capacity, C.cyan)}${gauge("CARGO", cargo.units, cargo.capacity, C.gold)}
    <div class="dim" style="margin-top:6px">mounts: ${mounts}</div>`;
  if (inv.length) {
    const t = el("table"); t.style.marginTop = "8px";
    t.innerHTML = `<tr><th>Cargo</th><th>Units</th></tr>` + inv.map(i => `<tr><td>${i.symbol}</td><td>${i.units}</td></tr>`).join("");
    p.appendChild(t);
  }
  return p;
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

/* ---------- system map ---------- */
let mapData = {};      // system -> {waypoints, links}
let mapWpPos = [];     // hit-test cache: [{p, sx, sy, r}]

function currentSystem() {
  if (focusShip) { const s = (state.ships || []).find(x => x.symbol === focusShip); if (s) return s.nav && s.nav.systemSymbol; }
  const s0 = (state.ships || [])[0]; if (s0 && s0.nav) return s0.nav.systemSymbol;
  const hq = state.hq || (state.agent || {}).headquarters;
  return hq ? hq.split("-").slice(0, 2).join("-") : null;
}
function wpStyle(p) {
  const t = p.type || "";
  if (t.indexOf("ASTEROID") >= 0) return { color: C.muted, r: 4 };
  if (t === "JUMP_GATE") return { color: C.cyan, r: 6 };
  if (t === "GAS_GIANT") return { color: C.gold, r: 9 };
  if (t === "PLANET") return { color: C.green, r: 8 };
  if (t === "MOON") return { color: "#7fd8c8", r: 4 };
  if (t.indexOf("STATION") >= 0) return { color: C.pink, r: 6 };
  if (t === "FUEL_STATION") return { color: C.gold, r: 5 };
  return { color: C.muted, r: 5 };
}
function transitFrac(route) {
  try {
    const dep = Date.parse(route.departureTime), arr = Date.parse(route.arrival), now = Date.now();
    if (!dep || !arr || arr <= dep) return 1;
    return Math.max(0, Math.min(1, (now - dep) / (arr - dep)));
  } catch (e) { return 1; }
}
async function vMap() {
  const m = $("#main");
  const sys = currentSystem();
  m.innerHTML = head("✦ SYSTEM MAP", sys ? `${sys} · hover to inspect, click to send the focused ship` : "");
  if (!sys) { m.innerHTML += `<p class="muted">no known system yet</p>`; return; }
  const panel = el("div", "panel"); panel.style.position = "relative";
  const cv = el("canvas"); cv.id = "mapcanvas"; cv.dataset.h = "520"; panel.appendChild(cv);
  const pop = el("div"); pop.id = "map-pop"; pop.style.display = "none"; panel.appendChild(pop);
  m.appendChild(panel);
  m.appendChild(mapLegend());
  if (!mapData[sys]) {
    try { mapData[sys] = await getJSON("/api/system/" + encodeURIComponent(sys)); }
    catch (e) { mapData[sys] = { waypoints: [], links: [] }; }
  }
  // a re-render (SSE state) during the await may have replaced the canvas —
  // draw on the live one, and bail if we've navigated away.
  if (view !== "map") return;
  const cvNow = $("#mapcanvas"), popNow = $("#map-pop");
  if (cvNow) drawMap(cvNow, popNow, sys);
}
function mapLegend() {
  const items = [["planet", C.green], ["moon", "#7fd8c8"], ["gas giant", C.gold], ["asteroid", C.muted], ["jump gate", C.cyan], ["station", C.pink], ["ship", C.pink]];
  const p = el("div", "panel");
  p.innerHTML = `<div class="phead">LEGEND</div><div class="row" style="gap:18px;font-size:12px">` +
    items.map(([n, c]) => `<span style="color:${c}">●&nbsp;<span class="muted">${n}</span></span>`).join("") + `</div>`;
  return p;
}
function drawMap(cv, pop, sys) {
  const wps = (mapData[sys] || {}).waypoints || [];
  const H = 520, { ctx, w } = sizeCanvas(cv, H);
  ctx.clearRect(0, 0, w, H);
  if (!wps.length) { ctx.fillStyle = C.muted; ctx.font = "12px monospace"; ctx.fillText("no waypoints", 10, 24); return; }
  const xs = wps.map(p => p.x), ys = wps.map(p => p.y);
  const minx = Math.min(...xs), maxx = Math.max(...xs), miny = Math.min(...ys), maxy = Math.max(...ys);
  const spanx = (maxx - minx) || 1, spany = (maxy - miny) || 1, pad = 44;
  const SX = x => pad + (x - minx) / spanx * (w - 2 * pad);
  const SY = y => pad + (y - miny) / spany * (H - 2 * pad);
  ctx.fillStyle = C.gold; ctx.beginPath(); ctx.arc(SX((minx + maxx) / 2), SY((miny + maxy) / 2), 4, 0, 7); ctx.fill();
  mapWpPos = [];
  for (const p of wps) {
    const sx = SX(p.x), sy = SY(p.y), st = wpStyle(p);
    ctx.fillStyle = st.color; ctx.beginPath(); ctx.arc(sx, sy, st.r, 0, 7); ctx.fill();
    if ((p.traits || []).some(t => t === "MARKETPLACE" || t === "SHIPYARD")) {
      ctx.strokeStyle = C.cyan; ctx.lineWidth = 1; ctx.beginPath(); ctx.arc(sx, sy, st.r + 3, 0, 7); ctx.stroke();
    }
    mapWpPos.push({ p, sx, sy, r: st.r + 4 });
  }
  for (const s of state.ships || []) {
    const nav = s.nav || {}; if (nav.systemSymbol !== sys) continue;
    let sx, sy;
    if (nav.status === "IN_TRANSIT" && nav.route) {
      const o = nav.route.origin || {}, d = nav.route.destination || {}, f = transitFrac(nav.route);
      ctx.strokeStyle = C.gold; ctx.globalAlpha = 0.4; ctx.setLineDash([4, 4]);
      ctx.beginPath(); ctx.moveTo(SX(o.x), SY(o.y)); ctx.lineTo(SX(d.x), SY(d.y)); ctx.stroke();
      ctx.setLineDash([]); ctx.globalAlpha = 1;
      sx = SX(o.x + (d.x - o.x) * f); sy = SY(o.y + (d.y - o.y) * f);
    } else {
      const wp = wps.find(x => x.symbol === nav.waypointSymbol); if (!wp) continue; sx = SX(wp.x); sy = SY(wp.y);
    }
    ctx.fillStyle = C.pink; ctx.beginPath(); ctx.moveTo(sx, sy - 6); ctx.lineTo(sx + 5, sy + 5); ctx.lineTo(sx - 5, sy + 5); ctx.closePath(); ctx.fill();
    ctx.fillStyle = C.text; ctx.font = "10px monospace"; ctx.fillText(s.symbol.split("-").pop(), sx + 8, sy + 3);
  }
  cv.onmousemove = (e) => {
    const r = cv.getBoundingClientRect(), mx = e.clientX - r.left, my = e.clientY - r.top;
    const hit = mapWpPos.find(o => Math.hypot(o.sx - mx, o.sy - my) <= o.r + 3);
    cv.style.cursor = hit ? "pointer" : "default";
    hit ? tip(`<b>${hit.p.symbol}</b><div class="dim">${hit.p.type}</div>`, e.clientX, e.clientY) : tip(null);
  };
  cv.onmouseleave = () => tip(null);
  cv.onclick = (e) => {
    const r = cv.getBoundingClientRect(), mx = e.clientX - r.left, my = e.clientY - r.top;
    const hit = mapWpPos.find(o => Math.hypot(o.sx - mx, o.sy - my) <= o.r + 3);
    if (hit) showWpPopup(pop, hit); else pop.style.display = "none";
  };
}
function showWpPopup(pop, hit) {
  const p = hit.p;
  pop.style.display = "block"; pop.style.left = Math.round(hit.sx + 10) + "px"; pop.style.top = Math.round(hit.sy + 10) + "px";
  pop.innerHTML = `<div class="phead">${p.symbol}</div><div class="muted">${p.type}</div><div class="dim" style="font-size:11px;max-width:220px">${(p.traits || []).join(", ") || "—"}</div>`;
  if (focusShip) {
    const b = el("button", "btn primary", `Send ${focusShip} here`); b.style.marginTop = "8px";
    b.onclick = async () => { await postJSON("/api/fleet", { ship: focusShip, action: "navigate", waypoint: p.symbol }); pop.style.display = "none"; poll(); };
    pop.appendChild(b);
  } else {
    const d = el("div", "dim"); d.style.marginTop = "6px"; d.textContent = "focus a ship in Fleet to send it here";
    pop.appendChild(d);
  }
}

function vAutomation() {
  const m = $("#main");
  const orch = state.orchestrator || {};
  m.innerHTML = head("⚙ AUTOMATION", "orchestrate the fleet · per-ship bots · live log");
  const bar = el("div", "panel"); bar.innerHTML = `<div class="phead">FLEET ORCHESTRATOR</div>`;
  if (orch.running) {
    const cfg = orch.config || {};
    const row = el("div", "row");
    const btn = el("button", "btn danger", "■ Stop Orchestrator");
    btn.onclick = async () => { await postJSON("/api/orchestrator", { action: "stop" }); poll(); };
    const bits = [`${Object.keys(orch.roster || {}).length} deployed`,
      cfg.expand ? `reinvest ${cfg.expand}` : "no reinvest",
      `reserve ${fmt(cfg.credit_buffer)}c`,
      cfg.max_ships ? `cap ${cfg.max_ships}` : null,
      cfg.cross_system ? "cross-system" : null,
      cfg.auto_contracts ? "auto-contracts" : null].filter(Boolean);
    row.appendChild(btn); row.appendChild(el("span", "muted", "running · " + bits.join(" · ")));
    bar.appendChild(row);
  } else {
    const cfg = el("div", "row"); cfg.style.marginBottom = "8px";
    cfg.innerHTML = `
      <input id="o-expand" placeholder="reinvest ship type — blank = off" style="width:280px">
      <input id="o-buffer" type="number" placeholder="reserve" value="100000" style="width:120px">
      <input id="o-max" type="number" placeholder="max ships" style="width:110px">
      <label class="muted"><input id="o-cross" type="checkbox"> cross-system</label>
      <label class="muted"><input id="o-contracts" type="checkbox"> auto-contracts</label>`;
    bar.appendChild(cfg);
    const row = el("div", "row");
    const btn = el("button", "btn primary", "🚀 Orchestrate Fleet");
    btn.onclick = async () => {
      await postJSON("/api/orchestrator", {
        action: "start",
        expand: $("#o-expand").value.trim().toUpperCase(),
        credit_buffer: $("#o-buffer").value,
        max_ships: $("#o-max").value,
        cross_system: $("#o-cross").checked,
        auto_contracts: $("#o-contracts").checked,
      });
      poll();
    };
    row.appendChild(btn); row.appendChild(el("span", "muted", "idle — deploys a bot per ship; reinvests profit if a ship type is set"));
    bar.appendChild(row);
  }
  m.appendChild(bar);

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
   <div class="panel"><div class="phead">GOODS WATCHLIST</div><div class="dim" style="font-size:12px;margin-bottom:6px">click a good for its price history</div><div id="watch"></div></div>
   <div class="panel" id="drill" style="display:none"><div class="phead" id="drill-h"></div><canvas id="drillcv" data-h="200"></canvas></div>
   <div class="panel"><div class="phead">REALIZED P&L BY GOOD</div><div id="pnlbars"></div></div>`;
  interactiveChart($("#cw"), [{ name: "", color: C.green, points: stats.credits }], 180, fmt);
  // watchlist with sparklines
  const w = $("#watch");
  const tbl = el("table"); tbl.innerHTML = `<tr><th>Good</th><th>Sell</th><th>Δ</th><th>Trend</th></tr>`;
  for (const it of stats.watchlist || []) {
    const tr = el("tr"); tr.style.cursor = "pointer";
    tr.innerHTML = `<td>${it.good}</td><td>${fmt(it.last)}</td><td class="${it.delta >= 0 ? "pos" : "neg"}">${it.delta >= 0 ? "+" : ""}${fmt(it.delta)}</td>`;
    const td = el("td"); const cv = el("canvas", "spark"); td.appendChild(cv); tr.appendChild(td); tbl.appendChild(tr);
    tr.onclick = () => showPriceHistory(it.good);
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

async function showPriceHistory(good) {
  const d = $("#drill"), h = $("#drill-h"), cv = $("#drillcv");
  if (!d) return;
  d.style.display = "block"; h.textContent = "PRICE HISTORY · " + good;
  const series = await getJSON("/api/price/" + encodeURIComponent(good));
  const buy = series.filter(r => r.purchase_price != null).map(r => ({ t: r.observed_at, v: r.purchase_price }));
  const sell = series.filter(r => r.sell_price != null).map(r => ({ t: r.observed_at, v: r.sell_price }));
  interactiveChart(cv, [{ name: "buy", color: C.gold, points: buy }, { name: "sell", color: C.green, points: sell }], 200, fmt);
  d.scrollIntoView({ behavior: "smooth", block: "nearest" });
}

let logBuf = [];
function pushLog(line) {
  logBuf.push(line); if (logBuf.length > 300) logBuf.shift();
  const box = $("#log"); if (!box) return;
  const d = el("div", "line"); d.innerHTML = `<span class="t">${line.t}</span>${line.msg}`;
  box.appendChild(d); box.scrollTop = box.scrollHeight;
}
function refreshLog() {
  const box = $("#log"); if (!box) return;
  box.innerHTML = logBuf.map(l => `<div class="line"><span class="t">${l.t}</span>${l.msg}</div>`).join("");
  box.scrollTop = box.scrollHeight;
}

function render() {
  if (!state) return;
  ({ overview: vOverview, fleet: vFleet, contracts: vContracts, markets: vMarkets,
     map: vMap, automation: vAutomation, analytics: vAnalytics }[view] || vOverview)();
}

/* ---------- setup screen (first run) ---------- */
function renderSetup() {
  $("#nav").innerHTML = "";
  const m = $("#main");
  m.innerHTML = `<h2 class="title">◈ WELCOME, COMMANDER</h2><p class="sub">let's get you flying — no .env editing needed</p><div id="setup-err" class="neg"></div>`;
  const err = (msg) => { $("#setup-err").textContent = msg || ""; };
  const submit = async (body) => {
    err("checking…");
    const r = await postJSON("/api/setup", body);
    if (r.ok) { boot(); } else { err(r.error || "setup failed"); }
  };
  const p1 = el("div", "panel");
  p1.innerHTML = `<div class="phead">I HAVE AN AGENT TOKEN</div>`;
  const r1 = el("div", "row");
  const t = el("input"); t.placeholder = "paste agent token"; t.style.width = "420px";
  const b1 = el("button", "btn primary", "Sign in");
  b1.onclick = () => submit({ mode: "token", token: t.value.trim() });
  r1.append(t, b1); p1.appendChild(r1); m.appendChild(p1);

  const p2 = el("div", "panel");
  p2.innerHTML = `<div class="phead">REGISTER A NEW AGENT</div><div class="muted" style="font-size:12px;margin-bottom:8px">needs an account token from spacetraders.io</div>`;
  const r2 = el("div", "row");
  const acc = el("input"); acc.placeholder = "account token"; acc.style.width = "300px";
  const call = el("input"); call.placeholder = "callsign (3-14)"; call.style.width = "150px";
  const fac = el("input"); fac.placeholder = "faction"; fac.value = "COSMIC"; fac.style.width = "120px";
  const b2 = el("button", "btn gold", "Register");
  b2.onclick = () => submit({ mode: "register", account_token: acc.value.trim(), callsign: call.value.trim(), faction: fac.value.trim() });
  r2.append(acc, call, fac, b2); p2.appendChild(r2); m.appendChild(p2);
}

/* ---------- live updates (SSE, with polling fallback) ---------- */
function applyState(s) {
  state = s;
  if (s && s.configured === false) { renderSetup(); return; }
  updateChrome(); render();
}
async function poll() {
  try { applyState(await getJSON("/api/state")); } catch (e) { /* keep last */ }
}
let pollTimer = null, es = null;
function startPolling() { if (!pollTimer) { poll(); pollTimer = setInterval(poll, 4000); } }

function connect() {
  if (es) { try { es.close(); } catch (e) {} }
  try { es = new EventSource("/api/stream"); } catch (e) { startPolling(); return; }
  es.addEventListener("state", e => applyState(JSON.parse(e.data)));
  es.addEventListener("log", e => pushLog(JSON.parse(e.data)));
  es.onopen = () => { if (pollTimer) { clearInterval(pollTimer); pollTimer = null; } };
  es.onerror = () => { startPolling(); };
}

async function boot() {
  buildNav();
  const s = await getJSON("/api/state").catch(() => ({ configured: false }));
  applyState(s);
  if (s.configured === false) { if (es) { es.close(); es = null; } return; }
  try { logBuf = await getJSON("/api/log?limit=100"); } catch (e) { logBuf = []; }
  connect();
}
boot();
