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

| Level | Schema / data type      | Live on Standard |
| ----- | ----------------------- | ---------------- |
| L0    | OHLCV-1s / 1m / 1h / 1d | Yes              |
| L0    | Definitions             | Yes              |
| L0    | Statistics              | Yes              |
| L0    | Status                  | Yes              |
| L1    | CMBP-1                  | Yes              |
| L1    | TCBBO                   | Yes              |
| L1    | CBBO                    | Yes              |
| L1    | Trades                  | Yes              |

## Historical data included

| Level | Schema / data type      | Standard history shown |
| ----- | ----------------------- | ----------------------:|
| L0    | OHLCV-1s / 1m / 1h / 1d | 13+ years              |
| L0    | Definitions             | 13+ years              |
| L0    | Statistics              | 13+ years              |
| L0    | Status                  | 13+ years              |
| L1    | CMBP-1                  | Last 12 months         |
| L1    | TCBBO                   | Last 12 months         |
| L1    | CBBO                    | Last 12 months         |
| L1    | Trades                  | Last 12 months         |

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

| Level | Schema / data type      | Live on Standard |
| ----- | ----------------------- | ---------------- |
| L0    | OHLCV-1s / 1m / 1h / 1d | Yes              |
| L0    | Definitions             | Yes              |
| L0    | Statistics              | Yes              |
| L0    | Status                  | Yes              |
| L1    | MBP-1                   | Yes              |
| L1    | TBBO                    | Yes              |
| L1    | BBO                     | Yes              |
| L1    | Trades                  | Yes              |
| L2    | MBP-10                  | **No**           |
| L3    | MBO                     | **No**           |

## Historical data included

| Level | Schema / data type      | Standard history shown |
| ----- | ----------------------- | ----------------------:|
| L0    | OHLCV-1s / 1m / 1h / 1d | 16+ years              |
| L0    | Definitions             | 16+ years              |
| L0    | Statistics              | 16+ years              |
| L0    | Status                  | 16+ years              |
| L1    | MBP-1                   | Last 12 months         |
| L1    | TBBO                    | Last 12 months         |
| L1    | BBO                     | Last 12 months         |
| L1    | Trades                  | Last 12 months         |
| L2    | MBP-10                  | Last 1 month           |
| L3    | MBO                     | Last 1 month           |

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

| Level | Schema / data type      | Live on Standard |
| ----- | ----------------------- | ---------------- |
| —     | Full market summary     | Yes              |
| L0    | OHLCV-1s / 1m / 1h / 1d | Yes              |
| L0    | Definitions             | Yes              |
| L0    | Statistics              | Yes              |
| L0    | Status                  | **No**           |
| L1    | MBP-1                   | Yes              |
| L1    | TBBO                    | Yes              |
| L1    | BBO                     | Yes              |
| L1    | Trades                  | Yes              |
| L2    | MBP-10                  | **No**           |
| L3    | MBO                     | **No**           |
| L3    | Imbalance               | **No**           |

## Historical data included

| Level | Schema / data type      | Standard history shown |
| ----- | ----------------------- | ----------------------:|
| —     | Full market summary     | Yes                    |
| L0    | OHLCV-1s / 1m / 1h / 1d | 8+ years               |
| L0    | Definitions             | 8+ years               |
| L0    | Statistics              | 8+ years               |
| L0    | Status                  | 8+ years               |
| L1    | MBP-1                   | Last 12 months         |
| L1    | TBBO                    | Last 12 months         |
| L1    | BBO                     | Last 12 months         |
| L1    | Trades                  | Last 12 months         |
| L2    | MBP-10                  | Last 1 month           |
| L3    | MBO                     | Last 1 month           |
| L3    | Imbalance               | Last 1 month           |

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

This inventory was transcribed only from the screenshots supplied for the three Standard plans. It intentionally does not add dataset codes, API syntax, schema definitions, or entitlements that were not visible in those screenshots.
