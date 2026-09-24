# September 24 Gameplan — complete and verified

**Native preparation completed at 2026-09-24 06:50:11 UTC / September 23 23:50:11 PDT.**
It prepared the **September 24 action session from the completed September 23 source session**,
before both the 03:30 Pacific completion target and unchanged September 24 04:00 Pacific
hard deadline (`2026-09-24T11:00:00Z`). The 21:05 Pacific recurring schedule is unchanged.

**Final verification passed with zero evidence errors.** The complete native/archive
audit returned `VERIFIED_WITH_COVERAGE_NOTES`; all twelve check sections passed. The
separate directional estimator-inference audit returned `VERIFIED`, confirmed native
completion, and reproduced every reported assessment score with **maximum absolute
error 0.0 for all four horizons**. Coverage limitations and unqualified sizing remain
explicit below. [Full verification](C:/dev/ducketz/artifacts/analysis/overnight-20260924/final-verification.json),
[exact model-inference verification](C:/dev/ducketz/artifacts/analysis/overnight-20260924/yg-final-verification.json).

The readable [September 24 Gameplan](C:/DATASTORE/ml/gameplan-trade-plan-runs/20260924T064617.781093Z/Gameplan.md)
contains all **264 forecasts, 264 stock-only option intents, and 264 augmented planning rows**
for the eleven configured symbols. All **33 production OPRA symbol/schema cursors** cover
the September 23 source session. The [native final receipt](C:/DATASTORE/ml/overnight-runs/20260924T064422.315446Z/receipt.json)
reports `COMPLETE`, zero orders and disabled overnight broker-order authority.

## Recovery and immutable source

The original attempt `20260924T040733.615371Z` stopped after Schwab rejected a refresh
with `invalid_grant`. Its persisted rejection prevented repeated OAuth POSTs. The user
completed local reauthorization; safe metadata verified a new authorization at
04:42:25 UTC and a cleared rejection marker. Descendant `20260924T044352.904210Z`
resumed the unfinished fetch and completed publication. All eleven subsequent Schwab
captures succeeded. [Reauthorization evidence](C:/dev/ducketz/artifacts/analysis/overnight-20260924/resume-notes.md)
retains only safe metadata.

Enrichment then exposed an integration mismatch: older archive rows correctly lacked
eight optional operational sizing inputs, while the sizing fitter required them on
every row. The focused training-only repair validates all target evidence first,
excludes only all-eight-null inputs under the archive contract, records counts and
identity hashes, and reproduces that admission during source verification. Malformed,
partial, infinite and legacy missing inputs still fail. **64 focused tests passed**;
nullable-selector hardening changes no current cohort or completed model.
[Repair and tests](C:/dev/ducketz/artifacts/analysis/overnight-20260924/enrichment-repair.md),
[final test output](C:/dev/ducketz/artifacts/analysis/overnight-20260924/enrichment-repair-tests.txt).

Final descendant `20260924T064422.315446Z` resumed only enrichment, planning and actuals.
The complete ancestry is:

`20260924T040733.615371Z` → `20260924T044352.904210Z` → `20260924T064422.315446Z`.

Every descendant retained the same archive/XNAS/raw-direction YG policy and original
deadline. Publication [20260924T062615.050242Z](C:/DATASTORE/ml/nightly-gameplan-runs/20260924T062615.050242Z/receipt.json)
remained pinned; its receipt SHA-256 is
`c107c5300ae50ff24d26cd77451db49224969f38b318d01c0c75376466235853`.
Successful acquisition, evaluation and directional training/publication were not rerun
for the enrichment repair. All eight required stages completed across this ancestry.

## Historical integration

The runtime recomputed six missing XNAS minute prefixes: **AAPL, AMZN, COST, GOOG, MU
and NVDA, 2019-08-19 through exclusive 2025-01-13**. The other five symbols already
had the required coverage. Fresh exact preflights returned **$0**, **5,285,468 records**
and **295,986,208 estimated bytes**, below the 20 GB limit. Required free capacity was
5,960,681,536 bytes; available capacity was 1,346,755,018,752 bytes. The native extension
reports verified completion and exact cursor preservation during prefix acquisition;
ordinary current-session acquisition then legitimately advances current cursors.
[Native history receipt](C:/DATASTORE/ml/stock-target-history-runs/20260924T061929.906354Z/receipt.json),
[prefix manifest](C:/DATASTORE/ml/stock-target-history-runs/20260924T061929.906354Z/feature-history-manifest.json),
[preflight](C:/DATASTORE/ml/stock-target-history-runs/20260924T061929.906354Z/feature-history-preflight.json).

The manifest-bound archive report records **17,080 causal feature contexts**, with action
dates from 2018-05-31 through 2026-09-24. Published training cohorts are:

| Horizon | Rows | First action date | Last action date | Quality-excluded targets |
|---|---:|---|---|---:|
| 1h | 171,511 | 2018-05-31 | 2026-09-23 | 217 |
| 4h | 42,858 | 2018-05-31 | 2026-09-23 | 66 |
| 1d | 29,602 | 2019-09-18 | 2026-09-23 | 105 |
| 1w | 5,831 | 2019-09-18 | 2026-09-17 | 89 |

Native second/minute verification reports **190 partitions**, **16,443,370 second rows**
and **2,842,334 exact overlapping minute OHLCV matches**. No unmatched second-only
minutes supplied training rows; **synthetic training rows and additional training rows
from seconds are both zero**. Six undefined second observations and sixteen undefined
minute observations are disclosed. The final independent audit verified **856 bound
source files totaling 946,734,200 bytes** and exactly reproduced every eligible row of
all four archive cohorts and the second/minute consistency report.
[Archive report](C:/DATASTORE/ml/nightly-gameplan-runs/20260924T062615.050242Z/archive-history.json).

The bounded quality audit verified all **53 provider-degraded symbol/schema/date
intervals** for 2021-07-07, 2021-10-26 and 2022-09-19, plus **17 undefined-observation
exclusions**, with 25 quality resets, 26 split boundaries and five raw-discontinuity
boundaries. All saved cohorts have zero target-window crossings of excluded
quality, split or discontinuity boundaries, and zero invalid warmup contexts. SNDK's
old warnings precede its first daily observation, so no pre-IPO context is invented.
[Exact quality audit](C:/dev/ducketz/artifacts/analysis/overnight-20260924/resumed-provider/archive-quality-exclusion-review.md).

## Directional and sizing results

Saved-model review reproduced source/partition/support evidence and promotion arithmetic
with **zero evidence errors**. All four directional groups and all **264 forecast rows
are `PROMOTED`** under their unchanged v2 criteria. This is separate from sizing quality:
all four sizing models fitted, and **none qualified**.

| Horizon | Directional status | Sizing fit / fitted scopes | Qualified scopes | Failed sizing quality checks |
|---|---|---:|---:|---|
| 1h | PROMOTED | FITTED / 143 | 0 | Return MSE superiority |
| 4h | PROMOTED | FITTED / 78 | 0 | Return MSE superiority |
| 1d | PROMOTED | FITTED / 11 | 0 | Brier, log-loss and return MSE superiority |
| 1w | PROMOTED | FITTED / 39 | 0 | Brier, log-loss, return MSE and downside MSE |

Directional 1h and 1w scores are worse than their training-rate baselines but within
the saved v2 allowances; 4h and 1d beat both baselines. Qualification does not imply
baseline outperformance or future predictive success. Every exact route has fitted
history, although the smallest TWST counts remain sparse: hourly 16:00 has one fitted
example, daily D+1 has two and weekly has eight. No quality status was relabeled and
assessment outcomes did not choose a retry or candidate.

Sizing retained 92,800 / 25,012 / 3,776 / 3,750 finite causal rows for 1h / 4h / 1d / 1w,
respectively. Missing optional inputs excluded 72,749 / 17,846 / 2,141 / 2,081 rows;
these exclusions affect only sizing. All eligible directional archive rows remain
published. No concrete fitting, scoring or development-selection defect was found
after the admission repair. [Saved model review](C:/dev/ducketz/artifacts/analysis/overnight-20260924/model-review.md),
[sizing arithmetic](C:/dev/ducketz/artifacts/analysis/overnight-20260924/sizing-quality-review.json),
[sizing publication](C:/DATASTORE/ml/stock-trader-model-runs/20260924T064431.699829Z/training-report.json).

## Planning and actuals

Planning has **154 available hourly price points** and a complete conserved cash/share
projection from fresh literal cash **$100,438.79**, no pending reserved cash and zero
working orders. The saved ledger contains 45 hypothetical buys and 17 hypothetical
sales; conditional ending cash is $5,028.14–$5,838.94, with base $5,434.84. These are
planning assumptions, before fees and taxes; the no-fill baseline remains unchanged.
The bounded output audit reconstructed whole quantities, event costs, shared cash,
symbol/horizon holdings and every hourly/ending balance.

The publication retains its own saved `stock-direction-50-v2`, signal-driven holdings
without scheduled expiry sales, and `sparse-session-planning-reference-completion-v2`
with a 240-minute maximum. Its **155 explicitly synthetic zero-volume planning minutes**
are disclosed in the readable plan:

| Symbol | Observed close | Original observation, Pacific September 23 | Planning minutes carried to 17:00 |
|---|---:|---|---:|
| CROX | $124.50 | 15:16 | 104 |
| PATH | $13.03 | 16:48 | 12 |
| TWST | $158.20 | 16:21 | 39 |

These are `ASSUMED_NO_TRADES` planning anchors; original observations are preserved.
They do not enter native archives, training labels or actuals, and they do not change
the actual five-minute observation rule. [Planning/output audit](C:/dev/ducketz/artifacts/analysis/overnight-20260924/resumed-provider/output-review.md).

The [September 23 actuals review](C:/DATASTORE/ml/gameplan-actuals-review-runs/20260924T064939.839915Z/Gameplan-results.md)
preserves the original preopening forecasts, trade plan and price estimates. Of 264
forecasts, **165 are evaluated, 66 remain future and 33 are mature without usable
boundary data**. Of 154 same-clock prices, **134 are compared and 20 remain missing**.
There are 94 correct calls among 165 scored (56.97%); future, missing and neutral
outcomes are excluded. Market observations are not broker fills or realized P/L.

| Symbol | Evaluated forecasts | Mature forecasts awaiting data | Future forecasts | Missing same-clock prices |
|---|---:|---:|---:|---:|
| AAPL | 18 | 0 | 6 | 0 |
| AMZN | 18 | 0 | 6 | 0 |
| COST | 14 | 4 | 6 | 2 |
| CROX | 7 | 11 | 6 | 7 |
| GOOG | 18 | 0 | 6 | 0 |
| IONQ | 17 | 1 | 6 | 1 |
| MU | 18 | 0 | 6 | 0 |
| NVDA | 18 | 0 | 6 | 0 |
| PATH | 10 | 8 | 6 | 4 |
| SNDK | 18 | 0 | 6 | 0 |
| TWST | 9 | 9 | 6 | 6 |
| **Total** | **165** | **33** | **66** | **20** |

Cumulative evaluation covers every saved Gameplan from September 4 using each saved
universe/source: **4,776 forecasts, 3,797 evaluated, 770 mature awaiting data and 209
future**. [Native evaluation](C:/DATASTORE/ml/gameplan-evaluation-runs/20260924T063305.422285Z/summary.json).
The final verifier confirmed cumulative coverage against every immutable saved universe
and independently reproduced the actuals outcomes from the verified local archive.

## Provider and optional-source limitations

Loop A completed all five configured providers with zero capture failures: 180 changed
Parquets, 44 fundamental and 379 technical outputs, and 33 verified OPRA Historical
scopes. OPRA's exact zero-dollar acquisition estimate was 3,594,568,328 bytes, below
the native 20 GB cap. [Provider audit](C:/dev/ducketz/artifacts/analysis/overnight-20260924/resumed-provider/provider-completion.md).

All six configured direct CME scopes contain every requested symbol. Optional derived
CME context still excludes the old September 3 window because partitioned history
shadows newer aggregate captures and group-alias metadata does not provide continuous
root identity. Resumed MBP captures are bounded, unsaturated snapshots around the prior
session's 20:48 UTC boundary, not full-depth interval coverage. FMP's fresh quote passes
its isolated derivation, while retained history still rejects the same 9.471-second
quote/receipt clock skew against the five-second gate. These exclusions were not hidden
or weakened. [CME diagnosis](C:/dev/ducketz/artifacts/analysis/overnight-20260924/cme-derived-diagnosis.md)
and [current resumed provider disposition](C:/dev/ducketz/artifacts/analysis/overnight-20260924/resumed-provider/provider-completion.json).

Pricing remains quarantined on **99 exact routes**: all 51 compact source bindings and
the 17-generation authority ending August 20 are unchanged. Completeness, freshness and
20-target gates still fail; non-Pricing inputs are preserved. [Pricing audit](C:/dev/ducketz/artifacts/analysis/overnight-20260924/resumed-provider/pricing-gate-review.md).

No overnight stage or audit placed, cancelled or replaced an order, started a trader,
changed trading controls, risk limits, licenses or schedules, or relaxed model gates.
Prior immutable publications, source archives, holdings and reservations were preserved.
Full archive and estimator-inference verification is complete. The root supervisor
released its claim at **2026-09-24 07:04:53.208202 UTC** after stopping the monitoring
helper and confirming no remaining helper process or in-flight renewal.
[Final supervision release](C:/dev/ducketz/artifacts/analysis/overnight-20260924/supervision-release-final.json).
