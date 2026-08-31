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

## Hourly live + shadow owner

At each scheduled wake, run exactly once from `C:\dev\ducketz`:

```powershell
.\.venv\Scripts\python.exe -m ml.stock_trader.runtime `
  --datastore-target pc `
  --execute `
  --queue-at-open
```

The runtime itself resolves the XNYS calendar. The opening-queue path is valid
only during the official pre-open and only for a prediction whose target starts
at that session's core open. During the day it submits only inside the XNYS core
session. Closed sessions publish observation evidence but submit nothing.

One invocation creates both lanes from the same prediction, account, working
orders, and quote snapshot:

- `LIVE`: thresholded and bounded orders eligible for Schwab submission.
- `SHADOW`: a lower-threshold, larger-cap challenger that is never submitted.

The submission loop explicitly filters for `decision_lane=LIVE`. All live and
shadow decisions, including `NO_TRADE`, are immutable and later paired to the
same receipt-verified Loop B market outcome.

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
