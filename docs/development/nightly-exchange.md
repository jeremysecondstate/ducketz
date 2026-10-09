# Private nightly Gameplan exchange

Current responsibility ownership and recovery authority are in
[nightly-operations.md](nightly-operations.md). Verified terminal exchanges retain
their original publications and receipts without replaying preparation or adoption.

`tools.nightly_exchange` connects the two completed local preparation runs to
Scout's one-account synthesis and Atlas's adoption of that exact result. Each
invocation performs one bounded wake or reports what evidence is still missing.
Scout composes; Atlas remains the sole prospective executor, and trader startup
remains manual. Legacy LOG stays paused.

## Authority and transport

The local human must approve this PC's bindings and private exports before its
exchange is enabled. A peer notice, a copied configuration or a successful hash
check cannot grant that approval. Atlas needs its own locally approved account
binding and snapshot access. Routine packet delivery and status responses then
use the configured automation; the human need not relay files between PCs.

This is a deliberately separate data transport through a privately bound
CODEXSTORE synchronized directory. It is not a fallback for the installed Git
coordination helpers. Git coordination remains authoritative for source
availability, review requests and status notices. Preserve its existing
Drive-signal bindings, queues, locks, completion identities and receipts.

The authorized private packet content is limited to the two owners' frozen
Gameplan and Stats exports, one sanitized account snapshot, the resulting
combined plan, and the completion evidence needed to verify adoption. The
snapshot may contain cash, equity, held shares and exposure across the agreed
22-symbol universe, open-order reservations and horizon ownership. Do not copy
credentials, account numbers, raw broker responses, databases or fitted models.
Private exchange packets and configurations must not enter Git publication.

The synchronized directory supplies access control and delivery. Hashes bind
exact bytes and detect inconsistent selections; they do not authenticate a
sender independently of that directory's permissions. Each PC writes only its
own actor namespace. Remote paths are provenance, never instructions to execute
or paths to open on the receiving PC.

## Local configuration

Use the existing reviewed private configuration, such as
`scratch/nightly-workflow/exchange-config.json`. Preserve its bindings and
durable session state; do not create replacement monitors or copy peer bindings:

- `actor` must match the installed local profile.
- `workflow_config`, `local_profile` and `coordination_active` select existing
  absolute local files. Verify the pinned coordination installation before use.
- `exchange_root` is the explicitly approved local mount of
  `CODEXSTORE/ducketz-nightly-exchange/v1`; both PCs bind the same shared folder
  through their own local mount paths. `state_root` is private local state,
  outside that synchronized folder.
- `owners.atlas` and `owners.scout` contain the frozen, disjoint eleven-symbol
  research universes. The local owner's list must match its profile/watchlist;
  the union is the agreed 22-symbol planning and execution universe.
- `account_scope_sha256` is the same reviewed opaque account-scope digest on
  both PCs. It is not an account number.
- `private_exchange_authorized` becomes `true` only under that PC's local human
  approval. Atlas additionally supplies `account_config`, the absolute path to
  its existing `state/account-gameplan/config.json` under its datastore. Scout
  omits that field.

Keep `peer_communication_enabled: false` in the separate preparation workflow
configuration: the preparation worker still performs local work and records its
completed exports. The exchange task owns transport. Changing the preparation
configuration cannot retroactively authorize or preserve an older completion.

```powershell
& .\.venv\Scripts\python.exe -B -m tools.nightly_exchange --config C:\dev\ducketz\scratch\nightly-workflow\exchange-config.json --check
# One locally authorized exchange wake:
& .\.venv\Scripts\python.exe -B -m tools.nightly_exchange --config C:\dev\ducketz\scratch\nightly-workflow\exchange-config.json
```

On Atlas only, the authorized wake may add `--allow-snapshot-refresh` to obtain
a fresh sanitized snapshot through its local adapter. Scout rejects that flag.
Neither the normal exchange wake nor snapshot refresh grants order authority.
`--check` verifies authorization, scope and both eleven-symbol lists without
capturing an account snapshot.

## Packet flow and incomplete arrival

Under `exchange_root`, each selection lives at
`sessions/<action_date>/<actor>/<kind>/selection.json`, with immutable packet
bytes at `packets/<digest>.json` in that same kind directory. A packet envelope
declares its type, actor, sessions and bindings and carries bounded encoded file
bytes. The selection pins its byte count and SHA-256 as well as its content
digest, so partial arrival cannot become a different accepted generation.

| Packet kind | Writer | Purpose |
| --- | --- | --- |
| `preparation` | Each owner | Its exact completed local Gameplan and Stats exports. |
| `snapshot` | Atlas | One sanitized account snapshot for Scout's synthesis. |
| `joint` | Scout | The frozen combined plan and exact synthesis evidence. |
| `accepted` | Atlas | Evidence that Atlas adopted that same combined result. |

Preparation, joint and acceptance selections are immutable. Atlas may refresh
the snapshot selection before a joint result exists; a saved synthesis keeps
its originally selected bytes. Both local preparations must match the intended
action date, completed Stats session, account scope and frozen universes before
composition. Both UI publications are verified before completion is reported.

Synchronization may deliver a selection before its packet, or a file only
partly. Such arrival is pending work, not proof of readiness. A later wake may
retry. Once the declared packet bytes have arrived, malformed, conflicting or
changed evidence must fail verification; it must not cause a silent switch to
another source generation.
Do not fabricate empty peer results or infer zero balances from missing files.

Atlas's `capture_snapshot` adapter accepts only a complete observation no more
than 60 seconds old with unchanged local account and profile bindings. Its
export preserves account-wide gross exposure and reservations, including amounts
outside the per-symbol union; unknown reserves or incomplete ownership evidence
block capture. It exposes only the approved fields, using opaque allocation
digests instead of native allocation identifiers.

Atlas alone supplies the native account snapshot for normal exchange in every
saved activation mode. Scout supplies research and synthesis; it does not supply
execution history, an ownership ledger, an export or a migration prerequisite.
Both matching completed research preparations must verify before Atlas captures
that snapshot. It supplies cash and account-wide exposure once while preserving
account identity, exact coverage, freshness, pending-order and reservation checks.
The exchange never changes activation or switches a frozen session's mode.

Historical `PREPARING` ownership requests, challenge-bound observations and
responder records are retained as planning evidence only. Their two-ledger route
is legacy reference material, not a startup or handoff prerequisite. Normal wakes
do not launch a Scout ownership responder, wait for Scout execution history or
interpret missing peer records as zero holdings. Preserve any original historical
account/horizon/allocation/order/fill identities and reconcile actual matching IDs
through normal accounting; do not blindly import or combine another database.
Reading an existing ledger never refreshes its saved reconciliation timestamp.

Snapshot freshness is checked against the current time for the first synthesis.
The composition contract permits at most 900 seconds between the snapshot
observation and synthesis and rejects future observations. A retry of a completed
result verifies its frozen original evidence; it does not refresh capital underneath
an already accepted plan. Atlas's execution checks still obtain and reconcile
current account evidence when the manually started trader runs.
If a frozen selection expires before its first adoption, it remains pending for
explicit review; retry must not backdate that first adoption. Atlas also verifies
that the returned plan binds the exact snapshot it originally published, even
when a newer snapshot selection has since arrived.
Before shared publication, Atlas saves immutable local origin evidence for the
snapshot. Received packets and caches cannot recreate that evidence or substitute
for it during handoff.

## Completion, retries and source installation

Local state records selected packet hashes, stable completion identities, exact
configuration and profile bindings, the installed release and all three exchange
helper source hashes. Retries retain them across partial publication or adoption.
A process failure after one UI artifact is adopted is not joint completion:
resume and verify
the exact remaining artifact and receipt. Do not edit immutable packets or
selection files to repair a failure.

Missing local preparation, peer preparation, snapshot, joint result or Atlas
acceptance is reported as pending with its reason. Exchange completion requires
Atlas's acceptance of the exact selected joint result. Keep this separate from
the read-only readiness states: `JOINT_READY_LOCAL` verifies Scout's own combined
UI, while `HANDOFF_VERIFIED_LOCAL` verifies Atlas's. Neither activates cutover,
proves a running trader or authorizes an order. Successful synthesis and handoff
select their receipts for `ml.nightly_readiness` as documented in
`nightly-workflow.md`.

Source adoption must respect the active nightly session. Preparation binds Git
HEAD and the Python source bytes under `ml`, `app` and `datafetching`. Unreviewed
changes during an incomplete session invalidate its source evidence. Source
intake, review and queued publication may continue, including priority
reconciliation. A failed matching preparation may receive an explicitly reviewed
repair with exact before/after source hashes and a preserved-state audit; only
the unfinished stages resume, and a frozen continuation requires the complete
verified repair chain. Ordinary installation waits until active work finishes.
A terminal COMPLETE/HANDOFF_VERIFIED_LOCAL session retains its original receipt;
a later manual activation or reviewed startup-source change does not reopen or
relabel that completed session.

Use one bounded native wake with existing local locks and durable state. Preserve
the original preparation deadline and deduplicate unchanged pending findings.
Infrastructure checks use offline fixtures; they do not train, call providers,
submit orders or restart applications. Current live execution bindings and
runtime deployment remain separate from exchange enablement.
The planning boundary is Atlas's verified adoption of Scout's exact synthesized
Gameplan, combined Stats and receipt. A completed frozen session is retained
without rerunning preparation, account capture or handoff. Legacy research
partitions, absent Scout history and migration/cutover procedures do not create
an additional manual-start gate. Jeremy's explicit Start-Gameplan-Trader command
is the activation and worker-start action; the normal worker obtains current
account evidence and reconciles native ownership and orders before submission.
Planning completion, source publication, account activation and a running worker
remain separate facts.
