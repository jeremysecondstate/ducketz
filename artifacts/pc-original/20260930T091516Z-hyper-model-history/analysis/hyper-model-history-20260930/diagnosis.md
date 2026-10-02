# Paper holds and missing scores — September 30, 2026

Read-only capture at **02:07:33 a.m. Pacific**. The current run is `20260930-paper-round-12`, with its original 01:05:03.926644 a.m. seed. No model, runtime, policy, schedule, opening, or cadence history was changed by this investigation.

The models are producing probabilities every five minutes. **None of the four market ensembles qualifies to drive trades.** Each latest publication's own model report fails both Brier score and log-loss comparisons against both the past-prior and neutral 50% baseline. This is an actual quality failure, separate from whether a prediction is finite or successfully printed.

| Market | Latest P(not-down) | Assessment Brier | Neutral Brier | Assessment log loss | Neutral log loss |
|---|---:|---:|---:|---:|---:|
| BTC | 52.54% | 0.250896 | 0.250000 | 0.694942 | 0.693147 |
| ETH | 48.68% | 0.250328 | 0.250000 | 0.693806 | 0.693147 |
| HYPE | 51.13% | 0.250166 | 0.250000 | 0.693480 | 0.693147 |
| ZEC | 51.36% | 0.250880 | 0.250000 | 0.694927 | 0.693147 |

These are forecasts for 02:05–02:10 a.m. Pacific, published about eight seconds after 02:05. Each report contains 811 chronological assessment rows. Lower Brier/log loss is better; these are validation losses, not trading returns. The recipe has logistic regression 32%, Extra Trees 16%, histogram gradient boosting 16%, MLP 16%, and random forest 20%. The five families contribute positive weight for every market. The full per-family probabilities, losses, accuracy, and AUC are retained in `diagnosis.json` with the exact forecast and model IDs.

There is a second condition for an entry: the configured probability threshold is 57.5% for long and 42.5% for short. Published ensemble probabilities in this namespace ranged from 47.23% to 54.88%; none reached an entry threshold. The qualification failure is the first block. Lowering either check merely to obtain fills would not establish prediction quality.

## What the rows mean

The committed ledger at capture has **243 decisions: 8 fills, 187 holds, and 48 skips**. Reasons:

- 142 holds exclude an unqualified Research prediction.
- 45 holds had no currently valid forecast: 24 failed the reader's age check, and 21 expired while executable books were being fetched.
- 33 skips rounded an already tiny risk adjustment below quantity precision.
- 15 skips had an adjustment below the venue's minimum fill notional.
- All 8 fills were initial inventory reductions to the risk caps. Their $28.8592049075 fees remain in the experiment. There were no signal-driven fills.

For the screenshot at **02:00:03**, the prior BTC prediction expired at **02:00:00**. The new one was published at **02:00:06.849**, 3.073 seconds after that recorded hold. The other three markets were published by 02:00:07.045. Thus that row genuinely had no valid current probability at decision time; a later score must not be inserted into it. The model history can show both publications and expiry explicitly while the Decisions view explains the gap.

For a representative ZEC skip at 02:07:30, the proposed cap trim was only **$0.5883**: requested quantity −0.00041816, rounded to −0.0004, against the venue minimum **$10**. It was not a directional trade withheld despite a qualified signal. Repeated cap checks cause repeated small adjustment skips.

Forecast outcomes are still evaluable even with no fills: the namespace has 13 matured predictions per market at capture. This is a small forward sample, with no active/qualified observations. It cannot establish trading performance. The published forward Brier scores were BTC 0.25379, ETH 0.24233, HYPE 0.24983, ZEC 0.25875; the evidence includes the sample count and role.

## Deadline evidence preserved

A native read-only comparison was collected within the committed round deadline's five-minute window. Paper endpoint **02:05:54.781**, actual-account collection complete **02:06:16.277**; respectively 50.855 and 72.350 seconds after the 02:05:03.927 deadline. Native observation skew is 21.495 seconds, below 120 seconds. Mirror, external-flow, and performance-comparability checks pass.

Common-mark Paper equity is **$43,894.19100532** versus actual **$43,923.95325197**, an edge of **−$29.76224665** and excess return **−0.06774943 percentage points**. Native forecast evidence contains zero valid qualified in-round forecasts. This file preserves endpoint evidence only; this UI investigation did not assess, score, archive, reset, or advance another round.

Comparison: `round12-endpoint-20260930T090600Z.json`, SHA-256 `0bb2216d4e08e7b1fbc96f75fba5a93013738c2b3fce714586bb8146e63aa1ff`.

Machine evidence: `diagnosis.json`, raw read-only snapshot: `diagnosis-raw.json`.


## UI verification — 2026-09-30T09:15:15.910074+00:00

Added the model-history table and exact saved expiry/staleness reasons. 60 UI and 109 service tests passed; all336 retained model rows were matched to their own forecast/report. Wide and narrow previews are retained beside this audit. Existing Duckets GUI must be reopened to load the code. A separate automation currently owns round13 maintenance (archived); this follow-up did not change its models, lifecycle or schedule. Model quality remains unresolved.
