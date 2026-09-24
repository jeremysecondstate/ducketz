# Historical training integration — September 23, 2026

The user explicitly authorized using all configured symbols' retained historical
archives in the existing **Loops Overnight Gameplan** Scheduled task. This is a
prospective nightly preparation change, not a replacement for the active session's
frozen publication. The task remains ACTIVE at 21:05 America/Los_Angeles, with a
03:30 completion target and the native next-session 04:00 hard deadline.

The existing automation was updated through the app tool. Its schedule, model,
reasoning effort, project, working directories and failure-only notification policy
were verified unchanged. Exact before/after configuration copies are saved here as
`automation-before.json` and `automation-after.json`. The new operating contract
is prepended; the fresh command adds `--archive-history`. Older attempts preserve
their saved flag, source, probability target and deadline when resumed.

Implemented causal daily/hourly archive features; optional operational features
join only when available by the archived decision clock. Raw files, normalized
files, manifests, receipts and split sources are bound as evidence. All source
quality, split/discontinuity, chronological partition, exact minute-boundary and
model qualification gates remain in effect. Seconds verify exact native minute
overlap without creating duplicate training samples. New archive-feature models
cannot inherit an older-feature champion. Tail pins and publication readers check
the new saved contract; cumulative evaluation retains each historical selector.

The native history stage's opt-in prefix extension retains current cursor bytes
and uses existing native partition writers. It needs fresh exact zero-dollar cost,
provider range, finite record count and bounded capacity preflights. The read-only
preview identified AAPL/AMZN/COST/GOOG/MU/NVDA prefixes 2019-08-19 to 2025-01-13.
The other five current symbols already cover their observed feature histories.
No provider request or download was made during this implementation; the normal
scheduled stage performs those checks and acquisitions. Successful verified
prefixes are reused on future runs.

Verification:

- 396 focused tests passed (28 existing library warnings); see `test-results.txt`.
- All 195 inventory partitions passed raw/normalized hash and native metadata
  checks; full raw-record normalization replay was not performed.
- Actual read-only feature materialization: 17,069 causal rows, all eleven stocks,
  with earliest eligible rows reaching 2018/2019 where available.
- Actual read-only four-group construction: 86,263 hourly, 20,879 four-hour,
  11,989 daily and 2,380 weekly examples before the six pending prefixes; all
  groups have both classes and all eleven symbols. The current input grid has
  264 rows. These are eligible cohorts, not new fitted or qualified models.
- Seconds verification: 2,842,334 exact minute OHLCV matches and zero additional
  minute buckets; undefined native observations are excluded explicitly.

No pipeline, fitting, publication or trader was started during setup. No order,
trading control, active forecast, risk limit or production cursor was changed.
Tonight's actual provider availability, runtime and out-of-sample model scores
remain to be observed. More historical examples do not guarantee a quality pass.

Final provenance review bound current archive files into the separate cumulative evaluation manifest. Unknown optional-feature availability is excluded. Post-review focused verification: 39 tests passed. All historical/current group causal clocks passed the real-data read-only smoke. Supervision was released; see supervision-release.json.
