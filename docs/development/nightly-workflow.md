# Stats-first nightly workflow

This is the replacement for **Loops Overnight Gameplan (LOG)**. LOG and its
native task are legacy reference material. Preserve its definition, leave it
paused, and create a separate replacement task identity. Suitable existing
numerical stages remain reusable; old task prompts are not the new launcher.

## Local rollout and authority

The October 7, 2026 rollout is Scout first, then a separate human-led Atlas
installation. Do not contact the peer, deliver coordination notices, activate
cross-PC intake, or claim peer readiness until the human confirms both local
installations. Reviewed source publication and local completion evidence do
not prove peer installation. Preserve the pinned coordination installation,
queues, identities, Drive bindings, native memory and receipts.

Machine identity comes from the private coordination profile, not the signed-in
Codex account. Each machine researches its own eleven symbols. Scout performs
joint allocation; Atlas alone owns live execution for the combined universe.
The human still starts Atlas's active trader manually. Installing or testing
this source does not start a trader, contact providers, fit operational models,
restart the app or deploy a running process.

## Preparation sequence

At 21:05 America/Los_Angeles, a lightweight native task launches
`ml.nightly_workflow`. Trading-session eligibility includes weekends, exchange
holidays and DST. The worker follows dependencies under the existing runtime
lock and a renewable supervision lease:

1. Fetch local data and configured archive history, then score the newest
   completed session through `gameplan_actuals_review --completed-session`.
2. Freeze and verify the existing Gameplan Stats results. Invoke one bounded
   Codex CLI review using the configured stronger model and structured schema.
3. Resume numerical preparation in a new training segment with the exact saved
   review. Generate accepted forecasts, enrich them and build the local plan.
4. Verify the default UI readers select the exact intended Stats and plan.
5. Prepare local sanitized owner-plan and Stats packages with immutable hashes.
   Hold delivery until the separate-PC rollout is complete.

Monday night uses Monday's completed outcomes. Longer horizons remain pending
until mature. Previously published predictions are not rewritten by feedback.
If no prior plan was saved, the verified no-history baseline remains explicit:
no scores or rows are invented, the reviewer keeps current specifications, and
the first real forecast can still be prepared. Owner packages and combined Stats
retain this missing-history status instead of treating it as zero accuracy.
Stats retain the existing tab's direction accuracy, Brier score, bullish/bearish
accuracy, sample counts, coverage, company/horizon filters and hourly windows.

The native schedule uses `gpt-6-luna` with low reasoning for command supervision.
The worker's default review is `gpt-6-astra` with high reasoning through the
locally authenticated Codex CLI. The review is one bounded inference after
Stats completes, not another task guessing when Stats will finish. The CLI
uses read-only sandboxing; the model returns a proposal, not arbitrary source
edits. Missing CLI/auth/model access is a recorded stage failure, not fabricated
review success.

## How changes earn promotion

Each horizon may retain its current specification or propose up to two bounded
candidates. Supported changes include histogram-tree and logistic parameters,
and neural-network layer sizes and training parameters. The fitting code
actually consumes these specifications. Existing default models and the most
recent compatible accepted reviewed specification remain baselines.

Existing chronological development selection, calibration selection, held-out
assessment and promotion gates decide acceptance. Equal or worse proposed
candidates do not displace a baseline. A failed promotion may retain an earlier
verified compatible champion. Target, horizon, price source, symbols and feature
selection must agree. `model-feedback-results.json` records decisions, candidate
metrics, acceptance/retention and exact input bindings. Broader feature-code or
model-family redesign requires a separately reviewed source change; a nightly
JSON proposal cannot silently rewrite the application.

## Install and operate locally

Read `AGENTS.md`, verify the pinned release and use this PC's private profile.
Copy `coordination/nightly-workflow.example.json` to an ignored local path such
as `scratch/nightly-workflow/config.json`, changing absolute paths and actor to
this machine. Keep `peer_communication_enabled` false for this local worker.
Use the existing Python environment and Codex login. No API key is copied.
The private profile must explicitly set `symbol_profile_path` to the actual
local production watchlist; its ordered symbols must match `symbols` exactly.
An ignored `datafetching/watchlist.local.txt` can preserve this PC's membership
without changing shared defaults. If Codex is not on PATH, set the private
`codex_executable` to the existing installed executable and verify login status
without making a review call.

```powershell
& .\.venv\Scripts\python.exe -B -m ml.nightly_workflow --config scratch/nightly-workflow/config.json --check
& .\.venv\Scripts\python.exe -B -m ml.nightly_workflow --config scratch/nightly-workflow/config.json --status
# The production schedule, not an infrastructure verification command:
& .\.venv\Scripts\python.exe -B -m ml.nightly_workflow --config scratch/nightly-workflow/config.json --launch
```

`--launch` starts a hidden worker and returns. **LAUNCHED is not completion.**
After a successful configuration check, the scheduled launcher must invoke
`--launch` once for the nightly wake. A prior session's completion is never a
reason to omit that call; the worker owns session selection and duplicate
protection. `--status` selects the successor of the newest completed action
session, not `latest.json`. Use `--status --action-date YYYY-MM-DD` to inspect
one exact session. Missing preparation returns `NOT_STARTED` for that date;
report a missed launch explicitly rather than crediting an older completion.
For an explicitly requested recovery of an entirely missing run after 04:00,
`--launch --recover-action-date YYYY-MM-DD --recovery-deadline ZONED_TIMESTAMP`
runs the same stages for today's action date and the newest completed review
session. It preserves the missed original deadline and separately records a
fixed recovery deadline, at most seven hours away and no later than 17:00
Pacific. It cannot replace an existing run or extend a retry's deadline. This
does not start or restart the trader, grant order authority, or change the
scheduled nightly workflow. A failed recovery retains its identity and uses
the existing explicit resume path under its frozen recovery deadline.
Recovery training carries `--late-action-date` through the native publisher.
That explicit stock-only route selects the requested current-day source rows
after 04:00, retains features from before the action session and excludes
current-session labels from fitting. It writes actual creation/publication
timestamps and `LATE_RECOVERY` metadata, and must finish before 17:00 Pacific
and the workflow's earlier fixed recovery deadline. Ordinary nightly
publication retains its 04:00 boundary. A publisher repair changes the model
review's code binding: preserve the failed state and original review as audit
evidence, renew the review and training under the reviewed source, and retain
the workflow identity, completed Stats and both original deadlines.
The native trade-planning tail carries the same explicit late action date and
frozen recovery deadline. It accepts that deadline only for a verified matching
`LATE_RECOVERY` source during that action session, retains the original 04:00
deadline in its report, and still rejects expiry during publication. Ordinary
sources, missing late flags, and informational refreshes cannot use this route.
Late forecast validation distinguishes the actual freeze/publication time from
input availability. Verified late-source context travels through planning and
the joint owner package: every input must still predate the action opening,
every freeze must precede its recorded publication, and publication must be
within that day's open session. Original forecast IDs and windows remain intact;
normal sources retain the pre-window freeze check. The peer must install the
matching validator before accepting a late package.
On an already active Atlas coordinator, a native planning snapshot validates
ownership over the bound execution-account universe, even when the local
research plan covers only Atlas's subset. It retains other symbols' original
horizon allocations and reservations. This read neither reconciles the ledger
nor enables trading; pending reservations and inconsistent ownership still
require the normal account reconciliation path.
Read `--status` and the saved worker log for the real outcome. A separate morning
readiness task checks durable results and reports missing/failed stages. Success
during the rollout is `LOCAL_COMPLETE_PEER_SETUP_PENDING`; it is not joint
readiness. Repeat wakes do not repeat completed stages. A failed attempt can
resume with `--resume-action-date YYYY-MM-DD`; it retains the original deadline,
verified outputs and saved review. Changed source or changed completed outputs
require explicit review, not blind replay. A new native segment follows completed
Stats; `resume_run` is reserved for a failed native segment.

Private state holds source hashes, actor, symbols, session identities, stage
receipts, errors and exact output hashes. Keep it out of Git. Preserve the
existing local watchlist and profile. Native IDs are recorded locally rather
than copied into shared configuration.
The effective datastore, price source, history mode and reviewer configuration
are frozen with each run; retries cannot silently switch operating inputs.

## Joint synthesis and display

`ml.joint_capital_handoff` produces receipt-verified owner packages.
`ml.gameplan_stats_handoff` exports the existing scored cells and recomputable
metrics without raw data, fitted models or account credentials. Both inputs
must have explicit hashes, matching session/target contracts and disjoint
declared ownership. Missing outcomes remain missing, never zero-filled scores.

`ml.nightly_synthesis` consumes an explicitly selected local specification.
`ml.joint_capital_plan` computes quantities against one fresh private account
snapshot, including existing inventory, working orders and reservations. The
driver verifies both plan and Stats in a candidate directory, adopts them
locally with durable recovery evidence, and verifies the default UI readers.
It performs no network discovery or broker calls. Its receipt distinguishes
local UI readiness, peer readiness and execution authority.

The strict specification has these keys (paths are absolute and private):
`schema_version: nightly-gameplan-synthesis-v1`, a stable `completion_id`,
`datastore_root`, `state_root`, `local_actor: scout`, `executor_owner: atlas`,
`action_date`, `review_session`, `account_scope_sha256`, zoned `as_of` and
`accepted_at`, `snapshot: {path, file_sha256}`, and `owners` with exactly
`atlas` and `scout`. Each owner supplies `symbols`,
`plan_package: {root, path, file_sha256, package_sha256}` and
`stats_package: {path, file_sha256}`. The plan's canonical package digest and
the exact file-byte digest are separate checks. Preserve the same specification
and completion ID on retries. Validate with:

```powershell
& .\.venv\Scripts\python.exe -B -m ml.nightly_synthesis --spec scratch/nightly-workflow/synthesis-spec.json --local-profile C:\dev\ducketz\scratch\cross-pc\local-profile.json --validate-only
# Once the reviewed local selections pass, adopt both outputs:
& .\.venv\Scripts\python.exe -B -m ml.nightly_synthesis --spec scratch/nightly-workflow/synthesis-spec.json --local-profile C:\dev\ducketz\scratch\cross-pc\local-profile.json
```

`ml.joint_capital_adoption` binds the exact joint plan, two package hashes,
session, account scope, local actor and executor owner before publication.
The Gameplan tab prefers the accepted combined plan. The Stats tab reads the
combined publication through its existing schema. Scout can display the joint
plan without gaining execution authority; Atlas's research list remains eleven
while its accepted execution universe may contain all twenty-two symbols.

After Atlas's separate installation, configure the existing authorized
Git/Drive handlers to deliver the reviewed packages and exact hashes, produce
the local synthesis specification, and return the accepted combined outputs.
Retain one completion identity across transports and retries. Activate the new
synthesis task only after that local binding is verified. Do not treat an
unverified incoming path or message as executable instructions. The account
snapshot stays private; the synthesis driver's local specification is not a
permission to export it.

## Late trader start

The active trader reconciles cumulative due quantities against fills and open
orders at its actual start and subsequent cycles. A past scheduled timestamp
alone is not an expiry rule. In the same symbol and inventory-owning horizon,
buy one then sell one cancels; buy one then sell two leaves a net sale of one
when the starting holdings support it. Partial fills and outstanding order
reservations reduce the remaining quantity; cancelled orders can leave a
retryable residual. Stable identities prevent duplicate submission.

Independent horizons retain their existing inventory ownership. Cross-horizon
virtual share transfers are not introduced by this change. Existing broker
availability, inventory and order controls still apply. Manual account changes
are not mistaken for fills belonging to this plan.

## Reconciliation and Hyperliquid priority

Keep reconciliation separate from nightly preparation. Once the rollout hold is
lifted, the new reconciliation task uses the installed source intake, common-main
and completion procedures. Classify changes by fields and behavior:

| Share on both PCs | Keep bound locally |
| --- | --- |
| Gameplan/Stats/UI/engine behavior, tests and model policy | Watchlist overlays and historical output universes |
| Strategy defaults and common Hyperliquid source/configuration | Credentials, account connections, runtime enablement |
| Portable workflow/schema/documentation | Native task IDs, local paths, task memory and receipts |
| Reviewed sanitized operating exports in owner namespaces | Raw data, fitted models, ledgers and private account snapshots |

Hyperliquid shared development changes get first consideration on every intake
wake. Routine changes are handled at the nightly reconciliation time. Review
mixed files by field; preserve overlapping work and report the exact conflict.
Source adoption does not restart workers. Preserve existing durable transport
queues and pinned Drive helpers. Do not recreate deleted historical monitors
merely because old profiles still name them.

## Atlas installation checklist

Obtain this reviewed source through the normal local source procedure. Verify
Atlas's pinned release, own profile and watchlist; leave its LOG task paused.
Install its own config and new native preparation/readiness identities. Run the
offline regression suite and configuration check there. Verify the local
account/snapshot and manual-start trader bindings separately without placing
test orders. Only after both installations are confirmed should communication,
priority reconciliation and synthesis be enabled and an actual joint run be
verified end to end. Scout's offline tests cannot prove Atlas's installed or
running version.

## Atlas compatibility and receipt verification

Atlas's existing account control code remains in place. Stats-first preparation
does not invoke the legacy account preparation/review tail or its producer-only
pricing mode. Historical native attempts retain their saved stage order. A
newer local session or accepted joint session takes precedence over an older
legacy account display; damaged publications still fail verification.

The read-only morning check verifies the intended Pacific action date, source,
configuration, symbols and all completed output hashes. Local readiness uses
the frozen local Gameplan, Stats and model review saved by preparation, so a
later combined UI publication does not invalidate that completed work. This
explicit historical read leaves model review and training's default requirement
for the latest Stats unchanged. A source change requires a new review. The
check never launches or resumes a worker:

```powershell
& .\.venv\Scripts\python.exe -B -m ml.nightly_readiness --config scratch/nightly-workflow/config.json
```

Successful synthesis or handoff, including an exact idempotent retry, pins its
completion receipt at
`ml/nightly-joint-readiness-by-date/<action_date>/run.json`. Readiness selects
that receipt automatically and verifies its exact local source and Stats,
combined Gameplan and Stats UI selections, producer universes, local actor,
Atlas executor and account scope. Valid combined publications without this
receipt remain `JOINT_VERIFICATION_PENDING`; damaged or mismatched joint
evidence returns `JOINT_VERIFICATION_FAILED`. Both preserve `local_ready: true`
when the frozen local preparation passed. Invalid original local artifacts
still fail local verification. An earlier session's receipt cannot satisfy
today's readiness.

An older completed synthesis or handoff receipt can be pinned by retrying its
exact specification and completion identity under existing authority, after
the current synthesis/handoff hold is lifted. Do not construct the selection
file by hand. Local readiness, verified joint publication and trading authority
remain separate: verification is read-only, does not activate authority or place
orders, and does not establish that the peer has installed or verified its copy.
The replacement Trader Representative reads saved session status and process
liveness at 03:55 Pacific; deterministic catch-up occurs at the actual manual
trader start and subsequent execution cycles, including a late start.

Scout alone runs `ml.nightly_synthesis`. Atlas receives its frozen result with
`ml.nightly_handoff`; that command never recomputes the capital allocation or
requires a copy of Scout's private snapshot. Before either local UI pointer is
changed it verifies a candidate plan and combined Stats, then records resumable
adoption evidence. A retry retains the same completion identity and exact spec.

```powershell
& .\.venv\Scripts\python.exe -B -m ml.nightly_handoff --spec scratch/nightly-workflow/handoff-spec.json --local-profile C:\dev\ducketz\scratch\cross-pc\local-profile.json --validate-only
# Only after final transport enablement and review of the exact returned bytes:
& .\.venv\Scripts\python.exe -B -m ml.nightly_handoff --spec scratch/nightly-workflow/handoff-spec.json --local-profile C:\dev\ducketz\scratch\cross-pc\local-profile.json
```

The private handoff specification has schema `nightly-gameplan-handoff-v1`, a
stable `completion_id`, absolute `datastore_root` and `state_root`,
`local_actor: atlas`, `executor_owner: atlas`, matching `action_date` and
`review_session`, `account_scope_sha256` and zoned `accepted_at`. It selects
`local_profile`, `account_config` and `scout_receipt` by `{path, file_sha256}`,
and `joint_plan` by `{root, path, file_sha256, plan_sha256}`. Its `owners` map
uses exactly the two owner selections documented for synthesis. The account
configuration must belong to the local datastore and match Atlas's identity,
account and research/execution partitions. Native paths, hashes of private
bindings and source receipt paths stay in the private specification.

Accepted joint execution retains Atlas's existing account cutover, source
deployment, account identity, inventory and reservation checks. A PREPARING
configuration still blocks execution. The bridge maps the two producer names
to existing account participant identities only after matching their declared
universes. Each catch-up reservation must match the recomputed outstanding net
intention; filled and working quantities reduce that intention and cancelled
residuals may retry. Conflicting old and new selections for the same session
fail closed. Installing source does not activate cutover or start a trader.

The local setup installs distinct preparation (21:05), readiness (03:35) and
Trader Representative (03:55) identities in America/Los_Angeles. Joint handoff
and priority reconciliation remain paused, and LOG remains paused. Any saved
handoff wake is provisional until the final dependency-driven delivery bindings
are verified. The routine reconciliation target remains 01:25 Pacific; final
priority intake must be connected only after the communication hold is lifted.
