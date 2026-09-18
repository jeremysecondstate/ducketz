# September 18 Yung Gameplan deployment

Completed September 18, 2026, approximately 00:40 Pacific (07:40 UTC). The operator explicitly chose the upcoming September 18 session for YG and retained the original OG solely for evaluation and differential analysis.

YG now predicts strictly positive raw observed price return (`raw-price-direction-v1`). Cost/profitability targets remain separate. Hourly and four-hour selection now evaluates the same fixed logistic C grid as daily: 0.001, 0.01, 0.1, 1, selected on development data. Chronological partitions, calibration, sample/source checks and assessment criteria remain in place. Legacy publications and resumes retain their original target identity; cross-target champions are rejected.

## Verified active source

- ACTIVE deployment: `C:/DATASTORE/ml/gameplan-deployment-runs/20260918T073523.985203Z`.
- YG publication: `C:/DATASTORE/ml/nightly-gameplan-runs/20260918T072555.813034Z`.
- Readable YG: `C:/DATASTORE/ml/gameplan-trade-plan-runs/20260918T072910.923004Z/Gameplan.md`.
- Frozen OG: `C:/DATASTORE/ml/nightly-gameplan-runs/20260918T054532.489998Z` and its original trade plan `20260918T054811.145288Z`.
- Comparison: `C:/DATASTORE/ml/gameplan-deployment-runs/20260918T073523.985203Z/Comparison.md`.

Native run `C:/DATASTORE/ml/overnight-runs/20260918T072554.369355Z` completed publication, enrichment and trade planning at 07:31:25 UTC, before the unchanged 04:00 Pacific deadline. Completed provider/history work was reused. No retry or deadline exception was needed. This expressly narrower replacement run did not rerun prior-session actuals.

The verified dated source guard accepts the pinned YG and rejects OG for new entries/directional instructions. A read-only opening-reader check returned 44 instructions, all bound to the YG source; no broker calls or entry-slot claims occurred. Source checks also cover actual loaded instructions before reservation/submission, and preserve existing owned exits. Source selection does not enable trading controls or launch a trader.

## Quality and validation

All 345 saved-output checks passed: 264 forecasts, 264 stock-only intents and 264 augmented rows across eleven symbols; all 154 hourly prices; source/receipt/manifest bindings; unchanged OG; and every conditional cash/share event and summary. The seven CROX synthetic planning minutes remain explicitly disclosed with the original observed timestamp intact. Saved assessment scores reproduce exactly. Focused training, evaluation, UI, runtime, deployment and resume tests passed; test batches overlap and are not summed.

Four-hour and daily models pass the recorded v2 tolerances, which do not assert baseline outperformance. Hourly fails retained directional information because development-selected calibration is flat. Weekly fails Brier/log-loss tolerances. No source, label, inference or fitting defect justified another training attempt. All four independent enrichment groups fitted; none qualified.

The current conditional plan contains ten buys, all weekly, and no sales. The existing manually selected Gameplan execution policy consumes saved instructions independently of model promotion; that behavior was preserved. The weekly assessment failure therefore does not itself suppress those instructions. Improved future accuracy is not established.

Actual native UI verification showed the September 18 Yung Gameplan with all 264 forecasts and eleven companies. OG Gameplan Stats retains September 17 results: 62/164 correct (37.8%), Brier 0.2412877260, 66 pending maturity and 34 awaiting data. The app was left on YG with All horizons, All companies and All forecasts selected. Future completed outcomes are compared using matched source/window identities; raw-direction and cost-target probability scores remain distinct.

No trader was started, no orders were placed, and trading controls/risk/schedules were unchanged. The Windows stock-session launcher remains disabled; the user's normal manual initial-start procedure still applies. Supervision owner: `1356f80a-178d-4ebb-bf4b-aff394fe9d44`; release is recorded separately in `supervision-release.json`.

Evidence in this directory: `output-review.md/json`, `model-review.md/json`, `active-source-verification.json`, and the focused test/review artifacts. Do not repeat the completed OG or YG preparation on subsequent wakes.
