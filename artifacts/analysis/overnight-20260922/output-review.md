# September 22 Gameplan and September 21 actuals review

Reviewed **2026-09-22T05:59:38.447327+00:00**. Readable output and saved evidence are consistent within this bounded review; root independently verifies full source bindings, hashes and cash/share conservation.

## Fresh account and conditional scenario

The September 22 plan uses the pinned YG publication `20260922T054940.677673Z`. Fresh read-only account capture at **September 21 22:52:39 Pacific** records **$21,142.53 literal available cash**, account value **$130,802.57**, exposure **$109,660.04**, **zero working orders** and **zero reservations**. The separate broker capacity of $90,672.03 is not substituted for literal cash. Settled-cash reporting is unavailable. Capture succeeded on its first attempt.

Original saved ownership is **OBSERVED_CONSISTENT**, account-matched and safe, with no reasons or blocked symbols; **planning_override is null**. All 17 existing horizon allocations remain evidenced. Planning read the saved ledger and fresh broker snapshot without performing native reconciliation or assigning actual holdings.

All **264 augmented rows / 24 per symbol** appear in eleven adjacent capacity/direction tables. The ledger contains **38 conditional buys and 20 bearish sales**, with **14 hourly summaries** and **no scheduled expiry sales**. It preserves the **$1,057.1265** cash buffer. Conditional ending cash is **$26,720.55–$27,457.11**, base **$27,091.30**, before fees and taxes. These are planned fills, not submitted orders, observed fills or realized P/L.

| Stock | Starting shares | Conditional ending shares |
|---|---:|---:|
| AAPL | 24 | 0 |
| AMZN | 39 | 57 |
| COST | 0 | 2 |
| CROX | 46 | 53 |
| GOOG | 40 | 10 |
| IONQ | 159 | 0 |
| MU | 9 | 14 |
| NVDA | 46 | 58 |
| PATH | 467 | 945 |
| SNDK | 9 | 6 |
| TWST | 61 | 78 |

The no-fill baseline retains **$21,142.53 cash and every starting share above**. Actual missing fills or unavailable sale proceeds require recalculation. The recorded policy accumulates bullish holdings and sells on same-horizon bearish signals; forecast boundaries do not create automatic sales.

**Model caveat:** the readable plan reports **198/209 entry windows passed assessment**. Eleven weekly forecasts did not pass; the recorded manual-policy scenario still contains seven weekly buys and three weekly bearish sales. Do not describe all directional models as qualified.

## Planning prices and disclosed assumptions

All **154 hourly price points** are available, observed under one fixed planning cutoff **2026-09-22 05:52:49.078114 UTC**. The current path uses XNAS.ITCH and the recorded sparse-session planning-reference v2 / conditional-path v3 contracts. The readable review discloses all three current carries:

| Symbol | Last observed close | Observed time, Sep 21 Pacific | Effective boundary | Gap / synthetic minute bars |
|---|---:|---|---|---:|
| CROX | $123.00 | 16:53 | 17:00 | 7 |
| PATH | $13.62 | 16:31 | 17:00 | 29 |
| TWST | $165.00 | 13:32 | 17:00 | 208 |

There are **244 synthetic zero-volume bars** under the explicit **ASSUMED_NO_TRADES** planning assumption. TWST’s **208-minute** carry is material and must remain visible. Same-session native acquisition coverage is recorded for each anchor; original observation timestamps remain separate from the 17:00 effective boundary. Historical planning provenance records **941 observed and 390 carried closing references**. Native/training price modification flags are false; the five-minute actuals rules remain separate.

## September 21 frozen forecast outcomes

The results review selects frozen YG `20260919T061628.240412Z` and records **161 evaluated, 37 mature missing observations, 66 pending maturity**. Raw-price direction correctness is **93/161 = 57.76%**; future, neutral and missing outcomes do not enter that accuracy. This is separate from cost-adjusted model scores and broker P/L.

There is **no matching pre-open September 21 trade plan**. All **154 saved estimates/midpoints are absent**; **133 actual same-clock prices** are present and 21 are missing within the native rule. No price errors or in-range results are calculated. Missing estimates were not rebuilt, and the old September 18 trade plan was not misused as Monday’s scenario.

Missing forecast outcomes are COST 6, CROX 11, PATH 10 and TWST 10. Their saved source-bound diagnostics report complete acquisition coverage at both boundaries; absent or out-of-tolerance observations remain unavailable. All 154 same-clock comparison rows also retain complete acquisition coverage. Repeating an unchanged download would not establish the missing observations.

Evidence: [readable September 22 Gameplan](C:/DATASTORE/ml/gameplan-trade-plan-runs/20260922T055228.508721Z/Gameplan.md), [cash/share ledger](C:/DATASTORE/ml/gameplan-trade-plan-runs/20260922T055228.508721Z/direction-ledger.json), [reference assumptions](C:/DATASTORE/ml/gameplan-trade-plan-runs/20260922T055228.508721Z/planning-reference-completion.json), [September 21 results](C:/DATASTORE/ml/gameplan-actuals-review-runs/20260922T055447.175940Z/Gameplan-results.md), [structured review](C:/dev/ducketz/artifacts/analysis/overnight-20260922/output-review.json).

This review read only saved output JSON, small output Parquets and Markdown. It made no broker/provider requests, scanned no market archive, changed no production artifact and did not duplicate root’s full price/hash verification.
