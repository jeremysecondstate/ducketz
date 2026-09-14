# Historical source-selection repair validation

Read-only replay of the exact inputs and as-of time of the published September 14 Gameplan. No provider request, model fit, publication or pointer change.

The new selector uses each immediately prior exchange session's latest eligible completed hourly feature bar. Its end must reach that session's actual regular close; features must be known by the earlier of the run as-of time or source-date 17:05 Pacific. It does not inspect the rolling model's next-target clock or label.

## COST January–March 2026

All 61 action sessions now have eligible prior-session features (previously zero). Observed labels still need genuine minute endpoints within five minutes; source eligibility alone does not manufacture an outcome.

| Horizon | Prior aligned rows | Repaired aligned rows | Action sessions with aligned rows |
|---|---:|---:|---:|
| 1h | 0 | 550 | 61 |
| 4h | 0 | 141 | 61 |
| 1d | 0 | 12 | 7 |
| 1w | 0 | 5 | 5 |

## Verification

- Verified all 308 pinned XNAS.ITCH price files and the exact source sample checksum.
- Legacy reconstruction exactly reproduces every saved cohort's source clocks, targets, returns and endpoint prices.
- The new selector leaves all seven Monday source feature values and causal clocks exactly unchanged.
- All original published output checksums and all five current pointers remained unchanged.
- Every repaired aligned label still passes the unchanged five-minute endpoint limits.
- Chronological partitioning, model fitting and performance validation were not run by this audit; these are candidate aligned rows, not a claim that every row enters a fitted model.

Detailed machine-readable counts: `source-selection-repair-validation.json`; monthly counts: `source-selection-monthly-counts.csv`.

## Independent integration review

Reviewed the prospective publisher, target builder, cumulative evaluator, champion retention and related regression tests. The source selection version is carried through forecast rows, training cohorts, model payloads, model reports and publication configuration. Training and current rows must agree. A model fitted under the old selector cannot be retained as a champion for the new selector. Old publications continue to reconstruct their old feature selection, and old/new observations remain separate in cumulative evaluation.

The OPRA completed-through requirement derives from the source session's 17:05 Pacific feature cutoff, so accepting a regular-close feature bar does not relax the required cursor date. Target-price source identity and the five-minute endpoint rule remain unchanged.

The review identified and confirmed correction of one publication-order issue: source metadata validation now runs before any receipt or latest-pointer write. The regression test proves invalid source metadata leaves the previous pointer byte-for-byte unchanged and writes no receipt. All eight publication tests passed after the correction. No additional integration blockers were found. This review did not modify production data or the JSON replay proof.
