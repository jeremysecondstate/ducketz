# Optional learned sizing assessment audit

Audited at 2026-09-13T05:10:47.644023+00:00. Read-only inspection of saved evidence and production code; no provider calls, retraining, model publication, control changes or orders.

## Operational meaning

The previous user-facing phrase "research-only sizing" conflated an optional learned allocation strategy with the policies actually used for the current Gameplan and stock worker. The learned models do not determine the quantities in the current Gameplan or the scheduled fixed-budget worker. Their assessment failures do not explain missing COST actual prices.

- The installed Scheduled Task `Ducketz Independent Stock Session` invokes `docs/datafetch-ml/start_stock_session.ps1` without a policy override. That script defaults to `fixed-horizon-budget-v1` (line 4).
- `Start-Gameplan-Trader.cmd` line 3 explicitly selects `gameplan-direction-current-market-v1` for a manual start.
- `ml/stock_trader/independent_runtime.py` line 184 loads the learned model only for `qualified-enrichment`; lines 192 onward retain signals directly for the fixed-budget and Gameplan policies.
- `ml/gameplan_trade_planning.py` lines 124–142 compute opportunity quantities from current account evidence, configured horizon budgets and the upper planning price. The direction projection likewise uses recorded cash/holdings and configured capacity.
- `ml/gameplan_trade_planning.py` lines 373–389 append the saved enrichment assessment as optional context after quantities are calculated. It does not supply quantity authority.
- Learned heads estimate net-positive-trade probability, expected net return, downside and an allocation fraction. The optional learned engine scales a horizon cap by that allocation fraction (`ml/stock_trader/independent_engine.py`, line 93). Historical-price training does not learn broker fill/execution outcomes; execution fields retain explicit policy defaults.

## Saved assessment evidence

Source: `C:/DATASTORE/ml/stock-trader-model-runs/20260912T051308.132302Z/training-report.json`.
Model fingerprint: `83a81f77aaff781f8887a2fa6d5229be2cc161a17ce2062d20dc32b33765a460`.
Source Gameplan: `ml/nightly-gameplan-runs/20260912T051210.050260Z`.
All four models are fitted; all seven symbols have admitted targets. The saved model supports no qualified horizons. Every scope is rejected for `HELD_OUT_HORIZON_QUALITY_NOT_PROMOTED`, not for an insufficient fitted-scope count.

| Horizon | Admitted rows | Fit rows (train + selection) | Calibration rows | Assessment rows / decision clusters | Fitted / qualified scopes | First training decision UTC |
| --- | ---: | ---: | ---: | ---: | ---: | --- |
| 1h | 24853 | 18303 | 3089 | 3451 / 63 | 91 / 0 | 2025-01-11T00:05:00+00:00 |
| 4h | 7261 | 5251 | 943 | 1053 / 63 | 52 / 0 | 2025-01-11T00:05:00+00:00 |
| 1d | 1428 | 1040 | 185 | 202 / 50 | 7 / 0 | 2025-03-08T00:05:00+00:00 |
| 1w | 1416 | 988 | 162 | 194 / 50 | 28 / 0 | 2025-03-04T00:05:00+00:00 |

The quality rule (`ml/stock_trader/independent_training.py`, lines 293–297 and 332) requires at least 10 distinct assessment decision timestamps, strictly lower Brier, log loss and expected-return MSE than training-derived constant baselines, calibration error at most 0.15, downside MSE no worse than baseline, and nonconstant probability output. All current groups have sufficient assessment timestamps.

The current pooled scope policy (`ml/stock_trader/scope_qualification.py`, lines 81–100) additionally requires at least one exact-scope fitted decision and 20 fitted decisions for the same symbol/route pooled across its observed durations. Current minimum pooled counts are 32 (1h), 35 (4h), 53 (1d), and 43 (1w), so those coverage requirements pass.

| Horizon | Brier model / baseline | Log loss model / baseline | Return MSE model / baseline | Downside MSE model / baseline | Calibration error |
| --- | --- | --- | --- | --- | --- |
| 1h | 0.226223132445 / 0.230727519021 | 0.643912153427 / 0.654094495576 | 0.000103135544383 / 0.000102717482006 | 4.44041968456e-05 / 4.55504395622e-05 | 0.023864494132 |
| 4h | 0.245309324205 / 0.246988537341 | 0.683747172127 / 0.687117448905 | 0.000614763371005 / 0.000601488539218 | 0.000220947027875 / 0.000242896741991 | 0.0180555697848 |
| 1d | 0.255204873433 / 0.24957991278 | 0.70497920035 / 0.692306996892 | 0.00204441432302 / 0.00199216824552 | 0.000601734663361 / 0.000587923563566 | 0.0956366690615 |
| 1w | 0.258493717877 / 0.260236838895 | 0.713037961934 / 0.713996447821 | 0.0096736497337 / 0.00883071830409 | 0.00248890044682 / 0.00245937595193 | 0.133288669964 |

| Horizon | Failed criteria |
| --- | --- |
| 1h | expected-return MSE is 0.407% worse than baseline |
| 4h | expected-return MSE is 2.207% worse than baseline |
| 1d | Brier is 2.254% worse than baseline; log loss is 1.830% worse than baseline; expected-return MSE is 2.623% worse than baseline; downside MSE is 2.349% worse than baseline |
| 1w | expected-return MSE is 9.545% worse than baseline; downside MSE is 1.200% worse than baseline |

These are held-out model-quality results. They do not demonstrate a training process failure, inadequate symbol onboarding, or that adding historical rows necessarily fixes the model. They also do not turn an unobserved market price into a measured outcome.

## Independent review of wording changes

Reviewed the current diff in `app/ui/rolling_forecast_data.py`, `ml/gameplan_trade_review.py`, `docs/loops-system-analysis/INDEPENDENT_STOCK_HORIZONS.md`, `docs/loops-system-analysis/NIGHTLY_GAMEPLAN.md`, and `tests/test_independent_stock_forecast_ui.py`.

No blockers found. The UI changes replace labels, warnings and explanatory text only. Persisted status IDs including `RESEARCH_FORECAST`, `RESEARCH_FORECAST_IN_PROGRESS`, `OPENING_GAP_RESEARCH`, and `RESEARCH_NOT_PROMOTED` remain intact. The promoted predicate, target clocks, numeric calculations, actionability timestamps, validation rules and control logic are unchanged. The two test edits assert the replacement display strings while retaining existing status checks.

The report accurately states that these optional models are separate from the fixed sizing rule for the plan and distinguishes training completion from validation. The documentation correctly states that current Gameplan quantities and fixed-budget worker sizing do not depend on optional learned-model qualification. The current changes do not relabel any persisted model as approved.

Root reported 49 targeted tests passed; this review inspected the diff and code paths rather than rerunning those tests. Existing frozen Gameplans and model reports were not rewritten by this audit.

## Evidence checksums

- `manifest.json`: 3292 bytes, SHA-256 `9a02d396aeb442ae9082aa0791966ced4c4ae7c0ed33bb9f529f96141cf2c2e3`.
- `training-report.json`: 327392 bytes, SHA-256 `a8a404bb6e398036c600cbf2a431938b99b7b5e26a80833340d1745554e66a8f`.
- `model.json`: 6698702 bytes, SHA-256 `23b5c3d18c3ee02b0539cb6e81cb4079892394c2d89d81efd22d033c2ddca0c7`.
- `receipt.json`: 716 bytes, SHA-256 `dcbfc6a198d18c882e55c71d6fbed05ca68fe27f770f2ddc21449f90eb840455`.
