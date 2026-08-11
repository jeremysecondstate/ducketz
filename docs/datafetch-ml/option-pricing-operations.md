# Option Pricing shadow operations

This runbook preserves eligibility protocol v2 as the readable legacy
three-symbol/OPRA workflow and adds the separate Loop-native causal BSGP policy
v3 for all ten watchlist symbols and both CALL/PUT routes. Paid OPRA is optional
benchmark data under v3, never a prerequisite. `PRODUCTION_ELIGIBLE` authorizes
only a separately approved canary; it never authorizes an order or automated
action. Every Pricing artifact and report must retain
`automated_action_allowed=false` until a distinct activation workflow is
approved outside this runbook.

The three conclusions are intentionally independent. `CAPTURE_READY` means the
deployed Loop-native sidecar is publishing causally and fail-closed.
`RESEARCH_GATE_ELIGIBLE` requires enough independent, session-blocked evidence
for evaluation. `PRODUCTION_AUTHORIZED` additionally requires every gate,
candidate, lockbox, operational, Strategy, and explicit operator authorization
stage. Installing code or migrating policy can establish none of the latter two
by itself.

## Installation contract

Use a clean CPython 3.13 environment. The exact production package
versions are in `requirements-ml-runtime.lock`; `pyproject.toml` declares every
direct package imported by the Pricing runtime, including Databento.

```powershell
py -3.13 -m venv .venv-pricing
.\.venv-pricing\Scripts\python.exe -m pip install pip==26.2.1
.\.venv-pricing\Scripts\python.exe -m pip install -r requirements-ml-runtime.lock
.\.venv-pricing\Scripts\python.exe -m pip install -e . --no-deps
.\.venv-pricing\Scripts\python.exe -m pip check
.\.venv-pricing\Scripts\ducketz-option-pricing --help
.\.venv-pricing\Scripts\ducketz-option-pricing-opra --help
.\.venv-pricing\Scripts\ducketz-option-pricing-fred --help
.\.venv-pricing\Scripts\ducketz-option-pricing-loop-native --help
.\.venv-pricing\Scripts\ducketz-option-pricing-admin --help
.\.venv-pricing\Scripts\ducketz-option-pricing-lockbox --help
```

The operational preflight compares installed production imports to the exact
lock, runs `pip check`, smoke-tests all six CLIs with bounded subprocess
timeouts, verifies capacity and receipt chains, and checks the last published
runtime benchmark:

```powershell
ducketz-option-pricing-admin --datastore C:\path\to\approved-datastore operational-preflight
```

Running this against `C:\DATASTORE` is a write and requires explicit approval.

Before the next regular session, the operator view separates current-session
inputs from historical eligibility blockers:

```powershell
ducketz-option-pricing-admin --datastore C:\DATASTORE capture-current-rate
ducketz-option-pricing-admin --datastore C:\DATASTORE readiness
```

`capture-current-rate` copies the latest normalized FEDFUNDS receipt into the
causal rate lane with its real local fetch time. It enables only later live
targets and never claims historical coverage. Loop A performs the same bridge
after every successful FEDFUNDS fetch. `readiness` is read-only and exits 6
until all evidence gates pass.

## Configuration and secrets

The continuous live Pricing scope comes from `datafetching/watchlist.txt` by
default, with CALL and PUT priced independently for every active symbol. Legacy
v2 eligibility/OPRA evidence remains exactly `NVDA GOOG MU`. Loop-native v3
scope is independently frozen to
`NVDA GOOG MU AAPL MSFT AMZN META TSLA CAT SNDK`, with all twenty symbol/side
routes retained even when missing. Expanding either scope does not expand paid
evidence or lockbox authority. The continuous Pricing runtime reads already
committed local inputs and does not need a provider credential. The guarded
OPRA estimator/importer reads
`DATABENTO_API_KEY` from the environment and never renders it. Missing secrets
are terminal for estimates/imports. Databento metadata calls have three bounded
attempts and a 30-second endpoint timeout. Paid `get_range` has a 180-second
endpoint timeout and one attempt so retry cost can never exceed the approved
request list.

The historical macro importer separately requires `FRED_API_KEY` in the process
environment or repository `.env`. The key is used only in HTTPS requests to the
official API, is never included in console output, manifests, receipts, raw
provider-response files, or normalized Parquet, and is redacted from provider
failures. Do not pass it as a command-line argument.

## Historical FRED/ALFRED vintage import

Historical rate evidence must come from an explicit FRED API real-time-period
request, not the current-revised CSV history. All four bounds are mandatory so
the operator can review the exact acquisition scope. For example:

```powershell
ducketz-option-pricing-fred --datastore C:\DATASTORE `
  --series FEDFUNDS `
  --realtime-start 2026-01-01 `
  --realtime-end 2026-08-10 `
  --observation-start 2025-01-01 `
  --observation-end 2026-08-10
```

The importer uses FRED API v1 `fred/series/observations` with `output_type=1`
and explicit `realtime_start`/`realtime_end`; each finite observation must carry
its own `realtime_start` and `realtime_end`. It also receipts the official
series metadata and `fred/series/vintagedates` response. The latter defines the
dates on which new or revised series data were released. The FRED real-time
period represents when information was known until it changed. See the official
[real-time-period documentation](https://fred.stlouisfed.org/docs/api/fred/realtime_period.html),
[observation endpoint](https://fred.stlouisfed.org/docs/api/fred/series_observations.html),
and [vintage-date endpoint](https://fred.stlouisfed.org/docs/api/fred/series_vintagedates.html).

FRED/ALFRED provides a provider date, not a guaranteed intraday availability
timestamp. Policy
`ALFRED_REALTIME_START_DATE_END_OF_DAY_AMERICA_CHICAGO_V1` therefore makes a
vintage usable only at the next America/Chicago midnight. `release_at` and
`available_at` record that conservative provider clock; `fetched_at` records the
actual local acquisition clock. Neither overwrites the other. Re-fetching an
existing provider vintage preserves its first local receipt, and conflicting
content fails closed.

Each acquisition commits checksummed provider responses, normalized Parquet,
manifest, and receipt under
`ml/option-pricing-evidence/fred-alfred-vintages/<UTC>/` before materializing
append-only macro-vintage and rate-feature partitions. The receipt says
`historical_coverage_status=NOT_EVALUATED`: successful import proves only its
exact rows and clocks. Rerun the OPRA estimator's rate-coverage preflight before
claiming that all scheduled targets are covered. A current observation, a
current `last_updated` timestamp, or today's revised history never establishes
historical availability.

The default OPRA command only estimates. It prints every exact request boundary,
raw eligible contract set, estimated cost, billable byte estimate, aggregate
total, and storage-capacity result. Production scope is exactly 504 chronological
clusters per symbol over at least six calendar months at 10:00, 11:30, 13:30,
and 15:00 New York time. Before approval, rerun the estimate with the selected
ceiling and create the immutable pending record:

```powershell
ducketz-option-pricing-opra --datastore C:\DATASTORE --definitions-only `
  --max-cost-usd <exact-ceiling> `
  --write-authorization-template C:\path\to\opra-definition-authorization.json
```

The operator must independently review every request in that JSON, then record
the approval id, identity, and timestamp and explicitly authorize both external
cost and the datastore write. `--execute` requires that exact record as
`--authorization-record`. Any request, estimate, policy hash, storage contract,
or ceiling change invalidates it before `get_range`. The authorization is copied
and checksummed into the immutable import receipt.

## Phase ordering and startup

The approved scheduler order for each natural target is:

1. wait for the exact completed underlying bar;
2. verify a strictly earlier Loop-native model generation, run Pricing, and
   atomically commit the unchanged Black-Scholes rows plus the optional shadow
   sidecar;
3. only then fetch/publish the independent Options receipt;
4. on a later Pricing cycle, reconcile the earliest committed prediction to the
   first exact later quote; and
5. after the fast publication, allow the bounded local worker to materialize
   only already matured outcomes and publish a new generation that is eligible
   no earlier than a future target.

The helper is normally launched by the exact-ten-symbol Pricing runtime after
its fast publication. It uses a separate interprocess lock and a minimum refresh
interval so concurrent or repeated cycles cannot multiply training work. It can
also be exercised explicitly for diagnosis:

```powershell
ducketz-option-pricing-loop-native --datastore C:\DATASTORE `
  --trainer-cutoff 2026-08-11T08:00:00Z --dry-run
```

`--dry-run` performs no datastore write. Neither mode has a provider client or
credential path. Training failure, insufficient sessions, stale support, or an
invalid model leaves the next target on Black-Scholes with an explicit fallback
status; it does not delay Options.

Start only one writer. The CLI owns
`.ducketz-option-pricing-runtime.lock`; another writer fails immediately. The
normal command is:

```powershell
ducketz-option-pricing --datastore C:\path\to\approved-datastore `
  --watchlist datafetching\watchlist.txt
```

Before installing, restarting, or modifying a production supervisor, obtain the
separate operational-write/supervisor approval. The runtime never starts Options,
Strategy, Schwab, a supervisor, or an order consumer.

## Shutdown and restart

Use the supervisor's ordinary graceful stop or `Ctrl+C` for an interactive
process. A clean stop exits zero. Each cycle is idempotent: the earliest
receipt-proven prediction for a natural symbol/target/contract key is retained.
Restarting cannot backdate a prediction or replace its first availability.

Exit codes are actionable:

| Code | Meaning |
|---:|---|
| 0 | Cycle published with no health alerts. |
| 1 | Unclassified runtime failure. |
| 2 | Invalid configuration. |
| 3 | Dependency contract failure. |
| 4 | Disk/resource capacity failure. |
| 5 | Publication/receipt failure. |
| 6 | Evidence or health alert requires operator action (`DEGRADED` or `FAIL`). |

## Crash recovery and interrupted writes

Runs are written to hidden staging directories. A crash before the atomic rename
leaves an unreachable staging directory; a crash after rename but before receipt
leaves an orphan run. Neither can become authoritative through discovery. A
restart verifies the current pointer and complete receipt chain before preserving
prior predictions. Do not manually promote or delete an orphan.

The health record at `ml/option-pricing-health/latest.json` reports missed phases,
stale pointers, broken chains/lineage, required-route loss, drift, interval or
constraint failure, evidence-count stagnation, latency, memory, and disk alerts.
Treat every terminal alert as fail-closed.

## Capacity and retention

The runtime enforces hard row, fit-row, elapsed-time, peak-memory, and free-disk
limits before publication. Immutable evidence, policy, candidate, lockbox, and
eligibility receipts are retained forever unless a separately authorized archive
procedure is approved. The runtime never auto-deletes evidence. A disk-capacity
failure occurs before a Pricing authority update.

## Candidate and lockbox

Freeze only after gates 1–8 pass on real untouched offline assessment evidence:

```powershell
ducketz-option-pricing-admin --datastore C:\path\to\approved-datastore freeze-candidate
```

The candidate identity checksums the model copies, source inputs, dependency
contracts, code, policy, and closed-lockbox request inventory. Later source,
code, dependency, model, or policy changes fail verification; fitting,
calibration, selection, and hyperparameter changes are prohibited under that
identity.

Do not invoke the lockbox CLI until all ten non-lockbox gates and operational
readiness pass and the operator has supplied a record with action
`OPEN_AND_SCORE_OPTION_PRICING_LOCKBOX_ONCE`, the exact candidate and policy
hashes, one maximum attempt, operator identity, and approval time. The attempt
receipt is written before any target DBN is decoded. Failure, interruption, or a
second attempt permanently invalidates the candidate. A changed candidate or
policy requires a genuinely fresh future lockbox.

## Pointer rollback

Rollback restores the immediately previous fully verified Pricing pointer and
does not delete either run. Inspect both records first. Then obtain an explicit
record with action `RESTORE_PREVIOUS_VERIFIED_OPTION_PRICING_POINTER`, exact
current and target run paths, operator identity, approval ID, and time:

```powershell
ducketz-option-pricing-admin --datastore C:\path\to\approved-datastore rollback-pointer `
  --authorization-record C:\path\to\rollback-authorization.json
```

The command verifies the target in the current receipt chain, writes the pointer
atomically, re-verifies it, restores the original pointer if verification fails,
and publishes a rollback receipt. Evidence is never removed.

## Disaster recovery

Restore the datastore to a new volume without changing relative paths. Verify
filesystem checksums, then run the read-only status command followed by the
operational preflight. Never reconstruct receipts, rewrite timestamps, or point
at a run that was not already receipt-reachable. If any immutable source is
missing or changed, eligibility remains `NOT_PRODUCTION_ELIGIBLE`; rebuild a new
candidate from new real evidence rather than repairing historical receipts.

## v1 compatibility and promotion

Existing v1 run manifests, Parquet schemas, embedded shadow gate reports, and
consumer paths remain readable. V2 adds checksums to new manifest input records
and publishes policy, Strategy, operational, lockbox, candidate, health, and
eligibility artifacts under separate roots. V1 artifacts are readable but cannot
alone satisfy v2 lineage or production eligibility.

`ducketz-option-pricing-admin status` verifies current artifacts without changing
rankings, UI ordering, candidate scores, order construction, or Schwab payloads.
No command in this workflow submits an order or activates a production consumer.
