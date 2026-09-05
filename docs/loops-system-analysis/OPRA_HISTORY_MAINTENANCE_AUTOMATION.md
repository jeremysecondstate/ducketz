# OPRA strategy-history maintenance contract

This checked-in file is the durable production OPRA maintenance contract. The
active owner is Loop A's bounded stage inside `ml.overnight_runtime`. The former
`loops-opra-history-maintenance` standalone Scheduled task is paused to prevent
duplicate ownership. Read this file before manually invoking that fallback.

## Purpose and ownership boundary

The lane incrementally maintains the six production parents and the three
schemas required by options-strategy modeling: `ohlcv-1h`, `cbbo-1m`, and
`definition`. Exact `cbbo-1m` snapshots supply historical candidate entry/exit
BBO economics; hourly bars supply surface context/cross-checks; definitions
preserve point-in-time contract identity. It is not an independent supervisor
and is not the prospective live Options snapshot owner.

It must not start, stop, restart, or repair a recurring loop; delete data;
touch a broker or order path; fetch research-only OPRA schemas; publish a live
option snapshot; merge OPRA with Schwab evidence; or alter Loop A equity bars.
If the legacy Options runtime is explicitly launched, it uses
`--skip-historical-catchup`, so it cannot race this owner.

## Exactly one maintenance attempt

Operate from `C:\dev\ducketz`. First verify that no OPRA history synchronization
is already active by observing the canonical sync lock and its PID through the
normal runtime-lock reader semantics. Do not delete or edit a lock. If the lock
is live, unverifiable, or changes during inspection, report `DEFERRED_LOCKED`
and perform no provider request.

The overnight owner runs the equivalent command once per UTC date after the
17:00 PT stock close (00:00 UTC during daylight time). If the nightly owner is
intentionally offline and the fallback is explicitly invoked, run this command
exactly once:

```powershell
.\.venv\Scripts\python.exe -m datafetching.options_history `
  --datastore-target pc `
  --schemas ohlcv-1h cbbo-1m definition `
  --incremental-only `
  --max-estimated-download-bytes 20000000000 `
  --max-estimated-cost-usd 1 `
  --max-incremental-catchup-days 30
```

Do not retry it in the same wake. The command performs provider size and cost
preflight for every requested scope before downloads, publishes those
preflights, selects a fair oldest-cursor-first subset within the aggregate
budget, advances each selected cursor by at most thirty calendar days, verifies or
atomically publishes partitions, and refreshes OPRA health once. Missing or
invalid cursors remain `bootstrap required`; this maintenance path must never
expand them into a large initial fetch.

After the command succeeds, Loop A automatically runs the equivalent audit-only
catalog refresh. When manually invoking the paused fallback, run it once so the
operator catalog reflects new coverage:

```powershell
.\.venv\Scripts\python.exe -m datafetching.datastore_hygiene `
  --datastore-target pc
```

This second command must not include a cleanup or confirmation flag.

## Interpretation and notification

Parse the history command's exit code and final counters. `deferred_scopes>0`
with completed or verified work is normal budget backpressure and should be
continued on the next daily run. Weekend/holiday `NO_DATA` is acceptable
only when the synchronizer verifies the provider-native identity and advances a
cursor through the exact empty interval. A nonzero `failed_scopes` or
`capacity_blocked_scopes`, a failed preflight, a missing API key, invalid
receipt/checksum, stale or malformed cursor, audit failure, or uncaught error is
an incident.

Overnight “current” means coverage of the most recently completed market
session. Closed-market wall-clock age does not make that final session quote
stale. For the September daylight-time session, standard listed-options quotes
end at 13:00 PT (`20:00Z`) even though this system's stock action clock continues
through 17:00 PT. Order-time quote freshness is a separate future execution
gate.

Stay quiet for an unchanged or normally advancing run. Notify only on an
incident, required operator action, or when every `ohlcv-1h`, `cbbo-1m`, and `definition`
cursor reaches the provider's current entitled boundary. A completion notice
must cite `health/current.json` and the refreshed market-data catalog; it must
not claim that research-only schemas or prospective live OPRA capture are
current.

Always report zero broker/order actions. Never include credentials or provider
secrets in output.

## Corrective catch-up receipt

**Observed 2026-09-04 UTC:** a manual execution of the exact production scope
preflighted and completed all 18 symbol/schema pairs. It selected an estimated
11,464,500,352 bytes at USD 0 and reported zero failed, deferred,
capacity-blocked, or bootstrap-required scopes. All six symbols' `ohlcv-1h`,
`cbbo-1m`, and `definition` cursors now have `completed_through=2026-09-04`;
`health/current.json` reports latest events on September 3. The stack and the
former standalone schedule remained paused during this repair, and no broker or
order action occurred.
