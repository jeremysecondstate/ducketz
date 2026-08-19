# Loops system monitoring and guarded recovery

`ml.system_monitor` provides two deterministic monitoring layers for the seven
production loop owners. Both modes read existing processes, logs, locks,
publications, Parquet outputs, and UI adapters. They do not restart a process,
move an authority pointer, fetch a provider, place an order, or write a report
into the datastore.

`ml.system_guardian` wraps that unchanged read-only report with a narrow
liveness-recovery policy. Mutation requires the explicit `--repair-liveness`
flag. The Scheduled task uses that flag, but the guardian still permits at most
one allowlisted runtime restart per run.

## Commands

Hourly operations:

```powershell
.\.venv\Scripts\python.exe -m ml.system_monitor --datastore-target pc --mode hourly
```

Daily production/output quality:

```powershell
.\.venv\Scripts\python.exe -m ml.system_monitor --datastore-target pc --mode daily
```

The Codex heartbeat uses the deterministic combined selector:

```powershell
.\.venv\Scripts\python.exe -m ml.system_guardian --datastore-target pc --mode scheduled --repair-liveness
```

It selects daily mode during the weekday 2 PM local hour and hourly mode at
every other wake.

Use `--compact` when another tool will parse the JSON. An `UNHEALTHY` report
returns a nonzero process status; `HEALTHY` and evidence-quality `DEGRADED`
reports return normally so the caller can render their structured findings.

Omit `--repair-liveness` for a guardian dry run. A dry run reports an eligible
repair without stopping a process, deleting a stale lock, creating restart
logs, or writing an audit observation.

## Hourly operations contract

The hourly mode verifies:

- exactly one launcher/worker pair for each of CME, ALFRED, Loop A, Active
  Pricing, Directional Loop B, Options Capture, and Strategy;
- the production command arguments and each singleton lock's live worker PID;
- active stdout freshness and recent stderr, with market-closed idle allowances
  only for the calendar-gated Pricing and Options owners;
- a fresh zero-failure Loop A complete-cycle authority, including a race-safe
  check when the next cycle is currently `WRITING`;
- Loop A exact-target readiness, CME L2, ALFRED, and all-symbol option snapshot
  pointer/receipt/checksum contracts;
- fresh, immutable Loop B and Strategy publications, Pricing target/full
  authorities when applicable, and Strategy-to-Loop-B lineage;
- the rolling-forecast and Options Strategy UI adapters against their actual
  current-authority paths, including the rule that heuristic Scenario Coverage
  cannot enable manual order submission; and
- datastore free capacity and aggregate log size.

The XNYS calendar owns target expectations. Closed-market retention and a new
quarter-hour still inside its bounded settle window are informational, not false
failures. Once the settle window has elapsed during a regular session, missing
or lagging target evidence is a failure.

## Daily production/output contract

Daily mode includes every hourly check and additionally verifies:

- complete ALFRED vintage lineage, coverage, and lookahead guards;
- directional evaluation metrics for every production horizon, preserving each
  published reference warning for calibration, discrimination, Brier score,
  log loss, and accuracy;
- mature prospective live-label counts separately from offline evaluation, so
  `INSUFFICIENT_LIVE_LABELS` is never described as bad live performance;
- Strategy candidate score basis, full-pricing and quality coverage, model
  status, and observed option-outcome maturity, so non-probabilistic Scenario
  Coverage is never described as a calibrated probability; and
- the bounded, read-only exact-target Pricing-to-Strategy canary.

## Scheduled cadence

The active Codex heartbeat supplies both layers in local Pacific time:

- it wakes at 42 minutes past every hour, leaving the `+1`, `+5`,
  `+6`, and `+10` loop phases time to settle; and
- on the 2:42 PM weekday wake it selects production/output quality mode, after
  the regular XNYS session and downstream publications have settled.

The Scheduled task delegates all mutation decisions to `ml.system_guardian`.
The task prompt may explain the resulting JSON, but it must not improvise shell
repairs, process selection, code edits, data recovery, promotion, pointer
changes, backfills, provider maintenance, or orders.

## Guarded liveness-recovery contract

The guardian repairs only one unambiguous runtime fault per run:

- an owner is completely absent and its exact singleton lock is missing or
  records a PID proven dead;
- one process from an otherwise allowlisted launcher/worker command remains,
  and that exact residual tree can be stopped safely; or
- one healthy-looking pair is confirmed hung across two scheduled runs at
  least 30 minutes apart. The process IDs, creation clocks, stdout path, stdout
  size and modification clock, and a narrowly classified stale-publication
  failure must all remain unchanged.

Before restarting, the guardian rechecks the exact lock and PID state. It then
stops only the selected process tree, removes only that runtime's unchanged
lock after its PID is proven dead, and launches its closed-allowlist command in
a hidden window with fresh stdout/stderr files. A restart is successful only
when a new launcher/worker pair and matching worker lock verify. Publications
may remain temporarily unhealthy until the next normal cycle, and that state
is preserved in the post-restart monitor report.

The Options recovery command retains `--skip-historical-catchup`; an unattended
liveness restart resumes prospective capture without initiating historical
provider maintenance.

Automatic recovery is blocked when there are multiple ownership failures,
duplicates, a non-allowlisted command, an unreadable lock, a live foreign lock,
an integrity/authority verification failure, or a recent credential,
entitlement, rate-limit, or capacity error. Data quality, calibration, UI,
lineage, provider, and model findings remain report-only. Code edits, authority
pointer changes, historical backfills, model promotion, and orders are never
allowed.

Each attempted restart has a two-hour per-runtime cooldown and writes an
immutable receipt under:

```text
C:\DATASTORE\logs\ducketz\system-guardian\audit
```

Possible hangs write observation receipts there as well. Restart stdout and
stderr are written below the date-partitioned `system-guardian` log directory.

## Status meaning

- `HEALTHY`: no warning or failure; informational market/evidence states may be
  present.
- `DEGRADED`: operations remain readable, but at least one production-quality
  or recoverable operational warning needs attention.
- `UNHEALTHY`: at least one required runtime, authority, integrity, freshness,
  or UI contract could not be verified.

Every underlying monitor report explicitly publishes `read_only=true`,
`orders_placed=0`, and `automated_action_allowed=false`. The guardian's outer
report separately records whether the narrow liveness flag was enabled, every
process/lock action taken, before/after PIDs, verification state, and an
unconditional `orders_placed=0`.
