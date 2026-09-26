# Hyperliquid paper accounts, execution, and transfers

For the maintained cross-system map, accounting contracts and operating procedures,
see [Hyperliquid system analysis](hyperliquid-system-analysis/README.md).

This runtime applies recorded model forecasts to three local paper accounts.
It uses current public Hyperliquid order books for simulated fills and a SQLite
ledger for positions, cash, fees, funding estimates, and virtual transfers.
The data and model coordinators continue independently.

The configuration selected on **2026-09-26** sets
`require_qualified_forecasts: true`: only fresh, validated Qualified forecasts
drive allocation. Research forecasts remain visible for analysis but cannot
create signal exposure or virtual transfers. Without an accepted forecast,
Paper holds current exposure subject to its continuing risk reductions.
This runtime only accepts `mode: "paper"`. There is no switch that sends these
orders or transfers to the real accounts. The separate
[Powder runtime](hyperliquid-system-analysis/POWDER_ACTIVATION.md) remains inactive
until explicit user activation and has a different missing-signal policy.

The earlier mixed Research/Qualified experiment is retained at
`C:/DATASTORE/hyperliquid/_paper_archives/20260926T073643Z-research-and-qualified`.
The new experiment uses a fresh mirror baseline rather than resuming that
archive. Configuration alone does not prove it has started; inspect its own
opening snapshot and runtime evidence.

## Account roles and opening inventory

| Paper account | New trades allowed |
| --- | --- |
| Alex | Short perpetuals; buys can reduce or close an existing short |
| Jeremy | Long perpetuals; sells can reduce or close an existing long |
| Clear Pond | Spot purchases and sales of owned inventory; no borrowing or shorts |

The default `seed_mode: "mirror"` starts from a read-only snapshot of the real
accounts' balances and positions. It resolves public owner addresses and reads
account information; it never constructs a signing client. The separate public
market client permits only market metadata, order books, and funding history.
Account roles are validated before creating the ledger. If a required account
or asset valuation is unavailable, initialization fails visibly; it does not
quietly substitute the example cash balances.

Mirror initialization preserves perpetual quantity and entry price. Spot cost
basis is not reliably available, so spot inventory starts with the observed
opening mark as its paper cost basis. Small inherited spot balances in Alex or
Jeremy are retained as passive inventory; this strategy does not trade those
balances. Existing real open orders are counted in the snapshot metadata but
are not imported as paper orders.

The opening inventory and cash mirror the observed real accounts one-for-one;
they are not resized to the model allocation during seeding. The first trading
cycle can immediately change them through stop/cap reductions or an accepted
Qualified forecast, so a later portfolio need not match the opening snapshot.
New ledgers commit an explicit opening observation with zero experiment P/L and
zero fees before any fills. H.Y.P.E.R. retains this opening and its full inventory
separately from the evolving portfolio. Fresh mirrored stops use the opening mark
as a separate risk reference, preserving historical perpetual entry for accounting.
Losses before the experiment cannot immediately trigger its new 3% stop.

`python -m ml.hyperliquid_paper_runtime --config configs/hyperliquid-paper.json --prepare-only`
creates and publishes the opening without trading, then exits. If the ledger
already exists, it publishes that existing experiment and never refreshes its
balances. A normal launch resumes from it. This is a mutating preparation command,
not a health check; coordinate intentional replacement with Operations Watch and
preserve the previous experiment before creating a different mirror.

Account information and quotes are collected over several requests, so the
opening snapshot is not an atomic exchange snapshot. Unified-account balances
are adjusted to avoid counting perpetual unrealized P/L twice. After opening,
the paper ledger evolves independently: subsequent real trades, deposits,
withdrawals, or transfers do not change it.

Paper performance starts from opening marked equity. It does not claim an
inherited position's historical gain or loss as profit earned by this strategy.
Account P/L also subtracts net virtual transfers, so moving cash between the
three accounts does not manufacture a gain. The displayed pool is their sum;
collateral remains account-local.

An explicitly selected `seed_mode: "manual"` instead uses `initial_cash` and
starts without positions. The supplied $10,000-per-account values are examples
for that mode, not the cash used when mirroring. Once a ledger exists, restarting
resumes it. Changing seed settings does not replace existing paper balances.

## Cadence and forecast eligibility

- Models retrain every 15 minutes under the current model configuration.
- A new one-hour forecast is recorded on each completed 15-minute candle.
- The paper runtime polls every 30 seconds for quotes, new forecasts, and risk
  checks. A polling tick does not automatically create a new trade.
- Each forecast is processed once. Orders move existing holdings toward target
  positions rather than repeatedly buying the same target size.
- Forecasts must match the symbol, interval, and horizon, contain complementary
  finite probabilities, and have known source features. By default the decision
  candle must be at most 15 minutes old, the model at most 24 hours old, and the
  forecast's outcome time must still be in the future.
- A `qualified: true` publication must also have `role: "active"` and a
  matching model record with `eligible: true`; a badge alone is insufficient.

The runtime uses the first configured model horizon, initially four 15-minute
candles. Its one-hour horizon is a rolling forecast, not an instruction to close
every position exactly one hour after entry. A later forecast can maintain,
increase, reduce, or reverse the desired pool exposure while respecting the
account roles. With the current qualification requirement, missing, stale,
invalid or Research forecasts preserve the current position target rather than
requesting a signal-driven exit. They cannot increase exposure or request
virtual transfers. Stops, persisted cooldowns and account/symbol/pool exposure
caps still apply, and reductions require usable marks and executable books.

This behavior is recorded as recipe
`direction-volatility-v2-qualified-hold`. The optional legacy setting
`require_qualified_forecasts: false` retains `direction-volatility-v1`: Research
forecasts may allocate, while missing/invalid/stale forecasts use zero targets.
The [shared reader](../ml/hyperliquid_forecast_reader.py) validates publication
evidence; this hold-versus-zero choice belongs to the Paper consumer.

## Starting signal and sizing rules

Let `p` be the ensemble's probability of not-down and `e = abs(p - 0.5)`.
The current Paper evaluation phase uses the same thresholds for entry and exit:
`p >= 0.51` for bullish exposure and `p <= 0.49` for bearish exposure. Existing
longs exit below 51%; existing shorts exit above 49%. Flat and held positions
use the same inclusive boundary, with no hysteresis gap.
There is no directional position at an exactly neutral probability.

The phase change retains the same seed/ledger and records a new policy ID;
it does not reset prior results or demonstrate improved profitability. See the
[decision-gate investigation](hyperliquid-system-analysis/audits/2026-09-26-paper-decision-gates.md).
The `PaperConfig` constructor's legacy default remains 0.05; the checked-in
runtime configuration explicitly selects `entry_band=exit_band=0.01`.

Once the signal is active, confidence is:

```text
confidence = clip(e / 0.15, 0, 1)  # active shared-threshold policy
```

The confidence reaches one at `p >= 0.65` or `p <= 0.35`. It scales a dollar
volatility budget rather than treating a classification probability as an
expected return or a Kelly betting fraction.
Shared thresholds size conviction from neutral so an eligible 49%/51% boundary
has a nonzero target. This changes target sizes as well as activation. Unequal
entry/exit policies retain their historical hysteresis and sizing formula,
`clip((e-exit_band)/(saturation_band-exit_band), 0, 1)`. Existing minimum trade
sizes, exposure limits, qualification and cooldowns continue to apply.

For the initial one-hour horizon:

```text
horizon_sigma = volatility_log_return_20 * sqrt(4)
effective_sigma = max(horizon_sigma, 0.005)
uncapped_gross = pooled_equity * 0.001 * confidence / effective_sigma
symbol_cap = pooled_equity * 0.15
pool_room = max(0, pooled_equity * 0.60 - gross_exposure_in_other_symbols)
desired_symbol_gross = min(uncapped_gross, symbol_cap, pool_room)
```

Volatility is estimated from trailing observations; the square-root horizon
scaling is a modeling assumption. The budget is not a maximum-loss guarantee.
Opposing positions contribute separately to gross exposure rather than
canceling each other's risk limits.

For a bullish signal, 60% of the desired symbol exposure goes to Clear Pond
spot and 40% to Jeremy long perpetuals. Alex's target is zero. For a bearish
signal, Alex receives the short target and Jeremy/Clear Pond target zero.
These are pool-level targets: spot and perpetual buying do not each receive a
separate full-size bullish allocation. The first recipe does not intentionally
open offsetting long and short legs for the same forecast.

The current models are trained on perpetual candles. Applying their directional
forecast to spot is an explicit strategy assumption; there is no separate spot
model or spot/perpetual basis forecast. Spot fills and P/L nevertheless use the
actual spot market's book and mark.

For example, with $30,000 pooled equity, `p = 0.60`, one-hour sigma of 1%, and
sufficient remaining capacity, confidence is approximately 0.615. The symbol
target is approximately $1,846: $1,108 spot and $738 long perpetuals. Account
cash and collateral checks can reduce those amounts further.

Normal adjustments must exceed the larger of $25 or 10% of the target's
notional value. Complete exits and required risk reductions bypass this
adjustment threshold, although an executable fill still needs to meet its
minimum trade size. Starting settings can be changed in
`configs/hyperliquid-paper.json`; restart to apply policy changes consistently.

## Cash allocation, transfers, and risk checks

Targets are constrained by each account's cash and collateral. New exposure
targets use at most 80% of local marked equity. The paper ledger independently
rejects borrowing for new purchases and new perpetual risk above its 1x gross
collateral limit. Mirrored positions can begin outside these limits; reductions
remain possible so inherited risk can be brought down.

Virtual USDC transfers can fund a receiver's target from another account's
available surplus. They preserve a configured 10% of opening equity as a cash
reserve and respect donor collateral capacity. Transfers below $100 are skipped.
Only an increase in the current symbol's target can request a transfer; a hold
or reduction does not fund unrelated inherited positions. Incoming funds are
not redistributed again during the same allocation plan.
The ledger debits the donor and credits the receiver in the same transaction.
These virtual transfers have zero fees and no settlement delay; this is an
explicit paper assumption, not a promise about real withdrawal mechanics.

Stops trigger after a 3% adverse price move from the persisted risk reference:
the opening mark for a fresh inherited position, or its fill price for a new
position. Reductions preserve this reference; additions weight it with the
new fill. Historical average entry remains separate for accounting. Existing
legacy experiments retain their original entry-based reference on resume.
Stops are checked on polling ticks, not by an exchange-hosted
stop order. A risk reduction can therefore execute later at a worse
price, or remain partially unfilled. This runtime does not reproduce the
exchange's liquidation engine. The performance baseline begins at opening
paper equity, with an explicit zero-cost opening observation before trading.
After a simulated stop fill, that account and symbol are kept at a zero target
for one forecast horizon, initially one hour. The cooldown is reconstructed
from committed fills after a restart.

## Executable quotes and fill assumptions

Paper buys consume visible asks; sells consume visible bids. The simulator
walks fetched available levels and uses their executed-quantity volume-weighted
price with **zero additional slippage**. Separate configured taker fees are
0.045% for perps and 0.070% for spot, charged on filled notional. Quantity is rounded down
to the market's size precision. Insufficient depth yields a partial fill;
unfilled quantity is recorded. A rounded visible fill below $10 is skipped.

Books must be no more than 45 seconds old and no more than five seconds ahead
of the local clock. Crossed or locked books and nonfinite prices or quantities
are rejected. A strategy fill must use a book timestamp after its forecast
became available. The historical candle close or a model's `decision_price`
is never substituted for an execution quote.

Each tick freezes the forecasts before requesting books. A timing or missing-book
failure gets up to three snapshot attempts with short backoff; the frozen forecast
is checked again for expiry after fetching. A quote-only attempt with no monetary
side effects remains pending for later polls while its forecast is still valid.
Completed and partial executions are not replayed. Historical all-account
quote-only skips can finish through one append-only recovery cycle; their original
rows remain intact. Opening quotes are kept as baseline evidence and fresh books
are fetched for the first trading tick.

Each fill records its consumed raw price levels. `consume_fill(market, fill)`
returns a copy with that liquidity removed, allowing orders from multiple
paper accounts in the same cycle to share the same finite book depth. The current
zero-slippage policy uses those levels directly for its average execution price.
A newly fetched public book starts the next observation.

Spot routes are resolved from metadata by exact base asset and USDC quote.
BTC, ETH, and ZEC display names map to UBTC, UETH, and UZEC respectively; HYPE
uses its own token. The API book identifier is the spot **pair** index, which
is different from the base token index. Ambiguous mappings are unavailable
rather than guessed. Spot and perpetual marks remain distinct. These routes
follow the official [Hyperliquid info API](https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api/info-endpoint).

Default fees are 0.045% for perpetual taker fills and 0.070% for spot taker
fills, charged on each fill. These are base-tier assumptions from the
[official fee schedule](https://hyperliquid.gitbook.io/hyperliquid-docs/trading/fees).
Real tier, staking, referral, or market discounts are not inferred automatically.
The simulator does not claim maker queue priority or rebates.

## Funding estimates

The runtime periodically fetches published historical funding rates and
reconstructs the signed perpetual quantity held at each settlement from the
opening inventory plus earlier paper fills. A restarted runtime does not
charge all past funding using today's position size. Settlement identities
are deduplicated in the ledger.

Hyperliquid's actual hourly funding calculation uses signed quantity, the
settlement oracle price, and the funding rate; a positive rate charges longs
and credits shorts. See the [official funding specification](https://hyperliquid.gitbook.io/hyperliquid-docs/trading/funding).
The initial paper runtime uses the completed perpetual candle close at the
settlement's UTC hour as an explicit oracle-price proxy. It preserves the API's
reported settlement timestamp, which can include milliseconds after the hour:

```text
estimated_funding_cashflow = -signed_quantity_at_settlement * close_proxy * realized_funding_rate
```

The ledger and performance report label this funding as estimated. If the
required rate or price proxy is unavailable, the runtime reports a funding
error and leaves that settlement pending rather than inventing a zero charge.
Paper transfers and account-local collateral are modeled separately from
funding; the pool is not treated as one exchange margin account. Real margin
and withdrawal mechanics are described in the
[Hyperliquid margining documentation](https://hyperliquid.gitbook.io/hyperliquid-docs/trading/margining).

## Run, inspect, and stop

From `C:/dev/ducketz` in PowerShell, with the data and model outputs available:

```powershell
& .\.venv\Scripts\python.exe -m ml.hyperliquid_paper_runtime --once
```

This runs one paper cycle. It can create the mirrored opening ledger on its
first invocation and can commit simulated trades and transfers. It performs
no real exchange action. Start continuous operation, inspect it, or request a
graceful stop with:

```powershell
& .\.venv\Scripts\python.exe -m ml.hyperliquid_paper_runtime
& .\.venv\Scripts\python.exe -m ml.hyperliquid_paper_runtime --status
& .\.venv\Scripts\python.exe -m ml.hyperliquid_paper_runtime --stop
```

Use `--config PATH` for a different paper configuration. The model and paper
configurations must identify the same data root. A per-datastore process lock
prevents competing paper writers. Restarting resumes the existing ledger and
does not repeat already committed forecast cycles. No Windows startup task is
installed by these commands.

## Files and interpretation

With the supplied configuration, files live under
`C:/DATASTORE/hyperliquid/_paper/`:

```text
ledger.sqlite3              transactional source of truth
opening_snapshot.json      immutable seed inventory and baseline information
policy.json                settings used by the running paper process
policies/<policy-id>.json   preserved settings and recipe version for later events
performance.json           equity, fees, turnover, drawdown, qualification counts
initial_positions.parquet  inherited inventory
decisions.parquet          target, hold, skip, reduction, and execution reasoning
fills.parquet              executed paper quantities, prices, fees, forecast/model IDs
transfers.parquet          virtual cash movements
funding.parquet            deduplicated estimated funding cashflows
equity.parquet             per-account and pooled valuations
events.parquet             journal events
_runtime/
  status.json              current status, portfolio, quote/funding/forecast errors
  funding_cursor.json      funding-history progress
  .paper.lock              exclusive runtime ownership
  stop.request             pending graceful-stop marker
```

SQLite commits a cycle's transfers, fills, decisions, and valuation together,
or rolls the entire cycle back. Parquets are inspectable exports rather than
the source of truth. Parquets and the performance report refresh after changes
or 900 seconds since the previous export. Graceful shutdown exports Parquet
journals but does not regenerate `performance.json`; retain its `as_of_utc`.
The runtime status is the more frequent operational view.

Decision/fill records attribute a signal only when it was accepted. A rejected
Research publication is retained under `details.policy.rejected_forecast`,
with the exclusion or validation reason in policy details. Top-level forecast
IDs, qualification and probability are null when a hold or risk exit has no
accepted signal; they are not attributed to the rejected model. H.Y.P.E.R. exposes **No forecast**
and **Risk exit** labels with a Reason column.

The performance report separates qualified, research and unattributed fill counts and
reports fees, estimated funding, turnover, marked P/L, and drawdown. Its cash
benchmark is a flat 0% return. This is not a same-inventory buy-and-hold
counterfactual for the mirrored starting portfolio. Results reflect the
specified paper fill and funding assumptions; they do not establish live
execution quality or profitability.
