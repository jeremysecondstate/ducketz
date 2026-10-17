# Explicit recovered 15-minute population

`intraday-multi-provider-recovery-v1` is an opt-in population, separate from
`intraday-verified-source-observations-v1` and all frozen historical forecasts.
Its model candle identity is `multi:databento-schwab-fmp:15m`, dataset
`MULTI_PROVIDER`. This common identity permits a model to use an explicitly
mixed population; it does not turn independent providers into one feed.

`ml.intraday.recovered_population` supplies three pure functions:

- `wrap_primary(candle)` validates the existing observed-source contract and
  embeds the complete original candle and its hash. OHLCV and clocks remain
  unchanged, and native source/dataset fields remain explicit.
- `recovery_candle(assessment, session)` prefers eligible native Schwab one-minute
  observations, then eligible native FMP five-minute observations. It aggregates
  one source only; it never expands coarse bars into artificial minutes. The
  original assessment, chosen source records, receipt/hash references, session
  bounds, and actual availability are retained. Missing or invalid evidence
  returns `None`.
- `verified_recovery_candle(candle)` reconstructs contract-owned fields from
  the frozen evidence and checks the full result, including timing, source,
  granularity, counts, values, and original primary evidence.

An alternate-provider candle consists of positive-volume genuine native bars
from a completed quarter and a successful response whose recorded request
covers that quarter. Exact duplicates do not double-count volume. Conflicting
native rows are rejected. Multiple equal-receipt revisions with conflicting
aggregates cannot win by enumeration order. Within the preferred provider,
the newest eligible receipt is selected and frozen.

`observed_constituents` counts actual native bars. For example, two genuine
five-minute bars remain two constituents with one missing native slot. Their
absence does not manufacture the missing five minutes. Native first/last bar
clocks and interval seconds remain available for later anchor validation.

If no observed recovery exists, only an eligible unchanged assessment from
`cross-provider-probable-no-trade-v1` can produce a carry estimate. That policy
requires successful Databento, Schwab and FMP completed-interval responses,
an observed recent prior anchor, and no contradictory observed activity or
fresh price context. Missing optional brackets remain disclosed limitations;
they do not cause a wait for a later bar. Provider independence or
complete-market coverage is not established. The result is marked
`synthetic=true`, `no_trade=false`, and
`observation_kind=corroborated_price_carry_estimate`. It is an explicit estimate,
never a verified market-wide absence of trading or a native observation.

The estimate has zero observed constituents, one derived quarter constituent,
zero volume, and availability no earlier than its retained proof. A missing
or contradictory response leaves it unavailable. The selected synthetic-data
policy still controls training, inference and evaluation admission; this new
contract does not rewrite older forecasts' frozen policies.

The pure validator checks retained structural evidence. The acquisition owner
must retain and verify the referenced raw-response objects; a hash reference
alone is not a claim that a provider covered all markets. Successful recovery
does not itself start a trader, place an order, or establish model quality.

`save_recovery_candle(datastore_root, candle)` appends only alternate-provider
and derived candles as immutable SHA-256 JSON objects under
`intraday/gap-recovery/candles/`. A separate small `candles.sqlite3` catalog indexes
symbol, quarter open and actual availability. Primary observations remain in
their existing source store. Older objects and catalog entries are preserved.

`load_recovery_candles(..., start=..., end=..., cutoff=...)` uses read-only indexed
queries, verifies each selected object's digest and candle contract, and returns
the newest eligible revision per quarter. Supply `cutoff` for a historical
replay so a later revision cannot hide earlier eligible evidence. Equal-time
conflicting revisions are an explicit error. Bounds are 100,000 catalog rows
per symbol/request, 4 MiB per object, and 128 MiB cumulative object reads.
Missing catalogs return an empty list without creating files.

The history reader's `recovery_cutoff` is independent of its `end` bar-range
bound. It selects catalog revisions as of that explicit information time;
without a supplied cutoff it freezes the current time once at load entry.
Supervision forwards its frozen training cutoff, and the runtime CLI forwards
its captured history-load time. Each locally owned manual entrypoint must pass
its explicit population and captured load cutoff; the shared helper does not
replace divergent execution entrypoints. Atlas's local integration includes
that hook. An older eligible revision remains usable even if a newer revision
was received after the chosen cutoff.

Offline regression tests:

```powershell
python -B -m pytest -q tests/test_intraday_recovered_population.py tests/test_market_gap_evidence.py
```
