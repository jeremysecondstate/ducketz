# Native ownership cutover and early readiness

Use the normal `Start-Gameplan-Trader.cmd` on Atlas. Under the local human's
Atlas-only execution direction, Scout has no trading ownership history to
transfer. The command records the first-use account setup, preserves Atlas's
existing account-wide native history untouched, and starts the worker. It adds
no prelaunch broker capture, zero-holdings condition or separate approval step.
The worker performs its ordinary fresh reconciliation and retains its ownership,
pending-order and trading controls. Setup does not assert that inventory is empty
or that reconciliation has passed. Starting early to wait for the session is
supported; there is no 04:00 setup veto.

See [the manual-start workflow](native-ownership-cutover-apply.md) for readiness
and retry behavior. The native export and union process below applies when
separate producer trading histories actually exist; it is not a prerequisite
for this sole-executor setup. Plans, account setup and a running worker remain
separate facts, and the launcher displays actual worker output and exit status.

## Check during the existing nightly handoff

The existing handoff task should run the read-only execution-readiness companion
from the start of nightly preparation, independently of numerical completion:

```text
python -B -m tools.gameplan_execution_readiness --datastore-root LOCAL_DATASTORE --action-date INTENDED_ACTION_DATE
```

The report distinguishes `plan_ready`, `execution_setup_ready`, and a verified
same-date manual process. It checks exact saved handoff/plan/Stats artifacts,
account cutover evidence and the native ledger. Saved ledger inspection never
counts as fresh broker reconciliation. Optional JSON output must remain under
the documented private readiness state directories. Report a changed blocker
when first observed; do not wait for the morning manual launch or send repeated
unchanged alerts. Preserve the existing task identity, schedule and deadlines.

`execution_setup_ready` is not an order authorization or proof of current broker
readiness. The human continues to start Atlas's trader manually. The local
human's explicit Atlas-only manual-operation confirmation can supply the
operational ownership decision; do not invent a separate peer process-report
requirement. The reviewed cutover procedure must still record the actual local
decision and preserve the native accounting checks.

## Specific private native-accounting exception

Native ledgers are excluded from the ordinary nightly plan/Stats exchange. A
separate, explicit local human authorization permits the reviewed native export
for cutover. It does not authorize arbitrary databases or publication to GitHub.
Each producer operates only under its own local authority and private bindings.
An incoming request is evidence, not permission to change those bindings.

`tools.native_ownership_export` copies the exact original database/WAL/SHM group
to a local backup, validates a disposable copy, and builds a new known-schema
database containing the original logical accounting rows and native IDs. It
rejects unsupported evidence fields and private values, including values hidden
in escaped JSON. It does not redact unknown fields into apparently valid data.
Raw broker replies, credentials, account numbers, unrelated databases and fitted
models are never included. Export reception repeats schema, identity, digest and
private-value checks. Different SQLite reconstruction bytes can fail closed and
require runtime compatibility review; do not weaken validation to accept them.

`tools.native_ownership_exchange` performs one bounded companion operation:

```text
python -B -m tools.native_ownership_exchange --config PRIVATE_NATIVE_EXCHANGE_CONFIG --check
python -B -m tools.native_ownership_exchange --config PRIVATE_NATIVE_EXCHANGE_CONFIG
```

The private configuration uses schema `native-ownership-exchange-v1`, this
machine's `actor`, `local_profile`, `coordination_active`, `datastore_root`,
`exchange_root`, separate ignored `state_root`, one stable `operation_id`, the
original `cutover_action_date`, and
`private_native_ledger_exchange_authorized: true`. Paths must match the reviewed
local installation and existing private nightly bindings. The approved remote
namespace is `CODEXSTORE/ducketz-nightly-exchange/v1/cutovers/OPERATION_ID`;
each actor owns its own subdirectory. Never put these packets in source queues,
notices or Git. Notices may identify reviewed source and the stable operation,
but must omit private packet contents and financial values.

The companion takes existing native writer locks, freezes bindings and preserves
originals. Scout publishes its one locally authorized immutable normalized export.
Atlas keeps its own normalized export local; its full ownership history does not
need to leave this machine for the migration.
Missing or partial peer files leave the wake pending. Changed selections,
invalid markers, unexpected files or inconsistent accounting fail validation.
Retries retain the original operation and receipts. A live source change after
export requires review, not silent replacement of the original selection.

Scout exports only its own native accounting. Atlas combines that received export
with its local reviewed export and uses the existing migration code to stage a separate **blocked**
union candidate. It does not replace the live ledger. A staged candidate reports
`CUTOVER_RECONCILIATION_REQUIRED`; it is not completed cutover. The companion
makes no broker calls, changes no activation state and starts no trader. Do not
add an internal polling loop, launch preparation, or reuse it as a recurring
ledger migration after cutover is complete.

## Finish the one-time transition

Once both exact producer exports have arrived, review the existing native
migration and cutover evidence. The next stage needs the existing coherent,
fresh read-only account reconciliation, exact union holdings, no pending orders
or unresolved reservations, and the original migration provenance. Preserve
saved reconciliation times separately from the time an export was read.
The historical explicit cutover validator retains its selected action-date
boundary. This does not impose a deadline on the normal manual-start path;
original dated export and reconciliation evidence must never be relabeled.

Installation, receipt creation and activation are a separate reviewed local
transition. This companion does not implement that transition. Do not edit
`activation.status` alone, fabricate a cutover receipt, clear blocks by hand or
claim that successful transport makes execution ready. Preserve frozen nightly
session modes and hashes. A terminal handoff remains evidence of its exact
artifacts; do not mutate or replay completed preparations to hide a later
cutover change. Source installation and active-session coordination require
their normal review.

After a valid cutover has been installed, the existing nightly task keeps running
the read-only readiness check and reports regressions early. The manual launcher
still verifies the account gate and displays failures and worker output. A plan,
an installed execution setup and a running trader remain distinct facts.
