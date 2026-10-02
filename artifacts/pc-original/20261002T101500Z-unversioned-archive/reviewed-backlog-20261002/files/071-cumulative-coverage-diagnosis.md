# Cumulative coverage diagnosis

Read-only review completed October 1, 2026, around 06:47 UTC for evaluation `20261001T064116.865175Z`. The saved cutoff is `2026-10-01T06:34:05.960511+00:00`.

The 6,096 forecast rows contain **4,868 evaluated, 1,063 mature awaiting data and 165 pending maturity**. All 28 immutable publications match their own saved universe, forecast identities, probabilities and windows, with exactly 24 forecasts per saved symbol. A company absent before its onboarding is not counted as missing.

The 1,063 missing rows comprise:

- **820 explicit XNAS.ITCH rows.** Every row lacks at least one eligible native endpoint. All required start/end acquisition windows are covered by exact evaluator-bound native partition evidence. Across repeated publications there are 1,152 missing endpoint occurrences: 1,149 nearest observations outside the five-minute tolerance and three CROX September 30 afternoon starts with no later observation available in the saved source. No missing XNAS row has both valid endpoints present.
- **156 historical legacy forecasts:** 38 under `overnight-path-targets-v1`, 118 under `overnight-path-targets-v2`. They retain their original endpoint/feature matching.
- **87 historical independent forecasts** with absent explicit source metadata, which the existing native compatibility rule resolves to `canonical-equity-minute-v1` / EQUS.MINI. These are not relabeled XNAS.

All 243 non-XNAS missing rows were already missing in the preceding checksum-bound evaluation. The review did not reconstruct their older legacy outcome builders or substitute another dataset.

Compared with the exact prior evaluation, all 4,669 previously evaluated rows and their scores/timestamps remain unchanged. The prior 998 mature missing rows remain missing. The increase of 65 consists of 28 formerly pending rows that just matured and 37 newly included September 30 rows; 38 formerly pending rows became evaluated. This is not a loss of previously verified outcomes.

The **September 30** publication has 161 evaluated, 37 mature missing and 66 pending. Missing forecasts are COST 4, CROX 11, IONQ 4, PATH 9 and TWST 9. Their counts by horizon are 27 hourly, five four-hour and five daily. The new missing rows and newly matured missing rows all have complete saved XNAS request coverage; the limitation is eligible observed endpoints.

Across all dates, missing XNAS rows are:

| Symbol | 1h | 4h | 1d | 1w | Total |
|---|---:|---:|---:|---:|---:|
| AAPL | 1 | 1 | 0 | 0 | 2 |
| COST | 70 | 22 | 38 | 7 | 137 |
| CROX | 119 | 43 | 65 | 11 | 238 |
| GOOG | 3 | 1 | 0 | 0 | 4 |
| IONQ | 20 | 4 | 11 | 2 | 37 |
| PATH | 94 | 27 | 61 | 11 | 193 |
| TWST | 103 | 35 | 60 | 11 | 209 |
| **Total** | **410** | **133** | **235** | **42** | **820** |

AMZN, MU, NVDA and SNDK have no missing XNAS rows. Their older non-XNAS results remain separately recorded in the JSON matrix.

All 165 pending targets end after the saved cutoff: 110 daily, 44 weekly and 11 four-hour. Maturity alone will not guarantee a score if either required endpoint remains absent. These rows are not unfinished acquisition or missing current-session data.

This bounded diagnosis checked manifest/receipt/output hashes and the original 171 recent XNAS minute partitions used by the evaluator, comprising 137,537 distinct observed minutes. It did not repeat raw DBN reconstruction, fit a model, rerun the evaluator, call a provider/broker, or write production state. Complete XNAS request coverage is not proof that no trades occurred across the market. The existing five-minute actual-price rules remain in force; synthetic planning references cannot supply actual outcomes.

Exact hashes, saved universes, source/history/symbol/horizon counts, status transitions and every XNAS missing-row endpoint are in [cumulative-coverage-diagnosis.json](<LOCAL_CHECKOUT>/artifacts/analysis/overnight-20260930/run-20261001T040644/cumulative-coverage-diagnosis.json).
