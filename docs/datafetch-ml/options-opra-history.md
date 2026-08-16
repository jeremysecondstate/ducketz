# OPRA Standard history

## Normal six-symbol workflow

Run the one-time bootstrap once for every new parent symbol:

```powershell
python -m datafetching.options_history `
  --datastore-target pc `
  --watchlist datafetching\watchlist.txt
```

The bootstrap is idempotent and resumable. `--symbols` narrows parent symbols and
`--schemas` narrows schemas; omitted flags use the production watchlist and all
Standard schemas. A capacity-blocked or failed scope makes the command exit
nonzero without deleting already verified partitions.

| Schema | Initial lookback | Options Capture overlap |
|---|---:|---:|
| `ohlcv-1s` | 5 days | 1 day |
| `ohlcv-1m` | 100 days | 2 days |
| `ohlcv-1h` | 2,000 days | 5 days |
| `ohlcv-1d` | 5,000 days | 10 days |
| `definition` | 5,000 days | 3 days |
| `cmbp-1` | 1 month | 3 days |
| `status`, `statistics`, `trades`, `tcbbo`, `cbbo-1m`, `cbbo-1s` | 6 months | 3 days |

The recurring `datafetching.options_runtime` runs catch-up at most once per UTC
date. It advances only a verified v5 symbol/schema cursor and reports
`bootstrap required` for a new or invalid cursor; it does not perform a large
initial fetch. Overlap partitions are checksum-verified and naturally
deduplicated before publication.

The current `options-opra-symbol-history-v5` cursor records the exact
`lookback_policy`, `requested_start`, and `completed_through`; a cold-start
handoff also records its `bootstrap_manifest_id`. The reader accepts a legacy
v4 cursor only when its policy exactly equals the former schema-specific
bootstrap policy. Every new or advanced cursor is written as v5.

## Storage contract

The canonical root is:

```text
C:\DATASTORE\market-data\databento-opra\OPRA.PILLAR
```

Each partition is published at
`schema=<schema>\date=<UTC-date>\bucket=<symbol-bucket>\` with
`provider.dbn.zst`, `normalized.parquet`, `manifest.json`, and `receipt.json`.
Entitlement/preflight receipts live under `metadata`, symbol cursors under
`state\symbol-history`, and current verified totals under `health\current.json`.
Staging files are never consumer authority.

The capacity preflight compares destination free space with twice the provider
billable size plus a 5 GiB safety reserve. It blocks the requested scope when it
does not fit; it does not cap the datastore, truncate a date range, or delete
completed data.

## Administrative synchronization

`ml.option_pricing_opra` is for an explicitly chosen maintenance scope. With no
scope flags it means all Standard schemas and the full OPRA universe, which can
be far larger than the six-symbol bootstrap. Use its `--symbols`, `--schemas`,
`--start`, `--end`, and `--max-partitions` flags to bound a maintenance run.

Verify local partitions and republish current health without a provider call or
market-data download:

```powershell
python -m ml.option_pricing_opra --datastore-target pc --health-only
```

Provider metadata and capacity preflight without a timeseries download:

```powershell
python -m ml.option_pricing_opra `
  --datastore-target pc `
  --symbols AAPL.OPT `
  --schemas cbbo-1m `
  --metadata-only
```

An OPRA-enabled process, a configured provider name, an estimate, or an empty
directory is not proof of historical acquisition. Require nonzero normalized
Parquet rows, verified receipts/checksums, timestamp bounds, health counts, and
consumer-usage records.

## All-dataset cold-start alternative

`datafetching.databento_cold_start` can populate the same canonical OPRA
partitions while also building isolated CME and US-equity historical archives.
It is a one-time maintenance/bootstrap command, not a recurring owner. After
each verified OPRA scope it publishes the v5 symbol/schema history cursor so
Options Capture can take over forward maintenance; it does not take the Options
snapshot-writer lock or publish an option snapshot/pointer. See
[Databento cold-start bootstrap](databento-cold-start.md).
