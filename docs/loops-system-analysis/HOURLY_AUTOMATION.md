# Hourly Loops automation contract

This checked-in file is the complete durable instruction set for the
`loops-hourly-operations` standalone scheduled task. At the start of every
fresh scheduled chat, read this file completely before running a production or
workflow command. If it is missing, unreadable, or materially contradictory,
fail closed and perform no production action.

## Fresh-chat continuity protocol

Each scheduled run starts in a new chat. Continuity is carried only by the
checksum-verified advisory handoff chain and the authoritative Loops receipts;
never assume that a prior chat's implicit context is available.

1. Before the guardian or any mutation, run exactly one handoff read:
   `.\.venv\Scripts\python.exe -m ml.scheduler_handoff read --datastore-target pc --compact`.
   Parse its stdout as JSON even when it exits 2.
   - `EMPTY` is valid for the first standalone run.
   - `VALID` supplies an advisory summary, actions already attempted, evidence
     paths, changed files, and next bounded action. Revalidate every item
     against live process state, the current guardian schedule, and
     checksum-valid production receipts before relying on it.
   - `INVALID` or `ERROR` is a scheduling-continuity incident. Preserve the
     output, still run the single guardian baseline and report current health,
     but do not resume an unfinished repair, experiment, or mutation from the
     handoff and never guess or repair the pointer by hand.
2. Execute the production workflow below. A handoff never overrides
   `schedule.monitor_mode`, `schedule.lane`, `schedule.overnight_stage`, live
   ownership/lock evidence, or a verified publication receipt. Never repeat an
   action merely because it appears in `next_action` or the prior action list.
3. After all work and verification, but before the final response, commit
   exactly one successor handoff and perform no further production mutation:

   `.\.venv\Scripts\python.exe -m ml.scheduler_handoff commit --datastore-target pc --wake-id <guardian-checked-at-or-run-start-UTC> --monitor-mode <mode> --lane <lane> --stage-id <stage-or-NONE> [--stage-index <index>] --eligible-session <session-or-NONE> [--checked-at <guardian-checked-at>] --final-status <status> --stage-disposition <disposition> --incident-status <status> --summary <compact-summary> --next-action <one-bounded-next-action> [--action <completed-or-attempted-action>] [--evidence <exact-path>] [--changed-file <exact-path>] --compact`.

   Use the guardian `checked_at` as `wake_id`; only when it is unavailable, use
   the UTC run-start timestamp captured before the guardian. This key makes a
   repeated same-wake commit return `ALREADY_COMMITTED` without advancing or
   replacing the immutable chain. Use the exact parsed schedule/result values;
   use `UNKNOWN` only where the guardian output is missing or malformed. Keep
   `summary` within 2,400
   characters and `next_action` within 1,200, include every material action and
   useful receipt/report path with repeatable flags, never include secrets, and
   keep `orders_placed=0`. Parse and report the commit result. A failed commit
   leaves the prior pointer authoritative and is an unresolved
   scheduling-continuity incident; do not retry by manually editing files.

Lead the final response with the new handoff sequence and receipt path in
addition to the health/schedule fields required below. This advisory handoff
exists only to resume bounded work across fresh chats; guardian metadata and
verified production receipts remain the sole operational authority.

## Production workflow

Operate the production Loops health workflow from C:\dev\ducketz. On every wake, run exactly one guardian command: `.\.venv\Scripts\python.exe -m ml.system_guardian --datastore-target pc --mode scheduled --repair-liveness --compact`. Parse stdout as JSON even when it exits 2. Never run the guardian twice in one wake.

Deployment cadence contract: Directional Loop B's canonical owner runs every
30 minutes at UTC-clock phase `:05`/`:35`, permits exactly one retry only for a
classified transient failure after 60 seconds, and performs an immediate
startup cycle only when its verified authority age reaches 35 minutes. Its
freshness clock is the receipt's `promoted_at`: WARN after 35 minutes and FAIL
after 45. The `:35` cycle may legitimately still be computing at this task's
`:42` wake; the prior receipt remains authoritative meanwhile. Do not restart,
duplicate, or classify B as stale merely because that scheduled cycle is in
flight—use the canonical process/lock/log evidence and the monitor's
receipt-based freshness result. Deadline, integrity, and deterministic
contract failures never authorize an automatic retry or pointer rewrite.

After parsing the guardian result, require the top-level `schedule` object with schema `loops-overnight-accuracy-schedule-v1`. Treat `schedule.monitor_mode`, `schedule.lane`, and `schedule.overnight_stage` as the sole routing authority; do not infer a stage from the clock or catch up a missed stage. Missing, malformed, or contradictory schedule metadata is a scheduling incident: preserve the guardian evidence, do not guess, and do not begin accuracy work. `STANDARD_OPERATIONS` means there is no overnight stage. `OVERNIGHT_ACCURACY` means execute at most the one named stage after health is proven. Every stage has a 45-minute ceiling and must checkpoint or defer before the next wake; never overlap stages or start a later stage early.

The bounded shadow challenger command belongs only to stage `run-shadow-ablation`: `.\.venv\Scripts\python.exe -m ml.strategy_value_challenger --datastore-target pc --if-changed --compact`. At that stage run it at most once, parse stdout as JSON, and accept only `COMPLETE_SHADOW_ONLY` or `UNCHANGED_SKIPPED`, with `promotion_performed=false`, both production-mutation fields false, `orders_enabled=false`, and `orders_placed=0`. Preserve its immutable receipt path, source fingerprint, decision, and 1d/1w gate status. At every other stage, do not execute this command; inspect only the existing checksum-valid shadow receipt surfaced by the monitor. A failure, invalid receipt, or source fingerprint stale beyond monitor cadence is an incident, but it never authorizes changes to production candidates, model authority, feature policy, the options prediction loop, or any order path.

Classify the guardian baseline before deciding whether the scheduled accuracy stage may run. A blocking health incident is any FAIL, or a WARN involving runtime ownership/processes/locks/log liveness, provider authority or freshness, publication/receipt/checksum integrity, causal data availability, cross-loop lineage, storage, UI value parity, or order safety. A top-level `DEGRADED` result caused only by explicitly report-only prediction/model/evaluation findings may proceed to the scheduled evidence stage when every operational and contract check passes; preserve those WARNs as stage inputs and never relabel the overall result HEALTHY. The wake is fully healthy only when top-level status is `HEALTHY` and no check is WARN or FAIL. A live process alone is never proof of healthy data or predictions. Use the selected `mode` and `monitor.market` target so off-hours freshness means the latest eligible completed market boundary, not a future bar.

For every run, verify and report:
- exact owner/worker/lock health for all eight allowlisted runtimes;
- a fresh, zero-failure Loop A complete cycle and current six-symbol bar-readiness evidence;
- canonical operational equity OHLCV lineage from Databento EQUS.MINI (Schwab may be quote, option-chain, broker, or explicitly labeled fallback evidence, but never silently canonical OHLCV; XNAS.ITCH remains a separate cold archive);
- fresh verified Directional Loop B predictions for the configured 1h, 4h, 1d, and 1w routes;
- fresh receipt-verified promoted 1d/1w Strategy profit-model authority, including assessment promotion gates and `orders_placed=0`;
- current checksum-valid Options Capture, Active Pricing, Strategy, CME, and ALFRED publications;
- cross-loop lineage, both UI contracts including authoritative value/scale parity, and storage health.

Pooled sequence encoder and Loop C shadow lane:

- Treat `sequence_encoder_loop_c` as a required monitor inventory item on every
  wake. `NOT_PUBLISHED` is informational before the first approved shadow
  generation. Once either pointer exists, require its manifest, receipt,
  checksums, causal timestamps, `SHADOW_ONLY`/`OBSERVE_ONLY` authority,
  `automated_action_allowed=false`, `orders_enabled=false`, and
  `orders_placed=0`. An invalid published pointer is a contract warning and
  never authorizes a pointer repair, model fallback, ranking change, or order.
- The ordinary hourly path is inference-only. When the guardian baseline has
  no blocking incident, XNYS is open, a verified sequence-model publication
  already exists, and the current Loop B source generation changed, run at
  most once:

  `.\.venv\Scripts\python.exe -m ml.sequence_encoder.inference_runtime --datastore-target pc --information-cutoff <exact-current-Loop-B-causal_input_cutoff> --run-timestamp <current-UTC> --require-market-open --compact`.

  Parse the JSON. Accept only `READY_SHADOW`, `PARTIAL_SHADOW`,
  `UNCHANGED_SKIPPED`, or `MARKET_CLOSED_SKIPPED`, with
  `orders_enabled=false` and `orders_placed=0`. Preserve the immutable run,
  manifest, and publication receipt when one was produced. Do not run training
  in this path, and do not run inference while a verified current model is
  absent or the Loop B cutoff is missing/invalid.
- Hourly adaptation means fresh inference plus receipt-first calibration,
  maturity, drift, and lineage monitoring; it does not mean refitting or
  retuning on each wake. Outcomes for `1h`, `4h`, `1d`, and `1w` mature on
  different clocks, and overlapping hourly rows are not independent evidence.
  Keep pooled-encoder changes inside the stage-13 preregistration and stage-14
  bounded shadow-ablation path below. Do not run the separate weekly Loop C
  review command from this hourly task.
- Loop B and Options Strategies may consume the same verified distributions
  only through their checked-in shadow-report seams. Missing, partial, future,
  or invalid sequence evidence must remain visible and must not delay Loop B,
  change either production model/ranking, or acquire action authority. The
  intended causal order is current Loop B publication, sequence shadow
  inference, subsequent Strategy consumption, then optional Loop C observe
  evaluation; independently scheduled owners may consume the latest prior
  verified shadow generation without pretending it is same-cycle evidence.
- Loop C remains observe-only. Run it only during an open XNYS session and only
  when all four operator-approved inputs exist: an exact model-bound versioned
  risk record,
  a fresh reconciled portfolio snapshot, a fresh reconciled broker snapshot,
  and a halt-control snapshot. Look only at
  `C:\DATASTORE\controls\loop-c\current\risk-approval.json`,
  `portfolio-snapshot.json`, `broker-snapshot.json`, and `halt-control.json` in
  that same directory. The strict `ml.loop_c.inputs` contracts require an
  unexpired explicit `APPROVED` observe-only risk record whose model name,
  policy, semantic configuration fingerprint, distribution schema, authority,
  consumer, and ordered `1h`/`4h`/`1d`/`1w` horizon set match the current
  sequence publication exactly; horizon-specific predictive gates; fresh read-only,
  reconciled portfolio and broker state bound to checksum-valid datastore
  source receipts; and an independently issued unexpired halt control. At the
  start of each eligible open-session wake, before input validation, invoke the
  checked-in `ml.loop_c.schwab_snapshot --datastore-target pc --compact` once.
  This is the only authorized automated account refresh: it reuses the Schwab
  Duckets integration and may call only read-only account, working-order,
  bounded order-history, and transaction-history methods. Require its receipt
  to state `broker_data_http_methods=[GET]`, no persisted account/order/
  transaction identifiers, and zero orders. Never call any submit, replace,
  cancel, transfer, or other broker mutation method. Do not run
  `ml.loop_c.risk_proposal` hourly or rewrite an approved limit from changing
  balances/history; that command creates operator-review material only.
  Never invent limits,
  cash, exposure, positions, working orders, reconciliation, approval, or halt
  state. If any input is absent, stale, or invalid, record
  `LOOP_C_INPUTS_UNAVAILABLE` and skip without publishing. If all inputs are
  present, run `ml.loop_c.runtime` at most once after sequence inference. Start
  this observe-only operation immediately on the first eligible open-session
  wake; there is no evidence waiting period before it may compute. Its only
  acceptable authority is `OBSERVE_ONLY`; it contains no broker submission path
  and every output must keep zero-order safety.
- Pooled encoder training is eligible only inside
  `run-shadow-ablation`, only when stage 13's immutable preregistration names
  this exact challenger and supplies its causal cutoff, cohort, compute bound,
  metrics, and stop conditions. Training must use
  `ml.sequence_encoder.runtime`, chronological train/calibration/assessment
  partitions, equal decision-cluster weighting, causal next-state pretraining,
  and shadow publication. When stage 13 selects this challenger, encode one
  canonical sorted compact JSON object using schema
  `pooled-causal-sequence-preregistration-v1` and exactly these fields:
  `schema_version`, `challenger`, `authority`, `eligible_session`,
  `source_loop_b_run_path`, `source_loop_b_manifest_sha256`,
  `source_loop_b_samples_sha256`, `source_loop_b_predictions_sha256`,
  `causal_input_cutoff`, `symbols`, `maximum_sessions_per_symbol`,
  `configuration_fingerprint`, `hypothesis`, `primary_metric`, `safety_metrics`,
  `baseline`, `compute_bound`, `leakage_and_regime_risks`, `stop_conditions`,
  `rollback_condition`, `orders_enabled`, and `orders_placed`.
  Freeze the current receipt-verified Loop B generation and exact source
  checksums; set `challenger=pooled-causal-sequence-encoder`,
  `authority=SHADOW_ONLY`, `orders_enabled=false`, and `orders_placed=0`.
  Generate, do not hand-assemble, this object with the checked-in read-only
  proposal builder:

  `.\.venv\Scripts\python.exe -m ml.sequence_encoder.preregistration_proposal --datastore-target pc --eligible-session <guardian-eligible-session> --symbol <each-exact-symbol> --maximum-sessions-per-symbol 252 --compact`.

  Accept only `PROPOSAL_ONLY` with zero-order safety and copy its
  `handoff_actions` verbatim into the stage-13 successor receipt.
  Split the canonical UTF-8 string into ordered handoff actions named
  `SEQUENCE_PREREG_CANONICAL_<n>_OF_<count>=<fragment>` and include
  `SEQUENCE_PREREG_SHA256=<sha256-of-the-unsplit-string>`. Stage 14 must pass
  that still-current stage-13 receipt using `--preregistration-receipt`; the
  trainer rejects stale receipts, changed source authority, checksum drift, and
  any runtime argument mismatch before fitting. It atomically consumes that
  fingerprint once before fitting; failure or interruption is terminal evidence
  for the preregistration and does not authorize an automatic retry. No
  ordinary wake may train it, and no scheduler wake may promote it to active
  authority. Loop B and Options Strategies may begin their existing verified
  shadow consumption immediately after the first valid publication; no
  additional time embargo applies.
- First-publication bootstrap is an explicit deployment objective, not an
  open-ended model-shopping exception. While
  `sequence_encoder_loop_c.sequence_status=NOT_PUBLISHED`, treat the missing
  pooled encoder as the next eligible stage-13 bottleneck and preregister this
  exact checked-in challenger, provided the baseline is healthy and there is no
  current or terminal consumed attempt for its exact configuration fingerprint.
  For this bootstrap only, use the guardian's exact normalized watchlist and a
  frozen bound of `maximum_sessions_per_symbol=252` (one nominal trading year
  per symbol); do not expand the cohort or data bound during stage 14.
  Never displace an already-started handoff stage, bypass the guardian schedule,
  catch up a missed stage, or retry a consumed/failed fingerprint. After the
  first checksum-valid `SHADOW_ONLY` publication, this bootstrap rule is spent
  and stage 13 returns to evidence-ranked bottleneck selection.
- Loop C's option paper lane may select only `1d` and `1w` Strategy candidates.
  The pooled encoder remains bound to all four directional horizons because it
  is shared with Loop B, but `1h` and `4h` option candidates are explicitly
  ineligible and receive `OPTIONS_SHADOW_HORIZON_BELOW_1D`. Loop C continues to
  have no options broker path.
- Start the Loop C prospective evidence clock at its first successfully
  published open-session observe run, not at code deployment, risk approval, or
  sequence training. Continue running Loop C every eligible hourly wake while
  inputs remain valid. `loop-c-options-1d-plus-observe-evidence-gate-v2` allows only an operator
  review after all floors pass: 40 completed XNYS sessions, 30 mature 1d
  independent clusters, eight non-overlapping weekly
  cohorts, 20 reconciled observations, two halt drills, one rollback drill,
  all declared calibration/coverage/stress/stability/integrity/paper-broker
  gates, zero deterministic-gate violations, and zero orders. Before those
  floors, report `OBSERVE_ONLY_EVIDENCE_ACCUMULATING`; afterward report at most
  `ELIGIBLE_FOR_OPERATOR_REVIEW`. Never infer authority expansion or automatic
  promotion from elapsed time, evidence, a favorable proposal, or an approved
  observe-only risk file.
- This lane never supersedes the checksum-valid scheduler handoff. In
  particular, if the handoff says the prior stage-14 experiment is awaiting
  `compare-challenger`, perform that exact stage-15 comparison; do not replace
  it with a new encoder run, repeat stage 14, or advance the handoff out of
  sequence.

If the baseline has a blocking health incident, treat the wake as an incident and continue working rather than merely summarizing it; defer the accuracy stage. Preserve the guardian JSON and relevant log, receipt, pointer, and publication paths. Identify the producing runtime and the first bad target/boundary. Inspect process command lines and parent/worker PIDs, exact locks, recent stdout/stderr, provider availability and errors, configuration, latest Loop A cycle/bar receipts, Databento source lineage, and prediction/publication receipts. Distinguish process health from publication health and diagnose the root cause before mutating anything. Report-only accuracy/model WARNs with intact operational contracts are not liveness incidents and do not authorize a restart, pointer change, or gate weakening; route them into the named overnight evidence stage.

Use this bounded repair ladder:
1. Honor and verify any one-runtime liveness repair already performed by the guardian. Do not improvise a second guardian run.
2. For a transient or stuck producer, prove exactly one affected runtime and an allowlisted canonical command, then make the smallest bounded repair that uses the existing overlap/idempotency contracts. If a restart is required, stop only that verified runtime tree, start it through the checked-in canonical launcher/allowlist, and record before/stopped/after PIDs plus exact-lock handling.
3. For a code or configuration defect, preserve unrelated work, make the smallest root-cause patch, run focused tests or a direct repro, restart only the affected verified runtime when needed, and wait for a genuinely new eligible publication.
4. Finish with one read-only verification command using `.\.venv\Scripts\python.exe -m ml.system_monitor --datastore-target pc --mode <selected guardian mode> --compact`. Call the incident resolved only if that report is HEALTHY with no WARN/FAIL and its receipts prove fresh Databento bars and new/current predictions.

Fail closed instead of taking broad action when ownership is duplicated or ambiguous, multiple runtimes are wholly missing, the system appears intentionally shut down, credentials/entitlements/capacity block progress, an integrity or checksum failure lacks a proven repair, or a safe bounded fix cannot be established. Never run bulk history downloads, cold starts, OPRA bootstraps, unbounded backfills, broad datastore cleanup/deletion, authority-pointer rewrites, model promotion, or any order path. Do not expose secrets. Do not discard or overwrite unrelated work. Report the exact blocker and next bounded action when unresolved.

Lead the update with selected monitor mode, schedule lane, stage ID/index (or NONE), eligible session, final status, checked-at time, incident/remediation status, and whether fresh Databento fetching and fresh predictions were proven. List every initial and final WARN/FAIL with exact summaries and useful evidence. State the stage disposition and all receipt/report/screenshot paths used. For a repair, report root cause, files/config changed, tests, PIDs, lock handling, new receipt/publication paths, and verification. Keep healthy unchanged wakes compact so the task history remains useful.

Overnight accuracy pipeline:
Run this lane only when `schedule.lane=OVERNIGHT_ACCURACY`, no blocking operational/contract incident exists, and the scheduled stage's prerequisites are present. Bind all evidence to the selected completed XNYS session, immutable source fingerprints, model generations, prediction vintages, and publication receipts. A blocking operational/contract incident preempts the stage; a report-only model-quality warning becomes evidence for it. Unchanged fingerprints, an already-completed receipt, or immature outcomes produce an explicit verified skip rather than repeated work. End each stage with one disposition: `COMPLETED_EVIDENCE`, `UNCHANGED_VERIFIED_SKIP`, `INSUFFICIENT_MATURE_OUTCOMES`, `REPAIR_APPLIED`, `PROPOSAL_ONLY`, `DEFERRED_HEALTH_INCIDENT`, or `BLOCKED`.

Execute exactly the stage named by the guardian:
1. `seal-market-session` (13:42 PT): seal final bars, prediction and options publications, target boundaries, model generations, and fingerprints; reject mixed-session evidence.
2. `audit-input-quality` (14:42): score expected-versus-received coverage, gaps, duplicates, ordering, timestamp/session alignment, schema/unit stability, null and missing-reason coverage, causal availability clocks, lineage, latency, and freshness against comparable trailing 5- and 20-session baselines when available.
3. `audit-ui-output-parity` (15:42): require the hourly `ui_contracts` value-parity details to pass. Compare every Rolling Forecast route and Options Strategy candidate ID, label, timestamp, status, probability, percentage scale, expected value, and Scenario-Coverage-versus-ML meaning with the exact receipt-valid Parquet outputs. Run focused UI adapter/presentation tests. If a Duckets desktop window is already open, inspect the Rolling Forecasts and Options Strategies tabs at the canonical 1180x760 layout using a read-only Windows screenshot workflow; check missing/stale/NaN values, misleading empty/error states, clipping, overlap, truncation, contrast, spacing, scroll discoverability, and whether visible values match the publications. Do not launch or close the app merely for a screenshot, authenticate, sync accounts, expose identifiers, or touch any order control. If the app is closed, record `NOT_OBSERVED_APP_CLOSED`; this is not a backend failure when value parity passes. A visual or adapter defect may receive only the smallest tested presentation/data-adapter repair, with no model, publication, broker, or order mutation.
4. `evaluate-directional-1h` (16:42): evaluate causally mature 1h coverage, abstention/missing reasons, calibration/reliability, Brier score, log loss, hit rate, ranking, and symbol/regime cohorts.
5. `evaluate-directional-4h` (17:42): perform the same assessment for 4h and isolate horizon-specific failure patterns.
6. `evaluate-directional-1d` (18:42): assess prior mature 1d vintages for calibration, coverage, drift, ranking, and regime errors without scoring the current immature target.
7. `evaluate-directional-1w` (19:42): assess weekly vintages only with sufficient independent mature outcomes; otherwise report insufficient evidence.
8. `audit-cross-horizon-coherence` (20:42): explain material 1h/4h/1d/1w disagreements through horizon definitions, inputs, regimes, and vintages without forcing distinct horizons to agree.
9. `audit-options-inputs` (21:42): audit chain completeness, quote age, spreads, strikes, expirations, Greeks availability clocks, liquidity evidence, and explicit missing reasons.
10. `audit-pricing-execution` (22:42): audit Pricing coverage, conservative fills, fees, slippage, execution haircuts, surface/liquidity policy, and outlier valuations.
11. `evaluate-strategy-outcomes` (23:42): evaluate causally mature exact 1d/1w options constructions for positive net return after modeled execution and fees, by symbol, strategy family, pricing eligibility, model generation, and regime.
12. `audit-probability-calibration` (00:42): compare raw and calibrated probability distributions, reliability bins, ECE, Brier score, log loss, base rates, coverage, and probability collapse; keep Scenario Coverage non-probabilistic.
13. `select-nightly-bottleneck` (01:42): if the one-time first-publication bootstrap above is eligible, select and preregister the pooled causal sequence encoder; otherwise rank the single highest-value unresolved accuracy bottleneck. Preregister exactly one hypothesis, exact eligible cohort and causal cutoff, primary metric, safety metrics, baseline, checked-in gates, leakage/regime risks, compute/data bound, and rollback condition.
14. `run-shadow-ablation` (02:42): run the session's sole new bounded, isolated, shadow-only experiment using the preregistration and immutable inputs. Prefer the checked-in strategy-value challenger when it matches the bottleneck; when the preregistration explicitly selects the pooled sequence encoder, invoke its checked-in shadow trainer with `--preregistration-receipt <the-current-stage-13-receipt>` and preserve its model/calibration/distribution receipts. The trainer must validate the current handoff and use the receipt-bound cutoff, symbols, source checksums, configuration fingerprint, and data/compute bound; do not reconstruct or substitute them. Otherwise a small offline harness is allowed, but it must remain disconnected from production authority, runtime ownership, UI ranking, and orders.
15. `compare-challenger` (03:42): compare the exact immutable challenger from stage 14 against champion/baseline on identical chronological assessment-clean evidence. Do not retune, select a new cohort, or start another challenger.
16. `stress-and-gate-review` (04:42): stress the same result across symbols, regimes, windows, missing-data conditions, fees, and execution assumptions; accept, reject, or produce an approval-gated proposal. A favorable slice or post-hoc threshold is not a pass.
17. `preopen-freeze` (05:42): perform final health, rollback, receipt, and authority verification; summarize the overnight evidence and remaining trigger; prohibit new experimental or production change.

Across stages, preserve prediction vintage and model generation, evaluate labels only after causal maturity, keep chronological train/calibration/assessment separation and independent-cluster requirements, and never pool incompatible definitions or treat overlapping rows as independent. Prioritize upstream data correctness before model tuning. Never optimize accuracy while degrading calibration, coverage, execution cost, safety, or realized net return. Robust changes require representative evals and predeclared stop/retry limits, not more tool calls.

Also summarize both UI contracts, live-label maturity, Strategy model/Pricing coverage, and the Pricing-to-Strategy canary. Scenario Coverage is a heuristic grid pass fraction, never a probability; Calibrated Probability stays null until a fitted causal model and its declared evidence gate passes: full eligible Pricing for BSGP/Black-Scholes, or quality-passing OPRA execution evidence for the promoted daily/weekly model.

In weekly mode, include the immutable-evidence roll-up only where definitions match and both periods have sufficient independent observations; otherwise report INSUFFICIENT_WEEKLY_EVIDENCE or INCOMPATIBLE_WEEKLY_DEFINITIONS without manufacturing a trend. Review the prior comparable daily/weekly evidence, identify persistent versus one-session issues, rank the single highest-value unresolved bottleneck, retire failed or stale shadow ideas, and state the next evidence trigger. Do not repeatedly tune against the same assessment or ever open the sealed lockbox.

The user-facing Loop C portfolio meeting is owned by a separate Saturday task
whose durable contract is `docs\loops-system-analysis\WEEKLY_REVIEW_AUTOMATION.md`.
This hourly task may supply immutable receipts to it but must not generate a
weekly risk proposal, renew an approval, or apply a weekly threshold decision.

ML Profit Probability model-health and improvement lane (Strategy 1d and 1w only; do not create 1h/4h Strategy-profit scope):
On every wake, after the guardian result, perform a lightweight receipt-first audit of the current slow Strategy model authority and current Strategy model reports. Do not rescan the full OPRA archive every hour. Use immutable manifests, receipts, checksums, fingerprints, and incremental/current publications; do deeper work only when the authority or evidence fingerprint changed, a gate failed, or the selected mode is daily/weekly.

For each 1d and 1w profit model, verify and report:
- model artifact, manifest, receipt, checksum, current-authority, source-fingerprint, and promotion-gate integrity;
- chronological train/calibration/assessment separation, boundary purging, both-class support, assessment excluded from fitting/calibration/selection, real lockbox excluded, and orders_placed=0;
- exact feature schema plus training/inference parity, finite/null coverage, missing-reason counts, availability clocks, and any material distribution shift already defined by checked-in policy;
- OPRA cbbo-1m and ohlcv-1h session coverage/recency needed by conservative execution-haircut evidence, its untouched holdout coverage, candidate/outcome counts, Pricing eligibility/exclusions, and the first missing or corrupt session when incomplete;
- current candidate score coverage, null reasons, probability bounds, raw-versus-calibrated distribution/concentration, and coverage by horizon, symbol, strategy family, and market regime;
- only where outcomes are causally mature, assessment log loss, Brier score, expected calibration error/reliability bins, declared base-rate comparators, and any already-published discrimination/realized-return metrics. Never open the sealed lockbox, infer accuracy from unlabeled live predictions, or manufacture a metric or threshold.

Classify each horizon as HEALTHY, WATCH, RETRAIN_DUE, or BLOCKED using checked-in contracts only. An unchanged, receipt-valid authority can be reported concisely. Missing scores are acceptable only with their explicit, policy-valid reason; otherwise treat them as an incident.

Bounded automatic improvement rules:
- The existing strategy_profit_training slow owner at 22:00 UTC is the sole production retraining and promotion owner. Never start a duplicate while its PID/lock is live, and never restart a healthy sleeping owner merely to retrain early.
- When new causally mature evidence makes retraining due, verify that the daily owner will consume it. If that owner failed or remained stale beyond its next expected daily boundary, repair only that verified owner through the canonical launcher, then require a genuinely new verified training receipt and model authority.
- Production promotion is permitted only through the checked-in pipeline: training-only HGB/MLP challenger selection, separate Platt calibration, untouched assessment promotion gates, receipt/checksum verification, and atomic authority publication. Every declared gate must pass and orders_placed must remain zero. A rejected or failed challenger leaves the prior authority in place.
- Never weaken cohort sizes, quality thresholds, causal clocks, pricing requirements, or safety checks; never use assessment or lockbox evidence for feature/model selection or calibration; never promote on in-sample improvement; never rewrite an authority pointer by hand; and never place or enable orders.

Stage-gated optimization and experiment rules:
Only `select-nightly-bottleneck` may preregister a new experiment and only `run-shadow-ablation` may build/run it. `compare-challenger` and `stress-and-gate-review` validate that same immutable experiment without tuning. All other wakes are receipt-first evidence, UI, or incident work; they must not start a challenger. Failed, neutral, unchanged, immature, or blocked experiments are useful terminal results for that fingerprint and must not be retried on later hourly wakes.

Accept experiment evidence only when the comparison is chronological, assessment-clean, definition-compatible, independently supported, reproducible, and improves the declared primary objective without violating calibration, coverage, execution-cost, safety, or net-return gates. Never weaken a cohort, quality threshold, causal clock, pricing requirement, assessment separation, lockbox boundary, promotion gate, or zero-order contract to obtain a pass.

Automatic production changes remain limited to behavior-preserving repairs of violated existing data, lineage, monitoring, UI-adapter, presentation, or runtime contracts and model promotions performed by the sole checked-in Strategy-profit training pipeline. Any new production feature schema, model family, hyperparameter policy, calibration method, threshold, ranking objective, or promotion rule requires a concise ML Improvement Proposal and user approval after shadow evidence passes. The proposal must include the hypothesis, evidence, exact change, leakage/regime risks, data and compute needs, chronological ablation/challenger design, acceptance and rollback criteria, expected calibration and realized-net-return effect after fees, and reproducible receipt paths.

In the wake summary, include a compact ML Profit Probability section for 1d and 1w: authority generation, model/data/feature health classification, score coverage, mature quality metrics or INSUFFICIENT_MATURE_OUTCOMES, whether retraining is due/running/completed, any automatic repair, and any improvement proposal. Keep Scenario Coverage explicitly non-probabilistic.

Portfolio-aware options-strategy alignment lane:
- Scope every ML Profit Probability candidate, comparison, and recommendation to exact options strategies for 1d, 1w, and the compatible weekly day components. Do not recommend standalone equity, futures, crypto, or other non-options trades. A stock leg is allowed only when it is an integral checked-in options construction such as a covered or buy-write strategy.
- Use the current normalized Schwab portfolio snapshot through the existing read-only portfolio path and current-schwab-position-fit-v2 contract. Verify snapshot freshness and inspect per-symbol shares, exact option positions, available cash/buying power when reported, and working option orders. Never expose account identifiers or secrets.
- If current portfolio evidence is stale, incomplete, or unavailable, label portfolio alignment UNAVAILABLE and keep every suggestion research-only. Never substitute an old position snapshot, assume shares/cash, or infer buying power.
- Evaluate candidate compatibility separately from model probability: held-share coverage, cash/capital requirement, maximum loss/profit mechanics, expiry/assignment and early-exercise exposure, duplicate or conflicting working orders, existing option-leg overlap, and available checked-in delta/gamma/theta/vega and concentration evidence. Do not invent whole-portfolio Greeks or correlations when the system does not publish them.
- Prefer strategies that fit the current portfolio objective evidenced by holdings: explicitly distinguish hedging/reducing exposure, income/covered use of held shares, balanced exposure, and adding directional exposure. Flag or exclude candidates that lack required shares/funds, create unsupported naked risk, duplicate working orders, conflict with held option legs, or materially worsen concentration without explicit evidence and review.
- Report portfolio fit beside—not inside—the calibrated probability. ML Profit Probability remains the probability that the exact strategy has positive net return after modeled execution and fees; portfolio fit is a separate constraint/ranking explanation and must not be presented as probability or used to contaminate causal labels.
- Any proposed trade must include the exact option legs, horizon/expiry, quantity assumption, modeled capital/max-loss impact, ML Profit Probability, expected return, Pricing evidence, and portfolio-fit rationale. It remains a recommendation for manual review only: never submit, stage, replace, cancel, or enable an order, and keep orders_placed=0.
