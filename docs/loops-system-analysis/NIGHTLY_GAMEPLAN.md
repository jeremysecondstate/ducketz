# Overnight immutable gameplan

This is the operating contract for the Scheduled overnight task and its health
watch. The task stays with fetching, training, and prediction while they run.
It investigates errors immediately, repairs verified defects, and resumes the
failed stage when a restart is needed. Long, healthy training is expected.

## Operating day

- The six-symbol universe is `AAPL AMZN GOOG MU NVDA SNDK`.
- The stock action window is 04:00 through 17:00 America/Los_Angeles. All six
  symbols support the required extended-hours stock sessions.
- Heavy provider work, feature materialization, model fitting, assessment, and
  next-session planning run after the 17:00 stock close and before 04:00.
- “Current” overnight market evidence means the complete most recently finished
  market session. A final option quote can therefore be several clock hours old
  when the option market is closed and still be the correct newest observation.
- The stock universe's action clock extends to 17:00 PT, while standard listed
  equity options finish their normal executable session at 13:00 PT. Thus the
  September 3 OPRA close is `2026-09-03T20:00:00Z` even though stock extended
  hours continue afterward. Those clocks are intentionally not conflated.
- Quote age at overnight planning is not an order-time quote check. Any future
  live executor must revalidate the exact frozen legs against a current tradable
  quote and may execute or skip only. It may not substitute a different contract
  or rebuild the plan intraday.

Single-owner command:

```powershell
cd C:\dev\ducketz
.\.venv\Scripts\python.exe -u -m ml.overnight_runtime --datastore-target pc --once --scheduled
```

The command has no order authority. The fetch stage may read Schwab market data.
No overnight stage can place, cancel, or replace an order.
The scheduled mode checks the XNYS calendar after the system's 17:00 PT action
close. A weekday holiday writes a checksum-bound `NOOP_NON_SESSION_DATE`
receipt beneath `ml/overnight-runs`, runs no stage, and preserves the prior
gameplan pointer. A premature wake on an actual session fails closed.

## Sequential stages

The single overnight owner runs exactly these stages in order and stops on the
first failure:

1. `loop_a_close_fetch` — one Loop A close-cycle fetch, including the bounded
   production OPRA history owner.
2. `loop_b_directional_generation` — one complete Directional Loop B generation.
3. `gameplan_evaluation` — evaluate all saved Gameplans using the refreshed outcomes.
4. `strategy_profit_training` — train and assess the Options Strategy
   profitability models for `1h`, `4h`, `1d`, and `1w`.
5. `strategy_generation` — generate the exact options-strategy candidates from
   the new Loop B and Strategy-model authorities.
6. `gameplan_publication` — train the overnight path models and atomically freeze
   the next action date's stock forecasts and options intents.

Stages do not overlap and do not rely on intraday checksum timing between
independent recurring processes. The overnight run writes a stage report and
receipt beneath `C:\DATASTORE\ml\overnight-runs`.

## Active overnight supervision

`Loops Overnight Gameplan` starts at 17:05 Pacific on exchange-session weekdays.
It must remain active until the workflow completes or reaches an unresolved
failure. Starting a command and ending the Scheduled task is not completion.
`Loops Overnight Health Watch` checks every ten minutes, including weekends,
for a missed start, abandoned run, or failure that needs attention. Healthy work
continues across midnight, weekends, and exchange holidays.

The deadline is **04:00 Pacific on the next exchange session**. There is no short
per-stage timeout and no timeout merely because training is quiet. Friday
September 4 targets Tuesday September 8 because Monday is a market holiday.
The Friday run fetches Friday's completed session. Saturday reviews its outcomes;
there is no extra weekend market session to fetch.

Each stage writes an unbuffered `<stage>.log`. Every 30 seconds the owner updates
`stage-report.json` and appends `health.jsonl`: stage, PID and process creation
time, CPU time, memory, I/O, log growth, recent output/issues, and time left.
HGB/MLP fitting emits iteration progress, fit starts/completions, warnings, and
failures. Non-finite loss fails the fit. A warning needs inspection; it does not
automatically invalidate a model. Assessment/promotion criteria remain enforced.
Flat calibration emits an immediate `FIT_WARNING` and keeps that model group
research-only. This is a model-quality result, not a crashed training process:
inspect and report it without blindly restarting or weakening the promotion gate.

Read progress without starting work:

```powershell
.\.venv\Scripts\python.exe -m ml.overnight_runtime --datastore-target pc --status
```

Before starting, adopting, stopping, repairing, or resuming work, generate one
new UUID for this Scheduled task and acquire supervision:

```powershell
.\.venv\Scripts\python.exe -m ml.overnight_runtime --datastore-target pc --claim-supervision <your-uuid>
```

Proceed only on `ACQUIRED`. `BUSY` means another task owns supervision; report
its current health without changing anything and end this wake. Reuse your own
UUID to renew the claim at least once a minute, including during tests and repairs.
The claim expires after three minutes without renewal, allowing the health watch
to take over if a Scheduled task disappears. Never reuse another task's UUID.
If renewal returns `BUSY`, stop making changes. Release your claim when finished
using `--release-supervision <your-uuid>`. This coordinates the human-readable
Scheduled operators separately from the Python pipeline's process lock.

The Scheduled operator must:

1. Follow the active process session and inspect status, new logs, and health
   history at least once a minute. Read errors while training runs, not only
   after its final exit. Rising CPU/I/O or log progress indicates work; a fresh
   heartbeat alone only proves that the supervisor is alive.
2. Investigate tracebacks, failed fits, non-finite loss, repeated provider
   failures, memory exhaustion, or missing heartbeats immediately. A possible
   stall needs several observations over at least ten minutes with no CPU,
   I/O, or log progress, plus examination of the stage/process. Quiet healthy
   training continues. Do not restart simply because a warning appeared.
3. Fix an established cause with a focused non-trading repository repair and
   relevant tests. Preserve concurrent changes. Record evidence, changed files,
   validation, and restart decisions in `operator-notes.md` within the run.
   Do not change trading code/controls, risk limits, promotion thresholds, raw
   market evidence, or immutable Gameplans to make an error disappear. Do not
   substitute cached output for required training.
4. If the running stage must stop for a repair, request a controlled stop:

   ```powershell
   .\.venv\Scripts\python.exe -m ml.overnight_runtime --datastore-target pc --request-stop-run C:\DATASTORE\ml\overnight-runs\<run> --reason "Specific observed failure and repair"
   ```

   Wait for the terminal receipt. If the supervisor has actually exited, use
   `--recover-run <run> --reason "Verified supervisor exit"` instead. Recovery
   checks process creation times, refuses a living owner or reused PID, stops
   only its remaining children, and writes a terminal receipt. Never delete
   locks or stop unrelated Python/UI/trader processes.
5. After the repair passes its tests, resume the failed stage:

   ```powershell
   .\.venv\Scripts\python.exe -u -m ml.overnight_runtime --datastore-target pc --resume-run C:\DATASTORE\ml\overnight-runs\<failed-run> --once
   ```

   Resume verifies the failed receipt/logs, retains completed stages and the
   original deadline, and works after midnight and over weekends. Monitor the
   resumed attempt too. Do not blindly restart unchanged failures. After two
   unsuccessful recovery attempts for one cause, report the unresolved blocker;
   continue investigating if a distinct, testable fix is available.
6. Verify the final receipt, current Gameplan checksums/next-session date,
   144 forecasts, 144 intents, evaluation coverage, model assessments, zero
   overnight orders, and completed-session provider coverage. Report missing
   data and research-only model groups honestly.

The health watch checks current progress and the supervision claim first.
An active claim prevents a second operator from starting or repairing that run.
After the claim expires, the watch may acquire its own claim, adopt the existing
run, and use the same repair procedure. The process lock also prevents simultaneous
pipelines. Missing work may start only after an exchange session's 17:00 close;
after midnight use the existing attempt's resume path. Completed runs and holiday
no-ops do not trigger retraining. No active run means no new weekend fetch or
training job should be invented.

Completion, stage failure, launch failure, controlled stop, and deadline expiry
write final receipts tied to the stage report and logs. A hard process exit is
detected through stale health and finalized with recovery. A missed 04:00 deadline
is reported and does not authorize intraday replanning.

## Saved Gameplan evaluation

The first Gameplan date is **September 4, 2026**. Every receipt-verified plan from
that date remains saved, including older plans after the latest pointer advances.
The identity is saved run plus forecast ID, so repeated editions do not collide.
Already evaluated forecasts keep their original score and scoring time. Missing
or corrupt saved evidence is an explicit error.

`ml/gameplan-evaluation-latest/run.json` selects a checksummed cumulative
`evaluations.parquet`, `summary.json`, and `review.md`. Each forecast is:

- `PENDING_MATURITY`: its exact target window has not finished.
- `MATURE_AWAITING_DATA`: its window finished, but its outcome is unavailable.
- `EVALUATED`: the actual outcome and probability/direction scores are saved.

Both pending states are revisited. Evaluation follows refreshed data and also
runs before Gameplan fitting; its result survives a later training failure.
Saturday at 09:00 uses the same evaluator without fetching or fitting. The first
review covers September 4 only and retains longer forecasts until they mature.
Earlier prediction systems are not review inputs. On September 4, 1,102
unreferenced earlier prediction runs were moved out of active folders into
`C:\DATASTORE\retired-predictions\20260904`. Historical market data and
source records still needed by retained models, Gameplans, or execution evidence
remain available. Permanent deletion was blocked by automatic approval review;
retirement is reversible.

## OPRA production-history contract

Loop A owns one incremental update for every combination of the six symbols and
these three production schemas:

| Schema | Overnight use |
|---|---|
| `definition` | Point-in-time contract identity and terms |
| `cbbo-1m` | Exact historical option entry/exit BBO and execution evidence |
| `ohlcv-1h` | Option-surface/history context and cross-checks |

The cursor's `completed_through` date is exclusive. For example,
`completed_through=2026-09-04` proves the September 3 session was fetched. The
overnight plan refuses publication unless all 18 symbol/schema cursors cover the
most recently completed session required by the action date. Other retained
OPRA schemas are research history and have no production freshness promise.

For candidate construction, “current overnight quote” means the newest
completed options-session snapshot known before the next action window. The
selector does not require that snapshot to postdate later after-hours equity
bars. It prefers the exact canonical OPRA snapshot when providers share the
same market timestamp, records the quote's real age at planning time, and never
treats that snapshot as next-session execution authority.

OPRA remains the bid/ask authority. If a verified Schwab snapshot describes the
same market timestamp, the planner may fill only OPRA fields that CBBO does not
publish—underlying reference, open interest, volume, and Greeks. It never
replaces the OPRA bid, ask, contract identity, or quote timestamp.

Strategy-profit outcome construction uses the nearest causal `cbbo-1m` snapshot
at each historical entry and exit boundary wherever that archive exists. `1h`
training requires exact CBBO. For older `4h`, `1d`, and `1w` targets beyond the
shorter CBBO archive, a conservative `ohlcv-1h` fallback preserves the longer
history and is explicitly labeled modeled rather than executable BBO. Exact and
modeled evidence remain separate features/counts.

## Frozen forecast grid

Each completed overnight publication contains exactly 24 forecasts per symbol,
144 total:

| Model group | Routes per symbol | Anchors |
|---|---:|---|
| `1h` | 14 | 04:00 through 17:00 inclusive |
| `4h` | 4 | 04:00, 08:00, 12:00, 16:00 |
| `1d` | 5 | D+1 through D+5 |
| `1w` | 1 | one direct five-session forecast |

The intraday route suffix is the predicted checkpoint at the **end** of its
target window, not the time at which a reader should consume an already-matured
forecast. For example, `1h@06:00` predicts 05:00–06:00 and is consumed at
05:00. At 04:00 the reader records the two precomputed opening-gap checkpoints
and also consumes forward `1h@05:00` and `4h@08:00`. At 08:00 it consumes
`1h@09:00` and `4h@12:00`; at 16:00 it consumes `1h@17:00`. The 17:00 wake has
no new forward intraday window and records session close only.

Starting with the v2 planning contract, every intraday forecast and matching
option intent stores both `forecast_anchor_local` (target endpoint) and
`action_anchor_local` (first dispatch time). The reader validates the explicit
action field; its route-derived mapping exists only for the immutable v1
compatibility artifact.

The 04:00 anchor is the start of this system's stock action day. It does not
change the exchange hours during which an equity option can actually trade.
Options intents can be planned for every route, but a route outside a tradable
option session cannot become an option order until its exact frozen legs pass a
live quote/session check.

An intent is not a trade merely because both models exist. The paper-entry gate
also requires a promoted direction model, at least 0.05 direction edge from
0.50, a calibrated option-profit probability of at least 0.55, positive modeled
net profit and return on risk, matching delta direction, completed-session BBO
quality, and a complete listed-options execution window. Because route labels
are target endpoints, the 1-hour option-compatible checkpoints are 08:00
through 13:00 PT (entry 07:00 through 12:00), and the option-compatible 4-hour
checkpoint is 12:00 PT (entry 08:00). Other stock forecasts remain in the
immutable grid but their option intent is explicitly `NO_TRADE`.

For each model group, the overnight builder trains both a histogram-gradient
model and an MLP neural-network challenger, compares the two plus fixed blends
on a later chronological selection partition, calibrates on a separate
partition, and reports final performance on an untouched assessment partition.
Assessment failure leaves that group's output explicitly research-only; it is
never relabeled as promoted.
Promotion also requires varying calibrated probabilities on both the calibration
and assessment partitions, with both target classes available. A constant
base-rate fallback cannot pass as a promoted directional model. The report saves
the calibration status, slope, positive rate, and probability ranges.

## Immutable publication

The atomic pointer is:

`C:\DATASTORE\ml\nightly-gameplan-latest\run.json`

It selects one immutable directory beneath:

`C:\DATASTORE\ml\nightly-gameplan-runs\<generation>`

The generation contains:

- `gameplan.json`
- `forecasts.parquet`
- `option-strategy-intents.parquet`
- `prior-gameplan-evaluations.parquet` (snapshot of all saved forecast evaluations)
- `model-reports.json`
- one fitted artifact per model group
- checksum-bound `manifest.json` and `receipt.json`

The publisher will not replace the current pointer after 04:00 for that action
date. This makes the day's decisions reproducible and allows the next 17:00 run
to compare every matured directional forecast with what happened without
intraday plan drift. The current evaluator does not manufacture realized option
P/L from an intent: that requires an exact-leg execution/revalidation receipt.
A future mark-to-market study of an unexecuted intent must be published
separately and labeled counterfactual.

## Daytime consumers

The bounded paper/advisory consumer is:

```powershell
.\.venv\Scripts\python.exe -m ml.gameplan_executor --datastore-target pc --once
```

It verifies the gameplan receipt, action date, 04:00–17:00 window, and exact
route. It writes a decision receipt under
`C:\DATASTORE\ml\gameplan-decision-runs`. It does not train, fetch, alter the
gameplan, import a broker client, or place an order. `orders_placed` is always
zero.

Live stock or option execution is a separate deployment decision and requires
explicit operator authorization. For options, the only permitted future live
transition is same-leg revalidation followed by execute-or-skip.

The deployed stock-only consumer is:

```powershell
.\.venv\Scripts\python.exe -u -m ml.gameplan_stock_trader `
  --datastore-target pc --execute --target-horizon 1h
```

It requires two independent persistent controls (`CONFIRM_ACTIVE_TRADING` and
`CONFIRM_GAMEPLAN_STOCK_TRADING`) in addition to `--execute`, and then reuses
the established Schwab session, quote, cash, exposure, spread, sizing,
exact-once, deadline, and reconciliation controls. It never backfills missed
hours. At a 1h/4h overlap, the forward 4h route is confirmation: an opposite
actionable direction vetoes a new entry and agreement still yields at most one
shared-risk order per symbol. Options remain non-executable.

## Duckets forecast UI

The Duckets `Rolling Forecasts` tab now defaults to the checksum-verified
`ml/nightly-gameplan-latest/run.json` pointer whenever it exists. It does not
wait for, or activate, the paused daytime executor.

- On initial load, the UI selects the frozen `1h` row whose target window is in
  progress, then the nearest future row or final completed row at the edges of
  the action day.
- The same rule selects the current `4h` window. Consequently the displayed
  route advances at 04:00, 08:00, and 12:00 for forward four-hour windows; the
  16:00 endpoint remains visibly completed because no artificial 16:00–20:00
  route exists beyond the 17:00 action close.
- The ordinary `1d` card shows D+1. The remaining-week card shows the direct
  `1w` prediction plus all five frozen D+1 through D+5 daily predictions.
- Every displayed route also shows its checksum-bound options intent: the
  frozen Strategy name, modeled profit probability when one exists, pricing
  source, and the explicit `NO_TRADE` or same-leg-revalidation reason.
- An immediate load is followed by wall-clock-aligned refreshes five seconds
  after each hour. A 4-hour change therefore uses the same refresh path; no
  second scheduler is required.
- Probabilities, target windows, promotion status, and route identifiers are
  read from the immutable plan. Research-only forecasts remain visible with a
  research warning and are never relabeled promoted.
- UI rotation is read-only. It cannot fetch data, fit a model, rewrite a
  forecast, load a broker adapter, or place an order.

If the gameplan pointer is absent, the legacy rolling-intelligence output
remains a compatibility fallback. If a pointer exists but is stale or invalid,
the UI reports that condition instead of silently substituting a different
authority.

## Scheduler ownership

- `loops-hourly-operations` is repurposed as `Loops Overnight Gameplan` and runs
  the single owner at 17:05 PT on weekdays.
- The former standalone OPRA maintainer stays paused because OPRA maintenance is
  stage 1 of the overnight owner.
- The separate Strategy paper-ledger stays paused. The stock daily-adaptation
  schedule now runs the ten-minute overnight health watch. The cumulative
  Gameplan evaluator owns matured directional evaluation; option-intent P/L is
  not claimed without exact-leg execution or separately labeled counterfactual
  evidence.
- All former Loop-B intraday stock tasks stay paused. The gameplan hourly stock
  owner is active at minute 1 for 04:00–12:00 and 14:00–16:00 PT; a separate
  13:05 wake owns the frozen 13:00 generation after Schwab's closed session
  transition. The initial live boundary on September 4 is 10:00 PT. Missed
  earlier boundaries are not replayed.
- The Saturday read-only operator review remains independent.

## Failure behavior

- A provider, cursor, receipt, model, row-count, or checksum failure stops the
  run and preserves the prior valid pointer.
- A closed market with no newer session is not stale by itself.
- A weekday exchange holiday is an audited no-op, not a reason to retrain on
  unchanged data or replace the next session's plan.
- A supposedly current overnight dataset missing part of the latest completed
  session is stale and blocks publication.
- No stage may start the old recurring stack, delete a historical partition,
  modify a broker account, or infer order authorization.

## First observed publication

The first complete generation is
`C:\DATASTORE\ml\nightly-gameplan-runs\20260904T105944.876700Z`. It froze its
inputs at 03:59:44 PT and contains 144 forecasts plus 144 option intents for
`2026-09-04`. The atomic pointer completed at 04:00:09 PT, nine seconds after
the desired boundary. No order was placed and every intent was `NO_TRADE`.

The timing miss exposed two corrected implementation details:

- completed-session inference now reads the last eligible OPRA snapshot and the
  latest matching Schwab analytical snapshot instead of loading all committed
  snapshots; historical training still reads the complete archive;
- the publisher now performs a second clock check immediately before pointer
  advancement, so a future run that finishes after 04:00 fails closed and
  preserves the prior pointer.

The generation's direction models promoted `1h`, `4h`, and `1d`; `1w` remained
research-only. Its options candidates used promoted `1h`, `1d`, and `1w`
profit authorities. The `4h` profit model remained research-only, so no fitted
4-hour option score was allowed to authorize capital. Research predictions and
failure reasons remain retained for later retraining and evaluation.

The later [four-hour calibration audit](audits/2026-09-04/FOUR_HOUR_CALIBRATION.md)
found that the direction model's 24 four-hour probabilities were all exactly
50%: its calibration fell back to a constant base rate. The original promotion
gate missed that condition. Future publications reject that promotion, and the
UI now labels the saved flat output **No model signal**. The original Gameplan
and its published status remain intact for evaluation.

A pre-correction bounded-reader check at 04:08 PT wrote
`C:\DATASTORE\ml\gameplan-decision-runs\20260904T110818.182966Z`. It consumed
the frozen 04:00 endpoint routes, loaded no broker adapter, submitted zero
orders, and preserved every option decision as its recorded `NO_TRADE` state.
That check exposed that dispatching a route at its target endpoint can consume a
window that has already matured. The corrected reader now maps each action hour
to the next frozen forecast endpoint as described above, while retaining the
special precomputed opening-gap signals. The recurring daytime reader remains
paused. A corrected v2 follow-up receipt at 04:14 PT is
`C:\DATASTORE\ml\gameplan-decision-runs\20260904T111403.230433Z`; it consumed
the four intended 04:00 action routes (`1h@04:00`, `1h@05:00`, `4h@04:00`, and
`4h@08:00`), loaded no broker adapter, and submitted zero orders.

After explicit live-stock activation, the first forward live boundary ran at
10:00 PT and wrote
`C:\DATASTORE\ml\stock-trader-decision-runs\20260904T170009.829457Z`.
The risk engine selected and submitted one AAPL SELL limit for 20 owned shares
at $321.17; immediate reconciliation observed it fully filled at that price.
No option order path was enabled. Earlier September 4 boundaries were not
backfilled.
