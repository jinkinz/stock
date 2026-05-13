// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------
let state = {};
const selected = { trading_mode: "paper", approval_mode: "manual" };

const money = (v) => `$${Number(v || 0).toFixed(2)}`;
const pct = (v) => `${Number(v || 0).toFixed(2)}%`;

function formatQty(qty) {
  const n = Number(qty);
  return n % 1 === 0 ? n.toFixed(0) : parseFloat(n.toFixed(6)).toString();
}

async function api(path, options = {}) {
  const r = await fetch(path, { headers: { "content-type": "application/json" }, ...options });
  if (!r.ok) throw new Error(await r.text());
  return r.json();
}

// ---------------------------------------------------------------------------
// Main render — called on every SSE "state" event
// ---------------------------------------------------------------------------
function render(data) {
  state = data;
  const { settings, portfolio, last_quotes: quotes = [], universe_source = "sample",
          signals = [], proposals, tick_paused = false,
          session_pnl = 0, session_start_at = null } = data;

  selected.trading_mode = settings.trading_mode;
  selected.approval_mode = settings.approval_mode;

  // Top bar
  document.querySelector("#marketLine").textContent =
    `${settings.trading_mode.toUpperCase()} · ${settings.approval_mode} approval · ${settings.markets.join(", ")}`;

  // Settings form sync
  document.querySelectorAll('input[name="markets"]').forEach(el => el.checked = settings.markets.includes(el.value));
  document.querySelector("#universe").value = (settings.universe || []).join(", ");
  document.querySelector("#universeSource").textContent = `Universe: ${universe_source}`;
  document.querySelector("#budget").value = settings.budget;
  document.querySelector("#duration_minutes").value = settings.duration_minutes;
  document.querySelector("#max_scan_symbols").value = settings.max_scan_symbols;
  document.querySelector("#max_loss").value = settings.max_loss;
  document.querySelector("#max_trade_value").value = settings.max_trade_value;
  document.querySelector("#strategy_enabled").checked = settings.strategy_enabled;
  document.querySelector("#auto_tick_enabled").checked = settings.auto_tick_enabled;
  document.querySelector("#tick_interval_seconds").value = settings.tick_interval_seconds;
  document.querySelector("#allow_live_trading").checked = settings.allow_live_trading;

  document.querySelectorAll(".segmented").forEach(group => {
    const name = group.dataset.name;
    group.querySelectorAll("button").forEach(btn =>
      btn.classList.toggle("active", btn.dataset.value === selected[name]));
  });

  // Metrics
  const equity = portfolio.cash + Object.values(portfolio.positions).reduce((s, p) => {
    return s + p.quantity * (portfolio.last_prices[p.symbol] || p.avg_cost);
  }, 0);
  document.querySelector("#cash").textContent = money(portfolio.cash);
  document.querySelector("#equity").textContent = money(equity);
  paintPnl("#realized", portfolio.realized_pnl);
  paintPnl("#unrealized", unrealizedPnl(portfolio));
  paintPnl("#sessionPnl", session_pnl);
  document.querySelector("#sessionStart").textContent = session_start_at
    ? new Date(session_start_at).toLocaleTimeString() : "—";
  document.querySelector("#scanned").textContent = quotes.length;
  document.querySelector("#lastTick").textContent = data.last_tick_at
    ? new Date(data.last_tick_at).toLocaleTimeString() : "Never";
  document.querySelector("#quoteSource").textContent = quotes.length ? (quotes[0].source || "—") : "—";

  // Pause button
  const pauseBtn = document.querySelector("#pauseButton");
  const pauseStatusEl = document.querySelector("#pauseStatus");
  const pauseMetricEl = document.querySelector("#pauseMetric");
  pauseBtn.textContent = tick_paused ? "Resume Auto-Tick" : "Stop Auto-Tick";
  pauseBtn.classList.toggle("paused", tick_paused);
  pauseStatusEl.textContent = tick_paused ? "Paused" : "Running";
  pauseMetricEl.classList.toggle("paused-indicator", tick_paused);

  renderQuotes(quotes);
  renderSignals(signals);
  renderPositions(portfolio);
  renderProposals(proposals || []);
  renderDiagnostics(signals);
}

function paintPnl(sel, value) {
  const el = document.querySelector(sel);
  if (!el) return;
  el.textContent = money(value);
  el.classList.toggle("gain", Number(value) > 0);
  el.classList.toggle("loss", Number(value) < 0);
}

function unrealizedPnl(portfolio) {
  return Object.values(portfolio.positions).reduce((s, p) => {
    return s + p.quantity * ((portfolio.last_prices[p.symbol] || p.avg_cost) - p.avg_cost);
  }, 0);
}

// ---------------------------------------------------------------------------
// Sub-panel renderers
// ---------------------------------------------------------------------------
function renderPositions(portfolio) {
  const el = document.querySelector("#positions");
  const positions = Object.values(portfolio.positions).filter(p => p.quantity > 0);
  if (!positions.length) { el.innerHTML = `<div class="empty">No positions</div>`; return; }
  el.innerHTML = positions.map(pos => {
    const last = portfolio.last_prices[pos.symbol] || pos.avg_cost;
    const pnl = pos.quantity * (last - pos.avg_cost);
    return `<div class="row">
      <div><strong>${pos.symbol}</strong><br>
        <span>${formatQty(pos.quantity)} shares · avg ${money(pos.avg_cost)} · last ${money(last)}</span></div>
      <strong class="${pnl >= 0 ? "gain" : "loss"}">${money(pnl)}</strong>
    </div>`;
  }).join("");
}

function renderQuotes(quotes) {
  const el = document.querySelector("#quote");
  if (!quotes.length) { el.innerHTML = `<div class="empty">No quotes yet</div>`; return; }
  el.innerHTML = quotes.slice(0, 12).map(q => `
    <div class="row">
      <div><strong>${q.symbol}</strong><br><span>${q.source} · ${new Date(q.timestamp).toLocaleTimeString()}</span></div>
      <strong>${money(q.price)}</strong>
    </div>`).join("");
}

function renderSignals(signals) {
  const el = document.querySelector("#signals");
  if (!signals.length) { el.innerHTML = `<div class="empty">No scan yet</div>`; return; }
  el.innerHTML = signals.map(s => `
    <div class="row">
      <div><span class="tag ${s.action}">${s.action}</span>
        <strong> ${s.symbol} · ${(s.score * 100).toFixed(0)}%</strong>
        <br><span>${s.reason}</span></div>
      <strong>${money(s.price)}</strong>
    </div>`).join("");
}

function renderProposals(proposals) {
  const el = document.querySelector("#proposals");
  if (!proposals.length) { el.innerHTML = `<div class="empty">No proposals yet</div>`; return; }
  el.innerHTML = [...proposals].reverse().map(p => {
    const pending = p.status === "proposed";
    return `<div class="row">
      <div><span class="tag ${p.side}">${p.side}</span>
        <strong> ${formatQty(p.quantity)} ${p.symbol} @ ${money(p.price)}</strong>
        <br><span>${p.reason}</span>
        ${p.error ? `<br><span class="loss">${p.error}</span>` : ""}
      </div>
      <div>
        <span class="tag">${p.status}</span>
        ${pending ? `<div class="actions">
          <button class="approve" data-action="approve" data-id="${p.id}">Approve</button>
          <button class="reject" data-action="reject" data-id="${p.id}">Reject</button>
        </div>` : ""}
      </div>
    </div>`;
  }).join("");
}

function renderDiagnostics(signals) {
  const el = document.querySelector("#diagnostics");
  const withDiag = signals.filter(s => s.diagnostics);
  if (!withDiag.length) { el.innerHTML = `<div class="empty">No diagnostics yet</div>`; return; }
  el.innerHTML = `<table class="diag-table">
    <thead><tr><th>Symbol</th><th>Price</th><th>Volatility</th><th>Spread</th><th>Trend</th><th>Vol spike</th><th>News</th></tr></thead>
    <tbody>${withDiag.map(s => {
      const d = s.diagnostics;
      return `<tr>
        <td><strong>${d.symbol}</strong></td>
        <td>${money(d.price)}</td>
        <td><span class="diag-badge ${d.volatility > 40 ? "warn" : "ok"}">${d.volatility.toFixed(1)}%</span></td>
        <td>${(d.spread_pct * 100).toFixed(2)}%</td>
        <td>${d.trend_strength.toFixed(2)}%</td>
        <td><span class="diag-badge ${d.volume_spike ? "spike" : "ok"}">${d.volume_spike ? "SPIKE" : "normal"}</span></td>
        <td><span class="diag-badge ${d.news_gate ? "ok" : "warn"}">${d.news_gate ? "open" : "blocked"}</span></td>
      </tr>`;
    }).join("")}</tbody>
  </table>`;
}

// ---------------------------------------------------------------------------
// Audit log — driven by live SSE "audit" events, no refresh needed
// ---------------------------------------------------------------------------
const _auditBuffer = [];
const MAX_AUDIT_DISPLAY = 80;

function pushAuditEntry(entry) {
  _auditBuffer.unshift(entry);  // newest first
  if (_auditBuffer.length > MAX_AUDIT_DISPLAY) _auditBuffer.length = MAX_AUDIT_DISPLAY;
  renderAuditLog(_auditBuffer);
}

function renderAuditLog(entries) {
  const el = document.querySelector("#auditLog");
  if (!entries.length) { el.innerHTML = `<div class="empty">No audit entries yet</div>`; return; }
  el.innerHTML = entries.map(e => {
    const time = e.at ? new Date(e.at).toLocaleTimeString() : "—";
    const detail = e.detail ? Object.entries(e.detail).map(([k, v]) => `${k}: ${v}`).join(" · ") : "";
    const sym = e.symbol ? `<span class="audit-symbol">${e.symbol}</span> ` : "";
    return `<div class="audit-entry">
      <span class="audit-time">${time}</span>
      <span class="tag ${e.event}">${e.event}</span>
      <span class="audit-detail">${sym}${detail}</span>
    </div>`;
  }).join("");
}

// ---------------------------------------------------------------------------
// Sessions panel
// ---------------------------------------------------------------------------
async function loadSessions() {
  try {
    const data = await api("/api/sessions");
    renderSessions(data.entries || []);
  } catch {}
}

function renderSessions(entries) {
  const el = document.querySelector("#sessionsBody");
  if (!el) return;
  if (!entries.length) { el.innerHTML = `<tr><td colspan="5" class="empty" style="padding:12px">No sessions yet</td></tr>`; return; }
  el.innerHTML = [...entries].reverse().slice(0, 30).map(s => {
    const pnl = s.session_pnl || 0;
    const start = s.session_start ? new Date(s.session_start).toLocaleString() : "—";
    const end = s.session_end ? new Date(s.session_end).toLocaleString() : "—";
    return `<tr>
      <td>${start}</td>
      <td>${end}</td>
      <td>${money(s.start_equity)}</td>
      <td>${money(s.end_equity)}</td>
      <td class="${pnl >= 0 ? "gain" : "loss"}">${money(pnl)}</td>
    </tr>`;
  }).join("");
}

// ---------------------------------------------------------------------------
// Backtest
// ---------------------------------------------------------------------------
async function runBacktest() {
  const symbols = state.settings?.universe?.length ? state.settings.universe : null;
  const payload = { ticks: 60, starting_cash: 10000 };
  if (symbols) payload.symbols = symbols;
  const result = await api("/api/backtest", { method: "POST", body: JSON.stringify(payload) });
  renderBacktest(result);
}

function renderBacktest(result) {
  const panel = document.querySelector("#backtestPanel");
  panel.style.display = "block";
  const pnl = result.final_equity - result.starting_cash;
  const curve = (result.equity_curve || []).map(p => p.equity);
  const minE = Math.min(...curve), maxE = Math.max(...curve), range = maxE - minE || 1;
  const W = 600, H = 100;
  const pts = curve.map((e, i) => `${(i / (curve.length - 1)) * W},${H - ((e - minE) / range) * (H - 10) - 5}`).join(" ");
  document.querySelector("#backtestResult").innerHTML = `
    <div class="bt-summary">
      <div class="bt-stat"><span>Start</span><strong>${money(result.starting_cash)}</strong></div>
      <div class="bt-stat"><span>Final</span><strong class="${pnl >= 0 ? "gain" : "loss"}">${money(result.final_equity)}</strong></div>
      <div class="bt-stat"><span>P&L</span><strong class="${pnl >= 0 ? "gain" : "loss"}">${money(pnl)}</strong></div>
      <div class="bt-stat"><span>Trades</span><strong>${result.total_trades}</strong></div>
    </div>
    <svg class="equity-chart" viewBox="0 0 ${W} ${H}" xmlns="http://www.w3.org/2000/svg">
      <polyline points="${pts}" fill="none" stroke="#146c5c" stroke-width="2"/>
    </svg>
    <p class="hint" style="margin-top:8px">${result.ticks} ticks · ${result.symbols.join(", ")} · ${new Date(result.ran_at).toLocaleTimeString()}</p>`;
}

// ---------------------------------------------------------------------------
// Event wiring
// ---------------------------------------------------------------------------
document.querySelectorAll(".segmented button").forEach(btn => {
  btn.addEventListener("click", () => {
    const name = btn.closest(".segmented").dataset.name;
    selected[name] = btn.dataset.value;
    render({ ...state, settings: { ...state.settings, [name]: btn.dataset.value } });
  });
});

document.querySelector("#settingsForm").addEventListener("submit", async e => {
  e.preventDefault();
  const form = new FormData(e.currentTarget);
  const universe = String(form.get("universe") || "").split(/[\s,]+/).map(s => s.trim().toUpperCase()).filter(Boolean);
  render(await api("/api/settings", { method: "POST", body: JSON.stringify({
    markets: form.getAll("markets"), universe,
    budget: Number(form.get("budget")),
    duration_minutes: Number(form.get("duration_minutes")),
    max_scan_symbols: Number(form.get("max_scan_symbols")),
    max_loss: Number(form.get("max_loss")),
    max_trade_value: Number(form.get("max_trade_value")),
    trading_mode: selected.trading_mode, approval_mode: selected.approval_mode,
    strategy_enabled: document.querySelector("#strategy_enabled").checked,
    auto_tick_enabled: document.querySelector("#auto_tick_enabled").checked,
    tick_interval_seconds: Number(form.get("tick_interval_seconds")),
    allow_live_trading: document.querySelector("#allow_live_trading").checked,
  })}));
});

document.querySelector("#tickButton").addEventListener("click", async () => {
  render(await api("/api/tick"));
});

document.querySelector("#pauseButton").addEventListener("click", async () => {
  const endpoint = state.tick_paused ? "/api/tick/resume" : "/api/tick/pause";
  render(await api(endpoint, { method: "POST" }));
});

document.querySelector("#backtestButton").addEventListener("click", async () => {
  document.querySelector("#backtestResult").innerHTML = `<div class="empty">Running…</div>`;
  document.querySelector("#backtestPanel").style.display = "block";
  await runBacktest();
});

document.querySelector("#resetPaperButton").addEventListener("click", async () => {
  if (!confirm("Reset paper portfolio to $10,000 cash? This clears all positions and proposals.")) return;
  render(await api("/api/paper/reset", { method: "POST", body: JSON.stringify({ starting_cash: 10000 }) }));
  _auditBuffer.length = 0;
});

document.querySelector("#proposals").addEventListener("click", async e => {
  const btn = e.target.closest("button[data-action]");
  if (!btn) return;
  render(await api(`/api/proposals/${btn.dataset.id}/${btn.dataset.action}`, { method: "POST" }));
});

document.querySelector("#sessionsTab")?.addEventListener("click", loadSessions);

// ---------------------------------------------------------------------------
// SSE — fully live, no polling
// ---------------------------------------------------------------------------
function connectStream() {
  const es = new EventSource("/api/stream");
  const dot = document.querySelector("#liveDot");

  es.addEventListener("state", e => {
    try { render(JSON.parse(e.data)); } catch {}
  });

  es.addEventListener("audit", e => {
    try { pushAuditEntry(JSON.parse(e.data)); } catch {}
  });

  es.onopen = () => { if (dot) { dot.classList.add("live"); dot.title = "Live"; } };

  es.onerror = () => {
    if (dot) { dot.classList.remove("live"); dot.title = "Reconnecting…"; }
    es.close();
    setTimeout(connectStream, 3000);
  };
}

connectStream();
