# Private nightly Gameplan exchange

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

Copy `coordination/nightly-exchange.example.json` to an ignored local path such
as `scratch/nightly-exchange/config.json`. The example is intentionally disabled
and incomplete. Replace every placeholder with reviewed local bindings before
enabling it:

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
& .\.venv\Scripts\python.exe -B -m tools.nightly_exchange --config C:\dev\ducketz\scratch\nightly-exchange\config.json --check
# One locally authorized exchange wake:
& .\.venv\Scripts\python.exe -B -m tools.nightly_exchange --config C:\dev\ducketz\scratch\nightly-exchange\config.json
```

On Atlas only, the authorized wake may add `--allow-snapshot-refresh` to obtain
a fresh sanitized snapshot through its local adapter. Scout rejects that flag.
Neither the normal exchange wake nor snapshot refresh grants order authority.
`--check` rejects the disabled example until its authorization, scope and both
eleven-symbol lists are correctly bound; it does not capture an account snapshot.

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
| `ownership_request` | Atlas | A fresh challenge bound to both completed preparations. |
| `ownership` | Scout | A fresh observation of its own saved native ownership ledger, bound to that challenge. |
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

While the account is `PREPARING`, each PC's native ledger may cover only its own
eleven symbols. The ownership exchange reads those partitions separately and
validates their combined coverage against Atlas's fresh account snapshot.
A producer observation labels its
held quantities as the original saved reconciliation baseline, preserves that
baseline's timestamp, and separately records when the ledger was read. Reading
the ledger does not create a fresh broker observation or reconcile its contents.
Changed, missing, blocked or inconsistent ledger evidence cannot become an empty
safe inventory.
Validation uses a temporary local copy of the ledger files, removes it after the
read, and transfers only the sanitized observation.

After both preparations are verified, Scout may launch one hidden, bounded
ownership responder under a separate local lock. It reads only its own ledger
and responds to an exact Atlas challenge; it never calls a broker. Its maximum
loop lifetime is six minutes, and another wake must not extend a running child's
deadline or create an indefinite chain of workers. Local state retains its
lifecycle and failures. An expired or failed responder ends; a later scheduled
wake may start a new bounded attempt. Configuration checks do not launch it.
This deadline bounds the loop; it cannot forcibly interrupt a stalled filesystem
call. The responder must recheck expiry before publishing a response.

For that `PREPARING` route, `--allow-snapshot-refresh` lets Atlas issue a challenge
and wait at most 50 seconds for the matching fresh Scout observation. Missing or incomplete
responses leave the wake pending without calling a broker. Once the response is
verified, Atlas reads its own ownership ledger and captures one fresh account
snapshot. Both producer observations must remain within the existing 60-second
freshness limit and match that account's exact current holdings. The account
snapshot supplies cash and exposure once; producer records supply no cash.
Pending union orders or unresolved ledger reservations still require native
reconciliation and block this planning route. Immutable responses are never
relabelled as observations of a later challenge.

After a separately authorized cutover makes the account `ACTIVE`, Atlas retains
the existing native snapshot route against its own complete 22-symbol ledger.
That route keeps its account, ownership and freshness checks and does not require
the two producer observations. The exchange verifies the frozen account binding;
it neither changes activation nor switches modes underneath a selected session.

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
HEAD and the Python source bytes under `ml`, `app` and `datafetching`. Changing
HEAD or those bytes during preparation, or after preparation while that session
is still being exchanged, invalidates its readiness evidence. Source intake,
review and queued publication may continue, including priority reconciliation;
installation into the operating checkout waits until the session is finished.
Do not overwrite source and then reuse its older review or readiness receipt.

Use one bounded native wake with existing local locks and durable state. Preserve
the original preparation deadline and deduplicate unchanged pending findings.
Infrastructure checks use offline fixtures; they do not train, call providers,
submit orders or restart applications. Current live execution bindings and
runtime deployment remain separate from exchange enablement.
Combining planning evidence does not consolidate the two native ledgers, complete
account migration or activate cutover. An account in `PREPARING` remains blocked
for joint execution; its execution prerequisites and manual Atlas trader start
are separate from successful synthesis and UI adoption.
