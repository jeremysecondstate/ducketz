# September 25 final-verification preparation

Prepared local, read-only helpers for native root attempt `C:/DATASTORE/ml/overnight-runs/20260925T040822.112032Z`: source September 24, action September 25, original hard deadline September 25 04:00 Pacific (`2026-09-25T11:00:00Z`). The 03:30 target remains an operational completion target. These helpers have not audited the unfinished output.

All six Python helpers pass AST parsing. `preparation.json` records original and adapted SHA-256 hashes. Changes are confined to this directory. No provider/broker request, fit, source mutation, claim, pipeline launch, trader action or order was performed.

## Execute only after the native report and receipt both say COMPLETE

Use the repository virtualenv, from `C:/dev/ducketz`. Substitute the verified completed terminal resume path for the original path if recovery was required. Keep these potentially large archive/model checks sequential, with the operator renewing supervision and reading native health at least every minute.

```powershell
.\.venv\Scripts\python.exe -B artifacts/analysis/overnight-20260925/verification/verify_completed_run.py --overnight-run C:/DATASTORE/ml/overnight-runs/20260925T040822.112032Z 1> artifacts/analysis/overnight-20260925/verification/completion-audit.json 2> artifacts/analysis/overnight-20260925/verification/completion-audit-progress.log
.\.venv\Scripts\python.exe -B artifacts/analysis/overnight-20260925/verification/verify_yg_completion.py --overnight-run C:/DATASTORE/ml/overnight-runs/20260925T040822.112032Z --output C:/dev/ducketz/artifacts/analysis/overnight-20260925/verification/yg-completion.json
.\.venv\Scripts\python.exe -B artifacts/analysis/overnight-20260925/verification/audit_provider_completion.py --terminal-run C:/DATASTORE/ml/overnight-runs/20260925T040822.112032Z --hash-current
```

The full verifier reads the current pointers and proves they agree with this attempt's pinned publication; changed pointers produce an explicit failure. It verifies the ancestry, original eight stages, deadline, report/receipt/log hashes, zero orders, 264 forecasts and intents, 209 entry windows, own-universe cumulative historical coverage, 33 OPRA cursors, source-session XNAS acquisition, all four archive cohorts and feature dates, native warmup/quality/split exclusions, exact second/minute overlaps, and archive source files. It recomputes enrichment optional-input exclusions, the 264 planning rows, all 154 hourly price points, saved snapshot/ledger and cash/share conservation, synthetic assumptions and original observation times, plus September 24 actuals and the successor link. No unavailable outcome is filled or relabeled.

The YG supplement reproduces saved estimator inference, raw and calibrated assessment scores, training-rate baselines, chronological partition gaps, exact symbol/route support, fixed development selection grids, weekly shrinkage and promotion arithmetic. Failed quality gates are coverage notes rather than integrity failures when reported honestly. `--publication-only` is retained solely for an explicitly directed investigation after a terminal tail failure; it does not verify native completion.

After full completion, optional `review_models.py --gameplan-run <explicit pinned publication> --enrichment-run <explicit native enrichment run> --overnight-run <terminal attempt>` produces readable scores and fitted-versus-qualified sizing summaries. Its comparison baseline is frozen September 24 publication `C:/DATASTORE/ml/nightly-gameplan-runs/20260924T062615.050242Z`; unlike yesterday's helper, it does not incorrectly call that an older feature contract. Its default guard now requires full native completion.

## Boundaries and follow-up

The provider helper pins observed Loop A cycle `20260925T040822.974281Z-pid22132` and exact September 24 Historical OPRA scopes. If Loop A itself needs a new successful attempt, its cycle/run binding must be adapted from verified evidence; do not pretend the old cycle completed. It also assumes Historical completion for all 33 scopes; genuine native Live fallback must be assessed with the full verifier's Historical/Live union and an adapted bounded partition audit rather than bypassing checks. `--terminal-run` supports a completed descendant when the original Loop A stage succeeded. Current small cycle/cursor/health files must still belong to this completed fetch; advancement is an explicit mismatch.

The bounded provider helper rehashes 66 exact current-session raw/normalized files with `--hash-current`, checks fresh zero-dollar/capacity preflights, and compares compact optional diagnostics against the prior September 24 audit. It retains explicit missing CME-symbol observations and FMP source-skew caveats. Its CME literal-contract expectation remains the observed prior corrected scope; any changed provider mappings need independent evidence before adaptation.

The implementation contract has later operator-approved behavior documented in `INDEPENDENT_STOCK_HORIZONS.md`: above/below 50% directions (September 14) and signal-driven horizon holdings without expiry sales (September 16). Native planning also retains the documented sparse-session v3 reference policy (up to 240 minutes after regular close) rather than the older v2 15-minute rule. The recurring prompt contains earlier 54/46, expiry and 15-minute text. The supervising task explicitly directed preservation and verification of current native contracts, manifest-bound saved policy and manual mode. Helpers report the actual saved policy; no trading or publication behavior was edited.

These are operation helpers, not a replacement for supervision or permission to retry a completed session. Full source/cohort rebuilding is substantial local I/O and CPU; there is no heavy audit before native completion. Saved source/model gates remain authoritative. Any actual audit error requires diagnosis, and any research/missing state remains visible.

## Bounded helper review

`helper-review-tests.json` records nine passing bounded checks. The strengthened native verifier passed the saved September 24 three-attempt ancestry, all eight stages and bound log/report bytes; in-memory missing log inventories, altered hashes, truncated COMPLETE stage lists and incorrect resume inheritance were rejected. Numeric-report checks reject missing metrics and changed values while admitting only 1e-12 absolute inference roundoff. No current-run outputs or archives were audited.

The full archive check now also rebuilds the current causal rows and reproduces all 264 frozen raw/calibrated probabilities with the saved fitted models, without fitting. The YG supplement checks all fitted payloads including unpromoted groups, full calibration diagnostics, raw calibration scores, route/symbol assessment metrics and every support count. Candidate development scores are checked for saved selection consistency; discarded candidate estimators are not retrained. The supplement explicitly leaves `full_native_completion_verified` false because provider/planning/actuals verification belongs to the separate full audit.

Native archive APIs hash raw and normalized bytes and check their manifests; the second/minute audit compares normalized observations and DBN request headers without replaying every raw record. Original cursor snapshots are self-hashed and their semantic values match the extension manifest; current cursors are independently source-verified and cannot regress. The native extension reports exact byte preservation during prefix acquisition, but its receipt does not separately bind the entire original-cursor snapshot file by an outer hash. Ordinary daily acquisition may subsequently advance current cursors, so the verifier does not claim they must remain byte-identical to pre-extension snapshots.

The prior provider advisory comparison now points to the verified September 24 `resumed-provider/provider-completion.json`; the unused original-attempt provider report does not exist. Today's heavy verification remains pending native completion.
