// ─────────────────────────────────────────────
// Helpers
// ─────────────────────────────────────────────
let state = {};
const selected = { trading_mode: "paper", approval_mode: "manual" };

const money  = v => `$${Number(v || 0).toFixed(2)}`;
const pctStr = v => `${Number(v || 0) >= 0 ? "+" : ""}${Number(v || 0).toFixed(2)}%`;

function formatQty(qty) {
  const n = Number(qty);
  return n % 1 === 0 ? n.toFixed(0) : parseFloat(n.toFixed(6)).toString();
}

async function api(path, options = {}) {
  const r = await fetch(path, { headers: { "content-type": "application/json" }, ...options });
  if (!r.ok) throw new Error(await r.text());
  return r.json();
}

function el(id) { return document.getElementById(id); }

function paintPnl(id, value) {
  const e = el(id); if (!e) return;
  e.textContent = money(value);
  e.className = "acct-value " + (Number(value) > 0 ? "gain" : Number(value) < 0 ? "loss" : "");
}

function tsLabel(iso) {
  if (!iso) return "";
  const age = (Date.now() - new Date(iso).getTime()) / 1000;
  const stale = age > 30;
  return `<span class="section-ts${stale ? " stale" : ""}">${new Date(iso).toLocaleTimeString()}${stale ? " ⚠" : ""}</span>`;
}

// ─────────────────────────────────────────────
// Main render
// ─────────────────────────────────────────────
function render(data) {
  state = data;
  const {
    settings, portfolio,
    last_quotes: quotes = [],
    universe_source = "—",
    signals = [], proposals = [],
    tick_paused = false,
    session_pnl = 0, session_start_at = null,
    signals_updated_at = null,
    lb_connected = false, lb_error = null,
    last_tick_at = null,
    ai_status = {},
  } = data;

  // AI Brain status
  const aiEl = el("aiStatus");
  if (aiEl) {
    const aiOn = ai_status.connected;
    const providerLabel = ai_status.provider && ai_status.provider !== "none" ? ai_status.provider : null;
    aiEl.textContent = aiOn
      ? `${providerLabel || "AI"} · ${ai_status.call_count || 0} calls`
      : providerLabel ? `${providerLabel} (no key)` : "Rule-based fallback";
    aiEl.className = "status-val " + (aiOn ? "ok" : "");
    const lastCall = ai_status.last_call_at
      ? new Date(ai_status.last_call_at * 1000).toLocaleTimeString()
      : "—";
    aiEl.title = ai_status.error || (aiOn
      ? `Model: ${ai_status.model || "default"} · Last call: ${lastCall} · Fallbacks: ${ai_status.fallback_count || 0}`
      : "Set an AI API key in .env to enable");
  }

  // Sync AI controls to current state
  const aiSel = el("aiProvider");
  if (aiSel && data.ai_status?.provider) aiSel.value = data.ai_status.provider;

  const aiModelEl = el("aiModel");
  if (aiModelEl && data.ai_status?.model && document.activeElement !== aiModelEl)
    aiModelEl.placeholder = data.ai_status.model || "blank = default";

  const aiStrEl = el("aiStrategy");
  if (aiStrEl && data.ai_status?.strategy) aiStrEl.value = data.ai_status.strategy;

  const tpEl = el("targetProfit");
  if (tpEl && document.activeElement !== tpEl && !tpEl.disabled)
    tpEl.value = settings.target_profit || 0;

  const tphEl = el("targetProfitPerHour");
  if (tphEl && document.activeElement !== tphEl) {
    tphEl.value = settings.target_profit_per_hour || 0;
    // Keep target field locked/computed if an hourly rate is active server-side
    if (tpEl && settings.target_profit_per_hour > 0) {
      tpEl.value = settings.target_profit || 0;
      tpEl.disabled = true;
    } else if (tpEl) {
      tpEl.disabled = false;
    }
  }

  const lockEl = el("lockProfitPct");
  if (lockEl && document.activeElement !== lockEl)
    lockEl.value = settings.lock_profit_pct || 0;

  const slEl = el("stopLossPct");
  if (slEl && document.activeElement !== slEl)
    slEl.value = settings.stop_loss_pct ?? 2;

  const tsEl2 = el("trailingStopPct");
  if (tsEl2 && document.activeElement !== tsEl2)
    tsEl2.value = settings.trailing_stop_pct || 0;

  _updateStrategyDesc();

  const aiCfgStatus = el("aiConfigStatus");
  if (aiCfgStatus && data.ai_status) {
    const s = data.ai_status;
    if (s.error) {
      aiCfgStatus.style.color = "var(--red)";
      aiCfgStatus.textContent = "⚠ " + s.error;
    } else if (s.connected) {
      aiCfgStatus.style.color = "var(--green)";
      aiCfgStatus.textContent = `✓ ${s.provider} · ${s.model || "default"} · ${s.call_count} calls · ${s.fallback_count} fallbacks`;
    } else {
      aiCfgStatus.style.color = "var(--muted)";
      aiCfgStatus.textContent = s.provider !== "none" ? `Add ${s.provider.toUpperCase()}_API_KEY to .env` : "Momentum rules only (no AI provider)";
    }
  }

  selected.trading_mode = settings.trading_mode;
  selected.approval_mode = settings.approval_mode;

  // Sidebar status
  el("modeLabel").textContent = `${settings.trading_mode.toUpperCase()} · ${settings.approval_mode}`;

  const lbEl = el("lbStatus");
  if (lbEl) {
    lbEl.textContent = lb_connected ? "Connected" : "Disconnected";
    lbEl.className   = "status-val " + (lb_connected ? "ok" : "warn");
    lbEl.title       = lb_error || (lb_connected ? "Real prices active" : "Simulated prices");
  }

  const moEl = el("marketsOpen");
  if (moEl) {
    const mo = data.markets_open;
    if (!mo) {
      moEl.textContent = "24/7 (sim)";
      moEl.className = "status-val";
      moEl.title = "Simulated prices move around the clock — market-hours gate applies only with Longbridge connected";
    } else {
      const anyOpen = Object.values(mo).some(Boolean);
      moEl.textContent = Object.entries(mo).map(([m, open]) => `${m} ${open ? "✓" : "✗"}`).join(" · ");
      moEl.className = "status-val " + (anyOpen ? "ok" : "warn");
      moEl.title = anyOpen
        ? "Only open markets are scanned and traded"
        : "All selected markets are closed — scanning paused until open";
    }
  }

  const qsEl = el("quoteSource");
  if (qsEl && quotes.length) {
    const src = quotes[0].source || "—";
    qsEl.textContent = src;
    // Warn clearly if prices are simulated (not real)
    if (src === "paper-sim") {
      qsEl.className = "status-val warn";
      qsEl.title = "⚠ Prices are SIMULATED — Longbridge is not connected. These are NOT real market prices.";
    } else {
      qsEl.className = "status-val ok";
      qsEl.title = "Real market prices from Longbridge";
    }
  }

  const usEl = el("universeSource");
  if (usEl) usEl.textContent = universe_source;

  if (el("lastTick")) el("lastTick").textContent = last_tick_at ? new Date(last_tick_at).toLocaleTimeString() : "Never";
  el("scanned").textContent = quotes.length;

  // Settings form sync
  document.querySelectorAll('input[name="markets"]').forEach(i => i.checked = settings.markets.includes(i.value));
  el("universe").value            = (settings.universe || []).join(", ");
  el("budget").value              = settings.budget;
  el("duration_minutes").value    = settings.duration_minutes;
  el("max_scan_symbols").value    = settings.max_scan_symbols;
  el("max_loss").value            = settings.max_loss;
  el("max_trade_value").value     = settings.max_trade_value;
  el("tick_interval_seconds").value = settings.tick_interval_seconds;
  el("allow_live_trading").checked  = settings.allow_live_trading;

  document.querySelectorAll(".segmented").forEach(group => {
    const name = group.dataset.name;
    group.querySelectorAll("button").forEach(btn =>
      btn.classList.toggle("active", btn.dataset.value === selected[name]));
  });

  // Session buttons
  const running = settings.strategy_enabled;
  el("startSessionBtn").style.display = running ? "none" : "";
  el("endSessionBtn"  ).style.display = running ? ""     : "none";

  // Pause button
  const pauseBtn = el("pauseButton");
  pauseBtn.textContent = tick_paused ? "▶ Resume Auto-Tick" : "⏸ Stop Auto-Tick";
  pauseBtn.classList.toggle("paused", tick_paused);

  // Account strip
  const hasSession = portfolio.cash > 0 || Object.keys(portfolio.positions).length > 0;
  const holdings   = Object.values(portfolio.positions).reduce((s, p) =>
    s + p.quantity * (portfolio.last_prices[p.symbol] || p.avg_cost), 0);

  el("cash").textContent     = hasSession ? money(portfolio.cash) : "—";
  el("holdings").textContent = hasSession ? money(holdings)       : "—";
  el("sessionStart").textContent = session_start_at ? new Date(session_start_at).toLocaleTimeString() : "—";

  paintPnl("sessionPnl", session_pnl);
  paintPnl("unrealized", unrealizedPnl(portfolio));
  paintPnl("realized",   portfolio.realized_pnl);

  // Approval mode label
  const aml = el("approvalModeLabel");
  if (aml) aml.textContent = settings.approval_mode === "manual"
    ? "Manual — approve or reject each trade below"
    : "Auto — trades execute automatically";

  // Sub-panels
  renderPositions(portfolio);
  renderProposals(proposals, settings.approval_mode);
  renderSignals(signals, signals_updated_at);
  renderQuotes(quotes);
  renderDiagnostics(signals, signals_updated_at);

  // Badge counts
  const openPos   = Object.values(portfolio.positions).filter(p => p.quantity > 0).length;
  const pendingCt = proposals.filter(p => p.status === "proposed").length;

  const posBadge  = el("positionCount");
  if (posBadge)  { posBadge.textContent  = openPos;   posBadge.className  = "badge" + (openPos   ? " has-items" : ""); }
  const propBadge = el("proposalCount");
  if (propBadge) { propBadge.textContent = pendingCt; propBadge.className = "badge" + (pendingCt ? " urgent"    : ""); }
}

function unrealizedPnl(portfolio) {
  return Object.values(portfolio.positions).reduce((s, p) =>
    s + p.quantity * ((portfolio.last_prices[p.symbol] || p.avg_cost) - p.avg_cost), 0);
}

// ─────────────────────────────────────────────
// Positions
// ─────────────────────────────────────────────
function renderPositions(portfolio) {
  const container = el("positions");
  const positions = Object.values(portfolio.positions).filter(p => p.quantity > 0);
  if (!positions.length) { container.innerHTML = `<div class="empty-state">No open positions</div>`; return; }

  container.innerHTML = positions.map(pos => {
    const last       = portfolio.last_prices[pos.symbol] || pos.avg_cost;
    const costBasis  = pos.quantity * pos.avg_cost;
    const currentVal = pos.quantity * last;
    const pnl        = currentVal - costBasis;
    const pnlPct     = costBasis > 0 ? pnl / costBasis * 100 : 0;
    const cls        = pnl >= 0 ? "gain" : "loss";
    return `
    <div class="position-row">
      <div>
        <span class="position-symbol">${pos.symbol}</span>
        <span class="position-qty"> · ${formatQty(pos.quantity)} shares</span>
        <div class="position-meta">
          <span class="position-meta-item">Spent <strong>${money(costBasis)}</strong></span>
          <span class="position-meta-item">Now worth <strong>${money(currentVal)}</strong></span>
          <span class="position-meta-item">Avg <strong>${money(pos.avg_cost)}</strong></span>
          <span class="position-meta-item">Last <strong>${money(last)}</strong></span>
        </div>
      </div>
      <div class="position-pnl">
        <div class="position-pnl-value ${cls}">${pnl >= 0 ? "+" : ""}${money(pnl)}</div>
        <div class="position-pnl-pct ${cls}">${pctStr(pnlPct)}</div>
      </div>
    </div>`;
  }).join("");
}

// ─────────────────────────────────────────────
// Proposals
// ─────────────────────────────────────────────
const TTL = 300;

function renderProposals(proposals, approvalMode) {
  const container = el("proposals");
  if (!proposals.length) { container.innerHTML = `<div class="empty-state">No proposals yet</div>`; return; }

  const isManual = approvalMode === "manual";
  container.innerHTML = [...proposals].reverse().map(p => {
    const pending    = p.status === "proposed";
    const totalValue = p.quantity * p.price;
    let ttlHtml = "";
    if (pending && isManual && p.created_at) {
      const ageSec    = Math.floor((Date.now() - new Date(p.created_at).getTime()) / 1000);
      const remaining = Math.max(0, TTL - ageSec);
      ttlHtml = `<div class="proposal-ttl ${remaining < 60 ? "expiring" : ""}">⏱ ${ageSec}s old · expires in ${remaining}s</div>`;
    }
    return `
    <div class="proposal-row ${pending ? "pending" : ""}">
      <div>
        <span class="tag ${p.side}">${p.side}</span>
        <span class="proposal-amount"> ${money(totalValue)}</span>
        <div class="proposal-detail">${formatQty(p.quantity)} ${p.symbol} @ ${money(p.price)} · ${(p.confidence * 100).toFixed(0)}% confidence</div>
        <div class="proposal-reason">${p.reason}</div>
        ${ttlHtml}
        ${p.error ? `<div class="proposal-ttl expiring">${p.error}</div>` : ""}
      </div>
      <div class="proposal-actions">
        <span class="tag ${p.status}">${p.status}</span>
        ${pending ? `<button class="btn-approve" data-action="approve" data-id="${p.id}">✓ Approve</button>
                     <button class="btn-reject"  data-action="reject"  data-id="${p.id}">✕ Reject</button>` : ""}
      </div>
    </div>`;
  }).join("");
}

// ─────────────────────────────────────────────
// Signals
// ─────────────────────────────────────────────
function renderSignals(signals, updatedAt) {
  const tsEl = el("signalsTs");
  if (tsEl) tsEl.innerHTML = tsLabel(updatedAt);
  const container = el("signals");
  if (!signals.length) { container.innerHTML = `<div class="empty-state">No scan yet — enable Auto scan or Run Tick</div>`; return; }

  container.innerHTML = signals.map(s => {
    const cls = s.action === "buy" ? "gain" : s.action === "sell" ? "loss" : "neutral";
    return `
    <div class="signal-row">
      <div>
        <span class="tag ${s.action}">${s.action}</span>
        <strong style="margin-left:6px">${s.symbol}</strong>
        <div class="signal-meta">${money(s.price)}</div>
        <div class="signal-reason">${s.reason}</div>
      </div>
      <div style="text-align:right">
        <div class="signal-score ${cls}">${(s.score * 100).toFixed(0)}%</div>
        <div style="font-size:11px;color:var(--muted)">score</div>
      </div>
    </div>`;
  }).join("");
}

// ─────────────────────────────────────────────
// Quotes
// ─────────────────────────────────────────────
function renderQuotes(quotes) {
  const container = el("quote");
  if (!quotes.length) { container.innerHTML = `<div class="empty-state">No quotes yet</div>`; return; }
  container.innerHTML = quotes.slice(0, 15).map(q => {
    const chg = q.prev_close > 0 ? (q.price / q.prev_close - 1) * 100 : null;
    const chgHtml = chg === null ? "" :
      `<div class="${chg >= 0 ? "gain" : "loss"}" style="font-size:11px">${pctStr(chg)}</div>`;
    return `
    <div class="quote-row">
      <div><span class="quote-sym">${q.symbol}</span>
        <div class="quote-meta">${q.source} · ${new Date(q.timestamp).toLocaleTimeString()}</div></div>
      <div style="text-align:right"><strong>${money(q.price)}</strong>${chgHtml}</div>
    </div>`;
  }).join("");
}

// ─────────────────────────────────────────────
// Diagnostics
// ─────────────────────────────────────────────
function renderDiagnostics(signals, updatedAt) {
  const tsEl = el("diagTs");
  if (tsEl) tsEl.innerHTML = tsLabel(updatedAt);
  const container = el("diagnostics");
  const withDiag  = signals.filter(s => s.diagnostics);
  if (!withDiag.length) { container.innerHTML = `<div class="empty-state">No diagnostics yet</div>`; return; }

  container.innerHTML = `<table class="data-table">
    <thead><tr><th>Symbol</th><th>Price</th><th>Day Δ</th><th>RSI</th><th>VWAP</th><th>EMA</th><th>Turnover</th><th>Volatility</th><th>Vol Spike</th></tr></thead>
    <tbody>${withDiag.map(s => {
      const d = s.diagnostics;
      const dayCls = (d.day_change_pct || 0) > 0 ? "gain" : (d.day_change_pct || 0) < 0 ? "loss" : "";
      const rsi = d.rsi || 0;
      const vwap = d.vwap_dist_pct || 0;
      const turnover = d.turnover ? (d.turnover >= 1e9 ? (d.turnover/1e9).toFixed(1)+"B" : (d.turnover/1e6).toFixed(1)+"M") : "—";
      return `<tr>
        <td><strong>${d.symbol}</strong></td>
        <td>${money(d.price)}</td>
        <td class="${dayCls}">${d.day_change_pct ? pctStr(d.day_change_pct) : "—"}</td>
        <td>${rsi ? `<span class="badge-pill ${rsi >= 75 ? "danger" : rsi <= 30 ? "warn" : "ok"}">${rsi.toFixed(0)}</span>` : "—"}</td>
        <td class="${vwap > 0 ? "gain" : vwap < 0 ? "loss" : ""}">${vwap ? pctStr(vwap) : "—"}</td>
        <td>${d.ema_trend ? `<span class="badge-pill ${d.ema_trend === "bull" ? "ok" : "danger"}">${d.ema_trend}</span>` : "—"}</td>
        <td>${turnover}</td>
        <td><span class="badge-pill ${d.volatility > 40 ? "warn" : "ok"}">${d.volatility.toFixed(1)}%</span></td>
        <td><span class="badge-pill ${d.volume_spike ? "danger" : "ok"}">${d.volume_spike ? "SPIKE" : "normal"}</span></td>
      </tr>`;
    }).join("")}</tbody></table>`;
}

// ─────────────────────────────────────────────
// Audit log
// ─────────────────────────────────────────────
const _auditBuffer = [];
const MAX_AUDIT = 80;

function pushAuditEntry(entry) {
  _auditBuffer.unshift(entry);
  if (_auditBuffer.length > MAX_AUDIT) _auditBuffer.length = MAX_AUDIT;
  renderAuditLog();
}

function renderAuditLog() {
  const container = el("auditLog");
  if (!_auditBuffer.length) { container.innerHTML = `<div class="empty-state">No entries yet</div>`; return; }
  container.innerHTML = `<div class="audit-list">` +
    _auditBuffer.map(e => {
      const time   = e.at ? new Date(e.at).toLocaleTimeString() : "—";
      const detail = e.detail ? Object.entries(e.detail).filter(([,v]) => v != null && v !== "").map(([k,v]) => `${k}: ${v}`).join(" · ") : "";
      const sym    = e.symbol ? `<span class="audit-sym">${e.symbol}</span> ` : "";
      return `<div class="audit-row">
        <span class="audit-time">${time}</span>
        <span class="tag ${e.event}">${e.event}</span>
        <span class="audit-detail">${sym}${detail}</span>
      </div>`;
    }).join("") + `</div>`;
}

// ─────────────────────────────────────────────
// Performance
// ─────────────────────────────────────────────
// Fetched on its own schedule rather than off the SSE state frame: metrics
// only change when a round trip closes, and the benchmark costs candle calls.
async function loadMetrics() {
  const win = el("metricsWindow")?.value || "all";
  try { renderMetrics(await api(`/api/metrics?window=${win}`)); } catch {}
}

function renderMetrics(report) {
  const container = el("performance"); if (!container) return;
  const m = report.metrics || {};
  const bench = report.benchmark || {};

  if (!m.total_trades) {
    container.innerHTML = `<div class="empty-state">No closed trades in this window yet —
      metrics appear once a position is opened and fully closed.</div>`;
    return;
  }

  const cls = v => Number(v) > 0 ? "gain" : Number(v) < 0 ? "loss" : "";
  const vsBench = Number(report.vs_benchmark_pct || 0);
  // Only expectancy and vs-benchmark are colour-coded. Everything else stays
  // neutral so the two numbers that decide "is this worth running" stand out.
  const profitFactor = m.profit_factor_undefined ? "∞" : Number(m.profit_factor).toFixed(2);

  const stat = (label, value, sub, klass = "") =>
    `<div class="perf-stat">
       <div class="perf-label">${label}</div>
       <div class="perf-value ${klass}">${value}</div>
       ${sub ? `<div class="perf-sub">${sub}</div>` : ""}
     </div>`;

  const warning = m.sample_warning
    ? `<div class="perf-warning">⚠ Only ${m.total_trades} closed trade${m.total_trades === 1 ? "" : "s"} —
        not yet meaningful. Treat every number here as noise until ~20 trades.</div>`
    : "";

  container.innerHTML = `
    <div class="perf-grid">
      ${stat("Expectancy per Trade", money(m.expectancy_per_trade),
             "Average $ a trade is worth", cls(m.expectancy_per_trade))}
      ${stat("Win Rate", `${(Number(m.win_rate) * 100).toFixed(1)}%`,
             `${m.wins} won · ${m.losses} lost of ${m.total_trades}`)}
      ${stat("Profit Factor", profitFactor,
             m.profit_factor_undefined ? "No losing trades yet" : "Gross wins ÷ gross losses")}
      ${stat("Max Drawdown", money(m.max_drawdown_dollars),
             `${Number(m.max_drawdown_pct).toFixed(1)}% from peak`)}
      ${stat("Fees as % of Gross", `${Number(m.fees_as_pct_of_gross).toFixed(1)}%`,
             `${money(m.total_fees)} paid in fees`)}
      ${stat("Strategy vs Buy-and-Hold", pctStr(vsBench),
             `Strategy ${pctStr(report.strategy_return_pct)} · Hold ${pctStr(bench.return_pct)}`,
             cls(vsBench))}
    </div>
    ${warning}
    <div class="perf-foot">
      Net P&L ${money(m.net_pnl)} · avg hold ${formatHold(m.avg_hold_seconds)} ·
      benchmark: ${bench.source || "—"}${bench.symbols?.length ? ` (${bench.symbols.length} symbol${bench.symbols.length === 1 ? "" : "s"})` : ""} ·
      return basis: ${report.return_basis || "—"}
    </div>`;
}

function formatHold(seconds) {
  const s = Number(seconds || 0);
  if (s < 60) return `${s.toFixed(0)}s`;
  if (s < 3600) return `${(s / 60).toFixed(1)}m`;
  return `${(s / 3600).toFixed(1)}h`;
}

// ─────────────────────────────────────────────
// Sessions
// ─────────────────────────────────────────────
async function loadSessions() {
  try { renderSessions((await api("/api/sessions")).entries || []); } catch {}
}

function renderSessions(entries) {
  const tbody = el("sessionsBody"); if (!tbody) return;
  if (!entries.length) { tbody.innerHTML = `<tr><td colspan="5" class="empty-cell">No sessions yet</td></tr>`; return; }
  tbody.innerHTML = [...entries].reverse().slice(0, 30).map(s => {
    const pnl = s.session_pnl || 0;
    return `<tr>
      <td>${s.session_start ? new Date(s.session_start).toLocaleString() : "—"}</td>
      <td>${s.session_end   ? new Date(s.session_end  ).toLocaleString() : "—"}</td>
      <td>${money(s.start_equity)}</td>
      <td>${money(s.end_equity)}</td>
      <td class="${pnl >= 0 ? "gain" : "loss"}">${pnl >= 0 ? "+" : ""}${money(pnl)}</td>
    </tr>`;
  }).join("");
}

// ─────────────────────────────────────────────
// Backtest
// ─────────────────────────────────────────────
async function runBacktest() {
  const symbols = state.settings?.universe?.length ? state.settings.universe : null;
  const payload = { ticks: 60, starting_cash: state.settings?.budget };
  if (symbols) payload.symbols = symbols;
  const result = await api("/api/backtest", { method: "POST", body: JSON.stringify(payload) });
  renderBacktest(result);
}

function renderBacktest(result) {
  el("backtestPanel").style.display = "";
  const pnl   = result.final_equity - result.starting_cash;
  const curve = (result.equity_curve || []).map(p => p.equity);
  const minE  = Math.min(...curve), maxE = Math.max(...curve), range = maxE - minE || 1;
  const W = 600, H = 80;
  const pts = curve.map((e, i) => `${(i/(curve.length-1))*W},${H-((e-minE)/range)*(H-8)-4}`).join(" ");

  el("backtestResult").innerHTML = `
    <div class="bt-grid">
      <div class="bt-stat"><div class="bt-stat-label">Starting Cash</div><div class="bt-stat-value">${money(result.starting_cash)}</div></div>
      <div class="bt-stat"><div class="bt-stat-label">Final Equity</div><div class="bt-stat-value ${pnl>=0?"gain":"loss"}">${money(result.final_equity)}</div></div>
      <div class="bt-stat"><div class="bt-stat-label">P&amp;L</div><div class="bt-stat-value ${pnl>=0?"gain":"loss"}">${pnl>=0?"+":""}${money(pnl)}</div></div>
      <div class="bt-stat"><div class="bt-stat-label">Trades</div><div class="bt-stat-value">${result.total_trades}</div></div>
    </div>
    <svg class="equity-chart" viewBox="0 0 ${W} ${H}" xmlns="http://www.w3.org/2000/svg" preserveAspectRatio="none">
      <polyline points="${pts}" fill="none" stroke="var(--green)" stroke-width="2"/>
    </svg>
    <div style="padding:0 16px 12px;font-size:11px;color:var(--muted)">
      <div style="padding-bottom:4px;${(result.data_source||"").startsWith("SIMULATED") ? "color:var(--red)" : "color:var(--green)"}">Data: ${result.data_source || "simulated"}</div>
      ${result.ticks} bars · ${result.symbols.join(", ")} · ${new Date(result.ran_at).toLocaleTimeString()}
    </div>`;
}

// ─────────────────────────────────────────────
// Collapsible panels — toggled by clicking header
// ─────────────────────────────────────────────
["diagToggle","sessionsToggle","auditToggle"].forEach(id => {
  const btn = el(id);
  if (!btn) return;
  btn.addEventListener("click", () => {
    btn.closest(".collapsible").classList.toggle("collapsed");
  });
});
// Note: collapsed class is set in HTML directly — no JS needed to initialise

// ─────────────────────────────────────────────
// Event wiring
// ─────────────────────────────────────────────
document.querySelectorAll(".segmented button").forEach(btn => {
  btn.addEventListener("click", () => {
    const name = btn.closest(".segmented").dataset.name;
    selected[name] = btn.dataset.value;
    render({ ...state, settings: { ...state.settings, [name]: btn.dataset.value } });
  });
});

el("settingsForm").addEventListener("submit", async e => {
  e.preventDefault();
  const form = new FormData(e.currentTarget);
  const universe = String(form.get("universe")||"").split(/[\s,]+/).map(s=>s.trim().toUpperCase()).filter(Boolean);
  render(await api("/api/settings", { method: "POST", body: JSON.stringify({
    markets: form.getAll("markets"), universe,
    budget:               Number(form.get("budget")),
    duration_minutes:     Number(form.get("duration_minutes")),
    max_scan_symbols:     Number(form.get("max_scan_symbols")),
    max_loss:             Number(form.get("max_loss")),
    max_trade_value:      Number(form.get("max_trade_value")),
    trading_mode:         selected.trading_mode,
    approval_mode:        selected.approval_mode,
    strategy_enabled:     state.settings?.strategy_enabled || false,
    auto_tick_enabled:    true,   // always on — controlled by Start/End Session
    tick_interval_seconds:Number(form.get("tick_interval_seconds")),
    allow_live_trading:   el("allow_live_trading").checked,
  })}));
});

el("startSessionBtn").addEventListener("click", async () => {
  render(await api("/api/settings", { method: "POST", body: JSON.stringify({
    ...state.settings, strategy_enabled: true, auto_tick_enabled: true,
  })}));
});

el("endSessionBtn").addEventListener("click", async () => {
  if (!confirm("End this trading session? Open positions will stay open.")) return;
  render(await api("/api/settings", { method: "POST", body: JSON.stringify({
    ...state.settings, strategy_enabled: false, auto_tick_enabled: false,
  })}));
});

el("tickButton").addEventListener("click",   async () => { render(await api("/api/tick")); loadMetrics(); });
el("pauseButton").addEventListener("click",  async () => {
  render(await api(state.tick_paused ? "/api/tick/resume" : "/api/tick/pause", { method: "POST" }));
});
el("backtestButton").addEventListener("click", async () => {
  el("backtestResult").innerHTML = `<div class="empty-state">Running simulation…</div>`;
  el("backtestPanel").style.display = "";
  await runBacktest();
});
el("resetPaperButton").addEventListener("click", async () => {
  if (!confirm(`Reset paper account to ${money(state.settings?.budget)}? All positions cleared.`)) return;
  render(await api("/api/paper/reset", { method: "POST", body: JSON.stringify({}) }));
  _auditBuffer.length = 0; renderAuditLog();
});

el("proposals").addEventListener("click", async e => {
  const btn = e.target.closest("button[data-action]"); if (!btn) return;
  render(await api(`/api/proposals/${btn.dataset.id}/${btn.dataset.action}`, { method: "POST" }));
});

el("sessionsTab").addEventListener("click", loadSessions);


// ─────────────────────────────────────────────
// AI Brain config
// ─────────────────────────────────────────────
const AI_DEFAULTS = {
  anthropic: "claude-opus-4-8",
  openai: "gpt-4o",
  gemini: "gemini-2.5-flash",
  openrouter: "meta-llama/llama-3.3-70b-instruct",
  ollama: "llama3.2",
  custom: "",
  none: "",
};

const STRATEGY_DESCS = {
  fifo:         "Buy momentum leaders, sell as soon as each position covers its share of the profit target. Lock gains fast, reinvest cash.",
  scalp:        "Many small wins (+0.3% per trade). High turnover. Cut losses at -0.2%. Compounds into target over session.",
  swing:        "Buy strong signals and hold through minor dips. Only sell on clear reversal. Fewer trades, bigger gains.",
  conservative: "High-confidence signals only (>0.75). Tiny positions. Tight stops. Capital preservation first.",
  aggressive:   "Max position sizes, trade every signal, let winners run. High risk — watch closely.",
};

function _updateStrategyDesc() {
  const desc = el("strategyDesc");
  const sel = el("aiStrategy");
  if (desc && sel) desc.textContent = STRATEGY_DESCS[sel.value] || "";
}

el("aiStrategy")?.addEventListener("change", _updateStrategyDesc);

// Live preview: typing $/hour shows the computed session target immediately,
// using whatever duration is currently set in the form (falls back to state).
el("targetProfitPerHour")?.addEventListener("input", () => {
  const rate = parseFloat(el("targetProfitPerHour").value || "0");
  const durationInput = el("duration_minutes");
  const duration = parseFloat((durationInput && durationInput.value) || state.settings?.duration_minutes || 390);
  const hint = el("targetProfitHint");
  if (rate > 0) {
    const computed = (rate * duration / 60).toFixed(2);
    el("targetProfit").value = computed;
    el("targetProfit").disabled = true;
    if (hint) hint.textContent = `= $${computed} over ${duration} min (${(duration/60).toFixed(1)}hr) at $${rate}/hr`;
  } else {
    el("targetProfit").disabled = false;
    if (hint) hint.textContent = "Set $/hour and it auto-calculates the session target. Or set Session Target directly and leave $/hour at 0.";
  }
});

el("aiProvider").addEventListener("change", () => {
  const def = AI_DEFAULTS[el("aiProvider").value] || "";
  el("aiModel").placeholder = def || "enter model name";
  el("aiModel").value = "";
});

el("saveAiBtn").addEventListener("click", async () => {
  const provider = el("aiProvider").value;
  const model    = el("aiModel").value.trim();
  const strategy = el("aiStrategy").value;
  const target   = parseFloat(el("targetProfit").value || "0");
  const targetPerHour = parseFloat(el("targetProfitPerHour").value || "0");
  const lockPct  = parseFloat(el("lockProfitPct").value || "0");
  const stopPct  = parseFloat(el("stopLossPct").value || "0");
  const trailPct = parseFloat(el("trailingStopPct").value || "0");
  const statusEl = el("aiConfigStatus");
  statusEl.style.color = "var(--muted)";
  statusEl.textContent = "Applying…";
  try {
    // Save target_profit + target_profit_per_hour + lock_profit_pct + ai_strategy_name
    await api("/api/settings", { method: "POST", body: JSON.stringify({
      ...state.settings,
      target_profit: target,
      target_profit_per_hour: targetPerHour,
      lock_profit_pct: lockPct,
      stop_loss_pct: stopPct,
      trailing_stop_pct: trailPct,
      ai_strategy_name: strategy,
    })});
    // Then hot-swap the AI provider/model/strategy
    const data = await api("/api/ai/config", { method: "POST",
      body: JSON.stringify({ provider, model, strategy }) });
    const s = data.ai;
    if (s.error) {
      statusEl.style.color = "var(--red)";
      statusEl.textContent = "⚠ " + s.error;
    } else if (s.connected) {
      statusEl.style.color = "var(--green)";
      statusEl.textContent = `✓ ${s.provider} · ${s.model || "default"} · strategy: ${s.strategy}`;
    } else {
      statusEl.style.color = "var(--muted)";
      statusEl.textContent = s.provider !== "none" ? `Add ${s.provider.toUpperCase()}_API_KEY to .env` : "Momentum rules only";
    }
  } catch (err) {
    statusEl.style.color = "var(--red)";
    statusEl.textContent = "Error: " + err.message;
  }
});


// ─────────────────────────────────────────────
// SSE — live
// ─────────────────────────────────────────────
function connectStream() {
  const es = new EventSource("/api/stream");
  const dot = el("liveDot");
  es.addEventListener("state", e => { try { render(JSON.parse(e.data)); } catch {} });
  es.addEventListener("audit", e => { try { pushAuditEntry(JSON.parse(e.data)); } catch {} });
  es.onopen  = () => dot?.classList.add("live");
  es.onerror = () => { dot?.classList.remove("live"); es.close(); setTimeout(connectStream, 3000); };
}

connectStream();

// Performance card: on load, on demand, and on a slow timer. Deliberately not
// tied to the SSE state frame — closed-trade metrics only move when a round
// trip completes, and the benchmark costs candle API calls.
el("metricsWindow")?.addEventListener("change", loadMetrics);
loadMetrics();
setInterval(loadMetrics, 60000);
