# Loops stack and Scheduled audit — September 4, 2026

> This is the earlier audit snapshot. See the [supervision update](SUPERVISION_UPDATE.md)
> for fixes, current Scheduled tasks, archived outputs, tests, and the prepared
> trader correction that remains unapplied.


The main documentation reflects the deployed architecture: one after-close acquisition/model/Gameplan workflow, followed by stock-only hourly consumption of the frozen plan. The deployment has important gaps in transition handling, evaluation continuity, recovery scheduling, and the remaining operating instructions.

This is an audit of the current working checkout and local artifacts, observed around 15:08–15:16 Pacific, before Friday's 17:05 run. The checkout contains substantial pre-existing changes, including the new Gameplan implementation; HEAD alone (`b144ca1964f15aa05e169c2b71158b083c1fac4e`) does not reproduce it. The accompanying [evidence snapshot](C:/dev/ducketz/docs/loops-system-analysis/audits/2026-09-04/evidence.json) records the inspected state. Production commands, controls, schedules, and market data were not changed by this audit; broker/provider calls were not made.

## Actual operating schedule

All ten saved automations were inspected. Four are active and six are paused. The active tasks use the local `C:\dev\ducketz` project, the `C:\DATASTORE` working directory, and `gpt-5.6-sol` with `ultra` reasoning. The Windows timezone is Pacific Standard Time, including its daylight-saving adjustment.

| Active task | Pacific schedule | Role |
|---|---|---|
| Loops Overnight Gameplan | Monday–Friday, 17:05 | `ml.overnight_runtime --once --scheduled`: fetch, Loop B, Strategy-profit training, Strategy candidates, Gameplan |
| Loops Gameplan Stock Trader — Hourly | Monday–Friday, 04:01–12:01 and 14:01–16:01 | Consume the current forward hourly route once; reconcile afterward |
| Loops Gameplan Stock Trader — 1 p.m. Transition | Monday–Friday, 13:05 | Consume the frozen 13:00 route during its ten-minute entry grace; currently defective, as described below |
| Loop C Weekly Operator Review | Saturday, 09:00 | Weekly review/reconciliation/proposal workflow; its paper-ledger instructions conflict |

The six paused tasks are standalone OPRA history, Options Strategy paper tracking, stock daily adaptation, legacy four-hour checkpoints, legacy regular opening, and legacy premarket opening. The former hourly guardian's automation ID, `loops-hourly-operations`, has been repurposed as the overnight job. There is no second active hourly guardian under that ID.

The process snapshot showed only the two related Python processes for the Duckets UI, with no recurring Loops supervisors. No matching Ducketz/Loops/Gameplan Windows Task Scheduler entries were found. These Scheduled tasks launch bounded invocations; an always-running Python trader is not required between wakes.

### This holiday weekend

Friday September 4's 17:05 run is expected to ingest Friday's completed data and build the Gameplan for **Tuesday September 8**. Monday September 7 is the Labor Day market holiday. The code's exchange-calendar calculation selected Tuesday 04:00 Pacific as the next stock action boundary after Friday's close; regular trading opens at 06:30 Pacific. [NYSE holiday calendar](https://www.nyse.com/trade/hours-calendars).

There is **no Saturday or Sunday acquisition/Gameplan wake**. Saturday's review is a separate workflow. Monday's overnight wake produces `NOOP_NON_SESSION_DATE`, preserving Friday's plan. This is sufficient if Friday succeeds, but it does not provide weekend catch-up after a failed or missed Friday run.

## Findings

### 1. P1 — The 13:05 trader cannot execute its intended 13:00 route

**Observed and reproduced.** The Gameplan adapter extends this boundary's entry deadline to 13:10, but still classifies its checkpoint using the original 13:00 timestamp. That instant belongs to the configured regular-to-PM closed interval. The shared execution engine then rejects the resulting `CLOSED` checkpoint even when the current time is already inside the PM session.

Today's [13:06 decision receipt](C:/DATASTORE/ml/stock-trader-decision-runs/20260904T200615.652775Z/receipt.json) reports `PREDICTION_INPUTS_UNAVAILABLE`, despite a `GAMEPLAN_STOCK_ACTIONABLE_RECEIPT_VALIDATED` handoff. It selected zero orders and made zero broker-state capture attempts. A read-only replay of the loader and time-in-force selector at 13:06:15 produced six signals labeled `CLOSED`, an execution window of `AFTER_HOURS`, and `ValueError: Unsupported stock checkpoint session: 'CLOSED'`. The equivalent 14:02 probe correctly returned `PM`.

The correction must preserve the frozen 13:00 forecast identity while explicitly supporting its eligible PM execution session. Both the time-in-force selector and target/session eligibility must agree; extending the grace period alone is insufficient. Add a full adapter-to-session regression case before treating this schedule as working.

Sources: [checkpoint assignment](C:/dev/ducketz/ml/stock_trader/gameplan.py:227), [time-in-force selection](C:/dev/ducketz/ml/stock_trader/runtime.py:1237), [target/session gate](C:/dev/ducketz/ml/stock_trader/session.py:159), [unsupported checkpoint error](C:/dev/ducketz/ml/stock_trader/session.py:251). The existing transition test checks the prediction gate, which explains why it passes without catching this failure.

### 2. P1 — Pending forecasts are lost from later evaluation passes

**Confirmed by code and an isolated reproduction.** `_evaluate_prior_gameplan` loads only the Gameplan selected by the current pointer. It does not scan older published plans or carry an unresolved-forecast ledger forward. Once a successor plan replaces that pointer, older D+2–D+5 and direct weekly forecasts are no longer revisited when they mature.

The isolated reproduction first recorded a weekly forecast as `PENDING_MATURITY`, advanced the simulated pointer to another plan, and then supplied the old forecast's now-mature outcome. The later evaluation contained only the newer plan's forecast. This contradicts the documentation's broad promise to evaluate all matured directional forecasts. It concerns directional forecast evaluation, not a claim about realized option P/L.

The evaluator needs a receipt-verified backlog across immutable generations, with each forecast retained until maturity and scored without duplication. Errors reading an existing prior plan should also remain visible: the current reader catches integrity/read failures and returns an empty evaluation frame.

Source: [prior-plan evaluator](C:/dev/ducketz/ml/nightly_gameplan.py:1684), especially its latest-pointer read at line 1705 and pending status at line 1754; [documented evaluation scope](C:/dev/ducketz/ml/nightly_gameplan.py:306).

### 3. P2 — Weekend recovery is absent, and the full overnight chain has no completed run evidence yet

**Observed configuration and evidence gap.** `C:\DATASTORE\ml\overnight-runs` did not exist at the audit snapshot. A complete Gameplan exists, but there is no receipt proving that the new single-owner command has yet completed all five stages in one production run. The current plan's successful publication should not be presented as that proof.

If Friday's single attempt fails or the app misses it, Saturday/Sunday have no build task and Monday's scheduled guard deliberately skips the holiday. The next ordinary eligible build is Tuesday after close, too late for Tuesday's opening plan. The same-day date check in the trader prevents use of an old action date; it does not repair the missing plan.

A recovery policy should discover the latest completed session that still lacks a valid next-session plan, allow a bounded repair before that action date's opening deadline, and avoid rebuilding already-complete sessions. Changing only the calendar recurrence to include weekends would not solve this: `--scheduled` currently returns a no-op on a non-session local date.

Sources: [saved overnight task](C:/Users/7980X/.codex/automations/loops-hourly-operations/automation.toml), [scheduled eligibility](C:/dev/ducketz/ml/overnight_runtime.py:46), [Gameplan date enforcement](C:/dev/ducketz/ml/stock_trader/gameplan.py:136).

### 4. P2 — Current operating documents still contain conflicting executable instructions

**Confirmed documentation drift.** The weekly-review header says the separate paper ledger is paused, while the same file later instructs the runner to refresh it. The active Saturday Scheduled prompt also explicitly requires that refresh and says to fail closed if the document is contradictory. The paused paper-tracking contract says it does not authorize a scheduled run. Saturday's job can therefore stop on the contradiction or execute a legacy workflow that the new header appears to retire.

The stock-trader contract has a correct Gameplan section at the top, but its later “Receipt-driven live + shadow owners” section still states that Loop B computes at `:06`/`:36` and gives imperative legacy `ml.stock_trader.runtime --execute` commands. These instructions are not clearly scoped as historical in that section. Two paused opening-task prompts also retain the obsolete global statement that no live executor is authorized.

Make the weekly ledger ownership explicit, align the Saturday prompt with that decision, and move the old executable stock procedures into an unmistakably historical runbook. A fresh Scheduled task should encounter one consistent current instruction set.

Sources: [weekly header](C:/dev/ducketz/docs/loops-system-analysis/WEEKLY_REVIEW_AUTOMATION.md:3), [weekly ledger command](C:/dev/ducketz/docs/loops-system-analysis/WEEKLY_REVIEW_AUTOMATION.md:93), [Saturday prompt](C:/Users/7980X/.codex/automations/loop-c-weekly-operator-review/automation.toml:5), [paused paper contract](C:/dev/ducketz/docs/loops-system-analysis/OPTIONS_STRATEGY_PAPER_AUTOMATION.md:3), [legacy stock instructions](C:/dev/ducketz/docs/loops-system-analysis/STOCK_TRADER_AUTOMATION.md:80).

### 5. P2 — Overnight failures lack the promised final receipt, and stages have no orchestration timeout

**Confirmed by an isolated failing-stage reproduction.** A child exit code of 1 writes a `FAILED` stage report and raises before `receipt.json` is written. The receipt is created only after all stages succeed. The reproduction produced one stage report and zero receipts. The Scheduled prompt's request to report a failed stage's receipt cannot always be satisfied.

`subprocess.run` also has no timeout or shared opening-deadline budget. Individual provider retries have their own bounds, but these do not bound the total model/fetch stage or ensure a stalled child terminates. A child-launch exception can leave the outer report at `RUNNING`. Successful outer status is based on child exit codes; the Scheduled prompt performs the separate final-artifact verification.

Finalize a checksum-bound terminal failure receipt and include the active stage, cause, logs, and relevant child artifacts. Give the orchestration a stage/deadline budget with controlled child termination. Keep the previous Gameplan pointer authoritative on failure.

Sources: [child invocation](C:/dev/ducketz/ml/overnight_runtime.py:275), [failure branch](C:/dev/ducketz/ml/overnight_runtime.py:299), [success-only receipt](C:/dev/ducketz/ml/overnight_runtime.py:311), [provider retry bounds](C:/dev/ducketz/app/services/databento_retry.py:11).

## Verified state and practical limits

- **Gameplan integrity:** `20260904T105944.876700Z` passed the checked-in pointer, receipt, manifest, and output-file verifier. It is for September 4 and contains exactly 144 forecasts plus 144 option intents. The forecast groups contain 84 hourly, 24 four-hour, 30 daily, and 6 direct weekly rows. Directional `1h`, `4h`, and `1d` are promoted; `1w` is research-only. Every option intent is a `NO_TRADE` variant. Its source publication placed zero orders. The deployed artifact is v1; current code supports it and implements v2 for future publications. The nine-second initial publication-boundary miss is already disclosed in `NIGHTLY_GAMEPLAN.md`.
- **Actual stock consumption:** both persistent activation controls are true. Today's receipts through 15:01 identify `immutable-nightly-gameplan` as their source. There were seven `SUBMITTED` execution events from today's live decisions; latest stored reconciliation snapshots showed six filled and one not yet found in recent order history. That last result is an unresolved observation, not proof of a failed order or a fill. A second attempt at the 10:00 generation was suppressed as already consumed.
- **Datastore append behavior:** normalized equity bars use timestamp-keyed upserts and atomic file replacement, preserving older observations while allowing revised values. This is more precise than a literal append-only description. Provider/dataset families remain separate. Native bars take precedence over derived latency bridges when loaded; leftover same-timestamp bridge rows are a storage-hygiene matter, not permission to merge different providers.
- **Completed-session OPRA:** all 18 production cursors report exclusive completion through September 4, covering September 3. All 18 September 3 production partitions passed raw and normalized checksums, receipt/manifest checks, and normalized-content validation. This establishes the preceding session's data at the snapshot; Friday's acquisition had not yet run. The audit did not revalidate every historical OPRA partition.
- **Pricing/macro scope:** the separate Active Pricing and ALFRED supervisors are stopped. The latest Active Pricing pointer is August 20; ALFRED completion is September 3. The current Loop B manifest rejects the pricing feature family on all 54 symbol/horizon routes, and all 11 `opx__` features are null in the six current overnight source rows. The nightly model still includes these columns in its candidate feature set; there is no fresh BSGP signal in those rows. Current FRED acquisition and existing ALFRED vintage history should not be described as a nightly ALFRED refresh. The per-loop headers largely acknowledge this distinction.
- **Local execution dependency:** the project exists, the app is running, and AC idle sleep is disabled in the current power scheme. Scheduled local jobs still depend on the computer and app remaining available. [Official Scheduled documentation](https://learn.chatgpt.com/docs/automations?surface=app).

## Documentation verdict

`README.md`, `NIGHTLY_GAMEPLAN.md`, `LOOP_INVENTORY.md`, `LOOP_MAP.md`, `LOOP_RELATIONSHIPS.md`, `SYSTEM_FUNCTIONALITY.md`, and `MONITORING.md` describe the main topology correctly. They should distinguish configured behavior from a successful five-stage production receipt, explain the absence of weekend recovery, correct the evaluation claim, and record the broken 13:00 transition. The weekly-review and stock-trader operating contracts require the instruction cleanup described above.

The user's description is therefore accurate as the intended normal path: **completed-session data → DATASTORE → new immutable Gameplan → next-session hourly stock execution**. It is not yet a fully verified, self-recovering feedback loop.

## Validation performed

| Checks | Result |
|---|---|
| Nightly Gameplan, Gameplan trader, stock trader, Loop A orchestration/batching, derived bars, datastore hygiene, independent loop isolation | 145 passed in 13.62 seconds |
| Loop B runtime pipeline, Strategy selection/profit training, OPRA, forecast UI, scheduler handoff, weekly scheduler memory | 231 passed in 89.42 seconds |
| Actual 13:00 adapter/session probe | Reproduced `Unsupported stock checkpoint session: 'CLOSED'` |
| Isolated evaluation across pointer advancement | Reproduced loss of the older pending weekly forecast |
| Isolated failing overnight child | Reproduced missing failure receipt; no child timeout supplied |
| Holiday/calendar probes | Friday accepted, Saturday and Labor Day skipped; next action target Tuesday September 8 at 04:00 Pacific |
| Current Gameplan and preceding-session production OPRA verification | Passed |

Total: **376 existing tests passed**. The second group emitted 1,778 NumPy/joblib deprecation warnings; there were no test failures. These passing tests do not establish end-to-end success of tonight's production run or cover the reproduced defects. The audit intentionally used local reads and isolated synthetic failures instead of launching ingestion, retraining, a scheduled runner, or a broker execution.
