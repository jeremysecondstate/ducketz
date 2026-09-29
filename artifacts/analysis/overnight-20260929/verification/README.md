# September 28 source / September 29 action verification

Prepared for native root `C:/DATASTORE/ml/overnight-runs/20260929T040727.779758Z` and observed Loop A cycle `20260929T040728.646690Z-pid62304`. Monday September 28 is the completed source session. OPRA/XNAS daily exclusive coverage ends September 29; the next action and original hard deadline are Tuesday September 29 at 04:00 Pacific (`2026-09-29T11:00:00Z`). The preparation target is 03:30 Pacific; do not extend healthy work to consume the window.

These read-only helpers write audit evidence only in this directory. They do not fit, fetch, snapshot a broker account, acquire supervision, reconcile ownership, change production state, start a trader, or submit orders. The supervising task must retain its claim and inspect native progress/logs at least once a minute. No completed-output audit was run during preparation.

## Final command

Run after both native `stage-report.json` and `receipt.json` say `COMPLETE`, from `C:/dev/ducketz` with repository Python:

```powershell
.\.venv\Scripts\python.exe -B artifacts/analysis/overnight-20260929/verification/run_final_checks.py --overnight-run C:/DATASTORE/ml/overnight-runs/20260929T040727.779758Z
```

If the native pipeline required recovery, supply the verified completed terminal descendant. The verifier checks ancestry, successful-stage inheritance, the original eight stages, source/archive/probability policies, original deadline, receipt/report/log checksums and zero overnight orders. Never pass an unrelated completed attempt. The sequential runner stops for diagnosis at the first failed helper and records commands, timing, status and logs in `audit-runner.json`.

1. `verify_completed_run.py` validates 24 forecasts/intents per current symbol (264 each, 209 entry rows, eleven symbols), all 33 OPRA cursors, source-day XNAS coverage, archive sources/rebuilt features/cohorts/current probabilities, sizing admission and partition evidence, planning snapshots and cash/share conservation, cumulative saved-plan evaluation, and frozen prior-session actuals.
2. `verify_yg_completion.py` re-runs saved estimator inference on assessment cohorts, reproduces raw/calibrated/baseline metrics, symbol/route support, chronological partitions, development-only grids/weekly shrinkage, calibration and exact promotion arithmetic.
3. `audit_provider_completion.py --hash-current` binds the observed Loop A cycle, all five providers, 33 source-day OPRA scopes and 66 raw/normalized hashes, exact zero-cost/provider-range/finite-count/capacity evidence, required outputs, and bounded optional CME/FMP advisories against the prior verified September 26 audit.
4. `review_models.py` produces readable scores and limits, factual comparison with the frozen September 28 publication `20260926T061604.318956Z`, and independently fitted-versus-qualified sizing status. Qualification failures remain explicit; the script does not require a particular pass/fail outcome.
5. `audit_optional_pricing.py` checks all 99 saved optional Pricing route gates, numerator/fraction and threshold arithmetic, exact non-Pricing fallback contracts, the current pinned Loop B manifest and its 51 compact Pricing source bindings against the prior verified authority. It reports sparse/missing optional sources without fitting or joining them.
6. `review_loop_b_weekly_prefix.py` verifies the saved Monday-source Loop B count: 99 intelligence routes contain 88 applicable LIVE forecasts and eleven explicit `1w-d5` rows marked `NOT_APPLICABLE_TO_REMAINING_WEEK`. The coherent weekly prefix has Tuesday–Friday targets; the fifth day would be next Monday. These dynamic Loop B counts do not change the separate independent Gameplan's fixed 264-row/five-session weekly contract.

The runner derives publication/enrichment identities from verified pinned artifacts. Budget roughly 15–25 minutes after native completion, depending on disk/cache and archive growth. The prior full reconstruction took about 12 minutes, estimator audit about 15 seconds and provider hashing about five minutes. Quiet audit I/O is not a reason to fit again. Read the active `*-stderr.log` while keeping supervision current. Root/preflight separately owns controls, schedules, trader identity and ownership before/after verification, including any evidenced native post-close reconciliation.

## Archive and sample checks

The publication manifest must bind `archive-history.json` plus its raw/normalized source inputs. Rebuilt sources check all configured symbols' feature dates, 21-session warmup, gaps/splits/discontinuities/provider-quality/undefined-row exclusions, and causal optional features. Each immutable horizon cohort is compared exactly with native reconstruction, including per-symbol count/date ranges, observed target prices, exact windows, five-minute endpoints and exclusions. All current raw/calibrated probabilities are reproduced with saved fitted estimators; no fitting occurs.

The runtime recomputes missing-prefix scope; the audit assumes no permanent six-symbol list. Requested prefixes require exact zero-dollar/provider-range/finite-count/capacity checks and unique native receipts. Verified reuse with zero new requests is valid. Original cursor snapshots are self-hashed and semantically bound to extension evidence; normal daily acquisition may advance current cursors. The extension receipt has no separate outer hash for its original-cursor snapshot file; the audit discloses that limit.

Seconds verify exact overlapping minute OHLCV without adding examples. Raw/normalized hashes and request headers are verified, but the seconds check does not replay every raw DBN record. Unmatched seconds remain disclosed and unused. Sizing admission is independently rebuilt: archive execution rows missing all eight operational observations may be excluded under the saved policy; partial or nonfinite inputs fail. Excluded-identity hashes, symbols, counts, feature exclusions, chronological partitions and saved inference remain bound to the source cohorts.

The full audit verifies planning reference completion, every 04:00–17:00 price point, original observation timestamps, source-cutoff and same-session synthetic anchors, frozen forecast identities, literal cash/holdings snapshot, event/hour/end conservation, and no-fill baseline. Explicit unavailable prices remain unavailable and do not require invented projections. Actuals must select the original September 28 preopening plan and matching trade plan; future/neutral/missing outcomes remain distinct from accuracy and broker fills. Cumulative evaluation checks each saved plan from September 4 against its own saved universe.

## Source provenance and policies

`audit-environment-baseline.json` captures 268 Python/doc/watchlist files at preparation, during the first native Loop A stage and before model/publication stages. Every major audit checks captured bytes; the runner checks again after. Any changed/missing/added source fails with explicit hashes for review. The baseline must not be silently overwritten. `preparation.json` records source helper hashes, adapted hashes and differences from the prior audit baseline. All 20 changes from that baseline are named Hyperliquid modules; the native stock-pipeline sources, both operating documents and watchlist are byte-identical. A read-only search found no Hyperliquid reference in the non-Hyperliquid `ml`/`datafetching` Python sources. Concurrent work remains untouched.

Current saved native contracts use `stock-direction-50-v2` (bullish above 0.50, bearish below 0.50), `accumulate_bullish_sell_on_bearish_no_scheduled_expiry_v1`, and planning v3 derivations with `sparse-session-planning-reference-completion-v2` (same-session carry up to 240 minutes after regular close within complete verified coverage). These documented September 14/16 contracts differ from older automation text's 54/46, scheduled expiry and 15-minute rules. Audits report the actual saved identities; this preparation changes no policy, gate or trading behavior. Directional publication status remains distinct from the selected manual execution policy and optional sizing qualification.

`preliminary_directional_review.py` may run once publication is COMPLETE and pinned while enrichment/planning continues. `preliminary_sizing_review.py` may run once sizing is COMPLETE. These bounded report/row checks defer archive/cohort/estimator reconstruction to the final runner. They do not establish full completion.

## Contingencies

The bounded provider helper expects Loop A to succeed in the original attempt and all 33 scopes to use Historical. If it succeeds only in a descendant or uses verified Live delivery, adapt the helper to verified stage/partition evidence rather than bypassing a mismatch. The full verifier already checks the exact Historical/Live union. Current pointers must still select the attempt's pinned outputs. Advancing them before this audit produces an explicit mismatch instead of silently verifying a different publication. No model is relabeled and no retraining is warranted solely because saved quality checks fail.

## Completed evidence

Native work completed without a restart at 06:29:23Z. The full 12-section reconstruction passed at 06:44:42Z. The next helper stopped before inference because concurrent `ml/hyperliquid_paper_policy.py` bytes differed from the original capture. Original failed runner, watcher and logs remain unchanged and are also copied under `first-audit-attempt`.

`reviewed-isolated-code-drift.json` and `.diff` bind the exact baseline/current bytes; the original baseline remains unchanged. An independent preflight AST closure of all eight native stage commands plus the runtime examined 199 reachable modules and excludes the changed policy and its direct importers. Shared application Hyperliquid account/info modules may be reachable; this is a narrowly scoped policy-file isolation finding.

`continue_final_checks.py` inherited the successful reconstruction and logs by hash, then completed the remaining five checks at 06:51:54Z. `audit-runner-continuation.json` is the successful continuation receipt; `audit-runner.json` intentionally retains the initial guard failure. No native stage or completed reconstruction was repeated. Do not rerun the completed September 28 source.

`provider-advisory-resolution.json` closes the bounded advisory review flag: the current CME message differs only in staleness age for the same September 3 candidate, all six current captures succeeded, both MBP captures remain capped, and the five historical FMP clock-skew rows remain rejected while the current quote passes. It preserves the original provider audit and all exclusions. `audit-findings.json` and `.md` provide final counts, ranges, model statuses, gaps and scope limits.
