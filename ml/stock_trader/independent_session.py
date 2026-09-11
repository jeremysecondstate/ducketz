"""Bounded native stock-session worker for independent entries and exits.

An entry batch runs once per hourly action slot. Extra checks manage existing
allocations only; they cannot multiply the number of entry batches.
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path

import pandas as pd

from datafetching.runtime_lock import exclusive_runtime_lock
from ml.independent_stock_targets import STOCK_TARGET_CONTRACT_VERSION
from ml.nightly_gameplan import ASSUMED_ROUND_TRIP_COST, read_current_gameplan
from ml.stock_trader.contracts import PredictionSignal, STOCK_TRADER_SYMBOLS, utc
from ml.stock_trader.gameplan import read_gameplan_stock_activation_intent
from ml.stock_trader.independent_runtime import LEDGER_RELATIVE_PATH, _has_inventory, run_independent_stock_trader_once
from ml.stock_trader.independent_signals import _validated_independent_forecasts
from ml.stock_trader.model import enrichment_signal_readiness, load_current_enrichment_model
from ml.stock_trader.session import stock_execution_window
from ml.stock_trader.market_features import read_frozen_market_feature_values
from ml.stock_trader.sizing_policy import LEARNED_SIZING_POLICY, FIXED_SIZING_POLICY, GAMEPLAN_SIZING_POLICY, validate_sizing_policy
from ml.stock_trader.publication import _write_json_atomic


def _independent_forecast_preflight(root: Path, *, action_date) -> dict:
    """Separate forecast qualification from the presence of a bullish opportunity."""
    from ml.stock_trader.independent_signals import verified_promoted_model_groups
    try:
        publication = read_current_gameplan(root)
        config = publication.manifest.get("configuration", {})
        if (config.get("target_contract_version") != STOCK_TARGET_CONTRACT_VERSION
                or config.get("action_date") != action_date.isoformat()
                or publication.receipt.get("action_date") != action_date.isoformat()
                or tuple(config.get("symbols", ())) != tuple(STOCK_TRADER_SYMBOLS)):
            raise ValueError("Current stock publication differs from this action date, target contract or universe")
        frame = _validated_independent_forecasts(pd.read_parquet(publication.run_directory / "forecasts.parquet"),
                                                 action_date=action_date.isoformat(), symbols=tuple(STOCK_TRADER_SYMBOLS))
        if not frame.target_price_source_contract.eq(config.get("target_price_source_contract")).all():
            raise ValueError("Stock forecast source differs from its declared publication")
        groups = verified_promoted_model_groups(publication)
        execution = frame.loc[frame.target_role.eq("EXECUTION")]
        checks, bullish, ready_windows = {}, [], []
        for row in execution.to_dict("records"):
            key = f"{row['symbol']}/{row['route']}"
            ready = row["model_status"] == "PROMOTED" and row["model_group"] in groups
            checks[key] = {"status": "READY" if ready else "NOT_READY",
                           "reason": "QUALIFIED_STOCK_FORECAST" if ready else "STOCK_FORECAST_NOT_QUALIFIED"}
            if ready:
                ready_windows.append(key)
                if row["direction"] == "BULLISH":
                    bullish.append(key)
        reason = ("QUALIFIED_STOCK_FORECASTS_WITH_BULLISH_ENTRY_SIGNALS" if bullish
                  else "QUALIFIED_STOCK_FORECASTS_NO_BULLISH_ENTRY_SIGNAL") if ready_windows else "NO_QUALIFIED_STOCK_FORECAST_WINDOWS"
        return {"status": "READY" if ready_windows else "NOT_READY", "reason": reason,
                "all_execution_windows_qualified": len(ready_windows) == len(execution),
                "qualified_forecast_windows": ready_windows, "bullish_entry_windows": bullish,
                "execution_window_count": len(execution), "checks": checks,
                "sizing_policy": FIXED_SIZING_POLICY, "run_path": str(publication.run_directory)}
    except (OSError, ValueError, TypeError, KeyError, RuntimeError) as exc:
        return {"status": "NOT_READY", "reason": "STOCK_FORECAST_PREFLIGHT_UNAVAILABLE",
                "error": f"{type(exc).__name__}: {exc}"}


def _independent_enrichment_preflight(root: Path, *, action_date) -> dict:
    """Verify fitted scope before an empty-inventory worker commits to its day.

The model loader verifies the actual artifact, fingerprint, manifest, and
receipt. Empty independent qualification is a definitive rejection for the
existing hourly v1 model. Each exact execution window is checked independently;
an unqualified horizon cannot block another horizon with qualified evidence.
"""
    try:
        model = load_current_enrichment_model(root)
    except (OSError, TypeError, ValueError, RuntimeError) as exc:
        return {"status": "NOT_READY", "reason": "ENRICHMENT_ARTIFACT_UNAVAILABLE",
                "error": f"{type(exc).__name__}: {exc}"}
    if STOCK_TARGET_CONTRACT_VERSION not in model.qualified_target_contracts:
        return {"status": "NOT_READY", "reason": "ENRICHMENT_INDEPENDENT_TARGET_CONTRACT_NOT_QUALIFIED",
                "model_fingerprint": model.model_fingerprint,
                "supported_horizons": list(model.supported_horizons),
                "qualified_target_contracts": list(model.qualified_target_contracts)}
    try:
        publication = read_current_gameplan(root)
        config = publication.manifest.get("configuration", {})
        if (config.get("target_contract_version") != STOCK_TARGET_CONTRACT_VERSION
                or config.get("action_date") != action_date.isoformat()
                or publication.receipt.get("action_date") != action_date.isoformat()
                or tuple(config.get("symbols", ())) != tuple(STOCK_TRADER_SYMBOLS)
                or "forecasts.parquet" not in publication.manifest.get("output_files", {})):
            raise ValueError("Current independent stock publication does not match this action date and universe")
        forecasts = _validated_independent_forecasts(
            pd.read_parquet(publication.run_directory / "forecasts.parquet"),
            action_date=action_date.isoformat(), symbols=tuple(STOCK_TRADER_SYMBOLS),
        )
        from ml.stock_target_prices import CANONICAL_STOCK_PRICE_SOURCE
        if not forecasts["target_price_source_contract"].eq(
            config.get("target_price_source_contract", CANONICAL_STOCK_PRICE_SOURCE)
        ).all():
            raise ValueError("Forecast rows differ from their declared target price source")
        checks = {}
        qualified_entries = []
        for row in forecasts.loc[forecasts["target_role"].eq("EXECUTION")].to_dict("records"):
            signal = PredictionSignal(
                str(row["symbol"]), str(row["model_group"]), str(row["id"]),
                utc(row["decision_timestamp"]).isoformat(), utc(row["target_window_start"]).isoformat(),
                utc(row["target_window_end"]).isoformat(), utc(row["target_window_start"]).isoformat(),
                utc(row["frozen_at"]).isoformat(), float(row["calibrated_probability"]),
                ASSUMED_ROUND_TRIP_COST, {str(row["model_group"]): float(row["calibrated_probability"])},
                str(row["model_family"]), str(row.get("model_artifact") or ""), "preflight-only",
                target_definition_version=STOCK_TARGET_CONTRACT_VERSION,
                target_price_source_contract=str(row["target_price_source_contract"]),
                enrichment_feature_values=read_frozen_market_feature_values(row),
            )
            key = f"{row['symbol']}/{row['route']}"
            checks[key] = enrichment_signal_readiness(model, signal)
            if (checks[key]["status"] == "READY" and row["model_status"] == "PROMOTED"
                    and row["direction"] == "BULLISH"):
                qualified_entries.append(key)
        ready = bool(qualified_entries)
        return {"status": "READY" if ready else "NOT_READY",
                "reason": "QUALIFIED_INDEPENDENT_ENTRY_WINDOWS_AVAILABLE" if ready else "NO_QUALIFIED_ENTRY_WINDOWS",
                "model_fingerprint": model.model_fingerprint, "checks": checks,
                "qualified_entry_windows": qualified_entries}
    except (OSError, TypeError, ValueError, RuntimeError) as exc:
        return {"status": "NOT_READY", "reason": "INDEPENDENT_TARGET_PREFLIGHT_UNAVAILABLE",
                "error": f"{type(exc).__name__}: {exc}"}


def _next_supported_session_bounds(as_of: object) -> tuple[pd.Timestamp, pd.Timestamp]:
    """Find the next supported full session using local wall-clock dates.

    Half days remain unsupported by the existing extended-session contract.
    Construct each day's local clock independently so DST cannot shift 04:00.
    """
    timestamp = utc(as_of)
    first_date = timestamp.tz_convert("America/Los_Angeles").date()
    for offset in range(32):
        candidate_date = (pd.Timestamp(first_date) + pd.Timedelta(days=offset)).date()
        opening = pd.Timestamp(f"{candidate_date.isoformat()} 04:00", tz="America/Los_Angeles")
        closing = pd.Timestamp(f"{candidate_date.isoformat()} 17:00", tz="America/Los_Angeles")
        if timestamp < closing and stock_execution_window(opening).executable:
            return opening, closing
    raise ValueError("No supported stock session was found in the next 32 calendar days")


def run_independent_stock_session(
    root: Path, *, execute: bool = False, clock=utc, sleep=time.sleep,
    runner=run_independent_stock_trader_once, reporter=print,
    sizing_policy: str = LEARNED_SIZING_POLICY,
    wait_for_open: bool = False,
) -> dict:
    """Serve one exchange date, optionally waiting for its opening first.

    Manual callers may opt into ``wait_for_open`` before the next supported
    session. While waiting, the process holds its session lock and reads only
    local activation controls; publication checks and trading start at 04:00
    Pacific. The default retains the bounded same-day scheduled behavior.

    Entries start at HH:01, with 13:06 after the broker's POST transition.
    Boundary exits receive checks before those entries. Closing exits start at
    16:59 and are checked every five seconds until the broker session closes.
    All are limit orders and fills remain best effort.
    """
    root = Path(root).resolve()
    sizing_policy = validate_sizing_policy(sizing_policy)
    started = utc(clock())
    day = started.tz_convert("America/Los_Angeles").normalize()
    if wait_for_open:
        opening, closing = _next_supported_session_bounds(started)
        day = opening.normalize()
    else:
        opening = day + pd.Timedelta(hours=4)
        closing = day + pd.Timedelta(hours=17)
        if not stock_execution_window(opening).executable:
            return {"status": "NOOP_UNSUPPORTED_OR_CLOSED_SESSION", "orders_submitted": 0}
        if started >= closing:
            return {"status": "NOOP_SESSION_FINISHED", "orders_submitted": 0}
        if started < opening - pd.Timedelta(minutes=5):
            raise ValueError("Start the bounded worker at or after 03:55 Pacific on its action date")
    attempted = set()
    calls = submitted = 0
    failed_cycles = consecutive_failures = 0
    last_cycle = None
    status_path = root / "state/independent-stock-trader/session-status.json"

    def publish_status(status):
        payload = {"schema_version": "independent-stock-session-status-v1", "status": status,
            "pid": os.getpid(), "started_at": started.isoformat(), "heartbeat_at": utc(clock()).isoformat(),
            "closes_at": closing.isoformat(), "execute": execute, "sizing_policy": sizing_policy,
            "action_date": day.date().isoformat(), "wakes_at": opening.isoformat(),
            "wait_for_open": wait_for_open,
            "calls": calls, "orders_submitted": submitted, "failed_cycles": failed_cycles,
            "consecutive_failures": consecutive_failures, "last_cycle": last_cycle}
        _write_json_atomic(status_path, payload)

    with exclusive_runtime_lock(root / "locks/independent-stock-session.lock", process_name="independent-stock-session"):
        if not read_gameplan_stock_activation_intent(root).active:
            publish_status("STOPPED_TRADER_INACTIVE")
            return {"status": "SESSION_STOPPED_TRADER_INACTIVE", "calls": 0, "orders_submitted": 0}
        if wait_for_open and utc(clock()) < opening:
            reporter(json.dumps({"status": "SLEEPING_UNTIL_OPEN", "pid": os.getpid(),
                "status_path": str(status_path), "action_date": day.date().isoformat(),
                "wakes_at": opening.isoformat(), "sizing_policy": sizing_policy}, sort_keys=True))
            try:
                while (now := utc(clock())) < opening:
                    if not read_gameplan_stock_activation_intent(root).active:
                        publish_status("STOPPED_TRADER_INACTIVE")
                        return {"status": "SESSION_STOPPED_TRADER_INACTIVE", "calls": 0, "orders_submitted": 0}
                    publish_status("SLEEPING_UNTIL_OPEN")
                    sleep(min(30., max(0., (opening - now).total_seconds())))
            except KeyboardInterrupt:
                publish_status("STOPPED_INTERRUPTED")
                return {"status": "SESSION_STOPPED_INTERRUPTED", "calls": 0, "orders_submitted": 0}
            # The publication can change while the nightly workflow finishes.
            # Read it only after waking, without claiming a missed entry slot.
            if not read_gameplan_stock_activation_intent(root).active:
                publish_status("STOPPED_TRADER_INACTIVE")
                return {"status": "SESSION_STOPPED_TRADER_INACTIVE", "calls": 0, "orders_submitted": 0}
            if utc(clock()) >= closing:
                publish_status("FINISHED")
                return {"status": "SESSION_FINISHED", "calls": 0, "orders_submitted": 0,
                        "reason": "SESSION_ELAPSED_WHILE_SLEEPING"}
        if not _has_inventory(root / LEDGER_RELATIVE_PATH):
            forecast_sizing = sizing_policy in {FIXED_SIZING_POLICY, GAMEPLAN_SIZING_POLICY}
            preflight = _independent_forecast_preflight if forecast_sizing else _independent_enrichment_preflight
            readiness = preflight(root, action_date=day.date())
            if readiness["status"] != "READY":
                publish_status("BLOCKED_PREFLIGHT")
                return {"status": "NOOP_STOCK_FORECASTS_NOT_QUALIFIED" if forecast_sizing else "NOOP_ENRICHMENT_NOT_QUALIFIED", "calls": 0,
                        "orders_submitted": 0, "enrichment_readiness": readiness}
        publish_status("RUNNING")
        reporter(json.dumps({"status": "SESSION_STARTED", "pid": os.getpid(),
            "status_path": str(status_path), "closes_at": closing.isoformat(), "sizing_policy": sizing_policy}, sort_keys=True))
        while (now := utc(clock())) < closing:
            if not read_gameplan_stock_activation_intent(root).active:
                publish_status("STOPPED_TRADER_INACTIVE")
                return {"status": "SESSION_STOPPED_TRADER_INACTIVE", "calls": calls, "orders_submitted": submitted}
            local = now.tz_convert("America/Los_Angeles")
            if local.date() != day.date():
                raise RuntimeError("The worker clock changed exchange dates")
            entry_minute = 6 if local.hour == 13 else 1
            slot = local.floor("h").isoformat()
            due_entry = (4 <= local.hour < 17 and local.minute == entry_minute and slot not in attempted)
            has_inventory = _has_inventory(root / LEDGER_RELATIVE_PATH)
            # Inventory polls wait through the broker's closed transitions.
            # No capture here can resolve an earlier failure; health stays
            # degraded until a later executable cycle verifies broker state.
            if due_entry or (has_inventory and stock_execution_window(now).executable):
                if due_entry:
                    attempted.add(slot)
                sizing_options = {"sizing_policy": sizing_policy} if sizing_policy != LEARNED_SIZING_POLICY else {}
                try:
                    result = runner(root, execute=execute, entries=due_entry, runtime_clock=clock, session_managed=True, **sizing_options)
                except Exception as exc:
                    last_cycle = {"status": "UNHANDLED_WORKER_ERROR", "error_type": type(exc).__name__}
                    failed_cycles += 1
                    consecutive_failures += 1
                    publish_status("FAILED")
                    raise
                calls += 1
                submitted += result.submitted_orders
                last_cycle = result.to_dict()
                cycle_status = last_cycle.get("status", "")
                failed = (bool(last_cycle.get("error")) or bool(last_cycle.get("stopped_after_error"))
                          or "UNAVAILABLE" in cycle_status or cycle_status.startswith("SUBMISSION_STOPPED"))
                failed_cycles += int(failed)
                if failed:
                    consecutive_failures += 1
                elif (last_cycle.get("broker_state_capture") or {}).get("status") in {"CURRENT", "CURRENT_AFTER_RETRY"}:
                    # A no-signal cycle can return before reading the broker.
                    # It is normal, but cannot resolve a prior broker failure.
                    consecutive_failures = 0
                reporter(json.dumps(last_cycle, sort_keys=True))
            publish_status("DEGRADED" if consecutive_failures else "RUNNING")
            # No new entries during the extra ownership/reconciliation checks.
            # Frequent checks around boundaries let exits settle before HH:01.
            seconds = 5 if local.minute in {0, 1, 5, 6, 59} and has_inventory else 30
            remaining = max(0., (closing - utc(clock())).total_seconds())
            if remaining:
                sleep(min(seconds, remaining))
        publish_status("FINISHED_WITH_ERRORS" if consecutive_failures else "FINISHED")
    return {"status": "SESSION_FINISHED_WITH_ERRORS" if consecutive_failures else "SESSION_FINISHED",
            "calls": calls, "orders_submitted": submitted, "failed_cycles": failed_cycles,
            "consecutive_failures": consecutive_failures,
            "unclosed_allocations": _has_inventory(root / LEDGER_RELATIVE_PATH)}


__all__ = ["run_independent_stock_session"]
