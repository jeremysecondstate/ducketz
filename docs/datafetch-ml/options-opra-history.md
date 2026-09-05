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

The legacy `datafetching.options_runtime` command retains
`--skip-historical-catchup`; prospective capture and potentially large history
downloads cannot compete inside that owner. The current single overnight
workflow invokes Loop A after the 17:00 PT close, and Loop A owns the daily
subprocess for `ohlcv-1h`, `cbbo-1m`, and `definition`. Exact `cbbo-1m` supplies
historical Strategy entry/exit BBOs, hourly bars supply surface context, and
definitions supply point-in-time identity. The standalone Scheduled task is
paused to prevent duplicate ownership. The maintenance lane advances
only a verified v5 symbol/schema cursor and reports `bootstrap required` for a
new or invalid cursor; it never turns a missing scope into a large initial fetch. Overlap
partitions are checksum-verified and naturally deduplicated before publication.
Each symbol/schema scope verifies its own receipts and files, while the
expensive global health inventory is rebuilt once after the complete batch
rather than once per scope. After a successful batch, Loop A invokes the
audit-only datastore hygiene command so the generated authority catalog reflects
the new dates; no cleanup flag is included.

The bounded production maintenance command is:

```powershell
.\.venv\Scripts\python.exe -m datafetching.options_history `
  --datastore-target pc `
  --schemas ohlcv-1h cbbo-1m definition `
  --incremental-only `
  --max-estimated-download-bytes 20000000000 `
  --max-estimated-cost-usd 1 `
  --max-incremental-catchup-days 30
```

Every requested scope receives a provider byte/cost preflight before any
download. Selection is oldest-cursor-first within each schema. Scopes that
would exceed the aggregate run budget are explicitly deferred to a later run,
not truncated or partially published. See
`docs/loops-system-analysis/OPRA_HISTORY_MAINTENANCE_AUTOMATION.md`.

At night, “current” means every required cursor covers the most recently
completed market session. The session's last option quote may naturally be
hours old after close. This is valid model/planning evidence and is distinct
from the current-quote requirement of any future live same-leg execution gate.

**Observed 2026-09-04 UTC:** the corrective catch-up preflighted and completed
all 18 production scopes (three schemas by six symbols), selected an estimated
11,464,500,352 bytes at USD 0, and reported zero failed, deferred,
capacity-blocked, or bootstrap-required scopes. Every production cursor is at
the exclusive `2026-09-04` boundary; `health/current.json` reports September 3
latest events for `ohlcv-1h`, `cbbo-1m`, and `definition`.

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
estimated compressed download size plus a 5 GiB safety reserve. Guarded runs
also compare the aggregate selected provider estimate with explicit byte and
USD budgets. A missing cost estimate fails a finite cost gate closed. These
checks do not cap the datastore, truncate a selected date range, or delete
completed data.

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

The provider-native DBN, normalized Parquet, manifest, and receipt inside one
published date/segment are complementary evidence encodings. Do not flatten
them or all dates into one mega-Parquet. Prospective OPRA/Schwab chain receipts
under `stocks/<SYMBOL>/options/snapshots/` are a different point-in-time
evidence family and never merge into historical OPRA. The generated authority
inventory is `C:\DATASTORE\catalog\market-data\current.json`.

## All-dataset cold-start alternative

`datafetching.databento_cold_start` can populate the same canonical OPRA
partitions while also building isolated CME and US-equity historical archives.
It is a one-time maintenance/bootstrap command, not a recurring owner. After
each verified OPRA scope it publishes the v5 symbol/schema history cursor so
Loop A can take over bounded forward maintenance; it does not take the Options
snapshot-writer lock or publish an option snapshot/pointer. See
[Databento cold-start bootstrap](databento-cold-start.md).
