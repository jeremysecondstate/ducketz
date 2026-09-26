# Hyperliquid Operations Watch

Operating contract: **2026-09-26 / v4**. Companion to [Monitoring and recovery](MONITORING.md).

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
        "quote_errors", "funding_errors", "training_market", "pending_intents", "connected")
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

The UI projection currently covers BTC/ETH/HYPE/ZEC at 15m/h4. Compare the four
small configuration content hashes against memory each run; JSON `version`
fields are schema versions and do not detect edited settings. Inspect changed
configurations and any referenced files before claiming coverage; custom
membership/root/horizons require matching raw checks before claiming coverage.
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
- Forecast display grace (1,020s) differs from Paper eligibility (900s). A forecast
  expiring near a candle boundary requires inspecting publication progress, not
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
- Powder with no prior activation/observations and no active owner is expected
  off. Do not alert merely because it has no ledger. Once activated by the user,
  compare saved state and observations with the last verified session. Explicit
  STOPPED/stop intent is intentional; HALTED, BLOCKED, unknown/open intents or a
  vanished RUNNING owner require a finding, even when intervention is out of scope.

## Recovery of an interrupted Paper session

The current baseline is data + models + qualified-only Paper intended running,
Powder off. Both earlier Paper runs are archived. The latest user-authorized
mirror opened at 2026-09-26T10:35:59.867633343Z with opening equity
42091.46503857236 and nine positions; see the
[comparison and refresh record](audits/2026-09-26-model-comparison-and-paper-refresh.md).
The tested training changes were rejected: retain uncapped fitting, calibration
192, assessment 288 and 900-second retraining. `max_train_rows` is an available
but unused setting. Use the latest seed from automation memory. Older seeds are
historical, not recovery targets.
The same ledger now continues under the user-authorized broader-entry phase:
`entry_band=0.04`, policy `993e26589020a5a0` (54% long / 46% short). Model
qualification and risk limits remain unchanged. Updated content hashes and
worker identities in memory supersede those from the original seed launch.
Hold/Skip decisions now save specific checks; they are expected outcomes, not
missing fills or grounds for recovery. See the
[phase record](audits/2026-09-26-paper-decision-gates.md).
Historical intent is not permission to override newer operator actions.

1. First inspect `_operations/paper-maintenance.json` when present. An
   `in_progress` record forbids recovery/reseeding while its owner completes an
   intentional experiment change. Respect every `stop.request`, including
   per-market requests, terminal
   `stopped` state, keyboard interrupt, finite/once completion, maintenance note
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

Healthy/unchanged runs need a minimal result. A finding should identify the
affected component, exact new evidence, action taken or blocker, verification
and next step. Put material incident evidence in dated `audits/` records; use
the changelog for operating-contract changes, not a new entry every 30 minutes.
Keep the app prompt and this procedure aligned when behavior changes.
