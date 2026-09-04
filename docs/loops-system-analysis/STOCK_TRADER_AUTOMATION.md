# Stock Trader Automation Contract

This is the durable production contract for the autonomous six-symbol stock
trader. It is separate from the Loops monitor/trainer owner and never grants
options-trading authority.

## Scope

- Asset class: stocks only.
- Exact universe: `AAPL`, `AMZN`, `GOOG`, `MU`, `NVDA`, `SNDK`.
- Live instructions: `BUY` and `SELL` only.
- No `SELL_SHORT`, options, transfers, or per-order human confirmation.
- The persistent operator switch is
  `C:\DATASTORE\controls\stock-trader\operator-intent.txt`.
- `CONFIRM_ACTIVE_TRADING=TRUE` permits the checked-in runtime to submit;
  `FALSE`, missing, or malformed prevents every new submission.

Databento Standard is the live prediction-data entitlement, not the broker
clock. The configured `EQUS.MINI` source can supply live non-empty OHLCV and L1
events for all six direct equity symbols. Loop B admits completed source hours
from 04:00--20:00 Eastern (01:00--17:00 Pacific) so a prediction is ready before
the first Schwab-actionable PRE checkpoint. Schwab's actionable stock clock is
the narrower 07:00--20:00 Eastern clock below. A missing OHLCV minute means no
trade aggregate was emitted, not permission to future-fill a label. See the
[local Standard entitlement inventory](../databento-plan/databento_standard_plan_data_access.md),
[Databento live-data guide](https://databento.com/docs/portal/live-data), and
[Schwab extended-hours schedule](https://international.schwab.com/investment-products/extended-hours-trading).

## Receipt-driven live + shadow owners

Loop B computes at `:06`/`:36`, but its receipt-verified publication normally
becomes authoritative later. The stock trader therefore wakes before the next
target and waits for publication authority instead of guessing from the wall
clock.

At the weekday PRE-opening wake (`03:47` PT), run exactly once:

```powershell
.\.venv\Scripts\python.exe -m ml.stock_trader.runtime `
  --datastore-target pc `
  --execute `
  --target-horizon 1h `
  --queue-at-premarket-open `
  --wait-for-actionable-prediction
```

This is the sole owner of the 04:00 PT PRE-opening target. The runtime admits
the queue only for that exact target and emits an AM limit order; it cannot use
the flag to submit an unrelated target while the broker session is closed.

At the weekday opening wake (`06:17` PT), run exactly once from
`C:\dev\ducketz`:

```powershell
.\.venv\Scripts\python.exe -m ml.stock_trader.runtime `
  --datastore-target pc `
  --execute `
  --target-horizon 1h `
  --queue-at-open `
  --wait-for-actionable-prediction
```

At each weekday 1h wake (`04:47` through `15:47` PT), run exactly once. The
`05:47` wake owns the distinct `06:00` PRE target; the separate `06:17`
opening-queue task owns the `06:30` regular-open target:

```powershell
.\.venv\Scripts\python.exe -m ml.stock_trader.runtime `
  --datastore-target pc `
  --execute `
  --target-horizon 1h `
  --wait-for-actionable-prediction
```

At the four weekday 4h-checkpoint wakes (`04:17`, `08:17`, `12:17`, and
`16:17` PT), run the command with `--target-horizon 4h` and without an opening
queue flag. These wakes target the explicit `04:30 PRE`, `08:30 REGULAR`,
`12:30 REGULAR`, and `16:30 POST` Pacific checkpoints.

The waiter resolves the next XNYS target for the explicitly selected horizon
from the same versioned exchange calendar used by Loop B. It accepts only an
unconsumed LIVE primary
prediction from the checksum-verified current publication whose target is
still ahead. The nearest target wins, with 1h preferred only on an exact tie.
The waiter immediately accepts a checksum-valid, unconsumed prediction for the
exact target and horizon while it remains actionable. It polls every 15 seconds
only when no such prediction is available, and stops 90 seconds before the
target so account state, quotes, sizing, and submission still occur before the
predicted window begins.

Receipt age is descriptive rather than an acceptance barrier. A prediction
created before the expected generation window is accepted immediately as an
explicit `FALLBACK_ACTIONABLE_RECEIPT`, and the enrichment model receives its
real `prediction_age_minutes`. Expired targets are never fallbacks. A prior
execution-requested LIVE decision consumes its prediction ID, including a
NO_TRADE decision, so later wakes cannot reuse it.
Live submissions additionally reserve prediction ID, symbol, and lane in a
durable datastore-root SQLite ledger before the broker POST. That stable key
prevents a later hourly decision ID from reusing the generation even if a crash
loses ordinary receipt artifacts. Non-waiting live entry points enforce the
same exact-next-target, near-term, and consumption rules.

After prediction handoff, the runtime retries only the complete read-only
Schwab state snapshot (account/positions, working orders, and all six quotes)
when a failure is classified as transient. Each retry recaptures all three
inputs together after a fixed three-second pause. The retry-start window is
capped at 120 seconds and shortened when necessary to preserve a 15-second
pre-target decision/submission reserve. An in-flight Schwab request may finish
after that budget, but the fresh clock check still prevents a late decision or
submission. Before parallel reads, authentication and account identity are
initialized once so one failed initialization cannot fan out into simultaneous
calls. The runtime fingerprints that account/OAuth generation before the reads,
verifies it after the complete snapshot, and requires the prepared submission
to match it. A concurrent reauthorization therefore retries the read snapshot
or stops before POST rather than mixing or trading the wrong account. OAuth is
Schwab's authorization mechanism. A cache-lock timeout,
pre-connect timeout, or definite rate-limit response is retryable because no
token mutation was accepted; an ambiguous post-send/read timeout, malformed
success payload, or authorization failure is fail-closed and is not retried.
After any retry,
the runtime advances the decision clock, re-reads the operator switch, and
revalidates prediction actionability, the current PRE/REGULAR/POST window, and
time in force. If the
safe target boundary has been reached, it publishes
`PREDICTION_EXECUTION_DEADLINE_PASSED` and submits nothing.
The decision artifact and receipt store the broker-state attempt count,
transient-failure count, elapsed and fixed-delay time, final error type, and the
sanitized failing component (`authentication`, `account_identity`, `account`,
`open_orders`, or `equity_quotes`).

Token refresh is single-flight across threads and processes that share the
credential cache. The refresh transaction reloads the cache under an OS lock,
writes it by durable atomic replacement, and lets waiters adopt a token another
process already refreshed. On Windows, the cache lives in the user's protected
LocalAppData `Ducket` directory; other platforms use the user's state directory.
An old repository-local cache is never imported; if detected, the runtime stops
with instructions to retire it and reauthorize so its refresh token is rotated.
Before the OAuth POST it records a non-secret in-progress marker. An ambiguous
timeout or crash leaves that marker fail-closed so no waiter repeats an
ambiguously committed refresh; Schwab must then be authorized again.
A definite credential rejection records a separate reauthorization-required
marker, preventing every symbol or process from reposting the same rejected
refresh credential. A successful authorization-code exchange clears either
marker by replacing the cached credential generation.

The scheduled command is still invoked exactly once. “No retry” at the task
level prohibits launching a second runtime process; it does not prohibit these
bounded, idempotent Schwab GET retries inside that one process. Broker writes
are different: order submission, replacement, and cancellation are never
blindly retried because a timeout can leave the mutation outcome ambiguous.
Live execution cannot use a historical `--decided-at` clock. The runtime also
checks the wall clock, session, target deadline, and operator switch both before
and immediately after reserving each exact-once submission intent; a failed
post-reservation gate is recorded as `NOT_SUBMITTED_SAFETY_CHECK`.
Potentially blocking token/account preflight is completed first and frozen as a
matching account-hash/token context. A synchronous durable reservation keyed by
both decision ID and stable prediction generation is committed before POST.
The same gates and identity match run again inside the prepared submission path
immediately before the order POST.
PREMARKET reads cannot implicitly stage a REGULAR/DAY order: only the explicit
opening-queue modes may cross that session boundary.

The runtime itself resolves the XNYS calendar. On an ordinary full session the
stock execution clock is `PRE` 04:00--06:25 PT, `REGULAR` 06:30--13:00 PT, and
`POST` 13:05--17:00 PT. The two five-minute transitions are closed. The
opening-queue path is valid only before the official open and only for a target
at that open. Early-close sessions retain official core hours and do not invent
extended sessions. A target more than 45 minutes away cannot keep the waiter
alive or create an overnight scheduling accident.

One invocation creates both lanes from the same prediction, account, working
orders, and quote snapshot:

- `LIVE`: thresholded and bounded orders eligible for Schwab submission.
- `SHADOW`: a lower-threshold, larger-cap challenger that is never submitted.

The submission loop explicitly filters for `decision_lane=LIVE`. All live and
shadow decisions, including `NO_TRADE`, are immutable and later paired to the
same receipt-verified Loop B market outcome. The handoff status, wait duration,
source run and promotion clocks, fallback use, missing symbols, and consumed
prediction IDs are stored in the decision receipt and weekly audit.

After the runtime, run the read-only reconciliation exactly once:

```powershell
.\.venv\Scripts\python.exe -m ml.stock_trader.reconciliation `
  --datastore-target pc
```

Reconciliation may read order history and fills. It appends sanitized per-fill
quantity, price, and execution time alongside aggregate fill status, without
persisting broker order/fill identifiers. It may not submit, replace, or cancel
anything.

Do not modify code, controls, models, scheduler definitions, unrelated orders,
or options from the scheduled task. Report the two command statuses and exit.

## Daily next-session adaptation

At `17:20` PT on weekdays, after the complete PRE/REGULAR/POST stock day has
closed, run:

```powershell
.\.venv\Scripts\python.exe -m ml.stock_trader.daily_adaptation `
  --datastore-target pc `
  --live-adaptation-weight 2
```

The adaptation job:

1. audits that complete actionable stock day, including PRE, opening queue,
   REGULAR, and POST decisions;
2. pairs live and shadow explanations to receipt-verified market outcomes and
   reconciled fills;
3. deduplicates both lanes by Loop B prediction so the same realized path is not
   counted twice;
4. retains the deduplicated historical Loop B cohort as the statistical anchor;
5. gives each new unique trader observation weight two; and
6. publishes the checksum-verified enrichment model used by the next session.

One session is incremental evidence, not standalone statistical validation.
Weekly evaluation remains responsible for longer-run comparisons, reason/style
breakdowns, and paper-options review.

The Saturday stock audit also builds a local receipt-matched FIFO lifecycle
from reconciled LIVE fills. It links each matched entry and exit back to both
stock-trader decisions and Loop B predictions, reports gross realized P/L
before unavailable fees, and keeps open inventory explicit. This evidence is
always labeled `LOCAL_FIFO_STOCK_TRADER_FILLS_NOT_BROKER_TAX_LOTS`; it does not
replace Schwab's official statements, tax lots, fee accounting, or account
P/L, and it must not be blended with forward-window prediction economics.

## Current execution semantics

- BUY sizing uses equity, broker-reported non-marginable buying power, current
  exposure, quotes, and pending BUY orders.
- SELL sizing uses owned uncommitted shares and the same single-order equity cap.
- Existing option rows may reduce broker buying power and gross exposure but
  option-only metadata omissions cannot disable stock-order state.
- Market orders remain disabled; urgency may select passive, midpoint, or
  marketable limit orders.
- `PRE` uses Schwab `AM`, `REGULAR` uses `DAY`, and `POST` uses `PM`. A
  regular-session decision targeting the 13:05 PT POST boundary uses Schwab
  `EXT` so a limit can be entered before PM-only entry begins and remain active
  across the boundary.
- Every extended or seamless order is forced to `LIMIT`. A relative bid/ask
  spread above `0.5%` fails closed with `EXTENDED_SPREAD_TOO_WIDE`.
- All six symbols are in both the Databento watchlist and stock-trader universe.
  A symbol can still produce `NO_TRADE` when a usable quote or trading interest
  is absent; configured eligibility is not a guarantee of a fill.
- The model's protective price is recorded for evaluation. It is not presented
  as a broker-native protective child order because that submission capability
  has not been verified in this integration.
- Setting the operator switch to `FALSE` stops new submissions. It does not
  silently cancel existing working orders.
