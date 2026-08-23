# Codex handoff: resolve the Loops daily production DEGRADED state

Work in `C:\dev\ducketz` and own the diagnosis, implementation, tests, and bounded production verification needed to make the deterministic daily Loops audit genuinely healthy.

The goal is not to hide warnings. The goal is a causally valid `ml.system_monitor --mode daily` result with top-level `HEALTHY`, zero `WARN`, zero `FAIL`, fresh Databento equity bars, fresh Directional Loop B predictions, exact-target Pricing evidence, fitted Strategy evidence, and both UI contracts passing.

Continue until the root causes are fixed and verified, or fail closed with the exact irreducible evidence blocker. Do not call a missed immutable target “repaired.” Prove the coordination fix on a genuinely new eligible regular-session target.

## Start here

1. Inspect `git status`, the complete existing diff, and relevant untracked files before editing.
2. Preserve all existing work. Do not revert, overwrite, broadly reformat, or silently replace previous changes.
3. Read the current code and tests rather than assuming the older handoff remains current.
4. Capture one read-only baseline with:

   ```powershell
   .\.venv\Scripts\python.exe -m ml.system_monitor --datastore-target pc --mode daily --compact
   ```

5. Treat the datastore as production evidence. Preserve immutable receipts, manifests, checksums, causal cutoffs, and authority lineage.

The previous prompt at `C:\Users\7980X\Downloads\HANDOFF-PROMPT.md` is background only. Its original probability/UI integrity problem is largely represented in current code and tests. Do not blindly reapply it.

## Working-tree state to preserve

At handoff creation, the repository was on `main` at `a97638d` with substantial uncommitted work:

- `app/services/databento_cme_context.py`
- `datafetching/orchestrate.py`
- `ml/option_pricing/causal.py`
- `ml/option_pricing/operations.py`
- `ml/option_pricing_runtime.py`
- `tests/test_loop_a_orchestration.py`
- `tests/test_market_cycle_coordination.py`
- `tests/test_option_pricing_core.py`
- `tests/test_option_pricing_protocol_v2.py`
- untracked `tests/test_databento_cme_context.py`

Those changes include cycle-anchor scheduling, bounded prior-boundary recovery, Pricing/Loop A lock coordination, standard OCC contract semantics, operational resource limits, CME warning handling, and focused tests. Inspect and validate them before extending them. Do not assume they are correct merely because they exist.

## Authoritative incident snapshot

The scheduled guardian selected `daily` at:

`2026-08-20T21:43:17.857569+00:00`

The final read-only daily monitor checked at:

`2026-08-20T21:46:14.705269+00:00`

Final status:

- top-level: `DEGRADED`
- `PASS=20`
- `INFO=1`
- `WARN=3`
- `FAIL=0`

Exact warnings:

1. `directional_prediction_quality`
   - `Offline directional evaluation is complete but quality warnings are present.`
2. `strategy_prediction_quality`
   - `Strategy is publishing research-only Scenario Coverage, not calibrated probabilities.`
3. `pricing_strategy_canary`
   - `The latest regular target does not yet pass the read-only Pricing-to-Strategy canary.`

Exact INFO:

- `pricing_publications`
  - `Market is closed; Pricing retains its last verified target authority.`

All seven process pairs, locks, active logs, current publication integrity checks, cross-loop lineage, UI contracts, and storage checks passed. Do not waste time restarting healthy processes merely because the daily evaluation is degraded.

## Operational evidence that already passes

At the final monitor:

- CME: launcher `27244`, worker `41212`
- ALFRED: launcher `26512`, worker `53592`
- Loop A: launcher `61264`, worker `61796`
- Pricing: launcher `50052`, worker `7096`
- Loop B: launcher `23480`, worker `8008`
- Options: launcher `59240`, worker `32948`
- Strategy: launcher `56404`, worker `28268`
- every exact singleton lock was `OWNED` by its matching worker
- latest complete Loop A generation: `20260820T213020.000789Z-pid61796`
- Loop A completed all six symbols with zero failures
- exact bar-readiness target: `2026-08-20T20:00:00+00:00`
- readiness receipt:
  `C:\DATASTORE\loop-a\bar-readiness\1787256000000000000\receipt.json`
- readiness was not published until `2026-08-20T20:29:28.059925+00:00`
- canonical operational equity OHLCV lineage remained Databento `EQUS.MINI`
- Databento `XNAS.ITCH` remained a separate cold archive
- latest Loop B publication:
  `C:\DATASTORE\ml\runs\20260820T213723.203670Z`
- Loop B had 54/54 verified routes with no publication failures
- all six exact `20:00Z` Options authorities existed using explicitly labeled Schwab fallback
- latest verified Pricing target authority remained `19:45Z`:
  `C:\DATASTORE\ml\option-pricing-target-outcomes\1787255100000000000-1787256245940107000`
- latest Strategy publication observed by the publication check:
  `C:\DATASTORE\ml\strategy-runs\20260820T212500.061962Z`
- both production UI adapters passed and exposed no heuristic candidate as manually actionable
- no orders were placed

## Problem 1: the close-boundary Pricing canary is causally missing

The first bad target is exactly:

`2026-08-20T20:00:00+00:00`

Established sequence:

1. The preceding Loop A cycle overran and finished at `20:04:04Z`.
2. The `20:00Z` Loop A cycle began at `20:04:24Z`.
3. Its Databento watchlist stage did not begin until `20:29:00Z`.
4. Six-symbol readiness was published at `20:29:28Z`.
5. Pricing was scheduled at phase `+1 minute` with a bounded `480`-second readiness deadline.
6. Pricing correctly recorded `READINESS_DEADLINE_MISSED` and did not change its current authority.
7. Options also lacked the exact completed Databento 1-minute bars by its causal deadline (`20:18:16Z`) and initially failed closed.
8. Options later recovered the exact `20:00Z` snapshots after readiness existed, but Pricing correctly refused to backdate an outcome outside its causal window.
9. The read-only canary therefore reports:

   `StrategyPricingCanaryError: Canary deadline expired before every exact-target check passed; target=2026-08-20T20:00:00+00:00; last_error=TargetOutcomeError: No authoritative Pricing outcome exists for target 2026-08-20T20:00:00+00:00`

Relevant logs:

- Loop A:
  `C:\DATASTORE\logs\ducketz\system-guardian\20260820\140335-loop-a.stdout.log`
- Pricing:
  `C:\DATASTORE\logs\ducketz\system-guardian\20260820\141844-active-pricing.stdout.log`
- recovered Options worker:
  `C:\DATASTORE\logs\ducketz\system-guardian\20260820\210935-options-capture.stdout.log`

The old Options worker had already been repaired in a prior wake after direct evidence of process-local degradation. Do not attribute every remaining delay to that old PID without new evidence.

### Required coordination fix

Make Loop A → Options → Pricing coordination reliably publish a new eligible exact target before every causal deadline under realistic production load.

The solution must:

- preserve exact target, contract, cutoff, validity interval, checksum, and provider lineage;
- never fabricate readiness or a Pricing outcome;
- never use a future bar or quote;
- never backdate a publication;
- tolerate a bounded provider-availability delay without silently skipping the next boundary;
- ensure heavy non-readiness Loop A work cannot indefinitely block the small exact-bar readiness contract;
- keep Databento `EQUS.MINI` canonical for operational equity OHLCV;
- keep Schwab clearly labeled as options/quote/broker fallback, never canonical equity OHLCV;
- keep `XNAS.ITCH` a separate cold archive;
- preserve independent Options and Pricing idempotency;
- expose the first delayed stage and deadline in receipts/telemetry.

Do not merely increase timeouts. First establish why the Databento readiness stage was delayed roughly 25 minutes after cycle start. Inspect scheduling overrun, CPU/memory contention, datastore locking, provider concurrency, and whether a lightweight readiness lane should be separated from the full Loop A cycle. Any timing change must remain inside the causal source window.

Validate the uncommitted cycle-anchor, prior-boundary recovery, and datastore-cycle-lock changes against actual schedule math. Ensure process reload recovery cannot publish an expired target and cannot skip an immediately due target.

The immutable `20:00Z` miss cannot be repaired. Production proof must use a genuinely new eligible regular target.

## Problem 2: Strategy still has no calibrated model or full Pricing coverage

The final daily quality read observed:

- candidate rows: `3840`
- routes: `24`
- calibrated candidate rows: `0`
- fully priced rows: `0`
- quality-passing rows: `0`
- Scenario Coverage rows: `3840`
- complete observed option-outcome rows: `27160`
- model statuses: `MODEL_NOT_FIT=9`
- Pricing statuses: `Delayed=344`, `Unavailable=3496`
- missing horizons: none

The guardian read an earlier current Strategy generation with `4800` candidates and `30` routes. The final monitor read a later generation for the quality check while some earlier checks still referenced the prior publication. These are different generations, not a trend.

Scenario Coverage is a local heuristic grid pass fraction. It is not a probability. Preserve the current semantic separation:

- `scenario_coverage_score` may rank research-only candidates;
- raw, calibrated, and decision probability fields must stay null unless a fitted causal model is valid;
- `Calibrated Probability` must stay null until a fitted Strategy model and full eligible Pricing evidence exist;
- candidates failing Pricing, liquidity, quote-validity, or surface-quality gates must not become manually actionable.

### Required Strategy/Pricing fix

Build the sustainable path from observed outcomes to reusable fitted Strategy models and exact Pricing evidence.

The solution must:

- diagnose why `27160` complete observed outcomes still yield `MODEL_NOT_FIT` for all nine horizons;
- separate outcome availability from Pricing eligibility and report excluded-row reasons precisely;
- materialize outcomes append-only/incrementally rather than replaying the full OPRA archive every live cycle;
- use only observed point-in-time BBO outcomes for labels, never synthetic Black-Scholes labels;
- preserve chronological train, calibration, assessment, and untouched lockbox boundaries;
- fit and reuse immutable checksum-valid model artifacts only when evidence gates pass;
- keep Black-Scholes as the constrained baseline when BSGP residual evidence is unavailable;
- keep BSGP explicitly identified as an enhancement, not a universal prerequisite;
- attach Pricing evidence only when contract, target, cutoff, validity interval, and source lineage match exactly;
- reject future, stale-target, partial-leg, corrupt, or mismatched Pricing evidence;
- publish honest per-route reasons when a model cannot be fitted;
- never promote a model merely to make the monitor green.

Do not game `ml.system_monitor` by weakening its PASS condition to accept one token calibrated row. Resolve the configured route/horizon evidence honestly.

## Problem 3: Directional quality has measured offline weaknesses

The final daily report contained `5987` offline evaluation rows. These warnings are measured performance, not merely immature labels:

| Horizon | Accuracy | ROC AUC | Calibration gap | Published warning |
|---|---:|---:|---:|---|
| `1h` | 0.6483357453 | 0.6079187211 | 0.0908067293 | calibration gap > 0.05 |
| `4h` | 0.4550898204 | 0.4411835548 | 0.0896639927 | calibration gap > 0.05; AUC < 0.5 |
| `1d` | 0.5418994413 | 0.5703767464 | 0.0951818959 | calibration gap > 0.05 |
| `1w` | 0.5264550265 | 0.5314804947 | 0.1015632282 | calibration gap > 0.05 |
| `1w-d1` | 0.4564102564 | 0.4715921136 | 0.0780207598 | calibration gap > 0.05; AUC < 0.5 |
| `1w-d2` | 0.4557291667 | 0.4953383459 | 0.0813509236 | calibration gap > 0.05; AUC < 0.5 |
| `1w-d3` | 0.4841269841 | 0.5089373681 | 0.0630636902 | calibration gap > 0.05 |
| `1w-d4` | 0.4629629630 | 0.4951724138 | 0.0705357544 | calibration gap > 0.05; AUC < 0.5 |
| `1w-d5` | 0.4550264550 | 0.4696129486 | 0.0568942462 | calibration gap > 0.05; AUC < 0.5 |

Live labels exist overall (`131` completed forecasts), but route-level live maturity is still insufficient:

- `INSUFFICIENT_LIVE_EVIDENCE=30`
- `NO_COMPLETED_DECISIONS=24`

Do not describe insufficient live maturity as measured poor live performance. Do not claim a trend without a comparable prior immutable evaluation under identical definitions.

### Required Directional fix

Use a scored, leakage-safe model-improvement loop for the affected horizons.

The solution must:

- preserve point-in-time features and ALFRED vintage/release causality;
- preserve chronological partitions and the untouched lockbox;
- inspect class balance, route aggregation, calibration support, threshold policy, feature drift, and model-family suitability;
- improve calibration with training/calibration evidence only;
- never tune against assessment or lockbox labels;
- never invert predictions simply because an AUC is below 0.5 without proving the feature/label orientation defect;
- compare challengers on identical immutable cohorts and definitions;
- report accuracy, Brier score, log loss, AUC, calibration gap, row counts, and confidence/uncertainty for every horizon;
- retain an existing model when a challenger does not demonstrate a real held-out improvement;
- leave live-evidence status honest until independent observations mature.

Do not resolve the warning by increasing references, deleting weak horizons, suppressing checks, or changing metric definitions without a separately justified, tested contract migration.

## Problem 4: pin monitor evidence to one generation

During the final monitor, `strategy_publication` and the UI check referenced the `4800`-candidate generation while `strategy_prediction_quality` observed a later `3840`-candidate generation. Loop B and Strategy pointers advanced while the monitor ran.

Make a daily monitor report internally generation-consistent, or make every check explicitly carry the exact immutable generation it evaluated and reject/report cross-check generation drift. Do not silently combine different current pointers into one apparent snapshot.

Add a test that advances a pointer between checks and proves the report cannot mix incompatible generations.

## Relevant code

Start with these files, but follow the data and do not limit inspection artificially:

### Scheduling and readiness

- `datafetching/orchestrate.py`
- `datafetching/decision_time.py`
- `datafetching/bar_readiness.py`
- `datafetching/loop_a_cycle.py`
- `datafetching/databento_fetch.py`
- `datafetching/options_runtime.py`
- `ml/option_pricing_runtime.py`
- `ml/option_pricing/target_outcome.py`
- `ml/option_pricing/causal.py`
- `ml/strategy_pricing_canary.py`
- `docs/datafetch-ml/start_all_loops.ps1`

### Strategy outcomes, Pricing, and calibration

- `ml/strategy_selection/outcome_store.py`
- `ml/strategy_selection/runtime.py`
- `ml/strategy_selection/model.py`
- `ml/strategy_selection/market_state.py`
- `ml/strategy_selection/chain.py`
- `ml/strategy_selection/contracts.py`
- `ml/option_pricing/strategy_outcomes.py`
- `ml/option_pricing/strategy_shadow.py`
- `ml/option_pricing/schwab_materialization.py`
- `ml/option_pricing/model.py`
- `ml/option_pricing/prediction.py`
- `ml/strategy_runtime.py`
- `ml/strategy_publication.py`

### Directional models and evaluation

- `ml/runtime_pipeline.py`
- `ml/model_runtime.py`
- `ml/model_features.py`
- `ml/models/registry.py`
- `ml/calibration.py`
- `ml/rolling_samples.py`
- `ml/rolling_materialization.py`
- `ml/feature_registry.py`
- `ml/live_evidence.py`
- `ml/prediction_runtime.py`
- `ml/current_publication.py`

### Monitoring and UI contracts

- `ml/system_monitor.py`
- `ml/system_guardian.py`
- `app/ui/options_strategy_data.py`
- `app/ui/options_strategies.py`

## Required focused tests

Add or strengthen tests that prove:

1. A long Loop A cycle cannot silently skip the following eligible boundary.
2. A delayed Historical boundary cannot publish premature readiness.
3. Exact readiness can recover only while the target remains causal.
4. Future, stale, partial-symbol, mismatched, or corrupt readiness is rejected.
5. Pricing waits for a coherent completed Loop A generation rather than reading files mid-write.
6. Pricing can recover the immediately prior boundary after a process reload only inside the causal window.
7. An expired boundary publishes an honest terminal state and is never backdated.
8. A constrained Black-Scholes target can publish when BSGP residual evidence is unavailable.
9. Strategy accepts only exact contract/target/cutoff/validity Pricing evidence.
10. Incremental observed-outcome materialization fits and reuses an immutable model without full OPRA replay on every live cycle.
11. Train, calibration, assessment, and lockbox partitions remain chronologically isolated.
12. Scenario Coverage never populates or displays a calibrated probability.
13. Calibrated rows require a fitted model plus full Pricing and quality gates.
14. Directional challenger evaluation cannot read assessment/lockbox labels during training, selection, or calibration.
15. All affected directional horizons report comparable held-out metrics.
16. A monitor invocation cannot mix incompatible Loop B/Strategy generations.
17. Both UI adapters expose source, pricing availability, calibration state, and quality warnings honestly.
18. Automated and manual order paths remain disabled unless their existing independent authorization contracts pass.

## Validation commands

Run focused tests first:

```powershell
.\.venv\Scripts\python.exe -m pytest tests\test_loop_a_orchestration.py tests\test_loop_a_cycle.py tests\test_market_cycle_coordination.py tests\test_pricing_options_sequencing.py tests\test_option_pricing_core.py tests\test_option_pricing_protocol_v2.py tests\test_option_pricing_loop_native_bsgp.py tests\test_option_pricing_shadow_consumers.py tests\test_ml_strategy_selection.py tests\test_ml_runtime_pipeline.py tests\test_ml_prediction_runtime.py tests\test_ml_calibration.py tests\test_options_strategy_ui.py tests\test_strategy_pricing_canary.py tests\test_system_monitor.py -q
```

Then run:

```powershell
.\.venv\Scripts\python.exe -m pytest -q
git diff --check
```

Do not weaken or delete an existing protection merely to make these tests pass.

## Production canary and success criteria

Do not claim resolution from fixtures alone. After focused and full tests pass, use the checked-in canonical launchers and the smallest affected-runtime restart scope needed for the changed code.

Before any restart:

- prove the exact allowlisted command line;
- prove unambiguous launcher/worker ownership;
- record before PIDs and matching lock ownership;
- preserve unrelated runtimes;
- fail closed if ownership is duplicated or ambiguous.

After any restart:

- record stopped and after PIDs;
- record exact stale-lock handling;
- verify the replacement pair and lock;
- wait for a genuinely new eligible regular target.

The new-target canary must prove, for the same exact target:

- six-symbol Databento `EQUS.MINI` Loop A readiness arrived within the causal deadline;
- exact Options authorities exist with explicit provider lineage;
- Pricing published a checksum-valid Black-Scholes or BSGP target outcome;
- Strategy attached exact-leg Pricing coverage;
- at least one fitted, calibrated candidate passes full Pricing and quality gates only if legitimate evidence supports it;
- Scenario Coverage remains visibly heuristic and non-actionable;
- Pricing-to-Strategy canary is `PROVEN`;
- both UI contracts load the same pinned generations and display correct semantics;
- no order was placed;
- final daily monitor is `HEALTHY` with zero `WARN` and zero `FAIL`.

Use the monitor as the final read-only gate:

```powershell
.\.venv\Scripts\python.exe -m ml.system_monitor --datastore-target pc --mode daily --compact
```

If measured directional quality still fails published references, the task is not complete even when operations and the Pricing canary pass. Continue the leakage-safe improvement loop or report the exact evidence limitation.

## Safety boundaries

- Never place an order or enable automated action.
- Never fabricate labels, calibration, readiness, Pricing evidence, or a canary result.
- Never use future data or change causal cutoffs retroactively.
- Never backdate a publication.
- Never rewrite an authority pointer to manufacture recovery.
- Never submit bulk Databento history jobs or OPRA bootstraps.
- Never run unbounded backfills or replay the full OPRA archive in every live Strategy cycle.
- Never expose credentials or tokens.
- Never delete or broadly clean the datastore.
- Never promote a model solely to silence monitoring.
- Never restart the whole stack when one affected runtime is sufficient.
- Never discard existing uncommitted work.
- Do not commit, push, or open a PR unless the user explicitly asks.

## Final report

Lead with the final daily status and checked-at time. Then report:

- root causes by workstream;
- every changed file and why;
- tests and exact results;
- before/stopped/after PIDs and lock handling for any restart;
- the new exact-target readiness, Options, Pricing, Strategy, and canary receipt paths;
- all directional metrics by horizon on identical held-out evidence;
- Strategy calibrated-versus-Scenario-Coverage state;
- Pricing coverage and model-fit state;
- live-label maturity separately from measured offline quality;
- both UI contracts and pinned generations;
- remaining limitations or blockers;
- confirmation that no orders or forbidden recovery actions occurred.

