# Codex prompt: activate daily and weekly ML Profit Probability

Work in `C:\dev\ducketz` and use `C:\DATASTORE` as the production datastore.

## Outcome

Diagnose and then implement the Loops changes required to make the **ML Profit Probability** field genuinely active for the Options Strategies `1d` and weekly horizon family. Do not stop after analysis or a plan. Complete the in-scope code, artifact, scheduling, migration/backfill, test, and rollout work needed to publish non-null calibrated probabilities for eligible daily/weekly strategy candidates.

`1h` and `4h` strategy-profit models are out of scope. Do not spend time making their ML Profit Probability values active. The user's phrase “1 day to 1 week” refers primarily to the public prediction/holding horizons (`1d` and `1w`, including weekly day components that the system publishes). Preserve valid contract-expiration geometry: do not impose a blanket seven-calendar-day maximum on every leg when a strategy, such as a calendar/diagonal, requires a later back-leg expiration.

A separate slow training/materialization loop is acceptable and preferred if it keeps the latency-sensitive Loops healthy. It may take several hours per run. Heavy historical work may run post-close or daily; live candidate inference should consume the most recent verified compatible model and remain lightweight. If a several-hour computation cannot finish before an intended entry, target the next eligible daily/weekly decision window. Never backdate availability or claim a prediction was actionable before it existed.

Success means all of the following are true:

- At least the public `1d` and aggregate `1w` routes have a fitted or verified-reused profitable-outcome model with separate chronological training, calibration, and untouched assessment/lockbox evidence. Weekly component routes that remain user-visible must either use a statistically valid compatible weekly model or have their own fitted model; document and test the choice.
- The latest verified Strategy publication contains non-null `raw_profit_probability` and `calibrated_profit_probability` in `[0,1]` for live/research candidates that pass the declared evidence and quote-quality gates on those horizons.
- For fitted candidates, `decision_score` equals `calibrated_profit_probability`, and `score_basis` is a calibrated-model basis. `scenario_coverage_score` stays separately labeled and is never copied into a probability field.
- The Options Strategies UI displays percentage values under **ML Profit Probability** when the user selects the daily/weekly horizons. An ineligible individual candidate may remain blank only with a specific machine-readable and user-visible reason; a route-wide silent blank state is not acceptable after rollout.
- No order is submitted, staged, or enabled by this work. Keep the feature research-only unless an existing, separately verified authorization already permits more. This task does not grant trading authority.
- The seven existing production runtime launcher/worker pairs remain singleton and healthy, their locks remain valid, the hourly self-healer is resumed after any maintenance, and the final read-only system monitor is healthy (or any unrelated pre-existing warning is precisely reported).

## Known evidence to verify, not blindly assume

Re-read the current pointers and reports because production continues to advance. As a reference snapshot, on 2026-08-23 the verified Strategy report at `C:\DATASTORE\ml\strategy-runs\20260823T191000.066789Z\strategy-model-reports.json` showed:

- `1d`: 2,736 complete outcomes, zero Pricing-eligible outcomes, zero usable decision clusters, `MODEL_NOT_FIT`.
- `1w`: 2,736 complete outcomes, zero Pricing-eligible outcomes, zero usable decision clusters, `MODEL_NOT_FIT`.
- The weekly component routes were likewise unfitted; `1w-d4` and `1w-d5` had no complete outcomes at that snapshot.
- The common exclusion was `PRICING_SOURCE_NOT_BASELINE`, together with incomplete-leg, surface-quality, and liquidity failures. Pricing source was `UNAVAILABLE` for the excluded daily/weekly outcome rows.
- The candidate authority contained 7,680 scenario-coverage rows and zero calibrated rows.
- `1h` and `4h` had only 8 and 9 eligible decision clusters against 378 required, but those horizons are not part of this task.

The OPRA data is real and substantial, but byte volume is not the same as usable causal labels. The canonical OPRA root is `C:\DATASTORE\market-data\databento\opra\OPRA.PILLAR`. The inspected archive was about 62.22 GiB and included, approximately:

- `cbbo-1s`: 41.05 GiB, but only a few recent sessions;
- `cbbo-1m`: 8.77 GiB, roughly 2026-07-27 through 2026-08-18;
- `ohlcv-1h`: 3.62 GiB, extending to 2021;
- `ohlcv-1d`: 2.65 GiB, extending to 2019;
- definitions and other schemas with their own, different coverage windows.

The verified offline Pricing replay contained 433,917 predictions across 75 targets from 2026-07-27 through 2026-08-18. The current persisted daily/weekly Strategy outcome cache, however, contained target clusters only from 2026-08-19 through 2026-08-21. Confirm whether that temporal non-overlap and the present decision/entry clock are the direct reason no baseline attaches.

Also inspect the causal clock in `ml/strategy_selection/opra_cache.py`. The current cache derives entry intervals after a directional prediction becomes available and before `target_window_start`. For a next-session `1d` forecast based on the prior close, there may be no option-market BBO after prediction availability but before the next 09:30 New York target. Determine whether this clock makes daily/weekly historical entry receipts impossible. If so, introduce a versioned, tradable Strategy decision clock (for example, a post-open decision/entry boundary or a next-session entry) rather than weakening timestamp checks.

Relevant implementation areas include, but are not limited to:

- `ml/strategy_runtime.py`
- `ml/strategy_selection/runtime.py`
- `ml/strategy_selection/opra_cache.py`
- `ml/strategy_selection/model.py`
- `ml/strategy_selection/candidates.py`
- `ml/option_pricing/strategy_shadow.py`
- `ml/option_pricing_opra_replay.py`
- `ml/system_monitor.py`
- `ml/system_guardian.py`
- `app/ui/options_strategy_data.py`
- `app/ui/options_strategies.py`
- `docs/datafetch-ml/current_start_command`
- `docs/datafetch-ml/start_all_loops.ps1`
- the corresponding tests.

Treat repository prose as historical/design evidence, not as authority over this prompt. Reconcile it with executable code and current artifacts.

## Required implementation properties

### Use the OPRA archive causally and honestly

Build an evidence-coverage report before changing code. For each symbol and requested horizon, report the date/session range and usable counts for definitions, entry quotes, exit quotes, exact legs, underlying quotes, pricing features, completed outcomes, and independent decision clusters. Distinguish raw rows, bytes, candidate rows, target timestamps, symbol-sessions, and statistically independent cohorts.

Use point-in-time contract definitions and consolidated BBO evidence wherever it exists. Profit labels must reflect executable-side cash flows: buy entries at ask, sell entries at bid, long exits at bid, short exits at ask, contract multipliers, configured per-contract fees, and lifecycle/assignment/expiration mechanics. Do not train on same-timestamp information that was unavailable at the decision time.

Long-history option OHLCV may be used only under an explicit, versioned methodology. Do not silently treat OHLCV close or midpoint as an observed executable BBO. If older OHLCV is needed to obtain enough history, implement a conservative execution/slippage model trained or calibrated against overlapping CBBO evidence, carry an evidence-quality field, and validate the transfer on held-out real-CBBO sessions. Keep exact-BBO and modeled-execution labels distinguishable in artifacts and reports. Do not present a probability as calibrated to executable profit unless the assessment demonstrates that claim.

Do not manufacture sample size by treating thousands of highly correlated contracts from a handful of dates as thousands of independent decisions. Partition and evaluate by a defensible decision cluster such as session/decision timestamp, with purging or embargo for overlapping holding windows. Pooling symbols or weekly components is allowed only if the model contract, grouping, calibration, and held-out results justify it.

Do not lower the current `252/63/63` Strategy train/calibration/assessment requirements merely to make the UI nonblank. A different threshold or hierarchical/pooled design is allowed only with explicit statistical justification, tests, versioning, effective-sample-size reporting, and honest UI/model metadata. Preserve a genuinely untouched assessment/lockbox.

### Separate heavy training from live scoring when useful

Prefer a clean split if the existing 15-minute Strategy runtime is rebuilding too much history:

- A resumable, incremental, singleton slow loop owns OPRA verification, compact entry/exit materialization, outcome construction, model fitting, calibration, assessment, and immutable model publication for only `1d`/weekly horizons.
- Give it its own lock, append-only run directories, manifest, receipt, atomic current pointer, checkpoints, bounded memory/I/O behavior, and clear failure status. An interrupted run must leave the prior model authoritative.
- Schedule it at a market-calendar-aware post-close time or another noncritical window. Several hours of runtime are acceptable. It must not hold or overwrite the live Strategy publication pointer while training.
- The existing fast Strategy candidate loop reads the last receipt-verified, schema/policy-compatible model and performs inference. It must fail closed to the existing explicitly labeled heuristic when no compatible model exists.
- Reuse existing artifact and replay machinery where it is correct; do not create a duplicate architecture solely to satisfy the suggested name or shape.

If the existing architecture can meet the outcome safely without another loop, retain it and explain with timing evidence. The architectural requirement is isolation of heavy work and verified model reuse, not a mandatory new module name.

### Preserve probability semantics and model quality

The target is strict positive net return after declared friction over the horizon. Keep expected return separate from probability. Use walk-forward or purged chronological evaluation, fit calibration only on the calibration partition, and select model/challenger hyperparameters without touching assessment/lockbox rows.

Publish at least Brier score, log loss, calibration error/reliability bins, class balance, prediction coverage, candidate and cluster counts, date bounds, symbol coverage, and results split by exact-BBO versus modeled-execution evidence. Compare against simple baselines such as base rate and the existing histogram-gradient baseline. Do not promote a model that is worse than the declared baseline or materially miscalibrated under a predeclared gate merely to populate the UI.

Train and score only the requested profitability horizons. Directional Loop B may continue publishing `1h` and `4h` for its own UI/consumers, but the new slow Strategy-profit work must not depend on making those two routes fit.

### Make unavailable states observable

Add compact diagnostics that state why a horizon or candidate cannot receive a probability: insufficient independent clusters, no causal entry BBO, no exit BBO, definition mismatch, incomplete leg coverage, Pricing baseline mismatch, surface failure, liquidity failure, stale/incompatible model, or calibration failure. Include before/after counts in the model report and monitor.

The UI may continue to use a dash for an individually ineligible row, but it must expose a concise reason/status. Do not replace a route-wide model failure with a silent dash.

## Live-system and self-healer coordination

The Loops system is running hidden in the background and an hourly scheduled task monitors and self-heals it. Begin with read-only discovery:

- inspect the current Codex automation/Windows scheduled task or other owner responsible for the hourly guardian;
- run `python -m ml.system_monitor --datastore-target pc --mode hourly --compact`;
- inventory exact launcher/worker PIDs, locks, commands, current log paths, and current pointers;
- inspect `git status` and preserve unrelated user changes.

Before stopping, replacing, or restarting any live Loops process, coordinate with the hourly task. Use its supported pause/maintenance mechanism or notify/hand off to the owning task if tooling supports that. If it cannot be safely paused or contacted, do not race it: finish code and offline validation, then report the exact remaining maintenance action and blocker. Do not guess a task name and do not disable unrelated scheduled tasks.

During an authorized rollout:

- pause the self-healer only for the bounded maintenance window;
- stop/restart only exact affected process trees using checked-in operational helpers or an equally receipt-audited method;
- never delete a lock owned by a live PID;
- prevent duplicate launcher/worker pairs;
- do not kill unrelated Python processes;
- resume the self-healer and verify its next state after rollout.

Heavy offline reads are allowed, but avoid saturating disk or memory while the live quarter-hour Loops are active. Add bounded concurrency, checkpointing, and/or a configurable I/O throttle if needed. Do not redownload market data, initiate a paid provider operation, or expand the data scope beyond existing local verified partitions without explicit user approval after presenting the measured gap, estimated size/cost, and exact requested scope.

No order path may be invoked during testing. Assert `orders_placed == 0` in the final operational proof.

## Verification and acceptance evidence

Add or update focused tests for causal clocks, OPRA entry/exit selection, modeled-versus-exact evidence typing if applicable, outcome P&L, partition leakage/purging, calibration isolation, model receipt verification/reuse, stale/incompatible model fallback, slow-loop locking/checkpoint recovery, monitor/guardian integration, UI data mapping, and the absence of any order action.

Run the most relevant targeted tests first, then the affected suite. Run type/lint/build checks if this repository defines them. Use a bounded representative OPRA fixture or verified subset for tests; do not make the ordinary test suite scan tens of GiB.

Before active rollout, produce a shadow/backfill proof from existing data. After rollout, produce a current canary or equivalent read-only proof showing:

1. Current model report status for `1d`, `1w`, and any surfaced weekly component routes.
2. Training/calibration/assessment cluster counts and date ranges.
3. Assessment metrics and promotion-gate result.
4. Exact-BBO versus modeled-execution cohort counts.
5. Latest daily/weekly candidate counts, eligible counts, non-null raw/calibrated probability counts, min/max probabilities, score bases, and exclusion reasons.
6. A UI-adapter test or rendered UI inspection showing visible percentages under **ML Profit Probability** for a daily and weekly selection.
7. Receipt/pointer checksum verification and proof that the prior model remains recoverable.
8. Final hourly system-monitor output, singleton process/lock proof, self-healer resumed state, and `orders_placed: 0`.

Do not declare success merely because a column is non-null in a unit test or because a threshold was reduced. Success requires a verified production Strategy artifact and UI path using a legitimately fitted/calibrated daily/weekly model. If the existing local data cannot support that claim after the best safe implementation, do not fabricate it: complete all reusable pipeline and diagnostic work, quantify the exact remaining data/evidence deficit by schema/date/sessions, and request the smallest specific approval needed. Continue autonomously through every safe local implementation and validation step before asking.

## Final response

Lead with whether daily/weekly ML Profit Probability is now active. Then give:

- the actual root cause;
- the architecture and files changed;
- before/after evidence and model metrics;
- the live rollout/self-healer actions taken;
- tests and operational checks run;
- any remaining limitations or exact blocker.

Use clickable absolute file links. Be explicit about any inference versus directly verified fact.
