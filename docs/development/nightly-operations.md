# Nightly responsibility operations

Jeremy's October 13, 2026 direction permanently authorizes this nightly workflow. Atlas and Scout each produce Gameplans for their own eleven symbols. Synthesize those two Gameplans once, using one actual cash and stock balance, into the same 22-symbol Gameplan shown on both PCs. Atlas can perform synthesis directly and return the completed result to Scout without a reciprocal acceptance wait.

Atlas runs the combined Gameplan trader and its own 15-minute trader. Scout runs its own 15-minute trader. All three trade and log independently. There is no 15-minute synthesis, cross-trader reconciliation, compatibility verification, shared trading ledger, distributed grant, or approval prerequisite. Preparing the plans and traders is permanently authorized; the human manually starts each trader.

The current operating contract is [Independent trading systems](independent-traders.md).

## Responsibilities

| Responsibility | Required result |
| --- | --- |
| Local data and Stats | Completed-session inputs and original matured forecast outcomes |
| Local models and Gameplan | Each PC's own eleven-symbol Gameplan with unchanged forecast identities |
| Gameplan synthesis | One chronological 22-symbol cash/stock plan from the two Gameplans |
| Display and return | The same combined Gameplan and Stats available on Atlas and Scout |
| Trader preparation | Atlas: Gameplan plus local 15-minute trader; Scout: local 15-minute trader |
| Shared source upkeep | Preserve ownership, publish completed source and maintain both local installations |

Use one dated workflow and its existing local configuration, task identities, schedules and state. The deterministic dispatcher and native tasks resume dependencies as completed outputs become available; supervision checkpoints are not artificial stage start times. Preserve the exchange calendar and truthful actual times.

## Permanent authority and exceptions

The whole normal nightly operation is pre-authorized. Do the work and repair routine failures without requesting verification or approval from Jeremy, the peer PC or another trader. No cross-trader prerequisites, source-transition ceremony or reciprocal acceptance loop belongs in this operating flow.

A real missing input, unavailable service or numerical error remains an actual exception. Retain useful progress, repair the cause, and resume unfinished work. Do not manufacture data, silently change target windows, count immature outcomes, or repeat unchanged deterministic failures. Shared code fixes still preserve other writers' changes and use the installed source publication procedure; offline software tests are not trading prerequisites.

Report meaningful completion, a real failure or required human action once. Keep unchanged supervision quiet and retain its compact cursor. Avoid duplicate alerts and acknowledgment loops. Native task settings are local bindings; this document does not create or change a schedule.

## Manual controls and independent state

Preparation leaves each trader ready for a manual start. It does not submit an order, start a trader or override an explicit stop. Atlas's two launchers are `Start-Gameplan-Trader.cmd` and `Start-15m-Trader.cmd`; Scout runs its own 15-minute launcher. A stopped or degraded trader has no effect on either other trader.

Gameplan uses `state/gameplan-trader/holdings.sqlite3` and `state/gameplan-trader/authority.sqlite3`. Each PC's 15-minute trader uses its own `intraday/execution/` state. Preserve the original mixed ledgers as history. Independent workers record actual own orders/fills and use current broker cash/positions as capacity, without inspecting another trader's ledger.

## Private data and task housekeeping

Keep private broker account state, databases, raw data, fitted models, credentials, native IDs, task memory and original receipts local unless covered by that PC's explicit export authority. Keep the private nightly exchange separate from GitHub discussion and source publication.

Scheduled tasks complete their native turn normally and must not archive their own running chat. Retain the separately authorized delayed cleanup mechanism described in the local completed-scheduled-chat cleanup runbook, with its existing identities and boundaries.
