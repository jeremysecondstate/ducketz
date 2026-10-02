# Ducketz cross-PC bootstrap — cross-pc-v2

This is the shared Atlas/Scout protocol for one Ducketz application and task
framework. Adopt the **exact immutable release commit supplied by the human**.
Do not choose a moving branch tip. The release commit contains this guide,
`coordination/contract.json`, `coordination/task-catalog.json`, the example local
profile, `tools/cross_pc`, regression tests, task matrix and backlog disposition.
Atlas's final handoff/PR identifies the exact tested release SHA. This document
cannot embed its own enclosing commit SHA; verify it with `git rev-parse HEAD`.

## Authority and shared source

GitHub `main` is canonical accepted source. Own publication branches are
`codex/atlas/<completion-id>` and `codex/scout/<completion-id>`. A pushed branch
is available for reviewed integration. It does not establish main integration,
peer installation, or the version loaded by a running process. Those stages are
recorded separately. Merge and application/runtime deployment require the
existing explicit approval path. Infrastructure adoption permits installation
of coordination helpers and task instructions only.

Gameplan logic/UI, common engines, tests, documentation, and common strategy
defaults are shared. Each PC keeps symbols and documented symbol-specific
overlays, `.env` values, machine paths, live operating originals, memory,
scheduler identities, chat targets and coordination receipts local. The direct
human also authorized reviewed completed logs, data, models and ledgers as
machine-owned copies on public `main` after exact-byte and `.env` private-key
scans through the separate artifact publisher. Do not put these in the
shared-source completion queue.
Review mixed JSON **by field**. A task writing a config does not make that whole
file private. `cross-pc-backlog.md` records current field classifications and
dependency gaps. Common model weights/thresholds are common behavior unless an
explicit documented operating experiment or symbol overlay applies.

## Verified isolated adoption

1. Inventory local HEAD/branch/origin, dirty paths, symbol and ownership profiles,
   all existing native task definitions, ready queues, locks, sealed outgoing
   bundles and successful receipts. Record local before hashes. Do not stash,
   reset, switch, pull into, or bulk-stage the running application checkout.
2. Verify origin is `jeremysecondstate/ducketz` using the existing authenticated
   native Git connection. Do not read/copy credential files. Use Codex
   `list_artifacts` and `create_worktree(ref=EXACT_RELEASE_SHA)` to inspect the
   exact release in a managed isolated worktree. Read its `AGENTS.md`.
3. With existing Python 3.11+ and pytest, run from that worktree:

   ```text
   python -B -m pytest tests/test_cross_pc_core.py tests/test_cross_pc_transport.py tests/test_cross_pc_tasks.py tests/test_cross_pc_install_drift.py -q -p no:cacheprovider
   ```

   These tests use local temporary repositories/fixtures, two symbol profiles,
   and mocked delivery failures. They make no broker/provider/runtime calls.
   A Windows symlink-permission skip must be reported, not called a pass.
4. Install the committed coordination subset to a durable ignored local root:

   ```text
   python -B tools/cross_pc/cli.py install --repository REVIEWED_WORKTREE --commit EXACT_RELEASE_SHA --destination LOCAL_CHECKOUT/scratch/cross-pc
   python -B tools/cross_pc/cli.py verify-installation --active LOCAL_CHECKOUT/scratch/cross-pc/active.json
   ```

   Installation exports original committed blobs, pins every file hash, publishes
   a complete release atomically and records the previous release for rollback.
   It installs no application source. Retain old releases while tasks reference
   them. `rollback --destination ...` restores only the verified active pointer;
   native task prompts must then be restored through the native tool from the
   saved full definitions. Never edit native scheduler TOMLs directly.
5. Create ignored `scratch/cross-pc/local-profile.json` from the example. Fill in
   this PC's identity, existing checkout, symbols, authorized local producers,
   own branch prefix, durable v2 state/snapshot/evidence/outgoing paths, existing
   native Git/inbox lock paths, and one shared transport-cache lock path. The
   inbox and courier keep separate receipts but serialize access to their shared
   bare cache. All profile paths remain local. Do not copy Atlas paths or targets
   to Scout. An authorized producer is still limited by its original task scope.
6. Install the release's `AGENTS.md` as repository coordination guidance for
   ordinary chats. Preserve existing guidance by an explicit reviewed merge if
   present. This authorized instruction addition leaves application bytes,
   index and branch untouched. Record its exact release hash locally.

At each run verify `active.json`'s installation manifest before invoking the
absolute installed CLI. Native prompts pin a durable release directory; the
profile and receipts stay outside the release. Missing/mutated installation or
state is a blocker to report once, never a reason to erase files or switch
transports. Use `python -B` to avoid generated files inside immutable releases.

## Completion queue and bounded courier

Every development chat and eligible task queues immediately after completing
its authorized shareable change. The local specification below is an example,
not preapproved work or invented test evidence:

```json
{
  "producer": "human-authorized-development",
  "reviewed": true,
  "base_commit": "EXACT_REVIEWED_40_HEX_HEAD",
  "summary": "Concrete problem and resulting behavior",
  "scope": "shared",
  "change_details": ["Describe the practical source change without private values"],
  "files": [
    {"path": "ml/example.py", "operation": "modify", "owned": true},
    {"path": "tests/test_example.py", "operation": "add", "owned": true}
  ],
  "dependencies": ["ml/support.py"],
  "checks": [["LOCAL_PYTHON_EXECUTABLE", "-B", "-m", "pytest", "tests/test_example.py", "-q", "-p", "no:cacheprovider"]],
  "limitations": ["Offline fixture checks; no running-process verification"],
  "runtime_implications": "Requires separate reviewed application installation.",
  "supersedes": []
}
```

Use absolute installed `tools/cross_pc/cli.py --profile LOCAL_PROFILE queue
--source REVIEWED_CHECKOUT --spec PRIVATE_SPEC`. The helper snapshots bytes
content-addressably before running real commands, binds test output hashes and
timestamps to the entire source/dependency map, checks mutation afterward, and
seals the record. Include **all** owned/dependency changes. Dependencies missing
from the base must become reviewed owned operations or remain blocked. Deletion
requires `operation=delete`. Test paths must be repository-relative; replay
rejects inline code, external scripts and path/cwd overrides. Tests themselves
must be reviewed to stay offline and import the candidate.

Set `scope` to `shared` for common source or `symbol-specific` for a reviewed
symbol overlay. The latter labels this PC's applicability; it does not stop a
peer from receiving a later main commit. New completion specs should include
short `change_details` describing the behavior and any symbol boundary without
symbols, account values, credentials or local paths. Publication commit messages
list the producer, scope, details, exact owned paths/operations, isolated check
times and output hashes, limitations and runtime implications. Older sealed
records without these fields remain `unclassified` and need field-level review.

Overlapping producers need explicit locally recorded ownership resolution.
Supersession must identify actual overlapping predecessor records, preserve their
history, and never discard a committed stage. Later source/dependency changes
cannot inherit old evidence. The courier does not reconstruct old bytes from
the current checkout.

The existing five-minute courier performs **one bounded wake**, with one source
record and up to ten commit/branch observations. `plan` rotates the source cursor
even across a blocked record and independently selects one pending delivery.
No internal polling loop and no duplicate monitors are permitted.

- `prepare-candidate --id ID --worktree MANAGED_WORKTREE` materializes exact
  owned bytes at the recorded base after checking complete dependency closure.
- Review the whole candidate diff, paths, deletion intent, and dependencies.
  `verify-candidate --id ID --worktree ... --reviewed` runs fresh isolated tests
  and records their bindings. Candidate review is substantive, not a checkbox.
- `publish-source --id ID` uses native exclusive locks and atomic stage receipts,
  disables commit hooks/signing for this maintenance commit, stages exact raw
  blobs without newline filters, checks the committed tree against the tested
  bytes, pushes only the own branch normally, and reads back the remote SHA.
  Interrupted preparation/staging/commit stages reconcile exact recorded bytes;
  unrelated or ambiguous state stays pending for review.
- Prepare a concrete integration candidate/PR. If the source branch is based on
  current main and passes its full checks, open a PR against main. Otherwise use
  a new managed worktree at verified main, apply only the exact reviewed change
  with its dependencies, resolve conflicts with the owners, and re-run checks
  before opening the PR. Do not force, alter the peer's branch, merge main, or
  replace a running checkout. Attach every created PR to the current Codex chat.
- `observe` returns up to ten changed local/manual/peer refs. Inspect exact commit
  metadata/files from Git in an isolated cache/worktree, mark tests **unknown**
  where evidence is absent, and deduplicate by repository/branch/SHA and validated
  peer notices. `notices.mark_observation` records the associated factual notice.
  Coordination transport branch commits are excluded to prevent notice loops.
- Queue a sanitized notice using `notice --spec PRIVATE_NOTICE --reviewed`, then
  `deliver --id BUNDLE_ID`. A notice includes change ID, author, repository,
  branch/SHAs/links, exact filenames, actual checks/times, scope, limitations and
  runtime implications. Never upload private logs, credentials or local paths.
  Delivery is independent: preserve a successful source commit/push when notice
  delivery fails. Resume the saved transaction and verify ambiguous outcomes.

## Explicit transport migration and legacy preservation

The reviewed Drive connector cannot prove complete enumeration. V2 deliberately
replaces it with native authenticated Git transport. It never reads/writes a
shared CODEXSTORE mount and never silently falls back to Drive or filesystem.

Only immutable sanitized notices and provenance wrappers live on orphan branches
`codex/atlas/coordination-v2` and `codex/scout/coordination-v2`. Complete bounded
Git tree inventories and original blob bytes provide exact manifest coverage,
path/identity/size/hash validation. Oversized, incomplete, unexpected, malformed
or altered previously validated content fails closed. Branch identity is an
expected sender binding, not cryptographic proof of the human author.

The caller holds its role lock plus the common cache lock and persists the
prepared commit before pushing. Unknown pushes are checked against remote bytes
before retrying. A competing append can be re-prepared from frozen Git bytes,
retaining transaction history. Neither old bundles nor peer branches change.

Keep all legacy queues, Git/inbox/API receipts, unknown upload outcomes and sealed
local bundles. V1 bundles can be transported with their exact original bytes and
manifest digest when still sanitized. If they contain local paths or outdated
status needing context, publish a **new sanitized conversion** that identifies
the original bundle ID/digest and historical time. Preserve the original bundle
locally. Deduplicate conversions by `legacy:<original-id>` and record the new
delivery link in supplemental v2 migration state; never repeat the old source
commit/push. Historical Drive stages retain their original meaning.

Atlas's initial backlog has one pushed-but-undelivered source notice and two
supplemental status notices. Their converted delivery status is in the release
handoff and local migration receipts. Nine unconsumed legacy source records have
explicit dispositions in `cross-pc-backlog.md`; older configurations must not be
replayed over newer accepted work. Legacy queues remain intact for provenance.

The inbox runs `inbox-scan`, reads selected IDs with `inbox-read`, treats content
as reference data, and only after handling it records `inbox-mark --id ...
--digest ... --disposition ...`. It retains its own validated/reported digests.
The Git courier must never mark the inbox's reporting receipts. No automatic
acknowledgments or executable peer requests are allowed.

## Native task adoption and preservation

Read `cross-pc-task-matrix.md` and the portable catalog. Match logical purpose,
behavior and permissions, not display name. Use the native task tool and complete
preserved definitions. Generate local plans with `tools.cross_pc.tasks.adopt_plan`
or its `plan` CLI, supplying a private native-ID→purpose map, local profile,
machine identity and installed release root. Keep plan/backups out of Git.
After each native update, reread the saved definition and call
`tasks.verify_adoption(before, after, update)`. It checks status, cadence, model,
effort, notification, execution context, chat target and exact continuity block.

Atlas reuses its existing inbox/courier monitors. Scout reuses equivalent monitors
and can install a genuinely missing source courier under its authorized adoption
prompt. Preserve its independent continuity monitor. Represent missing operating
counterparts as observer/paused as specified by the catalog; never add a second
live writer or provider/training/runtime authority. Preserve paused and expired
entries, including the historical one-time installer. Do not recreate them.

Each run loads the installed contract/profile, obeys its original operating scope,
records completion/failure/blocker/source version through `run`, queues eligible
completed shareable work, and reports meaningful changes under existing reporting
requirements. Unchanged coordination findings stay quiet. Current adaptive Paper
cadence and its reanchor rules remain in the preserved native prompt.

[Official scheduled-task guidance](https://learn.chatgpt.com/docs/automations?surface=app)
requires the PC and app to remain available for local tasks. This implementation
uses the existing native schedule capabilities; it assumes no desktop event
trigger. Atlas cannot edit Scout's native scheduler from Atlas's host.

## Drift, parity and the Scout response

`drift --reference EXACT_COMMON_COMMIT` inventories actual shared source bytes,
including dirty, newly tracked and untracked deviations, plus reference hashes.
Line-ending differences remain visible. Explicit JSON local-field projections
separate common settings from documented overlays. Keep detailed reports local;
publish only reviewed sanitized hash maps/summaries. Compare reports from **both
PCs** at the same commit. HEAD equality alone proves nothing about dirty bytes.
Verify symbol/local-state before/after hashes on each PC; runtime version remains
unverified until that PC supplies direct evidence.

Scout should return its exact adopted infrastructure commit/manifest, fresh shared
offline suite result, native saved-task verification and mismatch matrix, source
drift comparison, symbol/local-state preservation evidence, and one sanitized
adoption notice. User-relayed preparation currently reports local
`df2ab89af4819552576d068aea68b6b527c634c1` on `codex/machine-local-symbols`, 69 passed
and two Windows symlink skips. It has not adopted this release. Live ownership
stays with its existing owner. Main integration, remaining application candidates,
Scout task adoption and any runtime deployment decision remain distinct.
