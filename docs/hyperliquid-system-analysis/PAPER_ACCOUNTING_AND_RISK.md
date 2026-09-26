# Paper accounting and risk

Verified against repository code and the checked-in configuration on **2026-09-25**.
The numbers below describe this configuration snapshot, not permanent strategy
rules or a statement about a running process's loaded settings. Compare its
`_paper/policy.json` and event `policy_id` with the checked-in configuration.

This page explains the accounting and decision boundaries. For the poll sequence,
see [Paper loop](loops/paper.md); for component usage, see the existing
[paper guide](../hyperliquid-paper.md).

## Account ownership and the opening baseline

| Account | Managed inventory | Allowed adjustment |
| --- | --- | --- |
| Alex | Short perpetuals, quantity ≤ 0 | Sell to increase; buy to reduce or close |
| Jeremy | Long perpetuals, quantity ≥ 0 | Buy to increase; sell to reduce or close |
| Clear Pond (`clearpond`) | Owned spot inventory, quantity ≥ 0 | Buy or sell owned units; no borrowing or shorts |

The default `seed_mode=mirror` reads public account information and current public
quotes once. It resolves owner addresses without constructing a signing client.
Perpetual quantity and entry cost survive the copy. Spot inventory uses its opening
mark as paper cost basis because reliable historical spot cost is unavailable.
Alex/Jeremy inherited spot inventory remains **inherited, unmanaged**; the active
strategy selects their perpetual positions only. That passive inventory still
contributes to valuation and gross exposure.

The opening requests are not an atomic exchange snapshot. Unified-account handling
subtracts reported perpetual unrealized P/L from base cash so local valuation does
not count it twice. Invalid account roles or missing inherited-asset valuations
fail initialization; the manual cash examples are never a mirror fallback.
Real open orders are counted in seed metadata but are not imported as paper orders.

After seeding, real account changes do not update the paper accounts. An existing
database opens with `open_existing=True`; a missing completed seed is an error,
not permission to create replacement balances. Changing `seed_mode` or
`initial_cash` and restarting does **not** reseed the established experiment.

Sources: [mirror_accounts](../../ml/hyperliquid_paper_seed.py),
[PaperRuntime.initialize](../../ml/hyperliquid_paper_runtime.py),
[PaperLedger initialization](../../ml/hyperliquid_paper_ledger.py).

## What equity and P/L mean

For account `a`, quantities are signed and spot/perpetual marks are separate:

```text
unrealized_pnl(position) = quantity × (mark − average_entry)
equity(a) = cash(a) + sum(spot_quantity × spot_mark) + sum(perp_unrealized_pnl)
gross_exposure(a) = sum(abs(quantity) × mark)
total_pnl(a) = equity(a) − initial_equity(a) − net_transfers(a)
net_realized_pnl(a) = realized_pnl(a) + funding(a) − fees(a)
```

`net_transfers` is receipts minus donations. Account P/L removes it; pooled
internal transfers sum to zero. Pooled values are already sums of the three
accounts: never add account rows to the pooled row a second time.

| Quantity | Correct reading | Common incorrect reading |
| --- | --- | --- |
| `total_pnl` | Marked paper performance since opening, including costs | Historical gain since an inherited entry |
| `net_realized_pnl` | Realized result plus signed funding minus fees | Headline paper performance |
| Position unrealized P/L | Result from retained entry to current mark | Entirely earned after paper opening |
| Gross exposure | Sum of absolute spot/perp notionals | Net long minus short exposure |
| Transfer count/amount | Virtual cash allocation among accounts | New pooled capital or trading turnover |

For example, an inherited short can realize an old loss when closed while
`total_pnl` remains close to zero if its mark barely changed after opening.
The opening marked equity already absorbed that inherited loss.

For Alex/Jeremy, ledger free cash is
`max(0, min(cash, equity − gross_exposure / max_gross_leverage))`; the runtime
uses the ledger's default `max_gross_leverage=1`. Clear Pond free cash is bounded
cash. These are simplified paper collateral rules, not exchange maintenance
margin, liquidation prices, or universally deployable funds.

The runtime's pooled drawdown is `equity / running_peak − 1`, with the peak at
least opening equity. The read-only UI adapter uses `initial_equity + total_pnl`
for account drawdown, avoiding artificial losses from internal donations. It
calculates peaks before chart downsampling. The performance export is a dated
report; retain its `as_of_utc` separately from the latest ledger observation.

Sources: [PaperLedger.state and _fill](../../ml/hyperliquid_paper_ledger.py),
[PaperRuntime.report](../../ml/hyperliquid_paper_runtime.py),
[view adapter](../../app/services/hyperliquid_paper_view.py).

## Configuration snapshot and sizing

Values verified in [hyperliquid-paper.json](../../configs/hyperliquid-paper.json):

| Parameter | Value | Meaning |
| --- | --- | --- |
| `require_qualified_forecasts` | `false` | Research and qualified forecasts can participate |
| `poll_seconds` | 30 seconds | Wait after each loop iteration, not guaranteed start-to-start latency |
| `entry_band` / `exit_band` / `saturation_band` | 0.05 / 0.02 / 0.15 | Absolute distance of P(not-down) from 0.5 |
| `volatility_budget_fraction` / `sigma_floor` | 0.001 / 0.005 | Dollar-volatility sizing budget and horizon-volatility floor |
| `per_symbol_gross_fraction` / `pool_gross_fraction` | 0.15 / 0.60 | Desired gross limits relative to pooled equity |
| `bullish_spot_fraction` | 0.60 | Bullish allocation share assigned to Clear Pond |
| `account_utilization` / `reserve_cash_fraction` | 0.80 / 0.10 | Account exposure ceiling and reserve relative to opening equity |
| `min_trade_notional` / `rebalance_min_delta_fraction` | $25 / 0.10 | Normal adjustment threshold |
| `transfer_min_amount` | $100 | Minimum planned virtual transfer |
| `stop_loss_fraction` | 0.03 | Adverse move from entry that requests reduction |
| `perp_fee_rate` / `spot_fee_rate` | 0.00045 / 0.00070 | Fixed simulated taker fee fractions |
| `slippage_bps` | 2 | Adverse price adjustment after visible-book VWAP |
| Forecast / model / quote maximum ages | 900 / 86,400 / 45 seconds | Eligibility boundaries, detailed below |

The runtime takes the first configured model horizon: currently four 15-minute
bars, or one hour. Horizon volatility is the forecast's exact source-row
`volatility_log_return_20 × sqrt(horizon_bars)`.

Let `p=P(not-down)`, `e=abs(p−0.5)`, `E=max(pooled_equity,0)`, and `O` be other
gross exposure. A fresh direction enters at `e≥0.05`; existing exposure matching
that direction remains eligible only at `e>0.02` (with small floating-point
tolerances). Matching and opposing legs are examined separately, not netted.
Neutral/failed eligibility produces zero desired exposure.

```text
confidence = clip((e − exit_band) / (saturation_band − exit_band), 0, 1)
             when active; otherwise 0
effective_sigma = max(horizon_sigma, sigma_floor)
uncapped_gross = E × volatility_budget_fraction × confidence / effective_sigma
desired_gross = min(uncapped_gross, E × per_symbol_gross_fraction,
                    max(0, E × pool_gross_fraction − O))
```

Bullish desired gross is split 60% Clear Pond spot / 40% Jeremy long perps;
Alex's target is zero. Bearish gross becomes Alex's negative target; both long
targets are zero. This is probability conviction scaled by volatility, not an
expected-return forecast, Kelly allocation, or guaranteed loss bound.

Normal changes require `abs(target−current) ≥ max($25, 10%×abs(target))`.
A full exit or forced reduction bypasses this policy threshold; executable
quantity precision and the simulator's separate $10 fill minimum still apply.

Source: [target_notionals and should_rebalance](../../ml/hyperliquid_paper_policy.py).

## From desired exposure to an executable change

The runtime applies account constraints after the pool policy. Reserve-adjusted
available cash is `max(0, free_cash − 10%×initial_equity)`. A spot increase budgets
its cost with `1 + spot_fee_rate + 0.002`; the last term is a fixed planning buffer,
distinct from configured execution slippage. Perpetual target funding compares
required gross divided by account utilization with the receiver's equity.

A donor can contribute only its available surplus and collateral-safe room.
The transfer plan skips amounts/deficits below $100 and prevents a recipient from
redonating newly received cash in the same plan. Within a cycle, transfers are
applied before fills and committed in the same transaction; insufficient depth
can therefore leave transferred cash unused in the receiver.

After planned transfers, each account's target is clipped by its 80% equity
capacity after other positions. Spot targets also respect available purchase
cash. A second fill-size check accounts for fee and adverse execution costs on
increases. Reductions are processed before increases, and shared visible book
depth is consumed once across orders in the coin's plan.

The ledger independently rejects forbidden direction changes, borrowing for new
risk, and new perpetual exposure above its 1x collateral limit. Pure perpetual
reductions can proceed when inherited exposure/cash is already outside limits.
Neither the 80% target nor 1x ledger rule simulates exchange liquidation.

The policy detail flag `account_cash_constraints_applied=False` refers to the
pool-sizing stage. It does not establish that later runtime constraints were
nonbinding; the runtime does not rewrite that flag.

Sources: [PaperRuntime._plan_transfers and _trade_coin](../../ml/hyperliquid_paper_runtime.py),
[PaperLedger.execute_cycle](../../ml/hyperliquid_paper_ledger.py).

## Quotes, partial fills, fees and estimated funding

| Mechanism | Recorded behavior and limitation |
| --- | --- |
| Quote selection | Buy visible asks; sell visible bids. Candle/model prices are never substituted for execution. |
| Book eligibility | Identity, finite levels, uncrossed/unlocked prices, ≤45 seconds old and ≤5 seconds future-dated; forecast-driven books must be at least as recent as forecast publication. |
| Fill price | Walk visible depth, compute raw VWAP, then apply adverse 2 bps. No queue position or latency model. |
| Fill size | Floor to market precision; limited depth yields partial fills; below-$10 executable notional is skipped. Requested, rounded, executed and remainder quantities are distinct. |
| Fees | `abs(executed_quantity) × fill_price × configured_fee_rate`; no automatic real-account tier/discount inference. |
| Spot routes | Exact base/USDC metadata routing; BTC/ETH/ZEC map to UBTC/UETH/UZEC. Ambiguous routes fail rather than being guessed. |
| Transfers | Local USDC debit/credit, zero fee and no settlement delay; no real exchange transfer is submitted. |

A `filled` simulator status means the rounded request was filled; precision dust
can still make the original requested quantity differ from executed quantity.
Consumed raw levels are preserved; the slippage adjustment does not manufacture
extra depth. New public observations provide fresh depth next time.

Funding is checked no more often than every 300 seconds. For each historical
settlement, quantity is reconstructed from initial perpetual inventory plus
fills strictly before that settlement. The signed estimate is:

```text
funding_cashflow = −quantity_at_settlement × completed_perp_close_proxy × published_rate
```

The proxy must be the exact completed candle at the settlement's UTC hour.
Missing rates/proxies remain pending errors, not fabricated zero charges.
Settlement/account identities are deduplicated, including after a cursor write
failure or restart. Funding retains its settlement time and is booked in the
current ledger cycle; it is explicitly an estimate, not the exchange ledger.

Sources: [PublicPaperMarket and simulate_fill](../../ml/hyperliquid_paper_market.py),
[PaperRuntime._funding](../../ml/hyperliquid_paper_runtime.py).

## Failure, replay and stopping boundaries

Invalid, missing, stale, matured or excluded forecasts request zero managed
targets when usable quotes permit execution. A missing quote for a required
reduction preserves that leg and blocks risk increases in the coin's plan.
More severely, the market provider omits a market whose book fails validation:
if that market is held, the missing mark aborts the entire tick before its
per-symbol risk handling. There is no guaranteed last-mark valuation fallback.

Committed forecast IDs execute once. No-forecast cycles use a 15-minute bucket;
completed keys suppress ordinary retry attempts even after a partial/unfilled
result. Active stop or over-limit paths can use a 30-second risk key. Thus a
30-second polling cadence is not a promise to retry every stale exit every tick.

Stops request zero targets after a 3% entry-based adverse move, using inherited
entry for mirrored perps. They are polled, can gap, and can fail to fill. After a
stop fill, that account/coin targets zero for one forecast horizon from the last
stop fill; restart reconstructs this cooldown from committed fills.

Each ledger cycle uses `BEGIN IMMEDIATE` and commits transfers, funding, fills,
decisions, valuations and its cycle identity together, or rolls them all back.
Duplicate cycle IDs return committed results. The datastore lock excludes another
paper writer. SQLite remains authoritative if later JSON/Parquet export fails.

Graceful stop finishes the current iteration, exports journals and closes the
ledger. It neither continues risk checks nor automatically closes positions.
Shutdown export does not itself rebuild `performance.json`. Restart resumes the
same ledger, without replaying successful cycle IDs or reseeding.

Powder has a separate [execution runtime and ledger](POWDER_ACTIVATION.md).
Its view remains **Not connected** until actual observations exist; selecting
it cannot enable trading. Paper accounting is never used as real balances or
actual fills. Manual real-account mutations share Powder's account locks.

Implementation limits: the market module also fixes a 45-second book ceiling,
so relaxing the config alone cannot relax that limit. `read_status` creates its
control directory and opens the lock; use the UI adapter for a strictly read-only
artifact projection. See [runtime](../../ml/hyperliquid_paper_runtime.py),
[ledger](../../ml/hyperliquid_paper_ledger.py), and
[UI contracts](UI_AND_DATA_CONTRACTS.md).
