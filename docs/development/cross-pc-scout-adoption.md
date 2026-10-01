# Scout adoption of cross-pc-v2

Scout starts from Atlas release
`7860a4a958193abb1943f13f56f6a445c28d8f61`. These notes cover local differences
found during adoption. The shared contract and task catalog remain cross-pc-v2
and catalog `2026-10-01.1`. A published Scout follow-up is a reviewed integration
candidate; it does not establish main integration or application deployment.

## Preserve the symbol profile before publication

Scout uses an ignored `datafetching/watchlist.local.txt`. The completion queue
rejects conventional `*.local.*` and `*.local` filenames in both owned operations
and dependencies before creating snapshots or test evidence. Shared defaults and
explicit example templates remain eligible for substantive review. Naming alone
does not prove that an example is sanitized.

Keep the existing symbol file unchanged. The installed private profile binds its
current symbols and records the file's digest locally. Do not publish local
profiles, credentials, machine paths, native task IDs, or chat targets.

## Use compatible locks during the transport cutover

Scout's v1 helpers acquire a lock by creating an exclusive file and remove that
file on release. The v2 helpers keep a persistent file and acquire a native
byte-range lock. Reusing a v1 path would not serialize these different schemes
and would leave a file that v1 treats as a stale lock.

Scout therefore binds new v2 source, inbox, and common-cache lock paths in its
private installation directory. Preserve all legacy lock paths and evidence.
Replace the native inbox/outbox prompts explicitly, let any old bounded run
finish, and verify the saved new prompts before relying on scheduled v2 work.
Never delete an unknown lock or silently resume the shared-mount transport.

The five retained Scout outbound bundles were already recorded as published and
reported at adoption preflight. Keep those receipts and original bundle bytes.
They do not become pending v2 deliveries merely because their directories exist.
Only newly reviewed findings or a necessary provenance-preserving conversion
should produce a new notice.

## Preserve Scout's task behavior

Reuse the existing five-minute inbox and message outbox. The outbox becomes the
authorized bounded source courier and GitHub notice publisher; its old Git
prohibition must be replaced, not retained beneath an appended contract suffix.
The inbox similarly replaces the legacy scanner instructions while retaining
its separate bounded follow-up of Scout's dedicated data-fetch monitor.

Scout has an independent fifteen-minute continuity monitor. Do not copy Atlas's
embedded rollover block into either communication monitor. Preserve the entire
Scout continuity prompt, its four-task transfer list, compaction threshold, and
transaction rules. If its journal is idle, a future preparation captures the
updated native definitions. If a rollover is in progress, finish or reconcile
that transaction before changing its captured prompts. Verify Scout's preserved
prompt explicitly; the generic Atlas-marker comparison is insufficient.

Keep the two existing completion monitors paused and preserve their different
chat targets and reporting requirements. Create missing operating counterparts
only within the catalog's Scout role ceilings:

- Overnight readiness, Loops health, weekly saved-Gameplan review, and weekly
  existing-research review may observe local saved evidence. These narrower
  observers do not run pipelines, collect provider data, train models, repair
  source, or infer fresh Scout activity from copied historical Atlas artifacts.
- Stock daytime supervision stays paused because Atlas retains live execution
  ownership. Hyperliquid watch and Paper improvement stay paused pending local
  ownership and operating authority. Options paper tracking remains disabled.
- Do not recreate the retired one-time installation or historical review.

Preserve existing native fields through the native tool and verify saved
definitions after each write. New standalone observers bind to the local
Ducketz project and Pacific business timezone, including daylight saving time.
Record any explicit local model preference where the catalog's inherited-chat
preference cannot apply to a new standalone task.

## Verification and reporting

Run the shared offline suites and the Scout fixtures for legacy-state
preservation, separate lock schemes, native heartbeat definitions, paused roles,
and local-profile publication rejection. Keep real task readback evidence and
before/after file hashes private. The sanitized adoption notice records exact
source commits, installed manifest digest, actual test times/results, purpose
reconciliation, and remaining blockers.

Compare shared source against the same immutable reference on both PCs. Include
dirty changes and line-ending deviations. A matching protocol or successful push
does not prove application or runtime parity. Preserve separate evidence for
main integration, installed coordination code, application source, and running
processes.
