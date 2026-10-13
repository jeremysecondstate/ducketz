# Stock execution runtime and retained legacy engine

Reviewed October 11, 2026 on Atlas against HEAD
`90704d21801f60426493bd6cc650a7fac38d4f61` and inventoried working-tree source.
This document retains useful execution contracts while separating the retired
Loop-B checkpoint deployment from the current Gameplan path. Detailed horizon
and holding rules are in [independent horizons](INDEPENDENT_STOCK_HORIZONS.md);
local saved schedules are in [stock automation](STOCK_TRADER_AUTOMATION.md).

## Scope

The stock runtime trades underlying shares using BUY, SELL and NO_TRADE. SELL
is bounded by actual available broker shares and outstanding commitments; no short-sale
or options-submission authority follows from this document. Named Loop C's
separate Strategy/risk scope must not be replaced by the stock runtime's name.

Research membership comes from each PC's configured watchlist. A combined
execution universe is explicitly bound to the accepted account plan and can
cover both research partitions without widening either producer's training
watchlist. Each immutable publication retains its own universe.

The supported ordinary Pacific broker windows remain distinct: PRE before the
regular open, REGULAR, then POST after the broker transition; current code
checks actual exchange/session availability. A forecast's reference time is
not proof that its later order is tradable. Extended-session limitations and
unsupported half days retain their existing checks.

## Current Gameplan execution

`ml.gameplan_stock_trader` dispatches to the independent session/runtime for
independent targets. The selected `gameplan-direction-current-market-v1` route
consumes saved instructions without repeating model research qualification as
another execution veto. It still enforces selected-source/deployment, account
role, controls, current portfolio/quote evidence, ownership and native order
limits. The older fixed-budget and qualified-enrichment strategies retain
different qualification and holding semantics.

The selected signal-driven policy accumulates bullish purchases and sells
eligible owned stock on bearish instructions. Forecast target expiry measures
accuracy and does not trigger automatic liquidation. Its original 1:2:3:4
allocation weights coexist with cash, symbol/gross exposure and order limits;
they do not guarantee filled share ratios. Bounded longer-horizon sales need
the exact [frozen fallback policy](CROSS_HORIZON_BEARISH_FALLBACK.md).

Accepted-plan execution nets due planned quantities against confirmed fills
and live reservations within their owning horizons. Future instructions wait;
past clock times alone do not discard an accepted residual during the supported
day. Unrelated manual trades are not fills for the plan. Confirmed rejections
require review, while unknown submissions remain committed. See
[catch-up](INDEPENDENT_STOCK_HORIZONS.md#accepted-plan-catch-up) and
[rejection recovery](CURRENT_SYSTEM.md).

Current October 13 operating direction: every night Atlas and Scout each produce their own eleven-symbol Gameplan. Synthesize only those two Gameplans once with one actual cash and stock balance; return the same 22-symbol Gameplan and Stats to both PCs. Atlas may complete synthesis directly without a peer acceptance wait. Atlas runs the combined Gameplan trader and its own 15-minute trader; Scout runs only its own 15-minute trader. All three run and log independently. No cross-trader synthesis, reconciliation, verification, compatibility proof or approval is required. Nightly preparation is permanently authorized; the human manually starts each trader. See [independent traders](../development/independent-traders.md).

## Operator switch

The existing stock switch and separate Gameplan-stock switch plus explicit
execution mode remain required. Missing, malformed or disabled controls are
inactive. This document deliberately makes no claim about their current local
values. The user retains those controls and any explicit hold.

The [manual launcher](../../Start-Gameplan-Trader.cmd) selects the Gameplan
policy and supported wait/session mode. A verified preopening worker holds its
session identity and updates local heartbeat without broker requests or entry
claims, then reads the selected plan after wake. It does not create a recurring
schedule. Nightly preparation does not start a trader. The human starts each independent worker manually.

## Planning versus actual execution

The Gameplan's capacity and direction quantities, price ranges and cash ledger
are conditional estimates bound to frozen inputs. They do not constitute
orders, broker reservations, fills or available sale proceeds. Combined planning
uses original forecasts and one snapshot rather than adding independently sized
plans. Current holdings are signal-driven, so old projected-expiry exits do not
describe the selected manual policy.

Live orders use actual quotes, available cash, eligible inventory and outstanding
orders. The manual policy includes its current ask/bid and wide-spread midpoint
handling; planning ranges never veto an order merely because a quote is outside
them. Exposure and submission freshness remain checked. See
[planning prices](NIGHTLY_GAMEPLAN.md#account-aware-trade-plan-review).

## Reservation, capture and recovery contracts

- Account/order identity is stable across decisions and separately checked
  against credential-generation changes during coherent capture/submission.
- Persistent native and account reservations precede submission. Acceptance is
  not a fill, and ambiguous writes retain their commitments.
- Exact terminal evidence releases only an unfilled quantity; elapsed time or a
  cancellation request alone does not do so. Filled sales create no invented
  cash credit before broker-usable cash is observed.
- Order evidence precedes a newer coherent portfolio snapshot. Unknown orders,
  are this worker's own recovery concern. External account movements change
  actual capacity without becoming cross-trader discrepancies.
- Bounded retry applies to classified idempotent reads. An ambiguous order
  submission/replacement/cancellation must not be blindly repeated.
- Worker health follows actual capture/cycle evidence. A living PID, heartbeat,
  successful separate diagnostic or empty no-signal cycle is insufficient to
  clear a prior unresolved broker failure.

Exact receipt fields, creation-time/lock checks, readiness and source binding
must be evaluated under the installed revision. This review read source and
tests; it did not capture an account, reconcile a live ledger or start a worker.

## Legacy hourly critical path

The retained `ml.stock_trader.runtime` engine uses Loop B checkpoint publications,
the older learned-enrichment inference, live/shadow decision lanes and durable
prediction-generation uniqueness. It waited for one exact actionable target,
captured coherent account/order/quote evidence, produced the decision, reserved
the identity and rechecked controls/clock/account immediately before submission.

These mechanisms explain older receipts and remain implemented dependencies.
Their former one-process-per-checkpoint schedule and global “never backfill a
missed hour” description are not the current accepted-plan catch-up contract.
Do not restart a paused checkpoint stack to exercise the shared risk engine.

## Legacy and qualified-enrichment contract

The old learned strategy considered actionable direction, other horizon context,
spread/volume, cash/exposure, inventory, age and time. Its multi-head outputs
included action-worth probability, allocation, expected net return, downside,
urgency and holding time. Deterministic arithmetic constrained those outputs to
cash, share and risk availability; it did not invent a larger learned allocation.

This remains useful model/runtime provenance and supports the explicit retained
qualified-enrichment mode. It does not describe the selected manual strategy's
quantity rule or imply a learned model is qualified because training completed.
Historical bootstraps and daily-adaptation schedules retain their original
receipts; no new fitting is authorized here.

## Decision-to-reality audit

Older weekly audits join saved decision/prediction identity to mature outcomes
and preserve taken and abstained decisions. Counterfactual forward-window
returns and hypothetical quantities are separate from exact reconciled broker
fills. A local FIFO lifecycle can join matched system BUY/SELL fill receipts,
retaining unmatched holdings and pre-existing/manual inventory explicitly.

`LOCAL_FIFO_STOCK_TRADER_FILLS_NOT_BROKER_TAX_LOTS` means exactly that: it is not
broker tax-lot selection, a fee-inclusive account return or an official tax
record. The September 3 attribution audit remains historical evidence, not a
claim of current profitability. Exact local trades and receipt paths remain in
the privately archived original; shared text does not restate account details.

## Legacy deployment reference

Earlier premarket/opening/hourly/four-hour stock tasks and daily adaptation were
retired or repurposed. The historical fixed-budget independent worker and
separate 13:05 launcher also have different scheduling/holding semantics.
Atlas's October 11 Windows stock-session registration is disabled; inspect
Scout independently. Current saved native tasks, installed helpers and observed
processes must be reported separately.

The September 4 prepared session-label patch remains a
[historical patch](audits/2026-09-04/gameplan-session-fix.patch) and
[offline validation](audits/2026-09-04/gameplan-session-fix-validation.json).
The inspected legacy `checkpoint_session_for_target` still delegates directly
to the calendar, while the current manual engine checks the actual execution
window. The old patch's existence is not proof it was installed or that today's
consumer needs it. Do not apply it as a documentation-cleanup step.

See [RELEVANCE_REVIEW.md](RELEVANCE_REVIEW.md) for the exact original Git
reference, local archival evidence and unresolved operating findings.
