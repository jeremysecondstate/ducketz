# Nightly responsibility operations

This is the current operating arrangement under Jeremy's October 9, 2026
implementation request. Earlier rollout holds and report-only recovery
instructions in task history are historical. Preserve the completed October 9
recovery and its original receipts. A completed prior date does not satisfy a
new night. Jeremy has manually started Atlas's trader; inspect actual saved
status and process liveness instead of assuming it remains stopped.

## One pipeline, distinct responsibility owners

Each PC keeps one durable workflow configuration, state root, action-date run,
workflow lock and native runtime lock. Native Scheduled Tasks own supervision
and corrective work. A deterministic watchdog dispatches actual dependencies
without asking models to repeatedly inspect unfinished stages. A task wake and
watchdog wake use the same coordinator; neither creates another pipeline.

| Responsibility | Coordinator stages | Prerequisite | Routine supervision |
| --- | --- | --- | --- |
| DATASTORE CATCH-UP | `datastore_catchup` | intended kickoff or recorded missed wake | 21:05 Pacific launch |
| GAMEPLAN STATS | `prepare_stats` | catch-up receipt | 03:00 readiness-risk checkpoint |
| MODEL REVIEW, TRAINING & PREDICTING | `model_review`, `train_and_plan` | newest completed Stats | 03:00 exception checkpoint; review is dispatched immediately after Stats |
| GAMEPLAN | `local_gameplan` | accepted predictions | 03:00 readiness-risk checkpoint |
| GAMEPLAN SYNTHESIS | existing private exchange | both exact research packages | every five minutes |
| DUCKETZ DISPLAY | `verify_display`, `local_handoff`; accepted combined readers | local plan, then exact synthesis/handoff | 03:35 confirmation checkpoint |
| REPO RECONCILIATION | source and coordination queues | independent | every five minutes; ordinary review after 01:25 |
| TRADER REP | saved readiness and normal worker evidence | Atlas only | morning check and recurring market-session checks |

The checkpoint times are supervision and notification times, not guessed stage
start times. The watchdog launches the next eligible responsibility as soon as
its prior completion is seen. Local display verification and export precede
synthesis. The display responsibility also checks the exact accepted combined
Gameplan and Stats after synthesis. Scout has seven tasks and no Trader Rep.

Use a lightweight supported model for routine supervision. The structured
review after Stats uses the configured stronger model once at that boundary.
Substantive source defects and difficult repair reviews use stronger reasoning.
Record actual locally supported models and native readbacks privately; do not
infer Scout's account/model access from Atlas's settings.

The native Windows watchdog is installed through
`tools/register_nightly_watchdog.ps1`, not by changing scheduler storage. It
uses the existing Python, checkout, config and interactive Windows identity.
It has a 21:05 trigger, a five-minute timer, a logon trigger, StartWhenAvailable,
and IgnoreNew instance policy. It does not start or stop the desktop app or a
trader. The deterministic watcher can run while the app is closed; model review
still requires the authenticated CLI and available usage, and native Codex
supervision requires the app. While a machine is off or signed out, nothing
runs; the next available logon/timer/native wake resumes the dated work.

## Dispatch and recovery

Use this PC's absolute private config and installed Python from its checkout:

```text
python -B -m ml.nightly_workflow --config PRIVATE_CONFIG --check
python -B -m ml.nightly_workflow --config PRIVATE_CONFIG --dispatch --dry-run
python -B -m ml.nightly_workflow --config PRIVATE_CONFIG --dispatch
```

The first two commands do not launch production. The last is standing-authorized
normal operation. `--launch --responsibility datastore|stats|model|gameplan|display
--catch-up` launches only an eligible responsibility; use one actual role.
The private `responsibility_owners` map records the existing native identities.
Keep legacy Loops Overnight Gameplan paused and reference-only.

Session selection uses the application exchange calendar. Preserve the 21:05
Pacific kickoff and original 04:00 action-date deadline across weekends,
holidays and DST. A missing eligible run is different from a healthy worker, an
interrupted run or a verified terminal run. Healthy work is left running;
completed stages and verified native receipts are reused after interruption.
Never backdate actual creation/publication times or use later observations in
a frozen prediction. Stats preserve immature outcomes and no-history baselines.

The private automatic-recovery policy records Jeremy's standing authorization
and bounded attempts. A late recovery keeps the original action date and
deadline and records a fixed cutoff within the documented seven-hour and
17:00 Pacific limits. Retries do not extend that cutoff. An expired continuation
does not become permission to create another. Existing special planning-tail
records remain source-bound and immutable.

Each actionable failure has its responsibility owner, evidence, classification
and disposition. Inspect stage logs, prerequisites and actual worker liveness.
Retry transient failures only within the recorded budget and cooldown. Leave
unavailable external dependencies resumable. Identical deterministic failures
require a focused repair, not repeated execution. Claim source repair once,
coordinate overlapping paths through atlas-scout, reproduce it offline, review
the whole owned diff and dependencies, and publish exact tested source through
the pinned completion queue. Install through a reviewed audited transition,
then resume that unfinished stage with the same identity and frozen deadlines.
Keep the original failure, completed outputs and source transition evidence.
Never replace source used by a healthy active session to clear a status report.

REPO RECONCILIATION owns cross-stage source defects; the failing responsibility
supplies its exact evidence and verifies recovery. Its five-minute wake also
checks unresolved external failures for verified restored usage, authentication,
runtime, storage or private transport availability. It uses the same audited
dependency-restored transition before retrying; it does not wait for a daily
checkpoint or repeatedly probe an unchanged unavailable dependency. Handoff
exclusively owns the dated 03:00 and 03:35 alarms; other tasks supply evidence.
For a genuine authority
boundary, retain the precise remaining requirement. Report meaningful failure,
correction, verified recovery and completion once. Preserve per-action-date
03:00 readiness-risk and 03:35 missed-confirmation notifications. These times
do not prohibit startup. Record notification identities in local memory/state
and suppress unchanged notices and acknowledgment loops.

## Private handover and execution boundaries

Scout numerically synthesizes both research packages against one Atlas
account-wide snapshot. Atlas sends its package and verifies/adopts Scout's
exact returned Gameplan, Stats and receipt. Atlas never runs numerical
synthesis. Once that handover is loaded, planning is complete. Do not add a
Scout execution-history export, migration, cutover, duplicate broker preflight
or approval ceremony. A terminal exchange verifies retained evidence without
preparation, account capture, synthesis or adoption replay.

Atlas alone executes all 22 symbols and all horizons. Jeremy alone manually
starts and stops the trader. No responsibility task or watchdog may change
trader controls, start/stop/restart it, or submit/cancel/replace orders. Trader
Rep reads saved worker/account evidence and verifies that the normal worker's
late-start logic nets overdue intentions, credits fills and reservations, and
prevents duplicate submissions. Do not fetch a broker account merely to fill
an infrastructure report. Offline fixtures are not production order evidence.
Missing ownership history never establishes zero holdings.

## Coordination and completion

Read the pinned coordination manifest, contract, local profile, this runbook,
nightly-workflow and nightly-exchange documents, and relevant retained task
memory. Each wake performs bounded substantive coordination in the verified
atlas-scout checkout using a durable cursor. Fetch before work, preserve dirty
and concurrent edits, assign issues and reuse the Project board. Keep
`thebreakdown.md` aligned. Answer routine technical questions directly through
sanitized files/issues; do not require Jeremy to relay them.

Preserve the pinned Git/Drive/request/incoming-source helpers, original IDs,
queues, receipts and successful publication stages. Priority intake favors
Hyperliquid development. Use the installed exact-byte completion queue and
managed isolated publication procedure. Prepare tested PRs; publication is
separate from main integration, installation, peer availability and runtime
verification. Do not infer merge or deployment permission.

Private financial packets remain exclusively in the authorized
`CODEXSTORE/ducketz-nightly-exchange/v1` exchange. Public coordination excludes
credentials, account state, financial packets, databases, raw data and fitted
models. Task IDs, definitions, operating paths and task memory remain local.

Verify native task definitions, enabled state, timezone, next-run readbacks,
model settings, launch permissions, runtime paths, storage, authenticated
bindings, dispatcher decisions and offline recovery checks. State separately
whether tonight is configured to launch and whether its actual production
preparation and handover have completed.
