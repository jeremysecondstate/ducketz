# Independent market-data and model runtimes

The recommended deployment is five independent processes. CME/L2, Schwab
options, and options-strategy work are deliberately outside the critical paths
of Loop A and directional Loop B.

## Artifact ownership

| Process | Sole authoritative writes |
| --- | --- |
| `datafetching.cme_runtime` | `pools/cme/events/databento/**`, CME successful-query cursors, immutable five-minute L2 snapshots, and the existing hourly CME cross-asset feature artifact |
| `datafetching.options_runtime` | Immutable Schwab raw-chain, normalized-contract, and option-quality snapshot directories and their pointer; it also maintains the legacy monthly option mirrors |
| `datafetching.orchestrate` (Loop A) | Equity bars and quotes, non-CME shared macro data, fundamentals, technicals, and signals |
| `ml.prediction_runtime` (Loop B) | Immutable directional sample, prediction, evaluation, monitoring, and intelligence runs plus `ml/latest/run.json` |
| `ml.strategy_runtime` | Immutable strategy candidates, audits, reports, and copied model artifacts plus `ml/strategy-latest/run.json` |

External CME and Options modes are the default. Do not run an inline writer and
its independent runtime against the same datastore. A shared exclusive writer
lock rejects that configuration before an inline request can write an owned
artifact. Dead-process lock files are reclaimed on restart; a live owner is
never displaced.

## Recommended startup

Open one PowerShell terminal for each continuous command:

```powershell
# 1. Complete-history CME collection. One request at a time is the conservative default.
python -m datafetching.cme_runtime --datastore-target pc --max-concurrency 1

# 2. Loop A. CME and option-chain work stay external by default.
python -m datafetching.orchestrate --datastore-target pc `
  --watchlist datafetching\watchlist.txt `
  --cme-mode external --options-mode external `
  --providers databento fmp fred schwab sec --interval-minutes 15

# 3. Schwab options. It skips a symbol until a completed Databento 1m clock exists.
python -m datafetching.options_runtime --datastore-target pc `
  --watchlist datafetching\watchlist.txt `
  --interval-minutes 15 --phase-offset-minutes 2

# 4. Directional Loop B. Start after Loop A has published one COMPLETE generation.
python -m ml.prediction_runtime --datastore-target pc --provider databento `
  --horizons 1h 4h 1d 1w --feature-profile loop-a-all-v1 `
  --model-family logistic --calibration platt --round-trip-cost 0.001 `
  --interval-minutes 15 --phase-offset-minutes 5

# 5. Strategy processing. It consumes only an already-published Loop B run.
python -m ml.strategy_runtime --datastore-target pc `
  --interval-minutes 60 --phase-offset-minutes 10
```

Use `--datastore <temporary-path>` during development and migration tests. None
of these commands requires deleting, resetting, or bootstrapping an existing
datastore. Add `--once` to any command for one bounded cycle. Press `Ctrl+C` to
stop a continuous process; the current atomic artifact remains authoritative.

The CME command explicitly loads `DATABENTO_API_KEY` from the repository-root
`.env`. An environment variable already set in the launching shell takes
precedence; the `.env` loader never overwrites it.

## Cadences and phase offsets

The CME defaults are `ohlcv-1m=60s`, `bbo-1m=15s`, and `mbp-10=5s`, with UTC
phase offsets of 1s, 2s, and 0s respectively. Override individual values with
repeated options such as:

```powershell
python -m datafetching.cme_runtime --datastore-target pc `
  --cadence mbp-10=5 --phase-offset mbp-10=0 `
  --overlap-seconds mbp-10=2 --chunk-minutes mbp-10=5
```

The provider's default MBP latest window is only 30 seconds. It is used only to
establish an initial endpoint. Thereafter the runtime retains complete event
history from its `queried_through` cursor, with a safety overlap, while also
publishing latest-state snapshots. It is not treated as a historical
continuation window.

Complete-history MBP streaming requests default to a 250,000-record safety cap
(about 92 MB of uncompressed MBP-10 records). A response that reaches the cap
is not treated as complete and is not allowed to advance the cursor. The
runtime first splits its symbol set, then its exact time range, and retries
until every child request is unsaturated. Override the cap only when needed:

```powershell
python -m datafetching.cme_runtime --datastore-target pc `
  --record-limit mbp-10=250000
```

This preserves complete history while preventing Databento's provisional
recent-data size estimate from turning a small recovery slice into a nominal
greater-than-5-GB streaming request.

Options default to every 15 minutes at UTC phase +2 minutes. Loop A defaults to
15 minutes. Loop B defaults to phase +5 minutes, and Strategy defaults to hourly
at phase +10 minutes. These offsets are operational defaults, not timestamp
semantics.

## Three clocks and causal selection

CME data keep three separate concepts:

- the exact provider event timestamp, stored at nanosecond precision and never
  rounded or overwritten;
- `fetched_at`, the local receipt/availability clock (there is no redundant
  `received_at` alias);
- `snapshot_for`, the clean derived boundary. L2 boundaries are aligned to
  `:00`, `:05`, `:10`, and so on.

An event enters a five-minute snapshot only when both its provider event time
and `fetched_at` are no later than `snapshot_for`. A late-arriving old event
therefore cannot be inserted retroactively into an already immutable bucket.
Snapshots record event age, receipt age, and quality status.

An Options receipt also separates `snapshot_for` from `available_at`.
`snapshot_for` is the latest completed Databento 1m decision boundary;
`available_at` is when the local Schwab response completed. Realized-volatility
context is capped at the last successfully committed Loop A generation. An
active or failed Loop A cycle does not hide or replace that prior committed
cutoff, and the Options process never waits for the active cycle.

Directional Loop B uses its completed Loop A generation time as the causal
input cutoff. It reads checksum-verified option receipts with
`available_at <= cutoff`; an in-progress generation is invisible. Existing
option freshness semantics are unchanged:

| Directional horizon | Maximum option age |
| --- | ---: |
| `1h`, `4h` | 2 hours |
| `1d` | 1 day |
| `1w` family | 3 days |

Strategy may run later. Its manifest binds the exact authoritative Loop B
record, every selected option receipt, and the stock BBO source files. It never
adds files to or changes the source Loop B directory.

## Atomic publication

Each option generation is first written into a private staging directory. Raw,
normalized, and surface Parquets are checksummed; `manifest.json` and
`receipt.json` are written only after all three succeed. The directory is then
renamed into its immutable name and the per-symbol pointer is atomically
replaced. Receipt-aware readers ignore staging directories and directories
without a receipt. The legacy monthly mirrors are compatibility outputs, not a
commit signal.

Loop B follows the same immutable-run plus receipt plus atomic-pointer pattern
at `ml/latest/run.json`. Strategy has a separate authority at
`ml/strategy-latest/run.json`. Directional publication completes before
Strategy begins and remains valid if Strategy is slow or fails.

## Restart and delay behavior

- CME advances its cursor to every successful query endpoint, including a quiet
  range with zero events. Restart begins at the previous successful endpoint
  minus the configured overlap. Large gaps are chunked, persisted partitions
  are deduplicated, and a failure leaves the cursor before the failed range.
- CME OHLCV history is partitioned daily; BBO and MBP history is partitioned
  hourly, so a small continuation never rewrites an unbounded Parquet.
- An orphan CME or Options staging directory is not authoritative. The last
  valid pointer remains readable. A committed CME L2 directory whose pointer
  update was interrupted republishes that pointer on restart.
- Options retries or skips independently. Loop A continues with fundamentals,
  technicals, and signals, and Loop B reuses the newest causally eligible
  committed option receipt subject to the unchanged freshness limits.
- Strategy retries the current Loop B source until it publishes a matching
  Strategy receipt. A slow Strategy run cannot hold directional predictions
  open.
- If CME is delayed, Loop A and Loop B continue. CME-derived features are used
  only when their existing hourly point-in-time freshness contract permits.
  That contract and calculation version were not changed.

## Compatibility modes

Inline modes exist only for controlled compatibility operation:

```powershell
python -m datafetching.orchestrate --datastore-target pc `
  --cme-mode inline --options-mode inline --once
```

Stop the independent CME and Options processes before using that command. The
writer locks deliberately reject two owners. `--skip-cme` remains available,
but is unnecessary in the recommended external mode.

## Console timing output

Structured timings use compact one-line `START` and `END` records by default.
They omit null fields but retain the UTC correlation time, stage, symbol,
provider/schema, request range, attempt, row count, operation, status, and
elapsed milliseconds. For JSON Lines ingestion, set the format before starting
a process:

```powershell
$env:DUCKETS_TIMING_FORMAT = "json"
python -m datafetching.cme_runtime --datastore-target pc
```

## Concurrency and performance check

CME accepts only `--max-concurrency 1` or `2` and defaults to 1. The repository
fixture benchmark verifies that two workers remain bounded and shortens four
independent delayed requests. A real-provider comparison should retain the
default of one unless operational 504 rates show that two is safe.

Run all local hot-path benchmarks without touching the operational datastore:

```powershell
python benchmarks\benchmark_orchestration_hotpaths.py
```

The benchmark compares legacy ID-generating analytical reads, dictionary/JSON
continuation upserts, and repeated full-frame strategy receipt scans with their
new analytical, vectorized, and indexed paths.
