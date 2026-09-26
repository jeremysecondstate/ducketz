# September 28 Gameplan — verified with coverage notes

**Final verification passed with coverage notes at September 25, 23:46:06 Pacific** (`2026-09-26T06:46:06Z`). All four audit commands exited zero; all 12 full-audit sections passed with no errors. Remaining limitations are the disclosed model qualifications, missing observations and planning assumptions below. Supervision was released at September 25, 23:55:39 Pacific after final peer review. [Audit runner](C:/dev/ducketz/artifacts/analysis/overnight-20260926/verification/audit-runner.json) · [full audit](C:/dev/ducketz/artifacts/analysis/overnight-20260926/verification/completion-audit.json).

The native eight-stage run completed at **September 25, 23:31:24 Pacific** (`2026-09-26T06:31:24Z`), preparing **Monday September 28** from Friday September 25. It retained `--archive-history`, XNAS.ITCH minute targets and the original **September 28, 04:00 Pacific** deadline. The native receipt records **zero orders** and disabled broker-order authority. All eight stages succeeded in the original attempt, without a restart. [Native receipt](C:/DATASTORE/ml/overnight-runs/20260926T040723.834511Z/receipt.json) · [stage report](C:/DATASTORE/ml/overnight-runs/20260926T040723.834511Z/stage-report.json).

The readable [September 28 Gameplan](C:/DATASTORE/ml/gameplan-trade-plan-runs/20260926T062723.283372Z/Gameplan.md) references the pinned [YG publication](C:/DATASTORE/ml/nightly-gameplan-runs/20260926T061604.318956Z/gameplan.json). The eleven-symbol grid has **264 forecasts and 264 stock-only intents**, including 209 entry windows. The publication receipt hash is `4f1e9c7a6a01868a194b5015d106163ea322655322f59d53b4a297e5daebbd4d`. [Publication receipt](C:/DATASTORE/ml/nightly-gameplan-runs/20260926T061604.318956Z/receipt.json).

**Model quality.** All four directional groups pass their saved v2 operating criteria. Saved estimator inference reproduced assessment scores, route/symbol metrics, calibration diagnostics and support counts with **zero maximum score error**. All 264 current raw/calibrated forecasts also reproduced exactly from rebuilt archive features. All groups retain probability variation and 63 assessment decision clusters. Only 4h and 1d outperform both training-rate baselines; 1h and 1w qualify within the approved tolerances. [Model review](C:/dev/ducketz/artifacts/analysis/overnight-20260926/verification/model-review.md) · [inference audit](C:/dev/ducketz/artifacts/analysis/overnight-20260926/verification/yg-completion.json).

| Horizon | Directional status | Brier: actual / baseline / allowed maximum | Log loss: actual / baseline / allowed maximum |
|---|---|---|---|
| 1h | PROMOTED | 0.250356 / 0.250000 / 0.255000 | 0.693873 / 0.693147 / 0.703147 |
| 4h | PROMOTED | 0.249715 / 0.250035 / 0.255035 | 0.692577 / 0.693216 / 0.703216 |
| 1d | PROMOTED | 0.250459 / 0.250768 / 0.255768 | 0.694069 / 0.694683 / 0.704683 |
| 1w | PROMOTED | 0.253310 / 0.251549 / 0.256549 | 0.699879 / 0.696275 / 0.706275 |

TWST remains thin: one fitted row for `1h@16:00`, two per daily route, and eight weekly rows. Group qualification does not establish precise accuracy for those scopes.

**Historical integration.** The audit verified **944 manifest-bound source files totaling 949,223,542 bytes** and reproduced every eligible cohort row exactly. Existing minute prefixes were reused with **zero new prefix requests**; normal daily acquisition completed 11 exact requests at $0 with capacity checks. All 11 original cursor snapshots verified and current cursors did not regress. [Archive report](C:/DATASTORE/ml/nightly-gameplan-runs/20260926T061604.318956Z/archive-history.json) · [history receipt](C:/DATASTORE/ml/stock-target-history-runs/20260926T060449.032360Z/receipt.json).

| Horizon | Reproduced rows | Cohort action dates | Additional quality exclusions |
|---|---:|---|---:|
| 1h | 171,767 | 2018-05-31–2026-09-25 | 217 |
| 4h | 42,928 | 2018-05-31–2026-09-25 | 66 |
| 1d | 29,677 | 2019-09-18–2026-09-25 | 105 |
| 1w | 5,846 | 2019-09-18–2026-09-21 | 89 |

Causal feature histories end at source session **September 25**, preparing action date **September 28**. Verified first source/action dates are:

| Symbols | First feature source session | First feature action date |
|---|---|---|
| AAPL, AMZN, COST, GOOG, MU, NVDA | 2019-09-17 | 2019-09-18 |
| CROX | 2018-05-30 | 2018-05-31 |
| TWST | 2018-11-29 | 2018-11-30 |
| PATH | 2021-05-19 | 2021-05-20 |
| IONQ | 2021-10-29 | 2021-11-01 |
| SNDK | 2025-03-24 | 2025-03-25 |

The reconstruction preserves **25 quality resets, 70 excluded intervals, 26 split boundaries, five target-discontinuity boundaries and 17 undefined-observation exclusions**. Across 212 verified second/minute partitions, **16,443,370 second rows** support **2,842,334 exact overlapping minute OHLCV comparisons** across all eleven symbols, with zero extra training rows, synthetic rows or unmatched eligible seconds-only minutes. Undefined observations remain excluded.

Verification checked native payload hashes, normalized values and DBN request headers; it **did not replay every raw DBN record**. Original cursor contents are individually self-hashed and semantically bound to the extension manifest, but the native receipt does not separately hash the entire original-cursor snapshot file. These limits do not imply byte-for-byte equality after legitimate daily cursor advancement.

Separate learned sizing models fitted all four horizons but have **zero qualified scopes**. Return MSE fails its strict baseline gate in every group; daily/weekly also fail Brier and log loss, and weekly fails downside MSE. Development minima, calibration, convergence, chronological purges and qualification checks agree. Exact optional-input admission/exclusion was reconstructed: 93,040/72,749 rows (1h), 25,082/17,846 (4h), 3,791/2,141 (1d), and 3,765/2,081 (1w). No concrete fitting/data defect or justified unchanged retry was found. [Final model evidence](C:/dev/ducketz/artifacts/analysis/overnight-20260926/verification/model-review.json) · [original training report](C:/DATASTORE/ml/stock-trader-model-runs/20260926T062539.552484Z/training-report.json).

**Planning scenario.** The read-only September 25, 23:27:28 Pacific snapshot reports **$1,652.39 available cash**, no working orders and no cash reservations. Its conditional ledger contains **28 events: 16 buys and 12 sells**; projected ending cash is **$102.94–$384.80**, with **$243.58** at the midpoint. Recomputed capacity, all 154 hourly prices, every cash/share event, hourly balance and ending total verified. These are planned-fill assumptions before fees/taxes; actual fills, cash and holdings remain authoritative. [Account snapshot](C:/DATASTORE/ml/gameplan-trade-plan-runs/20260926T062723.283372Z/account-snapshot.json) · [cash/share ledger](C:/DATASTORE/ml/gameplan-trade-plan-runs/20260926T062723.283372Z/direction-ledger.json).

Four current prior-close anchors use **307 explicitly synthetic, zero-volume planning minutes**. Their original observations are unchanged; each effective boundary is September 25 at 17:00 Pacific. The recorded reason is `ASSUMED_NO_TRADES`, not proof that no trading occurred. Training, evaluation and actuals retain actual observations and the five-minute boundary rule. [Reference completion evidence](C:/DATASTORE/ml/gameplan-trade-plan-runs/20260926T062723.283372Z/planning-reference-completion.json) · [synthetic planning bars](C:/DATASTORE/ml/gameplan-trade-plan-runs/20260926T062723.283372Z/synthetic-reference-bars.parquet).

| Symbol | Carried price | Original observation, September 25 Pacific | Gap / synthetic minutes |
|---|---:|---|---:|
| COST | $921.72 | 16:52 | 8 |
| CROX | $126.14 | 13:01 | 239 |
| PATH | $12.48 | 16:52 | 8 |
| TWST | $184.49 | 16:08 | 52 |

The saved current contracts are **`stock-direction-50-v2`** (bullish above 50%, bearish below 50%), **signal-driven accumulation/sales without scheduled expiry exits**, and **planning v3 / `sparse-session-planning-reference-completion-v2`**, allowing bounded same-session after-hours references up to 240 minutes. These documented September 14/16 contracts differ from older 54%/46%, expiry-sale and 15-minute boilerplate; this run did not change their behavior. [Current contract](C:/dev/ducketz/docs/loops-system-analysis/INDEPENDENT_STOCK_HORIZONS.md:60) · [planning report](C:/DATASTORE/ml/gameplan-trade-plan-runs/20260926T062723.283372Z/report.json).

**Actual outcomes and coverage.** The September 25 review preserves every frozen forecast/trade-plan column and all original hourly estimates: **160 forecasts evaluated, 38 mature awaiting data, 66 pending maturity**; **137 hourly prices compared, 17 awaiting data**. Raw direction was correct for **92/160 (57.5%)**, including eight evaluated opening-gap research rows. The full audit recomputed outcomes from the original verified archive with no synthetic actuals. All mature-missing intervals have complete source coverage; their missing/stale observations remain unavailable. Complete acquisition does not prove a qualifying trade occurred. [September 25 results](C:/DATASTORE/ml/gameplan-actuals-review-runs/20260926T063044.178275Z/Gameplan-results.md) · [detailed coverage](C:/dev/ducketz/artifacts/analysis/overnight-20260926/preflight/actuals-coverage-review.md).

The publication-refreshed cumulative evaluation at **`20260926T062336.586580Z`** verifies **5,304 saved forecasts: 4,245 evaluated, 894 mature awaiting data, 165 pending**, from September 4 onward. Historical publications retain their own universe and target semantics. Observed prices are not broker fills or realized P/L. [Current cumulative review](C:/DATASTORE/ml/gameplan-evaluation-runs/20260926T062336.586580Z/review.md) · [summary](C:/DATASTORE/ml/gameplan-evaluation-runs/20260926T062336.586580Z/summary.json).

**Provider coverage.** All five provider scopes completed; 456 logged stock Parquet paths exist. All **33 OPRA scopes/cursors**, exact zero-dollar preflights and **66 raw/normalized payload hashes** passed, with no discrepancies. Estimated acquisition was **4,854,402,936 bytes**, below the 20 GB cap. [Provider audit](C:/dev/ducketz/artifacts/analysis/overnight-20260926/verification/provider-completion.md).

Optional limitations remain: all 99 Pricing routes fail unchanged admission gates against the retained August 20 authority, so verified non-Pricing fallbacks remain active. CME rejected the same stale September 3 partition-backed context; its message changes with age. All six current captures contain their symbols, but capped book requests remain saturated. One transient 504 recovered through the native retry. No new actionable provider defect was identified. [Pricing evidence](C:/dev/ducketz/artifacts/analysis/overnight-20260926/preflight/pricing-gate-review.md) · [CME evidence](C:/dev/ducketz/artifacts/analysis/overnight-20260926/preflight/cme-advisory-review.md).

The provider audit's `ADVISORY_REVIEW_REQUIRED` flag concerns only the changed CME age message; the linked bounded CME review resolves that review requirement and preserves the optional exclusion. FMP's retained full-history energy-context input still fails on the same September 2 quote clock skew (**9.471 seconds**, versus five allowed); the current quote passes pure validation and produced no current diagnostic. Neither retained rejection was hidden or weakened. [Provider advisory evidence](C:/dev/ducketz/artifacts/analysis/overnight-20260926/verification/provider-completion.json).

**Preservation.** All 21 preservation checks passed: operator controls, environment, launchers, seven automation definitions, Windows task, 51 entry/recovery claims, watchlist and terminal stock session remained unchanged. Ownership, shares and allocations were logically unchanged; pending reservations/blocks remain empty. No broker reconciliation or ledger mutation was needed. The prior daytime session still reports `FINISHED_WITH_ERRORS` with its separately documented boundary failures; the preexisting disabled Windows launcher remains disabled. Six unrelated Hyperliquid processes were preserved. [Preservation review](C:/dev/ducketz/artifacts/analysis/overnight-20260926/preflight/final-preservation.md).

The implementation guard admitted only **six exact reviewed Hyperliquid hashes** after a dependency review found no references from 237 non-Hyperliquid sources. The original baseline is intact and before/after audit guard results match. No native stock-pipeline repair, trader start or trading-control change was made. [Explicit code review](C:/dev/ducketz/artifacts/analysis/overnight-20260926/verification/reviewed-isolated-code-drift.json).

**Operator closure complete:** the monitor stopped and the root released its supervision UUID at `2026-09-26T06:55:39Z`. Final peer review verified the figures, caveats and all 29 original evidence links. The usage interruption occurred after all substantive work was complete; the expired claim was reacquired solely to close it, with no pipeline or repair running during that gap. [Release receipt](C:/dev/ducketz/artifacts/analysis/overnight-20260926/supervision-release.json). No rerun is needed for the completed September 25 source session.
