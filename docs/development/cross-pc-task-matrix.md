# Cross-PC task inventory and adoption

Contract: `cross-pc-v2`. The portable catalog is
[`coordination/task-catalog.json`](../../coordination/task-catalog.json).
Atlas inspected all twelve saved native definitions on October 1, 2026.
Scout evidence is the human-relayed preparation report from that date; Atlas
has not inspected Scout's native scheduler. An inventory entry is not proof
of source installation, current execution, or operating parity.

## Atlas inventory and Scout mismatches

All wall-clock schedules use America/Los_Angeles. Intervals and original
native recurrence strings remain unchanged during prompt adoption. "Chat
default" means the saved heartbeat has no explicit model/effort override;
adoption must not invent one.

| Logical purpose | Atlas saved state and semantic cadence | Model / effort | Scout reported state and adoption disposition |
| --- | --- | --- | --- |
| `cross_pc_inbox` | Active, every 5 minutes; at most 10 candidates | Chat default | Active every 5 minutes using historical filesystem transport. Reuse and deliberately migrate; verify local receipts first. |
| `source_courier` | Active, every 5 minutes; at most 1 ready record and 10 observations | Chat default | Missing. Existing 5-minute message outbox explicitly disables Git. Reconcile purposes locally before installing the source publisher; do not assume the message monitor already provides it. |
| `overnight_gameplan` | Active, daily 21:05; native exchange-calendar no-ops and original next-session 04:00 deadline | gpt-6-astra / ultra | No native counterpart found. Observer ceiling until local operating authority is established. |
| `loops_operations_watch` | Active, every 90 minutes; missing fresh daily recovery no earlier than 21:15 | gpt-5.6-luna / low | No native counterpart found. Preserve local ownership and reconcile before any creation. |
| `weekly_gameplan_review` | Active, Saturday 09:00 | gpt-5.6-luna / medium | No native counterpart found. Evaluation/reporting purpose; no code, broker or training authority. |
| `weekly_opportunity_research` | Active, Sunday 09:00; one requested weekly report, duplicate suppression | Chat default | No native counterpart found. Research/reporting does not authorize production membership or model changes. |
| `stock_daytime_supervision` | Active, weekdays 03:55; bounded pre-opening check | gpt-5.6-luna / low | No counterpart. Atlas retains live execution ownership. Any peer counterpart is observational and cannot start a trader or place orders. |
| `hyperliquid_operations_watch` | **Paused**, saved 30-minute cadence | gpt-6-luna / xhigh | No counterpart; operating permissions unresolved. Preserve Atlas pause and require local ownership reconciliation. |
| `hyperliquid_paper_improvement` | Active, v3 adaptive cadence with a two-hour floor | gpt-6-luna / max | No counterpart; accepted experiment ownership is local to Atlas. Scout ceiling is observation pending explicit reconciliation. |
| `options_paper_tracking` | **Paused**, saved Tuesday–Saturday 00:17 | gpt-6-astra / max | No counterpart. Preserve the paused policy; no active counterpart. |
| `historical_fallback_installation` | Saved Active with one occurrence at 17:10; authorization expired October 1 at 04:00 | Chat default | No counterpart required. Do not recreate, replay, or extend the expired deployment window. |
| `historical_gameplan_review` | **Paused**, saved 30-minute interval for September 9 | Chat default | No counterpart required. Preserve historical terminal state. |

Atlas's overnight Gameplan and options-paper definitions retain the saved
`failed_runs_only` notification preference. Other definitions have no saved
notification override. Native task names, IDs, project/chat targets, context
directories and creation timestamps are preserved locally; they are not
part of the portable catalog.

Scout additionally reports active standalone communication continuity every
15 minutes and paused bootstrap/data-fetch completion monitors. The catalog
records those three purposes without guessing missing native details.
Atlas's continuity is embedded in its two existing five-minute communication
monitors; it must not gain a duplicate standalone continuity schedule.

## Authority and adaptive timing

Catalog roles are ceilings, not grants. Each original native task prompt
continues to define its narrower operating scope and dated exceptions. A
read-only or reporting task can communicate factual findings and retain
local coordination evidence without gaining application-code, trading or
runtime authority. Existing evaluation/research output writes are still
governed by that task's own contract. A source-capable task queues only
completed work it was already allowed to make.

Hyperliquid Paper Improvement preserves a two-hour minimum ladder,
verified-win increment of one hour, verified-loss reduction of one hour
with a two-hour minimum, and retained duration for ties and unscored/late/
unavailable comparisons. It retains its existing native PAUSED-then-ACTIVE
reanchor after an independently verified fresh opening, immutable assessment
and timing gates. Adoption itself only changes the coordination prompt;
it must not run a round, apply a historical penalty again, reseed, or
reanchor the schedule. Operations Watch remains paused and cannot assume
the improvement task's scoring/tuning/lifecycle authority.

One-time and paused definitions receive versioned communication guidance
without becoming new work. A saved Active flag on an expired one-time task
does not renew its operating authorization or justify recreating it.

## Native prompt adoption helper

[`tools/cross_pc/tasks.py`](../../tools/cross_pc/tasks.py) is a read-only plan
builder and post-update verifier. It never edits native TOML, task memory,
receipts, scheduler databases, or app state. Use an ignored private local
bindings JSON object mapping each existing native ID to a logical purpose,
or to an object with `purpose` and an explicitly permitted `role`.

From the installed pinned release, prepare a private review plan with:

```text
python -B -m tools.cross_pc.tasks plan --native-root NATIVE_AUTOMATIONS_DIRECTORY --catalog RELEASE/coordination/task-catalog.json --bindings PRIVATE_BINDINGS_JSON --release-root ABSOLUTE_PINNED_RELEASE --profile ABSOLUTE_PRIVATE_PROFILE --machine pc-original
```

Use `pc-new` on Scout after its local audit. Native bindings, full saved
prompts and review plans remain private; do not publish the emitted JSON.
The helper reports missing bindings as `no_creation`, rejects unknown
native fields pending review, rejects ephemeral worktree paths, and keeps
the original prompt bytes before an idempotent shared-contract suffix.
It never resolves a missing purpose by cloning another PC's native task.

Review each full update object and reread its saved native definition before
applying it. A changed definition means rebuild and review the plan. Apply
objects sequentially with the native `automation_update` tool only. The
helper preserves status, cadence, explicit model/effort, notification
preference, project/execution context and heartbeat target. Context fields
such as saved working directories that the native tool cannot directly set
remain in the plan's preserved-field fingerprint and must pass readback.

Call `verify_adoption(before, fresh_after, update)` after each native tool
result. It compares all saved fields except the intended prompt and native
update timestamp, compares the exact reviewed prompt, and verifies the
communication-continuity block unchanged. Keep that verification with the
local installation evidence; tool success alone is insufficient. If a
field drifts or a prompt is truncated, stop further updates, preserve the
evidence, and use the native tool for a reviewed restoration or repair.

The original transport/courier instructions of the two communication tasks
require their separately reviewed explicit migration before applying the
suffix. The helper does not silently rewrite those instructions and does
not replace their existing activity-based chat-rollover block. The pinned
installation provides the shared contract, catalog and helper; the private
profile supplies local symbols, paths, authority and state bindings.

The suffix invokes the absolute installed `tools/cross_pc/cli.py` entrypoint,
so its run-record and completion-queue commands do not depend on the running
checkout's import path. For source-capable roles it explicitly replaces
the earlier manually written v1 ready-record instructions: enqueue each
new completed revision once through the v2 snapshot helper. Existing v1
records and receipts remain intact for explicit reconciliation. The update
does not authorize a task to make changes outside its original scope.

[Official scheduled-task guidance](https://learn.chatgpt.com/docs/automations?surface=app)
was checked October 1, 2026: local tasks need the PC and desktop app running.
Desktop event triggers are unavailable, so the existing bounded five-minute
schedules remain the delivery mechanism. Standalone and in-chat task
destinations keep their existing semantics.

## Evidence required from Scout

Scout's report establishes a pre-adoption helper baseline (69 passed,
2 Windows-symlink skips), sixteen processed inbox messages, and five
published retained outbound bundles. It establishes no adoption of this
release. Scout must return the immutable installed commit and verified
release hashes; its local profile/ownership validation; sanitized task
purpose, role, preserved cadence/status/model/effort and readback results;
transport migration and receipt-preservation evidence; exact shared-source
byte drift, including dirty deviations; and its own offline acceptance
results. Missing or owner-restricted purposes stay explicitly unresolved.
No Atlas-side update can verify or edit Scout's native scheduler.
