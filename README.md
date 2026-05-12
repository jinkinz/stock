# Longbridge Trading Tool

A small local trading dashboard for testing an AI-assisted trading workflow.

The app starts in paper mode. Live Longbridge order submission is disabled unless you switch to live mode and enable the live-order checkbox in the dashboard.

Instead of trading one fixed ticker, the dashboard scans a selected market universe across US, HK, and SG symbols. You can leave Custom Universe blank to use symbol discovery where Longbridge exposes it, then sample fallback for unsupported markets, or enter your own symbols such as `AAPL.US, 700.HK, D05.SG`.

## Run

```bash
python3 -m trading_tool.app
```

Then open:

```text
http://127.0.0.1:8765
```

## Live Trading Setup

Install the Longbridge SDK only when you are ready to test live connectivity:

```bash
pip install longbridge
```

Set credentials in your shell or a local secret manager. Do not commit them.

```bash
export LONGBRIDGE_APP_KEY="your-app-key"
export LONGBRIDGE_APP_SECRET="your-app-secret"
export LONGBRIDGE_ACCESS_TOKEN="your-access-token"
```

Longbridge documents these exact environment variables for legacy API-key access.

## Two-Month Paper Run

For a local paper run, use `Trading Mode = Paper`, keep `Allow live order submission` unchecked, set `Approval = Auto` if you want simulated orders to execute without manual clicks, enable `Strategy running`, and enable `Auto paper scan`. For roughly two months, set Duration to `86400` minutes.

Paper state is persisted to `trading_tool/state/paper_state.json`. Simulated order fills are appended to `trading_tool/state/trade_log.jsonl`. Keep the server running for continuous scans.

## Safety Model

- Paper mode is the default and uses a local simulator, not Longbridge live/demo order routing.
- Manual approval is the default.
- Live mode cannot submit orders unless `Allow live order submission` is checked.
- The first scanner ranks a multi-symbol universe with simple momentum logic, not a profit guarantee.
- Budget, max trade value, max loss, and duration are enforced before proposals.
- Live orders are submitted as day limit orders.
- The app suppresses duplicate pending proposals for the same symbol and side.
- Paper trades and state are persisted under `trading_tool/state/`.
- `Allow live order submission` only gates real order placement. Live market data can be used without enabling that final order switch.

## Market Notes

Longbridge documentation shows symbol formats for US (`AAPL.US`), HK (`700.HK`), CN (`600519.SH` / `000568.SZ`), and SG (`D05.SG`). It also documents a `security_list` endpoint, but that endpoint currently says it only supports US Overnight securities. The main OpenAPI overview emphasizes HK and US for supported trading functions, so test SG in paper mode and confirm your account/API permission before enabling live orders.

## Roadmap

- Add a proper Longbridge positions sync for live mode.
- Add historical backtesting before running a strategy.
- Add richer diagnostics: volatility, spread, volume spikes, trend strength, and news gates.
- Add audit logs for every signal, approval, rejection, and order result.
