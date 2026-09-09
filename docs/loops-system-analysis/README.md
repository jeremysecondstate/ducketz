# Ducketz Loops system analysis

This directory documents the current Loops implementation. Code, immutable
receipts, provider cursors, and datastore health are authoritative; prose alone
is not proof that a provider is connected or an artifact is current.

## Current operating model

Normal operation is one sequential overnight workflow plus a daytime consumer.
The daily overnight schedule was updated on 2026-09-08:

- 21:05 America/Los_Angeles daily: fetch and append the latest completed session,
  including production OPRA history; build Loop B; maintain XNAS stock target
  history; evaluate saved Gameplans; train four independent stock target groups;
  publish the immutable next-session Gameplan; train independent enrichment;
  publish the required account-aware trade-plan review from that same pinned
  Gameplan. Full runs require the final `gameplan_trade_planning` stage.
  The native calendar skips fresh weekend/holiday work. XNAS Historical normally
  releases at 21:00 Pacific; actual provider coverage is still checked. The Scheduled
  operator watches progress/errors, repairs verified failures, and resumes the
  failed stage. `Loops Operations Watch` checks hourly at :00, covers abandoned
  work, and may start a missing fresh run only after 21:15 PT.
- 04:00–17:00 PT: the Duckets `Rolling Forecasts` tab reads the frozen plan and
  rotates its displayed 1-hour and 4-hour routes on wall-clock boundaries. No
  provider fetch, training, or replanning occurs in the UI consumer; D+1 through
  D+5 and the direct weekly forecast remain available in the weekly detail. The
  same cards expose each route's frozen options intent, including its Strategy,
  modeled profit probability, pricing source, and explicit no-trade/revalidation
  reason.
- The current independent stock session worker starts at 03:55 Pacific on
  weekdays and consumes each forward boundary once, with separate horizon
  ownership and the selected fixed-budget policy. Scheduled long entries use
  the confidence-weighted shared cash budget and all existing live checks.
  Historical note: the September 4 consumer began at 10:00 PT without replaying
  missed routes and used the four-hour route as confirmation for hourly entries.
- Manual start is one launch of [`Start-Gameplan-Trader.cmd`](../../Start-Gameplan-Trader.cmd).
  It enables the Gameplan policy, sleeps with a local heartbeat until the next
  supported 04:00 Pacific opening, then wakes without another click. Before wake
  it makes no broker/model/entry-slot calls. The 03:55 Scheduled launcher adopts
  that worker when present; its own fixed-policy default stays unchanged.
- Every Gameplan from September 4 stays in durable evaluation history, including
  longer forecasts from older plans. Saturday reviews this history.
- After the next 17:00 close: evaluate all matured directional forecasts against
  the completed day, then build the successor plan. Options intents retain their
  lifecycle status; realized option P/L requires exact-leg execution receipts,
  and any future non-executed outcome study must be labeled counterfactual.

The authoritative contract is [Overnight immutable gameplan](NIGHTLY_GAMEPLAN.md).
The [September 4 supervision update](audits/2026-09-04/SUPERVISION_UPDATE.md) records
the deployed fixes, Scheduled changes, verification, and remaining trader proposal.
The [four-hour calibration audit](audits/2026-09-04/FOUR_HOUR_CALIBRATION.md)
explains the first Gameplan's all-50% four-hour row and the corrected promotion
check and display warning.

The former eight recurring supervisors remain implemented for diagnosis and
explicit recovery, but they are stopped and are not the production scheduling
model. The former hourly guardian/adaptive trainer, standalone OPRA history,
Options Strategy paper tracker and prior intraday stock tasks are paused.
The former stock daily-adaptation schedule now hosts the overnight health watch.
The 03:55 independent stock session worker owns daytime stock execution; the
separate 13:00 transition task remains paused. No document authorizes restarting
the old stack or enabling options orders.

The separate `ml/gameplan-trade-plan-latest/run.json` publication retains all 168
forecasts. Projected Trade Quantity is standalone horizon capacity; the adjacent
Direction Based Trade Qty follows one shared cash/stock ledger: approved 54%/46%
directions buy/sell eligible holdings, and Neutral is zero. Fresh cash and all
seven stock balances feed ordered bearish sales, due exits and bullish buys.
Main rows show post-hour cash/shares, with hourly and end-of-day portfolio tables.
Prices use a median-centered +/-20bps working range, a conditional fill
assumption rather than a confidence interval; wider history stays in evidence.
Planning price and cash ranges are estimates only and never execution gates.
The manual Gameplan policy follows the same directions using current ask/bid
prices, actual cash and eligible held shares, with only broker-confirmed fills
updating live balances. The scheduled fixed-policy preview remains expandable.
Day 1 is the completed source session;
the upcoming session is Day 2, with actual dates. See the
[cash/stock projection contract](NIGHTLY_GAMEPLAN.md#account-aware-trade-plan-review)
for protected holdings, no-fill behavior, costs and verification boundaries.

Daily model development includes regularized logistic challengers selected on
chronological development partitions. New directional assessment policy v2
allows Brier score up to 0.005 and log loss up to 0.01 above their baselines while
retaining the other quality checks. Actual scores and the policy are saved;
qualification within these allowances does not claim baseline outperformance.
Earlier immutable publications retain their original policies and scores.

## Current data authority

- The production universe is configured in `datafetching/watchlist.txt` and
  shared by Loop A, ML, option routes, and the stock trader. Additions follow
  [Symbol onboarding](SYMBOL_ONBOARDING.md), including history parity, candidate
  training, publication checks, and activation receipts.
- An onboarding candidate may publish read-only stock-direction forecasts while
  OPRA is delayed, using non-options features and the normal horizon quality
  gates. The UI labels these research forecasts separately; their immutable
  history is evaluated separately by the existing evaluation step. This does
  not publish an options Gameplan or activate the candidate. See
  [Forecasts while options history is delayed](SYMBOL_ONBOARDING.md#forecasts-while-options-history-is-delayed).
- Canonical operational equity bars remain Databento `EQUS.MINI` under
  `C:\DATASTORE\stocks`. Schwab history and the differently identified
  `XNAS.ITCH` archive remain separate evidence families; an audit found no exact
  OHLC/OHLCV equality supporting a blind cross-provider merge.
- Loop A owns production OPRA `definition`, `cbbo-1m`, and `ohlcv-1h` maintenance
  for every configured parent. Other OPRA schemas are retained research history without
  a freshness promise.
- Overnight currentness means complete data from the most recently finished
  session. Final closed-market quotes may be hours old and still be the newest
  correct planning evidence.
- Options Strategy outcome training uses exact historical `cbbo-1m` entry/exit
  snapshots wherever available; `1h` requires exact CBBO, while older
  `4h`/`1d`/`1w` rows may use explicitly labeled conservative hourly fallback
  evidence. A future live order must separately revalidate the same frozen legs
  at execution time and may execute or skip only.
- The source gameplan and paper reader remain advisory-only. Stock execution is
  a separate adapter over the established risk engine and requires two explicit
  persistent switches plus `--execute`; options remain paper/no-trade only.

The current OPRA cursor/coverage observations and data cleanup boundaries are in
[OPRA maintenance](OPRA_HISTORY_MAINTENANCE_AUTOMATION.md) and
[Datastore hygiene](DATASTORE_HYGIENE.md).

## Prediction authorities

The pipeline supports the following related authorities; the active stock-only
scope omits optional Strategy training/generation:

1. Directional Loop B data/features and compatible predictions.
2. Options Strategy profitable-outcome models for `1h`, `4h`, `1d`, and `1w`,
   each with histogram-gradient and MLP challenger selection.
3. Exact Strategy candidates using completed-session option evidence for
   planning.
4. One immutable gameplan: 24 forecasts and 24 options intents per symbol,
   or 168 of each for seven symbols. Saved historical plans retain their own
   original universe and row counts.
5. Separate learned sizing assessments from the pinned training cohorts.
6. A separate immutable account-aware trade plan and readable Gameplan review;
   projected capacities do not confer live order authority.

The first observed generation for action date 2026-09-04 is
`ml/nightly-gameplan-runs/20260904T105944.876700Z`. It contains all 288 rows and
zero order actions. See the gameplan contract for its measured model statuses,
explicit all-`NO_TRADE` option result, and the corrected nine-second initial
publication-boundary miss.

The old option-pricing, option-capture, CME, ALFRED, Loop B, Strategy, and
training supervisors remain described in the per-loop reports because their
modules and artifacts still form the bounded overnight stages. References to
their old minute/hour recurrence describe legacy implementation capability, not
the current scheduler.

## Evidence labels

- **Confirmed:** established by executable implementation or an explicit
  contract.
- **Observed:** established by a timestamped run/artifact and not assumed to be
  permanent.
- **Inferred:** supported by multiple paths but not an explicit contract.
- **Historical:** true for a prior deployment and retained for diagnosis only.
- **Unknown:** not established by repository or current artifacts.

## Index

- [Overnight immutable gameplan](NIGHTLY_GAMEPLAN.md)
- [Retired hourly automation](HOURLY_AUTOMATION.md)
- [System functionality](SYSTEM_FUNCTIONALITY.md)
- [Loop inventory](LOOP_INVENTORY.md)
- [Loop relationships](LOOP_RELATIONSHIPS.md)
- [Visual loop map](LOOP_MAP.md)
- [Prediction contribution matrix](PREDICTION_CONTRIBUTION_MATRIX.md)
- [Options Strategy ML upgrade](OPTIONS_STRATEGY_ML_UPGRADE.md)
- [Monitoring and recovery](MONITORING.md)
- [Datastore authority and hygiene](DATASTORE_HYGIENE.md)
- [OPRA history maintenance](OPRA_HISTORY_MAINTENANCE_AUTOMATION.md)
- [Stock trader runtime](STOCK_TRADER_RUNTIME.md)
- [Stock trader Scheduled contract](STOCK_TRADER_AUTOMATION.md)
- [Pooled sequence encoder and Loop C](POOLED_SEQUENCE_LOOP_C.md)
- Per-loop implementation reports in [loops](loops/)
