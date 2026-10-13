# Atlas: Codex and Ducketz scheduled tasks

October 13, 2026 readback of the fourteen task definitions updated for the permanent independent-trader workflow: eleven are ACTIVE and three are PAUSED. Saved task identity, schedule, model settings and active/paused status were retained. Times below use America/Los_Angeles. This inventory covers the updated Gameplan, 15-minute and coordination tasks; unrelated task definitions are outside this change.

Every scheduled night, Atlas and Scout each produce their own eleven-symbol Gameplan. Synthesize those two Gameplans once against actual cash and stock balances and return the same twenty-two-symbol Gameplan and Stats to both PCs. Atlas can complete that synthesis directly. Atlas runs the combined Gameplan trader and its local eleven-symbol 15-minute trader; Scout runs only its local eleven-symbol 15-minute trader. Each trader operates and logs independently. See [independent traders](independent-traders.md).

Nightly production and routine repair have permanent human authorization. No verification, approval, compatibility proof, peer acceptance round or other-trader status is an operating prerequisite. Jeremy manually starts each trader. Scheduled tasks never auto-start or restart a trader, clear a manual stop or submit trades themselves.

## Current native schedule

| Exact display name | State | Pacific schedule | Responsibility |
| --- | --- | --- | --- |
| Atlas Datastore Catch-up | ACTIVE | Monday-Friday 21:05 | Complete local nightly data work and continue unfinished production. |
| Atlas Gameplan Stats | ACTIVE | Monday-Friday 03:00 | Produce local Stats from available mature outcomes. |
| Atlas Model Review Training and Predictions | ACTIVE | Monday-Friday 03:00 | Complete model work and predictions for the local Gameplan. |
| Atlas Local Gameplan | ACTIVE | Monday-Friday 03:00 | Produce Atlas's eleven-symbol Gameplan and private exchange package. |
| Atlas Ducketz Display and Readiness | ACTIVE | Monday-Friday 03:35 | Complete and display the combined Gameplan and Stats; prepare Atlas's two independent manual traders. |
| Atlas Joint Gameplan Handoff | ACTIVE | Monday-Friday, every twenty minutes from 21:00 through 23:40 | Exchange the two local Gameplans and complete the single combined result. |
| Atlas Joint Gameplan Handoff After Midnight | ACTIVE | Tuesday-Saturday 00:00, 00:20 and 00:40 | Continue the same operating evening's handoff without duplicate synthesis. |
| Atlas Priority Source Reconciliation | ACTIVE | Monday-Friday, every twenty minutes from 21:00 through 23:40 | Complete shared source work and GitHub coordination independently of nightly production. |
| Atlas Priority Source Reconciliation After Midnight | ACTIVE | Tuesday-Saturday 00:00, 00:20 and 00:40 | Continue the same source responsibility using its existing records. |
| Atlas Trader Representative | ACTIVE | Monday-Friday 03:55, then 04:05 and hourly through 17:05 | Prepare manual starts and support each human-started trader through its own records. |
| Stay in touch with Scout | ACTIVE | Every fifteen minutes | Complete concrete source and Gameplan handoffs; report meaningful changes only. |
| Atlas 15m first live session watch | PAUSED | Every fifteen minutes if resumed | Observe Atlas's human-started 15-minute trader through its own records. |
| Loops Gameplan Weekly Review | PAUSED | Saturday 09:00 if resumed | Review historical Gameplan outcomes. |
| Loops Overnight Gameplan | PAUSED | Daily 21:05 if resumed | Retained older nightly definition; ordinary production uses the active responsibilities above. |

A scheduled wake continues its assigned work and records the actual outcome. The 03:00 and 03:35 tasks are work opportunities, not approval or confirmation deadlines. Evening and after-midnight companions share one logical operating date and existing records. A saved ACTIVE status does not claim that a particular night's work completed.

## Manual trading and local state

Atlas's Gameplan trader uses the published combined plan and its own execution records. Each 15-minute trader uses its own symbols, configuration, predictions and execution records. Actual broker cash, stock and order responses provide account capacity. Another trader's liveness, orders, ledger, acceptance state or source attestation is never consulted as a condition for operating.

A configured local launcher can report `READY_FOR_MANUAL_START` without a preflight approval stage. Normal market-time order eligibility is evaluated by the trader when manually running. Preparation does not change live controls. A paused task stays paused unless the human separately changes its status.

## Communication and evidence

Routine Atlas-Scout discussion and source handoffs use GitHub under the local profile's `github_only` policy. The private CODEXSTORE exchange carries the authorized numerical Gameplan packages and account balances. Preserve credentials, native task IDs, memory, receipts, models and financial records locally. Source review and exact-byte publication records remain development work; pending publication or an old repair claim cannot hold up nightly production.

The October 13 task readback was taken from the saved definitions after the native prompt updates. The update preserved schedules and status. Current task inventory does not infer Windows Task Scheduler settings from older records; no Windows task change is described here. The [earlier task chapter in Git history](https://github.com/jeremysecondstate/ducketz/commits/main/docs/development/atlas-scheduled-tasks.md) retains previous observations and retired operating instructions.
