# Native inventory readiness at manual Start

Use the normal command on Atlas:

```powershell
& "C:\dev\ducketz\Start-Gameplan-Trader.cmd"
```

When Atlas is the sole executor and Scout has no trading history, the existing
local direction selects first-use account setup. The manual command records it without
importing nonexistent Scout history, changing the native ledger, or adding a
prelaunch broker check. The worker then performs its ordinary fresh account
reconciliation and applies its existing ownership and order safeguards. First-use
setup does not claim that holdings are zero or that broker reconciliation passed.
Atlas's existing account-wide ownership history remains authoritative.

When existing producer histories need preservation, this manual command verifies
the staged native ownership histories, checks the current account through one read-only broker
snapshot, installs the coherent native union, records local account readiness,
and then starts the worker. The launcher performs this setup before changing
manual intent or starting a process. No separate approval document, hand-written
JSON specification, or hash-copying step is required. The local human's existing
Atlas-only manual operating instruction is sufficient; no Scout process proof
is required.

The overnight handoff prepares the plan and checks either the authorized
first-use configuration or the required staged histories early. Readiness should
be established
by 04:00 Pacific; that is a preparation target, not a deadline that prevents a
later manual Start. The worker's existing market-session and trade-entry rules
still apply. This document describes implemented behavior, not evidence that a
production activation or trader start has occurred.

## What readiness means

The read-only execution-readiness report distinguishes the following states:

| State | Meaning |
| --- | --- |
| `READY_FOR_MANUAL_START` | The requested day's completed plan and authorized first-use configuration or staged native union verify. The account can remain PREPARING. The worker still performs its normal reconciliation. |
| `EXECUTION_SETUP_READY` | The ACTIVE account's local setup and saved plan evidence verify. This does not establish a running worker or current broker readiness. |
| `EXECUTION_SETUP_BLOCKED` | A concrete prerequisite is missing or inconsistent; the report identifies it. |

`SCOUT_OWNERSHIP_HISTORY_PENDING` applies when actual Scout history must be
preserved and has not arrived. It is not a requirement to manufacture history
for an authorized first-use setup. A completed financial planning packet does
not contain the native IDs
needed to reconstruct ownership. The readiness check does not call a broker to
fill that gap. Other real blockers include changed native history, unresolved
reservations, pending orders, damaged evidence, and ownership that disagrees with
current account holdings. The command returns a failure reason before starting
a worker when these checks fail.

Scheduled launches and existing-worker adoption remain read-only preflight
paths. Only the explicit manual Start path calls
`ensure_gameplan_account_ready(root, sizing_policy=...)`. Its success result is
`ACCOUNT_READY` with `ready=true`; the helper itself never starts a process or
places, cancels, or replaces an order. An already ACTIVE account verifies its
existing activation receipt and skips native transfer, union installation, and
broker capture. Starting early to wait for a future session remains supported.

## Retry with the same command

If setup is interrupted, run `Start-Gameplan-Trader.cmd` again. A known incomplete
first-use configuration write resumes from its exact retained receipt without
touching accounting. A known incomplete history-installation attempt resumes
automatically: it either retries the original verified candidate
or continues from the exact union already installed. It preserves committed
native IDs and snapshot history and obtains a new fresh account observation
when needed. An already ACTIVE account skips migration. Unknown ledger changes,
conflicting locks, or inconsistent receipts remain a concrete blocker.

History installation retains the original native files and transaction receipts.
It checks the
live database before and after installation and only marks the account ACTIVE
after reconciliation succeeds. The read-only account observation must remain
within 60 seconds through final activation checks. Existing controls are enabled
by the launcher only after setup and the account gates pass.

## Readiness and private evidence

A ready plan or `ACCOUNT_READY` message does not mean a trader is running. The
worker reports `SLEEPING_UNTIL_OPEN` while waiting and `SESSION_STARTED` when its
session begins. The launcher displays worker output and the actual exit code.
A blocked setup returns its reason before controls or a worker are started.

Keep the original operation, migration date, packet selections, completion IDs,
and receipts across retries. A later manual start records its actual observation
time without relabeling the old preparation. Completed nightly sessions retain
their frozen planning mode; setup does not replay preparation or synthesis.

Atlas's native ownership history and backups remain local. The specifically
authorized Scout history arrives only through the existing private
`CODEXSTORE/ducketz-nightly-exchange/v1` packet path. Account records, financial
packets, databases, and raw broker replies never belong in GitHub or shared-source
notices. Setup does not change schedules or start a Scout worker.

## Historical explicit adapter compatibility

The older `native-ownership-cutover-v1` explicit `--spec` entry point retains its
original dated validation and authority-document semantics for existing callers
and receipts. The normal manual launcher uses `native-ownership-manual-start-v1`
and the separate startup reconciliation APIs, so those historical administrative
requirements do not become new manual-start prerequisites. Original records are
retained rather than rewritten.
