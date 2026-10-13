# Nightly responsibility operations

This is the current operating arrangement under Jeremy's October 9, 2026
implementation request. Earlier rollout holds and report-only recovery
instructions in task history are historical. Preserve the completed October 9
recovery and its original receipts. A completed prior date does not satisfy a
new night. Jeremy controls Atlas's trader start and stop. Inspect actual saved
status, process liveness and current human reports before describing its state.

October 9 status note: Jeremy subsequently reported manually stopping the trader
after repeated order attempts appeared stuck. Atlas later found the original
worker still alive. Current liveness and the Atlas-owned diagnosis are tracked
in [execution issue #4](https://github.com/jeremysecondstate/atlas-scout/issues/4).
Offline nightly verification does not resolve that separate production incident.

## Standing automatic operations

Jeremy's October 13, 2026 instruction is standing preauthorization for this
workflow every night. Run the existing local preparation, Scout synthesis and
private delivery automatically without another human confirmation, peer approval
or verification ceremony. Atlas supplies its Gameplan and Scout supplies its
Gameplan; Scout performs the actual cash/share synthesis using the balances and
planning evidence already available with those plans or their latest existing
basis, then sends the completed result to Atlas. Sending it completes the handoff.
Do not request a third fresh account snapshot or wait for Atlas acceptance.

Atlas has two independent traders: the merged Gameplan trader and Atlas 15m.
Scout has its own independent 15m trader. Each keeps its own logs, uses actual
broker capacity through its existing connection and manages its own orders.
No trader's runtime, readiness, lock, reconciliation or approval is another
trader's prerequisite. Jeremy's manual first trader start remains manual; no
new approval request is needed. Code-publication and privacy procedures remain
separate engineering duties and cannot become new nightly operating gates.

## One pipeline, distinct responsibility owners

Each PC keeps one durable workflow configuration, state root, action-date run,
workflow lock and native runtime lock. Native Scheduled Tasks own supervision
and corrective work. A deterministic watchdog dispatches actual dependencies
without asking models to repeatedly inspect unfinished stages. A task wake and
watchdog wake use the same coordinator; neither creates another pipeline.

| Responsibility | Coordinator stages | Prerequisite | Routine supervision |
| --- | --- | --- | --- |
| DATASTORE CATCH-UP | `datastore_catchup` | intended kickoff or recorded missed wake | 21:05 Pacific launch |
| GAMEPLAN STATS | `prepare_stats` | catch-up receipt | local daily checkpoint; actual receipt dispatches the stage |
| MODEL REVIEW, TRAINING & PREDICTING | `model_review`, `train_and_plan` | newest completed Stats | local daily checkpoint; review is dispatched immediately after Stats |
| GAMEPLAN | `local_gameplan` | accepted predictions | local daily checkpoint; accepted predictions dispatch the stage |
| GAMEPLAN SYNTHESIS | existing private exchange | both Gameplans | configured native schedule; Scout every 20 minutes in its evening window |
| DUCKETZ DISPLAY | `verify_display`, `local_handoff`; accepted combined readers | local plan, then exact synthesis/handoff | dated 03:00 risk and 03:35 confirmation coverage |
| REPO RECONCILIATION | source and coordination queues | independent | configured native schedule; Scout every 20 minutes in its evening window |
| TRADER REP | saved readiness and normal worker evidence | Atlas only | morning check and recurring market-session checks |

The native checkpoint schedules are local operating bindings: Atlas uses 03:00
Stats/model/Gameplan checkpoints, while Scout uses 21:05 checkpoints and a
04:05 catch-up fallback. Scout display wakes at 03:00 and 03:35; Atlas display
wakes at 03:35. The configured handoff/synthesis supervisors provide the
shared dated notification coverage. Verify actual enabled definitions and
next-run readbacks on each PC; this table is not an installation receipt.

The checkpoint times are supervision and notification times, not guessed stage
start times. The watchdog launches the next eligible responsibility as soon as
its prior completion is seen. Local display verification and export precede
synthesis. The display responsibility also checks the exact accepted combined
Gameplan and Stats after synthesis. The dated schedule inventory is descriptive; each trader's local logs and watch remain independent of the nightly handoff.

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

### Claim, repair and verify the same unfinished stage

`ml.nightly_stage_repair` is the normal preparation repair interface. Before
editing an isolated candidate, save a private request containing `action_date`,
`repair_id` and optional null `completion_record`, then claim with the exact
responsibility/reconciliation owner:

```text
python -B -m ml.nightly_stage_repair --config PRIVATE_CONFIG --owner OWNER --claim PRIVATE_CLAIM_REQUEST
python -B -m ml.nightly_stage_repair --config PRIVATE_CONFIG --owner OWNER --reviewed --prepare PRIVATE_REPAIR_REQUEST
python -B -m ml.nightly_stage_repair --config PRIVATE_CONFIG --owner OWNER --reviewed --apply FROZEN_SPEC
```

The request for `prepare` binds the same action date and repair ID, isolated
candidate, explicit `changes` map, actual published `completion_record`, risk,
rationale, runtime implications and actual passing checks. Checks retain argv,
integer exit code zero, absolute log, timestamps and exact `source_files` hashes
across `ml`, `app`, `datafetching`, `tools`, `fundamentals`, `options`, `signals`
and `technicals`. Source publication generates the Completion-Record after the
checks; never invent one before claiming. A restored external dependency uses
`risk: external_dependency`, `changes: {}`, unchanged source and actual
restoration evidence. Its local completion identity does not claim a source
publication. Policy/data/model changes must explicitly declare their reviewed
invalidation and preserve old evidence; orchestration changes retain compatible
completed stages.

Preparation and exchange share one `state_root/repair-owner.json` under
`workflow.lock`, across all dates and responsibilities. It contains owner,
repair ID, token, domain, action date and a completion record that is null until
reviewed evidence binds it. It has no timeout takeover. Installing the fix does
not release the owner. Normal dispatch validates the exact applied audit,
resumes the same stage and releases ownership only after its saved output is
verified. An interrupted save/release reuses that success without repeating the
stage. A later genuine failed attempt may continue under the same owner with a
new stable repair ID and preserved ancestry, up to three repairs per chain.
Unchanged failures and exhausted budgets retain the precise unresolved
requirement; no transition renews a deadline. The legacy source-repair helper
also refuses mutation while any global owner is present.

For a failure after preparation has completed, use the distinct exchange repair
interface in `nightly-exchange.md`. Never reopen completed preparation merely
to make the preparation helper accept an exchange failure.

The failing responsibility owns its evidence and verified disposition. REPO
RECONCILIATION coordinates shared source defects, and the five-minute
handoff/synthesis supervisor may repair as a bounded delegate for the saved
responsibility owner between daily checkpoints. Every delegate retains the
same persistent claim, token and completion identity; it cannot create a
competing repair. The reconciliation task's five-minute wake also
checks unresolved external failures for verified restored usage, authentication,
runtime, storage or private transport availability. It uses the same audited
dependency-restored transition before retrying; it does not wait for a daily
checkpoint or repeatedly probe an unchanged unavailable dependency. Display
and handoff/synthesis share one dated notification ledger, so an early or missed
native wake cannot lose either threshold or produce competing notices.
For a genuine authority
boundary, retain the precise remaining requirement. Report meaningful failure,
correction, verified recovery and completion once. Preserve per-action-date
03:00 readiness-risk and 03:35 missed-confirmation notifications. These times
do not prohibit startup. Record notification identities in local memory/state
and suppress unchanged notices and acknowledgment loops.

Use `tools.nightly_notifications` with the private workflow config and a fresh
read-only readiness observation for the intended action date. `--inspect`
returns due or pending events; `--claim` binds a stable event to the actual
automation and native thread. Include that event ID in the actionable native
notice. A later wake uses actual native thread and inbox readback with
`--confirm`; preparing a final message is not delivery. Confirmed non-delivery
allows another claim for the same event, while an unknown outcome stays pending
without expiry or takeover. The helper does not send messages or independently
attest the caller's proof. See the shared
[notification runbook](https://github.com/jeremysecondstate/atlas-scout/blob/main/coordination/runbooks/nightly-notifications.md)
for the exact commands and evidence fields. Keep proof and financial evidence
private, and preserve original claims and outcomes.

Keep a compact current memory block and small per-task cursor. Historical
memory is retained but is not reread or appended on every unchanged wake.
For no new actionable delta, record `outcome=unchanged`, then call the native
`set_thread_archived` tool with `archived: true` and no thread ID to archive only
that scheduled run. A scheduler-required inbox directive does not replace the
archive call. Report new meaningful failure, correction, recovery and completion
once; a repeated dependency condition is not a new alert.

## Private handover and independent execution

Scout combines Atlas's Gameplan and Scout's Gameplan, including actual cash/share
planning arithmetic using the available plan balances or latest existing basis,
and sends the synthesized result to Atlas. This repeats automatically every night
under Jeremy's standing preauthorization. Delivery is handoff completion. No
third fresh snapshot, Atlas acceptance, human approval or cross-trader readiness
or reconciliation is required. Atlas's optional local ingestion receipt remains
separate from delivery and does not trigger an acknowledgment loop.

Atlas operates the merged Gameplan trader and its independent 15m trader. Scout
operates its independent 15m trader. Each trader uses its own existing broker
connection and current actual capacity, manages its own exact order IDs and keeps
its own local forecasts, intentions, orders, fills and cancellation logs. Do not
make another trader's process, source, lock, account ledger or status a condition
of operation. Own-order duplicate prevention and truthful fill records remain
part of each trader's local behavior.

Jeremy's manual first start stays manual. Responsibility tasks do not start,
stop or restart traders merely to prepare, inspect or complete nightly planning.
A saved component's historical `execution_authorized: false`, missing peer receipt
or old `HANDOFF_VERIFIED_LOCAL` status is not a new permission request. Keep
original receipts and meaningful actual outcomes without replaying completed
preparation or inventing a successful live trade.

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
