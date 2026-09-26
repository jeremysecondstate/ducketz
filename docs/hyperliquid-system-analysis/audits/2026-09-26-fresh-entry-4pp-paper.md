# Fresh Paper mirror with broader entries

User-requested refresh on **2026-09-26**, starting with 54%/46% entry thresholds.
The user explicitly excluded the preceding Paper sample from analysis and asked
that it be deleted without an archive.

## Excluded sample and completed deletion

Gracefully stopped Paper PID 45828. The excluded sample is identified only by
seed `2026-09-26T10:35:59.867633343Z` / experiment
`20260926T103559Z-qualified-refresh`; do not use its results, fills, screenshot
observations or prior checkpoint for strategy evaluation or comparisons.

Automatic policy review rejected both the combined cleanup and an exact-path
recursive deletion command with **"blocked by policy"**, without further reason.
The old ledger, projections, checkpoint backup, experiment records, scratch
phase records and verification files were therefore moved outside active and
analysis directories to:

`C:/DATASTORE/hyperliquid/_pending_deletion/20260926T103559Z-excluded-paper`

**The user completed deletion.** The directory above was verified absent at
**2026-09-26 11:36:32 UTC**. It was a temporary cleanup location, not an analytical
archive. The sample remains permanently excluded from analysis and recovery;
the retained exclusion registry is
`C:/DATASTORE/hyperliquid/_operations/excluded-paper-runs.json`. No further cleanup
command is required. At that check the fresh Paper PID 72648 remained healthy
and all projected sources were fresh. Cleanup completion leaves operating
contract v5 unchanged.

No new analytical archive was created. The independent training-window
comparison used market/model data, not this Paper sample; it was preserved at
`C:/DATASTORE/hyperliquid/_model_research/20260926T103000Z-window-comparison`.
The two still-earlier archives and ongoing model/data histories are separate.

## Fresh opening

Fresh public account reads created a new seed at
**2026-09-26T11:30:32.118497133Z (04:30:32 PT)**:

| Account | Opening marked equity |
| --- | ---: |
| Alex | $5,879.211643 |
| Jeremy | $6,151.792708 |
| Clear Pond | $30,068.525626 |
| Pool | $42,099.529977 |

The mirror contains all nine inherited positions, including passive spot dust.
It uses current public balances/inventory and sequential marks; account reads
are not atomic. Paper then evolves independently, with simulated fees and risk
adjustments measured against the new opening baseline.

Policy **993e26589020a5a0** applies from the first decision: new long entry
P(not-down) >=54%, short entry <=46%, Qualified-only forecasts. The 52%/48%
retention bands, one-hour post-stop cooldown, size minimums, position limits,
fees and slippage remain in force. New inherited stop violations can therefore
cause fresh reductions/cooldowns; this reset does not abolish valid Hold/Skip
decisions. Their exact saved checks remain visible in the updated UI.

Only Paper was restarted, as PID 72648. Data/model workers continue without
retraining or model-history resets. Powder remains inactive. This refresh does
not change implementation or policy, so the prior 412-test verification applies;
fresh operations and ledger observations are checked separately.

Verification found cycles advancing 6 to 9, with committed observations from
11:31:04.354131 to 11:32:39.313788 UTC. All active journal timestamps belong to
the new seed, and all twelve initial decisions record the 54%/46% checks.
The nine opening positions reconstruct the account equity exactly; differences
from sequential source account summaries are under nine cents per account.
All 17 sources were fresh, without errors, warnings, duplicate workers or Powder.

New Paper ancestry is 72648 -> venv 57492 -> hidden cmd 73056 -> WMI. Both
launcher and cmd parents are outside Windows jobs. The seven initial fills
include fresh stop reductions; only this run's cooldown origins exist. Operations
Watch now uses contract v5 and this seed. Fresh-only proof:
`C:/DATASTORE/hyperliquid/_operations/fresh-entry-4pp-verification.json`.
