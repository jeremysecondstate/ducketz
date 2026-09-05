# Gameplan supervision update — September 4, 2026

The overnight Scheduled tasks now supervise fetching, training, and prediction
while they run. They inspect progress and errors, make focused repairs backed by
tests, and resume the failed stage when a restart is necessary. Quiet, healthy
training can run for hours. The deadline is 04:00 Pacific on the next exchange
session; Friday September 4 therefore targets Tuesday September 8.

## Scheduled tasks now saved

| Task | Pacific schedule | Responsibility |
|---|---|---|
| Loops Overnight Gameplan | Weekdays 17:05, exchange-calendar guard | Fetch/append, build directional data/models, evaluate saved Gameplans, train Strategy models, generate candidates, train/save next-session Gameplan; supervise the entire run |
| Loops Overnight Health Watch | Every ten minutes, including weekends | Adopt an abandoned run, investigate failures, and perform verified repair/recovery; no new weekend trading session is invented |
| Loops Gameplan Weekly Review | Saturday 09:00 | Evaluate saved Gameplans from September 4; retain pending forecasts |
| Loops Gameplan Stock Trader — Hourly | Weekdays 04:01–12:01 and 14:01–16:01 | Existing live stock consumption; unchanged |
| Loops Gameplan Stock Trader — 1 p.m. Transition | Weekdays 13:05 | Existing live stock consumption; unchanged |

Five schedules are active and five remain paused. The health watch reuses the
former paused daily-adaptation automation ID. The model, project, working
directories, and existing notification settings were preserved. The evening
operator and backup watch use a three-minute renewable ownership claim, renewed
at least once a minute, to prevent simultaneous repairs.

## What changed in the runner

- Thirty-second health records expose the stage, child process, CPU, memory,
  I/O, log growth, recent output/errors, and time left. Model fits also emit
  iteration progress, start/completion messages, warnings, and failures.
- Non-finite model loss fails the fit. A warning requires examination and does
  not automatically trigger a restart or bypass model assessment.
- Stage errors, launch failures, controlled stops, deadline expiry, and success
  receive final records tied to their stage report and logs.
- Controlled stop/recovery targets the owned process tree. Recovery refuses a
  living supervisor or reused process ID. A verified restart skips completed
  stages and preserves the original next-session deadline.
- Scheduled operators inspect progress at least once a minute, investigate
  errors immediately, and record repairs/tests in the run's operator notes.
  Healthy long fits are allowed; repeated unexplained failures are reported.

See the complete [operating procedure](../../NIGHTLY_GAMEPLAN.md).

## Saved predictions and Saturday review

Every published Gameplan from September 4 stays in cumulative evaluation history.
Advancing the latest Gameplan pointer cannot hide an older D+2–D+5 or weekly
forecast. Pending targets are revisited until mature data exists; already scored
forecasts retain their scores. Missing or corrupt saved evidence is explicit.
Evaluation is stored independently so a later training failure cannot erase it.

The first DATASTORE evaluation saved all 144 forecasts from September 4. At its
15:49 snapshot, 96 were mature but awaiting the post-close outcome data, and 48
had not matured; none was scored using unavailable data. September 5's Saturday
review covers September 4 only, with longer forecasts retained for later review.
It no longer invokes a paper ledger or creates risk proposals.

The docs, current command, diagrams, and Saturday instructions now describe this
workflow. Historical market data remains available for training.

## Earlier outputs

A dependency scan identified 1,102 earlier prediction runs not referenced by any
retained model, Gameplan, or execution record. Permanent deletion was rejected
by automatic approval review with only “blocked by policy.” A safer reversible
move succeeded: those runs (15.9 GB) are outside active prediction folders at
`C:\DATASTORE\retired-predictions\20260904`. The 1,046 earlier source runs
still referenced by retained evidence remain in place. No historical market
parquets, current Gameplan, execution receipts, or trading controls were removed.
All protected paths and current publication verifiers passed after the move.

## The 1 p.m. correction

The [source patch](gameplan-session-fix.patch) is prepared and validated, but
**not applied to the active trader**. It fixes the forecast's CLOSED label at
the regular close, treating that forecast as the following POST window. The
actual broker pause remains enforced. The proposal is based on the exchange's
regular close rather than a hard-coded 13:00 comparison.

Offline validation used September 4's saved Gameplan for every action hour from
04:00 through 16:00. At 13:06 it admits the six valid POST signals and chooses
PM order duration; the actual pause, session close, weekend, and holiday checks
still reject non-tradable times. No broker was contacted. See
[validation](gameplan-session-fix-validation.json) and
[reproduction script](validate_trader_proposal.py).

Schwab documents the actual pause in its
[extended-hours schedule](https://international.schwab.com/investment-products/extended-hours-trading).
The live schedules and existing entry deadlines were left unchanged.

## Verification and remaining production check

The focused regression suite passed **84 tests**. It covers saved forecast
maturity across pointer changes, immutable evaluation history, live error
visibility, real HGB/MLP fit progress, failed-stage receipts/resume, deadline
termination, controlled recovery, reused-PID protection, supervision ownership,
Strategy training/selection, and the existing Gameplan stock adapter.
The suite also emitted 1,582 existing joblib/NumPy deprecation warnings; these
were warnings, not failed tests. [Test output](supervision-tests.txt).

The full after-close production pipeline was not started during the market day.
Its first run with these supervision changes remains scheduled for 17:05.
No claim is made that tonight's provider fetch or training has already succeeded.
The current Gameplan, Directional publication, Strategy publication, and saved
evaluation all verified after the source/output changes.

[Recorded evidence](supervision-update-evidence.json) includes source checksums,
current schedule names/statuses, the computed deadline, and test results.
