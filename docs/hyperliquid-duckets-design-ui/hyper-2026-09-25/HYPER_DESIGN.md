# H.Y.P.E.R. — Paper and Powder interface direction

Current concepts — version 2:

- [Paper — compact header, running simulation](hyper-paper-command-center-v2.png)
- [Powder — matching compact layout, not connected](hyper-powder-command-center-v2.png)

Version 2 removes the entire large in-content title and subtitle band from
both environments. The global H.Y.P.E.R. navigation tab remains. Paper/Powder
subtabs now sit immediately below application navigation, and the recovered
height gives the chart and trading workspace more room.

Previous concepts — version 1, retained for reference:

- [Paper — original title band](hyper-paper-command-center.png)
- [Powder — original title band](hyper-powder-command-center.png)

The concepts use illustrative data and were created with the built-in image
generation tool; [PROMPTS-v2.md](PROMPTS-v2.md) preserves the revision prompts,
and [PROMPTS.md](PROMPTS.md) preserves the original prompt set. This document
supplies the exact labels and data bindings for a later implementation.

## Purpose and identity

Keep the top-level Duckets tab named exactly **H.Y.P.E.R.** alongside the
existing application tabs. Do not repeat H.Y.P.E.R. as a large title inside the
page. The supplied branding can optionally appear in an About view or tooltip,
but neither line belongs in a persistent in-content header:

> Habēre Yper- Prodictum Electrōnicum Rōboris
>
> “To have an ultra-productive electronic asset of strength”

The two subtabs immediately below application navigation are **Paper** and
**Powder**. Paper displays the
running simulation. Powder is the future live-money counterpart and uses the
same layout, account roles, columns, chart choices, and decision inspector.
Switching tabs changes the viewed operating environment; it is not an action
that enables live trading or converts simulated holdings into real positions.

The visual reference is
`docs/hyperliquid-duckets-design-ui/concept-a-portfolio-hype-command-center.png`:
deep navy background, subtly lighter panels, thin blue-grey borders, mint
selection states, compact typography, and dense but readable financial tables.
The concept images are design illustrations, not screenshots of an implemented
interface. Paper values may use a clearly dated illustrative snapshot; Powder
must remain visibly **planned / not connected** until its execution integration
exists.

## Shared screen structure

Use the supplied desktop composition, scaling to the app's available window
size. The arrangement should
remain usable at smaller widths through sensible column collapse rather than
shrinking every label. Keep essential operational status above the fold.

1. **Application navigation:** retain existing tabs and add H.Y.P.E.R. as the
   selected top-level entry.
2. **Mode row:** Paper/Powder subtabs sit immediately beneath application
   navigation. There is no intervening H.Y.P.E.R., Latin, or English title
   band. The right edge holds the environment badge, runtime state, and last
   portfolio observation time. Give the reclaimed vertical space to the chart
   and trading workspace rather than replacing the removed header with padding.
3. **Five summary metrics:** pool equity; P/L since start; gross exposure;
   fees; worst drawdown. Free cash/collateral belongs in account detail, and
   estimated funding appears in the cost breakdown under the chart.
4. **Three account cards:** Alex, Jeremy, Clear Pond, in that order, with role
   labels and identical internal structure.
5. **Main workspace:** approximately 21% width for the account rail, 51% for
   the chart and working tables, and 28% for forecasts and a persistent decision
   inspector. It should show a complete selected decision without forcing
   narrow, wrapped table cells.
6. **Operational strip below the mode tabs:** separate data, forecasting, training, and execution
   clocks. A green dot must refer to a specific healthy process or fresh source,
   not imply that every component has produced a new observation.

Paper uses a persistent **SIMULATED** badge. Powder preserves panel geometry
and uses **REAL MONEY · PLANNED** plus **Not connected** in the current concept.
The latter has no working execution control in this design stage. Mint can
identify selection and healthy connectivity in both environments; an amber
environment treatment helps distinguish Powder without recoloring its entire
interface. Red is reserved for losses, rejected actions, or risk problems.

## Summary labels and their sources

Paper's latest portfolio is available in
`C:/DATASTORE/hyperliquid/_paper/_runtime/status.json`. Performance summaries
in `performance.json` have their own `as_of_utc`; they can be older than the
current portfolio observation. The UI must preserve that distinction.

| Visible label | Existing Paper source or derivation | Important interpretation |
| --- | --- | --- |
| Pool equity | `portfolio.pooled.equity` | Current marked equity across the three virtual accounts |
| Paper P/L since start | `portfolio.pooled.total_pnl` | Change from opening marked equity, including costs; inherited gains/losses are not counted as new strategy profit |
| Return vs opening equity | `total_pnl / initial_equity` | Label as a return relative to the opening baseline, not a separately calculated time-weighted return |
| Free cash / collateral | `portfolio.pooled.free_cash` | Before additional strategy cash-reserve rules; do not call it fully deployable capital |
| Gross exposure | `portfolio.pooled.gross_exposure` and `/ equity` | Opposing long/short legs contribute separately; they do not cancel gross risk |
| Worst drawdown | `abs(performance.max_drawdown_fraction)` | Historical worst decline from a running equity peak, displayed as a positive loss magnitude, with the performance report's timestamp |
| Fees paid | `portfolio.pooled.fees` | Cumulative simulated fill fees |
| Funding · estimated | `portfolio.pooled.funding` | Signed cashflow using the current funding approximation; missing settlements are not silently treated as zero |

Do not label `net_realized_pnl` as Paper P/L. For example, in the coherent
02:30 UTC observation on 2026-09-26, pooled `total_pnl` was approximately
**−$42.32**, while `net_realized_pnl` was approximately **−$829.51**. Closing
mirrored perpetual positions realizes gains or losses accumulated before the
simulation began. Only the former is the appropriate headline for strategy
performance since opening.

A dated Paper concept can use this internally consistent snapshot:

| Metric | Illustrative 2026-09-26 02:30 UTC value |
| --- | ---: |
| Opening equity | $42,078.28 |
| Pool equity | $42,035.96 |
| Paper P/L since start | −$42.32 / −0.10% |
| Free cash / collateral | $36,023.15 |
| Gross exposure | $6,012.81 / 14.30% |
| Fees paid | $29.77 |
| Funding · estimated | $0.00 |
| Worst drawdown | 0.10% |

Use **Illustrative snapshot** near the concept's timestamp. Do not combine
these figures with a newer live equity figure while retaining the older P/L.

## Account cards

Each card contains a small avatar or initials, account name, role, equity in
large text, P/L since opening below it, then free cash/collateral and gross
exposure as two compact rows. A small exposure meter can show gross exposure
relative to marked equity. Its limits should identify the paper policy rather
than imply an exchange liquidation calculation.

| Account | Permanent role label | Paper interpretation |
| --- | --- | --- |
| Alex | Short perps | Nonpositive managed perpetual quantities; buys reduce shorts |
| Jeremy | Long perps | Nonnegative managed perpetual quantities; sells reduce longs |
| Clear Pond | Spot | Owned spot inventory and cash; no shorting or borrowing |

For the dated concept snapshot, account equity / Paper P/L were approximately
Alex **$5,825.08 / −$11.69**, Jeremy **$6,175.86 / −$6.70**, and Clear Pond
**$30,035.02 / −$23.92**. The account P/L already excludes net internal transfers.

Small inherited spot balances can exist in Alex or Jeremy. Their position
records have `passive: true`; mark these **Inherited · unmanaged** or hide them
behind a dust toggle. They must not make the account card appear to violate
its managed trading role. Paper does not currently simulate exchange
maintenance margin or liquidation prices, so those numbers must not appear
as if supplied by the paper ledger.

## Performance chart

The primary chart is **Paper P/L since start**, with an opening-baseline zero
line and options for **Equity** and **Drawdown**. Provide time selectors such
as **1H · 24H · 7D · All** and a pool/account selector. Use an honest axis and
tooltip values; a tiny early sample must not resemble a long profitable track
record through a heavily compressed or exaggerated display.

Data comes from the SQLite `equity` table or its exported Parquet. For the
pooled series, select rows where `account == "pooled"`; do not sum both the
three account rows and the already pooled row. Multiple valuations can occur
within a polling interval. For chart downsampling take the last observation
per bucket, never sum equity observations. Account P/L charts use their
transfer-adjusted `total_pnl`, avoiding false jumps when USDC moves internally.

The existing comparator is a flat cash return. If shown, label it **Cash
baseline**. There is no implemented same-inventory buy-and-hold counterfactual
or Sharpe ratio; the concept should not invent either. Trade and transfer
markers can be toggled. Fee and funding views belong in a secondary breakdown
or the inspector rather than overwhelming the main chart.

## Working tables

Use shared table tabs **Positions · Decisions · Fills · Transfers** beneath the
chart. A compact latest-actions preview can remain visible while the Positions
tab is selected. Account, asset, and qualification filters apply consistently.

**Positions:** account, market/type, side, quantity, average entry, current
mark, gross notional, and unrealized P/L. Distinguish spot marks from perpetual
marks. An inherited position's unrealized P/L is measured from its retained
entry; it is not necessarily P/L earned since the simulation started. Put
that explanation in the position tooltip and inspector.

**Decisions:** time, account, market, action, current notional, target notional,
P(not-down), model qualification, and concise reason. Holds and skips matter:
they explain why a fresh forecast did not cause a trade. Long underlying codes
such as `entry_deadband` should render as **Below entry threshold**; the raw
identifier can remain in details. Do not infer a cooldown solely from a zero
target; use the recorded stop history and deadline.

**Fills:** time, account, market, buy/sell or increase/reduce, executed quantity,
execution price, executed notional, fee, and qualified/research badge. Show
requested versus filled quantity in the inspector. A simulated partial fill
must not be rendered as if the full target were reached. `details_json`
contains execution information, consumed book levels, forecast ID, model ID,
source data run, probability, and policy reasoning.

**Transfers:** time, source, destination, amount, reason, and status. Paper
transfers are committed virtual ledger movements with no fee or settlement
delay. A count of transfers is not net capital inflow; pooled internal
transfers net to zero. Avoid charting transferred USDC as trading volume.

Qualified and research are model-evaluation labels, not profit/loss labels.
Use neutral outlined badges, for example **Qualified model** and **Research
model**. A profitable research fill is still research, and a qualified model
can lose money. Do not equate the 3 qualified / 11 research fill counts in the
example with wins and losses.

## Persistent decision inspector

The right panel follows the selected decision, fill, transfer, or position.
Its title identifies account, asset, and action. Default sections are:

- **Signal:** P(not-down), P(down), one-hour horizon, forecast availability time,
  and outcome time. Small per-model bars can show the four component scores.
- **Allocation:** current exposure → desired target → executable change,
  probability threshold, volatility estimate, confidence, relevant cap, and
  account funding limitation. Explain a constraint before showing raw formulas.
- **Execution:** requested and executed quantity, simulated VWAP, added
  slippage, fee, unfilled remainder, book timestamp, and fill time.
- **Provenance:** model qualification, model publication time, training-data
  cutoff, calibration cutoff, policy version, and expandable IDs. “Trained
  recently” must not imply that all recent candles were in the estimator's
  fitting partition; the current evaluation design deliberately reserves later
  observations.

For a transfer, replace signal/execution content with donor cash, donor reserve,
receiver requirement, amount, and ledger status. For a skip, make the skip
reason the first line rather than showing an empty order ticket. This is an
operations and review interface; an always-visible manual trade composer is
less useful here than the decision's explanation.

## Refresh clocks and operational truth

The intended UI refresh is approximately every five seconds. That means it
checks for newer local state; it does not create one-second market quotes or
new forecasts. Show distinct source timestamps and cadences:

| Stage | Current cadence | Appropriate UI wording |
| --- | --- | --- |
| UI state read | About 5 seconds | View refreshed … |
| Paper quote, mark, and risk cycle | About 30 seconds | Portfolio observed … / Paper loop running |
| Candle publication and prediction | Every completed 15-minute candle | Latest candle close … / Forecast available … |
| Candidate training | Hourly, based on source-candle progress | Model published … / Training in progress when a job exists |
| Parquet/performance exports | After changes, periodically, and on shutdown | Export as of … |

The UI should read the ledger in a read-only transaction for a current chart
and journal, or receive a suitable application projection. The Parquets are
exports and can lag the running paper portfolio. Do not label a source merely
**Live** based on a timer animation. Process state should come from the runtime
lock/status accessor, and stale observation times should be displayed even if
a saved JSON file still says `running`.

Paper status already distinguishes `last_error`, per-symbol `errors`,
`quote_errors`, and `funding_errors`. Present a compact actionable status such
as **ETH quote unavailable — last position retained in valuation** only when
the underlying behavior supports it. Missing required held-asset marks can
degrade a cycle; a stale number is not a fresh successful mark. The status
payload does not presently expose every raw quote timestamp, so a global
“book age 2s” label cannot be inferred from `updated_at_utc`.

Use local **PT** in the interface with exact UTC timestamps in tooltips. Avoid
hard-coding PST during daylight saving time. A planned next event can be shown
as an estimate, not a promise that training will finish exactly at that time.

## Powder: same geometry, different execution evidence

Powder mirrors the Paper composition, but every balance, position, fill,
funding item, and transfer must eventually come from the actual execution and
reconciliation layer. The currently available paper files cannot populate a
screen labeled real money as if they were real results. The design concept
uses placeholders and **Not connected** until that integration exists.

Future source distinctions:

| Panel | Powder evidence required |
| --- | --- |
| Equity and positions | Reconciled account balances and positions, with account abstraction understood and per-account timestamps |
| P/L since start | An explicit live opening baseline and cashflow-adjusted accounting; inherited historical P/L kept distinct |
| Decisions | The chosen live policy and forecast IDs, preserved separately from execution results |
| Order/fill journal | Exchange acknowledgement, exchange order ID, actual fills, partial quantities, cancellations, and rejections |
| Fees and funding | Actual exchange ledger amounts; estimate badges only while values remain estimates |
| Transfers | Requested, submitted, pending, confirmed, failed/rejected, and reconciled amounts as supported by the real transfer path |

An accepted order request is not a fill. A requested target is not an existing
position. A submitted transfer is not cash already available in the receiver.
The same columns and inspector should make those distinctions legible without
changing screen geometry. Source and mode identifiers should remain attached
to every record and export so a Paper action cannot appear in Powder's
confirmed activity.

Potential future live controls must reflect the actual integration's state.
For this concept, execution buttons are disabled and their label explains that
Powder is not connected. Paper can expose a graceful **Stop paper** control and
refresh-view action when wired to its existing runtime controls. Neither tab
should imply that stopping the process automatically closes positions.

## Scope of this deliverable

This brief and the companion concept images establish visual structure and
field meanings. They do not implement widgets, connect Powder, send orders,
change account balances, or alter the running data/model/paper processes.
