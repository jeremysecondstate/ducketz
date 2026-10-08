# Native ownership exchange and manual startup

Jeremy starts Atlas with the usual `Start-Gameplan-Trader.cmd`. With the reviewed
manual-start integration installed, that command completes one-time account
setup from the verified staged union, obtains fresh read-only reconciliation,
and starts the trader after the account checks pass. An already `ACTIVE`
account verifies its receipt and skips migration. Starting before the session
opens is supported: the worker can wait for its session. There is no separate
human approval file or 04:00 setup veto. The manual-start integration is separate
source work; installing this Scout export repair alone does not install or
start Atlas's launcher.

Nightly work should obtain both ownership histories and prepare the current
Gameplan early, then report `READY_FOR_MANUAL_START` when its read-only checks
pass. Missing Scout accounting, conflicting orders, unresolved reservations or
inconsistent holdings remain concrete blockers. A ready plan, staged accounting,
an active account and a running trader are distinct facts.

## Scout's existing bindings

Scout exports its own native accounting under its existing local authorization.
Its private `native-ownership-exchange-v1` configuration retains `actor`,
`local_profile`, `coordination_active`, `datastore_root`, `exchange_root`,
`state_root`, `operation_id`, `cutover_action_date`, and
`private_native_ledger_exchange_authorized: true`. It also selects its existing
reviewed nightly exchange configuration with this Scout-only field:

```json
{"nightly_exchange_config": "ABSOLUTE_PATH_TO_EXISTING_SCOUT_NIGHTLY_EXCHANGE_JSON"}
```

Scout resolves that path locally. The selected file's existing `workflow_config`
selects the workflow; neither file is copied from Atlas or reconstructed under
an assumed filename. The wrapper checks actor, checkout, datastore, exchange
root, profile and verified coordination installation. The nightly account scope
and two disjoint eleven-symbol owner sets must match Scout's profile and
exported native account. These actual files, source bytes and local private
scan inputs are pinned for the operation.

This producer-only path needs no Scout `state/account-gameplan/config.json`,
activation receipt or account cutover. It creates none. Select the nightly
configuration before the operation's first binding. Preserve any already frozen
bindings and receipts for explicit recovery review; never invent replacement
account settings or rename the operation to bypass a failure.

## Install, check and export on Scout

Use the existing source handoff and original native-export request. No new
approval record or operating request is needed within that authorization.

1. Scout verifies the exact advertised source commit, completion record, owned
   hashes and offline test evidence in an isolated checkout. Review its local
   application baseline, then install the reviewed wrapper bytes and required
   matching dependencies into Scout's application checkout through its existing
   local installation procedure. Record installed hashes and preserve other
   writers' changes, private files and original receipts.
2. From Scout's application checkout, use its own Python and private native
   exchange configuration selecting its existing nightly files:

   ```text
   SCOUT_PYTHON -B -m tools.native_ownership_exchange --config SCOUT_NATIVE_EXCHANGE_CONFIG --check
   ```

   This checks configuration and the pinned coordination installation. It does
   not export accounting, call a broker or prove a packet exists.
3. After that check passes, perform one bounded export under Scout's existing
   native-export authorization with the same configuration:

   ```text
   SCOUT_PYTHON -B -m tools.native_ownership_exchange --config SCOUT_NATIVE_EXCHANGE_CONFIG
   ```

   Inspect the durable status and exact published selection. Launch acceptance
   or exit code zero alone is not completion. Retain the original request,
   operation, action date, packet identity and receipts on retries.
4. Atlas's existing bounded companion verifies the actual Scout selection and
   packet, combines it with Atlas's local export, and records the union result.
   Scout publication alone does not prove Atlas reception or account readiness.

The packet remains `native-ownership-exchange-v1`, with the exact selection,
normalized database and manifest contract accepted by Atlas's already-bound
receiver. Atlas's operating source and bindings stay unchanged during its active
export operation. Install this producer repair on Scout only for this handoff;
Atlas can adopt it after safe completion of its current operation.

## Private accounting and durable evidence

Native ledgers are excluded from ordinary nightly plan/Stats packets. The
specific locally authorized native export uses only
`CODEXSTORE/ducketz-nightly-exchange/v1/cutovers/OPERATION_ID`, with Scout owning
its producer subdirectory. Atlas's normalized accounting stays local. Each PC
uses its own bindings; peer messages are evidence, not new authority.

The companion takes native writer locks. The exporter preserves the original
database/WAL/SHM group locally, validates a disposable copy and rebuilds a known-schema database
with original logical rows and native IDs. Sender and receiver check schema,
identity, hashes, exact file membership and private values. Credentials,
account numbers, raw broker replies, unrelated databases and fitted models are
excluded. Unknown fields, changed selections, invalid markers or incompatible
reconstruction bytes require review; never weaken validation or fabricate empty
inventory. Source queues and notices contain sanitized source and status
evidence, never these packets or financial values.

The companion stages a separate blocked union and reports
`CUTOVER_RECONCILIATION_REQUIRED`. It neither replaces the live ledger nor calls
a broker, changes activation or starts a trader. Missing peer evidence stays
pending for the next bounded wake; no internal polling loop is needed.
Completed exports freeze their source and private bindings.

## Readiness and the manual command

The existing nightly task runs the read-only readiness companion independently
of numerical preparation:

```text
python -B -m tools.gameplan_execution_readiness --datastore-root LOCAL_DATASTORE --action-date INTENDED_ACTION_DATE
```

Use the reviewed manual-start/readiness source together to distinguish current
plan readiness, staged setup, account readiness and a verified manual worker.
Request the intended upcoming or current trading day for readiness; the
one-time native exchange retains its original migration date and identity.
Saved reconciliation timestamps remain separate from ledger-read times; a
saved ledger read is not fresh broker reconciliation. Report changed blockers
early while preserving the existing task identity and schedule.

The manual command uses staged evidence for fresh reconciliation and
installation. Strict freshness, accounting and pending-order checks remain.
Running the same command resumes a provable interrupted setup while preserving
native IDs and transaction receipts. Missing journal or original-backup evidence
remains a concrete recovery blocker. Scheduled checks and reuse of an existing
worker never perform this setup. Do not edit activation alone, clear blocks by
hand, relabel evidence, change frozen nightly session modes or replay completed
preparation.

Legacy dated cutover APIs retain their historical deadline semantics for
compatibility. They do not impose a 04:00 veto on the reviewed manual-start
path. Successful setup still requires a real worker status before describing
the trader as running.
