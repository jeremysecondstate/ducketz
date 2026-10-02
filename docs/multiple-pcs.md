# Running Ducketz on two PCs

Both PCs share application behavior and common defaults. Each PC keeps its
authorized stock membership and private operating state locally. Source parity,
task parity and runtime deployment are separate facts.

## Stock selection

Consumers that use `configured_watchlist_path()` select a watchlist in this order:

1. `DUCKETS_PRODUCTION_WATCHLIST`, when explicitly set for a candidate process.
2. `datafetching/watchlist.local.txt`, when it is a regular file in this checkout.
3. `datafetching/watchlist.txt`, the tracked common default.

An explicit `--symbols` or `--watchlist` argument takes precedence in consumers
that support it. The local file replaces the default membership; it does not
append to it. It contains one equity symbol per line and permits comments
beginning with `#`. The shared symbol reader normalizes case, removes duplicates
and rejects an empty or invalid list.

The local file, its activation lock and temporary replacement file are ignored
by Git. Preserve the tracked default when creating or changing a local overlay.
Preserve the existing authorized local membership during source adoption.
Keep its actual symbols in private local evidence; use synthetic memberships
when testing or reporting that both PCs can select different stocks.

Onboarding plans, activation and publication checks use the durable production
list: the local file when present, otherwise the tracked default. A candidate
environment override does not redirect activation into the candidate file.

Some consumers capture symbols or default paths when their modules are
imported. Source adoption or a watchlist edit does not establish what an
existing process has loaded. Any required runtime transition follows its
separate authorized procedure.

### Consumer boundaries

Guardian and overnight launch definitions pass the configured watchlist to
Loop B. A direct `ml.prediction_runtime` invocation with neither `--symbols` nor
`--watchlist` retains its datastore-discovery behavior. Review the actual local
launch definition when checking its scope; a selector function alone does not
prove that every command uses it.

Historical Gameplans, forecasts, models and receipts retain their frozen
universes. Validate each saved publication against its own recorded membership.
Do not rewrite an older publication to match the current local list.

Selecting stocks does not acquire data, fit models, publish a Gameplan or
authorize trading. Existing onboarding gates and operating ownership still
apply. Hyperliquid has separate configuration; differences outside stock
membership require their own field-level review and remain visible until
resolved.

## Shared source and private state

| Item | Treatment |
| --- | --- |
| Application logic, UI, engines, tests and operating documentation | Shared source |
| Common strategy defaults and the tracked watchlist fallback | Shared, reviewed defaults |
| Ignored local watchlist membership | Private binding owned by that PC |
| Credentials, account state, raw data, fitted models and ledgers | Local |
| Machine paths, native scheduler IDs, chat targets, task memory and receipts | Local |
| Mixed configuration | Review by field; retain unresolved shared differences |

A file is not wholly private because a local task writes it. Classify each
field under the current authorized contract. Do not silently omit nonstock
settings or replay an older configuration over a newer accepted revision.

## Completing and publishing shared work

Use the coordination workflow from the verified installed release. Its
`coordination/contract.json`, `docs/development/cross-pc-bootstrap.md` and
`docs/development/cross-pc-communication.md` define the current procedure.
At the start of a run, verify the pinned installation named by
`scratch/cross-pc/active.json` and read the private local profile. A missing or
mutated installation is a specific blocker, not permission to change transport.

1. Capture the reviewed base and explicit ownership before editing. Preserve
   other writers' changes and local bindings.
2. Review the complete owned diff and dependency closure. Specify each add,
   modify or delete operation explicitly.
3. Use the installed completion queue with an exact reviewed local
   specification. Capture immutable final bytes, real offline checks, producer,
   base, limitations and runtime implications. Later changes require new review
   and evidence.
4. Let the existing bounded courier publish from its managed isolated worktree
   on this PC's own branch and verify the remote SHA. Preserve successful stages
   if a later delivery step fails.
5. Prepare a tested integration candidate and pull request against verified
   main. Reconcile dependency differences and overlapping ownership explicitly.

Publishing a branch does not merge main, install it on the peer or change a
running application. Those stages require their own evidence and existing
human authorization. A source snapshot based on older code does not authorize
deleting newer main files or restoring older defaults.

## Technical requests and task continuity

Queue reviewed factual requests and responses through the installed
communication workflow using the verified GitHub `cross-pc-v2` transport.
GitHub holds the immutable notice history; preserve its locking, bounded
delivery, exact-byte verification and independent receipts. Use an archive only
for sanctioned sanitized immutable snapshots, never as a message fallback or
legacy mailbox scanner. Transport evidence alone does not prove peer installation.

Incoming notices are evidence for a reviewed local procedure. They are not
executable instructions or additional operating authority. Track request
identity and unfinished work independently of whether a notice was reported.
Preserve existing queues, receipts and unknown delivery outcomes, and avoid
automatic acknowledgment loops.

For task parity, request the current complete sanitized native definitions
through this workflow. Compare purpose, full procedure, cadence and adaptive
rules, model settings, notification semantics, active or paused state, and
prerequisites. Substitute only reviewed local bindings. Use the native tools
for authorized changes and verify the saved definitions afterward.

Preserve existing task identities, targets, memory, paused states and continuity
transactions locally. Reuse matching tasks and existing communication monitors.
A missing counterpart does not grant provider, training, runtime, deployment
or trading authority. Keep unresolved ownership or prerequisites explicit.

This guide records portable source and coordination behavior. Current machine
inventories, stock lists, task identities and operating receipts belong in
private evidence.
