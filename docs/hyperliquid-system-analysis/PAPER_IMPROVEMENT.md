# HYPER Paper improvement cycle

Operating contract: **2026-10-02 / v6**.

## September 30 user-directed exploratory Paper policy

The user explicitly replaced qualified-only conservative Paper admission with
an exploratory policy: start loose, observe simulated trades, then tighten from
evidence. From the next verified accepted handoff, valid Research forecasts may
drive Paper alongside Qualified forecasts. Use `require_qualified_forecasts=false`,
`entry_band=exit_band=0`, `rebalance_min_delta_fraction=0`, and
`min_trade_notional=10`. These settings remove the discretionary probability and
70%-target adjustment filters and the extra $25 minimum. Every fresh finite
directional forecast reaches sizing; an unchanged/zero target, exchange quantity
precision, the executable $10 floor, insufficient cash or book depth can still
produce a truthful hold/skip. Never invent a fill merely to fill every decision row.

Research remains labeled Research; its model qualification is not rewritten.
Missing, malformed or expired forecasts hold existing targets while independent
stops, cooldowns and exposure/collateral checks continue. Retain simulated fees,
executable-book pricing, the existing caps, stops, source validation and continuous
5m collection. Powder uses its own retained strict policy file and stays inactive;
Paper exploration must not liberalize current or future real execution.

New exploratory rounds use `valid-in-round-forecast-v2` for WIN evidence: a fresh,
finite in-round forecast actually consumed by Paper, with a matching retained
model record, can be Research or Qualified. Preserve truthful role/qualification,
timing, account, external-flow, common-mark and after-cost return checks. Existing
rounds keep their committed historical rule. This supersedes the earlier
qualified-only WIN restriction prospectively; it does not rescore history.

Retain this exploratory admission policy through automated rounds. Tightening
must be a documented response to observed Paper evidence, not an automatic return
to qualification-only admission or inactivity. Reviews must report forecast-driven
fill counts, turnover/costs, and remaining hold/skip causes. This explicit user
policy change is the improvement for its transition; no extra model retune is
required simply because the closing strict-policy round loses or ties.

The user authorized an independent **Hyperliquid Paper Improvement** Scheduled
task using **GPT-6 Luna / Max**. The earlier Astra / Ultra setup is historical;
the task was initially every three days and subsequently
changed on September 28 to **two-hour competitive rounds with an adaptive
interval**. It evaluates Paper against the same actual accounts shown
in Hyperliquid Duckets, improves underperforming models, preserves the completed
experiment, and starts a new one-for-one paper mirror. This is standing
authorization for these simulated experiments and their necessary local code,
model, and parameter changes. It does not authorize real orders or transfers.

The user subsequently authorized the **5m/h1 Paper trial**: five-minute candles,
forecasts for the next five minutes, and fitting due each 300 seconds of
source-candle progress. Preserve this frequency unless newer user instructions
change it; do not restore historical 15m/h4 defaults. Use the accepted receipt
and configured interval/horizon throughout candidate evaluation and health
checks. Preserve the accepted positive-weight family count (currently five per
slot) and the 70/15/15 split.
Existing target-time expiry applies; Paper still polls books/risk every 30s and
exports performance every 900s. Forecasts need not cause trades or close a
position at their target. Fixed-bar feature windows now span one-third the
clock time and the horizon-based stop cooldown is five minutes.

Retain collected 5m candles and immutable publications across experiment
resets. The user wants this history available for possible future aggregation
to longer bars. No resampler or independent multi-interval collector is
implemented yet; do not claim that changing the configured interval keeps 5m
collection alive automatically. A future longer-interval trial must explicitly
preserve continuous 5m collection and correctly aggregate completed bars.

## Review and decide

Use `C:/dev/ducketz/.venv/Scripts/python.exe`, project `C:/dev/ducketz`, and
datastore `C:/DATASTORE/hyperliquid`. Read the newest scheduled-task memory,
`_paper/experiment.json`, `_operations/paper-current-accepted.json` when present,
and `_operations/paper-maintenance.json`. Respect a different maintenance owner,
newer user stop intent, and `_operations/excluded-paper-runs.json`. Never analyze,
restore, or reconstruct an excluded experiment. Preserve independent research,
unrelated changes, historical archives, and the healthy data coordinator.

The former three-day schedule and 60-hour minimum are superseded. Read
`_operations/paper-improvement-cadence.json` for the active round's committed
experiment identity, seed, duration and due time. The October 2 user rule is
**one step forward, one step back with a two-hour floor**: a verified **WIN adds
exactly one hour** to the next round; a verified **LOSS subtracts exactly one
hour, never below two hours**. Thus losses take 4 → 3 → 2 hours; a loss at two
hours stays at two hours, and a win at two hours advances to three hours. A
**TIE keeps the duration**. An unavailable, late or carry-in result is explicitly
**UNSCORED and keeps the duration**; it cannot promote or incur a loss penalty.
Losses and ties still require evidence-backed improvement. Use
`ml.hyperliquid_paper_cadence` to assess and advance the durable scorecard; never
hand-edit history or infer an outcome from a green total.

Rule `win-plus-one-loss-minus-one-floor-two-v3` applies prospectively. Retain
historical v1/v2 assessments, the September 29 `adopt-loss-penalty` amendment,
and every already committed active seed/due under their recorded rule. For the
first v2 successor after this change, complete the normal verified lifecycle,
then run native `advance --adopt-two-hour-floor` with a unique change ID and the
expected ending/successor IDs. It revalidates the immutable v2 assessment and
accepted opening, writes a receipt-first v3 amendment, and clamps that successor
to at least two hours without rescoring, reseeding, restarting, or altering the
ending round. It refuses a late successor, incompatible identities, unresolved
review ownership, a pending/conflicting state, or a reused/tampered amendment.
Thereafter ordinary `status`, `assess`, and `advance` use the recorded v3 policy.

Each invocation completes at most one cycle. Before the active round's due
time, do not tune, archive or reset it. A duplicate assessment or completed
rollover cannot advance twice. Interrupted cycles resume from their recorded
stage and ID; never create a second archive or opening to hide an incomplete
transition. A user's pending real-account purchase/snapshot instruction takes
precedence: wait for their completion update before creating the new mirror.

Capture the ending comparison promptly at the committed deadline, before
prolonged research. Both portfolio endpoints must cover the full round and be
no more than one five-minute candle late; native observation-skew limits also
apply. A delayed app wake is not evidence of performance at the missed deadline.
The initial already-running experiment predates this ladder and is a carry-in,
not a claimed first-two-hour result. Its next verified fresh mirror starts the
first scored two-hour round. Never require a mid-round transient lead to count:
the result is the endpoint excess return after trading costs over that round.

A win requires a positive finite common-mark Paper equity edge and excess
return, plus the native `performance_comparable`, `mirror_baseline_verified`,
`zero_external_flows_verified` and `paper_beating_actual` checks. Deposits,
withdrawals, incomplete flow histories, stale observations, mismatched accounts
or seeds, and mere raw display gaps cannot produce a win. Keep full losing and
unscored history. One winning round earns the next challenge; it is not proof
of durable trading skill.

The earlier September 30 clarification introduced
`win_forecast_rule=qualified-in-round-forecast-v1` for the qualified-only rounds
accepted under that instruction. For those committed rounds, a positive balance
edge without a valid model prediction is not a WIN. Promotion requires at least
one finite, qualified, active forecast published and
observed in that same round, still valid when its Paper decision committed,
matching the accepted market/horizon and an independently retained eligible
model record. A valid policy hold qualifies; executing a trade is not required.
Missing, stale, unqualified or unprovable forecast evidence makes a positive
endpoint UNSCORED (`no_eligible_forecast_evidence`) and retains its duration.
Verified negative LOSS and zero-edge TIE remain scored normally. Historical
assessments and rounds without this prospective marker keep their recorded
meaning. Native comparison retains endpoint-bounded decision/model evidence
and hashes. Under either rule, publication alone is insufficient: Paper must
consume the valid forecast in-round. Newly accepted exploratory rounds use the
v2 admission rule stated above instead of this historical qualified-only rule.

1. Capture a read-only comparison using the native comparison command below.
   Record real and Paper equity per account and pooled, observation times,
   opening equity, returns, cash flows, fees, funding, turnover, drawdown,
   forecast coverage/qualification, and position attribution where available.
   Use the configured public owner identities for Alex, Jeremy and Clear Pond,
   the same account-mode accounting as Hyperliquid Duckets, and both native
   and common-mark valuations. Never use old screenshot amounts as live values.
2. Compare the same opening/end interval and account universe. Separate raw
   dollar gap from investment return, external deposits/withdrawals from P/L,
   and actual portfolio trading from an unchanged opening-inventory benchmark.
   Identify missing or truncated cash-flow history. Incomplete comparability
   must be explicit; do not assert alpha from raw account totals. Sequential
   provider requests are not an atomic snapshot.
3. On a loss or tie, investigate the material causes and make
   a concrete, evidence-backed improvement. Parameters, weighting, features,
   architectures, and new model families are authorized. If a model is removed,
   add at least one replacement in that cycle; the active model count must not
   shrink. Count the estimator members/families in the deployed bundle for each
   configured market/horizon, not successive retrained checkpoints or archived
   releases. A replacement must be included in that evaluated and published
   bundle with a positive ensemble contribution; a dormant research artifact,
   duplicate name, or zero weight does not satisfy replacement. A market's
   Qualified/Research status records model assessment, not model removal;
   current exploratory Paper admits valid forecasts from either status. Keep an
   inventory before and after. Investigate fees and turnover as well as
   probability losses. If comparisons are incomplete, repair collection
   and use available cost/signal evidence without inventing a relative score.
4. Test candidate changes in isolated research directories against a pinned
   incumbent and chronological data. Preserve 70/15/15 partitions, horizon
   purging, no future leakage, truthful qualification assessment, and executable quote/fee
   assumptions. Before fitting, record candidate recipes, source hashes,
   development/confirmation partitions, selection criteria and acceptance
   criteria, including relevant per-market regressions and cost/turnover evidence.
   Record all tried candidates, including failures. Use development folds for
   selection and a later confirmation block where feasible; lock the selected
   recipe before examining confirmation and do not substitute a runner-up using
   those outcomes. A failed trial can motivate a new explicitly declared trial,
   but retain its failure and label reused confirmation as development evidence;
   use a new later block or a clearly identified prospective Paper hypothesis
   instead of repeatedly claiming confirmation on the same observed outcomes.
   Disclose that repeatedly reused assessments are selection evidence, not an untouched
   final test. Do not optimize only for the last short round or promise profit.
   A reasonable paper trial can be labeled a prospective hypothesis when
   retrospective evidence is mixed; record why it is reasonable, its expected
   effect and what the next review should measure. Never describe it as proven
   improvement or silently relabel failed acceptance criteria as passing.
5. Run focused meaningful tests and verify configuration/model artifact
   compatibility. Retain a winning recipe for its next, longer challenge unless
   a concrete defect requires a documented fix. Unavailable comparisons call
   for collection repairs and a recorded hypothesis, never an invented loss/win.
   Preserve truthful model qualification; Paper signal admission follows the
   newer exploratory policy above. Do not expand leverage/exposure caps, reduce
   simulated fees, remove stops, or alter real execution to manufacture a win. Shared
   model code is also consumed by Powder: if Powder is active, do not hot-change
   its model namespace or policy; report the conflict and preserve the session.

## Archive and start the next experiment

Capture comparison evidence with a new output path:

```powershell
python -m ml.hyperliquid_paper_comparison --data-root C:/DATASTORE/hyperliquid --output <cycle-directory>/comparison.json
```

The collector calls the same Duckets portfolio service and retains sanitized
public source responses. Automatic relative-performance ranking requires a
verified mirror and complete empty external-flow histories. Nonempty, capped or
malformed histories make the automated ranking unavailable until their flows
are classified and reconciled; raw and common-mark equities remain evidence.

The staged lifecycle command is `python -m ml.hyperliquid_paper_review`.
Run `--help` for exact current arguments. Every stage uses one stable cycle ID;
evidence lives under `_operations/paper-improvement/<cycle-id>/`.

Read the round with `python -m ml.hyperliquid_paper_cadence status` and finalize
its captured endpoint with `python -m ml.hyperliquid_paper_cadence assess
--comparison <cycle-directory>/comparison.json`. A nonfinalizable early result
requires a fresh comparison after a committed Paper observation reaches due.
An immutable finalized assessment cannot be replaced with a more favorable one.
After native `complete`, run `python -m ml.hyperliquid_paper_cadence advance`.
The one-time `init` adopted the preexisting run as an unscored carry-in; do not
reinitialize the ledger to bypass existing history. All commands accept
`--root C:/DATASTORE/hyperliquid` (the default).

The reusable independent checks are:

```powershell
python -m ml.hyperliquid_paper_verify --stage opening --label opening --output-dir <cycle-directory>
python -m ml.hyperliquid_paper_health --expect prepared --label prepared-health --opening-report opening.json --output-dir <cycle-directory>
python -m ml.hyperliquid_paper_verify --stage trading --label first-cycle --opening-report opening.json --output-dir <cycle-directory>
python -m ml.hyperliquid_paper_health --expect trading --label trading-health --opening-report opening.json --output-dir <cycle-directory>
```

After a bounded follow-up, run health again with a new label and
`--previous-health trading-health.json` to require a newer committed observation.
These read-only checks never initialize a missing ledger. Use unique labels;
existing verification receipts are never overwritten.

1. `begin` records exclusive maintenance intent and pre-change provenance.
   Snapshot the actual incumbent configs/source before applying a selected
   production change. Research may proceed while the original Paper runs.
2. Request graceful stops through native `hyperliquid_paper_runtime --stop`
   and `hyperliquid_model_runtime --stop`. Verify both exact owner process
   trees exited and locks are available. Never force-kill a living fit, delete
   a lock, or restart around an ambiguous owner. Keep data running.
3. `archive` preserves the complete stopped `_paper` and `_models` trees under
   a unique `_paper_archives` path with a checked SHA-256 manifest, SQLite
   integrity result, prior experiment identity, and source/config provenance.
   Review the final stopped result in addition to the earlier comparison.
   Verify preservation before initializing any replacement namespace.
4. Apply the tested or explicitly user-directed change. Start the native model worker with the hidden
   independent Windows launch from [Operations Watch](OPERATIONS_WATCH.md).
   Require fresh coherent predictions for all configured slots. Valid Research
   forecasts may drive signals under the accepted exploratory Paper policy;
   preserve their Research label and native qualification result. Record estimator membership,
   config hashes, split cutoffs, qualification, and current artifact identities.
5. `prepare` creates the fresh mirror using public read-only account responses
   and the native prepare-only runtime. Verify every inherited signed quantity,
   cash/collateral calculation and historical perpetual entry against retained
   source responses. Require zero initial fills, fees, funding, transfers and
   experiment P/L. Preserve separate opening-mark stop references. Source open
   orders are not imported; surface them and refuse an unqualified 1:1 claim.
   Do not invent balancing cash to match quotations captured at different times.
   After independent opening verification, promptly re-anchor the existing app
   automation to the next round's duration. The installed scheduler computes
   its following wake before a review begins and an unchanged-rule update
   preserves that time; neither behavior gives the new seed a full round.
   Use full `automation_update` PAUSED then ACTIVE updates for this same task,
   with `FREQ=HOURLY;INTERVAL=N`, where `N` is the committed successor duration,
   and all other fields preserved. Do this
   preferably within 120 seconds of the seed (at most 180), before extended live
   follow-up. This leaves room for the ending public-account reads. The scheduler
   anchors to the current minute and can add up to 119 seconds of jitter; do
   not insert unsupported DTSTART/nextRunAt fields or edit app databases/TOML.
   Record the requested duration, activation time, seed and due time. If this
   handoff is delayed, disclose it rather than count a late endpoint as a win.
6. `accept` verifies and publishes the new durable accepted baseline while
   retaining maintenance. Save independent opening/accounting verification.
   Start Paper through the hidden launch. Verify exact identities, fresh
   upstream publications, a newer committed Paper valuation, and unchanged
   immutable opening. Independently replay the first fills, fees and funding.
   Startup risk reductions and their costs are separate from the zero-cost
   opening; do not reset again to erase them.
7. `complete` clears maintenance only after the health/advancement checks pass.
   Synchronize Operations Watch memory with the accepted-record path, experiment
   ID, opening balances, config hashes and verified worker identities. The watch
   uses this durable handoff, never a hard-coded historical seed. Preserve its
   existing schedule/model/notification settings when an app prompt update is
   necessary. It remains an operations monitor and never performs reseeding.

8. Advance the cadence helper using the immutable ending assessment and the
   newly completed accepted baseline. The next deadline is the **new seed time
   plus the newly assigned duration**, so tuning downtime does not consume the
   next round. Verify that the existing `hyperliquid-paper-improvement`
   automation was re-anchored promptly after opening verification, preserving
   GPT-6 Luna / Max, local project, enabled state and notification settings.
   Record the schedule handoff with the committed cadence state. If synchronization
   fails, preserve the accepted Paper run and pending cadence state and repair
   only the schedule; never reseed again to hide that failure. Operations Watch
   keeps its independent 30-minute schedule and never changes the ladder.

At a wake a few seconds before the due time because of minute rounding, wait
only the remaining bounded interval (at most 60 seconds per wait) and capture a
new comparison after the due time. Do not skip a nearly due round until another
whole hourly recurrence. A materially wrong schedule is a synchronization
failure, not authority to shorten the round or claim late performance.

If a stage fails, preserve its evidence and guard, identify the exact remaining
work, and repair or resume the same operation. Do not label an incomplete
opening running or let Operations Watch recover during maintenance. Never
silently roll back to a historical or excluded ledger. Avoid resetting healthy
workers merely because a forecast is Research or there was no trade.

## Output and next review

Write a dated audit with comparison amounts and their timestamps, fee/turnover
attribution, candidate evidence, exact chosen changes, before/after model
inventory, tests, archive verification, new opening, live worker verification,
and the next scheduled review. Link the machine-readable receipts. Preserve a
cumulative cycle index so resetting Paper never erases losing history. Keep
the automation memory concise and current. Return a readable result on each
completed round: duration, WIN/LOSS/TIE/UNSCORED, after-cost relative result,
changes, new opening and the next challenge/deadline. Surface failures requiring action.

This local scheduled task needs the PC and desktop app running, the project
available, and network/model capacity. See [official scheduled-task
documentation](https://learn.chatgpt.com/docs/automations). The schedule requests
work; it cannot guarantee unattended availability or profitable trading.
