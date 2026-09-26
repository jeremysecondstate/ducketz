# September 25 source / September 28 action final verification

Prepared for native root `C:/DATASTORE/ml/overnight-runs/20260926T040723.834511Z` and observed Loop A cycle `20260926T040724.729021Z-pid46624`. The source is Friday September 25; OPRA/XNAS daily exclusive coverage ends Saturday September 26; the next action session and unchanged hard deadline are Monday September 28 at 04:00 Pacific (`2026-09-28T11:00:00Z`). Aim to finish this overnight promptly, targeting Saturday 03:30 Pacific rather than consuming the weekend allowance.

These are read-only audit helpers. No full audit has run against unfinished output. They never fit, fetch, snapshot a broker account, acquire supervision, reconcile ownership, modify production state, start a trader or submit orders. The supervising task owns and renews its existing claim while reading native health at least once a minute.

## Run only after the native report and receipt both say COMPLETE

From `C:/dev/ducketz`, using the repository Python:

```powershell
.\.venv\Scripts\python.exe -B artifacts/analysis/overnight-20260926/verification/run_final_checks.py --overnight-run C:/DATASTORE/ml/overnight-runs/20260926T040723.834511Z
```

If recovery was necessary, supply the verified terminal descendant. The full verifier proves inheritance from the root, retained successful stages, original eight-stage order, exact deadline/source/archive target policy, report and log hashes, and zero overnight orders. It then runs four checks sequentially:

1. `verify_completed_run.py`: native ancestry, 24 rows per configured symbol in forecasts and stock-only intents (264 each currently), 209 entry rows, 33 production OPRA cursors, completed-session XNAS source coverage, archive sources and rebuilt features/cohorts/current probabilities, sizing source/admission/partition checks, planning snapshot/conservation/reference evidence, cumulative saved-plan evaluation and original prior-session actuals.
2. `verify_yg_completion.py`: saved estimator inference on assessment rows, raw/calibrated/baseline scores, all route/symbol metrics and fitted counts, chronological partitions, development-only selection grids and weekly shrinkage, calibration diagnostics and honest current promotion arithmetic.
3. `audit_provider_completion.py --hash-current`: exact native Loop A cycle, five providers, 33 OPRA source-day scopes and 66 raw/normalized file hashes, fresh zero-dollar/provider-range/capacity checks, output existence and optional CME/FMP advisories compared with the verified September 25 audit.
4. `review_models.py`: readable actual scores and promotion-policy limits, factual comparison with frozen September 25 models, and independent fitted-versus-qualified sizing status with strict quality arithmetic, market-input admission/exclusions, scope reasons and development-selection evidence. It does not assume that any group must remain unqualified.

The runner derives immutable publication/enrichment paths from the successful full audit rather than guessing future generations or reading an unrelated latest pointer. It records each command, start/end time, exit code and output path in `audit-runner.json`. All subprocess logs stay here; errors stop the runner for diagnosis. Native controls/ownership/schedule pre/post comparison remains with the separate root/preflight audit.

The prior full audit took about 12 minutes (September 25 artifact timestamps 06:23:17–06:35:19 UTC), saved-estimator inference about 15 seconds, and provider hashing about five minutes. Budget roughly 15–25 minutes for this sequence, depending on disk/cache and archive growth; this is an estimate, not a timeout. Audit I/O may continue quietly between sections. Keep minute-by-minute supervision and inspect the active `*-stderr.log`; do not retrain when an audit is slow.

## Source and model checks

`archive-history.json` must be in the publication output manifest. All raw/normalized feature, minute-target and second-consistency inputs must match manifest bytes. Rebuilt archive features must match per-symbol feature/source dates, 21-session warmup, split/discontinuity/provider-quality/undefined-observation exclusions and optional-feature availability. Every saved horizon cohort is compared exactly with its native reconstruction, including target clocks, observed prices, five-minute absolute endpoint gaps, counts and per-symbol dates. All 264 current raw/calibrated probabilities are inferred again with the saved fitted models; there is no fit.

The native runtime computes the prefix manifest again. Audit scope is dynamic: no six-symbol prefix list is assumed. It verifies native exact zero-cost/range/finite-count/capacity checks whenever a prefix was requested, rejects duplicate request/preflight evidence, and permits verified reuse with zero requests. Original cursor snapshots are self-hashed and semantically bound to the extension manifest. The extension reports exact preservation during prefix acquisition; current cursors may legitimately advance during normal daily acquisition. The native receipt has no separate outer hash for the entire original-cursor snapshot file, and the report discloses this limitation.

Second-level data verifies exact overlapping minute OHLCV values without adding training rows. Raw and normalized archive hashes and request headers are checked; the seconds audit does not replay every raw DBN record. Unmatched seconds are disclosed, with no invented prices or samples.

Sizing admission is rebuilt from immutable directional cohorts: only archive execution rows missing all eight operational observations may be excluded. Partial/nonfinite inputs still fail native validation. Counts, symbols, feature exclusions and excluded target-identity hash must match. Native saved sizing-model loading verifies cohort/partition evidence and assessment inference. Strict sizing quality remains separate from directional v2 tolerance-based promotion.

## Saved policies and implementation drift

Current native docs/code use `stock-direction-50-v2` (bullish above 0.50, bearish below 0.50), `accumulate_bullish_sell_on_bearish_no_scheduled_expiry_v1`, and planning v3 with `sparse-session-planning-reference-completion-v2` (up to 240 minutes after the same session's regular close inside verified coverage). These are documented September 14/16 contracts, differing from earlier 54/46, expiry-exit and 15-minute boilerplate. Helpers check and report saved current policy identities. No behavior was changed by this preparation. Original forecasts keep their own saved universe, policy and target identity during evaluation.

Relevant evidence: `ml/stock_direction_policy.py:6`, `ml/gameplan_cash_ledger.py:19`, `ml/gameplan_price_completion.py:24`; `INDEPENDENT_STOCK_HORIZONS.md:60`, `:145` and `:287`. Raw-direction YG targets and promotion v2 have their own independent identities. The current manual instruction-reading policy's treatment of model checks must not be conflated with a publication quality pass or order authority.

`audit-environment-baseline.json` captures hashes of 255 Python/doc/watchlist files at preparation. Every major audit checks this baseline before heavy work; the runner checks it again after. Changed/missing/added source files fail with explicit paths and hashes. Review genuine concurrent changes before adapting the baseline; do not automatically accept drift. The original copy script refuses to overwrite an existing environment baseline.

The preliminary reviewer stopped on six concurrent Hyperliquid changes. Root authorized a read-only relevance review: 237 non-Hyperliquid `ml`/`datafetching` sources contain no Hyperliquid reference, and all captured native stock-pipeline sources remain unchanged. `reviewed-isolated-code-drift.json` retains exact old/new hashes, the reviewed Git heads and rationale; the companion `.diff` preserves source evidence. The original baseline remains intact. Only those exact reviewed bytes are admitted by the guard; new edits still fail. This exception grants no execution authority to any Hyperliquid code.

The now-complete pinned publication and sizing stages also have bounded preliminary reviews in `preliminary-directional-review.md/.json` and `preliminary-sizing-review.md/.json`. These read only small manifest-bound reports and forecast rows, recompute saved arithmetic, and explicitly defer archive/cohort/estimator reconstruction to the final runner. All four directional groups passed saved v2 gates; separate learned sizing fit all four groups with zero qualified scopes under its strict saved gates. These are observed current results, not assumptions built into the verifier.

Read-only comparison from commit `59fa889` to the captured HEAD found no changes to existing native overnight/model/planning modules or these contracts; additions under `ml`/`datafetching` are separate Hyperliquid modules. Concurrent UI work was left untouched. Relevant provenance and helper hashes are in `preparation.json`.

## Contingencies and coverage caveats

The bounded provider helper assumes the original Loop A stage succeeds and all 33 scopes use Historical. If Loop A fails and succeeds only in a descendant, adapt its stage/cycle binding from verified ancestry. Native Live fallback requires a corresponding bounded partition adaptation; the full verifier already checks the exact Historical/Live union. Do not bypass an audit mismatch or equate a valid new delivery mode with corruption.

Current pointers must still select the attempt's pinned outputs. Advancing them before auditing raises a mismatch rather than silently auditing another publication. All future/missing/neutral actual outcomes and unavailable planning references retain explicit availability states. Synthetic planning references preserve original observed times and remain absent from training/evaluation/actuals. A complete source interval does not prove a trade occurred or guarantee a usable endpoint. Research-only directional/sizing results remain quality limitations, not fabricated passes or grounds for unchanged retries.
