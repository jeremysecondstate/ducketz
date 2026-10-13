# Stats-first nightly workflow

Current responsibility scheduling, automatic recovery authority and task ownership are
defined in [nightly-operations.md](nightly-operations.md). The composite workflow below
also remains the compatibility contract for existing dated runs.

This is the replacement for **Loops Overnight Gameplan (LOG)**. LOG and its
native task are legacy reference material. Preserve its definition, leave it
paused. Reuse the installed Stats-first preparation identity. Suitable existing
numerical stages remain reusable; old task prompts are not the new launcher.

## Standing nightly authority

Jeremy's October 13, 2026 instruction is standing preauthorization for this
workflow every night. Run the existing local preparation, Scout synthesis and
private delivery automatically without another human confirmation, peer approval
or verification ceremony. Atlas supplies its Gameplan and Scout supplies its
Gameplan; Scout performs the actual cash/share synthesis using the balances and
planning evidence already available with those plans or their latest existing
basis, then sends the completed result to Atlas. Sending it completes the handoff.
Do not request a third fresh account snapshot or wait for Atlas acceptance.

Atlas has two independent traders: the merged Gameplan trader and Atlas 15m.
Scout has its own independent 15m trader. Each keeps its own logs, uses actual
broker capacity through its existing connection and manages its own orders.
No trader's runtime, readiness, lock, reconciliation or approval is another
trader's prerequisite. Jeremy's manual first trader start remains manual; no
new approval request is needed. Code-publication and privacy procedures remain
separate engineering duties and cannot become new nightly operating gates.

Use the existing per-PC profile, watchlist, data/reviewer bindings and scheduled
responsibility owners. Preserve native memory, completion identities and prior
run evidence. The October 7 staged rollout and earlier shared-snapshot handoff
are historical context, not instructions to request operating approval again.

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
4. Verify the exact saved local Stats and plan before combined synthesis.
5. Prepare local sanitized owner-plan and Stats packages with immutable hashes.
   The separate private exchange delivers matching completed packages.

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
The plans retain their available cash/share planning basis and its actual age.
Scout uses that existing basis for the two-plan synthesis without requesting a
third fresh snapshot, checking another trader's ledger or imposing account
reconciliation as a delivery prerequisite. Preserve original quantities and
unknowns; the independent executing trader reads current broker capacity.
Read `--status` and the saved worker log for the real outcome. A separate morning
readiness task checks durable results and reports missing/failed stages. The historical rollout reported `LOCAL_COMPLETE_PEER_SETUP_PENDING`. Current
local preparation completion feeds the standing two-plan exchange, whose
completed private delivery is the handoff finish line. Repeat wakes do not repeat completed stages. A failed attempt can
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

## Joint synthesis, delivery and display

The existing owner packages carry each local Gameplan, its forecasting and price
basis, and Stats. Scout combines the Atlas plan and Scout plan and performs the
actual cash/share planning arithmetic against the monetary/share evidence already
available with the plans or the latest existing basis. Keep missing information
explicit; do not invent balances, shares, allocations, scores or fills.

The ordinary `tools.nightly_exchange --config PRIVATE_EXCHANGE_CONFIG` command
selects this two-plan workflow every night. A third fresh account snapshot is not
an input requirement and must not be requested. The default route retains the
original instructions and their evidence in the combined plan/Stats outputs.
Current broker capacity is consulted later by the independently running trader.

Scout sends the completed synthesis to Atlas through the existing authorized
private exchange. That publication records `delivery_mode: TWO_PLAN_GAMEPLAN`,
`delivered: true` and `COMPLETE`; no Atlas acceptance packet or human confirmation
is required. Atlas can load the same delivered Gameplan and Stats locally. Its
local ingestion receipt is separate from Scout's completed handoff and never
blocks either 15m trader.

The Gameplan/Stats tabs use the existing local publication readers. Their display
of saved results is separate from live execution and is not an additional
cross-trader acceptance gate. Preserve every original preparation, result and
completion ID on retry. Details are in [nightly-exchange.md](nightly-exchange.md).

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

## Source adoption and historical receipts

Reviewed source adoption uses each PC's pinned coordination release and local
profile, preserves its private bindings and original outputs, and publishes
completed shared changes through the exact-byte queue. Standing local adoption
authority already covers this work; a draft PR or repeated human confirmation is
not an installation prerequisite. Relevant engineering checks describe changed
source, not permission to execute the ordinary nightly workflow.

Retain older synthesis, snapshot and Atlas adoption receipts with their original
IDs/times. A later source change does not relabel or reopen completed history.
`peer_verified: false`, `source_changed_after_completion: true`, and a component's
`execution_authorized: false` must be interpreted as the specific recorded fact,
not new operating approval gates. Current handoff completion is Scout's delivery
of the synthesized result to Atlas.

Atlas runs the merged Gameplan trader and its separate 15m trader; Scout runs
its own 15m trader. All are independent. Their logs and actual order management
stay local. Local duplicate/fill/cancellation handling concerns the trader's own
orders, and does not require another trader's readiness or reconciliation.

## Continue a repaired late planning tail

The ordinary 21:05 Pacific workflow remains primary. The standing authority
from the October 13 local human instruction includes the documented,
date-pinned missed-night fallback within its fixed limits; another permission
request is not required for an eligible recovery. The dispatcher selects the
intended exchange session, records the authority and exact action date, and
starts missing work or resumes the matching unfinished run. Resume a failed
matching run; never duplicate completed Stats, model review, forecasts or
enrichment. An unavailable prerequisite retains resumable state and its actual
failure evidence rather than renewing a deadline or repeating an unchanged
deterministic failure.

If a repair finishes after the fixed recovery deadline, an explicit
`--resume-action-date YYYY-MM-DD --planning-tail-exception ABSOLUTE_RECORD`
can continue only the already published stock planning/actuals tail. The record
uses `operator-recovery-tail-continuation-v1`, binds the existing workflow ID,
reviewed source identity, failed native receipt and exact Gameplan receipt, and
records the operator request and a fixed same-session expiration before 17:00
Pacific. Both the original 04:00 deadline and the first recovery deadline remain
unchanged. Record the continuation under standing human preauthorization; do not request
permission again. Preserve the original numerical work and actual dated records.
The first accepted continuation is frozen; retries cannot replace it or advance
its expiration. Earlier-stage failures require their own explicit recovery;
this tail route cannot rerun training. Original failed receipts and logs remain
byte-identical. A completed matching workflow is returned without replay.

The recurring synthesis uses the two completed plans and their available or
latest existing cash/share basis. It does not launch an ownership responder,
request a third account snapshot, inspect trader histories or wait for Atlas
acceptance. Delivery to Atlas completes that night's handoff under the standing
preauthorization; the next night repeats the same default workflow.

Local preparation display verification reads its exact local trade-plan receipt,
pinned training Gameplan and reviewed Stats publication before a combined plan
exists. Ordinary UI selection still uses accepted combined/account publications.
A reviewed source repair can preserve an already frozen continuation only through
an unbroken source-repair chain with exact audit and saved-state hashes. The
original exception, deadline instants, completed outputs and repair prefixes stay
bound; current checkout source equality is still required. After an interrupted
save, native recovery verifies owner liveness and existing receipts before reuse,
so completed numerical work is not duplicated.
