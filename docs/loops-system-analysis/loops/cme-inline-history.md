# CME inline event-history ingestion

Inline CME acquisition must advance the durable event history before the
cross-asset context materializer reads it. Previously, fresh inline captures
could update hot files while old partitioned events remained available; the
materializer preferred those partitions and could therefore use stale input.

`datafetching/databento_fetch.py` now passes uncapped original provider rows
and raw frames through `persist_cme_event_history` under the existing CME
writer lock. This reuses the canonical writer's instrument identity,
event/receive/fetched timestamps, provenance and deduplication. It avoids
merging hot-file aggregate group metadata into per-instrument event history.
Hot and raw captures remain available as local evidence.

Limit-saturated captures stay outside canonical event history and produce a
request-specific advisory. A current capped recapture blocks context
materialization even if earlier complete partitions already exist. Context
OHLCV/BBO/MBP acquisition or persistence failure also blocks materialization
for that invocation. Contracts-only failures retain diagnostic evidence and
allow otherwise valid context inputs to proceed.

This change preserves the existing partition writer and materializer quality
gates. It does not make stale or capped data eligible, repair provider quality
conditions, replay a completed run or change trading controls. Operational
evidence and account snapshots stay on the originating PC.

Regression coverage in `tests/test_cme_cross_asset_context.py`,
`tests/test_cme_runtime.py` and `tests/test_databento_cme_context.py` checks
stale-partition/fresh-inline behavior, provider identities and timestamps,
idempotent recapture, cap safety, context failure paths and contracts-only
isolation. Atlas's September 30 publication verification included these
suites in the 562 passing focused tests. Source publication alone performs no
production materialization, market-data download or broker operation.
