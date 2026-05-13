let state = {};
const selected = {
  trading_mode: "paper",
  approval_mode: "manual",
};

const money = (value) => `$${Number(value || 0).toFixed(2)}`;

async function api(path, options = {}) {
  const response = await fetch(path, {
    headers: { "content-type": "application/json" },
    ...options,
  });
  if (!response.ok) throw new Error(await response.text());
  return response.json();
}

// ---------------------------------------------------------------------------
// Render
// ---------------------------------------------------------------------------

function render(data) {
  state = data;
  const {
    settings,
    portfolio,
    last_quotes: quotes = [],
    universe_source: universeSource = "sample",
    signals = [],
    proposals,
    tick_paused: tickPaused = false,
  } = data;

  selected.trading_mode = settings.trading_mode;
  selected.approval_mode = settings.approval_mode;

  document.querySelector("#marketLine").textContent =
    `${settings.trading_mode.toUpperCase()} mode · ${settings.approval_mode} approval · ${settings.markets.join(", ")} markets`;

  document.querySelectorAll('input[name="markets"]').forEach((input) => {
    input.checked = settings.markets.includes(input.value);
  });
  document.querySelector("#universe").value = (settings.universe || []).join(", ");
  document.querySelector("#universeSource").textContent = `Universe source: ${universeSource}`;
  document.querySelector("#budget").value = settings.budget;
  document.querySelector("#duration_minutes").value = settings.duration_minutes;
  document.querySelector("#max_scan_symbols").value = settings.max_scan_symbols;
  document.querySelector("#max_loss").value = settings.max_loss;
  document.querySelector("#max_trade_value").value = settings.max_trade_value;
  document.querySelector("#strategy_enabled").checked = settings.strategy_enabled;
  document.querySelector("#auto_tick_enabled").checked = settings.auto_tick_enabled;
  document.querySelector("#tick_interval_seconds").value = settings.tick_interval_seconds;
  document.querySelector("#allow_live_trading").checked = settings.allow_live_trading;

  document.querySelectorAll(".segmented").forEach((group) => {
    const name = group.dataset.name;
    group.querySelectorAll("button").forEach((button) => {
      button.classList.toggle("active", button.dataset.value === selected[name]);
    });
  });

  // Metrics
  document.querySelector("#cash").textContent = money(portfolio.cash);
  document.querySelector("#equity").textContent = money(
    portfolio.cash +
      Object.values(portfolio.positions).reduce((sum, pos) => {
        const last = portfolio.last_prices[pos.symbol] || pos.avg_cost;
        return sum + pos.quantity * last;
      }, 0)
  );
  paintPnl("#realized", portfolio.realized_pnl);
  paintPnl("#unrealized", unrealizedPnl(portfolio));
  document.querySelector("#scanned").textContent = quotes.length;
  document.querySelector("#lastTick").textContent = data.last_tick_at
    ? new Date(data.last_tick_at).toLocaleTimeString()
    : "Never";

  // Quote source (from first quote's source field)
  const sourceEl = document.querySelector("#quoteSource");
  if (quotes.length > 0) {
    sourceEl.textContent = quotes[0].source || "—";
  }

  // Pause / resume button state
  const pauseBtn = document.querySelector("#pauseButton");
  const pauseStatusEl = document.querySelector("#pauseStatus");
  const pauseMetricEl = document.querySelector("#pauseMetric");
  if (tickPaused) {
    pauseBtn.textContent = "Resume Auto-Tick";
    pauseBtn.classList.add("paused");
    pauseStatusEl.textContent = "Paused";
    pauseMetricEl.classList.add("paused-indicator");
  } else {
    pauseBtn.textContent = "Stop Auto-Tick";
    pauseBtn.classList.remove("paused");
    pauseStatusEl.textContent = "Running";
    pauseMetricEl.classList.remove("paused-indicator");
  }

  renderQuotes(quotes);
  renderSignals(signals);
  renderPositions(portfolio);
  renderProposals(proposals || []);
  renderDiagnostics(signals);
}

function paintPnl(selector, value) {
  const element = document.querySelector(selector);
  element.textContent = money(value);
  element.classList.toggle("gain", Number(value) > 0);
  element.classList.toggle("loss", Number(value) < 0);
}

function unrealizedPnl(portfolio) {
  return Object.values(portfolio.positions).reduce((sum, pos) => {
    const last = portfolio.last_prices[pos.symbol] || pos.avg_cost;
    return sum + pos.quantity * (last - pos.avg_cost);
  }, 0);
}

// ---------------------------------------------------------------------------
// Render sub-panels
// ---------------------------------------------------------------------------

function renderPositions(portfolio) {
  const positions = Object.values(portfolio.positions).filter((pos) => pos.quantity > 0);
  const target = document.querySelector("#positions");
  if (!positions.length) {
    target.innerHTML = `<div class="empty">No positions</div>`;
    return;
  }
  target.innerHTML = positions
    .map((pos) => {
      const last = portfolio.last_prices[pos.symbol] || pos.avg_cost;
      const pnl = pos.quantity * (last - pos.avg_cost);
      return `<div class="row">
        <div><strong>${pos.symbol}</strong><br><span>${pos.quantity} shares · avg ${money(pos.avg_cost)}</span></div>
        <strong class="${pnl >= 0 ? "gain" : "loss"}">${money(pnl)}</strong>
      </div>`;
    })
    .join("");
}

function renderQuotes(quotes) {
  const target = document.querySelector("#quote");
  if (!quotes.length) {
    target.innerHTML = `<div class="empty">No quote yet</div>`;
    return;
  }
  target.innerHTML = quotes
    .slice(0, 12)
    .map(
      (quote) => `
    <div class="row compact">
      <div><strong>${quote.symbol}</strong><br><span>${quote.source} · ${new Date(quote.timestamp).toLocaleTimeString()}</span></div>
      <strong>${money(quote.price)}</strong>
    </div>
  `
    )
    .join("");
}

function renderSignals(signals) {
  const target = document.querySelector("#signals");
  if (!signals.length) {
    target.innerHTML = `<div class="empty">No scan yet</div>`;
    return;
  }
  target.innerHTML = signals
    .map(
      (signal) => `
    <div class="row">
      <div>
        <span class="tag ${signal.action}">${signal.action}</span>
        <strong>${signal.symbol} · ${(signal.score * 100).toFixed(0)}%</strong>
        <br><span>${signal.reason}</span>
      </div>
      <strong>${money(signal.price)}</strong>
    </div>
  `
    )
    .join("");
}

function renderProposals(proposals) {
  const target = document.querySelector("#proposals");
  if (!proposals.length) {
    target.innerHTML = `<div class="empty">No proposals yet</div>`;
    return;
  }
  target.innerHTML = proposals
    .slice()
    .reverse()
    .map((proposal) => {
      const isPending = proposal.status === "proposed";
      return `<div class="row">
        <div>
          <span class="tag ${proposal.side}">${proposal.side}</span>
          <strong>${proposal.quantity} ${proposal.symbol} @ ${money(proposal.price)}</strong>
          <br><span>${proposal.reason}</span>
          ${proposal.error ? `<br><span class="loss">${proposal.error}</span>` : ""}
        </div>
        <div>
          <span class="tag">${proposal.status}</span>
          ${
            isPending
              ? `<div class="actions">
              <button class="approve" data-action="approve" data-id="${proposal.id}">Approve</button>
              <button class="reject" data-action="reject" data-id="${proposal.id}">Reject</button>
            </div>`
              : ""
          }
        </div>
      </div>`;
    })
    .join("");
}

function renderDiagnostics(signals) {
  const target = document.querySelector("#diagnostics");
  const withDiag = signals.filter((s) => s.diagnostics);
  if (!withDiag.length) {
    target.innerHTML = `<div class="empty">No diagnostics yet</div>`;
    return;
  }
  target.innerHTML = `
    <table class="diag-table">
      <thead>
        <tr>
          <th>Symbol</th>
          <th>Price</th>
          <th>Volatility</th>
          <th>Spread est.</th>
          <th>Trend strength</th>
          <th>Vol spike</th>
          <th>News gate</th>
        </tr>
      </thead>
      <tbody>
        ${withDiag
          .map((s) => {
            const d = s.diagnostics;
            const volClass = d.volatility > 40 ? "warn" : "ok";
            const spikeClass = d.volume_spike ? "spike" : "ok";
            return `<tr>
              <td><strong>${d.symbol}</strong></td>
              <td>${money(d.price)}</td>
              <td><span class="diag-badge ${volClass}">${d.volatility.toFixed(1)}%</span></td>
              <td>${(d.spread_pct * 100).toFixed(2)}%</td>
              <td>${d.trend_strength.toFixed(2)}%</td>
              <td><span class="diag-badge ${spikeClass}">${d.volume_spike ? "SPIKE" : "normal"}</span></td>
              <td><span class="diag-badge ${d.news_gate ? "ok" : "warn"}">${d.news_gate ? "open" : "blocked"}</span></td>
            </tr>`;
          })
          .join("")}
      </tbody>
    </table>`;
}

// ---------------------------------------------------------------------------
// Backtest
// ---------------------------------------------------------------------------

async function runBacktest() {
  const symbols = state.settings
    ? state.settings.universe && state.settings.universe.length
      ? state.settings.universe
      : null
    : null;
  const payload = { ticks: 60, starting_cash: 10000 };
  if (symbols) payload.symbols = symbols;

  const result = await api("/api/backtest", { method: "POST", body: JSON.stringify(payload) });
  renderBacktest(result);
}

function renderBacktest(result) {
  const panel = document.querySelector("#backtestPanel");
  panel.style.display = "block";
  const pnl = result.final_equity - result.starting_cash;
  const pnlClass = pnl >= 0 ? "gain" : "loss";
  const container = document.querySelector("#backtestResult");

  // Mini equity curve using canvas
  const curve = (result.equity_curve || []).map((p) => p.equity);
  const minE = Math.min(...curve);
  const maxE = Math.max(...curve);
  const range = maxE - minE || 1;
  const W = 600, H = 100;
  const pts = curve
    .map((e, i) => {
      const x = (i / (curve.length - 1)) * W;
      const y = H - ((e - minE) / range) * (H - 10) - 5;
      return `${x},${y}`;
    })
    .join(" ");

  container.innerHTML = `
    <div class="bt-summary">
      <div class="bt-stat">
        <span>Starting Cash</span>
        <strong>${money(result.starting_cash)}</strong>
      </div>
      <div class="bt-stat">
        <span>Final Equity</span>
        <strong class="${pnlClass}">${money(result.final_equity)}</strong>
      </div>
      <div class="bt-stat">
        <span>P&L</span>
        <strong class="${pnlClass}">${money(pnl)}</strong>
      </div>
      <div class="bt-stat">
        <span>Trades</span>
        <strong>${result.total_trades}</strong>
      </div>
    </div>
    <svg class="equity-chart" viewBox="0 0 ${W} ${H}" xmlns="http://www.w3.org/2000/svg">
      <polyline points="${pts}" fill="none" stroke="#146c5c" stroke-width="2"/>
    </svg>
    <p class="hint" style="margin-top:8px">Simulated ${result.ticks} ticks · ${result.symbols.join(", ")} · ran at ${new Date(result.ran_at).toLocaleTimeString()}</p>
  `;
}

// ---------------------------------------------------------------------------
// Audit log
// ---------------------------------------------------------------------------

async function loadAuditLog() {
  try {
    const data = await api("/api/audit");
    renderAuditLog(data.entries || []);
  } catch {
    document.querySelector("#auditLog").innerHTML = `<div class="empty">Could not load audit log</div>`;
  }
}

function renderAuditLog(entries) {
  const target = document.querySelector("#auditLog");
  if (!entries.length) {
    target.innerHTML = `<div class="empty">No audit entries yet</div>`;
    return;
  }
  target.innerHTML = [...entries]
    .reverse()
    .slice(0, 50)
    .map((entry) => {
      const time = entry.at ? new Date(entry.at).toLocaleTimeString() : "—";
      const detail = entry.detail ? Object.entries(entry.detail).map(([k, v]) => `${k}: ${v}`).join(" · ") : "";
      const symbol = entry.symbol ? `<span class="audit-symbol">${entry.symbol}</span> ` : "";
      return `<div class="audit-entry">
        <span class="audit-time">${time}</span>
        <span class="tag ${entry.event}">${entry.event}</span>
        <span class="audit-detail">${symbol}${detail}</span>
      </div>`;
    })
    .join("");
}

// ---------------------------------------------------------------------------
// Event wiring
// ---------------------------------------------------------------------------

document.querySelectorAll(".segmented button").forEach((button) => {
  button.addEventListener("click", () => {
    const name = button.closest(".segmented").dataset.name;
    selected[name] = button.dataset.value;
    render({ ...state, settings: { ...state.settings, [name]: button.dataset.value } });
  });
});

document.querySelector("#settingsForm").addEventListener("submit", async (event) => {
  event.preventDefault();
  const form = new FormData(event.currentTarget);
  const universe = String(form.get("universe") || "")
    .split(/[\s,]+/)
    .map((s) => s.trim().toUpperCase())
    .filter(Boolean);
  const payload = {
    markets: form.getAll("markets"),
    universe,
    budget: Number(form.get("budget")),
    duration_minutes: Number(form.get("duration_minutes")),
    max_scan_symbols: Number(form.get("max_scan_symbols")),
    max_loss: Number(form.get("max_loss")),
    max_trade_value: Number(form.get("max_trade_value")),
    trading_mode: selected.trading_mode,
    approval_mode: selected.approval_mode,
    strategy_enabled: document.querySelector("#strategy_enabled").checked,
    auto_tick_enabled: document.querySelector("#auto_tick_enabled").checked,
    tick_interval_seconds: Number(form.get("tick_interval_seconds")),
    allow_live_trading: document.querySelector("#allow_live_trading").checked,
  };
  render(await api("/api/settings", { method: "POST", body: JSON.stringify(payload) }));
});

document.querySelector("#tickButton").addEventListener("click", async () => {
  render(await api("/api/tick"));
  await loadAuditLog();
});

document.querySelector("#pauseButton").addEventListener("click", async () => {
  const tickPaused = state.tick_paused;
  const endpoint = tickPaused ? "/api/tick/resume" : "/api/tick/pause";
  render(await api(endpoint, { method: "POST" }));
});

document.querySelector("#backtestButton").addEventListener("click", async () => {
  document.querySelector("#backtestResult").innerHTML = `<div class="empty">Running backtest…</div>`;
  document.querySelector("#backtestPanel").style.display = "block";
  await runBacktest();
});

document.querySelector("#proposals").addEventListener("click", async (event) => {
  const button = event.target.closest("button[data-action]");
  if (!button) return;
  render(await api(`/api/proposals/${button.dataset.id}/${button.dataset.action}`, { method: "POST" }));
  await loadAuditLog();
});

document.querySelector("#refreshAudit").addEventListener("click", loadAuditLog);

// ---------------------------------------------------------------------------
// Boot
// ---------------------------------------------------------------------------

api("/api/status")
  .then((data) => {
    render(data);
    loadAuditLog();
  })
  .catch((error) => {
    document.querySelector("#marketLine").textContent = error.message;
  });
