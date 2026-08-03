# Duckets fundamental direction

`fundamental-direction` is a point-in-time 0–100 score derived from annual and
quarterly FMP financial statements. It keeps earnings momentum, cash conversion,
accrual quality, balance-sheet resilience, tax quality, and
investment/dilution visible alongside the composite.

## Output

```text
DATASTORE/stocks/<SYMBOL>/fundamentals/fundamental-direction/fmp/quarterly.parquet
DATASTORE/stocks/<SYMBOL>/fundamentals/fundamental-direction/fmp/annual.parquet
```

Every file begins with one readable Duckets-generated `id`:

```text
id = period_end_date
```

If a provider supplies more than one fiscal row for the same period-end date,
the writer uses `period_end_date|fiscal_period` instead. It always selects the
smallest complete natural key that is unique inside the file.

The remaining columns contain readable dates, fiscal values, component scores,
confidence, and the final `fundamental_score` value. There are no separate
statement, feature-set, source, snapshot, or calculation identifiers.

The effective timestamp is the latest accepted or filing date across the
statements used for a fiscal period. If neither is available, the calculation
uses a conservative 90-day estimate and lowers confidence.

When market-regime rows are enriched, the most recent publicly available
quarterly score is joined with `merge_asof` on timestamps. The original
`technical_score` is preserved. `combined_conviction_score` uses at most 30%
fundamental weight, reduced by confidence and a 180-day freshness half-life.

Run directly:

```powershell
python -m fundamentals.main NVDA --datastore-target local
```

Loop A runs this calculation after provider fetching unless
`--skip-fundamentals` is supplied.
