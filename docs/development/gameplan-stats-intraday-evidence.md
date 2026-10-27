# Saved intraday prices in Gameplan Stats

The 17:30 America/Los_Angeles Gameplan Stats snapshot uses the native market
prices saved during the day by the independent 15-Minute Trader. Its collector
keeps these observations in the private intraday market-input store. Reading
those saved prices does not start the trader or create a dependency on its
current process, execution state, orders, model or manual controls.

## Observation contract

The reader accepts original FMP five-minute historical-chart responses under
the existing `observed-multi-source-stock-v2` price contract, and saved native
EQUS.MINI one-minute bars under contracts that already permit that source.
It verifies the collector receipt, original raw payload, symbol, interval and
acquisition timestamps. EQUS bars also require checksum-verified DBN segments
and native instrument definitions. The original provider identity remains
`fmp:historical-chart:5min` or `databento:EQUS.MINI:ohlcv-1m`; the independent
15-Minute Trader is the collector, not a new market-data provider.

Use original native one-minute or five-minute candles. Do not use generated fifteen-minute
candles, filled gaps, model features, quotes, fills or account values as target
prices. Do not relabel another dataset as FMP, broaden an older saved source
contract, combine two providers' endpoints, or loosen the saved target's
observation tolerance. A bad receipt is an error, not permission to trust its
projected values. Invalid, synthetic, incomplete and zero-volume candles remain
unavailable.

Receipt time determines when an observation was available. A later-received
revision cannot change an earlier snapshot. The normal selector still resolves
eligible revisions within one provider and preserves unresolved conflicts.
Other already supported price archives remain available under their original
contracts; they do not prove the intraday collector was read.

Only the Stats reconciliation path opts into these saved intraday receipts.
Shared price loaders default to their existing behavior, preserving each PC's
model-training inputs and saved recipes.

## Snapshot and display

New results retain the price source and collector evidence for each observed
boundary, with receipt/payload hashes and actual acquisition times. These fields
describe observed prices; the original saved probability, direction label,
policy metadata, target definition and maturity remain unchanged.

The Gameplan Stats tab reads the immutable published snapshot and its current
dated pointer. It displays snapshot timing, available-data coverage and recorded
price provenance. Refresh loads newly published results; it never fetches prices,
trains models or starts a trader. Missing historical provenance remains
unrecorded rather than being attributed to the intraday collector.

Daily Stats reviews outcomes through the specified 17:00 Pacific close. Missing
eligible observations remain awaiting data, and immature targets remain pending.
A completed snapshot means the review finished with available evidence, not that
every target matured or every market price was present. Reconciliation publishes
an immutable revision when the result or its recorded evidence changes.

The scheduling rules remain in [Gameplan Stats timing](gameplan-stats-timing.md):
Stats from source-session 17:30, the same-session evening minimum at 21:00,
the existing dispatcher kickoff at 21:05 and all later schedules retained.
After the normal evening acquisition, the existing post-fetch reconciliation
updates unfinished model feedback using the resulting reviewed snapshot.

Raw intraday receipts, operating outputs, private native bindings and account
state remain local. Shared source publication includes only the reviewed code,
tests and portable documentation.
