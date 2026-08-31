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

## Receipt-driven live + shadow owners

Loop B computes at `:05`/`:35`, but its receipt-verified publication normally
becomes authoritative later. The stock trader therefore wakes before the next
target and waits for publication authority instead of guessing from the wall
clock.

At the weekday opening wake (`06:17` PT), run exactly once from
`C:\dev\ducketz`:

```powershell
.\.venv\Scripts\python.exe -m ml.stock_trader.runtime `
  --datastore-target pc `
  --execute `
  --queue-at-open `
  --wait-for-actionable-prediction
```

At each weekday intraday wake (`07:47` through `11:47` PT), run exactly once:

```powershell
.\.venv\Scripts\python.exe -m ml.stock_trader.runtime `
  --datastore-target pc `
  --execute `
  --wait-for-actionable-prediction
```

The waiter resolves the next XNYS target from the same versioned exchange
calendar used by Loop B. It accepts only an unconsumed LIVE 1h prediction from
the checksum-verified current publication whose target is still ahead. The
expected fresh generation begins 25 minutes before that target. The waiter
polls every 15 seconds and stops 90 seconds before the target so account state,
quotes, sizing, and submission still occur before the predicted window begins.

An older receipt for the exact same future target is retained while waiting. If
the expected fresh generation has not promoted by the deadline, that still-
actionable receipt becomes the explicit `FALLBACK_ACTIONABLE_RECEIPT`; the
enrichment model receives its real `prediction_age_minutes`. Expired targets
are never fallbacks. A prior execution-requested LIVE decision consumes its
prediction ID, including a NO_TRADE decision, so later wakes cannot reuse it.

The runtime itself resolves the XNYS calendar. The opening-queue path is valid
only during the official pre-open and only for a prediction whose target starts
at that session's core open. During the day it submits only inside the XNYS core
session. A target more than 45 minutes away is not a near-term hourly target and
cannot keep the waiter alive or create an overnight scheduling accident.

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

Reconciliation may read order history and fills. It may not submit, replace, or
cancel anything.

Do not modify code, controls, models, scheduler definitions, unrelated orders,
or options from the scheduled task. Report the two command statuses and exit.

## Daily next-session adaptation

After the latest XNYS session and its final one-hour outcome have matured, run:

```powershell
.\.venv\Scripts\python.exe -m ml.stock_trader.daily_adaptation `
  --datastore-target pc `
  --live-adaptation-weight 2
```

The adaptation job:

1. audits that completed session, including its opening-queue decision;
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

## Current execution semantics

- BUY sizing uses equity, broker-reported non-marginable buying power, current
  exposure, quotes, and pending BUY orders.
- SELL sizing uses owned uncommitted shares and the same single-order equity cap.
- Existing option rows may reduce broker buying power and gross exposure but
  option-only metadata omissions cannot disable stock-order state.
- Market orders remain disabled; urgency may select passive, midpoint, or
  marketable limit orders.
- The model's protective price is recorded for evaluation. It is not presented
  as a broker-native protective child order because that submission capability
  has not been verified in this integration.
- Setting the operator switch to `FALSE` stops new submissions. It does not
  silently cancel existing working orders.
