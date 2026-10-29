# Retired Gameplan nightly workflow

Jeremy retired the Gameplan Trader and its nightly pipeline on October 29, 2026.
The former DATASTORE -> Gameplan Stats -> Gameplan model review/training -> local
Gameplan -> display/export -> synthesis/delivery chain is no longer an operating
workflow. Its previous permanent recovery and completion instructions are
superseded. Retired native tasks and the Gameplan dispatcher remain paused or
disabled; missing historical outputs are not a reason to restore them.

Current work is documented in [Nightly 15-minute operations](nightly-operations.md)
and [15-minute model supervision](intraday-nightly-supervision.md). Data-only
catch-up and the model supervisor have separate responsibilities and preserve
each PC's saved data/model recipe. Neither depends on Gameplan Stats or a combined
plan. The existing independent 15-minute trader retains its activation, stop and
order-recovery rules.

Legacy Gameplan modules, saved configurations, receipts and publications may
remain for historical reference and compatibility. Their presence does not
authorize execution, resumption, new scheduling or recreation of intentionally
removed Gameplan outputs. Preserve retained original history and private data;
shared-source coordination continues under its existing separate procedures.
