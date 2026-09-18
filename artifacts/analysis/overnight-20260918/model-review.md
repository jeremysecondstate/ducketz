# September 18 overnight model review

Reviewed at 2026-09-18T05:55:24.817733+00:00. Source session September 17; action date September 18.

Fresh immutable Gameplan: `C:\DATASTORE\ml\nightly-gameplan-runs\20260918T054532.489998Z`. All four saved assessment/raw/baseline scores reproduced exactly (maximum absolute discrepancy **0.0**), and every checked manifest-bound model/cohort/report checksum, native promotion gate, source clock, price boundary, label, chronological partition and exact symbol/route support count agreed.

## Directional qualification

The publication uses `independent-stock-directional-promotion-v2`: Brier at most baseline +0.005; log loss at most baseline +0.01; ECE at most 0.15; at least 10 assessment clusters; varying calibration/assessment probabilities and both calibration classes. Qualification within these tolerances does not claim baseline outperformance.

| Group | Status | Assessment / baseline Brier | Assessment / baseline log loss | ECE | Assessment rows / clusters |
|---|---|---|---|---|---|
| 1h | PROMOTED | 0.226472323084 / 0.235167345368 | 0.643230372412 / 0.663231043803 | 0.019362 | 4348 / 63 |
| 4h | PROMOTED | 0.249338176735 / 0.247935413019 | 0.691880151862 / 0.689013244530 | 0.025755 | 1192 / 63 |
| 1d | PROMOTED | 0.250217796945 / 0.249615087508 | 0.693670250224 / 0.692377333054 | 0.063785 | 1190 / 63 |
| 1w | RESEARCH_NOT_PROMOTED | 0.273979207991 / 0.256634774916 | 0.745838370349 / 0.706573715050 | 0.121274 | 248 / 63 |

There are 253 promoted forecast rows (154 hourly, 44 four-hour, 55 daily) and 11 unpromoted weekly rows. This includes 198 promoted entry forecasts; 55 promoted rows are non-entry context/outlooks. Every configured symbol/route has fitted target history, including the sparse TWST daily and weekly cohorts (5 and 6 fitted rows respectively); positive support is not a claim of independent per-symbol predictive quality.

Only hourly beats both baselines. Four-hour and daily pass the authorized tolerances. The daily candidate is regularized logistic C=0.001, selected as the minimum log loss over the fixed development-only C grid 0.001, 0.01, 0.1, 1 and other native candidates. All groups select the minimum saved development log loss; calibration uses separate purged development evidence and records assessment_used_for_selection=false.

## Weekly failure and bounded diagnosis

The fresh cohort has eight newly mature rows; chronological partitions advance. The saved minimum-development-log-loss blend remains 50% HGB/50% MLP. Calibration switches from Platt to identity because identity development log loss 0.7389724003705632 is below Platt 0.7391864557573372 (margin 0.000214055386774). Weekly held-out scores fail both authorized v2 tolerances, while sample, class, ECE and varying-probability gates pass. This is a validated adverse quality outcome and a narrowly separated development decision; saved-artifact checks establish no training/source/score defect. Assessment outcomes must not be used to replace calibration or select a challenger.

The weekly Brier excess is **+0.017344433074** (allowed +0.005); log-loss excess is **+0.039264655299** (allowed +0.01). Its calibrated/identity assessment range is 0.215735–0.859853. Calibration has 198 rows across 56 decision clusters and both classes. Its internal calibration development split has 72 fit rows and 96 validation rows, with 30 labels purged. All weekly targets retain observed XNAS.ITCH endpoints within five minutes; 6,734 candidate labels were excluded and 2,267 admitted, without substitution or synthetic training prices.

Compared with September 17, the weekly cohort grows from 2,259 to 2,267 rows, with no removed rows and unchanged shared labels/prices. The prior publication chose Platt on development log loss 0.730443822609 vs identity 0.731544621881 and passed v2 with Brier 0.256643306577/log loss 0.706673698246. The latest cohort rolls its partitions, and identity now wins narrowly. Earlier same-date champion retention cannot adopt a different action-date model merely because it scored better; the prior publication remains immutable.

The saved decision_timestamp retains prior-session feature availability, which may precede 17:00. It is not the actual overnight forecast dispatch. Native directional partitions purge at next target start; all twelve adjacent-partition last-label ends also precede the next earliest 17:05 post-close source_effective_cutoff. Weekly boundaries clear that cutoff by five minutes. No causal violation was demonstrated for the intended after-close forecast operation; strict source-decision separation is not claimed.

The discarded calibration candidate is not serialized, so its fit was not recreated. Selection was checked against saved development scores and implementation; all final assessment scores were reproduced by prediction only. No archive rescan or alternative candidate fitting was performed.

No concrete defect requiring repair or retraining was established. Preserve this weekly validation failure and the successfully published generation.

## Independent enrichment

Enrichment `C:\DATASTORE\ml\stock-trader-model-runs\20260918T054724.507836Z` is bound to the reviewed Gameplan. All four groups fitted; **zero scopes qualified**, supported_horizons and qualified_target_contracts are empty. Training reported zero orders.

| Group | Fitted scopes | Qualified scopes | Failed horizon quality conditions |
|---|---:|---:|---|
| 1h | 143 | 0 | return_mse_beats_baseline |
| 4h | 78 | 0 | brier_beats_baseline, log_loss_beats_baseline, return_mse_beats_baseline |
| 1d | 11 | 0 | brier_beats_baseline, log_loss_beats_baseline, return_mse_beats_baseline, probability_varies |
| 1w | 41 | 0 | brier_beats_baseline, log_loss_beats_baseline, return_mse_beats_baseline, adverse_mse_at_most_baseline |

Eleven scope diagnostics additionally lack sufficient pooled symbol/route evidence (6 hourly, 2 four-hour, 1 daily, 2 weekly); details are retained in the JSON and native training report. Enrichment uses its own strict quality policy, including return and adverse-return errors, and is separate from directional v2 promotion.

Learned enrichment is not required by manual gameplan-direction-current-market-v1 or fixed-horizon-budget-v1; qualified-enrichment requires it. The current manual Gameplan policy consumes saved trading instructions through load_execution_signals without checking model promotion, so the weekly directional validation failure does not itself block manual weekly instructions. Legacy fixed and qualified-enrichment policies retain their separate promotion and research gates. Weekly remains RESEARCH_NOT_PROMOTED in this publication. This corrects reporting only; no live behavior or trading controls changed.

The audit made no code, trading control, provider, broker, training, publication or supervision ownership changes. It wrote only these analysis artifacts.

Machine-readable evidence: `model-review.json`; reproducible prediction-only audit: `review_models.py`.
