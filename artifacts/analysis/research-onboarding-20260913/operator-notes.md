# September 13 research onboarding

The user selected CROX, PATH, TWST, and IONQ from the immutable September 13
Sunday research report for history acquisition, training, predictions, Gameplan
integration, and a reusable future onboarding workflow. A company watch memo
does not become a qualified investment recommendation through this selection.

The approved batch is `df613ade510b2775a99c051abf8472367e2c70208e13c1eacce8bad4c577d487`.
Its prior production universe is AAPL, AMZN, GOOG, MU, NVDA, SNDK, COST. Its
candidate universe appends CROX, PATH, TWST, IONQ. The batch has 100 Databento
schema requests, 230,492,746,800 uncompressed billing bytes, a $0 metadata quote,
and a 466,354,202,720-byte capacity requirement. Shared CME and macro archives
are reused. No trading controls, risk limits, order authority or paused options
training/Strategy tasks are changed.

## Inception correction

FMP's IONQ profile returns the SPAC predecessor date, January 4, 2021.
Databento XNAS.ITCH symbology verified the current IONQ ticker begins October 1,
2021. The original checksummed plan and already submitted jobs were retained;
requesting IONQ before its actual ticker inception returns no IONQ records.
The persistent IONQ price policy narrows Schwab and FMP prices to October 1.
The FMP research daily archive was re-fetched with that boundary and contains
1,241 rows. Future batch plans derive the later effective ticker inception
before creating any acquisition request. Corporate SEC filings may predate
listing, but no selected filing date precedes 2018.

## Completed implementation checks

- The combined onboarding, UI onboarding, Schwab features, FMP corporate,
  dynamic-universe and independent enrichment suite passed 108 tests.
- After adding stock batch preparation and source binding, the focused onboarding
  suite passed 17 tests, including rejection of a newly billable request before submission.
- The evaluation, actuals review, trade planning, source selection, stock-only
  Gameplan and independent-target compatibility suite passed 164 tests.
- Activation tests cover all-four atomic membership, idempotence, preservation
  of watchlist comments, incomplete histories, concurrent membership changes,
  advanced publication pointers and wrong/incomplete overnight evidence.
- The new overnight and trade-plan verifiers also accepted the existing
  independently published seven-symbol completed run and its 168 trade rows.
- Seven Scheduled prompts were updated through the app. Before/after snapshots
  verify every recurrence, state, model, effort, target and notification field
  was preserved. Historical paused follow-ups remain unchanged.

## Acquisition progress

The complete SEC archive contains 3,536 filings: 663 CROX, 665 PATH, 1,587 TWST
and 621 IONQ. Total compressed submission bytes are 619,127,585 (consult the
per-symbol corporate-history.json receipts for exact totals); uncompressed
content is about 1.976 GiB. The archive and FMP daily prices are checksummed.

The main fetch worker owns the renewable supervision lease and canonical
history writes. OPRA jobs prepare concurrently at Databento. XNAS preparation
only submits native checksum-bound batch jobs under this same owner; it does
not write canonical stock partitions or advance history cursors. The main
fetcher subsequently adopts those jobs and verifies every downloaded file.

Consult progress.json, history-receipt.json, overnight-run.json, validation.json
and activation.json for phase completion. A submitted download job or complete
SEC corpus alone does not mean the symbols have been activated.

## September 14 status-record repair

The initial fetch stopped at CROX OPRA Status on July 25, 2024. Its 23,754 native
records include one repeated, identical message (two matching rows). Status has
no provider sequence field: https://databento.com/docs/schemas-and-data-formats/status.
The collector now preserves native record order and multiplicity using the same
validated source ordinal already used for TCBBO. It does not drop either event.
Legacy published Status partitions retain their existing identity checks.

All 45 OPRA tests passed. A separate copy of the retained real partition
preserved every original column and all 23,754 rows; the native file SHA stayed
unchanged, the new identity had zero duplicates, and an existing July 17 legacy
partition still verified. See status-repair-verification.json. Acquisition
resumed with the same plan and native provider jobs in fetch-resume-1.log.

The resumed acquisition reached CROX CBBO-1S through November 26, 2025, then
stopped on November 27 because the count endpoint returned
`422 symbology_invalid_request` instead of zero. A direct provider probe
confirmed that the actual download and symbology endpoints also had no
resolvable parent for that exact interval, despite dataset-wide availability.
See empty-options-day-evidence.json (the enclosing approved scope still quotes $0).

The dense-day planner now forwards this specific count-endpoint resolution
failure to the native download path for confirmation. It does not infer zero
rows from a failed count, suppress access failures, or change the exact-row cap.
All 47 OPRA tests pass. The real native collector also reported one NO_DATA
interval, zero published rows, and zero errors; see
empty-options-day-verification.json. The same batch resumed via `run` in
run-resume-2.log, which continues automatically into training, validation, and
activation after every history request completes.

The related Databento cold-start and OPRA replay/publication suites passed
another 92 tests after both native collector repairs.

## Prepared-download cache warming

While the canonical worker processes dense CBBO-1S days, the bounded
prefetch-opra-batches.py helper downloads already-prepared, freshly zero-quoted
OPRA jobs into a separate DATASTORE mirror. It uses the native archive inventory,
path checks and source hashes, then atomically creates native cache entries
without replacing existing files. It does not write canonical partitions,
provider job state, cursors, publication pointers or membership. The sole
canonical worker rechecks every file before consuming it. Both processes stop
dependent work if the original supervision owner is lost. See prefetch.log,
prefetch-progress.json and the final prefetch-receipt.json for results.

Prefetch completed successfully: 31 jobs, 30,045 newly cached files, about
3.152 GiB of verified native sources. This is cached input awaiting the main
worker's canonical validation/publication, not an activation receipt.

## Dense-archive batch delivery

At 05:53 UTC the operator restarted only the identity-verified onboarding
worker (PID 64428, exact batch command/plan), preserving all native checkpoints
and the supervision token. The same `run` resumed in run-resume-3.log. This
applies daily-split batch delivery to high-volume quote archives and avoids
repeated per-day transfer overhead. The metadata planner still routes days
above its unchanged 20-million-record target into the existing deterministic
intraday requests, with the unchanged 25-million exact-validation maximum.
Existing split segments are reused on resume; a failed split cannot complete
the batch or discard its staged source. The selected schemas, dates, budget,
account quote requirement and candidate universe are unchanged.

The combined OPRA, cold-start, research onboarding and single-symbol onboarding
suite passed 117 tests. After adding the empty-existing-segment guard, all
52 OPRA tests passed again. Controlled provider fixtures cover full native batch publication and
resume for OHLCV, CBBO-1S and CMBP-1, dense split routing, failed split source
retention and resuming previously published intraday segments.

The real CROX CBBO-1S remainder then completed successfully through the new
native batch path: 131 verified daily partitions published and five provider
no-data dates retained. Its 646,432,504 compressed source bytes came from the
saved zero-cost job OPRA-20260914-DLK5RBNH9M. Previously completed daily partitions
were reused. The worker continued into trades and the prepared XNAS archives.
See run-resume-3.log for the canonical completion evidence.

TWST's completed zero-cost CBBO-1S job was also cached without canonical writes:
249 checksum-verified files, 336,554,801 compressed source bytes. The scoped
prefetch helper now accepts schema/symbol filters and a separate receipt label;
quote-prefetch-receipt.json preserves this receipt without replacing the earlier
31-job prefetch evidence.

The scoped quote prefetch subsequently completed PATH and IONQ as well. All
three complete 249-file quote jobs (TWST, PATH, IONQ) now have verified native
cache receipts in quote-prefetch-receipt.json. The canonical worker consumed
PATH and TWST successfully and will independently recheck IONQ's sources.
All four provider quote exports reached DONE with cost_usd=0; the final large
IONQ export contains 1,442,726,441 provider records. Provider totals include
files for dates classified unavailable; the native canonical publication keeps
its existing availability rules and records those omitted source files.

CROX, PATH and TWST completed all 25 selected schema requests with zero hard
provider failures. Their canonical CBBO-1S archives contain 284,276,022,
500,991,862 and 32,738,591 records respectively, through September 11. These
are data-acquisition completions only: the production watchlist remains at
seven until the batch's native training, publication and activation succeed.

A final read-only production/UI audit is prepared as verify-activation.py.
Run it only after activation.json reports ACTIVE. It verifies membership,
current pointer hashes, all schema/provider completions, 3N native OPRA cursors,
the Gameplan UI reader and its pending-symbol list, and measures the final
symbol-path logical footprint in one DATASTORE traversal.

All four symbols completed their 25 selected schema requests at
2026-09-14T09:19:26Z, with zero hard provider failures. IONQ's canonical CBBO-1S
archive contains 1,437,176,623 rows across 249 daily partitions. All corporate,
secondary-price and operational-history prerequisite receipts subsequently
completed; no secondary series were excluded.

The native candidate pipeline started at 2026-09-14T09:24:23Z and is bound to
`C:/DATASTORE/ml/overnight-runs/20260914T092423.869840Z`. Its original publication
deadline is 2026-09-14T11:00:00Z (04:00 Pacific). The batch worker is supervising
this exact attempt and will validate and activate only after it completes.

During normal Loop A upkeep, Schwab returned a conflicting options snapshot at
an already stored September 11 timestamp for each of the eleven symbols. The
existing collector rejected those divergent duplicates as non-blocking local
advisories and retained its saved snapshot evidence. It did not overwrite or
relax snapshot validation. Stock-only preparation continued into technical
feature generation. The exact advisories are in the native Loop A log.
