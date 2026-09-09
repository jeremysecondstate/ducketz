# Scheduled overnight operations

The automation ID `loops-hourly-operations` belongs to `Loops Overnight Gameplan`,
which wakes daily at 21:05 America/Los_Angeles and starts the entire Loop A close
fetch and downstream workflow on completed exchange sessions. Native calendar
checks skip fresh weekend/holiday work. This follows XNAS.ITCH's normal 21:00
Pacific Historical release; actual required coverage is still checked. Follow
[NIGHTLY_GAMEPLAN.md](NIGHTLY_GAMEPLAN.md) for the complete start, active
supervision, repair, stop, recovery, resume, and verification procedure.
The Scheduled task stays with the run and reads errors as they appear.
`Loops Operations Watch` checks for abandoned or failed work every 30 minutes.
It may start missing fresh overnight work only after 21:15 Pacific. Existing
unfinished attempts retain completed stages and their original deadline on resume.
Healthy training can continue until 04:00 on the next exchange session.

The hourly daytime stock task consumes the saved Gameplan; it does not train
or create a new plan. See [STOCK_TRADER_AUTOMATION.md](STOCK_TRADER_AUTOMATION.md).
Saturday's [review](WEEKLY_REVIEW_AUTOMATION.md) evaluates Gameplans starting
September 4 and retains longer forecasts until they mature. No paper-ledger
refresh belongs to this workflow.
