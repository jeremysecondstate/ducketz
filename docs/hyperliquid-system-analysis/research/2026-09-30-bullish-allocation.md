# Paper bullish allocation trial

Recorded September 30, 2026 for the round21 simulated Paper handoff. The selected
change is `bullish_spot_fraction=0.25`, previously `0.30`. At an identical portfolio
state, a bullish signal assigns 25% of its requested gross target to spot and 75%
to the long perpetual account, previously 30% and 70%. Requested total gross and
bearish targets are unchanged. Native account capacity, collateral, reserve cash,
executable books, quantity precision and the $10 minimum still determine fills.

This is a bounded prospective execution-cost hypothesis, not proven profitable
alpha. A completed two-hour native comparison showed a narrow after-cost loss.
Initial inherited-inventory adjustment dominated its fees; ongoing costs also
remained material. The change targets the marginal instrument fee mix and does
not claim to repair the entire relative loss or remove startup costs.

The first declared trial moved five percentage points of model weight from MLP
to Random Forest. It was rejected and its full evidence retained. Development
Brier and log loss improved slightly, but the one-basis-point cost proxy lost an
additional $15.1267 and turnover rose 3.38%. In the later block, after-cost loss
increased $0.88046 and both prediction scores worsened. Production model weights
therefore remain logistic 0.15, Extra Trees 0.10, histogram gradient boosting
0.25, MLP 0.10 and Random Forest 0.40. All five families retain positive weight.

The allocation trial was separately declared at 20:39:06 UTC. Its only candidate
was the five-percentage-point shift above; there was no parameter grid. Source,
configuration, features, labels, predictions and retained causal model bundles
were hash-pinned. Four previously examined chronological 24-hour development
blocks supplied 96 hours per market. The 38-bar September 30 17:10–20:15 UTC block
had already been examined in the rejected weight trial and is explicitly reused
development/diagnostic evidence, not untouched confirmation. The development
allocation result was locked before opening that later allocation result.

Models retained the configured five-minute candles, one-bar horizon, chronological
70/15/15 split, one-bar boundary purges, immature-label masking and no refit through
assessment. Forward observations followed the fitted bundle's assessment labels.
The allocation change leaves probabilities and model qualification identical.

| One-basis-point sensitivity | Incumbent | Candidate |
|---|---:|---:|
| 96-hour development net P/L | -466.054939 | -461.786710 |
| Development gross P/L | -39.676737 | -40.193825 |
| Development fees | 354.053438 | 349.247840 |
| Development turnover | 723247.651018 | 723450.458049 |
| Development fills | 6546 | 6485 |
| Development below-floor events | 108 | 126 |
| Later 38-bar net P/L | -43.867008 | -43.439242 |
| Later fees | 15.603971 | 15.234484 |
| Later turnover | 29945.336140 | 29927.849649 |

All declared net, fee, drawdown, turnover and per-market regression bounds passed
at 0/1/3/5 basis points of one-way slippage sensitivity. The declared turnover
bound allowed a 0.1% increase for changing equity and floor effects; development
increased approximately 0.0280%. Development gross capture declined $0.51709,
within the declared $1 bound. HYPE's one-basis-point net result worsened $0.48308,
within its declared $1 bound. Both pooled proxies remain negative after costs.
The mixed bullish fee rate falls from 5.25 to 5.125 basis points at unchanged
spot/perpetual fee rates; this does not establish an after-funding benefit.

A fixed-row sensitivity of the previous opening's eight initial executions
estimated $0.02351 more startup fees, within the declared $1 bound. It is not an
executable counterfactual: altered execution sequences can create different fills.
Native policy checks covered 224 identical-state cases, preserving requested
gross, bearish allocation, positive target opportunity, zero-target holds and the
$10 floor. Independent review and regression receipts are retained with the
local lifecycle evidence.

The simulator starts blocks flat with a $40,000 pool, frozen causal models,
unchanged 4.5-basis-point perpetual and 7-basis-point spot fees, completed-close
3% stops and a one-bar cooldown. It excludes historical executable books,
quantity precision, account cash/collateral, funding, intrabar stops, live latency,
300-second refits and inherited opening inventory. Extra perpetual allocation
increases potential funding and basis exposure and can encounter account capacity
sooner. The native runtime continues to enforce every existing cap and reserve;
requested target equivalence is not proof of identical realized exposure.

The next one-hour native Paper round is prospective confirmation. Measure total
and account-level common-mark excess returns after costs, startup versus ongoing
fees and turnover, funding, capacity/collateral skips, forecast-driven fills and
missed opportunity. No automatic further reduction of the spot fraction is
justified by this trial.

Exploratory Research/Qualified admission, zero entry/exit/rebalance bands, $10
minimum, saturation 0.21, volatility budget, simulated fees, stops, cooldowns,
leverage/exposure caps and model weights remain unchanged. Five-minute collection,
one-bar forecasts, 300-second source-progress refits, 30-second Paper polling and
900-second exports remain configured. Powder retains its separate strict policy
and stays inactive. Git publication conveys no runtime, account or real-execution
authorization on either machine.
