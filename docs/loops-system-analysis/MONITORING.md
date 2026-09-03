# Loops system monitoring and guarded recovery

`ml.system_monitor` is the read-only authority for hourly operations, post-close
daily evaluation, and the weekly evidence roll-up. Every layer reads existing
processes, command lines, singleton locks, logs, immutable publications,
Parquet outputs, lineage, and UI adapters. It never restarts a process, moves a
pointer, downloads provider history, promotes a model, or places an order.
`ml/system_monitor.py:164`

`ml.system_guardian` wraps exactly one monitor report with a narrow,
allowlisted liveness policy. Mutation is disabled unless
`--repair-liveness` is explicit, and even then one wake can repair at most one
unambiguous runtime fault. Publication, quality, model, lineage, and provider
findings remain report-only. `ml/system_guardian.py:81`,
`ml/system_guardian.py:237`

## Commands

Hourly operations:

```powershell
.\.venv\Scripts\python.exe -m ml.system_monitor --datastore-target pc --mode hourly --compact
```

Post-close production/output evaluation:

```powershell
.\.venv\Scripts\python.exe -m ml.system_monitor --datastore-target pc --mode daily --compact
```

Explicit weekly roll-up for deterministic inspection or testing:

```powershell
.\.venv\Scripts\python.exe -m ml.system_monitor --datastore-target pc --mode weekly --compact
```

The active Codex heartbeat runs exactly one command per wake:

```powershell
.\.venv\Scripts\python.exe -m ml.system_guardian --datastore-target pc --mode scheduled --repair-liveness --compact
```

Callers must parse the JSON even when the process exits 2. Exit 2 means the
final monitor status is `UNHEALTHY`; `HEALTHY` and evidence-quality
`DEGRADED` reports return normally. The guardian must never be run a second
time merely to clarify its first result.

## Runtime ownership and logs

The monitored deployment is eight independently scheduled singleton owners:
CME/L2, Daily ALFRED, Loop A, Active Pricing, Directional Loop B, Options
Capture, Strategy, and Strategy-profit training. Health requires all of the
following, not a PID alone:

Directional Loop B's canonical cadence is 30 minutes at phase `:05`/`:35`.
Publication freshness is measured from the receipt-verified `promoted_at`
availability clock, not the earlier computation start: WARN begins after 35
minutes and FAIL after 45. This is important at the hourly `:42` wake, when the
normal `:35` cycle can still be running while the prior verified authority
remains safely readable. One retry is allowed only for classified transient
failures; startup recovery runs immediately only at the 35-minute threshold.

- exactly one allowlisted launcher and one child worker for each owner;
- the expected module and production arguments in the worker command line;
- a live worker-owned singleton lock with the matching PID;
- monitor-visible active stdout/stderr and credible publication activity; and
- valid immutable receipts, checksums, pointers, and cross-loop lineage.

`ml.system_monitor.RUNTIMES` owns the monitored modules, arguments, and locks;
`ml.system_guardian.GUARDIAN_LAUNCHES` owns the only recovery commands. The
checked-in launcher imports both contracts instead of copying command strings.
It audits every existing pair first, never starts over a partial, duplicate, or
foreign-lock state, restores missing owners sequentially, and uses resolved
paths, an explicit working directory, unbuffered `-u`, redirected streams, and
`Start-Process -WindowStyle Hidden`. Options always retains
`--skip-historical-catchup`, so unattended liveness recovery cannot initiate a
provider-history maintenance cycle. `docs/datafetch-ml/start_all_loops.ps1:18`,
`ml/system_guardian.py:81`

Canonical new-launch logs live below
`C:\DATASTORE\logs\ducketz\background-launch` or the date-partitioned
`system-guardian` tree. The monitor treats `C:\DATASTORE\logs\ducketz` as the
primary hierarchy and also discovers already-running legacy launches beneath
`C:\DATASTORE\runtime-logs`, including both `.stdout/.stderr` and
`.out/.err` suffix pairs. Legacy discovery is compatibility, not the future
launch destination. `ml/system_monitor.py:427`

**Operational observation — 2026-08-19 22:45:36 UTC:** all seven owners had one
canonical launcher/worker pair, a matching live worker lock, and active primary
`logs\ducketz` streams. The preserved read-only report was `HEALTHY` with 19
`PASS`, 1 `INFO`, 0 `WARN`, 0 `FAIL`, no stale condition,
`read_only=true`, and `orders_placed=0`. No process was restarted during this
documentation reconciliation. The immutable proof is
`C:\DATASTORE\logs\ducketz\restart-proof\20260819T200349.8245990Z\20260819T224536.737912+0000-monitor-post-activation-stale-free-healthy.json`.

The proof deliberately distinguished Loop A's active `WRITING` generation from
`last_complete_generation`: the later cycle was liveness evidence, while
`20260819T223020.010102Z-pid5516` remained the zero-failure completed authority
finished at 22:35:52 UTC. A 22:59:29 UTC scheduled read-only follow-up remained
`HEALTHY` with the same 19/1/0/0 totals after Loop A completed generation
`20260819T224520.001083Z-pid5516` and Loop B and Strategy advanced normally.
Run IDs and process IDs are transient evidence, not architecture.

## Hourly operations contract

Hourly mode verifies:

- the eight exact owner/worker pairs, command arguments, locks, and active logs;
- Loop A complete-cycle and exact-target readiness, CME L2, ALFRED, and all
  symbol option-snapshot authority;
- fresh immutable Loop B and Strategy publications, applicable Pricing target
  and full authorities, and Strategy-to-Loop-B lineage;
- the current pooled sequence-encoder, Loop C observe, and exact Options
  Strategy paper-ledger pointers when published, including checksum/receipt
  integrity, shadow/observe-only authority, and zero-order safety;
- the Rolling Forecast and Options Strategy UI adapters, including the rule
  that heuristic Scenario Coverage cannot enable manual order submission; and
- datastore free capacity and total logs across both recognized log roots.

The XNYS calendar owns target expectations. A closed market, retained last
eligible target, or a quarter-hour still inside its settle window is
informational. A missing or lagging target after the in-session settle window
is a failure. Process health and publication health are separate conclusions:
a valid pair and lock do not make a stale publication healthy, and a fresh
publication does not prove singleton ownership.

For the Rolling Forecast UI, a weekly suffix route is current but
calendar-inapplicable only when one unambiguous created-LIVE per-symbol bundle
proves the aggregate-plus-contiguous-component prefix. A missing, malformed, or
ambiguous bundle remains stale and cannot be hidden by an N/A label.
`ml/runtime_pipeline.py:4005`, `app/ui/rolling_forecast_data.py:592`,
`tests/test_ml_weekly_context_model_runtime.py:361`

## Daily production/output contract

Daily mode contains the full hourly baseline and additionally verifies:

- complete ALFRED vintage lineage, coverage, and lookahead guards;
- directional evaluation metrics for every route: `1h`, `4h`, `1d`, `1w`, and
  `1w-d1` through `1w-d5`;
- chronological evaluation references for calibration, discrimination, Brier
  score, log loss, accuracy, and mature prospective LIVE labels;
- Strategy score basis, model state, exact Pricing coverage, observed option
  outcomes, and candidate coherence; and
- the bounded read-only exact-target Pricing-to-Strategy canary.

`INSUFFICIENT_LIVE_LABELS`, an incomplete outcome, or an unavailable fitted
model describes evidence maturity, not poor measured performance. Scenario
Coverage is a local scenario-grid pass fraction and never a probability.
`Calibrated Probability` remains null until a fitted causal Strategy model and
full eligible exact-leg Pricing coverage are both present.

The daily inventory still names all five weekly component slots. This does not
require a Wednesday LIVE bundle to fabricate Monday-through-Wednesday routes:
only the remaining calendar prefix is forecast, while later suffix slots are
classified as inapplicable under the coherent-bundle rule above.

## Options Strategy paper-ledger contract

The independent `loops-options-strategy-paper-tracking` Scheduled task runs at
00:17 America/Los_Angeles Tuesday through Saturday, after the prior session's
stage-11 exact Strategy outcome evaluation. It runs exactly once:

```powershell
.\.venv\Scripts\python.exe -m ml.loop_c.paper_ledger --datastore-target pc --compact
```

`TRACKING` and `NO_PAPER_TRADES_YET` are valid. The immutable receipt-backed
ledger reconstructs exact Loops-selected 1d/1w Options Strategy entries, keeps
open and evidence-pending positions separate from mature receipt-matched
outcomes, and exposes cumulative modeled net P/L plus current gross, signed,
and maximum single-position share obligations. Each entry carries its exact
legs, expirations, contract multiplier and quantity, maximum share obligation,
and target-window exit no later than expiration session. The task cannot contact Schwab, create an
option order, model an assignment as an execution, or attribute the operator's
personal Schwab positions to Loops.

Once the ledger pointer exists, `sequence_encoder_loop_c` validates its report,
manifest, receipt, checksums, safety fields, counts, pending/mature split, and
counterfactual net P/L. A missing never-published ledger is informational; an
invalid published artifact is a warning and never authorizes pointer repair or
broker action.

## Weekly evaluation contract

Weekly mode contains the complete daily and hourly baselines and then compares
the last two completed XNYS session weeks using the current verified immutable
Loop B publication and its checksum-bound evaluation evidence.
`ml/system_monitor.py:1375`

The roll-up:

- recognizes the final eligible XNYS session in each calendar week rather than
  assuming Friday, so holiday and shortened weeks are handled correctly;
- uses only immutable `LIVE` evaluations and collapses duplicates to one latest
  causal prediction per independent symbol/decision/target cluster;
- compares a route only when provider, model, prediction mode, target
  definition/specification, and round-trip-cost contracts are identical;
- requires at least 30 independent evaluated observations in each period; and
- reports accuracy, Brier score, log loss, positive rate, and mean calibrated
  probability deltas only after those gates pass.

If either period is immature, the result is
`INSUFFICIENT_WEEKLY_EVIDENCE`. If definitions differ, it is
`INCOMPATIBLE_WEEKLY_DEFINITIONS`. Neither status is a trend, and weekly mode
never repackages the `1w` prediction route as a system-wide weekly evaluation.

## Exchange-calendar-aware scheduled selection

The heartbeat wakes hourly at minute 42, allowing the `+1`, `+5`, `+6`, and
`+10` phases to settle. The monitor layer remains lightweight hourly except at
the local 2:42 PM wake, when it asks the XNYS calendar whether the local market
date is an eligible session and already closed:

- non-session or not-yet-closed date: hourly;
- eligible non-final session of the exchange week: daily; and
- final eligible session of the exchange week: weekly.

Thus an exchange holiday Friday selects weekly evaluation after Thursday's
eligible close; a hard-coded weekday test is not used.
`ml/system_monitor.py:3229`

Scheduled monitor and guardian JSON also contain a
`loops-overnight-accuracy-schedule-v2` object with a separate overnight accuracy
lane. On a completed eligible official session, the regular 17-stage window is
1:42 PM through 5:42 AM America/Los_Angeles. The local-hour
mapping is stable across daylight saving changes, while XNYS session evidence
prevents holidays or non-session dates from starting a stale cycle. An early
close must already be complete before the 1:42 PM seal stage, but it does not
shift or duplicate the regular stage catalog. Friday's cycle ends after the
Saturday 5:42 AM freeze; unchanged Friday evidence is not replayed throughout
the weekend.

The stage metadata includes the eligible session and official close, the
separate stock `equity_actionable_close`, whether that broader day is complete,
the next session open, week-final flag, stable stage ID and objective, action
scope, 45-minute bound, shadow-experiment permission, and pre-open freeze flag.
It is additive: only
the 2:42 PM stage selects the existing daily/weekly monitor layer, so expensive
quality and weekly checks are not repeated seventeen times.

## Overnight accuracy pipeline

Every overnight wake first completes the same guardian/health baseline. A
blocking runtime, provider, publication, integrity, lineage, storage, UI-parity,
or order-safety incident preempts and defers accuracy work. An explicitly
report-only prediction/model-quality warning does not authorize liveness repair
or block the evidence stage; it becomes input to that stage, while the overall
status remains `DEGRADED`. When operational contracts are established, the
single stage selected by `schedule.overnight_stage` is:

| Local wake | Stable stage ID | Responsibility |
|---|---|---|
| 13:42 | `seal-core-options-session` | Seal official core/options evidence while leaving the still-open stock POST lane provisional. |
| 14:42 | `audit-input-quality` | Audit gaps, duplicates, staleness, timestamps, schemas, nulls, latency, coverage, and lineage by PRE/REGULAR/POST without calling the open tail missing. |
| 15:42 | `audit-ui-output-parity` | Verify Duckets values and labels against authoritative outputs, then inspect visual presentation when the desktop app is already open. |
| 16:42 | `evaluate-directional-1h` | Evaluate only causally mature 1h coverage, calibration, proper scores, hit rate, and checkpoint-session cohorts. |
| 17:42 | `evaluate-directional-4h` | Seal the complete actionable stock day, then evaluate mature 4h quality by PRE/REGULAR/POST checkpoint. |
| 18:42 | `evaluate-directional-1d` | Evaluate prior mature 1d vintages, calibration, coverage, and drift. |
| 19:42 | `evaluate-directional-1w` | Evaluate weekly vintages only with sufficient independent mature outcomes. |
| 20:42 | `audit-cross-horizon-coherence` | Explain material horizon disagreements without forcing agreement. |
| 21:42 | `audit-options-inputs` | Audit chains, quote age, spreads, strikes, expirations, Greeks clocks, liquidity, and missing reasons. |
| 22:42 | `audit-pricing-execution` | Audit Pricing coverage, conservative fills, fees, slippage, haircuts, and outliers. |
| 23:42 | `evaluate-strategy-outcomes` | Evaluate mature exact 1d/1w strategy net returns after modeled execution and fees. |
| 00:42 | `audit-probability-calibration` | Audit raw/calibrated reliability, ECE, Brier, log loss, base rates, and collapse. |
| 01:42 | `select-nightly-bottleneck` | Preregister one evidence-backed hypothesis, cohort, metrics, and rollback rule. |
| 02:42 | `run-shadow-ablation` | Run the session's sole new bounded shadow-only experiment. |
| 03:42 | `compare-challenger` | Compare champion and challenger on identical chronological assessment-clean evidence. |
| 04:42 | `stress-and-gate-review` | Stress symbols, regimes, windows, missing data, fees, and execution; accept, reject, or propose. |
| 05:42 | `preopen-freeze` | Perform final health/rollback verification, summarize, and prohibit new experimental production change. |

Only the 2:42 AM stage may start a new shadow experiment. Later stages validate
that same immutable experiment; they do not tune a second candidate. Unchanged
fingerprints and immature outcomes produce an explicit verified skip. A stage
must checkpoint or defer rather than overlap the next hourly wake. No stage can
weaken causal clocks, assessment or lockbox separation, cohort or quality
thresholds, promotion gates, Pricing requirements, or the zero-order contract.
The sole checked-in Strategy-profit trainer remains the only production model
promotion owner.

The hourly `ui_contracts` baseline is data-authoritative even when no desktop
window is open. It compares every Rolling Forecast route and Options Strategy
candidate ID, probability, percentage scale, expected value, actionability
status, and Scenario-Coverage-versus-ML meaning with the exact current Parquet
publication. It also reports `visual_render_checked=false`, because a backend
adapter check must not masquerade as a screenshot review. At the 3:42 PM UI
stage, an already-open Duckets window may be inspected at its canonical size for
clipping, overlap, truncation, contrast, stale/blank values, and misleading
empty or error states. The scheduler does not pop open the app, authenticate,
or touch order controls merely to obtain a screenshot; a closed window is
reported as `NOT_OBSERVED_APP_CLOSED`, while value-parity failures remain real
health incidents.

## Guarded liveness-recovery contract

The guardian repairs only one unambiguous runtime fault per wake:

- the owner is entirely absent and its exact lock is missing or records a PID
  proven dead;
- exactly one process remains from an otherwise allowlisted pair and that exact
  residual tree can be stopped safely; or
- a healthy-looking pair is confirmed hung by two scheduled observations at
  least 30 minutes apart. PID, creation time, stdout path/size/mtime, and the
  narrowly classified stale-publication failure must all remain unchanged.

The first qualifying hang wake writes `OBSERVING_HANG` evidence and does not
restart. Before any later repair the guardian rechecks the exact process and
lock state. It stops only the selected tree, removes only the unchanged lock
after its worker is proven dead, launches the allowlisted command hidden, and
verifies a new pair plus worker-owned lock. A successful process repair can
still leave publication health pending until the next normal cycle.

Automatic recovery is fail-closed for multiple ownership faults, duplicates,
non-allowlisted commands, unreadable or live foreign locks, authority/integrity
failure, or recent credential, entitlement, rate-limit, and capacity errors.
Each attempted repair has a two-hour per-runtime cooldown. Restart and hang
observation receipts are immutable beneath:

```text
C:\DATASTORE\logs\ducketz\system-guardian\audit
```

The guardian never edits code, mutates authority pointers, deletes arbitrary
locks, initiates historical backfills or provider maintenance, promotes a
model, or places an order.

## Active standalone scheduler boundary

The existing automation is updated in place, not duplicated:

- ID: `loops-hourly-operations`
- Name: `Loops Monitor + Adaptive Trainer`
- Kind: standalone local scheduled task; every run starts a fresh chat
- Status: `ACTIVE`
- Cadence: hourly at local minute 42
- Responsibility: execute the one guardian command, parse its JSON even on
  exit 2, report selected mode/status/remediation and every WARN/FAIL, then run
  at most the one eligible receipt-backed overnight stage.

The standalone scheduler prompt routes every fresh chat to the complete,
checked-in contract in `HOURLY_AUTOMATION.md`; that file preserves the prior
production workflow verbatim and adds only the explicit handoff protocol.

Fresh chats resume through the advisory `loops-hourly-scheduler-handoff-v1`
receipt chain beneath:

```text
C:\DATASTORE\logs\ducketz\system-guardian\scheduler-handoff
```

Every wake reads and checksum-verifies `current.json` before the guardian
baseline, then commits one immutable handoff receipt before ending. The handoff
captures the prior run sequence, selected schedule metadata, outcome,
incident/remediation summary, actions already taken, evidence paths, changed
files, and the next bounded action. It never supplies health, stage, model,
publication, or order authority: a new run must revalidate every continuation
against the current guardian schedule and verified production receipts. An
invalid chain is a scheduling-continuity incident and cannot authorize replay
of an unfinished repair or experiment. `ml/scheduler_handoff.py`

The prompt may apply the smallest tested behavior-preserving root-cause repair
under its bounded repair ladder. It may not improvise broad process repair,
lock deletion, backfill, provider maintenance, pointer mutation, production
feature/model/policy changes, or orders.

## Status meaning

- `HEALTHY`: no warning, failure, or unresolved stale condition;
  informational market/evidence states may be present.
- `DEGRADED`: all required evidence remains readable, but at least one
  operational or production-quality warning needs attention.
- `UNHEALTHY`: at least one required runtime, authority, integrity, freshness,
  or UI contract could not be verified.

Every monitor report publishes `read_only=true`, `orders_placed=0`, and
`automated_action_allowed=false`. The guardian separately records whether the
narrow repair flag was enabled, its decision, all process/lock actions,
before/after evidence, verification, and unconditional `orders_placed=0`.

`INFO` is not silently discarded: it remains in the check inventory, but
`_overall_status` escalates only `WARN` to `DEGRADED` and `FAIL` to
`UNHEALTHY`. In the preserved proof, the sole `INFO` was correct market-aware
behavior: XNYS had no eligible option-evidence target, so Active Pricing had no
target to publish and did not backdate or fabricate a pointer. Process/lock
health, provider authority, publication freshness, cross-loop lineage, and UI
contract health still passed as separate checks. `ml/system_monitor.py:830`,
`ml/system_monitor.py:1767`, `tests/test_system_monitor.py:243`
