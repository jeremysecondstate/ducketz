# Loop inventory

Research-selected additions use [Research symbol onboarding](RESEARCH_SYMBOL_ONBOARDING.md): an explicitly selected batch, a 2018 historical floor, included-plan cost checks, candidate training, verified publication, and atomic activation. Read current membership from `datafetching/watchlist.txt` and validate historical publications against their own saved universes.

## Current scheduled owner

Normal operation has one sequential overnight workflow, its health watch, the
independent stock session worker, and a Saturday Gameplan review:

| Owner | Entry point | Cadence | Final authority | Order authority |
|---|---|---|---|---|
| Loops Overnight Gameplan | `ml.overnight_runtime --scheduled` | Daily 21:05 America/Los_Angeles; native XNYS weekend/holiday no-op; once | Gameplan plus `ml/gameplan-trade-plan-latest/run.json` | None |
| Loops Operations Watch (overnight) | `ml.overnight_runtime --status` and supervised recovery | Hourly at :00, including weekends; missing fresh runs eligible after 21:15 PT | One renewable supervision claim; stage logs and receipts | None |
| Loops Gameplan Weekly Review | `ml.gameplan_evaluation` | Saturday 09:00 PT | Cumulative saved-forecast evaluation history | None |
| Loops Gameplan Stock Trader — Hourly | `ml.gameplan_stock_trader --execute --target-horizon all --sizing-policy fixed-horizon-budget-v1 --run-session` | Weekday 03:55 PT start; one worker covers the action day and transition | Immutable stock decision/execution receipts | Stocks only |
| Loops Gameplan Stock Trader — 1 p.m. Transition | retained historical entry point | Paused; session worker owns transition | No separate active owner | None while paused |
| Manual Gameplan trader | `Start-Gameplan-Trader.cmd` | User starts once; sleep until next supported 04:00 PT, first entry 04:01; finishes 17:00 | Same frozen directions; actual cash/holdings/current quotes | Stocks only after the user's manual start |

The active stock-only XNAS workflow runs these stages sequentially:

| Stage | Module/owner reused | Main result |
|---:|---|---|
| 1 | `datafetching.orchestrate` | Latest completed-session provider data, exact readiness, production OPRA cursors |
| 2 | `ml.prediction_runtime` / Loop B pipeline | Directional samples, features, and compatible prediction authority |
| 3 | `ml.stock_target_history` | Verified completed-session XNAS targets under exact zero-dollar acquisition checks |
| 4 | `ml.gameplan_evaluation` | Evaluate all saved Gameplans from September 4 and retain pending forecasts |
| 5 | `ml.nightly_gameplan` | Immutable next-session plan: 24 forecasts and 24 stock-only option placeholders per symbol |
| 6 | `ml.stock_trader.independent_training` | Separate learned sizing fits and qualification from the four pinned cohorts |
| 7 | `ml.gameplan_trade_planning` | Required separate review: fresh cash/all-configured-symbol holdings, capacity and direction quantities, working prices, shared hourly ledger and end-of-day projection |

Explicit stock-and-options scopes retain `ml.strategy_profit_training_runtime`
and `ml.strategy_runtime` before publication. Older resumes preserve their
recorded stage endpoint. New full independent-stock runs complete only after
trade planning, under the original next-session 04:00 Pacific deadline.

Projected Trade Quantity is standalone horizon capacity. Adjacent Direction Based
Trade Qty uses approved 54%/46% directions through one shared hourly ledger:
bearish sales, due horizon exits, then bullish buys; Neutral adds no trade.
Post-hour cash/shares and EOD holdings retain protected later allocations.
Median-centered +/-20bps working prices are conditional assumptions, not confidence
intervals. Both planning price and cash ranges are estimates with no order-gating
authority. The manual Gameplan policy prices BUY from current ask and SELL from
current bid and recalculates size from actual cash/holdings. The scheduled
fixed-policy confidence-weighted preview remains separate. Upcoming daily rows are Day 2 with actual dates;
historical evidence needs at least two samples and stays outside the main table.
See [the review contract](NIGHTLY_GAMEPLAN.md#account-aware-trade-plan-review).

The stages are bounded commands, not simultaneously running supervisors.
Weekday exchange-holiday wakes produce a checksum-bound no-op receipt without
running a stage or advancing the gameplan pointer.

## Daytime component

`ml.gameplan_executor` remains a bounded advisory/paper reader.
`ml.gameplan_stock_trader` is the scheduled live stock consumer. It never
backfills, requires the two operator switches plus `--execute`, and uses the
existing stock risk, quote, broker-session, exact-once, and reconciliation
contracts. It has no options-order authority.

The explicit manual launcher selects `gameplan-direction-current-market-v1`
and `--wait-for-open`, enabling both stock controls in that one user action.
There is no second prompt or activation at 04:00. Before wake the one locked
worker emits `SLEEPING_UNTIL_OPEN` with a <=30-second heartbeat and uses local
controls only. The unchanged 03:55 Scheduled launcher recognizes and adopts the
manual worker instead of duplicating it. No recurring schedule is created by
manual start; existing scheduled policy defaults remain unchanged.

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
| Stock daily adaptation | Active as Loops Operations Watch | Hourly :00 daytime/overnight supervision; model fitting stays in the nightly build |
| Former intraday stock tasks | Paused | Superseded; must not run alongside the gameplan owner |
| Gameplan hourly stock owner | Active | 03:55 independent session worker; separate horizon ownership and joint cash/risk checks |
| Gameplan 13:00 transition owner | Paused | Transition handled by the existing independent session worker |
| Saturday Loop C operator review | Active | Independent read-only review |

No task may restart the legacy stack or infer options broker authority.
