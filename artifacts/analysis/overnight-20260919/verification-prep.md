# September 18 source / September 21 action verification preparation

Read-only review completed 2026-09-19. No pipeline, provider, account capture,
supervision, trading, or production-write operation was performed. The note,
supplemental verifier and its early-guard evidence are local analysis artifacts.

## Reusable full verifier

The September 18 final audit used
`artifacts/analysis/overnight-20260917/verify_completed_run.py`, not a separate
September 18 script. Its path, SHA-256, explicit dates and original run are saved
in `overnight-20260918/final-verification.json` under `verifier`. It supports
resume ancestry and eleven sections: overnight report/receipt/logs, Gameplan,
OPRA, target history, fetch-scope log, enrichment, trade plan, planning prices,
reference completion, cumulative evaluation and actuals review. It refuses to
inspect archives before the selected terminal report and receipt are COMPLETE.

Use the actual terminal attempt and original attempt IDs captured by the owner:

```powershell
$terminalRun = 'C:/DATASTORE/ml/overnight-runs/20260919T040748.657031Z'
$originalRun = '20260919T040748.657031Z'
& ./.venv/Scripts/python.exe -B artifacts/analysis/overnight-20260917/verify_completed_run.py --overnight-run $terminalRun --expected-original-run $originalRun --expected-source-date 2026-09-18 --expected-action-date 2026-09-21 --expected-deadline 2026-09-21T11:00:00Z > artifacts/analysis/overnight-20260919/final-verification.json
```

Run after native completion while the root owner continues its once-per-minute
supervision renewals. Capture `$LASTEXITCODE` immediately. Never use
`--only-fetch-log` as the final audit. Current production membership supplies
the `24 × N` and `3 × N` counts; historical evaluation uses each saved universe.
The current eleven-symbol expectation is 264 forecasts, 264 intents and 33 OPRA
cursors. The verifier already understands unavailable planning references,
signal-driven holdings and saved v2/v3 planning-reference semantics.
If a resume is necessary, substitute its terminal path while retaining the
original run ID.

## YG-specific additions

The existing full verifier already calls native `read_current_gameplan` and
`read_gameplan_run`, which now call
`ml.nightly_gameplan._verify_probability_target_metadata`. Native verification
checks raw-target identity in configuration, receipt, plan, model reports,
forecasts, four cohorts and fitted model payloads; it recomputes separate raw
and cost-adjusted labels. It also prevents raw rows without manifest provenance.
However, the old operator verifier does **not** assert that this fresh run was
supposed to be YG. The new companion `verify_yg_completion.py` supplies explicit
target checks, numerical model reproduction, exact symbol/route support and
status checks. Run it after native completion:

```powershell
& ./.venv/Scripts/python.exe -B artifacts/analysis/overnight-20260919/verify_yg_completion.py --overnight-run $terminalRun --output C:/dev/ducketz/artifacts/analysis/overnight-20260919/yg-final-verification.json
```

The helper's syntax parsed successfully. Its early guard was checked against the
running attempt and returned `NOT_READY_FOR_FINAL_VERIFICATION` with
`heavy_checks_started=false`; see `yg-verifier-preflight.json`. No saved models,
market archives or account evidence were loaded during that guard check.
It returns success for valid research-model reports with coverage notes; it
does not demand every directional group qualify. It checks:

- Every native attempt in this fresh run's resume ancestry records
  `probability_target_contract=raw-price-direction-v1`, `gameplan_variant=YG`.
  Native receipts bind these fields through stage-report checksum/size; their
  schema has no duplicate target metadata.
- The selected immutable publication's configuration, receipt, plan, reports,
  model payloads, fresh/retained cohorts and forecasts use the same pair.

After the native planning stage failed its account-ownership gate, the helper
gained an explicit `--publication-only` mode. This requires a terminal native
receipt/report and a successful publication stage, verifies their immutable
binding, and labels successful model checks
`MODEL_ARTIFACTS_VERIFIED_NATIVE_FAILED` when native status is FAILED. It keeps
`full_native_completion_verified=false`; ordinary mode still refuses anything
other than COMPLETE. The first publication-only invocation reproduced all four
assessment/raw/baseline scores exactly, with zero errors; see
`yg-publication-verification.json`. It did not read the account snapshot or
change the failed native receipt. Final complete verification remains pending.

The main final review should also confirm matching trade-plan metadata and that
cumulative evaluation preserves each publication's own target and includes
  the manifest-bound `og-yg-comparison.json` output when applicable. Keep each
  model's own Brier target separate from raw direction correctness.

For independent numerical model reproduction, do not run the old
`overnight-20260918/review_models.py` unchanged: it pins OG paths and dates,
requires cost-adjusted labels and has a pre-information-retention calibration
selection expectation. `yg-20260918/quality-followup/verify_models_quality.py`
contains the updated `repaired_group` arithmetic, source, cohort, retained
champion, logistic grid, weekly shrinkage and eligible-calibration checks, but
its CLI hard-codes September 18, output locations, the original YG and OG hash
baselines. Adapt its functions into a new run-local verifier; do not overwrite
the historical evidence or invoke its main function for September 21. For a
retained champion, reproduce scores using its `deployment.retained_cohort_output`,
not the fresh cohort, and confirm its own saved target identity and source.

Native APIs useful for the companion audit are
`ml.gameplan_promotion.build_promotion_gate` / `validate_promoted_report`,
`ml.stock_trader.independent_signals.verified_promoted_model_groups`, and
`ml.gameplan_development_selection` policy constants. Numerical qualification
and fitted enrichment status remain separate. The special September 18
deployment `prepare-revision` all-model activation gate and deadline should not
be silently generalized into another dated deployment operation.

## Contract discrepancies to report, not silently repair

1. Current `ml/stock_direction_policy.py` is `stock-direction-50-v2`: bullish
   strictly above 0.50, bearish strictly below 0.50, neutral exactly 0.50.
   `INDEPENDENT_STOCK_HORIZONS.md` lines 134–140 documents the September 14
   evening replacement of the older 46%–54% band. The automation prompt retains
   that older band. The full verifier checks the current native constants.
2. `NIGHTLY_GAMEPLAN.md` lines 293–312 still describes promoted-only projections,
   due horizon exits and expiry holdings. Current `gameplan_trade_planning.py`
   passes `signal_driven=True`; `gameplan_cash_ledger.py` documents and implements
   bullish accumulation and bearish horizon sales without model-promotion
   filtering or scheduled expiry sales. The September 16 section at
   `INDEPENDENT_STOCK_HORIZONS.md` lines 51–59 and the September 14 direct
   instruction section describe the newer behavior. Validate the saved
   `accumulate_bullish_sell_on_bearish_no_scheduled_expiry_v1` policy rather than
   inserting missing expiry transactions into a current scenario.
3. The automation prompt's 15-minute planning carry-forward ceiling is older
   than the documented sparse-session policy. Current native planning enables
   `allow_sparse_session_references=True`, uses price-band/path v3 and
   `sparse-session-planning-reference-completion-v2`, and permits at most 240
   same-session after-hours minutes after verified complete acquisition.
   `NIGHTLY_GAMEPLAN.md` lines 324–365 explicitly describes this later rule and
   preserves the older v2 15-minute current-anchor policy. Actuals/training still
   require actual observations within five minutes. Disclose actual synthetic
   anchors, historical carried references and immutable contract identity.
4. No numerical promotion-threshold discrepancy was found: native v2 requires
   Brier <= baseline + 0.005, log loss <= baseline + 0.01, retained probability
   variation, calibration quality and assessment samples. New YG development
   uses the fixed logistic grid for all four groups, information-retaining
   calibration selection, and weekly weights 0.25/0.5/0.75/1. These are documented
   at the start of NIGHTLY_GAMEPLAN.md. Historical policies remain frozen.
   The promotion source file has no pending diff and a September 8 modification
   time; its latest listed commit is `e7dac02` (September 9). No recent strict
   promotion switch was found. `nightly_gameplan.py` explicitly chooses v2 for
   independent stock selection and strict v1 for legacy non-independent runs.

This preparation does not establish final readiness; the owner must verify the
actual completed native outputs and report remaining research/missing coverage.
