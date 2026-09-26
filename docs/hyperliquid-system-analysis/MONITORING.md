# Monitoring and recovery

Last source verification: **2026-09-25**. This is a code/configuration audit,
not a claim that a saved PID or worker is currently alive. Commands below use
`C:/dev/ducketz` as the working directory and the supplied datastore root.
Model fitting cadence updated **2026-09-26** to 900 seconds.
Read [the system overview](README.md) and [loop inventory](LOOP_INVENTORY.md)
before changing process ownership. The separately configured
[Operations Watch](OPERATIONS_WATCH.md) performs a compact check every 30 minutes,
with conditional Paper recovery and read-only Powder monitoring. It requires
the local app/PC to be running and is not a Windows boot service.

On September 26, the workers were restored after a Codex app restart using
independent Windows process launches. See the dated
[recovery audit](audits/2026-09-26-paper-session-recovery.md) for evidence and
launch-lifetime limitations. Hidden windows alone do not establish that a
worker can survive the launching host's exit.

## Keep four different questions separate

| Question | Evidence | What it does not establish |
| --- | --- | --- |
| Is the process healthy? | Matching process command, current heartbeat, runtime and per-market errors | Fresh candles, valid forecasts, profitable trading |
| Are artifacts fresh and complete? | Published candle close, feature completeness, prediction availability and IDs | A model passed qualification or a forecast is still executable |
| Is a forecast eligible for Paper? | Runtime validation of symbol/horizon, complementary probabilities, decision/model age, future outcome and source volatility | An order filled or the model will make money |
| How has Paper performed? | Committed ledger equity, transfer-adjusted P/L, costs and drawdown | Live-money results or model qualification |

`qualified` / `research` are evaluation labels. The current Paper experiment
requires qualified signals; Research or unavailable forecasts retain existing
targets with independent risk checks. The previous mixed-model run is archived.
An alive model runtime can have a failed market slot while
retaining an older predictor. Read the slot errors even if its outer status is
`running`. Powder's **Not connected** state is expected until the separate runner
has recorded actual observations. See [Powder activation/recovery](POWDER_ACTIVATION.md),
[accounting and risk](PAPER_ACCOUNTING_AND_RISK.md) and
[UI contracts](UI_AND_DATA_CONTRACTS.md).

## Evidence locations

Paths below are relative to `C:/DATASTORE/hyperliquid`; `<coin>` currently means
BTC, ETH, HYPE or ZEC. Runtime membership comes from
[market configuration](../../configs/hyperliquid-markets.json).

| Component | Read these files / fields |
| --- | --- |
| Coordinator | `_coordinator/coordinator_status.json`: `updated_at_utc`, `status`, `config_error`, `markets`; `_coordinator/coordinator_events.jsonl` |
| Market worker | `<coin>/15m/loop_status.json`: `status`, `next_wake_utc`, `last_error`, `last_result`, `last_cycle_timing`; `loop_events.jsonl` in the same directory |
| Published data | `<coin>/15m/latest.json` → `runs/<run_id>/summary.json`, `feature_catalog.json`, `features.parquet`, `ohlcv.parquet`, `labels.parquet` |
| Model runtime | `_models/_runtime/status.json`: `config_error`, `training_market`, `timing_write_error`, `completed_jobs`, `markets["BTC/15m/h4"]` and other slots |
| Model slot | `status`, `training`, `last_error`, `last_prediction_error`, `last_prediction`, `candidate`, `last_report`, `last_training_timing` |
| Forecast / model | `_models/<coin>/15m/h4/latest_prediction.json`, `candidate.json`, `active.json`, `runs/<model_id>/record.json` and `report.json` |
| Training journal | `_models/_runtime/training_events.jsonl`: `outcome`, `timing`, source/model identifiers |
| Paper runtime | `_paper/_runtime/status.json`: `updated_at_utc`, `status`, `last_error`, per-symbol `errors`, `quote_errors`, `funding_errors` |
| Authoritative Paper | `_paper/ledger.sqlite3`: committed `cycles`, `equity`, `positions`, `decisions`, `fills`, `transfers`, `funding`, `seed` |
| Paper context / exports | `_paper/policy.json`, `policies/<policy_id>.json`, `opening_snapshot.json`, `performance.json`; `_paper/_runtime/funding_cursor.json` |
| Timing exports | `_timings/report.md`, `summary.json`, `timings.parquet`; these are point-in-time exports |

Lifetime ownership uses `_coordinator/.coordinator.lock`, each
`<coin>/15m/.loop.lock`, `_models/_runtime/.runtime.lock`, and
`_paper/_runtime/.paper.lock`. Data publication also uses
`<coin>/15m/.update.lock`. Each lifetime control directory has its own optional
`stop.request`. A file's presence alone does not prove lock ownership. Do not
delete locks or stop markers to make a status look healthy.

## Routine inspection: read paths only

Prefer [HyperliquidPaperViewService](../../app/services/hyperliquid_paper_view.py)
or H.Y.P.E.R.'s Operations panel. This adapter never constructs `PaperLedger`,
starts workers, probes runtime locks, fetches quotes, or changes policies.
It uses SQLite `mode=ro`, `query_only=ON`, a bounded transaction and query time
budget. Standard SQLite WAL reading can still create/use coordination sidecars
(`-wal` / `-shm`); it does not mutate authoritative ledger rows. Do not promise
zero filesystem activity, or copy only the database file during an active WAL.

This prints a bounded diagnostic projection without running a runtime CLI:

```powershell
@'
import json
from dataclasses import asdict
from app.services.hyperliquid_paper_view import HyperliquidPaperViewService
s = HyperliquidPaperViewService("C:/DATASTORE/hyperliquid").load_snapshot()
print(json.dumps({
    "read_at_utc": s.observed_at_utc,
    "portfolio_at_utc": s.portfolio_observed_at_utc,
    "sources": {key: asdict(value) for key, value in s.sources.items()},
    "warnings": s.warnings,
    "equity": s.pooled.get("equity"),
    "paper_pnl": s.pooled.get("total_pnl"),
}, indent=2))
'@ | & .\.venv\Scripts\python.exe -B -
```

The process probe checks PID existence and module command identity with
`psutil`; unavailable permissions/dependency produce **unverified**, not proof
of health. This is a read-only observation, not an atomic process-lifetime
guarantee. `sources` distinguishes `fresh`, `missing`, `partial`, `stale`,
`stopped` and `error`; raw `reported_status` and `process_alive` remain in `runtime`.

Read raw evidence when a summarized badge does not explain a market failure:

```powershell
Get-Content -LiteralPath 'C:/DATASTORE/hyperliquid/_models/_runtime/status.json' -Raw
Get-Content -LiteralPath 'C:/DATASTORE/hyperliquid/_paper/_runtime/status.json' -Raw
Get-Content -LiteralPath 'C:/DATASTORE/hyperliquid/BTC/15m/loop_events.jsonl' -Tail 8
Get-Content -LiteralPath 'C:/DATASTORE/hyperliquid/_models/_runtime/training_events.jsonl' -Tail 8
& .\.venv\Scripts\python.exe -B -m ml.hyperliquid_timings --hours 24
```

The timing command without `--export` reads existing evidence and prints a
report. Adding `--export` **writes** the three `_timings` outputs; it is an
explicit export operation, not required for monitoring. A partially appended
JSONL tail may be incomplete; do not rewrite the journal to repair its last line.

The current UI adapter fixes its market list to BTC/ETH/HYPE/ZEC and paths to
`15m/h4`; the normal app also uses its default datastore root. A custom runtime
universe/root requires inspecting the corresponding raw files and updating
the projection before treating the UI as complete. See [maintenance](MAINTENANCE.md).

## Clocks, source ages and measured work

| Clock | Current configured behavior | Inspect separately |
| --- | --- | --- |
| Data coordinator | Checks configuration about every second; at most two pipeline updates | Coordinator heartbeat versus each worker's candle close |
| Data publication | 15m close + 5s; startup catch-up; failed/late attempts retry from 15s up to 120s | `lag_intervals_after_delay`, `expected_close_utc`, gap counts and `next_wake_utc` |
| Model polling | Every 5s; one background training job globally | Poll heartbeat versus `training_market` and candidate publication |
| Candidate fitting | Every 900s (15m) of source-candle progress; failed fit/publication retry delay 60s | Fit/calibration cutoffs versus publication time; qualification gates remain unchanged |
| Forecast publication | Each completed 15m candle; four bars = one-hour horizon | `decision_close_utc`, `created_at_utc`, `target_close_utc` |
| Paper quote / risk cycle | About every 30s, plus work duration | Portfolio observation, errors and committed cycle |
| Funding lookup | At most once per 300s in Paper | Funding cursor and missing settlement/price-proxy errors |
| Paper exports | After changes or 900s since export; Parquets also on graceful shutdown | `performance.as_of_utc` versus latest ledger observation |
| UI reads | About every 5s, one bounded read worker | View refresh is not a new quote, forecast or valuation |

Values come from [market](../../configs/hyperliquid-markets.json),
[model](../../configs/hyperliquid-models.json) and
[paper](../../configs/hyperliquid-paper.json) settings; check saved policy and
running configuration before applying them to another installation.

Adapter age thresholds are display heuristics: data/forecast sources allow
1,020s, candidates twice the configured fitting cadence (1,800s at 900s), and
other sources normally twice their declared cadence. Paper's decision-candle
eligibility limit is **900s**, so a
forecast can fail Paper validation before its UI age badge becomes stale.
The adapter assigns performance exports the runtime's 900s cadence and flags
age above 1,800s. The previous 300s assumption caused normal quiet-cycle exports
to be labeled stale at 600s. Export age is separate from the latest committed
portfolio; an export-only delay does not prove a worker fault.
Missing/future timestamps or explicit errors can override age-only status.
The [September 26 rollover audit](audits/2026-09-26-paper-forecast-rollover.md)
found stale-forecast reductions followed by reentries around 32 seconds later.
This was trading-policy turnover, not evidence that the model process needed a
restart. The new qualified-only Paper recipe holds targets without an eligible
signal, with risk checks still active; the legacy mixed-model recipe retains
its prior zero-target fallback. An **Unavailable** historical fill label can mean
a reduction with no forecast attribution; it is not a third model quality tier.
See the [qualified-only experiment](audits/2026-09-26-qualified-only-paper.md).

Paper also limits model age to 24h and executable book age to 45s under the
supplied policy. Runtime `updated_at_utc` is not an individual book timestamp;
do not derive a universal quote-age claim from that heartbeat.

For measured work, read [timing boundaries](../hyperliquid-timings.md):

- Data `total_before_publish` includes pipeline work before final publication;
  outer `last_cycle_timing.total_seconds` also includes queue/publication and
  loop overhead. Queue wait is separate. The adapter's residual
  `publication_and_overhead_seconds` is **not** isolated publication duration.
- Model `worker_seconds` is fit/build work; `queue_wait_seconds`,
  `collection_wait_seconds` and `publication_seconds` are separate.
  `operation_seconds` excludes waits; `end_to_end_seconds` includes them.
  Legacy `last_training_seconds` includes submission-to-collection delay.
- `training_completed` means an attempt was collected. Read its `outcome`:
  `promoted`, `research_only`, `training_failed`, `publication_failed`, or
  `discarded_after_removal_or_stop`. Read sample counts before comparing p95s.

## Runtime controls are a separate operational action

Do not classify all `--status` commands as filesystem-pure reads:

| CLI | Actual status behavior |
| --- | --- |
| Coordinator / model | Reads saved JSON, then probes the lifetime `FileLock`; absent status returns early. `--output-root` selects the control root without parsing config. |
| Single-symbol data loop | Builds `LoopConfig`, reads saved JSON and probes `.loop.lock`; absent status returns early. |
| Paper | Parses paper config, creates `_paper/_runtime` even when absent, reads JSON if present and probes `.paper.lock`. No `--output-root`; root comes from `--config`. |

These implementations do **not** rewrite status JSON in `--status`. The lock
probe can create a lock path and the installed Windows `filelock` implementation
attempts to unlink it after successful release. Status adjustments such as
`not_running` are returned in memory. Paper instead adds `running` without
normalizing a stale saved `status`. The adapter above avoids these probes.
Sources: [coordinator](../../ml/hyperliquid_coordinator.py),
[data loop](../../ml/hyperliquid_data_loop.py),
[models](../../ml/hyperliquid_model_runtime.py),
[Paper](../../ml/hyperliquid_paper_runtime.py).

The following commands are **lifecycle references**, not diagnostic steps.
START means running the module without a control flag; there is no `--start`
flag. Each command runs in its own terminal when continuous operation is desired.

```powershell
& .\.venv\Scripts\python.exe -m ml.hyperliquid_coordinator
& .\.venv\Scripts\python.exe -m ml.hyperliquid_model_runtime
& .\.venv\Scripts\python.exe -m ml.hyperliquid_paper_runtime
```

START fetches/publishes data, trains/publishes models, or changes the virtual
ledger according to the selected component. Paper opens an existing ledger;
if absent, configured mirror/manual initialization creates an opening baseline.
Do not use START to investigate a missing database. `PaperLedger` construction
itself can initialize schema/state and is not a display-read API.

`ml.hyperliquid_coordinator --max-cycles 1` bounds attempts per symbol, including failures.
`ml.hyperliquid_model_runtime --once` can train, publish and record predictions/forward scores;
`--force-train` requests new fits. `ml.hyperliquid_paper_runtime --once` initializes/resumes,
executes one quote/risk cycle, may fill/transfer/fund, and exports on shutdown.
None is a dry run. All retain lifetime ownership checks.

```powershell
& .\.venv\Scripts\python.exe -m ml.hyperliquid_paper_runtime --stop
& .\.venv\Scripts\python.exe -m ml.hyperliquid_model_runtime --stop
& .\.venv\Scripts\python.exe -m ml.hyperliquid_coordinator --stop
```

STOP writes a graceful stop request if ownership is detected. Coordinator,
model and single-symbol controls recheck ownership and remove a redundant
marker if the owner exits during the request; Paper does not implement that
second race check. A returned request is not proof of completed shutdown.
For custom roots use coordinator/model `--output-root`; Paper needs the matching
`--config`. `Ctrl+C` requests the same graceful intent in the foreground.

Coordinator waits for its managed threads; it does not stop an external
single-symbol loop. Models wait for the current bounded fit and discard its
result after a stop request. Paper finishes its current tick, records terminal
status, exports Parquets and closes the database. **Stopping Paper does not
flatten positions, continue risk checks, or pause with active protection.**

## Recovery decisions

| Observation | Read first / recovery boundary |
| --- | --- |
| `waiting` after a current candle | Check `next_wake_utc`; normal scheduled wait needs no restart. |
| `waiting_for_candle`, `retrying` | Check lag, `last_error`, attempts and backoff. Let bounded retries work; preserve last published snapshot. |
| Unrecoverable candle gaps | Check `unrecoverable_gap_count` and source window. The latest-5,000-candle endpoint cannot recover older gaps; do not invent candles. |
| `external_worker` / lock conflict | Identify the actual owner and root. Do not launch a duplicate or delete its lock. |
| `config_error` | Compare accepted/running settings with edited JSON. Coordinator/models retain accepted settings; non-symbol changes require a planned restart. Paper has no last-good shared-market-config fallback: invalid market config degrades its current tick. |
| `waiting_for_data` / feature mismatch | Follow the data run ID, feature catalog and immutable run files before restarting models. |
| `training_failed` / `publication_failed` | Inspect slot error, event outcome and disk access. Preserve candidate/active history; forcing training is not a read-only repair. |
| Research forecast / no promotion | Inspect qualification report and forward metrics; research output is not itself a runtime failure. |
| `last_prediction_error` | Check predictor age, schema/source identity and latest feature row; a healthy outer runtime may still lack an eligible forecast. |
| Paper `errors[coin]` | Read exact forecast validation reason: decision age, model age, horizon, probability, source row or matured outcome. Qualified-only Paper holds targets without an accepted signal, while risk reductions require executable quotes. The legacy mixed-mode recipe uses zero targets. |
| `quote_errors` / missing held mark | Read route/book error and `last_error`; a cycle can degrade. Do not label retained values fresh or substitute candle prices for execution. |
| `funding_errors` | Check exact completed-candle proxy and funding cursor. Missing settlement evidence is not a zero cashflow. Preserve the cursor/history. |
| Partial fills / holds / skips | Inspect requested, executed and unfilled quantities plus reason/policy. A target is not a fill; no action may be correct. |
| Ledger missing/partial/read timeout | Inspect configured root, permissions, schema and warnings. Keep data intact; never reseed or rewrite the ledger to clear a UI placeholder. |
| `timing_write_error` / lagging export | Read underlying publication/ledger evidence. A timing journal failure or older export does not invalidate a successful committed operation. |

For an authorized full maintenance restart, account for Paper's unprotected
stopped inventory first; stop downstream before upstream, verify completion,
then restore data → models → Paper. Confirm current coherent data, a valid
forecast and quotes before expecting normal decisions. Existing Paper resumes
its ledger and reconstructs stop cooldowns; preserve opening seed, policies,
funding cursor and journals. Upstream-only interruption while Paper remains
running can invalidate forecasts. Qualified-only Paper then holds targets with
risk checks; legacy mixed-mode Paper can make stale-signal reductions.

Record incident times, root/config identity, source/model/forecast/cycle IDs,
first error and recovery in dated `audits/` evidence using
[the maintenance conventions](MAINTENANCE.md#evidence-conventions). Reserve
[CHANGELOG](CHANGELOG.md) for behavioral or operating-contract changes. Never
use blind reseeding, lock removal, pointer edits, or copied PIDs as recovery steps.
