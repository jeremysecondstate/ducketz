# Gameplan workflow handoff

Current October 13 operating direction: every night Atlas and Scout each produce their own eleven-symbol Gameplan. Synthesize only those two Gameplans once with one actual cash and stock balance; return the same 22-symbol Gameplan and Stats to both PCs. Atlas may complete synthesis directly without a peer acceptance wait. Atlas runs the combined Gameplan trader and its own 15-minute trader; Scout runs only its own 15-minute trader. All three run and log independently. No cross-trader synthesis, reconciliation, verification, compatibility proof or approval is required. Nightly preparation is permanently authorized; the human manually starts each trader. See [independent traders](../development/independent-traders.md).

**October 11, 2026 navigation update:** the old filename does not define Loop C's
independent observer/risk scope. Current preparation is Stats first, with separate
local responsibilities, one Gameplan synthesis and return to both PCs. Follow
[Current system](CURRENT_SYSTEM.md); the brief earlier handoff below is historical
navigation, not authority to launch LOG or a trader. The weekly evaluator remains
useful even though Atlas's saved weekly task is paused.

Follow [NIGHTLY_GAMEPLAN.md](NIGHTLY_GAMEPLAN.md): after close, fetch and append
the completed session, evaluate saved forecasts, train, and save the next
session's Gameplan. The Scheduled operator supervises this work as it runs and
repairs/resumes failed stages when needed.

The daytime stock trader follows [STOCK_TRADER_AUTOMATION.md](STOCK_TRADER_AUTOMATION.md).
Saturday follows [WEEKLY_REVIEW_AUTOMATION.md](WEEKLY_REVIEW_AUTOMATION.md).
Earlier design prompts are not operating instructions.
