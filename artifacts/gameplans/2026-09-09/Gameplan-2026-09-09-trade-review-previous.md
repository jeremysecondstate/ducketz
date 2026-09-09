# 2026-09-09 Gameplan — quantities and planning prices

**7 stocks · 168 frozen forecasts · 0 provisional BUY rows · 0 orders submitted**

Review generated Sep 08, 2026 22:04 PDT. All target windows and recorded times below are Pacific.

Options: 168 source-bound NO_TRADE_STOCK_ONLY placeholders; no option legs or order authority.

126 of 133 scheduled entry windows have promoted directional models; 7 remain research or unsupported. 35 rows are non-entry context. The long-entry threshold is 55.00%. Provisional capital reserved: **$0.00**.

A zero Trade Quantity means no proposed entry. Historical ranges shown beside zero quantities are reference information; they do not force trades or authorize selling current holdings. All proposed quantities reserve current cash together, without assuming future sale proceeds. Actual ask LIMIT prices, available cash, holdings, ownership and live controls must be revalidated at the exact entry window.

## Account snapshot

Account evidence captured Sep 08, 2026 22:04 PDT; status: OBSERVED. Cash evidence: CASH_ONLY_BOUNDED.

| Account measure | Recorded value |
| --- | --- |
| Account equity / liquidation value | $129,665.06 |
| Available cash after existing commitments | $112,081.48 |
| Reported cash balance | $112,081.48 |
| Reported settled cash | Unavailable |
| Existing working-order cash reserves | $0.00 |
| Gross account exposure | $17,583.58 |
| Working orders | 0 |

Ownership evidence: OBSERVED_CONSISTENT. No additional snapshot reason codes recorded.

| Stock | Current shares | Current investment / exposure | Broker price reference | Reference basis | Reference time, Pacific |
| --- | --- | --- | --- | --- | --- |
| AAPL | 1 | $317.58 | $316.34 | SCHWAB_LAST_TRADE | Sep 08, 2026 16:59 PDT |
| AMZN | 1 | $257.88 | $256.65 | SCHWAB_LAST_TRADE | Sep 08, 2026 16:59 PDT |
| COST | 0 | $0.00 | $909.68 | SCHWAB_LAST_TRADE | Sep 08, 2026 16:59 PDT |
| GOOG | 1 | $555.43 | $335.17 | SCHWAB_LAST_TRADE | Sep 08, 2026 16:59 PDT |
| MU | 1 | $1,008.45 | $1,001.31 | SCHWAB_LAST_TRADE | Sep 08, 2026 16:59 PDT |
| NVDA | 1 | $226.06 | $225.31 | SCHWAB_LAST_TRADE | Sep 08, 2026 16:59 PDT |
| SNDK | 1 | $1,754.98 | $1,743.68 | SCHWAB_LAST_TRADE | Sep 08, 2026 16:59 PDT |

Current investment is market-value exposure, including any options attributed to that underlying; it is not a cost-basis statement. Broker reference time is the actual quote/trade time, separate from account capture time.

## Quantity and price rules

The existing deterministic budget is `equity × min(0.15 × weight / 10, 0.05) × clamp(2 × published P(up) − 1, 0, 0.5)`. Horizon weights are 1h=1, 4h=2, 1d=3 and 1w=4. This is an explicit capital-utilization policy; no Kelly or optimal-allocation estimate is asserted.

Shared reservations stay within 95.00% of available cash, remaining 130.00% gross-equity headroom and 15.00% per-symbol equity headroom, including existing and pending positions. Quantities are whole shares budgeted at the **upper planning-range price**, with minimum order notional $25.00 and at most 6 planned orders in one entry batch. The plan does not reuse cash from an assumed future exit or replace an existing horizon allocation.

Price ranges are the **historical central 90% (5th–95th empirical percentiles)** of observed prior-session 17:00-close to entry-clock price ratios, anchored to the exact prior completed session's observed close. They describe historical dispersion and provide no guaranteed future coverage or fill. Daily and weekly entries use their actual first entry clock; context rows have no entry range.

Price evidence: xnas-itch-archive-v1; lookback 120 sessions; minimum 30 samples; observation tolerance 300 seconds. Each row reports admitted historical samples. Missing evidence remains unavailable.

## Directional model assessments

| Horizon | Status | Train / select / calibrate / assess rows | Brier: model / baseline | Log loss: model / baseline | Direction accuracy |
| --- | --- | --- | --- | --- | --- |
| 1h | PROMOTED | 16,306 / 2,588 / 3,153 / 3,699 | 0.230750 / 0.235597 | 0.653406 / 0.664060 | 62.10% |
| 4h | PROMOTED | 4,417 / 734 / 898 / 1,046 | 0.246321 / 0.247408 | 0.685767 / 0.687956 | 55.54% |
| 1d | RESEARCH_NOT_PROMOTED | 4,210 / 718 / 898 / 976 | 0.253475 / 0.249813 | 0.700755 / 0.692773 | 51.74% |
| 1w | PROMOTED | 850 / 126 / 163 / 193 | 0.249592 / 0.260987 | 0.692342 / 0.715529 | 51.81% |

Lower Brier score and log loss are better. The baseline is a constant probability estimated from training and selection data. Assessment rows are held out; these figures are separate from prior published-forecast evaluation and realized trading returns.

**1d remains RESEARCH_NOT_PROMOTED:** Brier score must beat the training/selection baseline; log loss must beat the training/selection baseline. This records failed promotion criteria; it does not establish the underlying cause of weaker performance.

## Independent sizing assessments

| Horizon | Fit status | Fitted / qualified scopes | Failed checks / recorded reason |
| --- | --- | --- | --- |
| 1h | FITTED | 91 / 0 | return error does not beat baseline |
| 4h | FITTED | 52 / 0 | return error does not beat baseline |
| 1d | FITTED | 7 / 0 | return error does not beat baseline; adverse-return error exceeds baseline; probability output has insufficient variation |
| 1w | FITTED | 28 / 0 | log loss does not beat baseline; return error does not beat baseline; adverse-return error exceeds baseline |

Fitted and qualified are separate outcomes. The selected fixed-budget planning policy uses promoted directional probabilities; learned sizing remains a separate assessment lane and retains its recorded research status.

## Forecasts and trade planning by stock

Every frozen forecast is retained. Probabilities are rounded to two decimal percentage points. All windows use the saved actual target timestamps, including overnight four-hour windows and the weekly expiry. Range cells marked reference only accompany zero-share proposals.

### AAPL

Historical range anchor: $316.34, observed Sep 08, 2026 17:00 PDT.

| Route | Target window, Pacific | Raw P(up) | Published P(up) | Direction / model | Trade Quantity | Trade Price (planning range) | Historical samples / status | Planning status / reason |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 1h@04:00 | Sep 09, 2026 04:00 PDT → Sep 09, 2026 05:00 PDT | 36.10% | 36.10% | BEARISH / PROMOTED | 0 | $313.54–$318.45 (reference only) | n=120 | Below the long-entry probability threshold |
| 1h@05:00 | Sep 09, 2026 05:00 PDT → Sep 09, 2026 06:00 PDT | 37.00% | 37.00% | BEARISH / PROMOTED | 0 | $313.17–$318.99 (reference only) | n=120 | Below the long-entry probability threshold |
| 1h@06:00 | Sep 09, 2026 06:00 PDT → Sep 09, 2026 07:00 PDT | 40.22% | 40.22% | BEARISH / PROMOTED | 0 | $313.68–$319.25 (reference only) | n=120 | Below the long-entry probability threshold |
| 1h@07:00 | Sep 09, 2026 07:00 PDT → Sep 09, 2026 08:00 PDT | 38.96% | 38.96% | BEARISH / PROMOTED | 0 | $311.03–$322.07 (reference only) | n=120 | Below the long-entry probability threshold |
| 1h@08:00 | Sep 09, 2026 08:00 PDT → Sep 09, 2026 09:00 PDT | 36.68% | 36.68% | BEARISH / PROMOTED | 0 | $309.57–$323.74 (reference only) | n=120 | Below the long-entry probability threshold |
| 1h@09:00 | Sep 09, 2026 09:00 PDT → Sep 09, 2026 10:00 PDT | 35.21% | 35.21% | BEARISH / PROMOTED | 0 | $309.81–$324.62 (reference only) | n=120 | Below the long-entry probability threshold |
| 1h@10:00 | Sep 09, 2026 10:00 PDT → Sep 09, 2026 11:00 PDT | 32.78% | 32.78% | BEARISH / PROMOTED | 0 | $309.35–$325.20 (reference only) | n=120 | Below the long-entry probability threshold |
| 1h@11:00 | Sep 09, 2026 11:00 PDT → Sep 09, 2026 12:00 PDT | 30.90% | 30.90% | BEARISH / PROMOTED | 0 | $309.85–$324.41 (reference only) | n=120 | Below the long-entry probability threshold |
| 1h@12:00 | Sep 09, 2026 12:00 PDT → Sep 09, 2026 13:00 PDT | 35.13% | 35.13% | BEARISH / PROMOTED | 0 | $310.16–$324.89 (reference only) | n=120 | Below the long-entry probability threshold |
| 1h@13:00 | Sep 09, 2026 13:00 PDT → Sep 09, 2026 14:00 PDT | 27.29% | 27.29% | BEARISH / PROMOTED | 0 | $310.19–$325.19 (reference only) | n=120 | Below the long-entry probability threshold |
| 1h@14:00 | Sep 09, 2026 14:00 PDT → Sep 09, 2026 15:00 PDT | 26.93% | 26.93% | BEARISH / PROMOTED | 0 | $309.92–$325.19 (reference only) | n=120 | Below the long-entry probability threshold |
| 1h@15:00 | Sep 09, 2026 15:00 PDT → Sep 09, 2026 16:00 PDT | 27.38% | 27.38% | BEARISH / PROMOTED | 0 | $309.77–$326.04 (reference only) | n=120 | Below the long-entry probability threshold |
| 1h@16:00 | Sep 09, 2026 16:00 PDT → Sep 09, 2026 17:00 PDT | 27.87% | 27.87% | BEARISH / PROMOTED | 0 | $309.19–$325.16 (reference only) | n=120 | Below the long-entry probability threshold |
| 1h@gap | Sep 08, 2026 17:00 PDT → Sep 09, 2026 04:00 PDT | 44.55% | 44.55% | BEARISH / PROMOTED | 0 | — | NOT_ENTRY | Non-entry research / outlook |
| 4h@04:00 | Sep 09, 2026 04:00 PDT → Sep 09, 2026 08:00 PDT | 41.04% | 44.88% | BEARISH / PROMOTED | 0 | $313.54–$318.45 (reference only) | n=120 | Below the long-entry probability threshold |
| 4h@08:00 | Sep 09, 2026 08:00 PDT → Sep 09, 2026 12:00 PDT | 45.00% | 45.26% | NO_EDGE / PROMOTED | 0 | $309.57–$323.74 (reference only) | n=120 | Below the long-entry probability threshold |
| 4h@12:00 | Sep 09, 2026 12:00 PDT → Sep 09, 2026 16:00 PDT | 36.15% | 44.39% | BEARISH / PROMOTED | 0 | $310.16–$324.89 (reference only) | n=120 | Below the long-entry probability threshold |
| 4h@16:00 | Sep 09, 2026 16:00 PDT → Sep 10, 2026 07:00 PDT | 45.54% | 45.31% | NO_EDGE / PROMOTED | 0 | $309.19–$325.16 (reference only) | n=120 | Below the long-entry probability threshold |
| 1d@D+1 | Sep 09, 2026 04:00 PDT → Sep 09, 2026 17:00 PDT | 61.80% | 61.80% | BULLISH / RESEARCH_NOT_PROMOTED | 0 | $313.54–$318.45 (reference only) | n=120 | Directional model is not promoted |
| 1d@D+2 | Sep 10, 2026 04:00 PDT → Sep 10, 2026 17:00 PDT | 61.21% | 61.21% | BULLISH / RESEARCH_NOT_PROMOTED | 0 | — | NOT_ENTRY | Non-entry research / outlook |
| 1d@D+3 | Sep 11, 2026 04:00 PDT → Sep 11, 2026 17:00 PDT | 57.37% | 57.37% | BULLISH / RESEARCH_NOT_PROMOTED | 0 | — | NOT_ENTRY | Non-entry research / outlook |
| 1d@D+4 | Sep 14, 2026 04:00 PDT → Sep 14, 2026 17:00 PDT | 60.81% | 60.81% | BULLISH / RESEARCH_NOT_PROMOTED | 0 | — | NOT_ENTRY | Non-entry research / outlook |
| 1d@D+5 | Sep 15, 2026 04:00 PDT → Sep 15, 2026 17:00 PDT | 63.26% | 63.26% | BULLISH / RESEARCH_NOT_PROMOTED | 0 | — | NOT_ENTRY | Non-entry research / outlook |
| 1w@D+5 | Sep 09, 2026 04:00 PDT → Sep 15, 2026 17:00 PDT | 64.84% | 45.37% | NO_EDGE / PROMOTED | 0 | $313.54–$318.45 (reference only) | n=120 | Below the long-entry probability threshold |

### AMZN

Historical range anchor: $256.61, observed Sep 08, 2026 17:00 PDT.

| Route | Target window, Pacific | Raw P(up) | Published P(up) | Direction / model | Trade Quantity | Trade Price (planning range) | Historical samples / status | Planning status / reason |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 1h@04:00 | Sep 09, 2026 04:00 PDT → Sep 09, 2026 05:00 PDT | 38.46% | 38.46% | BEARISH / PROMOTED | 0 | $254.08–$260.23 (reference only) | n=120 | Below the long-entry probability threshold |
| 1h@05:00 | Sep 09, 2026 05:00 PDT → Sep 09, 2026 06:00 PDT | 36.81% | 36.81% | BEARISH / PROMOTED | 0 | $253.84–$260.77 (reference only) | n=120 | Below the long-entry probability threshold |
| 1h@06:00 | Sep 09, 2026 06:00 PDT → Sep 09, 2026 07:00 PDT | 42.22% | 42.22% | BEARISH / PROMOTED | 0 | $253.23–$260.85 (reference only) | n=120 | Below the long-entry probability threshold |
| 1h@07:00 | Sep 09, 2026 07:00 PDT → Sep 09, 2026 08:00 PDT | 42.97% | 42.97% | BEARISH / PROMOTED | 0 | $250.90–$263.47 (reference only) | n=120 | Below the long-entry probability threshold |
| 1h@08:00 | Sep 09, 2026 08:00 PDT → Sep 09, 2026 09:00 PDT | 42.91% | 42.91% | BEARISH / PROMOTED | 0 | $251.05–$264.77 (reference only) | n=120 | Below the long-entry probability threshold |
| 1h@09:00 | Sep 09, 2026 09:00 PDT → Sep 09, 2026 10:00 PDT | 39.03% | 39.03% | BEARISH / PROMOTED | 0 | $250.11–$265.66 (reference only) | n=120 | Below the long-entry probability threshold |
| 1h@10:00 | Sep 09, 2026 10:00 PDT → Sep 09, 2026 11:00 PDT | 37.94% | 37.94% | BEARISH / PROMOTED | 0 | $249.91–$265.73 (reference only) | n=120 | Below the long-entry probability threshold |
| 1h@11:00 | Sep 09, 2026 11:00 PDT → Sep 09, 2026 12:00 PDT | 36.44% | 36.44% | BEARISH / PROMOTED | 0 | $250.46–$265.80 (reference only) | n=120 | Below the long-entry probability threshold |
| 1h@12:00 | Sep 09, 2026 12:00 PDT → Sep 09, 2026 13:00 PDT | 41.28% | 41.28% | BEARISH / PROMOTED | 0 | $249.63–$265.48 (reference only) | n=120 | Below the long-entry probability threshold |
| 1h@13:00 | Sep 09, 2026 13:00 PDT → Sep 09, 2026 14:00 PDT | 34.04% | 34.04% | BEARISH / PROMOTED | 0 | $250.08–$265.84 (reference only) | n=120 | Below the long-entry probability threshold |
| 1h@14:00 | Sep 09, 2026 14:00 PDT → Sep 09, 2026 15:00 PDT | 32.77% | 32.77% | BEARISH / PROMOTED | 0 | $249.80–$266.19 (reference only) | n=120 | Below the long-entry probability threshold |
| 1h@15:00 | Sep 09, 2026 15:00 PDT → Sep 09, 2026 16:00 PDT | 34.92% | 34.92% | BEARISH / PROMOTED | 0 | $249.74–$266.51 (reference only) | n=120 | Below the long-entry probability threshold |
| 1h@16:00 | Sep 09, 2026 16:00 PDT → Sep 09, 2026 17:00 PDT | 35.83% | 35.83% | BEARISH / PROMOTED | 0 | $249.83–$266.54 (reference only) | n=120 | Below the long-entry probability threshold |
| 1h@gap | Sep 08, 2026 17:00 PDT → Sep 09, 2026 04:00 PDT | 57.34% | 57.34% | BULLISH / PROMOTED | 0 | — | NOT_ENTRY | Non-entry research / outlook |
| 4h@04:00 | Sep 09, 2026 04:00 PDT → Sep 09, 2026 08:00 PDT | 44.65% | 45.23% | NO_EDGE / PROMOTED | 0 | $254.08–$260.23 (reference only) | n=120 | Below the long-entry probability threshold |
| 4h@08:00 | Sep 09, 2026 08:00 PDT → Sep 09, 2026 12:00 PDT | 39.83% | 44.76% | BEARISH / PROMOTED | 0 | $251.05–$264.77 (reference only) | n=120 | Below the long-entry probability threshold |
| 4h@12:00 | Sep 09, 2026 12:00 PDT → Sep 09, 2026 16:00 PDT | 38.23% | 44.60% | BEARISH / PROMOTED | 0 | $249.63–$265.48 (reference only) | n=120 | Below the long-entry probability threshold |
| 4h@16:00 | Sep 09, 2026 16:00 PDT → Sep 10, 2026 07:00 PDT | 45.64% | 45.32% | NO_EDGE / PROMOTED | 0 | $249.83–$266.54 (reference only) | n=120 | Below the long-entry probability threshold |
| 1d@D+1 | Sep 09, 2026 04:00 PDT → Sep 09, 2026 17:00 PDT | 51.27% | 51.27% | NO_EDGE / RESEARCH_NOT_PROMOTED | 0 | $254.08–$260.23 (reference only) | n=120 | Directional model is not promoted |
| 1d@D+2 | Sep 10, 2026 04:00 PDT → Sep 10, 2026 17:00 PDT | 54.55% | 54.55% | NO_EDGE / RESEARCH_NOT_PROMOTED | 0 | — | NOT_ENTRY | Non-entry research / outlook |
| 1d@D+3 | Sep 11, 2026 04:00 PDT → Sep 11, 2026 17:00 PDT | 50.24% | 50.24% | NO_EDGE / RESEARCH_NOT_PROMOTED | 0 | — | NOT_ENTRY | Non-entry research / outlook |
| 1d@D+4 | Sep 14, 2026 04:00 PDT → Sep 14, 2026 17:00 PDT | 51.01% | 51.01% | NO_EDGE / RESEARCH_NOT_PROMOTED | 0 | — | NOT_ENTRY | Non-entry research / outlook |
| 1d@D+5 | Sep 15, 2026 04:00 PDT → Sep 15, 2026 17:00 PDT | 49.92% | 49.92% | NO_EDGE / RESEARCH_NOT_PROMOTED | 0 | — | NOT_ENTRY | Non-entry research / outlook |
| 1w@D+5 | Sep 09, 2026 04:00 PDT → Sep 15, 2026 17:00 PDT | 53.36% | 43.35% | BEARISH / PROMOTED | 0 | $254.08–$260.23 (reference only) | n=120 | Below the long-entry probability threshold |

### COST

Historical range anchor: $909.24, observed Sep 08, 2026 16:59 PDT.

| Route | Target window, Pacific | Raw P(up) | Published P(up) | Direction / model | Trade Quantity | Trade Price (planning range) | Historical samples / status | Planning status / reason |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 1h@04:00 | Sep 09, 2026 04:00 PDT → Sep 09, 2026 05:00 PDT | 32.96% | 32.96% | BEARISH / PROMOTED | 0 | $905.88–$913.99 (reference only) | n=52 | Below the long-entry probability threshold |
| 1h@05:00 | Sep 09, 2026 05:00 PDT → Sep 09, 2026 06:00 PDT | 28.77% | 28.77% | BEARISH / PROMOTED | 0 | $903.10–$917.33 (reference only) | n=31 | Below the long-entry probability threshold |
| 1h@06:00 | Sep 09, 2026 06:00 PDT → Sep 09, 2026 07:00 PDT | 33.35% | 33.35% | BEARISH / PROMOTED | 0 | $902.23–$917.08 (reference only) | n=43 | Below the long-entry probability threshold |
| 1h@07:00 | Sep 09, 2026 07:00 PDT → Sep 09, 2026 08:00 PDT | 39.58% | 39.58% | BEARISH / PROMOTED | 0 | $888.78–$920.63 (reference only) | n=55 | Below the long-entry probability threshold |
| 1h@08:00 | Sep 09, 2026 08:00 PDT → Sep 09, 2026 09:00 PDT | 30.52% | 30.52% | BEARISH / PROMOTED | 0 | $886.29–$921.31 (reference only) | n=55 | Below the long-entry probability threshold |
| 1h@09:00 | Sep 09, 2026 09:00 PDT → Sep 09, 2026 10:00 PDT | 32.45% | 32.45% | BEARISH / PROMOTED | 0 | $890.41–$923.16 (reference only) | n=55 | Below the long-entry probability threshold |
| 1h@10:00 | Sep 09, 2026 10:00 PDT → Sep 09, 2026 11:00 PDT | 29.68% | 29.68% | BEARISH / PROMOTED | 0 | $887.90–$919.15 (reference only) | n=55 | Below the long-entry probability threshold |
| 1h@11:00 | Sep 09, 2026 11:00 PDT → Sep 09, 2026 12:00 PDT | 28.36% | 28.36% | BEARISH / PROMOTED | 0 | $886.36–$919.67 (reference only) | n=55 | Below the long-entry probability threshold |
| 1h@12:00 | Sep 09, 2026 12:00 PDT → Sep 09, 2026 13:00 PDT | 27.74% | 27.74% | BEARISH / PROMOTED | 0 | $885.23–$920.75 (reference only) | n=55 | Below the long-entry probability threshold |
| 1h@13:00 | Sep 09, 2026 13:00 PDT → Sep 09, 2026 14:00 PDT | 24.27% | 24.27% | BEARISH / PROMOTED | 0 | $886.87–$921.49 (reference only) | n=55 | Below the long-entry probability threshold |
| 1h@14:00 | Sep 09, 2026 14:00 PDT → Sep 09, 2026 15:00 PDT | 21.69% | 21.69% | BEARISH / PROMOTED | 0 | $886.64–$923.52 (reference only) | n=36 | Below the long-entry probability threshold |
| 1h@15:00 | Sep 09, 2026 15:00 PDT → Sep 09, 2026 16:00 PDT | 21.56% | 21.56% | BEARISH / PROMOTED | 0 | $885.30–$925.37 (reference only) | n=37 | Below the long-entry probability threshold |
| 1h@16:00 | Sep 09, 2026 16:00 PDT → Sep 09, 2026 17:00 PDT | 22.21% | 22.21% | BEARISH / PROMOTED | 0 | — | UNAVAILABLE_MINIMUM_SAMPLES | Below the long-entry probability threshold |
| 1h@gap | Sep 08, 2026 17:00 PDT → Sep 09, 2026 04:00 PDT | 28.02% | 28.02% | BEARISH / PROMOTED | 0 | — | NOT_ENTRY | Non-entry research / outlook |
| 4h@04:00 | Sep 09, 2026 04:00 PDT → Sep 09, 2026 08:00 PDT | 38.76% | 44.65% | BEARISH / PROMOTED | 0 | $905.88–$913.99 (reference only) | n=52 | Below the long-entry probability threshold |
| 4h@08:00 | Sep 09, 2026 08:00 PDT → Sep 09, 2026 12:00 PDT | 40.65% | 44.84% | BEARISH / PROMOTED | 0 | $886.29–$921.31 (reference only) | n=55 | Below the long-entry probability threshold |
| 4h@12:00 | Sep 09, 2026 12:00 PDT → Sep 09, 2026 16:00 PDT | 31.76% | 43.93% | BEARISH / PROMOTED | 0 | $885.23–$920.75 (reference only) | n=55 | Below the long-entry probability threshold |
| 4h@16:00 | Sep 09, 2026 16:00 PDT → Sep 10, 2026 07:00 PDT | 47.37% | 45.49% | NO_EDGE / PROMOTED | 0 | — | UNAVAILABLE_MINIMUM_SAMPLES | Below the long-entry probability threshold |
| 1d@D+1 | Sep 09, 2026 04:00 PDT → Sep 09, 2026 17:00 PDT | 21.40% | 21.40% | BEARISH / RESEARCH_NOT_PROMOTED | 0 | $905.88–$913.99 (reference only) | n=52 | Directional model is not promoted |
| 1d@D+2 | Sep 10, 2026 04:00 PDT → Sep 10, 2026 17:00 PDT | 31.87% | 31.87% | BEARISH / RESEARCH_NOT_PROMOTED | 0 | — | NOT_ENTRY | Non-entry research / outlook |
| 1d@D+3 | Sep 11, 2026 04:00 PDT → Sep 11, 2026 17:00 PDT | 18.68% | 18.68% | BEARISH / RESEARCH_NOT_PROMOTED | 0 | — | NOT_ENTRY | Non-entry research / outlook |
| 1d@D+4 | Sep 14, 2026 04:00 PDT → Sep 14, 2026 17:00 PDT | 38.42% | 38.42% | BEARISH / RESEARCH_NOT_PROMOTED | 0 | — | NOT_ENTRY | Non-entry research / outlook |
| 1d@D+5 | Sep 15, 2026 04:00 PDT → Sep 15, 2026 17:00 PDT | 20.67% | 20.67% | BEARISH / RESEARCH_NOT_PROMOTED | 0 | — | NOT_ENTRY | Non-entry research / outlook |
| 1w@D+5 | Sep 09, 2026 04:00 PDT → Sep 15, 2026 17:00 PDT | 47.65% | 42.38% | BEARISH / PROMOTED | 0 | $905.88–$913.99 (reference only) | n=52 | Below the long-entry probability threshold |

### GOOG

Historical range anchor: $335.28, observed Sep 08, 2026 16:59 PDT.

| Route | Target window, Pacific | Raw P(up) | Published P(up) | Direction / model | Trade Quantity | Trade Price (planning range) | Historical samples / status | Planning status / reason |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 1h@04:00 | Sep 09, 2026 04:00 PDT → Sep 09, 2026 05:00 PDT | 33.13% | 33.13% | BEARISH / PROMOTED | 0 | $331.26–$339.19 (reference only) | n=117 | Below the long-entry probability threshold |
| 1h@05:00 | Sep 09, 2026 05:00 PDT → Sep 09, 2026 06:00 PDT | 31.93% | 31.93% | BEARISH / PROMOTED | 0 | $330.40–$339.20 (reference only) | n=117 | Below the long-entry probability threshold |
| 1h@06:00 | Sep 09, 2026 06:00 PDT → Sep 09, 2026 07:00 PDT | 36.24% | 36.24% | BEARISH / PROMOTED | 0 | $330.06–$339.19 (reference only) | n=117 | Below the long-entry probability threshold |
| 1h@07:00 | Sep 09, 2026 07:00 PDT → Sep 09, 2026 08:00 PDT | 36.49% | 36.49% | BEARISH / PROMOTED | 0 | $329.32–$343.16 (reference only) | n=117 | Below the long-entry probability threshold |
| 1h@08:00 | Sep 09, 2026 08:00 PDT → Sep 09, 2026 09:00 PDT | 35.46% | 35.46% | BEARISH / PROMOTED | 0 | $328.06–$344.98 (reference only) | n=117 | Below the long-entry probability threshold |
| 1h@09:00 | Sep 09, 2026 09:00 PDT → Sep 09, 2026 10:00 PDT | 32.54% | 32.54% | BEARISH / PROMOTED | 0 | $328.02–$345.90 (reference only) | n=117 | Below the long-entry probability threshold |
| 1h@10:00 | Sep 09, 2026 10:00 PDT → Sep 09, 2026 11:00 PDT | 32.71% | 32.71% | BEARISH / PROMOTED | 0 | $327.94–$346.90 (reference only) | n=117 | Below the long-entry probability threshold |
| 1h@11:00 | Sep 09, 2026 11:00 PDT → Sep 09, 2026 12:00 PDT | 31.22% | 31.22% | BEARISH / PROMOTED | 0 | $326.80–$346.86 (reference only) | n=117 | Below the long-entry probability threshold |
| 1h@12:00 | Sep 09, 2026 12:00 PDT → Sep 09, 2026 13:00 PDT | 32.92% | 32.92% | BEARISH / PROMOTED | 0 | $325.74–$345.81 (reference only) | n=117 | Below the long-entry probability threshold |
| 1h@13:00 | Sep 09, 2026 13:00 PDT → Sep 09, 2026 14:00 PDT | 26.53% | 26.53% | BEARISH / PROMOTED | 0 | $324.71–$347.19 (reference only) | n=117 | Below the long-entry probability threshold |
| 1h@14:00 | Sep 09, 2026 14:00 PDT → Sep 09, 2026 15:00 PDT | 22.86% | 22.86% | BEARISH / PROMOTED | 0 | $324.98–$347.87 (reference only) | n=110 | Below the long-entry probability threshold |
| 1h@15:00 | Sep 09, 2026 15:00 PDT → Sep 09, 2026 16:00 PDT | 26.55% | 26.55% | BEARISH / PROMOTED | 0 | $325.00–$348.14 (reference only) | n=113 | Below the long-entry probability threshold |
| 1h@16:00 | Sep 09, 2026 16:00 PDT → Sep 09, 2026 17:00 PDT | 25.96% | 25.96% | BEARISH / PROMOTED | 0 | $325.11–$349.06 (reference only) | n=112 | Below the long-entry probability threshold |
| 1h@gap | Sep 08, 2026 17:00 PDT → Sep 09, 2026 04:00 PDT | 44.98% | 44.98% | BEARISH / PROMOTED | 0 | — | NOT_ENTRY | Non-entry research / outlook |
| 4h@04:00 | Sep 09, 2026 04:00 PDT → Sep 09, 2026 08:00 PDT | 43.04% | 45.07% | NO_EDGE / PROMOTED | 0 | $331.26–$339.19 (reference only) | n=117 | Below the long-entry probability threshold |
| 4h@08:00 | Sep 09, 2026 08:00 PDT → Sep 09, 2026 12:00 PDT | 39.93% | 44.77% | BEARISH / PROMOTED | 0 | $328.06–$344.98 (reference only) | n=117 | Below the long-entry probability threshold |
| 4h@12:00 | Sep 09, 2026 12:00 PDT → Sep 09, 2026 16:00 PDT | 43.39% | 45.11% | NO_EDGE / PROMOTED | 0 | $325.74–$345.81 (reference only) | n=117 | Below the long-entry probability threshold |
| 4h@16:00 | Sep 09, 2026 16:00 PDT → Sep 10, 2026 07:00 PDT | 47.05% | 45.46% | NO_EDGE / PROMOTED | 0 | $325.11–$349.06 (reference only) | n=112 | Below the long-entry probability threshold |
| 1d@D+1 | Sep 09, 2026 04:00 PDT → Sep 09, 2026 17:00 PDT | 53.86% | 53.86% | NO_EDGE / RESEARCH_NOT_PROMOTED | 0 | $331.26–$339.19 (reference only) | n=117 | Directional model is not promoted |
| 1d@D+2 | Sep 10, 2026 04:00 PDT → Sep 10, 2026 17:00 PDT | 53.51% | 53.51% | NO_EDGE / RESEARCH_NOT_PROMOTED | 0 | — | NOT_ENTRY | Non-entry research / outlook |
| 1d@D+3 | Sep 11, 2026 04:00 PDT → Sep 11, 2026 17:00 PDT | 54.10% | 54.10% | NO_EDGE / RESEARCH_NOT_PROMOTED | 0 | — | NOT_ENTRY | Non-entry research / outlook |
| 1d@D+4 | Sep 14, 2026 04:00 PDT → Sep 14, 2026 17:00 PDT | 57.32% | 57.32% | BULLISH / RESEARCH_NOT_PROMOTED | 0 | — | NOT_ENTRY | Non-entry research / outlook |
| 1d@D+5 | Sep 15, 2026 04:00 PDT → Sep 15, 2026 17:00 PDT | 55.62% | 55.62% | BULLISH / RESEARCH_NOT_PROMOTED | 0 | — | NOT_ENTRY | Non-entry research / outlook |
| 1w@D+5 | Sep 09, 2026 04:00 PDT → Sep 15, 2026 17:00 PDT | 54.71% | 43.58% | BEARISH / PROMOTED | 0 | $331.26–$339.19 (reference only) | n=117 | Below the long-entry probability threshold |

### MU

Historical range anchor: $1,001.20, observed Sep 08, 2026 17:00 PDT.

| Route | Target window, Pacific | Raw P(up) | Published P(up) | Direction / model | Trade Quantity | Trade Price (planning range) | Historical samples / status | Planning status / reason |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 1h@04:00 | Sep 09, 2026 04:00 PDT → Sep 09, 2026 05:00 PDT | 50.25% | 50.25% | NO_EDGE / PROMOTED | 0 | $959.51–$1,051.93 (reference only) | n=120 | Below the long-entry probability threshold |
| 1h@05:00 | Sep 09, 2026 05:00 PDT → Sep 09, 2026 06:00 PDT | 48.07% | 48.07% | NO_EDGE / PROMOTED | 0 | $956.42–$1,054.16 (reference only) | n=120 | Below the long-entry probability threshold |
| 1h@06:00 | Sep 09, 2026 06:00 PDT → Sep 09, 2026 07:00 PDT | 51.51% | 51.51% | NO_EDGE / PROMOTED | 0 | $950.46–$1,055.39 (reference only) | n=120 | Below the long-entry probability threshold |
| 1h@07:00 | Sep 09, 2026 07:00 PDT → Sep 09, 2026 08:00 PDT | 52.55% | 52.55% | NO_EDGE / PROMOTED | 0 | $938.69–$1,074.49 (reference only) | n=120 | Below the long-entry probability threshold |
| 1h@08:00 | Sep 09, 2026 08:00 PDT → Sep 09, 2026 09:00 PDT | 50.22% | 50.22% | NO_EDGE / PROMOTED | 0 | $938.22–$1,083.57 (reference only) | n=120 | Below the long-entry probability threshold |
| 1h@09:00 | Sep 09, 2026 09:00 PDT → Sep 09, 2026 10:00 PDT | 48.67% | 48.67% | NO_EDGE / PROMOTED | 0 | $931.07–$1,093.70 (reference only) | n=120 | Below the long-entry probability threshold |
| 1h@10:00 | Sep 09, 2026 10:00 PDT → Sep 09, 2026 11:00 PDT | 43.71% | 43.71% | BEARISH / PROMOTED | 0 | $927.50–$1,098.15 (reference only) | n=120 | Below the long-entry probability threshold |
| 1h@11:00 | Sep 09, 2026 11:00 PDT → Sep 09, 2026 12:00 PDT | 47.64% | 47.64% | NO_EDGE / PROMOTED | 0 | $930.72–$1,089.74 (reference only) | n=120 | Below the long-entry probability threshold |
| 1h@12:00 | Sep 09, 2026 12:00 PDT → Sep 09, 2026 13:00 PDT | 48.23% | 48.23% | NO_EDGE / PROMOTED | 0 | $926.95–$1,101.91 (reference only) | n=120 | Below the long-entry probability threshold |
| 1h@13:00 | Sep 09, 2026 13:00 PDT → Sep 09, 2026 14:00 PDT | 46.18% | 46.18% | NO_EDGE / PROMOTED | 0 | $914.05–$1,108.98 (reference only) | n=120 | Below the long-entry probability threshold |
| 1h@14:00 | Sep 09, 2026 14:00 PDT → Sep 09, 2026 15:00 PDT | 44.84% | 44.84% | BEARISH / PROMOTED | 0 | $914.33–$1,119.36 (reference only) | n=120 | Below the long-entry probability threshold |
| 1h@15:00 | Sep 09, 2026 15:00 PDT → Sep 09, 2026 16:00 PDT | 44.27% | 44.27% | BEARISH / PROMOTED | 0 | $912.27–$1,119.08 (reference only) | n=120 | Below the long-entry probability threshold |
| 1h@16:00 | Sep 09, 2026 16:00 PDT → Sep 09, 2026 17:00 PDT | 47.24% | 47.24% | NO_EDGE / PROMOTED | 0 | $904.32–$1,128.24 (reference only) | n=120 | Below the long-entry probability threshold |
| 1h@gap | Sep 08, 2026 17:00 PDT → Sep 09, 2026 04:00 PDT | 58.22% | 58.22% | BULLISH / PROMOTED | 0 | — | NOT_ENTRY | Non-entry research / outlook |
| 4h@04:00 | Sep 09, 2026 04:00 PDT → Sep 09, 2026 08:00 PDT | 54.65% | 46.18% | NO_EDGE / PROMOTED | 0 | $959.51–$1,051.93 (reference only) | n=120 | Below the long-entry probability threshold |
| 4h@08:00 | Sep 09, 2026 08:00 PDT → Sep 09, 2026 12:00 PDT | 56.84% | 46.39% | NO_EDGE / PROMOTED | 0 | $938.22–$1,083.57 (reference only) | n=120 | Below the long-entry probability threshold |
| 4h@12:00 | Sep 09, 2026 12:00 PDT → Sep 09, 2026 16:00 PDT | 52.72% | 45.99% | NO_EDGE / PROMOTED | 0 | $926.95–$1,101.91 (reference only) | n=120 | Below the long-entry probability threshold |
| 4h@16:00 | Sep 09, 2026 16:00 PDT → Sep 10, 2026 07:00 PDT | 53.12% | 46.03% | NO_EDGE / PROMOTED | 0 | $904.32–$1,128.24 (reference only) | n=120 | Below the long-entry probability threshold |
| 1d@D+1 | Sep 09, 2026 04:00 PDT → Sep 09, 2026 17:00 PDT | 53.66% | 53.66% | NO_EDGE / RESEARCH_NOT_PROMOTED | 0 | $959.51–$1,051.93 (reference only) | n=120 | Directional model is not promoted |
| 1d@D+2 | Sep 10, 2026 04:00 PDT → Sep 10, 2026 17:00 PDT | 54.55% | 54.55% | NO_EDGE / RESEARCH_NOT_PROMOTED | 0 | — | NOT_ENTRY | Non-entry research / outlook |
| 1d@D+3 | Sep 11, 2026 04:00 PDT → Sep 11, 2026 17:00 PDT | 53.38% | 53.38% | NO_EDGE / RESEARCH_NOT_PROMOTED | 0 | — | NOT_ENTRY | Non-entry research / outlook |
| 1d@D+4 | Sep 14, 2026 04:00 PDT → Sep 14, 2026 17:00 PDT | 67.88% | 67.88% | BULLISH / RESEARCH_NOT_PROMOTED | 0 | — | NOT_ENTRY | Non-entry research / outlook |
| 1d@D+5 | Sep 15, 2026 04:00 PDT → Sep 15, 2026 17:00 PDT | 64.54% | 64.54% | BULLISH / RESEARCH_NOT_PROMOTED | 0 | — | NOT_ENTRY | Non-entry research / outlook |
| 1w@D+5 | Sep 09, 2026 04:00 PDT → Sep 15, 2026 17:00 PDT | 62.19% | 44.88% | BEARISH / PROMOTED | 0 | $959.51–$1,051.93 (reference only) | n=120 | Below the long-entry probability threshold |

### NVDA

Historical range anchor: $225.21, observed Sep 08, 2026 17:00 PDT.

| Route | Target window, Pacific | Raw P(up) | Published P(up) | Direction / model | Trade Quantity | Trade Price (planning range) | Historical samples / status | Planning status / reason |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 1h@04:00 | Sep 09, 2026 04:00 PDT → Sep 09, 2026 05:00 PDT | 45.97% | 45.97% | NO_EDGE / PROMOTED | 0 | $220.87–$228.86 (reference only) | n=120 | Below the long-entry probability threshold |
| 1h@05:00 | Sep 09, 2026 05:00 PDT → Sep 09, 2026 06:00 PDT | 45.10% | 45.10% | NO_EDGE / PROMOTED | 0 | $221.40–$229.07 (reference only) | n=120 | Below the long-entry probability threshold |
| 1h@06:00 | Sep 09, 2026 06:00 PDT → Sep 09, 2026 07:00 PDT | 49.11% | 49.11% | NO_EDGE / PROMOTED | 0 | $221.17–$229.06 (reference only) | n=120 | Below the long-entry probability threshold |
| 1h@07:00 | Sep 09, 2026 07:00 PDT → Sep 09, 2026 08:00 PDT | 47.95% | 47.95% | NO_EDGE / PROMOTED | 0 | $220.08–$230.82 (reference only) | n=120 | Below the long-entry probability threshold |
| 1h@08:00 | Sep 09, 2026 08:00 PDT → Sep 09, 2026 09:00 PDT | 46.89% | 46.89% | NO_EDGE / PROMOTED | 0 | $219.20–$232.46 (reference only) | n=120 | Below the long-entry probability threshold |
| 1h@09:00 | Sep 09, 2026 09:00 PDT → Sep 09, 2026 10:00 PDT | 43.99% | 43.99% | BEARISH / PROMOTED | 0 | $218.77–$232.76 (reference only) | n=120 | Below the long-entry probability threshold |
| 1h@10:00 | Sep 09, 2026 10:00 PDT → Sep 09, 2026 11:00 PDT | 42.01% | 42.01% | BEARISH / PROMOTED | 0 | $218.74–$233.67 (reference only) | n=120 | Below the long-entry probability threshold |
| 1h@11:00 | Sep 09, 2026 11:00 PDT → Sep 09, 2026 12:00 PDT | 41.00% | 41.00% | BEARISH / PROMOTED | 0 | $218.45–$234.17 (reference only) | n=120 | Below the long-entry probability threshold |
| 1h@12:00 | Sep 09, 2026 12:00 PDT → Sep 09, 2026 13:00 PDT | 47.01% | 47.01% | NO_EDGE / PROMOTED | 0 | $217.90–$234.76 (reference only) | n=120 | Below the long-entry probability threshold |
| 1h@13:00 | Sep 09, 2026 13:00 PDT → Sep 09, 2026 14:00 PDT | 39.35% | 39.35% | BEARISH / PROMOTED | 0 | $216.62–$234.63 (reference only) | n=120 | Below the long-entry probability threshold |
| 1h@14:00 | Sep 09, 2026 14:00 PDT → Sep 09, 2026 15:00 PDT | 37.91% | 37.91% | BEARISH / PROMOTED | 0 | $217.26–$234.50 (reference only) | n=120 | Below the long-entry probability threshold |
| 1h@15:00 | Sep 09, 2026 15:00 PDT → Sep 09, 2026 16:00 PDT | 41.10% | 41.10% | BEARISH / PROMOTED | 0 | $217.72–$234.22 (reference only) | n=120 | Below the long-entry probability threshold |
| 1h@16:00 | Sep 09, 2026 16:00 PDT → Sep 09, 2026 17:00 PDT | 38.77% | 38.77% | BEARISH / PROMOTED | 0 | $217.39–$234.49 (reference only) | n=120 | Below the long-entry probability threshold |
| 1h@gap | Sep 08, 2026 17:00 PDT → Sep 09, 2026 04:00 PDT | 63.17% | 63.17% | BULLISH / PROMOTED | 0 | — | NOT_ENTRY | Non-entry research / outlook |
| 4h@04:00 | Sep 09, 2026 04:00 PDT → Sep 09, 2026 08:00 PDT | 45.21% | 45.28% | NO_EDGE / PROMOTED | 0 | $220.87–$228.86 (reference only) | n=120 | Below the long-entry probability threshold |
| 4h@08:00 | Sep 09, 2026 08:00 PDT → Sep 09, 2026 12:00 PDT | 49.30% | 45.67% | NO_EDGE / PROMOTED | 0 | $219.20–$232.46 (reference only) | n=120 | Below the long-entry probability threshold |
| 4h@12:00 | Sep 09, 2026 12:00 PDT → Sep 09, 2026 16:00 PDT | 44.11% | 45.17% | NO_EDGE / PROMOTED | 0 | $217.90–$234.76 (reference only) | n=120 | Below the long-entry probability threshold |
| 4h@16:00 | Sep 09, 2026 16:00 PDT → Sep 10, 2026 07:00 PDT | 48.32% | 45.58% | NO_EDGE / PROMOTED | 0 | $217.39–$234.49 (reference only) | n=120 | Below the long-entry probability threshold |
| 1d@D+1 | Sep 09, 2026 04:00 PDT → Sep 09, 2026 17:00 PDT | 45.11% | 45.11% | NO_EDGE / RESEARCH_NOT_PROMOTED | 0 | $220.87–$228.86 (reference only) | n=120 | Directional model is not promoted |
| 1d@D+2 | Sep 10, 2026 04:00 PDT → Sep 10, 2026 17:00 PDT | 40.29% | 40.29% | BEARISH / RESEARCH_NOT_PROMOTED | 0 | — | NOT_ENTRY | Non-entry research / outlook |
| 1d@D+3 | Sep 11, 2026 04:00 PDT → Sep 11, 2026 17:00 PDT | 39.01% | 39.01% | BEARISH / RESEARCH_NOT_PROMOTED | 0 | — | NOT_ENTRY | Non-entry research / outlook |
| 1d@D+4 | Sep 14, 2026 04:00 PDT → Sep 14, 2026 17:00 PDT | 59.30% | 59.30% | BULLISH / RESEARCH_NOT_PROMOTED | 0 | — | NOT_ENTRY | Non-entry research / outlook |
| 1d@D+5 | Sep 15, 2026 04:00 PDT → Sep 15, 2026 17:00 PDT | 56.08% | 56.08% | BULLISH / RESEARCH_NOT_PROMOTED | 0 | — | NOT_ENTRY | Non-entry research / outlook |
| 1w@D+5 | Sep 09, 2026 04:00 PDT → Sep 15, 2026 17:00 PDT | 54.04% | 43.46% | BEARISH / PROMOTED | 0 | $220.87–$228.86 (reference only) | n=120 | Below the long-entry probability threshold |

### SNDK

Historical range anchor: $1,743.19, observed Sep 08, 2026 17:00 PDT.

| Route | Target window, Pacific | Raw P(up) | Published P(up) | Direction / model | Trade Quantity | Trade Price (planning range) | Historical samples / status | Planning status / reason |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 1h@04:00 | Sep 09, 2026 04:00 PDT → Sep 09, 2026 05:00 PDT | 52.87% | 52.87% | NO_EDGE / PROMOTED | 0 | $1,661.18–$1,822.31 (reference only) | n=120 | Below the long-entry probability threshold |
| 1h@05:00 | Sep 09, 2026 05:00 PDT → Sep 09, 2026 06:00 PDT | 49.40% | 49.40% | NO_EDGE / PROMOTED | 0 | $1,657.10–$1,842.05 (reference only) | n=120 | Below the long-entry probability threshold |
| 1h@06:00 | Sep 09, 2026 06:00 PDT → Sep 09, 2026 07:00 PDT | 53.68% | 53.68% | NO_EDGE / PROMOTED | 0 | $1,628.07–$1,836.02 (reference only) | n=120 | Below the long-entry probability threshold |
| 1h@07:00 | Sep 09, 2026 07:00 PDT → Sep 09, 2026 08:00 PDT | 54.71% | 54.71% | NO_EDGE / PROMOTED | 0 | $1,603.81–$1,890.86 (reference only) | n=120 | Below the long-entry probability threshold |
| 1h@08:00 | Sep 09, 2026 08:00 PDT → Sep 09, 2026 09:00 PDT | 52.16% | 52.16% | NO_EDGE / PROMOTED | 0 | $1,588.81–$1,895.85 (reference only) | n=120 | Below the long-entry probability threshold |
| 1h@09:00 | Sep 09, 2026 09:00 PDT → Sep 09, 2026 10:00 PDT | 51.63% | 51.63% | NO_EDGE / PROMOTED | 0 | $1,570.44–$1,907.12 (reference only) | n=120 | Below the long-entry probability threshold |
| 1h@10:00 | Sep 09, 2026 10:00 PDT → Sep 09, 2026 11:00 PDT | 46.78% | 46.78% | NO_EDGE / PROMOTED | 0 | $1,558.65–$1,944.87 (reference only) | n=120 | Below the long-entry probability threshold |
| 1h@11:00 | Sep 09, 2026 11:00 PDT → Sep 09, 2026 12:00 PDT | 51.41% | 51.41% | NO_EDGE / PROMOTED | 0 | $1,566.33–$1,937.24 (reference only) | n=120 | Below the long-entry probability threshold |
| 1h@12:00 | Sep 09, 2026 12:00 PDT → Sep 09, 2026 13:00 PDT | 52.50% | 52.50% | NO_EDGE / PROMOTED | 0 | $1,557.56–$1,941.31 (reference only) | n=120 | Below the long-entry probability threshold |
| 1h@13:00 | Sep 09, 2026 13:00 PDT → Sep 09, 2026 14:00 PDT | 52.00% | 52.00% | NO_EDGE / PROMOTED | 0 | $1,561.31–$1,967.05 (reference only) | n=120 | Below the long-entry probability threshold |
| 1h@14:00 | Sep 09, 2026 14:00 PDT → Sep 09, 2026 15:00 PDT | 47.56% | 47.56% | NO_EDGE / PROMOTED | 0 | $1,554.23–$1,991.17 (reference only) | n=120 | Below the long-entry probability threshold |
| 1h@15:00 | Sep 09, 2026 15:00 PDT → Sep 09, 2026 16:00 PDT | 48.82% | 48.82% | NO_EDGE / PROMOTED | 0 | $1,553.22–$1,997.29 (reference only) | n=120 | Below the long-entry probability threshold |
| 1h@16:00 | Sep 09, 2026 16:00 PDT → Sep 09, 2026 17:00 PDT | 51.61% | 51.61% | NO_EDGE / PROMOTED | 0 | $1,536.48–$2,009.34 (reference only) | n=120 | Below the long-entry probability threshold |
| 1h@gap | Sep 08, 2026 17:00 PDT → Sep 09, 2026 04:00 PDT | 54.77% | 54.77% | NO_EDGE / PROMOTED | 0 | — | NOT_ENTRY | Non-entry research / outlook |
| 4h@04:00 | Sep 09, 2026 04:00 PDT → Sep 09, 2026 08:00 PDT | 52.59% | 45.98% | NO_EDGE / PROMOTED | 0 | $1,661.18–$1,822.31 (reference only) | n=120 | Below the long-entry probability threshold |
| 4h@08:00 | Sep 09, 2026 08:00 PDT → Sep 09, 2026 12:00 PDT | 48.82% | 45.62% | NO_EDGE / PROMOTED | 0 | $1,588.81–$1,895.85 (reference only) | n=120 | Below the long-entry probability threshold |
| 4h@12:00 | Sep 09, 2026 12:00 PDT → Sep 09, 2026 16:00 PDT | 50.70% | 45.80% | NO_EDGE / PROMOTED | 0 | $1,557.56–$1,941.31 (reference only) | n=120 | Below the long-entry probability threshold |
| 4h@16:00 | Sep 09, 2026 16:00 PDT → Sep 10, 2026 07:00 PDT | 49.52% | 45.69% | NO_EDGE / PROMOTED | 0 | $1,536.48–$2,009.34 (reference only) | n=120 | Below the long-entry probability threshold |
| 1d@D+1 | Sep 09, 2026 04:00 PDT → Sep 09, 2026 17:00 PDT | 47.92% | 47.92% | NO_EDGE / RESEARCH_NOT_PROMOTED | 0 | $1,661.18–$1,822.31 (reference only) | n=120 | Directional model is not promoted |
| 1d@D+2 | Sep 10, 2026 04:00 PDT → Sep 10, 2026 17:00 PDT | 47.30% | 47.30% | NO_EDGE / RESEARCH_NOT_PROMOTED | 0 | — | NOT_ENTRY | Non-entry research / outlook |
| 1d@D+3 | Sep 11, 2026 04:00 PDT → Sep 11, 2026 17:00 PDT | 49.25% | 49.25% | NO_EDGE / RESEARCH_NOT_PROMOTED | 0 | — | NOT_ENTRY | Non-entry research / outlook |
| 1d@D+4 | Sep 14, 2026 04:00 PDT → Sep 14, 2026 17:00 PDT | 59.38% | 59.38% | BULLISH / RESEARCH_NOT_PROMOTED | 0 | — | NOT_ENTRY | Non-entry research / outlook |
| 1d@D+5 | Sep 15, 2026 04:00 PDT → Sep 15, 2026 17:00 PDT | 52.39% | 52.39% | NO_EDGE / RESEARCH_NOT_PROMOTED | 0 | — | NOT_ENTRY | Non-entry research / outlook |
| 1w@D+5 | Sep 09, 2026 04:00 PDT → Sep 15, 2026 17:00 PDT | 74.40% | 47.31% | NO_EDGE / PROMOTED | 0 | $1,661.18–$1,822.31 (reference only) | n=120 | Below the long-entry probability threshold |

## Prior published-forecast evaluation

1,296 prior forecasts: 823 evaluated, 174 mature awaiting valid data and 299 pending maturity. Observed direction accuracy 64.03%; mean Brier score 0.227011. These are prior forecast scores, not realized trading returns.

## Source evidence

Production OPRA coverage recorded by the source publication: 21 symbol/schema cursors; exclusive completed-through 2026-09-09.

Frozen source: `ml/nightly-gameplan-runs/20260909T040455.436642Z`. Source receipt SHA-256: `eb2490d3ea8ffe2ce778c6daecf55db301b01f08f8b94ca09e904b1fd9402656`.

- [Immutable Gameplan](<C:/DATASTORE/ml/nightly-gameplan-runs/20260909T040455.436642Z/gameplan.json>)
- [Frozen forecasts](<C:/DATASTORE/ml/nightly-gameplan-runs/20260909T040455.436642Z/forecasts.parquet>)
- [Directional assessments](<C:/DATASTORE/ml/nightly-gameplan-runs/20260909T040455.436642Z/model-reports.json>)
- [Source receipt](<C:/DATASTORE/ml/nightly-gameplan-runs/20260909T040455.436642Z/receipt.json>)
