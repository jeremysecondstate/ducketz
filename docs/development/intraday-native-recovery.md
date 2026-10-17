# Receipt-bound native historical recovery

`intraday-covered-native-recovery-v2` is an explicit local recipe choice layered
on the existing covered EQUS.MINI interval contract. Installing shared code does
not activate it. The recipe supplies the recovery contract and local PC to the
history loader; the dataset builder rejects recovered candles without that opt-in.

The local selection file is `intraday/<pc>/minute-recovery/installed.json` under
the configured datastore. It binds PC, symbols, and exact hashes of immutable
receipts, original DBN objects, and normalized projections. The loader verifies
the DBN dataset, schema, requested symbols and time bounds, then compares every
normalized OHLCV value with the native float-price decode. An empty source
response supplies no candle or no-trade assertion.

Canonical historical files remain intact. A nonempty recovered quarter can be
added; an existing quarter can be replaced in the selected working snapshot only
when the new complete source snapshot contains all original minutes with exactly
equal prices and volume, plus genuinely additional minutes. Conflicting or omitted
original minutes retain the original working snapshot and produce explicit
conflict evidence. Original immutable response objects always remain available.

Recovered candles use `availability_kind=actual_historical_recovery`. Their
`available_at` is the actual response receipt, while `feature_cutoff_at` is the
historical quarter close under an explicit reconstruction assumption. Historical
indicator computation and actual information availability are separate clocks.
The dataset carries the receipt dependency through the feature warmup window and
the target. A fit before receipt cannot consume the recovered observation. Context
must still have been genuinely available by the historical feature cutoff; the
new receipt cannot backdate context or establish a historical live replay.

The optional `actual_receipts` selection explicitly pins original live
`minute-fetches` response and persistence receipts, bounded to 4,096 entries per
selection. Each selected symbol's native bytes and parsed bars are verified.
The existing exact-quarter resolver enforces consecutive stable sparse responses
and the minimum retry wait. Its actual candle clocks override historical close
assumptions where genuine contemporaneous evidence exists. Unstable, empty,
invalid and non-quarter requests remain explicitly accounted for. This bridge
does not scan the forecast database or interpret file modification times as
receipt clocks. Normal collection and its durable candle journal retain future
live observations independently.

The opted-in native loader also resumes the normal acquisition journal through
the shared saved-observation projection. Read-only rowid queries are bounded;
an owner-local indexed checkpoint under `intraday/<pc>/native-actual/` preserves
progress across symbols and restarts. Original actual clocks therefore reach
normal manual startup as well as non-trading preparation. Repeated identical
snapshots retain their earliest adequate receipt; differing newer revisions and
source exclusions remain explicit. This projection is independent of scratch
reports and never queries the forecast database or initiates a provider request.

Different feeds remain different sources. An entitled FMP or Schwab observation
does not prove that an omitted EQUS.Mini minute existed. A separately retained
alternative observation must name its own provider, schema, native interval,
constituents and receipt clocks. It cannot fill an EQUS-only reference or label.
A separately assessed source population must bind both reference and target to
its selected source contract, preserve qualification gates, and preserve spent
holdouts. Recovery never synthesizes class support.

Private manifests, native records, fitted artifacts and detailed observations
remain on their owner PC. Publication contains the correctness code, contract
documentation and offline tests. Source installation, local recipe selection,
continuous non-trading preparation and manually starting a trader remain separate
facts.

Native history merges retain the earliest actual receipt only within the latest
contiguous revision of the complete native-minute observations. Equal aggregate
OHLCV alone is insufficient. A reverted A/B/A sequence uses the new A receipt;
saved projections retain the intervening attempt identity and actual clock as
an `observed_native_revision_v1` boundary. Restoring saved candles cannot cross
that boundary. Legacy candle selection and immutable source records are unchanged.
