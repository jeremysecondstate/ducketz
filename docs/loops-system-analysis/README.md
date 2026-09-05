# Ducketz Loops system analysis

This directory documents the current Loops implementation. Code, immutable
receipts, provider cursors, and datastore health are authoritative; prose alone
is not proof that a provider is connected or an artifact is current.

## Current operating model

As of 2026-09-04, normal operation is one sequential overnight workflow plus a
lightweight daytime consumer:

- 17:05 PT: fetch and append the latest completed session, including production
  OPRA history; build Loop B; train four Options Strategy profit horizons;
  generate candidates; publish the immutable next-session Gameplan. The Scheduled
  operator watches progress/errors, repairs verified failures, and resumes the
  failed stage. A ten-minute health watch covers abandoned work.
- 04:00–17:00 PT: the Duckets `Rolling Forecasts` tab reads the frozen plan and
  rotates its displayed 1-hour and 4-hour routes on wall-clock boundaries. No
  provider fetch, training, or replanning occurs in the UI consumer; D+1 through
  D+5 and the direct weekly forecast remain available in the weekly detail. The
  same cards expose each route's frozen options intent, including its Strategy,
  modeled profit probability, pricing source, and explicit no-trade/revalidation
  reason.
- The active stock-only gameplan trader consumes each forward hourly boundary
  once. It began at 10:00 PT on September 4; missed earlier routes were not
  replayed. A simultaneously active 4-hour route confirms the hourly entry,
  with an opposite direction vetoing a new order instead of creating two
  competing orders.
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
The former stock daily-adaptation schedule now hosts the overnight health watch. The two new immutable-gameplan stock schedules
are the only daytime broker-mutation owners. No document authorizes restarting
the old stack or enabling options orders.

## Current data authority

- The production universe is `AAPL AMZN GOOG MU NVDA SNDK`.
- Canonical operational equity bars remain Databento `EQUS.MINI` under
  `C:\DATASTORE\stocks`. Schwab history and the differently identified
  `XNAS.ITCH` archive remain separate evidence families; an audit found no exact
  OHLC/OHLCV equality supporting a blind cross-provider merge.
- Loop A owns production OPRA `definition`, `cbbo-1m`, and `ohlcv-1h` maintenance
  for all six parents. Other OPRA schemas are retained research history without
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

The overnight pipeline produces or refreshes four related authorities:

1. Directional Loop B data/features and compatible predictions.
2. Options Strategy profitable-outcome models for `1h`, `4h`, `1d`, and `1w`,
   each with histogram-gradient and MLP challenger selection.
3. Exact Strategy candidates using completed-session option evidence for
   planning.
4. One 144-row immutable gameplan: 24 forecasts and 24 options intents per
   symbol.

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
