# Databento Standard Plan — Data Access Inventory

Compiled from the Databento plan screenshots provided on **2026-08-15**.  
This document records the **Standard** plan entitlements shown in those screenshots for:

1. **OPRA**
2. **CME Globex MDP 3.0**
3. **Databento US Equities**

> **Note:** The tables below preserve the history windows and availability shown in the screenshots. “Pay as you go for more history” means older/deeper historical data may still be fetchable beyond the included Standard-plan window, subject to usage-based pricing.

---

## Quick summary

| Dataset                   | Coverage                           | Symbols shown                             | Included L0 history | Included L1 history | Included L2/L3 history | Live data                            |
| ------------------------- | ---------------------------------- | -----------------------------------------:| -------------------:| -------------------:| ----------------------:| ------------------------------------ |
| **OPRA**                  | 18 exchanges                       | 1,600,000+                                | 13+ years           | Last 12 months      | Not listed             | Yes — listed L0/L1 schemas           |
| **CME Globex MDP 3.0**    | CME, CBOT, NYMEX, COMEX            | 650,000+                                  | 16+ years           | Last 12 months      | Last 1 month           | Yes — L0/L1; L2/L3 not shown as live |
| **Databento US Equities** | 45 venues = 15 exchanges + 30 ATSs | 24,000+ historical; 20,000+ live/overview | 8+ years            | Last 12 months      | Last 1 month           | Yes — selected L0/L1 schemas         |

---

## How to read the schema explanations

Each explanation uses `N × C columns`: `N` is the number of records returned for the selected symbols and time range, while `C` is the number of normalized Databento record fields that would become Parquet columns. A row count is therefore data-dependent; the field count is per schema, and an optional symbol mapping or file metadata column is not included. For a family such as OHLCV or BBO, the count applies separately to each requested interval/schema ID.

## Cold-start storage-sizing scenario

The two added sizing columns use the requested bootstrap horizon: 5 days of `ohlcv-1s`, 100 days of `ohlcv-1m`, 2,000 days of `ohlcv-1h`, 5,000 days of `ohlcv-1d` and `definition`, and one calendar month of every other available schema. For CME and US Equities, the one-month non-L0 window is a planning assumption so that the three datasets are comparable; it is **not** an assertion that only one month is included in the subscription.

`rows_M` means returned rows ÷ 1,000,000. The GiB values are conservative **free-space capacity** factors for retaining both provider DBN and normalized Parquet, calculated as `2 × DBN record size × rows / 2^30`; the current OPRA sync uses this same 2× expansion factor and separately requires one 5 GiB safety reserve per bootstrap batch. Actual files are usually smaller because the DBN is Zstandard-compressed and Parquet compresses repeated values, but no fixed per-symbol GiB exists for event data.

For the bounded OHLCV portion alone, a continuously trading single CME series with 23-hour sessions is at most about **0.063 GiB** across all four requested windows, and an always-active US-equity ticker is at most about **0.018 GiB**. An OPRA parent symbol such as `AAPL.OPT` expands to its option chain, so its storage is based on the returned child-contract rows rather than the parent-symbol count; all event schemas must be preflighted with Databento metadata before committing disk space.

> **Implementation note:** these columns use the requested one-month sizing scenario for the remaining OPRA schemas. The current `datafetching.options_history` implementation instead bootstraps those schemas with **six months**, so its actual event-row requirement is approximately six times the one-month row-based estimate below.

> **Exact preflight:** for a chosen direct symbol or parent scope, request Databento's [`get_billable_size`](https://databento.com/docs/api-reference-historical/metadata/get-billable-size) and [`get_record_count`](https://databento.com/docs/api-reference-historical/metadata/get-record-count) for every table row, then reserve `5 GiB + 2 × sum(billable GiB)`. This is the same capacity rule used by the OPRA history sync; do not replace the row-based estimates with a universal fixed GiB value.

# 1. OPRA — Standard

## Plan overview

- **Plan:** Standard
- **Price shown:** $199/month
- **Coverage:** 18 exchanges
- **Symbols:** 1,600,000+
- **Instrument scope shown:** Stock, ETF, and index options
- **Available history:** 13+ years
- **Standard includes:**
  - Live data
  - 13+ years of L0 history
  - 1 year of L1 history
  - Pay-as-you-go access for more history
- **Live-data license fees:** Screenshot shows **“No license fees”**

## Live data available

| Level | Schema / data type      | Live on Standard | Explanation | What would be fetched | How much in GiB |
| ----- | ----------------------- | ---------------- | ----------- | --------------------- | --------------- |
| L0    | OHLCV-1s / 1m / 1h / 1d | Yes              | This produces `N × 9 columns`: one row per instrument per non-empty 1-second, 1-minute, 1-hour, or 1-day trade aggregate. Columns are `ts_event`, `rtype`, `publisher_id`, `instrument_id`, `open`, `high`, `low`, `close`, and `volume`. | Historical bootstrap: 5 days `1s`, 100 days `1m`, 2,000 days `1h`, and 5,000 days `1d`; only non-empty bars. | `0.1043 × OHLCV rows_M`; no fixed OPRA-parent cap. |
| L0    | Definitions             | Yes              | This produces `N × 73 columns`: one row per point-in-time instrument definition/update. Columns cover timestamps/IDs, `raw_symbol`, update action and instrument class, tick/display/price limits, expiration/activation, lots/volume/multipliers, venue/currency/underlying/strike/maturity fields, and spread-leg metadata. | Historical bootstrap: point-in-time definitions over the prior 5,000 calendar days. | `0.9686 × definition rows_M`. |
| L0    | Statistics              | Yes              | This produces `N × 14 columns`: one row per published instrument statistic. Columns include timestamps/IDs, `ts_ref`, `price`, `quantity`, `sequence`, `ts_in_delta`, `stat_type`, `channel_id`, `update_action`, and `stat_flags`; `stat_type` identifies metrics such as opening, settlement, high/low, volume, open interest, VWAP, and auction values. | Historical bootstrap: every statistics event in the prior calendar month. | `0.1490 × statistics rows_M`. |
| L0    | Status                  | Yes              | This produces `N × 11 columns`: one row per trading-status change. Columns are timestamps/IDs, `action`, `reason`, `trading_event`, `is_trading`, `is_quoting`, and `is_short_sell_restricted`. | Historical bootstrap: every status change in the prior calendar month. | `0.0745 × status rows_M`. |
| L1    | CMBP-1                  | Yes              | This produces `N × 17 columns`: one row per consolidated top-of-book update, including trades and changes to displayed depth. Columns are timestamps/IDs, `action`, `side`, `price`, `size`, `flags`, `ts_in_delta`, top bid/ask prices and sizes, and `bid_pb_00`/`ask_pb_00` venue IDs. | Historical bootstrap: every CMBP-1 event in the prior calendar month. | `0.1490 × CMBP-1 rows_M`. |
| L1    | TCBBO                   | Yes              | This produces `N × 17 columns`: one row per consolidated trade with the consolidated BBO immediately before that trade. Columns are timestamps/IDs, trade `action`/`side`/`price`/`size`, `flags`, `ts_in_delta`, top bid/ask prices and sizes, and the bid/ask venue IDs. | Historical bootstrap: every TCBBO event in the prior calendar month. | `0.1490 × TCBBO rows_M`. |
| L1    | CBBO                    | Yes              | This produces `N × 15 columns`: one row per 1-second or 1-minute interval containing a trade or CBBO update. Columns are interval/trade timestamps, IDs, last-trade `side`/`price`/`size`, `flags`, top bid/ask prices and sizes, and the bid/ask venue IDs. | Historical bootstrap: every `cbbo-1s` and `cbbo-1m` record in the prior calendar month. | `0.1490 × combined CBBO rows_M`. |
| L1    | Trades                  | Yes              | This produces `N × 13 columns`: one row per trade event, or “time and sales” record. Columns are timestamps/IDs, `action`, `side`, `depth`, `price`, `size`, `flags`, `ts_in_delta`, and `sequence`. | Historical bootstrap: every trade in the prior calendar month. | `0.0894 × trades rows_M`. |

## Historical data included

| Level | Schema / data type      | Standard history shown | Explanation | What would be fetched | How much in GiB |
| ----- | ----------------------- | ----------------------:| ----------- | --------------------- | --------------- |
| L0    | OHLCV-1s / 1m / 1h / 1d | 13+ years              | This produces `N × 9 columns`: one row per instrument per non-empty 1-second, 1-minute, 1-hour, or 1-day trade aggregate. Columns are `ts_event`, `rtype`, `publisher_id`, `instrument_id`, `open`, `high`, `low`, `close`, and `volume`. | 5 days `1s`, 100 days `1m`, 2,000 days `1h`, and 5,000 days `1d`; only non-empty bars. | `0.1043 × OHLCV rows_M`; no fixed OPRA-parent cap. |
| L0    | Definitions             | 13+ years              | This produces `N × 73 columns`: one row per point-in-time instrument definition/update. Columns cover timestamps/IDs, `raw_symbol`, update action and instrument class, tick/display/price limits, expiration/activation, lots/volume/multipliers, venue/currency/underlying/strike/maturity fields, and spread-leg metadata. | Definitions over the prior 5,000 calendar days. | `0.9686 × definition rows_M`. |
| L0    | Statistics              | 13+ years              | This produces `N × 14 columns`: one row per published instrument statistic. Columns include timestamps/IDs, `ts_ref`, `price`, `quantity`, `sequence`, `ts_in_delta`, `stat_type`, `channel_id`, `update_action`, and `stat_flags`; `stat_type` identifies metrics such as opening, settlement, high/low, volume, open interest, VWAP, and auction values. | Every statistics event in the prior calendar month. | `0.1490 × statistics rows_M`. |
| L0    | Status                  | 13+ years              | This produces `N × 11 columns`: one row per trading-status change. Columns are timestamps/IDs, `action`, `reason`, `trading_event`, `is_trading`, `is_quoting`, and `is_short_sell_restricted`. | Every status change in the prior calendar month. | `0.0745 × status rows_M`. |
| L1    | CMBP-1                  | Last 12 months         | This produces `N × 17 columns`: one row per consolidated top-of-book update, including trades and changes to displayed depth. Columns are timestamps/IDs, `action`, `side`, `price`, `size`, `flags`, `ts_in_delta`, top bid/ask prices and sizes, and `bid_pb_00`/`ask_pb_00` venue IDs. | Every CMBP-1 event in the prior calendar month. | `0.1490 × CMBP-1 rows_M`. |
| L1    | TCBBO                   | Last 12 months         | This produces `N × 17 columns`: one row per consolidated trade with the consolidated BBO immediately before that trade. Columns are timestamps/IDs, trade `action`/`side`/`price`/`size`, `flags`, `ts_in_delta`, top bid/ask prices and sizes, and the bid/ask venue IDs. | Every TCBBO event in the prior calendar month. | `0.1490 × TCBBO rows_M`. |
| L1    | CBBO                    | Last 12 months         | This produces `N × 15 columns`: one row per 1-second or 1-minute interval containing a trade or CBBO update. Columns are interval/trade timestamps, IDs, last-trade `side`/`price`/`size`, `flags`, top bid/ask prices and sizes, and the bid/ask venue IDs. | Every `cbbo-1s` and `cbbo-1m` record in the prior calendar month. | `0.1490 × combined CBBO rows_M`. |
| L1    | Trades                  | Last 12 months         | This produces `N × 13 columns`: one row per trade event, or “time and sales” record. Columns are timestamps/IDs, `action`, `side`, `depth`, `price`, `size`, `flags`, `ts_in_delta`, and `sequence`. | Every trade in the prior calendar month. | `0.0894 × trades rows_M`. |

### OPRA takeaway

Your Standard OPRA subscription gives you the full listed **L0 history for 13+ years**, the listed **L1 history for the most recent 12 months**, and live access to all of the OPRA schemas shown above. The plan page also says you can **pay as you go for more history**.

---

# 2. CME Globex MDP 3.0 — Standard

## Plan overview

- **Plan:** Standard
- **Price shown:** $199/month
- **Coverage:** CME, CBOT, NYMEX, COMEX
- **Symbols:** 650,000+
- **Instrument scope shown:** Futures, options, and spreads
- **Available history:** 16+ years
- **Standard includes:**
  - Live data
  - 16+ years of L0 history
  - 1 year of L1 history
  - 1 month of L2 and L3 history
  - Pay-as-you-go access for more history
- **Live-data license fees:** Screenshot shows **“No license fees”**

## Live data available

| Level | Schema / data type      | Live on Standard | Explanation | What would be fetched | How much in GiB |
| ----- | ----------------------- | ---------------- | ----------- | --------------------- | --------------- |
| L0    | OHLCV-1s / 1m / 1h / 1d | Yes              | This produces `N × 9 columns`: one row per instrument per non-empty 1-second, 1-minute, 1-hour, or 1-day trade aggregate. Columns are `ts_event`, `rtype`, `publisher_id`, `instrument_id`, `open`, `high`, `low`, `close`, and `volume`. | Historical bootstrap: 5 days `1s`, 100 days `1m`, 2,000 days `1h`, and 5,000 days `1d`; only non-empty bars. | `0.1043 × OHLCV rows_M`; ≤`0.063` for a 23-hour-session CME series. |
| L0    | Definitions             | Yes              | This produces `N × 73 columns`: one row per point-in-time instrument definition/update. Columns cover timestamps/IDs, `raw_symbol`, update action and instrument class, tick/display/price limits, expiration/activation, lots/volume/multipliers, venue/currency/underlying/strike/maturity fields, and spread-leg metadata. | Historical bootstrap: point-in-time definitions over the prior 5,000 calendar days. | `0.9686 × definition rows_M`. |
| L0    | Statistics              | Yes              | This produces `N × 14 columns`: one row per published instrument statistic. Columns include timestamps/IDs, `ts_ref`, `price`, `quantity`, `sequence`, `ts_in_delta`, `stat_type`, `channel_id`, `update_action`, and `stat_flags`; `stat_type` identifies metrics such as opening, settlement, high/low, volume, open interest, VWAP, and auction values. | Historical bootstrap: every statistics event in the prior calendar month. | `0.1490 × statistics rows_M`. |
| L0    | Status                  | Yes              | This produces `N × 11 columns`: one row per trading-status change. Columns are timestamps/IDs, `action`, `reason`, `trading_event`, `is_trading`, `is_quoting`, and `is_short_sell_restricted`. | Historical bootstrap: every status change in the prior calendar month. | `0.0745 × status rows_M`. |
| L1    | MBP-1                   | Yes              | This produces `N × 17 columns`: one row per venue top-of-book update, including trades and changes to displayed depth. Columns are timestamps/IDs, `action`, `side`, `depth`, `price`, `size`, `flags`, `ts_in_delta`, `sequence`, and top bid/ask prices, sizes, and order counts. | Historical bootstrap: every MBP-1 event in the prior calendar month. | `0.1490 × MBP-1 rows_M`. |
| L1    | TBBO                    | Yes              | This produces `N × 17 columns`: one row per venue trade with the BBO immediately before that trade. Columns are timestamps/IDs, trade `action`/`side`/`depth`/`price`/`size`, `flags`, `ts_in_delta`, `sequence`, and top bid/ask prices, sizes, and order counts. | Historical bootstrap: every TBBO event in the prior calendar month. | `0.1490 × TBBO rows_M`. |
| L1    | BBO                     | Yes              | This produces `N × 16 columns`: one row per 1-second or 1-minute interval containing a trade or BBO update. Columns are interval/trade timestamps, IDs, last-trade `side`/`price`/`size`, `flags`, `sequence`, and top bid/ask prices, sizes, and order counts. | Historical bootstrap: every `bbo-1s` and `bbo-1m` record in the prior calendar month. | `0.1490 × combined BBO rows_M`. |
| L1    | Trades                  | Yes              | This produces `N × 13 columns`: one row per trade event, or “time and sales” record. Columns are timestamps/IDs, `action`, `side`, `depth`, `price`, `size`, `flags`, `ts_in_delta`, and `sequence`. | Historical bootstrap: every trade in the prior calendar month. | `0.0894 × trades rows_M`. |
| L2    | MBP-10                  | **No**           | This produces `N × 53 columns`: one row per order-book event across the top 10 price levels, including trades and aggregate depth changes. Columns are the 13 MBP event fields plus `bid_px_N`, `ask_px_N`, `bid_sz_N`, and `ask_sz_N` for levels `N=00…09` (40 depth columns). | Not live; historical bootstrap fetches every MBP-10 event in the prior calendar month. | `0.6855 × MBP-10 rows_M`. |
| L3    | MBO                     | **No**           | This produces `N × 14 columns`: one row per individual order-book event across all price levels. Columns are timestamps/IDs, `action`, `side`, `price`, `size`, `channel_id`, `order_id`, `flags`, `ts_in_delta`, and `sequence`, covering adds, cancels, modifies, fills, trades, and book clears. | Not live; historical bootstrap fetches every MBO event in the prior calendar month. | `0.1043 × MBO rows_M`. |

## Historical data included

| Level | Schema / data type      | Standard history shown | Explanation | What would be fetched | How much in GiB |
| ----- | ----------------------- | ----------------------:| ----------- | --------------------- | --------------- |
| L0    | OHLCV-1s / 1m / 1h / 1d | 16+ years              | This produces `N × 9 columns`: one row per instrument per non-empty 1-second, 1-minute, 1-hour, or 1-day trade aggregate. Columns are `ts_event`, `rtype`, `publisher_id`, `instrument_id`, `open`, `high`, `low`, `close`, and `volume`. | 5 days `1s`, 100 days `1m`, 2,000 days `1h`, and 5,000 days `1d`; only non-empty bars. | `0.1043 × OHLCV rows_M`; ≤`0.063` for a 23-hour-session CME series. |
| L0    | Definitions             | 16+ years              | This produces `N × 73 columns`: one row per point-in-time instrument definition/update. Columns cover timestamps/IDs, `raw_symbol`, update action and instrument class, tick/display/price limits, expiration/activation, lots/volume/multipliers, venue/currency/underlying/strike/maturity fields, and spread-leg metadata. | Definitions over the prior 5,000 calendar days. | `0.9686 × definition rows_M`. |
| L0    | Statistics              | 16+ years              | This produces `N × 14 columns`: one row per published instrument statistic. Columns include timestamps/IDs, `ts_ref`, `price`, `quantity`, `sequence`, `ts_in_delta`, `stat_type`, `channel_id`, `update_action`, and `stat_flags`; `stat_type` identifies metrics such as opening, settlement, high/low, volume, open interest, VWAP, and auction values. | Every statistics event in the prior calendar month. | `0.1490 × statistics rows_M`. |
| L0    | Status                  | 16+ years              | This produces `N × 11 columns`: one row per trading-status change. Columns are timestamps/IDs, `action`, `reason`, `trading_event`, `is_trading`, `is_quoting`, and `is_short_sell_restricted`. | Every status change in the prior calendar month. | `0.0745 × status rows_M`. |
| L1    | MBP-1                   | Last 12 months         | This produces `N × 17 columns`: one row per venue top-of-book update, including trades and changes to displayed depth. Columns are timestamps/IDs, `action`, `side`, `depth`, `price`, `size`, `flags`, `ts_in_delta`, `sequence`, and top bid/ask prices, sizes, and order counts. | Every MBP-1 event in the prior calendar month. | `0.1490 × MBP-1 rows_M`. |
| L1    | TBBO                    | Last 12 months         | This produces `N × 17 columns`: one row per venue trade with the BBO immediately before that trade. Columns are timestamps/IDs, trade `action`/`side`/`depth`/`price`/`size`, `flags`, `ts_in_delta`, `sequence`, and top bid/ask prices, sizes, and order counts. | Every TBBO event in the prior calendar month. | `0.1490 × TBBO rows_M`. |
| L1    | BBO                     | Last 12 months         | This produces `N × 16 columns`: one row per 1-second or 1-minute interval containing a trade or BBO update. Columns are interval/trade timestamps, IDs, last-trade `side`/`price`/`size`, `flags`, `sequence`, and top bid/ask prices, sizes, and order counts. | Every `bbo-1s` and `bbo-1m` record in the prior calendar month. | `0.1490 × combined BBO rows_M`. |
| L1    | Trades                  | Last 12 months         | This produces `N × 13 columns`: one row per trade event, or “time and sales” record. Columns are timestamps/IDs, `action`, `side`, `depth`, `price`, `size`, `flags`, `ts_in_delta`, and `sequence`. | Every trade in the prior calendar month. | `0.0894 × trades rows_M`. |
| L2    | MBP-10                  | Last 1 month           | This produces `N × 53 columns`: one row per order-book event across the top 10 price levels, including trades and aggregate depth changes. Columns are the 13 MBP event fields plus `bid_px_N`, `ask_px_N`, `bid_sz_N`, and `ask_sz_N` for levels `N=00…09` (40 depth columns). | Every MBP-10 event in the prior calendar month. | `0.6855 × MBP-10 rows_M`. |
| L3    | MBO                     | Last 1 month           | This produces `N × 14 columns`: one row per individual order-book event across all price levels. Columns are timestamps/IDs, `action`, `side`, `price`, `size`, `channel_id`, `order_id`, `flags`, `ts_in_delta`, and `sequence`, covering adds, cancels, modifies, fills, trades, and book clears. | Every MBO event in the prior calendar month. | `0.1043 × MBO rows_M`. |

## Other availability shown

- **Delayed data:** Included on Standard

### CME takeaway

Your Standard CME subscription gives you **16+ years of the listed L0 data**, **12 months of L1**, and **1 month of L2/L3 historical data**. Live access is shown for the listed L0 and L1 schemas, while **MBP-10 and MBO are not shown as live entitlements**. More historical depth can be obtained on a pay-as-you-go basis.

---

# 3. Databento US Equities — Standard

## Plan overview

- **Plan:** Standard
- **Price shown:** $199/month
- **Coverage shown in overview:** 15 exchanges and 30 ATSs
- **Historical table coverage:** 45 venues
- **Instrument scope shown:** All stocks and ETFs
- **Symbols shown in overview/live:** 20,000+
- **Symbols shown in historical table:** 24,000+
- **Available history:** 8+ years
- **Standard includes:**
  - Live data
  - 8+ years of L0 history
  - 1 year of L1 history
  - 1 month of L2 and L3 history
  - Pay-as-you-go access for more history
- **Live-data license fees:** Screenshot shows **“No license fees”**

## Live data available

Live coverage is labeled **Databento US Equities Mini** in the screenshot.

| Level | Schema / data type      | Live on Standard | Explanation | What would be fetched | How much in GiB |
| ----- | ----------------------- | ---------------- | ----------- | --------------------- | --------------- |
| —     | Full market summary     | Yes              | This is a bundle rather than one normalized schema: consolidated US-equity summary records combine daily OHLCV, statistics, and definitions. In Parquet, treat it as separate outputs—daily `ohlcv-1d` (`N × 9`), `statistics` (`N × 14`), and `definition` (`N × 73`)—rather than one rectangular table. | Reuse the requested daily OHLCV, 5,000-day definitions, and one-month statistics fetches; do not request a duplicate summary copy. | `0` incremental if those component files are reused. |
| L0    | OHLCV-1s / 1m / 1h / 1d | Yes              | This produces `N × 9 columns`: one row per instrument per non-empty 1-second, 1-minute, 1-hour, or 1-day trade aggregate. Columns are `ts_event`, `rtype`, `publisher_id`, `instrument_id`, `open`, `high`, `low`, `close`, and `volume`. | Historical bootstrap: 5 days `1s`, 100 days `1m`, 2,000 days `1h`, and 5,000 days `1d`; only non-empty bars. | `0.1043 × OHLCV rows_M`; ≤`0.018` for all four windows of one always-active ticker. |
| L0    | Definitions             | Yes              | This produces `N × 73 columns`: one row per point-in-time instrument definition/update. Columns cover timestamps/IDs, `raw_symbol`, update action and instrument class, tick/display/price limits, expiration/activation, lots/volume/multipliers, venue/currency/underlying/strike/maturity fields, and spread-leg metadata. | Historical bootstrap: point-in-time definitions over the prior 5,000 calendar days. | `0.9686 × definition rows_M`. |
| L0    | Statistics              | Yes              | This produces `N × 14 columns`: one row per published instrument statistic. Columns include timestamps/IDs, `ts_ref`, `price`, `quantity`, `sequence`, `ts_in_delta`, `stat_type`, `channel_id`, `update_action`, and `stat_flags`; `stat_type` identifies metrics such as opening, settlement, high/low, volume, open interest, VWAP, and auction values. | Historical bootstrap: every statistics event in the prior calendar month. | `0.1490 × statistics rows_M`. |
| L0    | Status                  | **No**           | If available, this would produce `N × 11 columns`: one row per trading-status change. Columns are timestamps/IDs, `action`, `reason`, `trading_event`, `is_trading`, `is_quoting`, and `is_short_sell_restricted`; it is not shown as a live Standard entitlement here. | Not live; historical bootstrap fetches every status change in the prior calendar month. | `0.0745 × status rows_M`. |
| L1    | MBP-1                   | Yes              | This produces `N × 17 columns`: one row per venue top-of-book update, including trades and changes to displayed depth. Columns are timestamps/IDs, `action`, `side`, `depth`, `price`, `size`, `flags`, `ts_in_delta`, `sequence`, and top bid/ask prices, sizes, and order counts. | Historical bootstrap: every MBP-1 event in the prior calendar month. | `0.1490 × MBP-1 rows_M`. |
| L1    | TBBO                    | Yes              | This produces `N × 17 columns`: one row per venue trade with the BBO immediately before that trade. Columns are timestamps/IDs, trade `action`/`side`/`depth`/`price`/`size`, `flags`, `ts_in_delta`, `sequence`, and top bid/ask prices, sizes, and order counts. | Historical bootstrap: every TBBO event in the prior calendar month. | `0.1490 × TBBO rows_M`. |
| L1    | BBO                     | Yes              | This produces `N × 16 columns`: one row per 1-second or 1-minute interval containing a trade or BBO update. Columns are interval/trade timestamps, IDs, last-trade `side`/`price`/`size`, `flags`, `sequence`, and top bid/ask prices, sizes, and order counts. | Historical bootstrap: every `bbo-1s` and `bbo-1m` record in the prior calendar month. | `0.1490 × combined BBO rows_M`. |
| L1    | Trades                  | Yes              | This produces `N × 13 columns`: one row per trade event, or “time and sales” record. Columns are timestamps/IDs, `action`, `side`, `depth`, `price`, `size`, `flags`, `ts_in_delta`, and `sequence`. | Historical bootstrap: every trade in the prior calendar month. | `0.0894 × trades rows_M`. |
| L2    | MBP-10                  | **No**           | If available, this would produce `N × 53 columns`: one row per order-book event across the top 10 price levels, including trades and aggregate depth changes. Columns are the 13 MBP event fields plus `bid_px_N`, `ask_px_N`, `bid_sz_N`, and `ask_sz_N` for levels `N=00…09` (40 depth columns); it is not shown as a live Standard entitlement here. | Not live; historical bootstrap fetches every MBP-10 event in the prior calendar month. | `0.6855 × MBP-10 rows_M`. |
| L3    | MBO                     | **No**           | If available, this would produce `N × 14 columns`: one row per individual order-book event across all price levels. Columns are timestamps/IDs, `action`, `side`, `price`, `size`, `channel_id`, `order_id`, `flags`, `ts_in_delta`, and `sequence`, covering adds, cancels, modifies, fills, trades, and book clears; it is not shown as a live Standard entitlement here. | Not live; historical bootstrap fetches every MBO event in the prior calendar month. | `0.1043 × MBO rows_M`. |
| L3    | Imbalance               | **No**           | If available, this would produce `N × 24 columns`: one row per auction-imbalance message. Columns include timestamps/IDs, reference and hypothetical clearing/collar prices, auction time/type/status, paired/total/market/unpaired quantities, sides, and venue-specific status flags; it is not shown as a live Standard entitlement here. | Not live; historical bootstrap fetches every imbalance message in the prior calendar month. | `0.2086 × imbalance rows_M`. |

## Historical data included

| Level | Schema / data type      | Standard history shown | Explanation | What would be fetched | How much in GiB |
| ----- | ----------------------- | ----------------------:| ----------- | --------------------- | --------------- |
| —     | Full market summary     | Yes                    | This is a bundle rather than one normalized schema: consolidated US-equity summary records combine daily OHLCV, statistics, and definitions. In Parquet, treat it as separate outputs—daily `ohlcv-1d` (`N × 9`), `statistics` (`N × 14`), and `definition` (`N × 73`)—rather than one rectangular table. | Reuse the requested daily OHLCV, 5,000-day definitions, and one-month statistics fetches; do not request a duplicate summary copy. | `0` incremental if those component files are reused. |
| L0    | OHLCV-1s / 1m / 1h / 1d | 8+ years               | This produces `N × 9 columns`: one row per instrument per non-empty 1-second, 1-minute, 1-hour, or 1-day trade aggregate. Columns are `ts_event`, `rtype`, `publisher_id`, `instrument_id`, `open`, `high`, `low`, `close`, and `volume`. | 5 days `1s`, 100 days `1m`, 2,000 days `1h`, and 5,000 days `1d`; only non-empty bars. | `0.1043 × OHLCV rows_M`; ≤`0.018` for all four windows of one always-active ticker. |
| L0    | Definitions             | 8+ years               | This produces `N × 73 columns`: one row per point-in-time instrument definition/update. Columns cover timestamps/IDs, `raw_symbol`, update action and instrument class, tick/display/price limits, expiration/activation, lots/volume/multipliers, venue/currency/underlying/strike/maturity fields, and spread-leg metadata. | Definitions over the prior 5,000 calendar days. | `0.9686 × definition rows_M`. |
| L0    | Statistics              | 8+ years               | This produces `N × 14 columns`: one row per published instrument statistic. Columns include timestamps/IDs, `ts_ref`, `price`, `quantity`, `sequence`, `ts_in_delta`, `stat_type`, `channel_id`, `update_action`, and `stat_flags`; `stat_type` identifies metrics such as opening, settlement, high/low, volume, open interest, VWAP, and auction values. | Every statistics event in the prior calendar month. | `0.1490 × statistics rows_M`. |
| L0    | Status                  | 8+ years               | This produces `N × 11 columns`: one row per trading-status change. Columns are timestamps/IDs, `action`, `reason`, `trading_event`, `is_trading`, `is_quoting`, and `is_short_sell_restricted`. | Every status change in the prior calendar month. | `0.0745 × status rows_M`. |
| L1    | MBP-1                   | Last 12 months         | This produces `N × 17 columns`: one row per venue top-of-book update, including trades and changes to displayed depth. Columns are timestamps/IDs, `action`, `side`, `depth`, `price`, `size`, `flags`, `ts_in_delta`, `sequence`, and top bid/ask prices, sizes, and order counts. | Every MBP-1 event in the prior calendar month. | `0.1490 × MBP-1 rows_M`. |
| L1    | TBBO                    | Last 12 months         | This produces `N × 17 columns`: one row per venue trade with the BBO immediately before that trade. Columns are timestamps/IDs, trade `action`/`side`/`depth`/`price`/`size`, `flags`, `ts_in_delta`, `sequence`, and top bid/ask prices, sizes, and order counts. | Every TBBO event in the prior calendar month. | `0.1490 × TBBO rows_M`. |
| L1    | BBO                     | Last 12 months         | This produces `N × 16 columns`: one row per 1-second or 1-minute interval containing a trade or BBO update. Columns are interval/trade timestamps, IDs, last-trade `side`/`price`/`size`, `flags`, `sequence`, and top bid/ask prices, sizes, and order counts. | Every `bbo-1s` and `bbo-1m` record in the prior calendar month. | `0.1490 × combined BBO rows_M`. |
| L1    | Trades                  | Last 12 months         | This produces `N × 13 columns`: one row per trade event, or “time and sales” record. Columns are timestamps/IDs, `action`, `side`, `depth`, `price`, `size`, `flags`, `ts_in_delta`, and `sequence`. | Every trade in the prior calendar month. | `0.0894 × trades rows_M`. |
| L2    | MBP-10                  | Last 1 month           | This produces `N × 53 columns`: one row per order-book event across the top 10 price levels, including trades and aggregate depth changes. Columns are the 13 MBP event fields plus `bid_px_N`, `ask_px_N`, `bid_sz_N`, and `ask_sz_N` for levels `N=00…09` (40 depth columns). | Every MBP-10 event in the prior calendar month. | `0.6855 × MBP-10 rows_M`. |
| L3    | MBO                     | Last 1 month           | This produces `N × 14 columns`: one row per individual order-book event across all price levels. Columns are timestamps/IDs, `action`, `side`, `price`, `size`, `channel_id`, `order_id`, `flags`, `ts_in_delta`, and `sequence`, covering adds, cancels, modifies, fills, trades, and book clears. | Every MBO event in the prior calendar month. | `0.1043 × MBO rows_M`. |
| L3    | Imbalance               | Last 1 month           | This produces `N × 24 columns`: one row per auction-imbalance message. Columns include timestamps/IDs, reference and hypothetical clearing/collar prices, auction time/type/status, paired/total/market/unpaired quantities, sides, and venue-specific status flags. | Every imbalance message in the prior calendar month. | `0.2086 × imbalance rows_M`. |

## Other availability shown

- **Delayed data:** Included on Standard
- **Full market summary:** Included for both historical and live data

## Licensing shown for Standard

| Licensing capability   | Standard |
| ---------------------- | -------- |
| Instant approval       | Yes      |
| Personal use           | Yes      |
| Commercial use         | Yes      |
| Non-display use        | Yes      |
| Display use            | Yes      |
| Real-time distribution | No       |
| Delayed distribution   | No       |
| White-labeling         | No       |
| Dedicated connectivity | No       |

### US Equities takeaway

Your Standard US Equities subscription gives you **8+ years of the listed L0 data**, **12 months of L1**, and **1 month of L2/L3 historical data**. Live access includes OHLCV, Definitions, Statistics, MBP-1, TBBO, BBO, Trades, and the full market summary. **Status, MBP-10, MBO, and Imbalance are not shown as live Standard entitlements.**

---

# Combined fetch matrix

## Live

| Schema / data type      | OPRA | CME Globex | US Equities |
| ----------------------- |:----:|:----------:|:-----------:|
| OHLCV-1s / 1m / 1h / 1d | Yes  | Yes        | Yes         |
| Definitions             | Yes  | Yes        | Yes         |
| Statistics              | Yes  | Yes        | Yes         |
| Status                  | Yes  | Yes        | No          |
| MBP-1                   | —    | Yes        | Yes         |
| CMBP-1                  | Yes  | —          | —           |
| TBBO                    | —    | Yes        | Yes         |
| TCBBO                   | Yes  | —          | —           |
| BBO                     | —    | Yes        | Yes         |
| CBBO                    | Yes  | —          | —           |
| Trades                  | Yes  | Yes        | Yes         |
| MBP-10                  | —    | No         | No          |
| MBO                     | —    | No         | No          |
| Imbalance               | —    | —          | No          |
| Full market summary     | —    | —          | Yes         |

## Included historical depth

| Schema / data type      | OPRA      | CME Globex | US Equities |
| ----------------------- | ---------:| ----------:| -----------:|
| OHLCV-1s / 1m / 1h / 1d | 13+ years | 16+ years  | 8+ years    |
| Definitions             | 13+ years | 16+ years  | 8+ years    |
| Statistics              | 13+ years | 16+ years  | 8+ years    |
| Status                  | 13+ years | 16+ years  | 8+ years    |
| MBP-1                   | —         | 12 months  | 12 months   |
| CMBP-1                  | 12 months | —          | —           |
| TBBO                    | —         | 12 months  | 12 months   |
| TCBBO                   | 12 months | —          | —           |
| BBO                     | —         | 12 months  | 12 months   |
| CBBO                    | 12 months | —          | —           |
| Trades                  | 12 months | 12 months  | 12 months   |
| MBP-10                  | —         | 1 month    | 1 month     |
| MBO                     | —         | 1 month    | 1 month     |
| Imbalance               | —         | —          | 1 month     |
| Full market summary     | —         | —          | Included    |

---

# Practical interpretation

For building local datasets, the largest **included historical windows** visible in your Standard subscriptions are:

- **OPRA:** 13+ years at L0; 12 months at L1.
- **CME Globex:** 16+ years at L0; 12 months at L1; 1 month at L2/L3.
- **US Equities:** 8+ years at L0; 12 months at L1; 1 month at L2/L3.

All three plan pages also indicate **pay-as-you-go access for more historical data**, so the Standard-included window is not necessarily the maximum history that can be fetched.

---

## Source note

Plan entitlements were transcribed from the screenshots supplied for the three Standard plans. Schema descriptions and normalized field counts were cross-checked against Databento's [schemas and data formats](https://databento.com/docs/schemas-and-data-formats/whats-a-schema) and the [US Equities Summary specification](https://databento.com/docs/venues-and-datasets/equs-summary), while capacity sizing follows the repository's OPRA DBN-plus-Parquet preflight model. Parent-symbol expansion follows Databento's [parent symbology documentation](https://databento.com/docs/standards-and-conventions/symbology); this document does not add entitlements that were not visible in the screenshots.
