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

function render(data) {
  state = data;
  const { settings, portfolio, last_quotes: quotes = [], universe_source: universeSource = "sample", signals = [], proposals } = data;
  selected.trading_mode = settings.trading_mode;
  selected.approval_mode = settings.approval_mode;

  document.querySelector("#marketLine").textContent = `${settings.trading_mode.toUpperCase()} mode · ${settings.approval_mode} approval · ${settings.markets.join(", ")} markets`;
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

  document.querySelector("#cash").textContent = money(portfolio.cash);
  document.querySelector("#equity").textContent = money(portfolio.cash + Object.values(portfolio.positions).reduce((sum, pos) => {
    const last = portfolio.last_prices[pos.symbol] || pos.avg_cost;
    return sum + pos.quantity * last;
  }, 0));
  paintPnl("#realized", portfolio.realized_pnl);
  paintPnl("#unrealized", unrealizedPnl(portfolio));
  document.querySelector("#scanned").textContent = quotes.length;
  document.querySelector("#lastTick").textContent = data.last_tick_at ? new Date(data.last_tick_at).toLocaleTimeString() : "Never";

  renderQuotes(quotes);
  renderSignals(signals);

  renderPositions(portfolio);
  renderProposals(proposals || []);
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

function renderPositions(portfolio) {
  const positions = Object.values(portfolio.positions).filter((pos) => pos.quantity > 0);
  const target = document.querySelector("#positions");
  if (!positions.length) {
    target.innerHTML = `<div class="empty">No positions</div>`;
    return;
  }
  target.innerHTML = positions.map((pos) => {
    const last = portfolio.last_prices[pos.symbol] || pos.avg_cost;
    const pnl = pos.quantity * (last - pos.avg_cost);
    return `<div class="row">
      <div><strong>${pos.symbol}</strong><br><span>${pos.quantity} shares · avg ${money(pos.avg_cost)}</span></div>
      <strong class="${pnl >= 0 ? "gain" : "loss"}">${money(pnl)}</strong>
    </div>`;
  }).join("");
}

function renderQuotes(quotes) {
  const target = document.querySelector("#quote");
  if (!quotes.length) {
    target.innerHTML = `<div class="empty">No quote yet</div>`;
    return;
  }
  target.innerHTML = quotes.slice(0, 12).map((quote) => `
    <div class="row compact">
      <div><strong>${quote.symbol}</strong><br><span>${quote.source} · ${new Date(quote.timestamp).toLocaleTimeString()}</span></div>
      <strong>${money(quote.price)}</strong>
    </div>
  `).join("");
}

function renderSignals(signals) {
  const target = document.querySelector("#signals");
  if (!signals.length) {
    target.innerHTML = `<div class="empty">No scan yet</div>`;
    return;
  }
  target.innerHTML = signals.map((signal) => `
    <div class="row">
      <div>
        <span class="tag ${signal.action}">${signal.action}</span>
        <strong>${signal.symbol} · ${(signal.score * 100).toFixed(0)}%</strong>
        <br><span>${signal.reason}</span>
      </div>
      <strong>${money(signal.price)}</strong>
    </div>
  `).join("");
}

function renderProposals(proposals) {
  const target = document.querySelector("#proposals");
  if (!proposals.length) {
    target.innerHTML = `<div class="empty">No proposals yet</div>`;
    return;
  }
  target.innerHTML = proposals.slice().reverse().map((proposal) => {
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
        ${isPending ? `<div class="actions">
          <button class="approve" data-action="approve" data-id="${proposal.id}">Approve</button>
          <button class="reject" data-action="reject" data-id="${proposal.id}">Reject</button>
        </div>` : ""}
      </div>
    </div>`;
  }).join("");
}

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
    .map((symbol) => symbol.trim().toUpperCase())
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
});

document.querySelector("#proposals").addEventListener("click", async (event) => {
  const button = event.target.closest("button[data-action]");
  if (!button) return;
  render(await api(`/api/proposals/${button.dataset.id}/${button.dataset.action}`, { method: "POST" }));
});

api("/api/status").then(render).catch((error) => {
  document.querySelector("#marketLine").textContent = error.message;
});
