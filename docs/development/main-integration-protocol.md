# Ducketz main integration and per-PC outputs

Both PCs use `main` as the accepted shared source. A completed source change is
first sealed with the local cross-pc-v2 queue, tested, and pushed on its
producer's `codex/atlas/<completion-id>` or `codex/scout/<completion-id>` branch.
That branch makes the exact reviewed bytes available; it does not by itself
change `main`, the other PC's checkout, or a running application.

The main integration helper accepts only an already pushed cross-pc-v2 source
record with its intact completion record, original and isolated passing checks,
exact own-branch commit, remote SHA receipt, and sealed `scope=shared` review.
Symbol-specific or unclassified source records stay on their producer branch
for separate field-level review. A commit message alone cannot stop the other
PC from pulling machine-local values from main. Files listed as mixed local
configuration in the profile also require peer overlay review before direct
main integration. Use a separate managed worktree.
The helper fetches and verifies the source branch and current `origin/main`,
requires `main` to descend from the reviewed base, and rejects any path changed
on main that is owned by or a reviewed dependency of the record. For an
unrelated main advance, it applies the original sealed bytes to current main.
Review that complete candidate diff, run fresh checks in the candidate, then
publish with the explicit main approval gate. The direct human's central-hub
request supplies standing approval for reviewed shared changes that pass these
gates; it does not approve a source conflict or a symbol-specific overwrite.
Publication uses a normal
fast-forward push, reads back the remote SHA, and retains an idempotent local
receipt. If main advances during review or push, prepare and test a new candidate.
Never force-push, silently resolve overlapping ownership, or stage the running
application checkout. A divergent old branch without a complete cross-pc-v2
source record needs its own reviewed integration work; it cannot be adopted by
branch name or by matching only some final blobs.

After this helper is included in a newly verified pinned release, its local
commands are:

```text
python -B ABSOLUTE_PINNED_RELEASE/tools/cross_pc/main_integration.py prepare --profile LOCAL_PROFILE --id COMPLETION_ID --worktree MANAGED_WORKTREE
python -B ABSOLUTE_PINNED_RELEASE/tools/cross_pc/main_integration.py verify --profile LOCAL_PROFILE --id COMPLETION_ID --worktree MANAGED_WORKTREE --reviewed
python -B ABSOLUTE_PINNED_RELEASE/tools/cross_pc/main_integration.py publish --profile LOCAL_PROFILE --id COMPLETION_ID --worktree MANAGED_WORKTREE --approved
```

Completed portable outputs that are appropriate to publish use explicit,
reviewed copies under `artifacts/<machine>/...`, where `<machine>` is the local
profile's `pc-original` or `pc-new`. Preserve the original local output and its
provenance. The direct human authorized completed logs, account snapshots,
holdings databases, models and other operating outputs in the public repository
when the exact reviewed bytes contain no `.env` private-key value. Use the
installed artifact publisher's sealed input hashes and credential scan. Keep
`.env`, live local bindings, native task IDs, task memory and coordination
receipts out of these published copies. Symbol-specific outputs identify their
producer PC and scope in commit metadata; shared Gameplan/UI/engine source and
common strategy defaults remain common on main. Review mixed configuration by
field.

Each PC adopts the new pinned release and updates its own native scheduled
tasks after a local audit. Preserve each task's cadence, status, model, effort,
notification setting, target, continuity instructions, and operating authority.
Atlas cannot verify or edit Scout's native scheduler from Atlas's host. A
five-minute courier may observe peer activity and prepare an integration
candidate, but a notice or Git push does not establish peer installation or
running-process version. Keep those stages in separate receipts.
