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
nonzero without deleting already verified partitions. These configured windows
are included Standard-plan access and are checked against
`docs/databento-plan/databento_standard_plan_data_access.md`.

| Schema | Initial lookback | Options Capture overlap |
|---|---:|---:|
| `ohlcv-1s` | 10 days | 1 day |
| `ohlcv-1m` | 100 days | 2 days |
| `ohlcv-1h` | 1,825 days | 5 days |
| `ohlcv-1d` | 2,555 days | 10 days |
| `definition` | 100 days | 3 days |
| `cbbo-1s` | 1 day | 3 days |
| `cbbo-1m` | 20 days | 3 days |
| `status`, `statistics`, `trades`, `tcbbo` | 1 month | 3 days |

`cmbp-1` remains an explicit `--schemas cmbp-1` research choice, but is not
part of the prediction-focused default bootstrap because a single day exceeds
the complete baseline storage target.

The recurring `datafetching.options_runtime` runs catch-up at most once per UTC
date. It advances only a verified v5 symbol/schema cursor and reports
`bootstrap required` for a new or invalid cursor; it does not perform a large
initial fetch. Overlap partitions are checksum-verified and naturally
deduplicated before publication. Each symbol/schema scope still verifies its
own receipts and files, while the expensive global health inventory is rebuilt
once after the complete daily catch-up pass rather than once per scope.

The current `options-opra-symbol-history-v5` cursor records the exact
`lookback_policy`, `requested_start`, and `completed_through`; a cold-start
handoff also records its `bootstrap_manifest_id`. The reader accepts a legacy
v4 cursor only when its policy exactly equals the former schema-specific
bootstrap policy, including the former 5,000-day daily/definition value. This
is read compatibility only; every new or advanced cursor is written as v5 with
the current schema-specific policy.

## Storage contract

The canonical root is:

```text
C:\DATASTORE\market-data\databento\opra\OPRA.PILLAR
```

Each partition is published at
`<schema>\<parent-symbol>\dates\<UTC-date>\segments\<full-day-or-UTC-range>\` with
`provider.dbn.zst`, `normalized.parquet`, `manifest.json`, and `receipt.json`.
Entitlement/preflight receipts live under `metadata`, symbol cursors under
`state\symbol-history`, and current verified totals under `health\current.json`.
Staging files are never consumer authority.

The capacity preflight compares destination free space with twice the provider's
estimated compressed download size plus a 5 GiB safety reserve. It blocks the
requested scope when it does not fit; it does not cap the datastore, truncate a
date range, or delete completed data.

A provider-native DBN that cleanly decodes to zero data records is retained in
staging and skipped as `NO_DATA` only after its dataset, schema, exact UTC
interval, symbols, and symbology are verified against the request. Databento's
SDK does not create a Parquet file for this valid empty response, and the writer
does not publish an empty canonical partition. Weekend/holiday status, warning
text, file size, and a missing Parquet file alone never establish no-data. If
the DBN is unreadable, malformed, truncated, partial, request-mismatched, or has
even one record despite producing no Parquet, synchronization fails closed.
All nonempty checksum, timestamp, duplicate-key, receipt, and atomic-publication
checks remain mandatory.

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
