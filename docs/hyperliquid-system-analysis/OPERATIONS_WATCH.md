# Hyperliquid Operations Watch

Operating contract: **2026-09-28 / v14**. Companion to [Monitoring and recovery](MONITORING.md).

## Current accepted run and improvement handoff

The user authorized a separate [Paper improvement task](PAPER_IMPROVEMENT.md)
on September 28, 2026. That task now owns adaptive competitive rounds starting
at two hours, intentional archives,
fresh account mirrors and model changes. This watch's authority remains health
observation and narrowly permitted recovery only; it never improves or reseeds.

The improvement task's persistent cadence receipt defines each ending round:
a verified win adds one hour to the next round; a loss/tie retains its duration.
Unscored or late results cannot promote it. The prior three-day schedule and
60-hour guard are historical. This watch keeps its 30-minute schedule, does
not score/promote rounds or edit improvement timing, and must honor a pending
user real-account purchase before any explicitly requested fresh mirror.

Read `_operations/paper-maintenance.json` before recovery. `in_progress` means
an intentional transition, including a prepared/accepted opening awaiting live
verification. Remain observational throughout any pass that saw maintenance.
After a completed handoff, `_operations/paper-current-accepted.json` and its
referenced evidence define the current experiment, seed, opening balances,
config/source hashes and model membership. Cross-check `_paper/experiment.json`
and the immutable ledger opening. Missing or inconsistent handoff evidence is
an investigation, never permission to initialize a new ledger.

Compare immutable opening hashes using the native `seed_info` implementation in
`ml.hyperliquid_paper_review`, or the independent verifier's matching canonical
serialization. A custom compact-JSON hash is not comparable to a stored hash
created with default JSON separators. The September 28 initial-positions alert
was reconciled to whitespace alone; all four native opening hashes matched.

The latest verified durable handoff supersedes the dated v11 baseline and
model-policy values below. Those values remain historical context and must not
be restored over a newer accepted experiment. Use current accepted configuration
and model reports for parameters, families and qualification; treat changed
configuration without a verified handoff as a finding. Preserve the existing
30-minute schedule, GPT-6 Luna / Extra High, and notification settings. The
improvement task updates compact watch memory after verifying the new run.

The user-authorized September 28 frequency transition selects **5m candles /
h1 next-five-minute forecasts**, retraining due every 300s of source progress,
with 30s Paper book/risk polls. Read the newest accepted receipt to determine
whether that transition is complete. The old 15m ledger/models and a checked
copy of its candle/coordinator history are archives, never restart targets.
Keep the 5m candle history across later Paper resets. Longer-bar aggregation
and simultaneous collection across intervals are future work, not implied by
changing the trading interval. This watch must not change the interval.

## Schedule and scope

The app task **Hyperliquid Operations Watch** checks this local project every
30 minutes, around the clock, using **GPT-6 Luna / Extra High**. Each run is an
independent, bounded operations check, like **Loops Operations Watch**. Its
settings live in the app under automation ID `hyperliquid-operations-watch`;
this document defines the maintained procedure. Created and verified active on
September 26, 2026; current enabled state must be checked in Scheduled.

The watch may resume unexpectedly terminated data, model and **simulated Paper**
workers under the conditions below. Powder is observed only: report unexpected
termination, stale observations, HALTED/BLOCKED states and unresolved intents.
Never activate/restart Powder, submit/cancel/replace orders, transfer real funds,
change execution gates or resolve ambiguous orders by replaying them.

Local scheduled runs require the computer powered on, the app running and the
project available. Network/usage availability also limits an AI check. This is
a periodic check, not continuous supervision or an operating-system boot task.
An outage can remain undetected until the next successful scheduled check.
See the [official scheduling documentation](https://learn.chatgpt.com/docs/automations).

## One compact check

Use `C:/dev/ducketz`, its `.venv/Scripts/python.exe`, and
`C:/DATASTORE/hyperliquid`. Read the newest compact automation memory first.
Reuse the recorded contract when unchanged; inspect relevant source/config only
when configuration, evidence or incident state changes. Do not repeatedly read
full journals, audit every forecast, retrain, call private APIs or delegate broad
reviews on healthy wakes. Finish promptly; do not poll until the next schedule.

This local projection uses the same bounded read-only adapter as H.Y.P.E.R.
It does not construct either ledger writer or any trading runtime. Run from
the project directory:

```powershell
@'
import hashlib
import json
from dataclasses import asdict
from pathlib import Path
from app.services.hyperliquid_powder_view import HyperliquidWorkspaceViewService
root = Path("C:/DATASTORE/hyperliquid")
s = HyperliquidWorkspaceViewService(root, history_limit=8, journal_limit=5).load_snapshot()
keys = ("status", "state", "reported_status", "pid", "config_path", "process_alive",
        "updated_at_utc", "reason", "last_error", "config_error", "errors",
        "quote_errors", "funding_errors", "training_market", "pending_intents", "connected",
        "lifecycle_phase", "prepare_only", "stop_reason")
def runtime(row):
    result = {key: row[key] for key in keys if key in row}
    result["slot_errors"] = {name: {key: slot[key] for key in
        ("last_error", "last_prediction_error") if slot.get(key)}
        for name, slot in row.get("markets", {}).items()
        if isinstance(slot, dict) and (slot.get("last_error") or slot.get("last_prediction_error"))}
    return result
controls = {"coordinator": "_coordinator", "models": "_models/_runtime",
            "paper": "_paper/_runtime", "powder": "_powder/_runtime"}
sources = {key: {**asdict(value), "age_seconds": round(value.age_seconds, 1)
           if value.age_seconds is not None else None} for key, value in s.sources.items()}
print(json.dumps({
    "read_at_utc": s.observed_at_utc,
    "config_sha256": {name: hashlib.sha256(Path("configs", name).read_bytes()).hexdigest()
        for name in ("hyperliquid-markets.json", "hyperliquid-models.json",
                     "hyperliquid-paper.json", "hyperliquid-powder.json")},
    "portfolio_at_utc": s.portfolio_observed_at_utc,
    "paper_seed_at_utc": s.seed.get("timestamp_utc"),
    "paper_baseline_equity": s.seed.get("baseline_equity"),
    "runtime": {"paper": runtime(s.runtime), "models": runtime(s.runtime.get("models", {})),
                "coordinator": runtime(s.runtime.get("coordinator", {})), "powder": runtime(s.powder.runtime)},
    "stop_requests": {key: (root / path / "stop.request").exists() for key, path in controls.items()},
    "sources": sources,
    "powder_sources": {key: asdict(value) for key, value in s.powder.sources.items() if key.startswith("powder")},
    "warnings": list(s.warnings) + list(s.powder.warnings),
}, indent=2))
'@ | & .\.venv\Scripts\python.exe -B -
```

The UI projection resolves membership, interval and primary horizon from the
configured model/market files; the new trial uses BTC/ETH/HYPE/ZEC at 5m/h1.
Its strict health helper also binds every configured slot to the actual recipe
and target expiry. Compare the four
small configuration content hashes against memory each run; JSON `version`
fields are schema versions and do not detect edited settings. Inspect changed
configurations and any referenced files before claiming coverage; custom
membership/root/horizons still require matching raw checks before claiming coverage.
The process probe checks module identity, not only PID existence. Before any
recovery, additionally verify full commands, config/root, process creation time
and launcher/child relationships using `psutil` or `Win32_Process`. A reused PID
is not an owner; a venv launcher and its Python child are not two runtimes.
Access-denied/unverified identity is a blocker to restart, not proof of death.

Interpret observations using [Monitoring](MONITORING.md):

- Require current runtime heartbeats, advancing committed Paper observations
  and fresh data/forecasts. A living PID or saved `running` value alone is insufficient.
- Investigate slot errors even when the outer model runtime is running. Check
  current training progress before labeling a slow fit stuck.
- Let existing network retries/backoff work while a correctly identified worker
  is alive. Read only a bounded recent log tail for a new failure. No repeated
  public/private endpoint probes during outages.
- Data display grace is the configured candle duration plus 120s. Forecasts
  become stale at target maturity even inside display age grace. Paper's 5m/h1
  forecast age limit is 300s and its target expires at the next candle close.
  A forecast expiring near a candle boundary requires inspecting publication progress, not
  automatically restarting models. No trade, a research forecast and no promotion
  are not failures.
- The current Paper experiment is qualified-only. Research or missing forecasts
  do not drive signal trades: existing targets are held, with independent risk
  exits still possible. Excluded Research forecasts are expected, not evidence
  that the model process is broken. Monitor prolonged data/forecast outages even
  when Paper continues marking and checking risk. Never loosen this gate to
  produce trades or treat a risk exit as an unqualified signal entry.
- A performance export aged 600–900s can be normal while committed cycles advance.
  Do not restart workers for export-only lag or transient SQLite read contention.
- A fresh mirror has an explicit `opening` observation: untouched signed inventory,
  $0 experiment P/L, zero fills and zero fees. `opening_prepared` or
  `prepare_only` is intentional preparation, not a failed trading worker. Subsequent
  Qualified signals or risk limits can change holdings and incur actual simulated
  execution costs; compare against the immutable opening, not the later Actual UI.
- Powder with no prior activation/observations and no active owner is expected
  off. Do not alert merely because it has no ledger. Once activated by the user,
  compare saved state and observations with the last verified session. Explicit
  STOPPED/stop intent is intentional; HALTED, BLOCKED, unknown/open intents or a
  vanished RUNNING owner require a finding, even when intervention is out of scope.

## Recovery of an interrupted Paper session

The following v11 numerical baseline is dated September 26. Once a v12 durable
handoff exists, substitute its accepted experiment and configuration throughout
these recovery checks. All stop/ownership/ledger-preservation conditions remain.

The current baseline is data + models + qualified-only Paper intended running,
Powder off. The latest user-authorized mirror is
`20260926T180818Z-701515-btc-mirror`, opened at
**2026-09-26T18:08:18.218420982Z**, with opening equity
**44083.99583107362** and nine inherited positions. Per-account baselines are
Alex **5736.898979623619**, Jeremy **6253.81725676128**, and Clear Pond
**32093.27959468872**. Prepare-only verification confirmed one opening cycle,
four equity rows, zero fills/decisions/transfers/funding, and zero experiment P/L
and fees before trading. Clear Pond's opening BTC is **0.2239149983**, an increase
of **0.02377618 BTC** from the preceding opening, incorporating the user's added
inventory. The difference between openings does not identify deposit timing or
price. Independently retained public source responses reconcile every signed
quantity, perpetual entry and account cash calculation with the seed.
Source-to-opening valuation differences were Alex
-0.006585, Jeremy +0.004615 and Clear Pond 0. Account reads and quotes remain
non-atomic; do not manufacture cash adjustments to force display equality.

Evidence is under
`_operations/20260926T1804020242875Z-fraction701515-btc-remirror/`:
`opening.json`, `opening-source-verification.json` and `opening-health.json`
verify preparation and source reconciliation; `model-split-verification.json`
records the fresh model identities and split sizes. `first-cycle.json`
independently replays later accounting against the immutable opening hashes.
`trading-health.json`, observed **18:10:58.439631 UTC**, verified data **57004**,
models **31624** (launcher **64232**, hidden cmd **57632**) and Paper **57936**
(launcher **2280**, hidden cmd **48216**), fresh sources and an advancing Paper
observation at **18:10:42.901325 UTC**, with no reported errors or warnings.
That observation had eight cycles and seven fills: equity **44053.32172417838**,
P/L **-30.67410689524** and fees **26.623819045**, with zero funding or transfers.
These post-opening strategy/risk executions are separate from the verified
zero-cost opening; they do not alter its baseline. Powder was disconnected
with zero pending intents.
Use newer automation memory and actual process identity for recovery; these
dated observations do not establish permanent health.

Current Paper policy **`fdf124acdc8fc9a6`** retains
`rebalance_min_delta_fraction=0.20`. Ordinary changes must reach
the greater of $25 or 20% of final target notional; complete exits and required
risk reductions still bypass that adjustment threshold. The wider threshold
is a prospective turnover experiment, not evidence of improved returns.
This deployment changed the model configuration hash; market/Paper/Powder
configurations did not change from v10. Compare all four current content hashes
against accepted memory.

Shared **49%/51% entry and exit** thresholds remain:
`entry_band=exit_band=0.01`, long eligibility at or above 51% and short eligibility
at or below 49%, for flat and held positions. Shared-threshold sizing uses
absolute edge/saturation, with nonzero size at an eligible boundary. Qualified-only
signals, minimum trades, exposure caps, stops and cooldowns remain in force.
Fills consume fetched executable depth at its VWAP with **zero additional
slippage**, plus configured taker fees **0.00045** for perps and **0.0007** for spot.
Spread, depth and changing marks still affect P/L; no account-specific fee tier
was inferred.

The approved live model split is now **70% fitting / 15% calibration / 15%
assessment**, configured with `split_mode=fractions`, the three explicit
fractions and `max_train_rows=null`. Fractions allocate mature usable rows
chronologically before purging: floor fit/calibration counts and place rounding
remainder in assessment. Labels reaching either next partition's first decision
are excluded without refilling. Reports distinguish requested fractions from
actual retained counts, purges and any window omissions. Fixed 192/288-row
blocks belong to earlier experiments and are no longer the active split.

Production retains the same four model types: logistic regression, Extra Trees,
histogram gradient boosting and MLP. MLP maximum iterations **100**, minimum
fit rows **1,000**, horizon **four 15-minute candles** and **900-second** retraining
remain unchanged. CNN/GRU/CNN-GRU and other expanded comparisons are offline
research only; the watch must not activate them. Earlier regularization,
MLP 300-iteration and training-window proposals remain rejected historical
experiments; see the [training comparison](audits/2026-09-26-training-regularization-and-budget.md).

The final 15% is still the chronological **promotion holdout**, used to qualify
the returned fitted/calibrated bundle. It is not an untouched final test, and
there is no refit through assessment afterward. The initial fresh fits therefore
end **381.75–382.75 hours** behind the input tip; this reported fit cutoff lag is
expected under the approved split, not a dead worker or stale publication.
Keep checking publication/model age, current forecast inputs and actual process
health separately. Fresh fits initially qualified ETH/HYPE, while BTC/ZEC were
Research; qualifications can change. Split validation and retrospective losses
do not establish profitability. See the [model guide](../hyperliquid-models.md).

The execution contract remains
`quote_execution_policy=forecast_first_bounded_quote_retry_v1`: freeze forecasts
before fresh books, attempt at most three snapshots per tick, and recheck expiry.
Pure all-account quote skips without fills/transfers/funding remain pending
while valid; narrow append-only recovery may handle historical quote-only skips.
Completed, partial or mixed executions remain consumed. Never remove completion
keys or force a replay. Deferred quotes alone do not warrant a restart; report
prolonged outages. Historical policy phases and decision checks are in the
[shared-threshold audit](audits/2026-09-26-shared-49-51-paper.md),
[quote-retry audit](audits/2026-09-26-paper-quote-retry.md) and
[decision-gate audit](audits/2026-09-26-paper-decision-gates.md).

`persisted_position_risk_reference_v1` preserves inherited historical perpetual
entry for accounting and the opening mark as the fresh stop reference. Partial
reductions retain the reference; additions weight it with executed entry; a new
position starts at its fill price. Legacy resumes retain their persisted
`legacy_avg_entry` reference. Never rebase stops, rewrite the seed or reset
cooldowns on restart.

The superseded **15:49:57** experiment and its full `_paper` and `_models` trees
are preserved at
`_paper_archives/20260926T1804020242875Z-before-fraction701515-btc-remirror`:
**205 files / 116,086,884 bytes**. Its original files match the SHA-256 manifest
in the current evidence directory's `preservation-before.json` and
`preservation-verified.json`; read-only SQLite integrity also passed before the
move. `operation.json` identifies the preserved predecessor. Data continued;
models and Paper were intentionally stopped and replaced. That old baseline
does not contain the latest BTC inventory and is not a recovery target.

The superseded **12:34:36** experiment and its full `_paper` and `_models` trees
are preserved at `_paper_archives/20260926T154800Z-before-training-retune`:
**404 files / 256,957,217 bytes**, with hashes verified by
`_operations/paper-retune-20260926/preservation-before.json` and
`preservation-verified.json`. Healthy data continued during the transition;
models and Paper were intentionally stopped and replaced. This archive is
historical evidence, not a recovery target.

The superseded **12:00:59** mirror remains intact at
`_paper_archives/20260926T120059Z-fresh-mirror-opening-replaced`, with manifest
`_operations/paper-remirror-zero-slippage-preservation.json`; its historical
nonzero-slippage fills remain evidence. See the
[book-VWAP restart audit](audits/2026-09-26-book-vwap-paper.md).
The disputed **11:30:32** mirror remains intact at
`_paper_archives/20260926T113032Z-fresh-entry-4pp-disputed`, with manifest
`_operations/paper-restart-20260926-preservation.json` and original metadata.
Its `analysis_eligible: true` metadata does not establish user acceptance or
authorize recovery; see the [reconstruction](audits/2026-09-26-paper-restart-reconstruction.md).

The **10:35:59** predecessor is permanently excluded from every evaluation and
recovery use. Automatic review initially rejected recursive removal; the user
completed deletion, and `_pending_deletion/20260926T103559Z-excluded-paper` was
verified absent at **2026-09-26 11:36:32 UTC**. Never analyze, reconstruct or
restore that sample. `_operations/excluded-paper-runs.json` records the exclusion.
Still-earlier archives and independent model research remain separate and
preserved. These restrictions remain in force under contract v11.

The watch may resume only the current verified seed; no further mirror or ledger
reset is authorized for the watch. Historical intent never overrides newer
operator stops, maintenance or user instructions.

1. First inspect `_operations/paper-maintenance.json` when present. An
   `in_progress` record forbids recovery/reseeding while its owner completes an
   intentional experiment change. Respect every `stop.request`, including
   per-market requests, terminal
   `stopped` state, `prepare_only=true`/`stop_reason=prepare_only`, keyboard
   interrupt, finite/once completion, maintenance note
   or newer user instruction. Paper may record `failed` during a graceful stop
   after a degraded tick; that ambiguous terminal state requires reporting, not
   an automatic restart. Missing status/history also requires investigation.
2. Recovery is permitted only for a previously verified continuous worker whose
   saved active state remained after its exact owner disappeared, with no stop
   intent or competing owner. Inspect any other Paper/Powder session before
   starting Paper; a switch to Powder may intentionally retire Paper. Do not
   run a second supervisor while another chat/operator is handling this incident.
3. Verify accepted config/root and the existing Paper ledger/seed are readable.
   Never initialize a missing ledger, reseed balances, alter strategy/risk/model
   settings, delete locks/stop markers, overwrite evidence or reset funding.
   `--prepare-only` is a deliberate operator lifecycle action, not a watch recovery
   action; do not turn a prepared opening into a trading session without newer
   operator authorization. A normal permitted recovery resumes the same seed and
   never adds another opening cycle or synthetic fill.
   The runtimes' own exclusive lifetime locks remain the final ownership gate.
4. Resume only absent components, upstream first: data → models → Paper. Verify
   coherent publications and inspect forecasts using
   `ml.hyperliquid_forecast_reader.read_forecast` with the loaded Paper policy,
   market interval and configured horizon. Also enforce
   `prediction["qualified"]` for signal eligibility when
   `policy.require_qualified_forecasts` is true; the validator corroborates an
   active eligible model but leaves consumer inclusion policy to its caller.
   Research-only slots do not prohibit resuming qualified-only Paper's risk and
   mark loop: those positions hold until an eligible signal or risk reduction.
   No qualified signals is an allowed operating state. Report upstream faults
   accurately instead of loosening the gate. The validator reads artifacts
   without creating a runtime. Keep healthy workers running. Do not force fitting
   or kill a live but stalled process from this watch.
5. Launch through the hidden Windows method below, at most one attempt per
   component per run. Recheck stop intent and process identity immediately before
   launch. Never remove a lock on launch conflict. Do not repeatedly retry an
   unchanged failure on later wakes; wait for changed evidence or operator action.
6. Verify the matching actual runtime and advancing heartbeat; for Paper require
   a newer committed portfolio observation and unchanged opening seed/baseline.
   A successful process-creation return is not recovery. Use one bounded follow-up
   after approximately 35 seconds, with a second only if active startup progress
   justifies it. Report unresolved startup rather than waiting indefinitely.

When multiple observers might intervene, leave recovery to the already recorded
owner; if ownership cannot be established, report the incident. This schedule
does not supply a cross-chat supervisor lease. Component locks prevent a second
runtime from taking over, but do not authorize competing recovery attempts.

## Independent hidden launch reference

Use only after the recovery checks above, for one allowed component. Values in
this example select Paper; use the corresponding module/config/control directory
for data or models. No Powder command belongs in this recovery procedure.

```powershell
$module = 'ml.hyperliquid_paper_runtime'
$config = 'hyperliquid-paper.json'
$control = 'C:\DATASTORE\hyperliquid\_paper\_runtime'
$stamp = Get-Date -Format 'yyyyMMdd-HHmmss'
$startup = New-CimInstance -ClassName Win32_ProcessStartup -ClientOnly -Property @{ShowWindow=[uint16]0}
$line = 'C:\Windows\System32\cmd.exe /d /s /c ""C:\dev\ducketz\.venv\Scripts\python.exe" -u -m ' + $module + ' --config "C:\dev\ducketz\configs\' + $config + '" 1>"' + $control + '\watch-' + $stamp + '.stdout.log" 2>"' + $control + '\watch-' + $stamp + '.stderr.log""'
Invoke-CimMethod -ClassName Win32_Process -MethodName Create -Arguments @{
    CommandLine=$line; CurrentDirectory='C:\dev\ducketz'; ProcessStartupInformation=$startup
}
```

| Component | Module | Config | Control directory under datastore |
| --- | --- | --- | --- |
| Data | `ml.hyperliquid_coordinator` | `hyperliquid-markets.json` | `_coordinator` |
| Models | `ml.hyperliquid_model_runtime` | `hyperliquid-models.json` | `_models/_runtime` |
| Paper | `ml.hyperliquid_paper_runtime` | `hyperliquid-paper.json` | `_paper/_runtime` |

This method was verified during the [September 26 recovery](audits/2026-09-26-paper-session-recovery.md).
It avoids the Codex-owned process tree; verify current ancestry instead of
assuming hidden `Start-Process` or a breakaway flag guarantees independence.
It does not survive reboot or logoff by itself and installs no Windows service.

## Memory and results

Keep a compact latest-state section in the task's automation memory: observation
time, contract version/config content hashes, expected active/intentional-stop state, verified
runtime identities, last committed Paper timestamp, original seed/baseline,
Powder session/state, open incident signature, recovery attempt and outcome.
Preserve unresolved findings across runs. Never infer renewed start permission
from elapsed time. Record no private keys, environment dumps or credentials.

Before writing memory or a final report about a maintenance/contract mismatch,
reread the maintenance marker, current runbook version and newest memory once.
If they changed during the check, distinguish the earlier observation from the
current verified state and resolve only findings the new evidence actually
settles. A pass that observed intentional maintenance remains observational;
completion during that pass does not authorize recovery in the same pass.
Preserve dated findings as resolved history, without leaving an outdated alert
as the current incident. Do not poll for maintenance to finish.

The maintenance owner updates the phase and expected state when resuming for
verification. Publish completion in this order: finish verification and update
the runbook/app prompt, atomically mark maintenance completed, then publish
accepted memory with the actual completion timestamp. A health observation
timestamp is not the maintenance-completion timestamp.

Healthy/unchanged runs need a minimal result. A finding should identify the
affected component, exact new evidence, action taken or blocker, verification
and next step. Put material incident evidence in dated `audits/` records; use
the changelog for operating-contract changes, not a new entry every 30 minutes.
Keep the app prompt and this procedure aligned when behavior changes.
