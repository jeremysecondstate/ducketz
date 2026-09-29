# Current FMP advisory review

Reviewed at 2026-09-29T04:29:22.783224+00:00. Status: KNOWN_RETAINED_CLOCK_SKEW_ADVISORY.

The optional shared energy derivation rejects preserved September 2 rows; current-cycle quote evidence passes the unchanged five-second gate. No newly actionable defect or provider retry is indicated.

The saved source has 1497 quotes, including 1 fetched in the current Loop A cycle. Native pure calculation passes on that current subset; full-history calculation still reports: FmpEnergyContextQualityError: FMP provider quote timestamp exceeds local receipt by 9.471s (maximum allowed clock skew is 5.000s). All five invalid historical rows remain before the current cycle; their complete selected timestamp/identity evidence and source hashes are in fmp-advisory-review.json. The first rejected row and native error match the September 16 audit. The diagnostic retains its September 2 receipt time because native upsert deduplicates the identical message, which was reproduced in memory; current-cycle log and fresh source evidence establish this recurrence. No source rows, calculation gates or provider state were changed.

This audit does not claim the optional full-history energy derivation completed. It confirms the current nonblocking advisory is the retained quality exclusion, with no new actionable defect. All broker/provider calls and production writes in this audit were zero.
