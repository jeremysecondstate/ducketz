# Bounded historical CME context

`ml.intraday.historical_market_inputs` reconstructs selected market-time context
from existing saved GLBX.MDP3 MBP-10 records. It performs no network requests,
starts no acquisition owner, and never expands an archive automatically.

The distinct contract is `intraday-cme-reconstructed-market-time-v1`. Its purpose
is retrospective training and validation. A saved quote can be a useful
historical input even when it was acquired later, but that does not establish
that a previous forecast had received it. Original exchange/provider clocks,
saved archive publication metadata, and the actual reconstruction time remain
separate. This contract cannot be used to claim contemporaneous evaluation.

## Selection and timing

An explicit `materialize_cme_history(datastore_root, start=..., end=..., roots=...)`
call reads existing continuous-root MBP-10 archive manifests and saved 5-minute
CME context snapshots. It checks saved checksums, source paths, dataset/schema,
native instrument and publisher identity, record type, clocks, and explicit
synthetic/source-empty markers before projecting data. It excludes gap-flagged
records and preserves each selected native book and its source-file hash and
row index. Raw-contract archive duplicates are not selected as continuous roots.

Each input is assigned to the first quarter boundary at or after its original
provider receive clock. The exchange event must be no later than that clock.
For each root and quarter, the latest clock and sequence select a native book;
conflicting equal-ranked books and ambiguous same-clock native contracts are
unavailable. Exact duplicates are deduplicated. A snapshot's scheduling or fetch
time never substitutes for its original quote clock.

The training join uses the reference candle's **close**, `reference_open + 15
minutes`, with `event <= provider_receive <= reference_close <= training_cutoff`.
Only an exact quarter match is used; no row is carried across decision slots.
The maximum quote age is 900 seconds, explicitly narrower than the live CME
receipt TTL of 1,800 seconds. The reconstruction contract never borrows an older
quarter merely because a live receipt would remain fresh. This is a
last-observed book feature, not a claim that every event or
all ten book levels were observed throughout the interval.

The two reconstructed fields are the existing root-specific `spread_relative`
and `depth_imbalance`, with availability indicators. The first uses the valid
top level; the second uses the sum of valid native bid/ask sizes across the
available ten levels. Missing levels, invalid books, absent roots, volume,
trade count, settlement, and open interest are never filled with observations
that were not saved.

## Consumer API

`HistoricalMarketInputs(datastore_root, artifact_path)` reads only the explicit
content-addressed artifact and rechecks row hashes, frozen native features, and
clock relations. It never discovers a newest artifact or materializes on read.

`join(decision_times, cutoff=..., purpose="retrospective_training")` returns
`feature_names`, aligned `rows`, per-row `provenance`, `version`, and `artifact`.
`retrospective_validation` is also accepted. Inference/live/contemporaneous
evaluation purposes are rejected. Its provenance uses `market_observed_at`,
never a fabricated local `received_at`.

The caller may overlay these fields into unavailable historical receipt fields
only. It must leave contemporary receipts and the inference row unchanged,
freeze the historical contract and artifact hash into training provenance, and
label affected assessments retrospective. The existing feature-support
transform learns support only from its base training partition, including when
historical context is supplied to calibration or held-out validation rows.

An explicit local policy may bind the artifact path and version. This is a
separate opt-in from the receipt market-input contract; existing frozen
forecasts retain their original configuration and source evidence.

## Resource bounds and reproduction

Default limits are 180 calendar days, 1,500 input files, 32 million input rows,
1.25 GiB of existing input files, and 32 MiB of compact output. Discovery reads
manifests and Parquet metadata before any event scan. Materialization hashes
selected files and streams projected columns in batches of 65,536 rows with
one reader; it does not copy the full raw archive. Explicitly narrower dates,
roots, or limits should be chosen for the useful model lookback. The output is
created exclusively as `<sha256>.json` under
`<DATASTORE>/intraday/market-inputs/historical-cme/`. Existing artifacts and
operational source files are preserved.

An offline test command is:

```powershell
python -B -m pytest -q tests/test_intraday_historical_market_inputs.py
```

Materializing an artifact establishes availability for newly prepared
retrospective models only. Record actual joined rows both before and after the
model's chronological training cap; file or artifact existence alone does not
establish model use. Live inference still requires its own genuine received
market context, and the trader still requires its normal manual start.
