# Databento cold-start bootstrap

`datafetching.databento_cold_start` is a one-time historical bootstrap. It is
not an eighth production loop and must not replace any command in
`current_start_command`. It does not acquire the CME, Loop A, Options, Pricing,
Loop B, ALFRED, or Strategy locks; it writes no readiness, snapshot, live-loop
cursor, model, or publication pointer.

## Scope and storage

The checked-in `datafetching/watchlist.txt` supplies the direct US-equity
universe. Each entry is fetched directly from the US-equity dataset and is
expanded only to its canonical OPRA parent (`AAPL` becomes `AAPL.OPT`). A
duplicate watchlist symbol or pre-expanded parent is rejected.

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
C:\DATASTORE\market-data\databento-opra\OPRA.PILLAR
```

Its existing provider DBN, normalized Parquet, manifest, receipt, health, and
per-symbol history cursor conventions remain in force. CME and US-equity
bootstrap archives use a separate, non-live namespace:

```text
C:\DATASTORE\market-data\databento-cold-start\archive-v1
C:\DATASTORE\state\databento-cold-start
```

Each non-OPRA request stores `provider.dbn.zst`, `normalized.parquet`, a
checksummed manifest, and a receipt. The state tree stores an immutable
request manifest, metadata preflight record, progress status, and a completed
request cursor. Existing verified partitions are reused; damaged or incomplete
evidence fails closed. Keep `--as-of` fixed when resuming so the same
deterministic manifest is selected.

## Exact coverage

Every dataset gets these windows:

| Schema | Window |
| --- | --- |
| `ohlcv-1s` | 5 days |
| `ohlcv-1m` | 100 days |
| `ohlcv-1h` | 2,000 days |
| `ohlcv-1d`, `definition` | 5,000 days |
| Every other available schema | one calendar month |

Schema coverage is:

- OPRA: `ohlcv-1s`, `ohlcv-1m`, `ohlcv-1h`, `ohlcv-1d`, `definition`,
  `statistics`, `status`, `cmbp-1`, `tcbbo`, `cbbo-1s`, `cbbo-1m`, and
  `trades`.
- CME: the four OHLCV schemas, `definition`, `statistics`, `status`, `mbp-1`,
  `tbbo`, `bbo-1s`, `bbo-1m`, `trades`, `mbp-10`, and `mbo`.
- US Equities: the four OHLCV schemas, `definition`, `statistics`, `status`,
  `mbp-1`, `tbbo`, `bbo-1s`, `bbo-1m`, `trades`, `mbp-10`, `mbo`, and
  `imbalance`.

US Equities Full Market Summary is recorded as a derived/reused view of
`ohlcv-1d`, `definition`, and `statistics`; no duplicate summary download is
requested.

## Safe commands

Use one fixed date for a preflight/execution/resume sequence. These commands
make no provider data download until the last one.

```powershell
$BootstrapAsOf = '2026-08-15'

python -m datafetching.databento_cold_start `
  --datastore-target pc `
  --watchlist datafetching\watchlist.txt `
  --as-of $BootstrapAsOf `
  --dry-run
```

`--dry-run` does not require Databento credentials or make network requests.
It validates the local universe/CME scope and prints the requested manifest.

```powershell
python -m datafetching.databento_cold_start `
  --datastore-target pc `
  --watchlist datafetching\watchlist.txt `
  --as-of $BootstrapAsOf `
  --preflight
```

`--preflight` requires `DATABENTO_API_KEY`, uses only Databento metadata for
record counts and billable sizes, writes the manifest/preflight evidence, and
calculates required free capacity as:

```text
5 GiB + 2 × total billable GiB
```

It reports one line per dataset/schema/symbol and blocks when the destination
volume is too small. It never truncates scope, deletes data, or makes a
timeseries request.

After reviewing the preflight, the only download command is deliberately
explicit:

```powershell
python -m datafetching.databento_cold_start `
  --datastore-target pc `
  --watchlist datafetching\watchlist.txt `
  --as-of $BootstrapAsOf `
  --execute `
  --confirm-billable-download
```

Execution re-runs metadata/capacity preflight immediately before fetching.
Rerun the identical command after a failure; verified partitions are checked
and skipped, while only incomplete scopes resume. A missing credential,
schema, CME scope, capacity check, ambiguous expansion, or receipt verification
stops execution before unsafe publication.
