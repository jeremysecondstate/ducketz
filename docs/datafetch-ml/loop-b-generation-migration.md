# Loop B generation migration and recovery

Loop B can read either the legacy mutable Loop A layout or a versioned
`duckets-data-generation/v1` snapshot. Generation input is resolved once per
cycle from `<loop-a-root>/<namespace>/current-generation.json`; all referenced
objects are treated as read-only. Loop B artifacts, models, runs, predictions,
and its authoritative pointer are written only below a separate, disjoint
`--loop-b-output-root`; neither root may contain the other.

This is migration machinery, not cutover approval. The legacy reader remains
available, the production Loop A process is unchanged, and no command here
publishes a Loop A generation.

## Reader contract

Resolution eagerly verifies pointer/manifest structure, generation identity,
dataset identity uniqueness, contract registration, and safe object containment.
When a route selects paths for materialization, that route then verifies:

- pointer and manifest schema, generation, status, timestamp, and path;
- unique canonical dataset identities and their registered consumer contract;
- object containment below the selected namespace's immutable object tree;
- exact path, SHA-256 digest, byte size, Parquet readability, and row count;
- required columns and provider schema versions for known datasets;
- event bounds and daily/monthly partition membership; and
- symbol, provider, scope, request key, and timeframe selection by manifest
  identity plus the exact
  `stocks/<symbol>/bars/<timeframe>/<provider>/<scope>/<request_key>` logical
  prefix rather than reconstructed mutable paths.

Unknown unrelated datasets remain forward-compatible, but a requested dataset
must match a registered Loop B contract. Selected symbol/pool identities must
also agree with both their immutable ownership path and any explicit Parquet
identity column. A present but malformed generation pointer never falls back to
legacy input. A selected corrupt route fails explicitly while unrelated routes
can continue.

## Shadow command

Use a shadow Loop A namespace and a distinct stable Loop B output root:

```powershell
python -m ml.prediction_runtime `
  --loop-a-root C:\data\duckets-data `
  --loop-a-format generation `
  --loop-a-namespace loop-a-shadow `
  --loop-b-output-root C:\data\duckets-loop-b-shadow `
  --symbols NVDA GOOG MU `
  --provider databento `
  --feature-profile loop-a-generation-v2 `
  --horizons 1h 4h 1d 1w `
  --once
```

`--loop-b-output-root` is mandatory with `--loop-a-root`. A generation run is
rejected if the output root is within the Loop A tree. Legacy operation remains
compatible with `--datastore`; explicitly supplying an output root also allows
legacy input and Loop B output to be split during migration.

Each run manifest records the Loop A format, generation, availability time,
pointer and manifest digests, and dataset count. It does not record credentials.

## Machine-readable migration evidence

Compare the two verified current Loop B publications without changing either
root:

```powershell
python -m ml.migration_report equivalence `
  --legacy-output-root C:\data\duckets-loop-b-legacy `
  --shadow-output-root C:\data\duckets-loop-b-shadow `
  --expected-symbol-count 20 `
  --output C:\reports\loop-b-equivalence.json
```

The command validates each authoritative pointer, immutable manifest, and
publication receipt. It returns `NOT_COMPARABLE_CONTRACT_MISMATCH` before value
comparison when the feature profile, horizon specification, model family, or
cost contract differs. That is the expected result when comparing the current
producer-v2 profile with `loop-a-all-v1`; it is not an equivalence failure that
may be waived with a numeric tolerance. Exit status is zero only for a complete
20-symbol `EQUIVALENT` report.

Collect resumable 20-symbol timing evidence by rescanning verified immutable
runs and receipts:

```powershell
python -m ml.migration_report timing `
  --loop-b-output-root C:\data\duckets-loop-b-shadow `
  --namespace loop-a-shadow `
  --expected-symbol-count 20 `
  --minimum-samples 60 `
  --interval-minutes 15 `
  --phase-offset-minutes 5 `
  --evidence-class live-shadow `
  --producer-evidence-report C:\reports\loop-a-live-shadow.json `
  --output C:\reports\loop-b-timing.json
```

The collector is idempotent: an interrupted invocation can be rerun and
deduplicates naturally by immutable run directory. It reports minimum, p50,
p95, and maximum receipt latency; completion before the next scheduled
boundary; route-error counts; rejected or invalid runs; and input generation
lineage. It remains `INCOMPLETE` below the sample threshold. `fixture` and
`unclassified` collections are always `NON_PRODUCTION_EVIDENCE`, even when
their timing checks pass. Report output is refused inside either read-only
source root. A live-shadow report additionally requires at least 60 samples even
if a lower CLI value is supplied, system-UTC runtime provenance, exact `+05`
phase alignment, fresh generation input at run start, a complete configured
symbol/horizon grid with no route errors, one feature profile, and generation
binding to the corresponding eligible Loop A live-shadow sample. Only runs
reachable through the authoritative receipt chain count. Duplicate schedule
slots or Loop A generations, orphan receipts, inconsistent timestamps, and
reports missing or extending the exact 11-gate producer schema are rejected;
the accepted producer report digest is recorded.

## Producer-v2 contract boundary

The calculated datasets currently emitted by `duckets-data` are not equivalent
to Loop B's default `loop-a-all-v1` contract. They use `bar_timestamp` and
`available_at`, calculation version `2.0.0`, schema
`duckets-data-technical-features/v2`, and the lifecycle schema
`technical-lifecycle-v2`; their available feature columns are also a strict
subset of the legacy/default model families. Loop B therefore never aliases
them into the default profile.

`--feature-profile loop-a-generation-v2` is the only adapter for those
datasets. It selects separate manifest contracts, maps `bar_timestamp` to the
assembler key, validates the persisted `available_at`, obtains ATR and
completion controls from the same generation's bar-shape dataset, and permits
the producer-native adjustment marker only when both inputs prove a zero-split
snapshot. The profile has independently named model features, so a model from
the legacy/default contract cannot be reused accidentally.

This profile is migration evidence, not equivalence evidence. The current
producer emits breakout-pressure for `1h` but not `1d`, and does not yet emit
all feature families required by `loop-a-all-v1`. A daily or weekly v2 route
therefore publishes `ROUTE_UNAVAILABLE` (or retains an explicitly stale
last-good forecast) when the required v2 family is absent. Removing that
blocker requires a deliberately versioned producer contract and model
validation; it must not be handled by weakening the default reader.

## Failure isolation

A failed symbol/horizon route no longer suppresses ready routes. The published
intelligence row is one of:

- `OPERATIONALLY_CURRENT` for a current forecast;
- `OPERATIONALLY_STALE` / `STALE_LAST_GOOD` when a prior successful forecast with
  the exact provider, feature/model/calibration/class-weight, cost, horizon, and
  specification contract is retained after the route fails; or
- `ROUTE_UNAVAILABLE` when no last-good forecast exists.

The run returns `COMPLETED_WITH_LIMITATIONS` and records redacted route errors.
The current weekly aggregate and contiguous Day 1 prefix remain an atomic
outlook per symbol; a failed symbol does not prevent another fully ready symbol
from issuing its weekly outlook.

## Crash and restart behavior

Runs are immutable. Objects and run outputs are written before a receipt, and
the authoritative `ml/latest/run.json` pointer changes last with an atomic
replace. Promotion, rollback, and retention share the same short publication
lock, so compare-and-swap validation and pointer replacement cannot race. A
failure before pointer replacement leaves the prior publication authoritative.
Duplicate scheduler invocation is rejected by a crash-released operating-system
file lock rooted in the Loop B output tree. Retention takes that same runtime
lock before the short publication lock, closing the run-directory/`.inflight`
creation gap without locking Loop A. A recurring process resolves the Loop A
pointer again for every cycle. An `.inflight` marker additionally protects a
run from retention until promotion completes.

Direct compatibility mirrors are convenience files rather than commit signals.
At startup Loop B verifies the authoritative pointer and reconciles those mirrors
from its selected immutable run, repairing an interruption between individual
mirror replacements without consulting or mutating Loop A.

Rollback restores only a previously verified receipt-chain entry:

```powershell
python -m ml.rollback_publication `
  --loop-b-output-root C:\data\duckets-loop-b-shadow `
  --confirm-rollback
```

The rollback validates the previous manifest and receipt, stages the pointer,
checks that the current pointer did not change concurrently, atomically replaces
it, reconciles every compatibility mirror from the selected immutable run, and
checksum-verifies those mirrors before returning. It never mutates Loop A.

Retention is dry-run by default and protects every run reachable from the
authoritative receipt chain:

```powershell
python -m ml.retention --loop-b-output-root C:\data\duckets-loop-b-shadow --keep-newest 3
python -m ml.retention --loop-b-output-root C:\data\duckets-loop-b-shadow --keep-newest 3 --apply
```

Only unreferenced immutable run directories without an `.inflight` marker are
eligible. The reachable set is recomputed while holding the shared publication
lock, and each selected run is atomically renamed before deletion. Loop A
objects, current or rollback-reachable Loop B runs, models, and pointers are
never retention targets.

## Cutover and rollback boundary

Do not switch production until all of these are evidenced:

1. The shadow generation reader completes the actual 20-symbol feature profile.
2. Shadow and legacy outputs are equivalent within the approved semantic
   tolerances for a representative window.
3. The measured Loop A live gate publishes before `+04`, and Loop B consumes
   fresh required data at `+05` and finishes before the next boundary.
4. Route failure, malformed object, stale required data, restart, and rollback
   drills pass on the deployment filesystem.
5. CI passes for both repositories and an operator explicitly approves cutover.

At approved cutover, change only the Loop B scheduler arguments from legacy
input to the validated generation input and separate output root. Keep the
legacy process and reader available. To roll back, stop new Loop B invocations,
restore the previous Loop B pointer if needed, restore the legacy scheduler
arguments, run one legacy validation cycle, and then resume. Never repoint or
stop Loop A as part of Loop B rollback.

## Evidence matrix

| Loop B goal from the redesign notes | Code evidence | Test evidence |
| --- | --- | --- |
| Split Loop A input and Loop B output | `ml/loop_a_input.py`, `ml/runtime_pipeline.py`, `ml/prediction_runtime.py` | generation read-only/output-root integration test |
| Validate immutable references without global route failure | eager pointer/manifest checks plus exact logical-prefix and `ResolvedLoopAInput.require_paths()` route validation | corrupt digest, size, rows, bounds, partition, schema, owner-path, timeframe relabel, and Parquet-identity tests |
| Preserve legacy compatibility | automatic/explicit legacy resolver | legacy CLI and absent-pointer tests |
| Keep producer v2 separate from legacy/default features | `loop-a-generation-v2` profile and v2 manifest contracts | default rejection plus missing-v2-family explicit-status test |
| Produce resumable machine-readable migration evidence | `ml/migration_report.py` equivalence and timing commands | cross-contract refusal and read-only 20-symbol fixture collection tests |
| Isolate one failed route | route-scoped materialization and intelligence publication | successful-symbol plus unavailable-symbol test |
| Retain last successful prediction | contract-matched prior authoritative intelligence lookup | compatible stale-last-good and incompatible-prior rejection tests |
| Bound watchlist to 20 and meet the next boundary in fixtures | `MAX_LOOP_B_SYMBOLS` and dynamic US-equity membership | complete 20-symbol fixture timing test |
| Restore a prior publication | `ml/rollback_publication.py` | atomic rollback and mirror-reconciliation tests |
| Prevent ambient credential loading | scoped `app.config` lookups | provider-free import/runtime suite |

The 20-symbol test is deterministic fixture evidence only. It is not live
provider, production filesystem, or production scheduling evidence.

Current migration verdict: **NOT READY**. Producer-v2/default feature
equivalence and complete daily/weekly v2 family coverage remain blocking gates,
in addition to the live and deployment evidence listed above.
