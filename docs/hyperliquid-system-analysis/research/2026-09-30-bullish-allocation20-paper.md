# Exploratory Paper bullish allocation trial: 20% spot

The September 30 trial changes only `bullish_spot_fraction` from 0.25 to 0.20.
At the same state, bullish exposure is allocated 20% to spot and 80% to
perpetuals. Total requested gross exposure, short targets, saturation 0.24,
volatility budget 0.00045 and all existing exposure/collateral caps remain
unchanged. This is a prospective marginal cost hypothesis, not proven alpha.

One candidate was declared before evaluation. Four reused chronological
24-hour forward blocks (96 hours per market) used pinned causal five-family
bundles with 70/15/15 partitions and strict one-bar boundary purging. Selection
was locked before inspecting a later 15-bar diagnostic. These are reused
development and later diagnostic evidence, not untouched confirmation. The
next native Paper round supplies prospective evidence.

At unchanged taker fees plus one basis point of assumed one-way slippage,
aggregate development net P/L improved from -401.372784 to -396.840557 per
four independent 40,000-dollar test pools. Fees declined from 304.791954 to
299.927714, turnover from 631,639.679398 to 630,872.141384, and fills from
6,337 to 6,181. The later diagnostic net improved only from -0.332243 to
-0.292668; fees declined from 4.607312 to 4.485341 and fills from 98 to 92.
HYPE's later net regressed by 0.039239, inside the declared bound. Gross P/L
regressed slightly through trade-floor and fee-feedback effects. All declared
net, cost, activity, drawdown and per-market bounds passed at 0/1/3/5 basis
points. These negative net results do not establish profitability.

The native policy matched pooled target sizing across 288 same-state cases.
The fixed startup-row sensitivity increased modeled fees by 0.018471, within
the predeclared one-dollar bound. It does not promise lower startup costs.
The proxy lacks historical executable books, exchange quantity precision,
cash/collateral allocation, funding, intrabar stops, latency and 300-second
refits. Live Paper continues to enforce each of those constraints.

Moving five percentage points of bullish allocation toward perpetuals reduces
the weighted taker fee from 5.125 to 5.0 basis points per bullish turnover
dollar without changing either fee rate. Extra perpetual funding or collateral
constraints can offset this small advantage. Review actual funding, capacity
skips, forecast-driven fills, initial versus ongoing costs, gross return,
drawdown and native common-mark after-cost excess return next round. No further
allocation shift is automatically justified.

All five positive families and weights remain: logistic .15, Extra Trees .10,
histogram gradient boosting .25, MLP .10 and random forest .40; calibration C
remains .1. Preserve five-minute candles, one-bar forecasts, 300-second source
progress refits, 30-second Paper polls and 900-second exports. Valid Research
and Qualified forecasts remain eligible, with zero entry/exit/rebalance bands
and the executable ten-dollar minimum. Missing/stale forecasts hold targets
while independent risk checks run. Powder retains its separate strict policy
and remains inactive. Git publication supplies no deployment or order authority.
