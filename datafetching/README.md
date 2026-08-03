# Duckets data fetching

The `datafetching` package owns provider ingestion and normalized storage. The
Loop A supervisor then invokes the fundamental, technical, and signal
calculations over those current files.

## Datastore layout

Stock-specific data uses a symbol-first hierarchy:

```text
DATASTORE/
└── stocks/
    └── NVDA/
        ├── bars/
        │   ├── 1m/
        │   │   ├── databento/{raw,normalized}/
        │   │   └── schwab/{raw,normalized}/
        │   ├── 1h/
        │   └── 1d/
        ├── quotes/
        ├── corporate/
        ├── fundamentals/
        ├── technicals/
        ├── signals/
        └── errors/
```

Shared context that does not belong to one stock lives under `pools`:

```text
DATASTORE/
└── pools/
    ├── macro/
    │   ├── GDP/GDP/fred/
    │   ├── CPI/CPIAUCSL/fred/
    │   ├── unemploymentRate/UNRATE/fred/
    │   ├── federalFunds/FEDFUNDS/fred/
    │   └── CLUSD/quote/fmp/
    └── cme/
        ├── CME_CONTEXT/
        └── CME_CONTRACTS/
```

## One readable ID per Parquet

Every raw, normalized, error, calculated, and model-facing Parquet has one
Duckets-generated string column named `id`. The value normally uses the
smallest natural key available in that file:

- normalized bars: `timestamp`;
- FRED observations: observation date;
- corporate statements: filing, period, or date columns available in the
  provider response;
- raw snapshots: a provider timestamp or another complete, unique readable
  provider key;
- errors: source, category, request, error type, and error message.

The Duckets `id` value is not a hash or UUID. No other Duckets-generated
`*_id`, `*_ids`, digest, receipt, or lineage column is persisted.
Loop generation, write-state, lease, acknowledgement, and other control-plane
fields are also stripped from normalized and calculated Parquets.

Provider-ingestion writers adapt when the usual recipe is not unique: they add
complete readable event values until it is unique. If a new provider shape has
no safe readable combination, the provider file receives deterministic
file-local `row-NNNNNN` IDs instead of failing the fetch. Explicit calculated
and Loop B schemas keep their declared natural-key validation.

Provider-native identifiers, UUIDs, and hashes are preserved in raw
provider-shaped data without a source allowlist. They remain provider values,
not the Duckets `id`. Calculated and normalized outputs do not carry them.

See
[`docs/datafetch-ml/parquet-id-contract.md`](../docs/datafetch-ml/parquet-id-contract.md)
for the repository-wide rule.

## Canonical Parquet datasets

Each provider request owns one predictable Parquet filename instead of creating
a new file for every fetch. Examples:

```text
stocks/NVDA/bars/1m/databento/normalized/NVDA_source_20d_1m_ohlcv-1m_1m.parquet
stocks/NVDA/corporate/income_statement/fmp/normalized/NVDA_income_statement.parquet
pools/macro/CPI/CPIAUCSL/fred/normalized/CPI_CPIAUCSL.parquet
```

Repeated fetches use atomic upserts:

- existing natural keys are replaced when provider values change;
- new natural keys append;
- identical data does not rewrite the file;
- raw JSON keeps the latest distinct snapshots in its canonical file;
- quote history appends only when market values change.

Normalized Databento bars have this exact physical order:

```text
id, timestamp, open, high, low, close, volume
```

`id` equals the readable UTC `timestamp`. The path supplies symbol, provider,
timeframe, scope, and request context; those values are not duplicated as
identifiers on every normalized row.

## Fetch commands

Bootstrap or refresh one symbol:

```powershell
python -m datafetching.main NVDA --profile full --datastore-target local
python -m datafetching.main NVDA --profile incremental --datastore-target local
```

Select provider lanes or a custom datastore:

```powershell
python -m datafetching.main NVDA --providers databento fmp fred --datastore D:\market-data
```

Refresh only official macro data:

```powershell
python -m datafetching.main NVDA --providers fred --datastore-target local
```

Skip shared CME context:

```powershell
python -m datafetching.main NVDA --skip-cme --datastore-target local
```

When no datastore argument is supplied, path selection uses
`DUCKETS_DATASTORE_DIR`, then `DUCKETS_OHLCV_PARQUET_DIR`, then the configured
default.

## Loop A supervisor

Put one symbol per line in `datafetching/watchlist.txt`, or pass `--symbols`.

Run continuously:

```powershell
python -m datafetching.orchestrate --datastore-target local
```

Run one complete cycle:

```powershell
python -m datafetching.orchestrate --datastore C:\data\ducketz --symbols NVDA GOOG --once
```

Change the polling interval:

```powershell
python -m datafetching.orchestrate --datastore-target local --interval-minutes 30
```

Each cycle:

1. fetches the selected provider lanes across the watchlist, using one
   multi-symbol Databento request per shared continuation window, FMP batch
   quote/market-cap requests, and a Schwab batch quote request;
2. fetches shared FRED, FMP commodity, and CME context once;
3. persists raw and normalized provider data;
4. recalculates point-in-time fundamentals;
5. recalculates technical metrics;
6. rebuilds cross-domain signals;
7. reports hard provider/persistence failures separately from non-blocking local
   calculation advisories and, unless `--once` was used, waits for the next
   interval.

Only hard failures mark a Loop A generation `FAILED`. Optional project-local
feature quality skips are stored under `diagnostics`, leave fetched provider
rows intact, and do not block Loop B from validating the available inputs.

The supervisor creates `.ducketz-orchestration.lock` in the datastore so two
Loop A processes cannot write the same files. `Ctrl+C` exits the loop and removes
the lock.

Provider endpoints that only accept one symbol remain isolated: FMP statements
and filings, Schwab price history and option chains, and SEC filing text. A
failed batch request falls back to those existing per-symbol paths. Loop A also
requests only the maximal Schwab history window for each native frequency; the
shorter overlapping windows previously produced no additional consolidated bar
coverage.

Useful calculation skips are `--skip-fundamentals`, `--skip-technicals`, and
`--skip-signals`. `--skip-cme` disables only shared CME fetching.

## Macro authorities and freshness

Official U.S. economic indicators come from FRED:

| Output | FRED series | Authority |
| --- | --- | --- |
| GDP | `GDP` | U.S. Bureau of Economic Analysis |
| CPI | `CPIAUCSL` | U.S. Bureau of Labor Statistics |
| Unemployment | `UNRATE` | U.S. Bureau of Labor Statistics |
| Federal funds | `FEDFUNDS` | Federal Reserve Board |

The FRED lane saves the complete available series and records the readable
series name, source agency, latest observation date, freshness age, and freshness
status. `FredSeriesSpec.series_id` selects the requested FRED series in code but
is not persisted as an identifier column. Stored rows use readable series,
endpoint, and path context instead. A stale series fails closed and creates an
error Parquet.

FMP remains the source for stock-specific corporate data, split history, and
commodity proxy quotes.

## Data semantics

Databento raw and normalized OHLCV preserve the provider's unadjusted market
scale. FMP split history remains under the stock's
`corporate/stock_splits` folder. The `technicals` package validates and applies
split adjustments without modifying source Parquets.

Completion and session fields are derived from the bar timestamp when needed.
Provider detail remains in raw/error artifacts instead of being copied into
calculated rows.
