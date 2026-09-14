# COST observation-source feasibility

Initial offline audit at 2026-09-13 04:28 UTC; followed by an authorized, bounded XNAS.BASIC feasibility probe completed at 04:31 UTC. Three exact COST minute requests passed zero-dollar and capacity preflights. No license changes, live requests, broker calls, production edits, or source substitutions were made.

**New XNAS.BASIC diagnostic data supplies observations within one minute at every probed missing boundary.** It is a concrete prospective candidate; it does not retroactively repair the seven missing September 11 outcomes in frozen XNAS.ITCH publications. COST has the same symbol contracts as the other stocks, but symbols can have different trading density on the selected venue. XNAS.ITCH represents trading on Nasdaq, not every trade in Nasdaq-listed stocks. Databento's documentation describes it as Nasdaq's exchange feed; Nasdaq confirms TotalView shows securities trading on Nasdaq. [Databento XNAS.ITCH](https://databento.com/docs/venues-and-datasets/xnas-itch), [Nasdaq TotalView](https://www.nasdaq.com/products/data/equities/nasdaq-totalview).

## New candidate probe

XNAS.BASIC includes Nasdaq, PSX, Texas, and FINRA/Nasdaq TRF trades; it is broader than XNAS.ITCH but is not every US venue. Its quotes remain Nasdaq-only. [Official candidate specification](https://databento.com/docs/venues-and-datasets/xnas-basic).

The precise requests were September 11 04:30–05:15 and 13:30–17:00 Pacific, and September 10 16:30–17:00. All three returned native `XNAS.BASIC`, `ohlcv-1m`, COST metadata matching exact requested ranges, with no partial/not-found mappings. They yielded 192 bars (35 + 140 + 17), all consolidated publisher 93. Exact cost was $0 for each request; total estimated billable bytes were 10,752. Stored native DBN was independently decoded again, reproduced each saved parquet exactly, matched preflight record counts, and passed every receipt checksum.

| Required boundary, Pacific | Native candidate close | Completed observation | Age |
| --- | ---: | --- | ---: |
| Sep 11 05:00 | 905.0000 | 05:00 | 0 minutes |
| Sep 11 14:00 | 904.8200 | 14:00 | 0 minutes |
| Sep 11 15:00 | 904.8117 | 14:59 | 1 minute |
| Sep 11 16:00 | 904.8000 | 16:00 | 0 minutes |
| Sep 11 17:00 | 904.9808 | 17:00 | 0 minutes |
| Sep 10 17:00 | 902.2000 | 17:00 | 0 minutes |

The first opening observations at/after September 11 14:00, 15:00 and 16:00 occur at 14:01, 15:00 and 16:00 respectively, also within the five-minute rule. These are source-produced bars; no synthetic observations were generated. Some native candidate rows have zero volume (43 of 192), including the 15:00 opening bar, so a prospective implementation must retain original provider values and validate their source semantics before interpreting volumes. All six closing rows above have a positive native volume; no zero-volume observation is required for that result.

Evidence: [candidate report](C:/dev/ducketz/artifacts/analysis/cost-recurrence-20260913/source-probe/report.json), [independent readback verification](C:/dev/ducketz/artifacts/analysis/cost-recurrence-20260913/source-probe/verification.json), [bounded acquisition script](C:/dev/ducketz/artifacts/analysis/cost-recurrence-20260913/probe_xnas_basic.py). Each window directory retains the raw DBN, exact request/preflight, native metadata, parquet, and checksum-bound receipt.

## Retained evidence

| Required boundary, Pacific | Existing EQUS.MINI minute bars | Existing Schwab minute evidence |
| --- | --- | --- |
| Sep 11 05:00 close | No earlier same-day candle | No Sep 11 minute history retained in inspected canonical/probe files |
| Sep 11 14:00 | Latest close 13:00; 60 minutes old | Same absence |
| Sep 11 15:00 | Latest close 13:00; 120 minutes old | Same absence |
| Sep 11 16:00 | Latest close 13:00; 180 minutes old | Same absence |
| Sep 10 17:00 prior close | Canonical archive latest close 13:52; separate verified 16:30–17:00 probe completely empty | Saved probe latest close 16:51; 9 minutes old |

The EQUS.MINI raw archive identifies every row as `provider_dataset=EQUS.MINI`, `source_schema=ohlcv-1m`. September 9, 10, and 11 contain 381, 359, and 373 rows; last completed bars are 13:00, 13:52, and 13:00 Pacific. The canonical Schwab minute file ends September 4. The saved September 10 probe has 479 candles and does demonstrate some different observations, but its 17:00 boundary still fails the five-minute rule. This is not proof that a fresh Schwab history request would have no additional observations; no such request was made.

The retained latest Schwab quote raw file has a September 11 16:59:38.838 Pacific trade timestamp (price 904.9808), fetched at September 11 21:21 Pacific. This is a current end-of-session snapshot, not a September 10 quote or a history of the missing September 11 hourly clocks; it cannot repair those outcomes. Older normalized snapshots retain quote timestamps but lack the original separate trade timestamp and do not establish a last-trade boundary by themselves.

Reproducible local evidence:

- [Offline script](C:/dev/ducketz/artifacts/analysis/cost-recurrence-20260913/audit_source_options.py)
- [Input paths, SHA-256 hashes, boundaries and ages](C:/dev/ducketz/artifacts/analysis/cost-recurrence-20260913/source-options-evidence.json)
- [Earlier source-verified probes](C:/dev/ducketz/artifacts/analysis/cost-closing-gap-20260911/verified-results.json)

## Prospective source decision

EQUS.MINI aggregates OHLCV over component venues and offers minute bars, but the documentation does not establish all-market trade coverage and the local evidence above shows it is not a demonstrated cure. [Databento EQUS.MINI specification](https://databento.com/docs/venues-and-datasets/equs-mini).

Databento's US Equities service also provides broader venue feeds, including off-exchange Nasdaq TRF trades through XNAS.BASIC. A prospective consolidated source is worth comparing, but must be proven at these exact premarket/afterhours boundaries before adopting it. EQUS.SUMMARY cannot replace minute observations: its schemas are daily OHLCV, statistics and definitions. [Databento US Equities introduction](https://databento.com/blog/introducing-databento-us-equities), [EQUS.SUMMARY schemas](https://databento.com/docs/venues-and-datasets/equs-summary).

The isolated seven-symbol 120-session XNAS.BASIC comparison is now complete. Across March 23–September 11, COST's missing native boundary checks fall from 547/3,120 with XNAS.ITCH to 48/3,120 with XNAS.BASIC when every zero-volume candidate record is excluded. Available mature entry windows rise from 1,569/2,275 to 2,199/2,275. The other six symbols have complete candidate endpoint coverage under that positive-volume sensitivity. Exact acquisition cost preflights were all $0 (43,772,232 total estimated billable bytes). All source deliveries were read back and checksum-verified. Provider metadata marks August 31 degraded for XNAS.BASIC; the separate conservative analysis excludes it and any target touching it. These findings demonstrate substantial prospective improvement, not complete future coverage or fitted-model qualification. [Full coverage review, per-period rates and all 19 routes](C:/dev/ducketz/artifacts/analysis/cost-recurrence-20260913/source-probe/validation-120/coverage-review.md).

Adoption requires a **new explicit prospective price-source policy**, its own manifests/history/model fits/qualification and future publications. Existing XNAS forecasts, actuals and their frozen source identities stay unchanged; no per-gap mixing with Schwab or EQUS.MINI. Quote-midpoint targets would be a different target definition requiring a separate policy and model assessment, not a replacement for missing trades. The approved short synthetic planning completion remains confined to planning and cannot create training labels or actual outcomes.
