# Gameplan stock trader

The daytime trader reads the immutable Gameplan created before that action day.
It does not fetch training data, fit models, or replace predictions during the
04:00–17:00 America/Los_Angeles action window.

The active Scheduled tasks are `Loops Gameplan Stock Trader — Hourly` at
04:01–12:01 and 14:01–16:01 on weekdays, and `Loops Gameplan Stock Trader —
1 p.m. Transition` at 13:05. Both run the existing stock-only command:

```powershell
.\.venv\Scripts\python.exe -u -m ml.gameplan_stock_trader --datastore-target pc --execute --target-horizon 1h
```

They consume the current forward hourly forecast once and perform read-only
reconciliation afterward. An overlapping 4-hour forecast confirms the hourly
entry; an opposite actionable direction vetoes the entry. The 16:00 forecast
covers 16:00–17:00. At 17:00 there is no new forward entry window.

The universe is AAPL, AMZN, GOOG, MU, NVDA, and SNDK. The current trader uses BUY
and SELL of owned stock; options execution and short selling are disabled.
The separate persistent stock and Gameplan activation controls plus `--execute`
are required. Current account/quote evidence, exposure, cash, spread, order-size,
exact-once, target deadline, and actual broker-session checks still apply.
The Gameplan artifact itself has no order authority; only the separate trader
can consume it under these controls.

Schwab's actual session includes a five-minute pause after the regular close.
That pause does not justify rejecting the 13:00–14:00 forecast after trading
resumes. The September 4 audit found that the current adapter labels its
13:00 start CLOSED, which then fails the order-session and target-session checks.
A [proposed correction](audits/2026-09-04/gameplan-session-fix.patch) labels a
forecast starting at the exchange's regular close as POST. It leaves actual
broker availability checks intact and applies to the exchange's close rather
than a hard-coded 13:00 time. It is **prepared and tested, not applied to the
active trader**. The current five-minute entry grace (ten at 13:00) and live
schedules have not changed in this supervision update.

The offline [validation](audits/2026-09-04/gameplan-session-fix-validation.json)
checks all 13 hourly action slots using September 4's saved Gameplan without
broker contact. To inspect/apply the prepared source correction manually:

```powershell
git apply --check --ignore-space-change docs/loops-system-analysis/audits/2026-09-04/gameplan-session-fix.patch
git apply --ignore-space-change docs/loops-system-analysis/audits/2026-09-04/gameplan-session-fix.patch
```

Overnight acquisition, active training supervision, repair, and saved forecast
evaluation follow [NIGHTLY_GAMEPLAN.md](NIGHTLY_GAMEPLAN.md). Saturday's review
follows [WEEKLY_REVIEW_AUTOMATION.md](WEEKLY_REVIEW_AUTOMATION.md). Neither
Scheduled workflow executes the stock trader or grants options authority.
