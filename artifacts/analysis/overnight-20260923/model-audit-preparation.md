# September 23 model audit preparation

Prepared 2026-09-23T05:17:52Z. `review_models.py` passed AST syntax parsing only and has not run. The September 22 source helper and all immutable artifacts remain unchanged. No fitting, provider/broker call, state repair or production edit occurred.

The current helper binds original overnight `20260923T040814.422433Z`, action September 23 and unchanged 11:00 UTC deadline. It accepts an explicit current native attempt and verifies its resume ancestry. It refuses review until that ancestry has a successful `gameplan_publication` stage and the supplied immutable publication matches the current native report's saved receipt hash. The previous comparison is explicitly the verified September 22 YG publication `20260922T054940.677673Z`; it is never selected through a latest pointer.

The review reproduces the saved numerical promotion gate, fixed development-selection result, observed five-minute label boundaries, chronological partitions and exact symbol/route fitted support. It reports qualification, assessment minus baseline, saved-policy limit and margin, strict wins against both baselines, forecast status counts, minimum route support and symbols without assessment outcomes. Strict baseline wins are factual diagnostics, not replacement selection or promotion gates. Current native v2 still allows Brier excess 0.005 and log-loss excess 0.01 while retaining its calibration, variation and sample checks.

When an explicit matching enrichment run is provided, learned sizing fit and qualified scope counts are verified and reported separately. No status is relabeled. Outputs are `model-review.json`, `model-review.md` and compact `model-metric-summary.json`; hashes and preparation scope are in `model-audit-preparation.json`.

After root explicitly establishes current publication availability, run from `C:/dev/ducketz`:

```powershell
$nativeRun = 'C:/DATASTORE/ml/overnight-runs/20260923T040814.422433Z'
$nativeReport = Get-Content -LiteralPath (Join-Path $nativeRun 'stage-report.json') -Raw | ConvertFrom-Json
$publicationRun = Join-Path 'C:/DATASTORE' $nativeReport.enrichment_gameplan.run_path
& ./.venv/Scripts/python.exe -B artifacts/analysis/overnight-20260923/review_models.py --overnight-run $nativeRun --gameplan-run $publicationRun
```

If resumed, set `$nativeRun` to the actual attempt path; the helper keeps the original root/deadline. Once native enrichment finishes, rerun the same explicit publication command with `--enrichment-run` and its verified new immutable path. This refreshes only these run-local review summaries.

The model review reproduces promotion arithmetic from saved scores; it does not claim estimator-inference score reproduction. The separate `verify_yg_completion.py` performs that saved-estimator inference. Its normal invocation requires COMPLETE report/receipt. Even its `--publication-only` option deliberately requires matching terminal COMPLETE, FAILED or CANCELLED status; it does not admit RUNNING just because publication is available. Use the helper's normal final command at native COMPLETE, or publication-only for a verified terminal tail failure when root explicitly chooses that bounded review. Full overnight completion is outside `review_models.py` and remains false in its output.
