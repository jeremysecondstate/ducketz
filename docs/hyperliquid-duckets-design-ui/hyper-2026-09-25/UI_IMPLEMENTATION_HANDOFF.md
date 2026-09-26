# H.Y.P.E.R. UI implementation handoff

Prepared September 25, 2026 for a focused UI build in the existing local
`C:/dev/ducketz` project. This handoff records the agreed design and current
backend state; the H.Y.P.E.R. application tab has not been implemented yet.

## Objective

Add a real **H.Y.P.E.R.** tab beside **Hyperliquid Duckets**, following the
approved compact-header concepts. Inside it, **Paper** displays the running
paper system, while **Powder** mirrors the layout for future real executions
and currently displays an honest **Not connected** state. Keep the existing
Hyperliquid Duckets tab and its manual account workspace.

Read [HYPER_DESIGN.md](HYPER_DESIGN.md) for exact visual and accounting
semantics. Inspect both approved version-2 images:

- [Paper](hyper-paper-command-center-v2.png)
- [Powder](hyper-powder-command-center-v2.png)

The large in-content H.Y.P.E.R. heading and Latin/English tagline were removed
at the user's request. Retain the app navigation label; put Paper/Powder
immediately below it. Use dark navy panels, mint accents, account cards left,
performance chart and journal center, forecasts and decision detail right.
Concept image numbers are illustrative; bind the implemented Paper view to
actual paper records, never to values copied from the images.

## Existing application and useful entry points

- `app/ui/ducket_bucket.py`: Tkinter/ttk application, shared theme and
  `DucketBucketApp._build_layout`; this currently mounts six tabs and no HYPER.
- `app/ui/hyperliquid_workspace.py`: existing workspace panels, table/selection
  patterns and background-loading examples; retain its manual trading behavior.
- `app/ui/hyperliquid_controls.py` and `app/ui/assets/hyperliquid/`: reusable
  visual controls, account/asset marks where appropriate.
- `app/ui/theme.py` plus existing `_apply_hyperliquid_styles()` and
  `_hyper_card()` helpers: theme/card precedents. Use HYPER-specific ttk style
  names for differences because ttk styles are shared across the application.
- `tests/test_ducket_bucket_app.py`, `tests/test_hyperliquid_duckets_ui.py`,
  `tests/visual_hyperliquid_duckets_fixture.py`: integration/visual precedents.
- Python runtime: `C:/dev/ducketz/.venv/Scripts/python.exe`.

Prefer a dedicated HYPER view module plus a small read-only local data adapter.
Keep file/SQLite work outside the Tk event loop, and apply widget changes on
the UI thread. Bound refresh concurrency, preserve selected rows and filters,
and cancel refresh callbacks when the view is destroyed. Follow the existing
application architecture rather than introducing another UI framework.

## Backend sources already built

All four markets (BTC, ETH, HYPE, ZEC) use shared code and separate datasets
under `C:/DATASTORE/hyperliquid`. Inspect live files at implementation time;
PIDs, timestamps, model qualifications and balances are not constants.

| Purpose | Sources |
| --- | --- |
| Market data | `configs/hyperliquid-markets.json`, `ml/hyperliquid_coordinator.py`, each symbol's `15m/latest.json` and `loop_status.json`, `_coordinator/coordinator_status.json` |
| Forecasts/models | `configs/hyperliquid-models.json`, `ml/hyperliquid_model_runtime.py`, `_models/_runtime/status.json`, per-symbol `_models/<coin>/15m/h4/` artifacts |
| Paper portfolio/status | `configs/hyperliquid-paper.json`, `ml/hyperliquid_paper_runtime.py`, `_paper/_runtime/status.json` |
| Paper journal/history | `_paper/ledger.sqlite3`, implemented by `ml/hyperliquid_paper_ledger.py`; tables include positions, fills, transfers, decisions, equity, funding, events and seed |
| Timing | `ml/hyperliquid_timings.py`, per-symbol `loop_events.jsonl`, `_models/_runtime/training_events.jsonl`; `_timings/` contains point-in-time exports |

The ledger is authoritative. Use a read-only SQLite connection/transaction
for display queries. Do not construct `PaperLedger` merely to read it: its
constructor can create schema/initialize state. Parquets and performance
exports can lag the latest ledger; retain their observation timestamps.

Further implementation details are in `docs/hyperliquid-data-pipeline.md`,
`docs/hyperliquid-models.md`, `docs/hyperliquid-paper.md`, and
`docs/hyperliquid-timings.md`.

## Agreed behavior

- Account roles: **Alex short perps**, **Jeremy long perps**, **Clear Pond spot**.
  Small inherited passive spot dust in the first two accounts is not managed.
- Paper began by mirroring real balances/positions, then evolved independently.
  Preserve its existing ledger and history. No reseeding for the UI.
- Show equity, P/L since paper opening, gross exposure, fees and drawdown;
  chart equity/P&L/drawdown and provide positions, fills/trades, transfers and
  decision journals, with account and asset filters.
- Headline P/L uses `portfolio.pooled.total_pnl`, not `net_realized_pnl`.
  Inherited gains/losses are not new paper performance; account P/L excludes
  net internal transfers. Pooled equity already includes the accounts.
- Display `P(not-down)` consistently. Qualified and research model badges
  identify qualification, not profitability. Both participate in current Paper.
- Refresh the view approximately every five seconds using bounded local reads.
  Show distinct source ages: data/features/predictions every 15 minutes,
  candidate fitting hourly, model polling every five seconds, paper quote/risk
  cycles every 30 seconds. Four forecast bars means a one-hour horizon.
- Include the new data/model duration breakdown in an expandable operations
  detail or unobtrusive status tooltip; do not replace source ages with timers.
  Separate work time from polling/queue waits and publication time.
- Missing, stale, stopped and partial-data states must be visible without
  freezing the workspace or substituting fabricated zeros.
- Powder shares panel geometry but has no real execution adapter. Use empty
  states for its execution/equity history; any shared model forecast preview
  must be labeled. Switching subtabs must not enable real-money trading.
- Implement only controls supported by the existing runtime. In particular,
  its graceful stop is not a pause that continues risk management, and does
  not automatically close positions. A concept's button wording is not proof
  that pause/resume behavior already exists.

## Build and verification scope

1. Implement the read-only adapter and meaningful tests for current, absent,
   stale and partially available artifacts plus accounting and filtering.
2. Mount the HYPER tab and its Paper/Powder views, reusing the approved layout
   and existing theme. Add tab integration coverage.
3. Verify that refreshes preserve interaction and never start duplicate data,
   training or paper daemons. Viewing the UI must not alter strategy settings.
4. Render an offline fixture for both subtabs at normal and smaller desktop
   sizes, inspect screenshots, and fix overflow/clipping/selection problems.
   Follow with read-only inspection against current local paper artifacts.
5. Run relevant tests and provide screenshots plus concise run instructions.

The local checkout contains substantial earlier uncommitted/untracked pipeline,
model, paper, timing, test and design files. They are work to preserve. Use
this existing checkout so the new chat can access those files. Do not clean,
reset or relocate it as part of the UI build. Background workers may still be
running; inspect status before any lifecycle changes. UI work does not require
changing their schedule or stopping the simulation.

## Suggested starting message

Implement the H.Y.P.E.R. tab in the existing local C:/dev/ducketz application.
Read docs/hyperliquid-duckets-design-ui/hyper-2026-09-25/UI_IMPLEMENTATION_HANDOFF.md
and follow HYPER_DESIGN.md plus both version-2 concept images. Connect Paper
to the existing local paper/model/timing artifacts; make Powder the matching
future real-money view with an honest Not connected state. Preserve the
running workers and existing uncommitted work. Build, test and visually verify
the actual UI without changing trading policies or enabling real execution.
