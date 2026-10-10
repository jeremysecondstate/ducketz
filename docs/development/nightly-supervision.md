# Nightly supervision continuity

Local preparation is only the first part of a completed overnight Gameplan.
The intended action date is complete only when the existing exchange verifier
freshly verifies the combined accepted plan, the completed-session Stats, their
exact dated receipts, and `joint_ready`, `ui_ready`, and `peer_verified`.
`LOCAL_COMPLETE_PEER_SETUP_PENDING`, a repair claim, an applied patch, a launched
worker, or a sent peer request does not establish overall completion.

`tools/nightly_supervision.py` provides an executable read-first decision and one
bounded private continuation ledger. It never dispatches a job, calls a model or
provider, installs source, takes over a claim, changes exchange evidence, or
controls a trader. Existing domain helpers remain the only operating path.
Every result explicitly has `mutation_authorized=false`; an instruction to
continue refers to already authorized work under the existing guards.

## Existing owners and wake boundaries

Priority Source Reconciliation is the sole writer of the supervision ledger.
Its existing five-minute native wake runs this check before ordinary daily
reconciliation, intake checkpoints, or an unchanged-work shortcut. It reuses
the exact retained repair owner, repair ID, completion identity, dated failure,
candidate, and frozen request. Native cron wakes can start separate chats, so
the ledger and existing repair records carry continuity across chat boundaries.

Joint Gameplan Handoff uses `--inspect` only, with Priority's automation ID as
`--owner`, so it sees the same incident and owner attestation. Joint retains its
existing exchange progress, peer stall/request cursor, and final dated display
verification responsibilities. It routes repairs to the existing Priority
owner and does not create a second repair worker or write the shared ledger.
Each role keeps its existing native task identity, schedule, memory,
notification identities, and coordination receipts. The October 10 direct-human
instruction selects `gpt-6-astra` with `ultra` reasoning for both PCs' existing
Priority and Joint owners and their delegated substantive repair worker. This
changes those supervisors' settings, not the model-training acceptance rules.

The Windows watchdog currently runs every five minutes, at nightly kickoff,
and after logon. It advances eligible local preparation through
`ml.nightly_workflow --dispatch`; it does not wake a Codex repair agent and does
not complete the private exchange. The existing native five-minute Priority
wake is the repair continuation bootstrap. This design adds no OS model daemon,
parallel monitor, GitHub event trigger, or reactivated legacy overnight loop.

An active substantive repair keeps its existing bounded worker and continues
across compaction while executable authorized work remains: inspect the cause,
preserve the claim, repair and test an isolated candidate, publish reviewed
bytes, apply through the proper domain helper, resume, and verify the result.
After applying a repair, follow the resumed job through full repair, recovery,
handoff, and final dated readiness rather than ending at `APPLIED`. A retained
interrupted transaction describes recovery work, never an acceptable completion.
Observe active repair/resumed progress at intervals no longer than 60 seconds;
renew the explicit owner attestation during that work, including long tests.
Quiet healthy training must continue; it is not a reason to restart it.

A turn can finish after verified completion or a concrete external/prerequisite
wait with retained evidence and the next action for the existing timely wake.
An assigned owner or logged error alone is not a completed recovery. A crashed
turn leaves the same durable repair identity for a later native wake; expiration
of an attestation never releases the repository repair claim. Before resuming
an apparently abandoned owner, inspect current native thread status, candidate
ownership, and existing locks. A stale attestation is not proof that another
writer has exited. Continue safe independent intake and the other PC's healthy
work while the blocked dependency remains guarded.

## Invocation and private installation

Use the PC's verified installed coordination contract and local profile first.
The module can run from a normally installed checkout:

```powershell
& $NightlyPython -B -m tools.nightly_supervision `
  --config $NightlyWorkflowConfig --exchange-config $NightlyExchangeConfig `
  --owner $NightlyPriorityAutomationId --inspect
```

During an active exchange, adding even an otherwise independent source file to
the application tree can invalidate a repair inventory. The supported narrow
installation instead copies only the exact reviewed helper to the ignored
private directory below and records its published source SHA, file SHA-256,
passing checks, and actual installed dependency review in a local manifest:

```text
scratch/nightly-operations/supervision/releases/<published-source-sha>/nightly_supervision.py
```

Verify that manifest and the helper's exact SHA-256 before every native use.
Keep the original application source, profile, active coordination release,
exchange selections, and repair inventory unchanged. Run the private absolute
script using the real installed application's imports, not the source candidate:

```powershell
$NightlyPriorPythonPath = $env:PYTHONPATH
try {
    $env:PYTHONPATH = $NightlyRepository
    & $NightlyPython -B $NightlySupervisionScript `
      --config $NightlyWorkflowConfig --exchange-config $NightlyExchangeConfig `
      --owner $NightlyPriorityAutomationId --inspect
} finally {
    $env:PYTHONPATH = $NightlyPriorPythonPath
}
```

All script/configuration/interpreter paths are absolute local bindings. The
installed exchange loader validates the actual repository/profile relationship.
Test source-base compatibility and the exact installed imports before adopting
the private script. A read-only inspection must succeed before enabling its use
in either native prompt. Source publication and each PC's installation proof
remain separate facts; Scout uses its own existing paths, actor, and task IDs.
Do not copy Atlas's private ledger, attestation, account data, or native IDs.

Priority replaces `--inspect` with `--record --wake-id <exact-native-run-or-turn-id>`
to write only `state_root/supervision/<action-date>.json`. Preserve the same wake
ID through retries and observations in that native turn. The same wake does not
increment the unchanged-wake counter twice. Writes are locked and atomic, and
timestamps cannot move backwards. Joint never uses `--record`.

The default date comes from the application's completed-session calendar.
`--action-date YYYY-MM-DD` explicitly inspects a validated exchange session using
the actual observation time; it does not alter the clock or extend deadlines.
A retained repository claim is inspected first even when it concerns an older
date, with `requested_action_date` and the actual claim's `action_date` reported
separately. This is preservation of unfinished work, not permission to run past
its original deadline.

## Decisions and retained evidence

| `next_action` | Existing owner's next step |
| --- | --- |
| `CONTINUE_SAME_OWNER` | Resume the retained claim/prepare/apply transaction. At `REPAIR_APPLIED`, invoke the ordinary matching domain continuation and verify it. |
| `RECONCILE_PARTIAL_CLAIM` | Inspect the preserved partial claim; use the same helper/identities to recover it, never replace or delete it. |
| `REPAIR_REQUIRED` | Diagnose the saved failure and pursue its authorized domain repair or real dependency restoration. |
| `WAIT_FOR_LIVE_OWNER` | Leave that owner active; use its exact thread/turn identity for the next native status observation. |
| `INSPECT_RUNNING_OWNER` | A `RUNNING` label lacks complete fresh process/heartbeat evidence. Inspect the original worker; do not infer death or launch a duplicate. |
| `WAIT_FOR_HEALTHY_WORKER` | Exact native run, PID birth, and recent heartbeat match. Keep it running and observe progress. |
| `WAIT_BACKOFF` / `WAIT_KICKOFF` | Retain the recorded timing and retry budget; use the existing next wake. |
| `DISPATCH_PREPARATION` | Use the ordinary prerequisite-aware dispatcher; it owns all deadline, source, and responsibility gates. |
| `ADVANCE_EXCHANGE` / `RESUME_EXCHANGE` | Joint uses the existing guarded exchange entrypoint and preserves selections and completed local preparation. |
| `COMPLETE` | The actual `_completed_result` just verified exact dated combined Gameplan/Stats and receipt bytes. Save the meaningful completion once. |

The result includes actor, action/review dates, incident ID, original/effective
deadline, retained repository owner, phase and evidence hashes, next action,
actual observation time, and final verification where available. The stable
incident identity is actor/date/domain. A changed error fingerprint, comment,
assignment, heartbeat, or routine snapshot refresh is not forward progress.
Progress requires newly verified completed steps, a forward verified repair
phase in the same transaction, or fresh final completion. Claim identity alone
never proves owner liveness. An exception during evidence verification is a
repair need, never completion.

## Explicit owner liveness attestation

An active Priority turn can add `--owner-liveness-attestation ABS_PRIVATE_JSON`.
First inspect to obtain the exact incident identity and retained repair ID.
Use actual native thread/turn IDs; if either is unavailable, omit the attestation
and inspect the native owner directly. Do not invent IDs or infer a Codex owner
from an unrelated PID. This record is an agent attestation with stated
provenance, not cryptographic proof of a native writer lock.

```json
{
  "actor": "Scout",
  "action_date": "2026-10-12",
  "incident_id": "<exact-inspect-result>",
  "automation_id": "<existing-Scout-Priority-automation-id>",
  "repair_id": "<exact-retained-repair-id-or-null>",
  "thread_id": "<actual-native-thread-id>",
  "turn_id": "<actual-native-turn-id>",
  "observed_at": "<actual-zoned-observation-time>",
  "provenance": "current_chat"
}
```

Use JSON `null`, not the string `"null"`, when there is no retained repair.
`current_chat` requires the exact `CODEX_THREAD_ID` in the invoking environment.
For another active owner observed through the native `read_thread` tool, set
`provenance` to `native_read_thread` and include `native_observation` with
`tool="read_thread"`, `status="running"`, and the same `thread_id`, `turn_id`,
`automation_id`, and actual `observed_at`. Retain the observed native response
privately; do not manufacture a running status from a saved assignment.

The helper rejects mismatched identity, missing turn/thread IDs, unknown
provenance, naive/invalid times, future observations, and explicit observations
older than five minutes. Priority's ledger retains the original provenance.
A later observer can wait on that bounded retained attestation; it is not
relabeled as a new native tool observation. The owning current chat remains
actionable. An expired retained attestation stops suppressing continuation,
but never authorizes claim takeover, concurrent edits, a retry budget reset,
or a deadline extension.

Keep private financial packets in their existing CODEXSTORE protocol and use
GitHub for sanitized coordination. No success claim may waive frozen packet,
snapshot freshness, account ownership, model-quality, privacy, or manual trader
controls. The final human-facing outcome must distinguish source publication,
each PC's installation, an actually running repair, and verified intended-date
combined display completion.
