# Read-only Loops system monitoring

`ml.system_monitor` provides two deterministic monitoring layers for the seven
production loop owners. Both modes read existing processes, logs, locks,
publications, Parquet outputs, and UI adapters. They do not restart a process,
move an authority pointer, fetch a provider, place an order, or write a report
into the datastore.

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
.\.venv\Scripts\python.exe -m ml.system_monitor --datastore-target pc --mode scheduled
```

It selects daily mode during the weekday 2 PM local hour and hourly mode at
every other wake.

Use `--compact` when another tool will parse the JSON. An `UNHEALTHY` report
returns a nonzero process status; `HEALTHY` and evidence-quality `DEGRADED`
reports return normally so the caller can render their structured findings.

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

The scheduled prompts must remain read-only. A warning or failure is reported
with evidence for operator review; it never authorizes a restart, recovery,
promotion, pointer change, or order.

## Status meaning

- `HEALTHY`: no warning or failure; informational market/evidence states may be
  present.
- `DEGRADED`: operations remain readable, but at least one production-quality
  or recoverable operational warning needs attention.
- `UNHEALTHY`: at least one required runtime, authority, integrity, freshness,
  or UI contract could not be verified.

Every report explicitly publishes `read_only=true`, `orders_placed=0`, and
`automated_action_allowed=false`.
