# Nightly Gameplan workflow

Jeremy's October 13, 2026 direction permanently authorizes this nightly workflow. Atlas and Scout each produce Gameplans for their own eleven symbols. Synthesize those two Gameplans once, using one actual cash and stock balance, into the same 22-symbol Gameplan shown on both PCs. Atlas can perform synthesis directly and return the completed result to Scout without a reciprocal acceptance wait.

Atlas runs the combined Gameplan trader and its own 15-minute trader. Scout runs its own 15-minute trader. All three trade and log independently. There is no 15-minute synthesis, cross-trader reconciliation, compatibility verification, shared trading ledger, distributed grant, or approval prerequisite. Preparing the plans and traders is permanently authorized; the human manually starts each trader.

The current operating contract is [Independent trading systems](independent-traders.md).

## Every night

1. Catch up each PC's required completed-session data under its existing bindings.
2. Score matured original forecasts, retaining pending/missing outcomes, then review models and produce that PC's eleven-symbol Gameplan.
3. Exchange the two Gameplan research packages and their saved Stats through the existing private nightly exchange.
4. Synthesize once from both packages and one actual broker account cash/stock snapshot. Project cash and holdings chronologically; do not add two independently sized plans or two copies of the account balance.
5. Publish the combined Gameplan and Stats and return the same completed result to both PCs. Display the full 22-symbol plan and the same account balances on each machine.
6. Prepare Atlas's Gameplan and local 15-minute trader, and Scout's local 15-minute trader, for manual start. Preparation neither starts trading nor requires any other trader's status.

## Local operation and recovery

Use the existing absolute private configuration and installed Python:

```text
python -B -m ml.nightly_workflow --config PRIVATE_CONFIG --dispatch
```

The deterministic dispatcher and native supervision use the same dated state and existing responsibility owners. Preserve completed work, original forecast identities, actual observation/publication times, task identities, local symbols, private bindings and receipts. Resume unfinished work after a real error. Do not create another pipeline, repeat completed training, backdate a forecast, or ask for routine overnight approval.

Stats precede model review. Model tests retain causal training inputs, held-out results, target windows and explicit abstentions. Missing outcomes never become zero-valued outcomes. The standing operating authority does not alter those mathematical contracts.

## Gameplan synthesis

`ml.nightly_synthesis` consumes the two selected Gameplan packages, Stats packages and the one account snapshot. Its local actor can be Atlas or Scout; the Gameplan executor remains Atlas. Keep one completion identity for the completed result and transport retries. Atlas may publish as soon as both research packages exist; delivery to Scout does not gate Atlas's plan or trader.

`ml.joint_capital_plan` performs the single chronological projection. Gameplan reads its own records from `state/gameplan-trader/holdings.sqlite3`; the related own order reservations are in `state/gameplan-trader/authority.sqlite3`. A fresh independent epoch starts with no recorded Gameplan fills. Actual broker holdings remain usable account capacity. Never invent fills or import a 15-minute trader's ledger to populate it.

Both Gameplan readers consume the same combined publication. Each producer's original symbols, forecasts and Stats remain traceable. The private exchange transports operating artifacts; GitHub carries shared source and coordination under the existing publication contract. Credentials, raw account databases and fitted models remain private.

## Manual execution

Atlas uses `Start-Gameplan-Trader.cmd` for its combined Gameplan trader and `Start-15m-Trader.cmd` for its local 15-minute trader. Scout uses its local `Start-15m-Trader.cmd`. Each worker reads actual broker cash, positions, orders and quotes for its own decisions, records only its own order IDs and fills, and honors its own manual stop control.

The 15-minute models, forecasts, orders and health never participate in Gameplan synthesis or acceptance. Gameplan health and source loading never gate either 15-minute trader. Independent forecast logging and actual execution outcomes remain distinct.
