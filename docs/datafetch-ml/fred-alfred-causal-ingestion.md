# Causal FRED/ALFRED macro ingestion

The production macro contract uses immutable ALFRED real-time intervals for
`FEDFUNDS`, `CPIAUCSL`, `UNRATE`, and `GDP`. Ordinary FRED graph-CSV snapshots
continue to be fetched for monitoring and prospective use, but they are never
eligible as historical Loop B evidence.

Run the one-time backfill after an authoritative `samples.parquet` exists:

```powershell
python -m ml.option_pricing_fred --datastore-target pc --backfill
```

The command derives a common provider request bound from the earliest eligible
1-day or weekly decision. The bound includes twelve monthly lags for CPI, one
monthly lag for unemployment, four quarterly lags for GDP, and each feature's
freshness allowance. It imports all four series, seals the raw and normalized
provider response, appends only new provider interval identities, derives the
complete release context, and runs readiness verification in the same command.
The unemployment-change freshness is independently bounded at 56 days: this is
the smallest tested bound that keeps eligible production coverage above 95%
across the documented 2025 missing-month release gap. CPI remains 45 days and
GDP remains 120 days.

## First run on an empty datastore

A new PC has no authoritative Loop B samples yet. After a successful Loop A
cycle for its complete stock universe, explicitly create decision-only evidence:

```powershell
$Symbols = @(python -c "from datafetching.symbol_universe import read_symbols; print('\n'.join(read_symbols()))")
python -m ml.macro_bootstrap --datastore-target pc --symbols $Symbols
```

Replace `<BOOTSTRAP_RECEIPT>` below with the exact receipt path printed by that command:

```powershell
python -m ml.option_pricing_fred --datastore-target pc --backfill --bootstrap-decisions "<BOOTSTRAP_RECEIPT>"
```

The bootstrap reuses the production adjusted bars, aligned technical inputs,
exchange calendars, processing delays and rolling target-window code. It seals
only symbol/horizon/decision timestamps and their source checksums. It creates
no model, predictions, Gameplan, execution authority or Loop B publication.
There is no automatic fallback to bootstrap evidence.

This explicit contract is limited to the complete daily/weekly scope of
`loop-a-all-bsgp-active-v3` with Databento inputs. It does not change the final
feature profile or any training partition. The ordinary ALFRED importer,
release-time semantics, 95% coverage threshold and zero-lookahead checks still
apply. The first full-profile materialization must reproduce the exact sealed
macro decision population and pass coverage again before it can return inputs
for model fitting. Missing symbols/routes, changed profiles, changed source
files or expanded decisions fail closed.

Keep the source generation stable between sealing and the first Loop B run.
If those inputs change, regenerate the bootstrap and rerun the explicit
backfill/readiness command; do not edit the receipt. After the first full-profile
Loop B publication, use the normal incremental command without the bootstrap
flag so readiness follows authoritative model samples. This bootstrap does not
supply any other required feature family or satisfy Gameplan readiness itself.

## Ongoing updates

After the backfill, one independent owner performs bounded overlapping updates
no more than once per UTC date:

```powershell
python -m datafetching.fred_alfred_runtime --datastore-target pc --utc-hour 7
```

The runtime uses `.ducketz-fred-alfred-import.lock` under the selected datastore root and publishes
an immutable daily receipt. Do not add ALFRED to the per-symbol Loop A worker or
to a 15-minute provider cycle.

ALFRED output type 1 clips intervals already active at the requested overlap
boundary. The importer retains that exact response in the sealed raw artifact,
then restores a clipped start in canonical evidence only when it matches an
identical prior immutable interval. This prevents an incremental request from
refreshing unchanged feature clocks. A gap longer than the bounded window fails
closed and requires the complete backfill command again.

Two receipts have intentionally separate authority:

- `ml/option-pricing-evidence/fred-alfred-vintages/.../receipt.json` seals the
  provider import and remains unchanged with `historical_coverage_status` set
  to `NOT_EVALUATED`.
- `ml/macro-readiness/fred-alfred/.../receipt.json` authorizes Loop B only after
  source checksums, revision uniqueness, lineage, per-feature freshness,
  lookahead, and at least 95% eligible coverage pass for 1-day and every weekly
  route. Because macro evidence is shared across symbols, coverage is measured
  once per horizon decision clock; symbol count and unequal symbol history do
  not reweight the gate. No waiting period or operator approval is part of this
  correctness gate.

Production Loop B must use `loop-a-all-bsgp-active-v3`. Its intraday contracts
remain the v2 sets; only the 1-day and 1-week sets include macro features. The
new feature-set names, versions, and semantic fingerprints prevent reuse of
models trained under the prior all-null macro contract.
