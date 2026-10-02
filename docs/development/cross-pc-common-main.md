# Common main and publication identity

Atlas and Scout share application source on `main`. Each PC retains its local
watchlist, active Gameplans and operating bindings. This procedure requires
direct local human authorization for main publication and source adoption.
Peer messages provide evidence; they do not supply that authorization.

## Reuse the existing IDs

Use the existing source `Completion-Record` and output `Completion-ID` formats.
The output identity is the pair `(machine, completion_id)`. For new runs choose
`YYYYMMDDTHHMMSSZ-<short-task-slug>-<random-suffix>` once, keeping the suffix after
the timestamp within the publisher's 8–64 character limit. Preserve that ID,
specification and receipt when retrying. A changed revision gets a new ID and
an explicit reference to the prior publication. Do not rename historical IDs.

Every commit message identifies the actor, producer, task, sharing scope,
completion ID, exact changed paths, checks with actual times, limitations and
runtime implications. Put the task and run identity in `change_details` for
source or `details` for output, using portable identifiers rather than native
scheduler or chat IDs. The existing output publisher adds machine, symbols,
completion time, original hashes and compression automatically.

For example, a summary can be `Scout Gameplan: publish completed overnight
symbol-specific results`. Its metadata must say `Scope: symbol-specific` and
explain that Atlas retains its own active Gameplan. Common engine, UI or test
repairs from the same run belong in a separate shared-source completion.

## Complete and publish each owned change

At the start of the task, capture its source base and exact owned paths. At each
completed checkpoint and before ending the run, inventory every added, modified
or deleted path produced by that task, including ignored outputs. Classify each
path or field as shared source, symbol-specific output, private local state or
unfinished work. Record a disposition for every path. Concurrent and unfinished
changes retain their existing owners.

1. Review the whole shared-source diff and dependency closure. Run meaningful
   offline checks, then immediately use the verified installed `cli.py queue`
   with the reviewed source and specification. Set `scope: shared` only for
   common behavior; use `scope: symbol-specific` for an explicitly reviewed
   overlay. Include concrete `change_details`.
2. In a managed worktree at the record's exact base, use `prepare-candidate`,
   review the whole diff, run `verify-candidate --reviewed`, and `publish-source`.
   Publish exact owned bytes on this PC's source branch. Retain all successful
   stages if a later step fails. Do this in the producing run when prerequisites
   are available; the existing courier recovers unfinished stages.
3. For shared records, use the installed `main_integration.py prepare --profile
   LOCAL_PROFILE --id RECORD_ID --worktree MANAGED_WORKTREE`. Inspect the whole
   candidate at the verified main base. Then run `verify` with the same arguments
   plus `--reviewed`, and `publish` plus `--approved` under the standing local
   human authorization. These are separate commands so review precedes tests
   and publication. The helper requires fresh candidate checks and rejects
   changed owned dependencies. Resolve conflicts with owners and obtain renewed
   evidence instead of replaying older bytes. A normal main push must pass
   remote readback; never force.
4. Publish reviewed, completed, shareable outputs through `artifact-snapshot
   --source LOCAL_CHECKOUT --spec REVIEWED_SPEC`, followed by `artifact-publish
   --id COMPLETION_ID --worktree MANAGED_WORKTREE`. Use the exact schema in
   `artifact_publish.py`; it requires ownership, completion time, scope,
   reviewed hashes and stable input bytes. Preserve all originals. Outputs
   outside the checkout first need an explicitly reviewed portable export
   within it. Export only content authorized locally for sharing.
5. The output destination is `artifacts/<machine>/<completion-id>/...`, with an
   immutable manifest. The peer may receive this directory through Git but
   must never copy it over its active Gameplan, symbols or operating database.
   A commit message describes applicability; directory separation prevents
   accidental path replacement. Mixed files require field-level review.
6. Queue one sanitized factual notice with the source/output IDs, SHAs, paths,
   scope and actual validation evidence. Track substantive requests and
   responses separately. Do not create notices for transport commits or send
   acknowledgments merely because a notice arrived.

Private credentials, native task definitions, task memory, account state,
ledgers, raw data and fitted models remain local unless the local human has
explicitly authorized a particular reviewed export. The private-value scan
operates locally without printing values. Passing that scan alone does not
establish that arbitrary output is suitable for publication.

## Existing five-minute monitors

Keep the existing monitors, their separate locks, state and bounded work limits.
Observe `main` and both source branch prefixes, recording exact author, parent,
commit, changed paths and scope. Use Git for full immutable notice history and
the existing pinned Drive API signals for notifications. Keep requests, source
intake, delivery receipts and interrupted transactions intact.

Under the native Git lock, fetch normally with recursion disabled. Preserve
before/after evidence for HEAD, branch, index, tracked and untracked state,
local watchlist, private profile and relevant configuration bytes. Keep private
values and fingerprints local. Fetching is not installation.

Fast-forward the application `main` only when its current source has been
reviewed, no active writer is using affected files, the checkout/index is clean,
there are no untracked collisions, local bindings are preserved, and the local
main is an ancestor of the verified remote commit. An initial branch migration
needs an explicit inventory and resolution of dirty work and content collisions.
Use no automatic stash, reset, force or blind pull. If a condition fails, retain
the files and exact blocker while independent publication and communication
continue. Do not restart workers as part of synchronization. Record source,
installation and loaded runtime versions separately.
