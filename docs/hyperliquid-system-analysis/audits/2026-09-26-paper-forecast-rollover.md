# Paper model labels and forecast-rollover turnover

Read-only investigation on **2026-09-26**, prompted by the account/Paper
screenshots. No strategy setting, worker lifecycle, ledger or real account was
changed. The original Paper run remains intact.

## What the labels establish

| Label | Implemented meaning |
| --- | --- |
| Qualified | Forecast served by a fresh eligible active model. The ensemble's log loss and Brier score must each be no worse than both historical-prior and 50/50 baselines, within `1e-9`. |
| Research | Forecast served by a usable candidate fallback rather than the qualified active model. Current Paper configuration permits these forecasts. |
| Unavailable | Recorded qualification is absent or not a boolean. This is missing attribution, not a lower-ranked model tier. |

See [qualification comparisons](../../../ml/hyperliquid_models.py),
[model selection/publication](../../../ml/hyperliquid_model_artifacts.py),
[UI label mapping](../../../app/services/hyperliquid_paper_view.py) and
[Paper configuration](../../../configs/hyperliquid-paper.json).

Current training requires at least 1,000 fitting rows, 192 calibration rows and
288 chronological assessment rows, with horizon purging between partitions.
The assessment is used for promotion and is not an untouched final test.
Qualification tests probability scores, not profitability after costs, a minimum
trade win rate or statistical significance. A UI filter changes displayed rows;
it does not change execution policy.

## Observed execution issue

The shared forecast reader rejects a decision more than **900 seconds** old.
New 15-minute publications arrive after the candle boundary. When a Paper tick
lands between expiry of the previous forecast and publication of its replacement,
the current runtime catches the validation error, passes no prediction and sets
all account targets for that coin to zero. With executable books, this reduces
inventory. A subsequent valid forecast can open it again.

Missing prediction metadata gives these reductions the UI label **Unavailable**.
They were not entries directed by a third, low-quality model. Sources:
[forecast validation](../../../ml/hyperliquid_forecast_reader.py) and
[Paper target/execution behavior](../../../ml/hyperliquid_paper_runtime.py).

Read-only SQLite evidence through **2026-09-26 07:15:35.720083 UTC**:

| Observation | Value |
| --- | ---: |
| Total fills | 142 |
| Research / Qualified / Unavailable attribution | 66 / 31 / 45 |
| Total simulated fees | $144.70429061 |
| Unavailable reduction fees | $48.40104159 |
| Unavailable reductions followed by same-account/asset reentry within about 32 seconds | 44 of 45 |
| Fees on those paired exits and reentries | $93.39073689 |

All 45 Unavailable fills have null model/forecast/qualification metadata, a zero
target and reason `Forecast is stale or has a future timestamp.` All reduced
exposure; none increased it. They occurred around quarter-hour boundaries.
The combined reader error also mentions future timestamps, but the checked
decision ages/publication sequence support expiry during rollover here.

For the screenshot's **00:15 Pacific** sequence:

- Seven exits at 07:15:04.243–04.257 UTC incurred $7.57177728 in fees.
- Seven reentries at 07:15:35.699–35.720 UTC incurred $8.57895165.
- About 31.46 seconds separated the groups; total fees were **$16.15072893**.
- Jeremy's BTC quantity went **0.01649 → 0 → 0.01824**. Its replacement Research
  forecast was created at 07:15:10.044256 UTC, using model
  `20260926T063013Z-6e18a88f`.

The $93.39 paired fees are observed associated costs, **not** a counterfactual
estimate of avoidable loss. New forecasts can legitimately change sizing and
direction. Spread/slippage and market movement also affect outcomes.

## Starting mirror and comparison limits

The existing seed already used `seed_mode=mirror` at
**2026-09-26T02:04:46.898381710Z**, with pooled opening equity
**$42,078.277612908096** and the original nine positions. Its quantities match
the holdings shown in the real-account screenshot. Paper then evolved under
its own forecasts, sizing, virtual transfers and risk rules.

The first tick immediately closed the inherited Alex HYPE/ZEC shorts for
`stop_loss`, measured against their historical average entry, and rebalanced
other inherited positions. Resetting a mirror with unchanged rules therefore
does not preserve those holdings indefinitely or repair the rollover mechanism.
Spot cost basis is initialized at the opening mark; this is an inventory/cash
mirror, not replication of exchange margin/liquidation behavior or open orders.

The supplied screens show Paper equity **$41,846.30**, cumulative P/L
**-$231.98** and fees **$144.70**, versus actual-account equity **$42,061.41**.
The displayed equity gap is **$215.11**. These are not synchronized: the actual
account view was last synced around 00:18:13 Pacific, versus the Paper observation
around 00:23:33. Actual historical unrealized P/L is also not the same measure
as Paper P/L since its opening snapshot. Use a synchronized, cashflow-adjusted
comparison before attributing a precise performance gap to the model.

Paper currently charges configured base taker assumptions: 0.045% perpetuals
and 0.070% spot. These match the standard tier-zero rates in the
[exchange fee schedule](https://hyperliquid.gitbook.io/hyperliquid-docs/trading/fees),
but are not verification of these accounts' actual discounts or tier.

## Recommended next experiment — not implemented by this audit

1. Distinguish a short, verified forecast-publication rollover from prolonged
   stale, invalid or missing data. During a bounded rollover wait, block new
   exposure and keep risk checks active; do not blindly trade an expired signal
   or suppress stop-loss/collateral reductions. Specify and test the maximum
   wait and prolonged-outage behavior before changing the policy.
2. Label non-model exits with their actual reason, such as **Forecast expired**
   or **Risk exit**, rather than implying an Unavailable model generated them.
   Retain all fills and fees in performance reporting.
3. Archive this run intact and start a separately identified mirror experiment
   only after the policy change is validated and the user chooses to restart.
   Preserve its ledger, seed, configs/policies, funding cursor and provenance;
   coordinate intentional maintenance with Operations Watch.
4. Evaluate the new Paper run beside an unchanged-opening-holdings benchmark
   using the same observation clock/marks, and separately track synchronized
   actual-account snapshots and cashflows. A reset changes the measurement
   origin; it does not establish better trading performance.

There is no proposal to activate Powder or modify real-account positions.
