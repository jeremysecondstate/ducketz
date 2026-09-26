# Hyperliquid loop inventory

Last verified 2026-09-25; code/config authority.
Model fitting cadence and Paper qualification/no-signal policy updated 2026-09-26.

This inventory describes implemented owners and their configured behavior. It
does not assert that a process is currently running, register a schedule, or
freeze a PID, model qualification, balance, or performance result. Check dated
runtime evidence using [Monitoring](MONITORING.md).

## Owners and authority

| Owner / stage | Entry point | Configured cadence or trigger | Authoritative output | Execution authority |
| --- | --- | --- | --- | --- |
| Market coordinator | [`ml.hyperliquid_coordinator`](../../ml/hyperliquid_coordinator.py) | Coordinator checks about every second; each market catches up immediately, then targets completed 15-minute candles plus 5 seconds | Per-market completed run selected by `latest.json`; coordinator owns worker lifetimes | Public-data requests only |
| Per-market data worker | [`ml.hyperliquid_data_loop`](../../ml/hyperliquid_data_loop.py) and [`hyperliquid_data_pipeline`](../../ml/hyperliquid_data_pipeline.py) | Independent candle timer and 15–120-second retry backoff | OHLCV, causal features, labels, summary and feature catalog | None |
| Model runtime | [`ml.hyperliquid_model_runtime`](../../ml/hyperliquid_model_runtime.py) | Poll completed data/model pointers every 5 seconds | Model publication and immutable forecast records | None |
| Candidate fitting | Same model runtime; one background job globally | Normally 900 seconds (15 minutes) of source-candle progress since the last candidate; missing/incompatible candidates also trigger fitting | Exact evaluated bundle, report and assessment; candidate/active pointers | None |
| Forecasting and matured scoring | Same model runtime, polling thread | New data/model identity; at most one saved forecast per decision close | `predictions.parquet`, `outcomes.parquet`; JSON projections and metrics | None |
| Paper portfolio / quote / risk cycle | [`ml.hyperliquid_paper_runtime`](../../ml/hyperliquid_paper_runtime.py) | 30-second configured polling, independently of candle/model timers | Transactional `_paper/ledger.sqlite3` | Simulated fills and internal virtual transfers only |
| Powder execution | [`ml.hyperliquid_powder_runtime`](../../ml/hyperliquid_powder_runtime.py) | Explicit user activation; 30-second configured polling | Separate `_powder/ledger.sqlite3` intents, actual fills and observations | IOC orders under account ownership; no transfer calls |
| H.Y.P.E.R. view refresh | [`HyperWorkspace`](../../app/ui/hyper_workspace.py), [`read adapter`](../../app/services/hyperliquid_powder_view.py) | Approximately 5 seconds while the workspace is mounted, including hidden tabs; bounded background reads | No new trading authority; projection of local evidence | None; switching modes never activates trading |
| Timing report | [`ml.hyperliquid_timings`](../../ml/hyperliquid_timings.py) | Explicit on-demand report/export | Point-in-time `_timings` export derived from saved artifacts | None |

The fitting, prediction and scoring rows describe stages inside one model owner,
not three additional daemons. A separately launched single-market loop is an
alternative owner for that market; it is not an additional required stage.
The timing exporter is not a scheduler or a prerequisite for publication.

## Current configuration boundary

The checked-in [market config](../../configs/hyperliquid-markets.json) specifies
BTC, ETH, HYPE and ZEC, a `15m` interval, `C:/DATASTORE/hyperliquid`, and two
concurrent data-update slots. The [model config](../../configs/hyperliquid-models.json)
selects four-bar forecasts, 900-second fitting progress and two numerical threads.
Qualification gates and reserved calibration/assessment blocks are unchanged;
more frequent fitting does not guarantee more Qualified candidates. The
[paper config](../../configs/hyperliquid-paper.json) now sets
`require_qualified_forecasts=true`: only fresh Qualified signals drive allocation.
Paper holds current exposure without an accepted signal, subject to independent
risk reductions; see the [Paper loop](loops/paper.md). These are configuration
facts, not permanent market membership or qualification claims.

| Owner | Applies during operation | Requires restart / other boundary |
| --- | --- | --- |
| Coordinator | Valid `symbols` edits; removed workers finish safely | Every other coordinator setting; invalid edits retain accepted config and expose `config_error` |
| Model runtime | Valid shared-market membership edits | Model settings and shared market interval/root; invalid edits retain accepted config and expose `config_error` |
| Paper runtime | Rereads shared market config each tick; continues observing inherited/held assets removed from strategy membership | Paper/model settings, selected interval and first model horizon are captured at construction; invalid market config fails that cycle into degraded status |
| UI | Reads new persisted observations | Does not apply strategy edits or manage worker lifetimes |
| Powder | Fresh account/order/book/forecast evidence every cycle | Pins configuration and account binding; changed files block execution and incompatible restart requires investigation |

Adding a symbol shares implementation, not fitted parameters or storage. Raw
snapshots live in `<coin>/15m`; fitted bundles and forecasts live in
`_models/<coin>/15m/h4`. Paper is one pooled ledger with account and coin keys.

## Ownership and independent progress

| Scope | Ownership mechanism |
| --- | --- |
| Datastore coordinator | `_coordinator/.coordinator.lock` |
| Market loop lifetime | `<coin>/<interval>/.loop.lock` |
| Market publication | `<coin>/<interval>/.update.lock` |
| Model runtime | `_models/_runtime/.runtime.lock` |
| Paper runtime | `_paper/_runtime/.paper.lock`; transactional SQLite writes |

Saved status is evidence with a timestamp, not proof of present lock/process
ownership. Several CLI status helpers probe file locks and can create lock
artifacts; the H.Y.P.E.R. display adapter avoids those helpers and uses read-only
source access plus process identity checks. Ordinary SQLite WAL readers can
create SQLite coordination sidecars without changing authoritative records.

Data publication feeds model fitting/inference. Saved forecasts and public
quotes feed paper allocation. Later completed candles score forecasts; paper
positions/cash/stop history affect subsequent paper decisions. Neither forward
metrics nor paper P/L currently adjusts model weights, fitting recipes, or
policy settings automatically. See [Loop map](LOOP_MAP.md).

A four-bar forecast on 15-minute candles describes an outcome one hour after
its decision close. Paper reassesses signals/risk; that horizon is not a fixed
one-hour exit order. Gracefully stopping paper does not close positions and
does not keep risk management running as a pause would.

## Related analysis

- [Market data](loops/market-data.md): ownership, publication and recovery.
- [Models](loops/models.md): fitting partitions, promotion and forward evidence.
- [Paper](loops/paper.md): simulated allocation and lifecycle.
- [Timing guide](../hyperliquid-timings.md): measured work versus waiting.
- [Maintenance](MAINTENANCE.md): keeping this inventory aligned with code.

The modules do not install Windows startup tasks or recurring Codex jobs.
Whether an operator has configured an external launcher is a separate live
environment question; no external scheduler inventory was asserted here.
