# Storage estimate with verified account plans and a 2018 floor

Checked September 13, 2026 Pacific / September 14 UTC. Market data ends at the latest completed provider day, September 11 (exclusive request end September 12). This was an estimation exercise: Databento metadata calls and small FMP/Schwab/FRED availability probes, with no historical download jobs or publication into DATASTORE.

The four-stock addition is approximately **77.94 GiB / 83.69 GB**. If the shared CME cold archive is also expanded to its full included history for the currently configured instruments, the combined addition is approximately **285.99 GiB / 307.08 GB**. Databento quoted **$0 in additional historical download charges** for both requested scopes under the active subscriptions.

## Verified subscriptions and included history

The account pages confirm **Standard** for Databento US Equities, OPRA, and CME; FMP shows **Premium**, 750 calls/minute, and 50 GB bandwidth per rolling 30 days, with about 2.83 GB used when inspected.

| Provider/data | Included historical access verified in the account | Window used in this estimate |
| --- | --- | --- |
| Databento US equities L0 | 8+ years of OHLCV bars, definitions, statistics, status | Later of 2018-01-01, listing date, and schema availability |
| Databento OPRA L0 | 13+ years of OHLCV bars, definitions, statistics, status | Later of 2018-01-01, listing date, and schema availability |
| Databento CME L0 | 16+ years of OHLCV bars, definitions, statistics, status | 2018-01-01 or later schema/instrument availability |
| L1 quotes and individual trades | Last 12 months | 2025-09-14 onward, subject to schema availability |
| US equities/CME L2 and L3 | Last month | 2026-08-14 onward |
| US equity auction imbalance | **L3**, last month | 2026-08-14 onward |
| FMP Premium | Up to 30 years; full fundamentals/ratios, historical prices, intraday charts | Corporate periods dated 2018 onward and prices from the later listing boundary |

Primary account sources: [Databento subscriptions](https://databento.com/portal/live-data), [US equities plan](https://databento.com/portal/live-data/plans/us-equities), [OPRA plan](https://databento.com/portal/live-data/plans/opra), [CME plan](https://databento.com/portal/live-data/plans/cme), [FMP subscription](https://site.financialmodelingprep.com/developer/docs/dashboard?tab=subscription). The local [plan inventory](../../docs/databento-plan/databento_standard_plan_data_access.md) is consistent with these history windows.

The previous $965.83 quote included older L1/L2/L3 data beyond Standard's included windows. The earlier $5.24 in the approximate plan-window scenario came entirely from incorrectly classifying **imbalance as L0**. The actual account table identifies it as L3. Correcting that classification and applying current rolling windows produces zero-cost quotes. Long included history does not apply to every data type.

## Four stocks: additional storage

This matches the **25 stock/options archive schemas already used for the seven-symbol setup**: 14 XNAS.ITCH schemas and 11 OPRA.PILLAR schemas. Options requests expand each `SYMBOL.OPT` parent into its available contracts. The estimate retains compressed native DBN and normalized Parquet, plus SEC documents and an operating-data allowance.

| Symbol | Earliest stock-price date targeted | Databento stock + options GiB | SEC submission-size allowance GiB | FMP, Schwab, operating data and overhead allowance GiB | Planning total GiB | Planning total GB |
| --- | --- | ---: | ---: | ---: | ---: | ---: |
| CROX | 2018-01-01; first trading day January 2 | 10.398 | 0.457 | 0.200 | **11.054** | **11.870** |
| PATH | 2021-04-21 | 17.086 | 0.454 | 0.200 | **17.741** | **19.049** |
| TWST | 2018-10-31 | 1.791 | 0.502 | 0.200 | **2.492** | **2.676** |
| IONQ | 2021-10-01 | 45.891 | 0.563 | 0.200 | **46.654** | **50.094** |
| **Total** | | **75.165** | **1.976** | **0.800** | **77.941** | **83.689** |

Databento's XNAS.ITCH archive starts **2018-05-01**. FMP and Schwab both returned CROX daily prices beginning **2018-01-02**, covering the daily-price gap. Individual schemas can start later than the company: for example, OPRA status, TCBBO, and one-second CBBO have later provider availability. Each metadata request honors its actual schema start.

About **68.04 GiB** of the Databento total is options data. IONQ's options alone account for about **41.40 GiB**. Activity and number of contracts matter more than the company's age.

The per-symbol 0.2 GiB is a **planning allowance**, not a measured forecast of every derived file. It covers the current style of operating bars, small FMP/Schwab datasets, technical/signal output, manifests, and other metadata. Measured existing stock research folders were roughly 0.06–0.18 GiB. Large new feature matrices, embeddings, model checkpoints, and indefinite live-data growth require their own retention choices.

## FMP, Schwab, SEC, and macro checks

- **FMP:** All 76 small endpoint probes succeeded, covering the current corporate endpoint families, listing-bounded daily prices, and a five-minute chart sample. The selected responses totaled about **2.02 MB of JSON**. Statements were filtered to corporate periods dated 2018 onward. The five-minute check establishes access, not a complete eight-year intraday download estimate; the current FMP collector concentrates on corporate data. One-minute charts and earnings-call transcripts are listed under Ultimate and were not assumed included in Premium.
- **Schwab:** All 36 probes succeeded. They covered daily/weekly/monthly prices from the target boundary, the configured five recent intraday frequencies, and current option-chain snapshots. Responses totaled about **6.62 MB of JSON**. Current chains contained 390 CROX, 656 PATH, 286 TWST, and 986 IONQ contracts. The chain endpoint supplies current contracts and Greeks; its expiration-date filters do not provide historical as-of chain snapshots back to 2018. Those snapshots must be accumulated over time. Schwab returned one IONQ daily bar before the requested listing boundary; a later import should enforce the boundary after retrieval.
- **SEC:** The existing submission index contains **3,536 filings dated 2018 onward**, totaling **2,121,599,787 bytes / 1.976 GiB** in SEC-reported full-submission sizes. This is a document allowance, not measured compressed storage. It includes eligible pre-IPO company/SPAC filings from 2018 onward, which are separate from stock-price history. Complete filings require paging beyond the current collector's recent-form scan. Filing attachments can contain earlier comparative financial information even when the filing date is after the cutoff.
- **FRED/ALFRED and FMP commodity context:** These are shared. Existing FRED current-series files already cover 2018 onward. Four one-row ALFRED metadata probes succeeded; their reported full 2018-onward result counts total only **947 vintage observations**. A **0.01 GiB shared planning allowance** is ample relative to the observed row sizes for filling this small macro history gap. The tables round this immaterial allowance out. Existing FMP commodity quote snapshots are reused; a current-quote endpoint cannot recreate snapshots from earlier dates.

This scope uses the existing provider families and archive schemas. Additional venues under the US equities subscription, every optional FMP endpoint, OPRA CMBP-1, and XNAS MBP-1 are outside this estimate. CMBP-1 and MBP-1 are currently deferred in the cold-archive policy.

## Shared CME: reuse versus expansion

DATASTORE already contains about **55.60 GiB** of shared CME material: **9.52 GiB** in the cold archive and **46.08 GiB** in runtime pools. Adding four stock symbols does not require duplicating this data. Its historical windows vary by schema; it is not already a complete 2018-onward archive at every included resolution.

For an optional expansion, the estimate uses the existing 13 cold-archive schemas and currently configured instruments:

- Continuous context: `ES.v.0`, `NQ.v.0`, `RTY.v.0`, `CL.v.0`, `GC.v.0`.
- Dated contracts: `ESU6`, `NQU6`, `CLV6`, `GCZ6`.

The dated contracts only have records during their own lifetimes. The current storage design keeps continuous and dated scopes separately, including their overlap; this estimate preserves that design.

| Included CME history expanded | Estimated additional GiB |
| --- | ---: |
| L0 from 2018 where available | 10.22 |
| L1 for the included year | 34.61 |
| L2 for the included month | 86.69 |
| L3 for the included month | 76.52 |
| **Total shared CME expansion** | **208.05 GiB / 223.39 GB** |

The desired cold-archive scope is estimated at **217.06 GiB**. About **9.01 GiB** of existing matching cold partitions can be reused, leaving **208.05 GiB added**. Existing other-contract files and runtime pools remain shared; their bytes are not added again. Current runtime collection uses three schemas, whereas this optional expansion sizes all 13 schemas already present in the cold archive.

All **26 grouped CME metadata requests quoted $0**. Most additional space is the included month of order-book data. The CME figure is one shared expansion across all stocks, not a per-stock multiplier.

## Method and interpretation

Databento record counts are converted to expected stored bytes using actual DBN-plus-Parquet compression observed in DATASTORE: COST for the four equities/options, and the matching CME schema/symbology group for CME. These are estimates, not upper bounds; different symbols, contracts, partition sizes, and compression can materially change the result. The SEC component uses reported full submission bytes; the other-data component is an explicit allowance.

Databento's [cost metadata endpoint](https://databento.com/docs/api-reference-historical/metadata/metadata-get-cost) applies flat-rate plan discounts. Its billable-size endpoint measures **uncompressed billing bytes**, which are different from compressed storage. The stock scope has about **214.66 GiB** of uncompressed billable data despite the $0 quote.

Final stored size is also different from ingestion workspace. Applying the repository's conservative `5 GiB + 2 × billable GiB` check to a single whole-scope batch gives about **434 GiB for the stocks**, or **1,752 GiB for stocks plus the optional CME expansion**. Those are capacity checks, not expected lasting disk growth; staged processing requires a separate peak-space assessment before downloading.

Zero additional historical charges assumes the current subscriptions stay active and requests remain inside included rolling windows. FMP requests remain subject to the existing bandwidth allowance. There were no subscription changes, no payment actions, and no bulk Databento download jobs.

Audit files:

- [Four-stock metadata preflight](stock-research-plan-aware-2018-preflight-20260913.json)
- [FMP, Schwab, and SEC availability/size inventory](stock-research-other-providers-2018-20260913.json)
- [Optional shared CME preflight](stock-research-shared-cme-2018-preflight-20260913.json)
- [FRED/ALFRED count preflight](stock-research-fred-2018-preflight-20260913.json)

One GiB is 1,073,741,824 bytes; one GB is 1,000,000,000 bytes. Totals exclude immaterial rounding and the optional 0.01 GiB macro allowance.
