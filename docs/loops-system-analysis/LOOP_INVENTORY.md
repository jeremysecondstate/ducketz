# Loop inventory

## Current scheduled owner

Normal operation has one sequential overnight workflow, its health watch, the
hourly stock trader, and a Saturday Gameplan review:

| Owner | Entry point | Cadence | Final authority | Order authority |
|---|---|---|---|---|
| Loops Overnight Gameplan | `ml.overnight_runtime --scheduled` | Weekdays 17:05 America/Los_Angeles; XNYS-session guard; once | `ml/nightly-gameplan-latest/run.json` | None |
| Loops Overnight Health Watch | `ml.overnight_runtime --status` and supervised recovery | Every ten minutes, including weekends | One renewable supervision claim; stage logs and receipts | None |
| Loops Gameplan Weekly Review | `ml.gameplan_evaluation` | Saturday 09:00 PT | Cumulative saved-forecast evaluation history | None |
| Loops Gameplan Stock Trader — Hourly | `ml.gameplan_stock_trader --execute --target-horizon 1h` | Weekday action hours at `:01`, excluding the 13:00 broker transition | Immutable stock decision/execution receipts | Stocks only |
| Loops Gameplan Stock Trader — 1 p.m. Transition | same entry point | Weekdays 13:05 PT for the frozen 13:00 generation | Immutable stock decision/execution receipts | Stocks only |

It runs the following bounded stages sequentially under one run receipt:

| Stage | Module/owner reused | Main result |
|---:|---|---|
| 1 | `datafetching.orchestrate` | Latest completed-session provider data, exact readiness, production OPRA cursors |
| 2 | `ml.prediction_runtime` / Loop B pipeline | Directional samples, features, and compatible prediction authority |
| 3 | `ml.gameplan_evaluation` | Evaluate all saved Gameplans from September 4 and retain pending forecasts |
| 4 | `ml.strategy_profit_training_runtime` | Independently assessed `1h`/`4h`/`1d`/`1w` Strategy-profit models; only passing horizons are promoted |
| 5 | `ml.strategy_runtime` | Exact options-strategy candidates |
| 6 | `ml.nightly_gameplan` | Immutable 144-forecast/144-intent next-session plan |

The stages are bounded commands, not simultaneously running supervisors.
Weekday exchange-holiday wakes produce a checksum-bound no-op receipt without
running a stage or advancing the gameplan pointer.

## Daytime component

`ml.gameplan_executor` remains a bounded advisory/paper reader.
`ml.gameplan_stock_trader` is the scheduled live stock consumer. It never
backfills, requires the two operator switches plus `--execute`, and uses the
existing stock risk, quote, broker-session, exact-once, and reconciliation
contracts. It has no options-order authority.

## Retained legacy supervisors

The repository still contains the former eight recurring owners for diagnosis,
historical artifact compatibility, and explicit operator-directed recovery:

1. `datafetching.cme_runtime`
2. `datafetching.orchestrate`
3. `datafetching.fred_alfred_runtime`
4. `ml.option_pricing_runtime`
5. `datafetching.options_runtime`
6. `ml.prediction_runtime`
7. `ml.strategy_runtime`
8. `ml.strategy_profit_training_runtime`

All eight recurring processes are stopped. Their `--forever` entry points and
`start_all_loops.ps1` are not the normal production start path. Their detailed
per-loop reports describe implementation capabilities and historical artifacts,
not current scheduler recurrence.

## Scheduled-task state

| Task | State | Current role |
|---|---|---|
| Loops Overnight Gameplan | Active | Single nightly owner |
| Standalone OPRA history maintenance | Paused | Folded into overnight stage 1 |
| Options Strategy paper tracking | Paused | Prevent overlap with nightly plan/evaluation |
| Stock daily adaptation | Paused | Superseded by nightly model/gameplan build |
| Former intraday stock tasks | Paused | Superseded; must not run alongside the gameplan owner |
| Gameplan hourly stock owner | Active | Forward 1h entries; active 4h route is confirmation, not a duplicate order |
| Gameplan 13:00 transition owner | Active | Executes the 13:00 generation after Schwab PM opens |
| Saturday Loop C operator review | Active | Independent read-only review |

No task may restart the legacy stack or infer options broker authority.
