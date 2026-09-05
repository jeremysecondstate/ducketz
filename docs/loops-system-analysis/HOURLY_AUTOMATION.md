# Scheduled overnight operations

The automation ID `loops-hourly-operations` belongs to `Loops Overnight Gameplan`,
which starts at 17:05 Pacific on exchange-session weekdays. Follow
[NIGHTLY_GAMEPLAN.md](NIGHTLY_GAMEPLAN.md) for the complete start, active
supervision, repair, stop, recovery, resume, and verification procedure.
The Scheduled task stays with the run and reads errors as they appear.
The health watch checks for an abandoned or failed run every ten minutes.
Healthy training can continue until 04:00 on the next exchange session.

The hourly daytime stock task consumes the saved Gameplan; it does not train
or create a new plan. See [STOCK_TRADER_AUTOMATION.md](STOCK_TRADER_AUTOMATION.md).
Saturday's [review](WEEKLY_REVIEW_AUTOMATION.md) evaluates Gameplans starting
September 4 and retains longer forecasts until they mature. No paper-ledger
refresh belongs to this workflow.
