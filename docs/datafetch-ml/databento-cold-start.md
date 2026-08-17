# Databento cold-start bootstrap

`datafetching.databento_cold_start` performs the initial historical baseline and
can later plan bounded overlap-fill runs for a newer `--as-of` date. It is
not an eighth production loop and must not replace any command in
`current_start_command`. It does not acquire the CME, Loop A, Options, Pricing,
Loop B, ALFRED, or Strategy locks. It writes no readiness, option snapshot,
model, or production publication pointer. For each nonempty, checksum-verified
OPRA symbol/schema scope it does publish the current v5 history cursor under
`state\symbol-history`; that narrow handoff lets Options Capture own later daily
overlap maintenance and does not grant live snapshot authority.

## Scope and storage

The checked-in `datafetching/watchlist.txt` supplies the direct US-equity
universe. Each entry is fetched directly from the US-equity dataset and is
expanded only to its canonical OPRA parent (`AAPL` becomes `AAPL.OPT`). A
duplicate watchlist symbol or pre-expanded parent is rejected.

The cold archive defaults to `XNAS.ITCH`, whose eight-year provider range and
schema catalog cover the configured interval, definition, and non-interval event
scopes. This is deliberately separate from Loop A's live
`DATABENTO_EQUITIES_DATASET` setting, which may be `EQUS.MINI` and does not
offer the full cold-start schema set. Override only with
`--equities-dataset` or `DATABENTO_COLD_START_EQUITIES_DATASET`; provider
metadata must still prove the exact schema/range contract.

CME is intentionally not inferred from equity symbols. The command requires
the existing explicit CME configuration:

```powershell
$env:DATABENTO_CME_DATASET = 'GLBX.MDP3'
$env:DATABENTO_CME_CONTEXT_SYMBOLS = 'NQ.c.0 ES.c.0'
$env:DATABENTO_CME_CONTEXT_STYPE_IN = 'continuous'
```

`DATABENTO_CME_CONTRACT_SYMBOLS` and
`DATABENTO_CME_CONTRACT_STYPE_IN` are also supported. Alternatively, pass one
or more `--cme-symbol` values together with `--cme-stype-in` and
`--cme-dataset`. Missing, duplicate, or mixed CME scope fails before a
Databento request is made.

OPRA uses the canonical consumer contract at:

```text
C:\DATASTORE\market-data\databento\opra\OPRA.PILLAR
```

Its existing provider DBN, normalized Parquet, manifest, receipt, health, and
per-symbol history cursor conventions remain in force. CME and US-equity
bootstrap archives use a separate, non-live namespace:

```text
C:\DATASTORE\market-data\databento\cme\GLBX.MDP3
C:\DATASTORE\market-data\databento\us-equities\XNAS.ITCH
C:\DATASTORE\state\databento\history
C:\DATASTORE\state\databento\history-cursors
```

All durable folder names state their market, dataset, schema, symbol, and date
scope directly. Checksums and request IDs remain inside JSON and are never used
as directory or file names. A representative tree is:

```text
C:\DATASTORE
├── market-data\databento
│   ├── opra\OPRA.PILLAR\cbbo-1s\AAPL.OPT\dates\2026-08-14\segments\full-day
│   ├── cme\GLBX.MDP3\mbp-10\NQ.C.0\windows\2026-08-14_to_2026-08-15
│   └── us-equities\XNAS.ITCH\ohlcv-1m\AAPL\windows\2026-05-07_to_2026-08-15
└── state\databento
    ├── history\prediction-focused-baseline\as-of\2026-08-15
    │   ├── manifest.json
    │   ├── preflight.json
    │   └── progress.json
    └── history-cursors\us-equities\XNAS.ITCH\ohlcv-1m\AAPL\cursor.json
```

Every published provider partition uses the same four artifact names:
`provider.dbn.zst`, `normalized.parquet`, `manifest.json`, and `receipt.json`.
The initial baseline and every later overlap-fill run go through these same path
builders and writers. Existing verified partitions are reused; damaged or
incomplete evidence fails closed. Keep `--as-of` fixed when resuming an
interrupted run so the same deterministic manifest is selected.

## Exact coverage

The manifest applies the checked-in Standard-plan entitlement exactly. The
normal configured scope is included data access; the command rejects a
provider range or edited manifest outside these boundaries instead of offering
an alternate execution mode.

| Dataset/schema | Configured window |
| --- | ---: |
| `ohlcv-1s` | 10 days |
| `bbo-1s` | 3 days |
| `cbbo-1s` | 1 day |
| Every OHLCV/BBO `*-1m` schema | 100 days |
| `cbbo-1m` | 20 days |
| Every `*-1h` schema | 1,825 days |
| Every `*-1d` schema | 2,555 days |
| Every `definition` schema | 100 days |
| `mbp-10` and `mbo` | one day |
| Every other non-interval schema | one calendar month |

The suffix rule includes BBO/CBBO interval schemas as well as OHLCV. Loop A's
native US-equity OHLCV requests use these same four limits; after the initial
fetch they resume from the latest persisted timestamp with overlap and Parquet
upsert deduplication. Verified OPRA bootstrap cursors hand the same pattern to
Options Capture for all OPRA schemas.

Schema coverage is:

- OPRA: `ohlcv-1s`, `ohlcv-1m`, `ohlcv-1h`, `ohlcv-1d`, `definition`,
  `statistics`, `status`, `tcbbo`, `cbbo-1s`, `cbbo-1m`, and
  `trades`.
- CME: the four OHLCV schemas, `definition`, `statistics`, `status`,
  `tbbo`, `bbo-1s`, `bbo-1m`, `trades`, `mbp-10`, and `mbo`.
- US Equities: the four OHLCV schemas, `definition`, `statistics`, `status`,
  `tbbo`, `bbo-1s`, `bbo-1m`, `trades`, `mbp-10`, `mbo`, and
  `imbalance`.

`cmbp-1` and `mbp-1` remain included-plan schemas but are deliberately
excluded from the prediction-focused default baseline. `cmbp-1` is
research-only in this repository, while the production depth feature uses
`mbp-10`; both large schemas remain available for separately scoped research
requests.

`EQUS.SUMMARY` is a distinct consolidated provider dataset and is not
manufactured from the venue-specific `XNAS.ITCH` records. The normal cold
archive does not request or claim that separate summary product.

## Safe commands

Use one fixed date for a preflight/execution/resume sequence. Omitting `--as-of`
during preflight selects the latest exclusive date bound common to every
required provider schema, so weekend and holiday starts do not request a future
historical boundary. Execution requires the same explicit date so it can select
the saved receipts without repeating provider metadata calls. An explicit
unavailable date still fails closed. These commands make no provider data
download until the last one.

```powershell
$BootstrapAsOf = '2026-08-15'

python -m datafetching.databento_cold_start `
  --datastore-target pc `
  --watchlist datafetching\watchlist.txt `
  --equities-dataset XNAS.ITCH `
  --as-of $BootstrapAsOf `
  --dry-run
```

`--dry-run` does not require Databento credentials or make network requests.
It validates the local universe/CME scope and prints the requested manifest.

```powershell
python -m datafetching.databento_cold_start `
  --datastore-target pc `
  --watchlist datafetching\watchlist.txt `
  --equities-dataset XNAS.ITCH `
  --as-of $BootstrapAsOf `
  --preflight
```

`--preflight` requires `DATABENTO_API_KEY`, uses only Databento metadata for
record counts and estimated compressed download sizes, writes the
manifest/preflight evidence, and calculates required free capacity as:

```text
5 GiB + 2 × total estimated download GiB
```

It reports one line per dataset/schema/symbol and blocks when the destination
volume is too small. It never truncates scope, deletes data, or makes a
timeseries request.

After reviewing the preflight, execution is deliberately explicit:

```powershell
python -m datafetching.databento_cold_start `
  --datastore-target pc `
  --watchlist datafetching\watchlist.txt `
  --equities-dataset XNAS.ITCH `
  --as-of $BootstrapAsOf `
  --execute `
  --confirm-download
```

Execution checksum-verifies and reuses the saved manifest and preflight. It does
not repeat Databento catalog, record-count, or estimated-size calls. Current
free disk space is rechecked locally before any download. Use
`--refresh-preflight` with `--execute` only when intentionally replacing the
saved receipt after the requested scope has changed. Rerun the identical normal
execution command after a failure; verified partitions are checked and skipped,
while only incomplete scopes resume. A missing credential, schema, CME scope,
entitlement match, capacity check, ambiguous expansion, or receipt verification
stops execution before unsafe publication. Storage capacity, including the
reserve and expansion allowance above, is the relevant normal-bootstrap
constraint.

Canonical OPRA cold-start requests automatically choose between two delivery
paths without changing the manifest, schema coverage, date windows, or durable
daily partition layout. A non-book schema with at least 30 missing
provider-available days uses one Databento batch job per parent symbol and
missing contiguous range, with DBN/Zstd output split by UTC day. Shorter gaps
continue to stream,
and exceptionally dense `cbbo-1s`/`cmbp-1` dates retain their record-count-based
intraday segmentation. This avoids using the batch queue for tiny requests while
removing thousands of request/response round trips from multi-year OHLCV
baselines.

Every submitted OPRA job is recorded immediately under
`market-data\databento\opra\OPRA.PILLAR\state\batch-jobs` with its exact request,
planned provider-available dates, job ID, and request checksum. Downloaded daily
files must match Databento's size and SHA-256 inventory, native DBN request
metadata, normalized timestamp bounds, and the canonical partition checks before
publication. Because a provider batch covers one continuous date range, it may
also contain files for dates marked `degraded`; those files are retained in the
job inventory as ignored evidence and are never promoted into the canonical
available-only plan. A completed job records both published dates and
provider-verified no-data dates. Interrupting while a job is queued, downloading,
or publishing therefore resumes that same job and does not resubmit already
covered no-data history. Batch archives are temporary staging; verified provider
DBNs move through the same canonical daily manifest/receipt contract used by
streaming. Daily batch DBNs may also report child option symbols that resolved
for only part of that day. Those files are accepted only when there are no wholly
unresolved symbols and normalization maps every returned row to a non-null raw
symbol; the partial-symbol count is retained in the partition's delivery
evidence.

The OPRA schema cursor is refreshed once per completed symbol/schema scope, and
the expensive all-partition health inventory is refreshed once after the OPRA
block rather than after every daily partition or parent-symbol request. Physical
Parquet compaction is deliberately separate: existing consumers continue to see
the established daily paths during the accelerated cold-start resume.

Generic CME and US-equity downloads use up to ten retries after the initial
provider call for transient stream or network failures (for example a
prematurely ended response, connection reset/timeout, or HTTP 502/503/504).
Exponential backoff is capped at 30 seconds per retry and at three minutes in
total, excluding time spent inside the provider calls themselves. Each call
writes to a new readable `attempt-NNN` directory. Failed and interrupted
attempts remain beneath `market-data\databento\.staging` for inspection; they
are never overwritten or treated as complete. Authentication, entitlement,
invalid schema/symbol, and other non-transient provider failures are not
retried.

When three small failed attempts have the same byte-for-byte partial response,
or ordinary streaming retries otherwise exhaust, the generic downloader uses
Databento's batch API for that same preflighted request. It requests one DBN/Zstd
file with `split_duration=none`, records the job ID and request checksum in
`batch-fallback.json`, polls the job, and downloads the provider file through
the SDK's resumable batch downloader. The provider-reported size and SHA-256,
the local raw checksum, normalized Parquet bounds, partition manifest, and
receipt must all verify before atomic publication. A malformed job, unexpected
job state, expired job, unsafe filename, multiple DBN files, or checksum
mismatch fails closed.

Batch fallback state is durable. If the command is interrupted while the job is
queued, processing, or downloading, rerunning the identical execution command
reuses the recorded job rather than submitting and billing another job. Retained
matching partials also cause a resumed run to choose the batch path immediately
instead of repeating a known-bad stream.

Before making another provider request, resume checks retained staging for the
same exact manifest request. A staging attempt is published only after its
request identity, manifest/receipt relationship, raw and normalized checksums,
Parquet timestamps, and row count all verify. Incomplete or corrupt attempts
remain staged. The Databento DBN source handle is explicitly released before a
Windows directory rename, before retrying, and while propagating an interrupt.
Any execution error other than an already-supported no-data result is recorded
and stops the coordinator immediately, so a publication or provider outage does
not trigger the remaining manifest downloads. `Ctrl+C` is not converted into an
ordinary request failure; the runtime lock unwinds and the current staging
attempt remains resumable.

For OPRA, Databento can return a valid provider-native DBN whose request
metadata and symbol mappings are present but whose data-record stream is empty.
Because the SDK emits no Parquet file for that response, the canonical writer
reopens the DBN, verifies its dataset, schema, exact UTC interval, symbol scope,
and symbology against the request, and probes the record iterator to clean EOF.
Only that provider-confirmed case is reported as, for example,
`NO_DATA ohlcv-1d/2025-08-24 provider returned a readable zero-record DBN`.
The native staging evidence is retained, no empty canonical partition is
published, and synchronization continues to later dates even under the
cold-start's fail-fast policy. The date's weekday, warning text, file size, and
missing Parquet file are not classification evidence. An unreadable, malformed,
truncated, partial, mismatched, or nonempty DBN whose conversion emitted no
Parquet remains fatal; checksum, timestamp, duplicate-key, manifest, receipt,
and atomic-publication validation are unchanged.

Databento `BentoWarning` messages about reduced-quality days are provider data
quality metadata, not transport or publication failures and not a quality
guarantee. They remain visible in command output. Warnings observed on a
successfully published generic request are also copied into that partition's
`manifest.json` under `provider_warnings` for later audit.

## Ownership boundary after execution

The coordinator holds only `.ducketz-databento-cold-start.lock` for its own
one-shot manifest and the canonical OPRA `state\sync.lock` while an OPRA scope
is synchronized. It never takes `.ducketz-cme-writer.lock`,
`.ducketz-orchestration.lock`, or `.ducketz-options-writer.lock`.

On verified OPRA completion it records `requested_start`, `completed_through`,
the exact lookback policy, and `bootstrap_manifest_id` in an
`options-opra-symbol-history-v5` cursor. Options Capture validates that cursor
before it performs forward overlap maintenance. CME and US-equity request
cursors remain cold-start progress state only and are not consumed as live-loop
authority.

For later maintenance, rerun the same preflight/execution sequence with a newer
`--as-of` date. A readable per-market/dataset/schema/symbol cursor changes the
request mode from `initial-baseline` to `overlap-fill`. The overlap is retained
for safe boundary reconciliation, while the new request is stored under the
same schema/symbol hierarchy and with the same four artifact filenames. OPRA's
Options-owned daily catch-up already calls the same canonical OPRA partition
writer. Loop A and the CME runtime likewise retain their established stable
upsert filenames for their prospective bar and event views; they do not create
timestamped or hash-named follow-up files.
