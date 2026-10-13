# Scout 15m manual-start runbook

**Preparation checkpoint: commands below are source-verified, but this document
does not declare the installation ready.** The Atlas service/readiness route,
private authenticated binding, compatible account writers, final installed
manifest and successful prestart connection check must be verified before the
human is handed the start command. Preparation leaves Scout stopped. The human
performs the first real-order start; the scheduled supervisor never starts it.

The implementation preserves the
[local-submission/shared-admission contract](intraday-scout-submission-contract.md).
Scout owns its broker orders and private books. Atlas provides one shared actual
cash/share reservation authority for Gameplan, Atlas 15m and Scout 15m. No Atlas
database is expected on Scout.

## Acceptance before handing over the command

Record these facts separately in Scout's private completion receipt:

1. Published source commit(s), Completion-Record identities and verified remote
   SHAs, with review and passing isolated offline checks for the final bytes.
2. Exact installed files/dependencies, expected-before installation checks and
   the final manifest at
   `C:\dev\ducketz\scratch\cross-pc\evidence\scout-live-final-preparation\final-installed-manifest.json`.
3. Scout's private binding at
   `C:\DATASTORE\intraday\scout\authority-binding.json`, and authenticated
   capabilities/readiness from the actual Atlas service without commitments.
4. Compatible loaded Atlas account writers or verified absence, plus verified
   Scout-local Gameplan/account-writer absence. Runtime repeats these checks.
5. Passing final installed prestart check, no active Scout session and no known
   engineering blocker; native supervisor settings and its actual path bindings
   verified. A pending or false supervisor binding is not a completed handover.

A prestart result does not prove a future loaded Scout session, successful real
fill or fresh account/market conditions at launch. Runtime gates check those
conditions when needed. A missing manifest, missing binding, unavailable
readiness route or incompatible writer is a blocking prerequisite with an owner
and next action, not a reason to weaken the startup guard.

## Commands in the ducketz PyCharm PowerShell terminal

The local profile selects the verified environment
`C:\dev\ducketz\.venv\Scripts\python.exe`; an activated terminal environment
is not required. The source entry point is `ml.intraday.live_session`. Its
`--live` flag is the explicit manual real-order start. The non-ordering Scout
check uses the separate `ml.intraday.scout_prestart` entry point.

After preparation acceptance, run this prerequisite first:

```powershell
Set-Location -LiteralPath 'C:\dev\ducketz'
& 'C:\dev\ducketz\.venv\Scripts\python.exe' -B -m ml.intraday.scout_prestart --repository 'C:\dev\ducketz' --datastore 'C:\DATASTORE' --check-connection
if ($LASTEXITCODE -ne 0) { throw 'Scout prestart failed. Resolve the reported prerequisite before starting.' }
```

The result is JSON with `mode: "connection"`, `ready: true`,
`local_checks_passed: true`, `runtime_started: false`,
`orders_or_commitments_created: false` and an empty `errors` list. It exits zero.
Its checks include installation, dependencies, binding, session absence, local
account writers, original books and authenticated connection/readiness. Private
connection receipts are under
`C:\dev\ducketz\scratch\scout-15m-prestart\authority-receipts`.

Readiness uses the authenticated, noncommitting `/v1/readiness` view with a
fresh request nonce. Scout verifies the saved reply against its pinned server,
account and participant binding, exact protocol and current observation expiry.
Both Atlas and Gameplan must have compatible loaded-writer evidence or verified
absence. A capability reply, positive dictionary or source file on disk does
not establish that readiness. A readiness receipt never reserves capacity or
authorizes an order. The final submission gate validates the saved readiness
receipt again locally; it does not refresh remote evidence during the broker
effect boundary.

Without `--check-connection`, the check stays local: a successful result has
`local_checks_passed: true` but `ready: false` and connection `not_requested`.
That result does not establish service readiness. A failed prerequisite exits
2 and reports a sanitized `check`, `type` and `message`; do not bypass it by
editing controls, fabricating a manifest or substituting a successful callback.

Private enrollment uses the published
[Atlas production interface](intraday-production-interface.md) and the existing
private exchange. Keep its accepted v1 binding and offer/request/response files
unchanged. `ml.intraday.scout_enrollment` constructs a separate v2 binding using
the retained, independently pinned offer receipt, original enrollment dates and
identity, and the exact server leaf DER hash. It rechecks the local key and
certificate pair and never starts a connection or creates an order commitment.
The installed environment includes the reviewed `admission-tls` dependency.
Enrollment inputs and resulting bindings stay private; the manual trading
command does not enroll or replace credentials.

Once the prerequisite succeeds and the human chooses to start, paste:

```powershell
& 'C:\dev\ducketz\.venv\Scripts\python.exe' -u -B -m ml.intraday.live_session --repository 'C:\dev\ducketz' --datastore 'C:\DATASTORE' --profile 'C:\dev\ducketz\scratch\cross-pc\local-profile.json' --watchlist 'C:\dev\ducketz\datafetching\watchlist.local.txt' --live
```

Keep this terminal open. The launcher keeps the existing defaults of a
30-second publication offset, one symbol worker and the configured ten-model
ensemble; the command does not change sizing or strategy defaults. It uses the
private local watchlist and existing broker/provider credentials. An unavailable
service or invalid runtime proof fails explicitly; there is no fallback to
Atlas execution or shadow-only operation.

## Startup and permitted times

Startup first records `STARTING`. Successful Scout authority preflight emits an
`authority_ready` JSON event with authenticated participant/protocol evidence,
readiness expiry and each account writer's compatibility state and proof hash.
After histories/provider and workers are prepared it records `RUNNING` and
prints:

```text
Scout 15m manual session running. New predictions can submit real orders. Ctrl-C stops this worker; resting orders retain their broker identities.
Session status: C:\DATASTORE\intraday\manual-session.json
Session log: C:\DATASTORE\intraday\sessions\manual-scout-15m-<session-id>\events.jsonl
```

The real session identity is `manual-scout-15m:<id>`; the directory replaces the
colon with a hyphen. Status includes that identity, `pc: "Scout"`, PID, process
creation time, heartbeat and actual evidence paths. A startup failure records
`FAILED` with the reason; it does not establish a running session. `RUNNING`
means workers exist, not that a full forecast, accepted order or fill exists.

All clock descriptions here use America/Los_Angeles. On a standard exchange
day, eligible forecast boundaries are 04:15 through 16:30 at 15-minute intervals.
With the default publication offset, each boundary is considered 30 seconds
later. For a boundary `B`, the fixed reference opens at `B-15 minutes` and its
target is `[B+15 minutes, B+30 minutes)`. Computation and provider waits can delay
publication; they never move that target or invent a missed-quarter order.

New broker submissions must occur strictly before **16:45 Pacific**, before the
forecast's target closes and within the broker's actual executable session.
Standard broker windows are 04:00–06:25, 06:30–13:00 and 13:05–17:00 Pacific;
the 16:45 cutoff shortens the last window for new 15m submissions. Exchange
holidays close the session, and early-close days use the official regular
session with complete reference/target intervals. Fresh full-ensemble, account,
quote, capacity, ownership and previous-order gates still apply inside a window.

Starting outside an eligible forecasting window does not bypass it. The worker
can remain running, recording closed/unspecified-edge events and waiting for an
eligible completed boundary. Scoring and exact order recovery run separately;
reconciliation may continue after cutoff. Starting near the last boundary may
produce a late forecast that is preserved and scored but cannot submit. There
is no requirement to demonstrate a real fill before handing over this command.

## Graceful stop and a later manual reload

Press **Ctrl+C once in the terminal running the launcher**. It withdraws the
manual session's order authority, records `STOPPING` and prints:

```text
15m order authority stopped. Waiting for active computation to finish; pending broker identities are retained.
```

Wait for active computation to finish and the command prompt to return. The
final status is `STOPPED`, or `FAILED` if a worker failed. An already in-flight
broker effect may require later reconciliation; stopping is not proof of
non-submission. The stop does **not** automatically cancel resting orders,
liquidate shares or release shared commitments. Their exact identities remain
in original private journals for recovery. The stopped worker no longer polls
them. Do not delete a journal or create replacement empty books to clear an
unresolved order.

A reviewed repair reload is human-controlled: verify the old worker has
finished, install reviewed changes with expected-before checks, rerun prestart,
then manually run the same launcher. The new session preserves and reconciles
original sessions. The supervisor follows planned repair reloads and does not
interpret a planned stop as completed first-session testing.

## Status, forecasts, fills and private accounting

Read the latest status from another PowerShell terminal:

```powershell
Get-Content -LiteralPath 'C:\DATASTORE\intraday\manual-session.json' -Raw
```

Before the first start the file can legitimately be absent. During a session,
use its actual `evidence_paths.log` to follow saved JSON events:

```powershell
$scoutStatus = Get-Content -LiteralPath 'C:\DATASTORE\intraday\manual-session.json' -Raw | ConvertFrom-Json
Get-Content -LiteralPath $scoutStatus.evidence_paths.log -Tail 40 -Wait
```

Ctrl+C in this **second** terminal stops log following only. It does not stop the
trader in the first terminal.

| Evidence | Actual Scout location and meaning |
|---|---|
| Current session | `C:\DATASTORE\intraday\manual-session.json`; exact identity, heartbeat, state and evidence paths. |
| Preserved session status/log | `C:\DATASTORE\intraday\sessions\manual-scout-15m-<id>\status.json` and `events.jsonl`; startup, `forecast_published` and `execution_disposition` summaries. |
| Forecasts and measured outcomes | `C:\DATASTORE\intraday\shadow.sqlite3`; `cycles`, `forecasts`, `outcomes`, immutable snapshots/inputs and `events`. The historical filename also holds `mode=live`, `pc=Scout` records. |
| Desired sizing and execution decisions | The same database's `intents`, `intraday_live_queue` and `events`; compare desired, adjusted and submitted quantities without calling them fills. |
| Data acquisition and waits | `C:\DATASTORE\intraday\acquisition.sqlite3`; exact interval attempts and durable provider-recovery evidence. |
| Original private orders/fills | `C:\DATASTORE\intraday\scout\inventory.json` selects original journals in `journals`; `records`, lifecycle evidence, exact broker IDs, cumulative fills and cancellation attempts. Never guess a journal filename. |
| Shared-authority evidence | `C:\DATASTORE\intraday\scout\receipts`; saved authenticated request/status/evidence replies. Atlas's authority database remains on Atlas. |
| Detailed private accounting | `C:\DATASTORE\intraday\scout\accounting`; current indexes of saved authority observations, matched to original requests, grants, fills and allocation provenance. Older observations remain in immutable pages under `accounting-history`. Each index retains the latest valid snapshot and explicit pending status when a refresh fails. |
| Model artifacts | `C:\DATASTORE\intraday\models`; original local fitted artifacts and manifests. |
| Errors | Terminal/startup status plus forecast store `events` such as `execution_retry`, `order_recovery_retry`, worker failures and acquisition/model events. The JSONL summary does not contain every detailed event. |

Acceptance proves a submission, not a fill. Actual filled quantities require
exact broker cumulative-fill evidence. `probability_not_down` is a forecast
probability, not measured historical accuracy. Scored, synthetic and pending
outcomes remain distinct. A cancellation acknowledgment is not terminal order
evidence, and raw local audit-event counts are not a wire backlog.

Accounting refreshes are separate from immediate order recovery. Failed or
stale detail observations retain the last valid snapshot and cannot release
capacity, repeat a submission or block exact cancellation recovery. Gross fill
cost/proceeds are supported by exact fills; unavailable fees, cost basis and
realized profit remain unknown. A terminal order does not mean that all later
accounting provenance has arrived.

For independent inspection, use saved files and SQLite URI `mode=ro` with
`PRAGMA query_only=ON`. Do not instantiate production stores/clients, copy whole
databases or use the obsolete `observe-read-only.py` helper. Financial/order
details, credentials and complete private books stay off public GitHub.

## First-session observation supervisor

The native task is named **Scout 15m first live session watch**. Required saved
settings are every 15 minutes from 04:00 through 17:00 Pacific, a new chat per run, local ducketz project,
GPT-6 Astra, Ultra reasoning, notifications for all runs and automatic archival
of successful runs off. Its private definition/verification receipts and
continuity live under
`C:\dev\ducketz\scratch\scout-15m-first-live-watch`. The private binding records
this preparation chat's actual identity and discovers each real session's
evidence paths. No Atlas chat/session/native task identity is copied.

Each run performs one bounded read-only observation, preserves cursors and
original sessions, and finishes. It checks ten-model participation, immutable
targets, latency/provider/lock waits, scoring states, Gameplan coexistence and
exact journal-to-authority lifecycle evidence. It reports meaningful failures,
human actions, recoveries and new first-cycle/scoring milestones once. Ordinary
in-flight `SUBMITTING`/local `SENDING` is not automatically an unknown failure.
Unchanged waiting for a human-started session is quiet.

The supervisor has no trading or worker-control authority. It cannot issue or
cancel orders, start training, change controls, or start/stop/restart a worker.
It can reproduce defects and prepare reviewed fixes in isolated owned source;
an active worker's source is never edited. It follows actual Pacific session
dates and planned repair reloads without an inherited expiration date. The
human's explicit end of first-session testing, or a later agreed completion
condition, pauses observation and produces one final outcome/unresolved-items
report without stopping traders. Runtime safeguards do not depend on the next
15-minute observation.
