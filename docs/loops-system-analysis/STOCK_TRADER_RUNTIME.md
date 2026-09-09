# Configured-universe stock trader

Status: the legacy Loop-B schedules remain paused as of 2026-09-04. The active
daytime stock entry point is `ml.gameplan_stock_trader`, a thin gameplan adapter
over this proven risk/execution runtime. It consumes frozen forward routes and
requires both the legacy stock switch and the separate gameplan-stock switch.
The paper-only `ml.gameplan_executor` remains available for inspection. Neither
entry point grants options-order authority.

September 8, 2026 selected strategy: COST activation is complete. All seven stocks
and all 133 execution windows in Gameplan `20260908T093314.374067Z` have promoted
forecast authority within the 168-row publication. The daily forecast retains
the verified same-day champion `20260908T085844.073361Z`; the failed challenger
remains research. The scheduled independent session worker starts at 03:55
Pacific with explicit `--sizing-policy fixed-horizon-budget-v1`, using conservative
deterministic 1:2:3:4 budgets within the existing risk limits. The learned sizing
run `20260908T092028.062383Z` remains research with zero qualified scopes and is
nonblocking for that selected strategy. In that September 8 publication, execution
forecasts were bearish or neutral and produced zero qualifying BUY entries.
Its exact schedule, ownership rules and forecast qualification requirements are documented
in [Independent stock horizons](INDEPENDENT_STOCK_HORIZONS.md).
The remaining sections describe the shared legacy engine and its preserved
contracts; the independent session worker is the active scheduled entry point.

Independent preparation selects `xnas-itch-archive-v1` explicitly. The native
`stock_target_history` stage requires a $0 preflight and verifies committed
history before source-correct evaluation. Gameplan publication writes four
checksum-bound training cohorts, and the supervised enrichment tail consumes
that pinned source. The required `gameplan_trade_planning` stage follows
enrichment and writes a separate immutable account-aware review under the same
original 04:00 Pacific deadline. EQUS.MINI live features and legacy labels keep their existing
identity; no price-source mixing or regular-session label substitution occurs.
The $0 native historical backfill completed 35 chunks and approximately 1.79
million minute rows. Forecast source, exact target, promotion and entry-window
checks remain mandatory. Fixed budgets use `min(0.5, max(0, 2*p - 1))` of each
capped horizon amount, with a minimum bullish forecast probability of 0.54.
They produce policy audit fields, not fabricated learned probabilities or
expected returns. The CLI default `qualified-enrichment` keeps its separate
model gate; deployment selects fixed budgets explicitly. The September 8 broker/ledger
readiness snapshot recorded zero working orders and zero owned horizon allocations;
manual one-share holdings in the original six symbols were untouched. Preparation
and evaluation place zero orders.

The current review puts **Direction Based Trade Qty** beside the standalone
**Projected Trade Quantity** capacity estimate. The direction plan uses fresh
cash and all seven current stock balances, approved Bullish >=54% / Bearish <=46%
probabilities and Neutral zero. At each hour one shared cash ledger processes
eligible bearish sales, due horizon exits, then bullish purchases. Rows show
post-hour cash and shares; the hourly/EOD tables preserve later horizon holdings.
The conditional scenario may sell unallocated held shares, while protecting
pending sells and other horizons. It neither shorts nor changes actual ownership.

The scheduled-default long-only policy keeps its confidence-weighted entry
preview in expandable details. The manual `gameplan-direction-current-market-v1`
policy uses the same promoted directions: bullish buys with actual cash,
bearish sells eligible current shares, and neutral holds. Explicit sell
reservations may assign unallocated manual shares, excluding pending sells and
other horizons; only observed broker fills change filled ownership. It never
spends hypothetical or unconfirmed sale proceeds. Capacity estimates remain independent opportunities rather than
simultaneous orders. Working prices use the historical median +/-20bps, not a
confidence interval; projected cash is before fees/taxes and depends on assumed
fills. See [the full projection contract](NIGHTLY_GAMEPLAN.md#account-aware-trade-plan-review).

**Planning price and cash ranges are estimates only and never execution gates.**
The manual policy reprices BUY limits from the current ask and SELL limits from
the current bid, rounding to the permitted tick. It recalculates whole-share
quantity from actual cash, holdings and outstanding orders. A market price or
cash balance outside the overnight range does not itself prevent an order.

For one-action manual use, launch
[`Start-Gameplan-Trader.cmd`](../../Start-Gameplan-Trader.cmd). This enables both
stock controls and starts the explicit Gameplan policy. Before the next
supported 04:00 Pacific session the worker stays `SLEEPING_UNTIL_OPEN`, with a
30-second local heartbeat and no broker, model, inventory or entry-slot activity.
It automatically reads the current Gameplan and wakes at 04:00; the first entry
batch is 04:01. There is no wake-time prompt or second manual activation.
Turning either control off stops the wait. Starting during an open session
starts immediately; weekends, holidays, unsupported half days and DST follow
the existing calendar. The process requires a running computer and ends at
17:00 after its selected session. The 03:55 Scheduled launcher recognizes and
adopts an already-running manual worker; its own fixed-policy default is unchanged.

Day 1 is the completed source session and the upcoming session is Day 2; daily
and weekly labels include actual dates while source IDs and windows remain
unchanged. Price bands use the available same-source history when at least two
observations exist. The main review retains cash, shares, investment and price
times, while sample-status and source details remain in supporting artifacts.

The daily development pool includes regularized logistic candidates selected
before assessment. New directional qualification policy v2 permits Brier score
up to 0.005 and log loss up to 0.01 above their training/selection baselines,
while retaining calibration, information, sample and exact-history requirements.
Published scores, measured baseline comparisons and the adopted policy stay
visible in model evidence. Qualification within an error allowance does not
establish measured baseline outperformance; older immutable runs keep their own
strict policy. Learned sizing assessment remains separate.

Selected scheduled command:

```powershell
.\.venv\Scripts\python.exe -u -m ml.gameplan_stock_trader --datastore-target pc --target-horizon all --sizing-policy fixed-horizon-budget-v1 --run-session --execute
```

Both stock controls remain required. Owned exits are checked before new entries;
entry batches occur at HH:01, except 13:06 after the broker transition. Cash,
gross/symbol exposure, single-order and six-order batch caps, whole shares,
spread limits and LIMIT orders remain enforced. Learned enrichment continues as
research/shadow work rather than being relabeled qualified.

## Scope

The runtime trades underlying shares only for `AAPL`, `AMZN`, `GOOG`, `MU`,
`NVDA`, `SNDK`, and `COST`. Its broker vocabulary is `BUY`, `SELL`, and `NO_TRADE`.
It neither constructs nor submits option instruments, and a SELL is capped at
owned shares not already committed to working sell orders. Short selling is
not part of this runtime.

Options remain a separate paper-only research lane. Any future Loop C options
shadow trader must use 1d/1w-or-longer strategies, separate receipts and
outcomes, and no Schwab options submission path.

The stock day is explicitly broader than the official regular session:

| Checkpoint session | Pacific window on an ordinary XNYS day | Schwab route |
|---|---:|---|
| `PRE` | 04:00--06:25 | `AM` limit |
| `REGULAR` | 06:30--13:00 | `DAY` |
| `POST` | 13:05--17:00 | `PM` limit |

The five-minute AM/core and core/PM transitions are closed. Early-close days
remain core-only because Schwab does not promise ordinary extended sessions on
those dates. A core decision targeting the first POST checkpoint uses the
`EXT` seamless limit route so it can be entered before PM-only entry opens.
Extended/seamless orders are always limits and fail closed when the relative
bid/ask spread exceeds `0.5%`.

Databento and broker clocks are separate. Standard-plan `EQUS.MINI` supplies
live non-empty OHLCV for the configured prediction watchlist; Loop B accepts
completed source hours from 01:00--17:00 Pacific (04:00--20:00 Eastern). That
extra source context prepares the 04:30 PRE prediction but does not authorize a
Schwab order before 04:00 Pacific.

## Legacy hourly critical path

1. Read the persistent operator switch.
2. If inactive, publish an inactive receipt and make no Schwab request.
3. Resolve the next XNYS target and wait for its unconsumed checksum-verified
   Loop B publication until 90 seconds before that target.
4. Prefer the expected fresh generation; if it is delayed, retain only an older
   still-actionable receipt for that exact target as an age-aware fallback.
5. Re-read the operator switch so a `FALSE` toggle during the wait stops before
   any Schwab request.
6. Load the current enrichment model, then fetch account/positions, working
   orders, and all configured stock quotes concurrently. A classified transient failure
   recaptures that complete read-only snapshot after a three-second pause, for
   up to a 120-second retry-start budget when the target leaves enough safe
   time. An already-running Schwab request may finish beyond that budget, but
   it cannot bypass the later clock gate. Non-transient authentication,
   payload, and validation failures fail immediately. Authentication and
   account identity are initialized once before the parallel fan-out. A
   non-secret account/OAuth-generation fingerprint is verified again after all
   three reads, so a concurrent reauthorization makes the whole snapshot retry
   instead of mixing two accounts. OAuth
   cache-lock contention, pre-connect timeouts, and definite rate limits are
   retryable; ambiguous post-send/read outcomes are not. Refresh is serialized
   across threads and processes, the shared token cache is durably replaced,
   and an in-progress/uncertain marker prevents a waiter from repeating an
   ambiguous OAuth mutation. That state requires fresh Schwab authorization.
7. After broker-read recovery, advance the decision clock, re-read the operator
   switch, and revalidate prediction actionability, session, and time in force.
   Retry time is shortened to retain 15 seconds before the target; reaching that
   boundary publishes `PREDICTION_EXECUTION_DEADLINE_PASSED` with no order.
8. Run one multi-head enrichment inference per symbol from the same snapshot.
9. Jointly convert model allocations into feasible whole-share quantities.
10. Publish the complete immutable configured-universe LIVE lane and SHADOW
   challenger from that same snapshot.
11. If deployment execution is enabled, prepare and freeze a matching Schwab
   token/account context, require its fingerprint to match the captured
   snapshot, and re-run all safety gates. Before each POST, durably reserve both
   the decision ID and the stable prediction-generation/symbol/LIVE identity in
   a synchronous SQLite ledger at the datastore root. Re-run the gates and
   identity check inside the prepared submission immediately before its POST.
   Reconciliation and outcome evaluation are outside the pre-submit critical
   path.

There is no repeated confirmation ceremony or sequential checksum/reload chain
between a published eligible decision and submission. Integrity and duplicate
suppression are implemented by publishing the decision before mutation, then
committing the datastore-root reservation ledger before creating the readable
per-decision intent artifact. The durable prediction-generation uniqueness
survives a restart even if ordinary decision or execution artifacts are lost.

The scheduler still starts exactly one runtime process per checkpoint. Bounded
internal retries apply only to idempotent Schwab reads. An order submission,
replacement, or cancellation is never automatically repeated after an error or
timeout because the broker-side outcome may be ambiguous.
Broker-state metadata in both the decision artifact and receipt records the
attempt count, retry wait, elapsed time, error type, and sanitized failing
component without storing account identifiers or response payloads.
Non-waiting live invocations apply the same exact-next-target, near-term, and
already-consumed prediction checks as the scheduled handoff path.
Historical `--decided-at` timestamps are dry-run-only. For live execution, the
operator switch, wall-clock target deadline, and market session are checked
before and immediately after each exact-once intent reservation, directly ahead
of the Schwab POST. A reserved intent stopped by that final gate receives a
terminal `NOT_SUBMITTED_SAFETY_CHECK` result.
Prediction checkpoint eligibility is checked against the current session, so a
PREMARKET run cannot implicitly queue a REGULAR/DAY order without the explicit
opening-queue flag.

## Legacy and qualified-enrichment contract

The nearest actionable Loop B 1h or 4h checkpoint is the primary direction
input, with 1h preferred only on an exact target-time tie; the other 1h/4h and
1d/1w probabilities provide context. The enrichment model also sees
spread, volume, cash/equity, current and pending symbol exposure, gross
exposure, held shares, day P/L, prediction age, time of day, and exact symbol.

The model emits:

- probability that an action is worth taking;
- target allocation/liquidation fraction (the primary order-size control);
- expected net return after waiting, spread, slippage and costs;
- adverse return and protective distance;
- execution urgency, limit aggressiveness, and expected holding time.

Deterministic arithmetic only enforces reality: available cash, gross and
per-symbol capacity, whole shares, current working orders, and available owned
shares for a SELL. It cannot invent a larger allocation than the model emits.

Order style is selected from urgency:

- low: passive limit;
- moderate: midpoint limit;
- high: marketable limit;
- very high: market only when the versioned policy explicitly permits it,
  otherwise a marketable limit;
- weak expected value: no order.

Every branch stores a stable reason code and plain-language explanation.

## Operator switch

The production location is:

`C:\DATASTORE\controls\stock-trader\operator-intent.txt`

It must contain exactly one nonblank line:

```text
CONFIRM_ACTIVE_TRADING=FALSE
```

or:

```text
CONFIRM_ACTIVE_TRADING=TRUE
```

Missing, unreadable, malformed, or `FALSE` means inactive. `TRUE` permits the
deployed trader to act without per-order human intervention. The runtime also
requires the deployment command's `--execute` flag, preventing an undeployed
developer invocation from becoming active merely because the persistent
production switch is true.

The production switch is currently `TRUE`. Changing it to `FALSE` prevents new
submissions without changing the checked-in deployment or silently cancelling
already-working orders.

## Commands

Non-mutating decision run:

```powershell
.\.venv\Scripts\python.exe -m ml.stock_trader.runtime `
  --datastore-target pc `
  --target-horizon 1h
```

Regular-opening deployment command (still inert unless the operator switch is
`TRUE`):

```powershell
.\.venv\Scripts\python.exe -m ml.stock_trader.runtime `
  --datastore-target pc `
  --execute `
  --target-horizon 1h `
  --queue-at-open `
  --wait-for-actionable-prediction
```

PRE-opening deployment command:

```powershell
.\.venv\Scripts\python.exe -m ml.stock_trader.runtime `
  --datastore-target pc `
  --execute `
  --target-horizon 1h `
  --queue-at-premarket-open `
  --wait-for-actionable-prediction
```

Weekly paired audit for the latest completed XNYS week:

```powershell
.\.venv\Scripts\python.exe -m ml.stock_trader.audit `
  --datastore-target pc
```

Read-only reconciliation of prior submissions (run after the critical path or
on the following wake):

```powershell
.\.venv\Scripts\python.exe -m ml.stock_trader.reconciliation `
  --datastore-target pc
```

This makes one bounded recent-order-history read and appends immutable broker
status, filled quantity, average fill, remaining quantity, fill count, and
sanitized per-fill quantity/price/execution-time snapshots to existing execution
events. It never persists broker fill/order identifiers and never cancels,
replaces, or submits an order.

Model fitting is deliberately separate from hourly inference and requires at
least 40 mature paired observations by default:

```powershell
.\.venv\Scripts\python.exe -m ml.stock_trader.training `
  --datastore-target pc
```

The bootstrap deduplicates repeated Loop B publications to the final
prospective prediction for each natural symbol/target window. The initial
production cohort contains 212 rows across 36 independent hourly target
windows and 8 sessions. Training publishes a new model artifact but never
changes the operator switch or submits an order. It uses both taken and
abstained decisions with mature counterfactual outcomes so NO_TRADE behavior
remains measurable.

After the 17:00 PT actionable stock close, the daily adaptation command audits
the PRE/REGULAR/POST live and shadow lanes, deduplicates them by Loop B
prediction, and publishes the model for the next session. New unique
observations receive weight two while the historical cohort remains the anchor:

```powershell
.\.venv\Scripts\python.exe -m ml.stock_trader.daily_adaptation `
  --datastore-target pc `
  --live-adaptation-weight 2
```

## Decision-to-reality audit

Each hourly decision has a stable `decision_id`. The weekly audit joins its
Loop B `prediction_id` to the receipt-verified mature evaluation and stores the
decision explanation beside:

- the observed forward raw return;
- the BUY/SELL-direction-aligned net return after the registered cost;
- selected and hypothetical quantity result dollars;
- expected-value error; and
- submission status when one exists.

When reconciliation has observed fills, the pair also includes exact filled
quantity, weighted-average entry fill, midpoint slippage, broker status, and a
conservative fill-slippage-adjusted result. Pending/unfilled orders remain
explicit rather than being treated as trades.

Thus a record such as
`WEAK_EXPECTED_VALUE_AFTER_WAITING_AND_SLIPPAGE -> NO_TRADE` remains directly
paired with what the market subsequently did. JSON contains the full pairs;
Markdown contains a compact row-by-row audit table and grouped reason/order-
style performance. It also contains the receipt-handoff status and reports
fallback decisions separately, allowing fresh-versus-fallback performance to
be evaluated from the same mature outcomes. JSON additionally groups counts
and mature results by `PRE`/`REGULAR`/`POST` checkpoint session and by exact
Loop B target-definition version, so the broadened contract is not silently
blended with legacy regular-only evidence.

Exact broker fill reconciliation is labeled separately from the midpoint
counterfactual. The audit now also pairs receipt-matched BUY and SELL fills FIFO
by symbol across the complete immutable reconciliation history. Each closed
local lifecycle retains both decision IDs, both Loop B prediction IDs, entry and
exit sessions/timestamps/prices, matched quantity, holding time, and gross
realized P/L before unavailable broker fees. Unmatched buys remain explicit
open tracked inventory; unmatched sells remain explicit rather than being
silently forced into a round trip.

This lifecycle is labeled
`LOCAL_FIFO_STOCK_TRADER_FILLS_NOT_BROKER_TAX_LOTS`. It is more granular than
the prior forward-window estimate, but it is not Schwab's official lot selection,
fee-inclusive account P/L, or tax record. Schwab statements remain authoritative
for those claims, and the audit never blends local FIFO realized P/L with the
prediction counterfactual.

**Observed 2026-09-03 07:46 UTC:** the first production audit under this
contract verified 82 decisions, with 46 mature prediction pairs and 24 pending.
Eleven sanitized LIVE fill records contained two system BUY fills totaling 25
still-open shares and nine SELL fills totaling 101 shares with no earlier
receipt-matched system BUY lot. The FIFO result therefore correctly reported
zero closed system-owned round trips and left those sells attributed to
pre-existing/manual inventory rather than inventing realized P/L. This is an
attribution-integrity check, not a profitability conclusion. Evidence:
`C:\DATASTORE\ml\stock-trader-weekly-audits\20260903T074629.212357Z\receipt.json`.

## Legacy deployment reference

The five stock schedules below are all paused. They describe the superseded
live/shadow deployment and do not authorize a run:

- `Loops Stock Trader — Premarket Opening` wakes at 03:47 PT, waits for the
  04:00 PRE-opening receipt, and may queue only that exact AM target.
- `Loops Stock Trader — Opening Live + Shadow` wakes at 06:17 PT on weekdays,
  waits for the opening-target receipt, and may use the explicit opening queue.
- `Loops Stock Trader — Live + Shadow` wakes hourly from 04:47 through 15:47
  PT. Its 05:47 wake owns the distinct 06:00 PRE target; the separate 06:17
  opening task owns the 06:30 regular-open target.
- `Loops Stock Trader — Four-Hour Checkpoints` wakes at 04:17, 08:17, 12:17,
  and 16:17 PT for the 04:30, 08:30, 12:30, and 16:30 targets.
- `Loops Stock Trader — Daily Adaptation` runs at 17:20 PT on weekdays and
  publishes only after that day's complete actionable stock window closes.

The runtime's XNYS calendar blocks holidays, five-minute transitions, and
extended execution on early-close days. All six configured symbols participate
in every eligible wake, but an absent usable quote, excessive spread, lack of
trading interest, or ordinary risk gate remains an explicit `NO_TRADE` rather
than a promise of a fill.

The exact scheduler contract is
`docs/loops-system-analysis/STOCK_TRADER_AUTOMATION.md`. A production-root dry
run on 2026-08-31 published six LIVE plus six SHADOW decisions, selected live
orders, and submitted zero orders as expected without `--execute`.
