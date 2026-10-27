# Bounded market-collection expiry maintenance

`QuarterHourMarketCollector.expire_tasks(keys, maximum=32, reviewed_tasks=None)` prepares and commits
the collector's existing expired outcome for an explicitly selected, bounded
set of tasks. Normal `collect`, `step`, and `run` do not call this method.
The ordinary request ordering, provider limits, retry policy, collection scope,
and 48-hour recovery horizon remain unchanged.

A task is eligible only when it is unsettled, currently due, and its original
quarter end is strictly more than 48 hours before that task's fresh start clock.
Exactly 48 hours remains eligible for the ordinary collection path. The helper
checks the native task key, request identity, quarter boundaries, and original
task contents before preparing evidence. It reports locked, ineligible, or
concurrently changed targets separately from committed tasks.

Use the existing local frozen collection configuration and the native
collector. Supplying `adapters={}` when constructing a maintenance collector
keeps that process without provider adapters. The expiry method itself has no
adapter execution path. It rejects collector subclasses and instance overrides
of the original collection or queue-reading methods.

The caller selects reviewed keys from this PC's own queue. Keys must be a list
or tuple of unique native identifiers and must fit the explicit bound, which
is limited to 128. Discovery and automatic scheduling remain separate from the
helper. Existing operator authority, local source review, and cooperative
writer ownership still apply to an actual maintenance invocation.

For each target, the helper acquires its existing task lock without waiting.
It acquires task locks before the global queue lock, holds them across receipt
preparation and the final queue publication, and releases the global queue
lock during receipt and index I/O. Immutable orphan receipts retain their
original reverse-filename inspection order, checksum validation, native index
validation, and matching-source precedence. A matching settled receipt is
recovered before a new expiry receipt is considered.

Fresh expiry uses the original empty `EXPIRED` receipt, reason, clocks,
configuration identity, canonical digest, fsync, and native index. Its queue
transition increments attempts, appends the receipt path, and applies the
existing retry formula. Orphan recovery preserves the original due time and
increments attempts only when its receipt path was not already recorded.
Source blocks retain their original semantics.

The final publication rereads the queue and compares each target's strict JSON
bytes with its reviewed original before merging that task's fields. Unrelated
updates remain in the fresh queue. A changed target keeps its prepared receipt
as recoverable orphan evidence. Successfully prepared and unchanged targets
can commit before an ordinary preparation exception is re-raised; the original
exception remains available to the caller. `KeyboardInterrupt` and
`SystemExit` immediately unwind and release locks. Receipts already persisted
before an interruption remain available for native recovery.

A completed maintenance report establishes queue transitions and immutable
receipt persistence. It does not establish provider coverage, genuine market
observations, model qualification, trader loading, order recovery, or full-cycle
latency recovery. Retain original source data, historical receipts, fitted
models, and local recipes. Existing terminal-index compaction continues under
its original seven-day rule; expiry is not an early evidence purge.

Callers can supply `reviewed_tasks`, a complete key-to-SHA-256 map of strict JSON
task bytes, after durably saving the original task records. The map is validated
and frozen before I/O. A changed initial task is fenced before receipt or index
work. Strict JSON rejects nonfinite values. If the final fresh queue contains
a nonfinite value anywhere, maintenance aborts publication and leaves all
prepared receipts as recoverable orphans; it does not normalize that invalid
queue or commit other successes through the native writer.
