# Paper round 13: prospective five-family weighting trial

Recorded September 30, 2026. This note describes the completed recipe evaluation
and the configuration change. It does not authorize deployment on another
machine or changes to real execution.

The preceding Paper round produced no eligible forecast witness. Two calibration
trials failed before the selected weighting trial. The selected recipe is a
prospective simulated experiment, not a confirmed improvement: it failed the
declared qualification-coverage requirement and the later diagnostic.

| Family | Previous weight | Selected weight |
| --- | ---: | ---: |
| Logistic regression | 0.32 | 0.15 |
| Extra Trees | 0.16 | 0.10 |
| Histogram gradient boosting | 0.16 | 0.25 |
| MLP | 0.16 | 0.10 |
| Random Forest | 0.20 | 0.40 |

All five families retain positive contribution in every configured market/horizon
slot, for 20 estimator members across four slots. Calibration C remains 0.1.
The configuration diff changes only these five weights.

The research preserved pinned source and public five-minute data hashes. Four
chronological 24-hour development blocks supplied 1,152 forward decisions per
market. These blocks were previously examined and explicitly reused as
development evidence. Every native fit retained the 70/15/15 chronological split,
one-bar boundary purging and immature-label masking; forward decisions followed
the final assessment label.

The first trial reduced calibration C to 0.01. Mean forward log loss worsened
from 0.69452059 to 0.69475884, Brier score worsened from 0.25068335 to 0.25080319,
and qualified slots fell from 6/16 to 4/16. Its cost-proxy gain came entirely
from zero trades. It was rejected. A separately declared C=1.0 trial also
worsened both proper scores and increased turnover about 3.42 times; it was
rejected. Both failures and their artifacts remain retained.

The third trial declared a single selection rule before family attribution:
rank families by mean development Brier score, with log loss and family name
as tie breakers, then assign weights 0.40, 0.25, 0.15, 0.10 and 0.10. The
resulting weights were locked before ensemble evaluation and the later
diagnostic. No grid or alternate recipe was selected using that diagnostic.

Under the selected weights, development mean log loss improved to 0.69446640
and Brier score to 0.25065709. Both remain worse than a neutral probability
benchmark. At the one-basis-point slippage sensitivity, the net proxy loss
decreased about 34.4% and turnover decreased about 17.5%; fills fell from 90 to
60. Net and turnover improved at all four slippage sensitivities. Approximately
52.6% of the one-basis-point net improvement came from eliminated BTC trades;
the rest came from changed ETH and ZEC activity. Qualified slots nevertheless
fell from 6/16 to 5/16, failing strict development acceptance.

The later 15-decision diagnostic was examined only after the recipe was locked.
It was not globally untouched confirmation, and its sample was too small to
establish profitability. Mean log loss worsened from 0.69553397 to 0.69639465
and Brier score from 0.25118973 to 0.25161742; ZEC exceeded the declared
per-market regression bounds. Both recipes had zero qualified slots and zero
trades. This result does not confirm better signals or resolve the absence of
eligible forecasts.

Independent review recomputed weighted assessment and forward probabilities,
proper scores, all four native qualification comparisons, aggregate qualification
counts and chronological boundaries for 40 model reports. It also verified 64
reused incumbent artifact hashes and the declared ranking against the locked
weights. Research serialization and production compatibility checks are retained
in the local audit evidence. Eight focused offline suites passed 550 tests at
09:23 UTC, with the final configuration and source hashes bound to the receipt.
These cover model configuration, recipes, models, artifacts, runtime, forecast
consumption, Paper policy and cadence. The rationale for a prospective trial is the modest
development probability and cost improvement with all five families preserved;
the failed acceptance criteria remain explicit.

The cost proxy uses the unchanged native target/rebalance rules and simulated
fees, plus 0/1/3/5 basis points of one-way slippage. It starts each block flat,
freezes models for the block, and observes stops at completed candle closes.
It lacks historical executable books, funding, inherited inventory, collateral
constraints, intrabar stops, latency and 300-second refits. It is not an
executable replay. Startup inventory-adjustment costs remain in the separate
Paper experiment accounting.

Five-minute candles, next-five-minute forecasts, 300-second source-progress
refits, 30-second Paper polling and 900-second exports remain configured.
Qualification requirements, simulated fees, stops, exposure/leverage caps and
all Paper policy parameters are unchanged. The next evaluation must measure
eligible forecast evidence, after-cost common-mark excess return, and startup
costs separately. Runtime ownership, opening verification and scheduling remain
local lifecycle responsibilities; a Git publication grants no execution authority.
