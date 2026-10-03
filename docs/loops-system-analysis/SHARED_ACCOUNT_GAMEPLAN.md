# Shared-account Gameplan and sole Atlas trader

The October 2 human decision is two forecast producers and **one Atlas / PC-original
execution host**. Atlas and Scout keep their own configured symbol universes,
model assessments and frozen accuracy records. One combined account calculation
uses both universes, one cash balance and one chronological ledger. With eleven
disjoint symbols per producer there are 528 combined forecasts; native plans and
stock-only options-intent receipts retain 264 rows each on their respective PCs.

This implementation is opt-in. Source publication, installation, ownership
consolidation, peer fencing and runtime activation are separate evidence. A
source commit, exported bundle or planning receipt does not establish a live
coordinated account. No command below enables controls or starts a trader.

Scout's [joint-capital composition](JOINT_ACCOUNT_CAPITAL.md) supplies the shared
projection. The account adapter adds native source proofs, the original deadline,
a 60-second account snapshot bound, publication and execution gates. It preserves
Scout's joint forecast identities while retaining each original source receipt
and forecast ID separately. The native tail runs this composition once; it does
not add two independently sized plans. The existing offline joint-capital APIs
remain available with their own explicit schemas.

## Workflow

1. Each local forecast pipeline completes its own frozen Gameplan and native
   source-only price preparation. Configured producers use the explicit
   `ACCOUNT_PRODUCER_SOURCE` mode: prices and forecasts are retained, while
   quantities and cash stay unavailable until the combined calculation. They do
   not consult a stale local ownership book or simulate a second cash balance.
   The optional `account_gameplan_preparation` stage exports
   only those pinned inputs. Original probabilities, horizons, model status,
   target/source contracts, directions, IDs and actual observation times remain
   traceable. A registered source-selection handoff must select that exact plan.
   Unconfigured pipelines retain the existing native account-planning behavior.
2. The completed source bundle is made available through the existing reviewed
   operating-artifact transport. Git/coordination notices convey immutable
   artifacts and pins; they are not a real-time cash lock or execution authority.
   The local per-date input registry identifies both exact transported bundles.
   Missing, incomplete, altered, overlapping or different-date sources stop the
   combined calculation. The supervisor must not substitute an older source.
3. Scout finishes its export without waiting for Atlas. Atlas reads one fresh,
   coherent account snapshot covering the explicit union, including outside
   account exposure and broker-visible pending commitments. It uses the native
   consolidated horizon ledger for ownership. It never adds two account balances
   or treats an earlier producer snapshot as fresh authority.
4. Atlas projects all symbols chronologically with the existing allocation,
   cash buffer, exposure limits, sale order and buy priority. Planned sales may
   fund later **conditional planning** purchases. Live buying capacity comes only
   from broker-usable cash and reconciled actual fills. An expected sale is never
   a live cash credit.
5. The immutable combined publication includes all rows, each producer's filtered
   readable view, the entire hourly account portfolio, ordered before/change/after
   cash events, ending holdings, no-fill baseline and source pins. Both UIs retain
   the same full-account balances. Neither UI independently recalculates cash.
6. The native actuals review continues to score each original publication. A new
   final `account_actuals_summary` stage exports its exact original-source result
   and combines both verified reviews on Atlas after successor preparation.
   Combined accuracy joins exact source-bound reviews, sums eligible correct and
   evaluated counts, and retains pending, missing, neutral and model-status
   breakdowns. It does not average producer percentages or interpret price
   observations as broker fills or realized profit.
   A missing peer review records `PENDING_PEER_RESULT` and fails only this final
   stage; after verified arrival, resume that stage with the same successor and
   deadline. A local result without its original source is explicitly unavailable.

The 21:05 Pacific schedule, next-exchange-session 04:00 deadline and model gates
remain unchanged. The optional stage is recorded in new configured attempts.
Resume uses the original saved stage list, source pins, configuration and deadline;
an older failed attempt never gains the new stage. The source export survives a
missing-peer failure, so recovery does not retrain completed stages.

## Execution

After verified activation, the user starts the existing manual Gameplan launcher
on Atlas once. It consumes both producers' due forecasts and obtains current
quotes for their explicit union. Scout's producer binding refuses execution.
The legacy stock one-shot entrypoint and other sizing modes are also refused on a
configured combined account. There is no automatic failover to Scout. Existing
native session and cycle locks continue to protect the sole Atlas worker.

The additional host-local account authority uses transactional reservations,
idempotent identities and a fenced lease. Each request references a real native
horizon reservation and allocation. It creates no alternate ownership ledger.
Reservation commits occur before submission; the final source, control, account,
quote and window gates are retained immediately before the broker request.

Broker-visible commitments overlap local reservations only when their exact
broker identities and quantities match. Partial fills require complete execution
evidence. Missing IDs, timeouts and uncertain submissions retain commitments and
block unsafe retries; lease expiry does not release money. Filled sales create
no credit in the reservation database. Only a newer coherent broker snapshot can
establish usable cash. Restart reuses the same authority and native ownership
databases. Copying these databases to two active hosts is not supported.

The approved own-horizon sales and capped longer-horizon fallback remain intact.
The coordinator does not change allocation weights, fallback ceilings, thresholds,
model assessments or actual-quote behavior. Planning ranges never set live order
prices or authorize spending. Shared cash priority uses the existing published
probability, then shorter horizon and symbol ordering across the combined universe.

## Private configuration and reviewed cutover

`state/account-gameplan/config.json` is a private local binding with schema
`sole-coordinator-account-gameplan-v1`. It contains `machine_id`, coordinator
`pc-original`, both disjoint `participants` symbol lists, the existing stable
account fingerprint, and `activation`. Local watchlists stay unchanged.
`activation.binding_sha256` is the canonical digest of the other fields.
`PREPARING` enables source preparation while blocking execution. `ACTIVE` still
requires a hash-pinned local cutover receipt checked by `verify_cutover`.

Before activation, review the exact installed source on both PCs, confirm all old
traders and writers have finished, fence Scout's execution paths and retain its
local proof. Consolidate only explicitly reviewed native ledger snapshots into
a **new** candidate with `ml.account_gameplan.migration`. It preserves original
IDs/evidence and rejects conflicting ownership, changing source bytes and unresolved
orders. Historical fallback records with the same account/date can contain different
producer baselines. The migration archives both complete original databases and
their provenance; conflicting past-only fallback keys are excluded from the new
active book, with no invented merged baseline. Any fallback conflict on or after
the explicit cutover action date is rejected. The future union baseline must come
from the native reconciled account snapshot. The result is deliberately blocked
and unreconciled. Never replace a live
ledger, discard a reservation, clear migration blocks or infer successful
reconciliation merely because offline consolidation succeeded.

A subsequent authorized cutover must reconcile current broker holdings and all
native ownership on Atlas, prove the full union ready, preserve both original
ledgers, and record the peer-fence receipt, migration manifest, fresh union
reconciliation and installed commit. The implementation has no automatic
activation command. The local receipt attests those reviewed facts; it is not a
substitute for obtaining their evidence. Runtime deployment remains pending until
this step is completed and verified. The user performs the initial manual start.

`ml.account_gameplan.cutover.reconcile_migration_candidate` supplies the bounded
union reconciliation step. It consumes an explicit migration-manifest pin, the
two reviewed symbol partitions, and matching pinned native `PortfolioEvidence`
and `PortfolioState` from one coherent GET-only capture. The caller checks stable
account identity before and after capture and retains the ordinary session/cycle
locks. Evidence must be at most 60 seconds old, before the cutover session's 04:00
opening, cover the exact union and include zero execution budgets and complete
order identities with no pending orders. The function makes no broker call.

The function reconstructs and audits the blocked migration from its archived
originals, then calls unchanged native reconciliation against **each** producer's
saved baseline in isolated copies. An unexplained reduction in the older baseline
cannot be hidden by the newer producer's snapshot. After both checks pass, it
releases only the exact migration-marker blocks in a new union copy and performs
native reconciliation again. Foreign blocks, unresolved orders, source changes
and incomplete or stale observations fail closed. It preserves the original
ledgers, blocked candidate, native IDs and all prior accounting evidence.

The new output contains the observed portfolio, both baseline results, the native
union result, a hashed manifest and an `UNION_RECONCILIATION_VERIFIED` receipt.
Its complete pinned broker snapshot and ledgers are private ownership evidence,
including account cash and holdings; they do not belong in forecast transport.
A final guard failure marks that receipt failed. This is candidate readiness:
the function never replaces a live ledger, installs configuration, creates an
ACTIVE cutover receipt, changes controls or starts a trader. Peer fencing and
the reviewed installation/activation procedure remain separate requirements.

Daily execution reads the immutable `ml/account-gameplan-by-date/<date>/run.json`
selection, bound to the host configuration and both source packages. The separate
`ml/account-gameplan-latest/run.json` is the UI pointer. A missing or bad combined
pointer must not quietly display a legacy local projection as the shared plan.
Neither a peer notice nor an edited current pointer can replace an already
selected execution source during the session.

## Offline operations and evidence

`python -B -m ml.account_gameplan.cli --help` exposes explicit source export,
source verification, combined planning, publication verification and accuracy
aggregation. Supply exact paths, producer registries and manifest pins; these
commands do not acquire data, capture broker state, train or submit orders. The
explicit `select-view` operation described below selects only a verified UI view.
A planning snapshot must be fresh at publication and completion
must precede the original opening deadline. Test clock injection exists only in
the Python API, not as a production CLI deadline bypass.

The native preparation module owns the configured nightly handoff. Its per-date
input registry contains source paths/hashes and symbols, never a second cash
balance. Artifact arrival, readiness and input registration require verified exact
bytes through the authorized transport. A completed producer export is
`SOURCE_READY`; it is not a claim that the combined plan or trader is ready.

After the reviewed artifact transport delivers the complete combined publication
to Scout's local `ml/account-gameplan-runs` directory, `select-view` verifies its
manifest pin, both symbol partitions and account binding, then selects Scout's
portion using Scout's own private configuration fingerprint. It requires an exact
previous UI-pointer checksum (or explicit absence), refuses an older session or a
different generation for the same session, and never creates Atlas's dated
execution selection. Copying Atlas's host-bound UI pointer to Scout is invalid.
The command can run after opening because it grants no execution authority.

Transport and private cutover bindings must be verified on both machines before
activation. The source package supplies no background transport daemon and makes
no claim that a peer has installed or imported it. Existing authorized artifact
supervision provides exact local copies and registers their pins; source notices
alone cannot satisfy the nightly source or actuals registries.

The explicit `register-inputs` command validates both transported sources against
the private configuration before sealing the per-date registry. It accepts a
reviewed JSON document with exactly two source records; paths are local artifact
references, not instructions to download, execute or change operating controls.
`register-accuracy` does the same for the two original-source reviews and their
saved universes, including legitimate smaller historical universes. It never
rebuilds a past forecast or supplies a missing result.

Focused offline tests cover cash conservation/global priority, per-producer UI
agreement, source and selection tampering, absent peers, deadline/resume behavior,
duplicate/concurrent cash claims, lease fencing, unknown submissions, partial
fills, pending-order deduplication and strict ledger consolidation. Current native
session, engine, ownership and UI regression suites accompany these tests.
