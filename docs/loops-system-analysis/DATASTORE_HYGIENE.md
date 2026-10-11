# Datastore authority and hygiene

Reviewed October 11, 2026: the provider-separation and storage-lineage contracts
remain relevant; the measurements, cleanup and provider observations below are
dated September 3–4 history. This review did not refresh a catalog or verify
today's provider coverage. See [current system](CURRENT_SYSTEM.md) for preparation
ownership and evidence limits.

The machine-readable catalog is `catalog/market-data/current.json` under this
PC's configured datastore; its human-readable companion is `current.md`.
The following operator command writes refreshed catalog reports without changing
market records. It was not run for this documentation review:

```powershell
.\.venv\Scripts\python.exe -m datafetching.datastore_hygiene `
  --datastore-target pc
```

## Equity bars: similar shape does not mean duplicate authority

The three daily EXAMPLE_EQUITY files that prompted this audit belong to different
datasets and roles:

| Location | Identity | Role | Recurring freshness |
|---|---|---|---|
| `stocks/<SYMBOL>/bars/<TIMEFRAME>/databento/normalized/` | Databento `EQUS.MINI` | canonical Loop A operational OHLCV | yes |
| `stocks/<SYMBOL>/bars/<TIMEFRAME>/schwab/normalized/` | `SCHWAB_PRICE_HISTORY` | secondary long-history/research series | no; Loop A uses Schwab quotes, not recurring Schwab history |
| `market-data/databento/us-equities/XNAS.ITCH/` | Databento `XNAS.ITCH` | distinct archive provenance and configured independent-stock target/feature history | maintained by `ml.stock_target_history` when that archive mode is configured; not merged into operational EQUS |

They must remain provider- and dataset-separated. A timestamp-only append would
silently combine a venue-specific series, a broad operational feed, and a
broker price-history definition. It would make volume and OHLC semantics
ambiguous and destroy reproducible lineage.

The 2026-09-03 live audit proved this is not merely theoretical. Across all six
symbols, every daily XNAS/EQUS and Schwab/EQUS comparison had overlapping dates
but **zero exact OHLC rows and zero exact OHLCV rows**. For EXAMPLE_EQUITY, the overlaps
were 849 XNAS/EQUS dates and 851 Schwab/EQUS dates. The canonical EQUS daily
series had 863 rows from 2023-03-28 through 2026-09-03; XNAS had 1,757 rows from
2019-08-19 through 2026-08-14; Schwab had 5,032 rows from 2006-08-17 through
2026-08-18. The other symbols showed the same non-equivalence, with EXAMPLE_EQUITY's
shorter listing history handled separately. Those counts belong to the dated
audit; a catalog's own observation time controls any later coverage claim.

Loop A readers already discover providers and timeframes, deduplicate within an
identity, and prefer native Databento rows to 1-minute-derived rows. They do not
need one cross-provider mega-Parquet.

## The consolidation that is safe

This describes the implemented consolidation rule and its dated result, not
authorization to run a new destructive cleanup during an ordinary audit.

Databento 1-minute-derived `1h` and `1d` files are latency bridges inside the
same operational dataset. Once a native EQUS row exists at the same timestamp,
the derived row is shadowed and adds no consumer-visible information.
`datafetching.bar_consolidation` now removes only those shadowed timestamps and
retains genuine derived-only gaps. Loop A performs this after successful
derived-bar generation, and the hygiene command can apply it explicitly with
`--consolidate-derived-bars`.

The 2026-09-03 cleanup removed 2,984 shadowed derived rows. Every derived daily
file and five derived hourly files became unnecessary; EXAMPLE_EQUITY retained three
hourly gap rows. This reclaimed 180,857 bytes without changing native EQUS,
Schwab, or XNAS data.

## OPRA history and prospective options are separate evidence

`market-data/databento/opra/OPRA.PILLAR` is immutable historical OPRA evidence.
Each date/segment partition intentionally contains provider-native DBN,
normalized Parquet, a manifest, and a receipt. Those are complementary source,
query, and verification encodings—not four disposable duplicates. Date/segment
boundaries make retries and checksum verification bounded; do not concatenate
the archive into one fragile giant Parquet.

`stocks/<SYMBOL>/options/snapshots/<provider>` contains prospective target-time
chain receipts. The audit found 1,146 Schwab fallback snapshots through
2026-09-03T20:00:00Z after the historical OPRA files appeared to stop. These
prove that the live Options owner continued publishing evidence; they are not
historical OPRA and must not be relabeled or merged into OPRA partitions.

The production options-strategy history set is exactly `ohlcv-1h`, `cbbo-1m`,
and `definition`. The HGB+MLP Strategy-profit trainer uses exact CBBO-minute
entry/exit economics wherever that coverage exists; `1h` requires that exact
path. Older `4h`/`1d`/`1w` targets may use explicitly labeled conservative
hourly-bar execution estimates calibrated on overlapping CBBO. Definitions
retain point-in-time contract identity. `cbbo-1s`, the
other OHLCV intervals, status, statistics, trades, and `tcbbo` remain retained
research history with no production freshness promise. Expanding that set
requires an explicit consumer and retention decision because those schemas
dominate storage.

The corrective guarded catch-up completed on 2026-09-04 UTC:

- provider-preflighted all 18 six-symbol `ohlcv-1h`/`cbbo-1m`/`definition`
  scopes and estimated 11,464,500,352 download bytes at USD 0;
- selected and completed all 18 within the explicit 20,000,000,000-byte and
  USD 1 limits, with zero failures, deferrals, or bootstrap-required scopes;
- advanced every production cursor to the exclusive 2026-09-04 boundary;
- published latest `ohlcv-1h` and `cbbo-1m` events through the September 3
  market close and latest September 3 definitions; and
- rebuilt `health/current.json`, which reported 6,714 hourly-bar partitions,
  174 CBBO-minute partitions, and 498 definition partitions.

Production history maintenance remains a bounded Loop A acquisition capability
used by its owning preparation path. Current responsibility and schedule evidence
is in [current system](CURRENT_SYSTEM.md); the former monolithic schedule is not
the present universal owner. The maintainer prioritizes the oldest cursor within
each schema, advances at most 30 calendar
days, reuses verified overlap partitions, and publishes health only after the
batch. The September standalone OPRA schedule was paused to prevent duplicate
ownership. That historical task disposition is not a fresh native registration
or process-liveness check. A fresh Strategy-profit model cannot be promoted when
any required
production OPRA cursor trails the newest required completed session. Exact
historical candidate entry/exit economics come from `cbbo-1m`.

Consumer-usage lineage is compacted to a source-file count, SHA-256 fingerprint,
and eight diagnostic examples rather than repeating thousands of full paths in
every daily event. Updating read counters patches the existing verified health
inventory atomically and preserves its original partition-verification clock;
it does not checksum the entire roughly 70 GiB raw-plus-Parquet archive a second
time immediately after maintenance.

## Safe cleanup classes and receipt

**Historical September 4, 2026 cleanup.** The original deletion classes,
irrecoverability statement and receipt below describe that completed operation.
They do not extend its authorization to new files.

Only two broad deletion classes were approved in this audit:

1. abandoned staging attempts old enough to be inactive and lacking published
   manifest/receipt authority; and
2. the bounded 2026-08-19 EQUS migration rollback trees, after all current
   native operational files proved to be `EQUS.MINI`.

The guarded cleanup acquired the dedicated hygiene lock, the cold-start and
OPRA-history locks for staging, and the Loop A/Loop B shared datastore-cycle
lock for derived-bar consolidation; hashed every selected file into an
immutable plan; rechecked path, size, and hash immediately before deletion;
and never selected a published OPRA partition, current operational bar, Schwab
history file, or XNAS archive file.

Cleanup `20260904T040318911826Z` deleted 277 files totaling 653,432,732 bytes
(623.16 MiB): 121 abandoned staging files and 156 migration-backup files. The
deleted bytes are not recoverable. Exact deleted paths and hashes remain in:

- `catalog/cleanups/20260904T040318911826Z/plan.json` under the configured datastore
- `catalog/cleanups/20260904T040318911826Z/receipt.json` under the same datastore

The plan SHA-256 is
`17b63ad82c38410187e7393fcded48502c5cce67f989ee54314393583474c810`.
The catalog refreshed by that September cleanup reported zero remaining
candidates in both approved classes; this is not an October inventory claim.

Any future destructive run must begin as a dry run. `--confirm-cleanup` is the
explicit commit gate; never infer it from an ordinary audit request.
