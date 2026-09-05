# Gameplan workflow handoff

Follow [NIGHTLY_GAMEPLAN.md](NIGHTLY_GAMEPLAN.md): after close, fetch and append
the completed session, evaluate saved forecasts, train, and save the next
session's Gameplan. The Scheduled operator supervises this work as it runs and
repairs/resumes failed stages when needed.

The daytime stock trader follows [STOCK_TRADER_AUTOMATION.md](STOCK_TRADER_AUTOMATION.md).
Saturday follows [WEEKLY_REVIEW_AUTOMATION.md](WEEKLY_REVIEW_AUTOMATION.md).
Earlier design prompts are not operating instructions.
