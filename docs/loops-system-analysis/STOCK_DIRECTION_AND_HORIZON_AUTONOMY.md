# Stock direction and horizon autonomy — October 19, 2026

Jeremy's October 19 direction applies to every symbol and every stock trader on
Atlas and Scout. Probabilities remain unrounded when making the decision.

| Published P(up) | Action |
| --- | --- |
| 50.01% or higher | BUY |
| 49.99% or lower | SELL |
| Strictly between 49.99% and 50.01% | HOLD |

The cutoffs are inclusive: `p >= 0.5001` and `p <= 0.4999`. The shared policy is
`stock-direction-5001-4999-v3`. This also applies to both independent 15-minute
traders. A legacy probability-entry gate must not raise the BUY cutoff.
Model selection, fitted probabilities, calibration and private recipes retain
their existing local ownership and evidence.

The 15-minute traders retain their existing freedom to sell available shares
across horizons. From the October 19 session, 1h and 4h have that same freedom:
their bearish directions can sell actual available shares belonging to any
horizon, without a longer-horizon requirement, same-clock bullish veto, weighted
fallback slot or daily fallback percentage cap. This applies to new decisions
from existing saved forecasts as well as newly prepared forecasts. The 1d and
1w ownership rules remain unchanged, including the existing 1d fallback policy.
Earlier descriptions of 1h/4h ownership protection and bounded fallback describe
the prior policy.

Available shares exclude working or uncertain sell reservations. Orders retain
their existing cash, whole-share, order-size, timing and live-quote requirements.
Each sale reserves shares atomically and attributes actual fills to the original
owners. A forecast direction is not a claim that an order was placed or filled.

Effective tonight and every subsequent night until Jeremy changes the policy,
the canonical Gameplan recipe is `direction-live-quote-v1`, with
`execution_quantity_basis=live_broker_capacity`. It replaces the previous
estimated-price creation method. Gameplan neither generates nor requires a
forecast planning price path, price band or estimated execution midpoint.
Its display omits those values, and new orders cannot be gated, sized or capped
by their projected quantities. Quantities and execution prices are determined
from current broker cash, available holdings, working-order reservations and
live bid/ask/midpoint quotes.

Older Gameplans remain immutable historical evidence. Their existence cannot
complete a new nightly run or cause the current display, synthesis or delivery
to select the superseded recipe. Compatible fetched data, fitted models and
probabilities remain reusable; only obsolete policy-dependent stages need new
outputs. Historical saved publications and known-order recovery remain intact.

Forward Gameplan publications record the policy version, both thresholds and
ownership context in their manifests and receipts. Saved historical evidence
keeps its original policy. Native nightly tasks use the portable
[instruction block](NIGHTLY_STOCK_POLICY_INSTRUCTIONS.md); the current workflow
authority is documented in [Nightly Gameplan](NIGHTLY_GAMEPLAN.md).
