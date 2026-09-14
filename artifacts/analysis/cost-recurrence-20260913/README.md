# COST recurring price gaps — September 12 investigation

COST uses the same stock universe, source loader, target windows, minute-completion convention, and five-minute observation rule as the other six symbols. The recent missing actuals reflect the density of observations supplied by the selected XNAS.ITCH venue feed. A subsequent training audit also found an upstream history-selection problem, detailed below; the two issues are distinct.

## What the native evidence shows

All 28 recent native DBN files reproduce their normalized Parquets exactly. Their symbol mappings and requested ranges are complete; no local normalization loss was found. These checks establish what the provider returned, not that no trading occurred anywhere during a missing interval.

On September 11, COST had 386 of 390 regular-session minute bars, 31 of 150 premarket minutes, and only **20 of 240 after-hours minutes**. The other six stocks had 109–218 after-hours minute bars.

| Completed session | COST missing hourly price points | Each other symbol |
| --- | ---: | ---: |
| September 8 | 1 of 14 | 0 of 14 |
| September 9 | 3 of 14 | 0 of 14 |
| September 10 | 2 of 14 | 0 of 14 |
| September 11 | 3 of 14 | 0 of 14 |

For September 11's missing 14:00, 15:00 and 16:00 opening observations, the next COST bars begin at 14:08, 15:14 and 16:59. Those are 8, 14 and 59 minutes away. Seven forecast outcomes lack at least one valid endpoint. Only one involves the September 10 closing reference previously completed synthetically for planning; the remaining six involve different observations on September 11. The prior planning repair never authorized synthetic actuals or training labels.

- [Raw archive comparison and exact endpoint evidence](C:/dev/ducketz/artifacts/analysis/cost-recurrence-20260913/raw-coverage-findings.md)
- [Per-symbol/session/hour data](C:/dev/ducketz/artifacts/analysis/cost-recurrence-20260913/raw-coverage-boundaries.csv)
- [Full native coverage evidence](C:/dev/ducketz/artifacts/analysis/cost-recurrence-20260913/raw-coverage.json)

## Implemented reporting repair

Future actuals reviews now explain each missing endpoint, preserve the closest observation on the required side of the clock and its actual time/distance, and distinguish verified complete request coverage from incomplete or unknown coverage. Main tables remain concise; expandable details expose the evidence. This prevents an observed-data gap from being presented as an unexplained download still in progress.

Price selection, five-minute rules, direction scores and outcome statuses are unchanged. Existing immutable Gameplans and results were not republished. An offline comparison verified exact equality of every original column across all 168 forecast rows and 98 price rows; all five production pointers were unchanged. The seven historical outcomes remain unscored.

- [Readable diagnostic preview](C:/dev/ducketz/artifacts/analysis/cost-recurrence-20260913/Gameplan-results-diagnostics.md)
- [Exact original-column verification](C:/dev/ducketz/artifacts/analysis/cost-recurrence-20260913/diagnostic-verification.json)
- [Reproducible verification script](C:/dev/ducketz/artifacts/analysis/cost-recurrence-20260913/verify_actuals_diagnostics.py)

Validation: the final combined actuals/source/target/statistics-reader suite passed **96 tests in 19.63 seconds**. Independent code review found no blocking defect. The operating procedure documents the new diagnostic distinction. All four optional learned sizing models completed training but did not pass validation. They do not determine quantities in the current Gameplan or scheduled fixed-budget worker and do not explain COST's missing actuals.

## Candidate source improvement

A separate, zero-dollar XNAS.BASIC test supplied all specifically probed missing COST clocks within 0–1 minute. This feed includes additional trade venues; it is not the same dataset as XNAS.ITCH. Its 192 native bars, exact request metadata and hashes are preserved separately. No candidate prices were substituted into saved forecasts or actuals.

The expanded comparison covers **120 sessions, March 23–September 11**, all seven symbols, and the March 20 prior-session close. Seven exact preflights quoted $0 before acquisition, with total estimated billable size 43,772,232 bytes. All native files reproduce their normalized data exactly; a final independent readback verified all 49 manifest-bound files and their sizes/hashes.

Using only positive-volume native bars, COST's missing boundary observations fell from **547/3,120 to 48/3,120**, a 91.2% reduction. Available mature entry windows rose from **1,569/2,275 to 2,199/2,275**. The other six symbols have no missing candidate boundaries in this comparison. COST therefore has materially worse coverage on the current feed, although the broader historical audit also finds a few current-feed gaps for AAPL, GOOG and NVDA.

Provider metadata marks August 31 XNAS.BASIC degraded. A conservative comparison excludes that date and every entry window touching it, as well as all zero-volume records, from both source comparisons: COST missing boundaries still fall from **538/3,094 to 47/3,094**, and available mature entry windows rise from **1,558/2,251 to 2,177/2,251**. This is strong coverage improvement, not complete coverage or model qualification. Native zero-volume semantics remain unresolved; the positive-volume calculation does not rely on them.

- [120-session coverage results, quality limits and all routes](C:/dev/ducketz/artifacts/analysis/cost-recurrence-20260913/source-probe/validation-120/coverage-review.md)
- [Final candidate file verification](C:/dev/ducketz/artifacts/analysis/cost-recurrence-20260913/source-validation-readback.json)
- [Source options and official documentation](C:/dev/ducketz/artifacts/analysis/cost-recurrence-20260913/source-options.md)
- [Concrete prospective integration scope](C:/dev/ducketz/artifacts/analysis/cost-recurrence-20260913/integration-scope.md)

The user's source-policy choice remains pending: prospective XNAS.BASIC migration after validation, or retention of the existing explicit XNAS.ITCH policy. No source migration has been implemented or activated. Adoption needs an explicit source/observation-policy identity, quality admission rules, native integration tests, and same-source history/model qualification. Existing XNAS.ITCH forecasts retain their original source and actuals. No live trader, trading control, order, model status, publication pointer or scheduled task was changed during this investigation.

## Follow-up: training history and plain-language model status

The user's questions prompted a separate audit of actual training inputs. All 168 published predictions are populated, including all 24 COST predictions. DATASTORE contains older hourly/daily histories reaching 2019–2023, while the selected one-minute target-price archive begins January 2025 (February 2025 for SNDK). There is no 120-session training cap; that number described the separate source comparison. Final pooled train-plus-selection fits contain 19,240 / 5,251 / 5,013 / 988 examples for 1h / 4h / 1d / 1w; COST contributes 1,340 / 371 / 289 / 57.

The legacy upstream eligibility filter admits historical features only when a rolling hourly sample already targets next-session 04:00 Pacific. COST had no eligible source days in January–March 2026 and one in April, despite existing regular-session data. This occurs before the independent minute endpoint checks and reduces training coverage. The user subsequently authorized the selection repair; its completed implementation and verification are recorded below. More old training data alone cannot supply a missing observed September 11 price.

The stock forecast UI and future report captions now use explicit validation language instead of "research-only". The optional learned sizing assessment identifies that its models are unused by current Gameplan quantities. Training completion, validation results, numeric calculations, persisted status IDs and trading decisions are unchanged. The relevant UI/report suite passed 49 tests; the final report-only check passed 10 tests (a subset of those 49). Independent review found no blockers. Re-rendering the saved current report offline changed only its sizing heading and explanatory caption; every data table remained identical. All five production pointers remain unchanged.

- [Exact training dates, counts and source-selection evidence](C:/dev/ducketz/artifacts/analysis/cost-recurrence-20260913/training-history-audit.md)
- [Optional sizing model assessment and actual selected policies](C:/dev/ducketz/artifacts/analysis/cost-recurrence-20260913/sizing-assessment-audit.md)
- [Current Gameplan with revised wording, offline preview](C:/dev/ducketz/artifacts/analysis/cost-recurrence-20260913/Gameplan-wording-preview.md)

## Authorized history-selection repair completed

Future independent Gameplans now use `independent-gameplan-prior-session-features-v1` in `ml/gameplan_source_selection.py`. It selects the latest eligible full hourly feature bar from exactly the preceding exchange session. The bar must start at/after regular open and end between the actual regular close and 17:00 Pacific. Real information/decision times must follow bar completion and be no later than both the run's as-of time and source-session 17:05. It rejects conflicting copies, invalid clocks, unapproved calendars and stale cross-session inputs. Weekends, holidays, early closes and daylight-saving transitions are covered.

The source-selection contract and original clocks are bound into model payloads/reports, Gameplan configuration, forecasts and cohorts. Retained champions must match the feature selection contract. Old publications retain their original selector during evaluation. OPRA freshness still covers the complete source session even if the chosen feature bar ends earlier. New metadata is verified before any receipt or current pointer is written, and by both publication readers.

Pinned replay verified 308 original price files and the exact sample file. The repair restores all **61 COST January–March 2026 source sessions**, yielding **550 hourly, 141 four-hour, 12 daily and 5 weekly aligned target rows** with the same five-minute rules. Across the full available period, COST aligned rows improve from 2,196/614/460/92 to 4,169/1,142/690/138 for 1h/4h/1d/1w. These are eligible training examples, not a claim that all enter the final fit or that predictive accuracy has improved. Monday's seven current feature rows/clocks are identical; legacy cohort reconstruction and all original outputs/pointers remain unchanged.

Validation: **270 regression tests passed**, plus **8 publication tests passed** after the final pre-publication validation fix (seven overlap the larger suite, one is new). Independent review found and resolved that publication-order issue; no blockers remain. No model retraining or production backfill was performed, and the completed Monday preparation was not rerun. The next new independent preparation uses the repaired selection.

- [Pinned repair validation and independent review](C:/dev/ducketz/artifacts/analysis/cost-recurrence-20260913/source-selection-repair-validation.md)
- [Reproducible validation](C:/dev/ducketz/artifacts/analysis/cost-recurrence-20260913/verify_source_selection_repair.py)

## Account access and 2024 extension

The user-authorized logged-in portal check on September 12 Pacific confirmed **Databento US Equities Standard**, with **EQUS.MINI and EQUS.SUMMARY active live**. XNAS.BASIC and XNAS.ITCH live access require Plus/Unlimited. Standard's historical table includes eight-plus years of L0 OHLCV across 45 venues; reverified successful $0 XNAS.BASIC historical downloads prove this account's actual access for the tested intervals. Historical access and live licensing must not be conflated. The saved plan inventory now states the distinction. No plan/license change was made.

Extending the current XNAS.ITCH minute history into 2024 is feasible for the six older symbols. All six already have feature rows on 252 sessions in 2024. Exact native preflights for December 29, 2023 through January 13, 2025 quote **$0**, **1,128,482 records** and **63,194,992 estimated billable bytes**, with capacity passed. SNDK is unmapped before February 24, 2025. No 2024 data was downloaded; this is a concrete, costed extension to compare against the shorter training history using the same recent holdout and unchanged causal/quality rules. A longer history is not yet demonstrated to improve accuracy.

- [2024 feasibility and account-access receipts](C:/dev/ducketz/artifacts/analysis/cost-recurrence-20260913/history-2024-feasibility.md)
- [Corrected Standard plan inventory](C:/dev/ducketz/docs/databento-plan/databento_standard_plan_data_access.md)
