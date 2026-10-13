# Combined Gameplan and independent traders

Jeremy's October 13, 2026 standing direction defines three independent traders. Atlas runs the combined 22-symbol Gameplan trader and Atlas's local eleven-symbol 15-minute trader. Scout runs its own eleven-symbol 15-minute trader. Each trades and logs its own activity with its own local state. No trader checks, verifies, reconciles, combines, or depends on another trader.

Only the two nightly eleven-symbol Gameplans are synthesized. Use one actual account cash/stock balance, publish the same combined Gameplan and Stats on both PCs, and prepare the traders every night under permanent authorization. Atlas may synthesize directly and return the completed result to Scout without an acceptance wait. The human manually starts each trader. See [Independent trading systems](../development/independent-traders.md).

## Combined Gameplan calculation

Each research producer retains its original forecast IDs, probabilities, direction/abstention labels, horizons, target windows, model assessments and planning prices. With eleven symbols per PC, the existing four-horizon issue schedule contains 264 local forecasts and 528 combined forecasts. Combine the two research packages once against one actual broker account snapshot; do not add separately sized plans or account balances.

The combined publication contains the full 22-symbol chronological cash/stock projection, original producer lineage, ordered cash events, conditional assumed fills and original Stats. Both PCs display the same completed Gameplan and account balances. Atlas may produce it directly and return it to Scout. Planned sale proceeds are conditional; live purchasing capacity comes from actual available broker cash.

## Independent execution and records

Atlas's manually started Gameplan worker consumes the combined plan and preserves original own order IDs, fills and pending intentions. It uses `state/gameplan-trader/holdings.sqlite3` and `state/gameplan-trader/authority.sqlite3`. Each PC's manually started 15-minute worker uses separate local `intraday/execution/` records and its own models.

No 15-minute activity is imported into Gameplan's planning or order ledger. Account share changes are actual capacity changes, not cross-trader reconciliation failures. A fresh Gameplan epoch records no fictional fills. Original mixed ledgers remain preserved history; current broker stock can be used through normal observed-stock assignment.

Keep the existing Gameplan account identity and configured combined symbol universe in its local binding. There is no recurring peer fencing, native-history union, cutover approval, compatibility attestation or reciprocal acceptance prerequisite. Ordinary nightly production and routine repair are permanently authorized; manual start is the only trader-start action.

## Source and private artifact boundaries

The research lists remain local eleven-symbol lists; combined Gameplan execution covers the union. Do not widen model watchlists merely to execute the combined plan. Preserve local credentials, native task IDs, memories, raw databases and original receipts. The private nightly exchange carries the completed operating packages/result; GitHub carries shared source and coordination. Software regression checks and exact-byte source publication do not create trading or cross-trader verification gates.

For current operating steps use [nightly workflow](../development/nightly-workflow.md), [private exchange](../development/nightly-exchange.md), and [independent traders](../development/independent-traders.md). Older ownership-consolidation commands remain historical in Git; they are not manual-start prerequisites.
