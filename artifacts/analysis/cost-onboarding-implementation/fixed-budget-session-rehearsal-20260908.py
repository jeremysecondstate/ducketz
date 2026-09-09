"""Rehearse a verified frozen stock session using simulated account evidence only.

Run from the repository with its venv Python. No network is permitted. All native
controls, decisions, locks, ledgers and entry slots are created under a new,
guarded system temporary directory; the source datastore is read only.
"""
from __future__ import annotations

import argparse
from collections import Counter
from contextlib import ExitStack
from dataclasses import asdict
import hashlib
import json
from pathlib import Path
import shutil
import socket
import sys
import tempfile
from unittest.mock import patch


REPOSITORY = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPOSITORY))

import pandas as pd
import requests

from ml.artifacts import file_checksum
from ml.nightly_gameplan import read_current_gameplan, read_gameplan_run
from ml.stock_trader import independent_runtime as runtime
from ml.stock_trader import independent_session as worker
from ml.stock_trader.contracts import PortfolioState, QuoteState, STOCK_TRADER_SYMBOLS
from ml.stock_trader.control import write_activation_intent
from ml.stock_trader.gameplan import write_gameplan_stock_activation_intent
from ml.stock_trader.horizon_ledger import HorizonLedger


SIMULATED_ACCOUNT = hashlib.sha256(b"SIMULATED-account-fixed-session-rehearsal").hexdigest()
SIMULATED_CONTEXT = hashlib.sha256(b"SIMULATED-broker-context-fixed-session-rehearsal").hexdigest()
MANUAL_SHARES = {"AAPL": 23., "COST": 17.}


class SimulatedClock:
    def __init__(self):
        self.now = pd.Timestamp("2026-09-08T10:55:00Z")
        self.sleep_count = 0

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        assert 0 < seconds <= 30
        self.now += pd.Timedelta(seconds=seconds)
        self.sleep_count += 1


class GuardedSimulatedBroker:
    def __init__(self):
        self.calls = Counter()

    def stable_account_fingerprint(self):
        self.calls["stable_account_fingerprint"] += 1
        return SIMULATED_ACCOUNT

    def verify_read_snapshot(self, expected):
        self.calls["verify_read_snapshot"] += 1
        assert expected == SIMULATED_CONTEXT

    def prepare_order_submission(self):
        self.calls["unexpected_order_preparation"] += 1
        raise AssertionError("The unchanged native forecasts must select no orders")

    def submit_prepared_order(self, *args, **kwargs):
        self.calls["forbidden_order_submission"] += 1
        raise AssertionError("No order submission is permitted in this rehearsal")

    def cancel_prepared_order(self, *args, **kwargs):
        self.calls["forbidden_order_cancellation"] += 1
        raise AssertionError("No cancellation is permitted in this rehearsal")

    def get_orders(self, *args, **kwargs):
        self.calls["unexpected_order_history"] += 1
        raise AssertionError("Empty native horizon inventory must need no order history")


def protected_inventory(root, run):
    candidates = [root / "ml/nightly-gameplan-latest/run.json",
        root / "controls/stock-trader/operator-intent.txt",
        root / "controls/gameplan-stock-trader/operator-intent.txt"]
    candidates.extend(path for path in run.rglob("*") if path.is_file())
    slots = root / "state/independent-stock-trader/entry-slots"
    candidates.extend(path for path in slots.glob("*") if path.is_file())
    return {path.relative_to(root).as_posix(): file_checksum(path) if path.is_file() else None
            for path in candidates}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-root", type=Path, default=Path("C:/DATASTORE"))
    parser.add_argument("--run-id", default="20260908T093314.374067Z")
    parser.add_argument("--output", type=Path, default=Path(__file__).with_suffix(".json"))
    args = parser.parse_args()
    source = args.source_root.resolve()
    run = source / "ml/nightly-gameplan-runs" / args.run_id
    assert run.resolve().parent == (source / "ml/nightly-gameplan-runs").resolve()
    output = args.output.resolve()
    assert not output.is_relative_to(source), "Evidence output cannot modify the source datastore"
    assert not output.exists(), "Use a new evidence filename for each rehearsal"
    publication = read_gameplan_run(source, run)
    before = protected_inventory(source, run)
    code_hashes = {name: file_checksum(REPOSITORY / name) for name in (
        "ml/stock_trader/independent_session.py", "ml/stock_trader/independent_runtime.py",
        "ml/stock_trader/independent_signals.py", "ml/stock_trader/fixed_horizon_budget.py",
        "ml/stock_trader/fixed_horizon_engine.py")}
    clock, broker, forbidden = SimulatedClock(), GuardedSimulatedBroker(), Counter()
    batches, decision_evidence = [], []

    def forbid(name):
        def rejected(*args, **kwargs):
            forbidden[name] += 1
            raise AssertionError(f"Forbidden rehearsal operation: {name}")
        return rejected

    def capture(session, *, observed_at, parallel):
        assert session is broker and pd.Timestamp(observed_at) == clock()
        broker.calls["simulated_portfolio_capture"] += 1
        held = {symbol: MANUAL_SHARES.get(symbol, 0.) for symbol in STOCK_TRADER_SYMBOLS}
        return PortfolioState(clock().isoformat(), 100000., 96000., 4000., 0., held,
            {symbol: shares * 100. for symbol, shares in held.items()}, {}, {}, 0,
            {symbol: QuoteState(symbol, 100., 100., 100., 100., 1000., clock().isoformat()) for symbol in STOCK_TRADER_SYMBOLS},
            "SIMULATED-portfolio-" + clock().isoformat(), SIMULATED_CONTEXT)

    with tempfile.TemporaryDirectory(prefix="fixed-budget-session-rehearsal-") as temporary:
        isolated = Path(temporary).resolve()
        # Verify the recursively cleaned temporary target remains in its intended
        # system temp parent and cannot be the production datastore.
        assert isolated.parent == Path(tempfile.gettempdir()).resolve()
        assert isolated.name.startswith("fixed-budget-session-rehearsal-")
        assert not isolated.is_relative_to(source) and not source.is_relative_to(isolated)
        copied_run = isolated / run.relative_to(source)
        shutil.copytree(run, copied_run)
        pointer = isolated / "ml/nightly-gameplan-latest/run.json"
        pointer.parent.mkdir(parents=True)
        current_pointer = source / "ml/nightly-gameplan-latest/run.json"
        current = json.loads(current_pointer.read_text(encoding="utf-8"))
        if current.get("current", {}).get("run_path") == run.relative_to(source).as_posix():
            shutil.copyfile(current_pointer, pointer)
            pointer_origin = "byte-identical current native pointer"
        else:
            pointer.write_text(json.dumps(publication.pointer, indent=2), encoding="utf-8")
            pointer_origin = "native saved-publication reader pointer for the exact immutable run"
        copied = read_current_gameplan(isolated)
        assert copied.manifest == publication.manifest and copied.receipt == publication.receipt
        copied_hashes = {path.relative_to(copied_run).as_posix(): file_checksum(path)
                         for path in copied_run.rglob("*") if path.is_file()}
        assert copied_hashes == {path.relative_to(run).as_posix(): file_checksum(path)
                                 for path in run.rglob("*") if path.is_file()}
        write_activation_intent(isolated, active=True)
        write_gameplan_stock_activation_intent(isolated, active=True)

        def runner(root, **kwargs):
            assert Path(root).resolve() == isolated
            assert kwargs["execute"] is True and kwargs["session_managed"] is True
            assert kwargs["entries"] is True and kwargs["sizing_policy"] == "fixed-horizon-budget-v1"
            result = runtime.run_independent_stock_trader_once(root, session=broker, **kwargs)
            payload = json.loads((result.run_directory / "decisions.json").read_text(encoding="utf-8"))
            decisions = payload["decisions"]
            assert result.submitted_orders == result.selected_orders == 0
            assert result.error is None
            assert result.status in {"NO_ORDERS_SUBMITTED", "NO_DIRECTIONAL_STOCK_ENTRY_SIGNAL"}
            assert all(row["quantity"] == 0 and row["order_payload"] is None for row in decisions)
            assert all(row["action"] == "NO_TRADE" for row in decisions)
            assert all(row["expected_net_return"] is None and row["trade_probability"] is None for row in decisions)
            batches.append({"simulated_at_utc": clock().isoformat(),
                "simulated_at_pacific": clock().tz_convert("America/Los_Angeles").isoformat(),
                "status": result.status, "native_directional_signal_count": len(decisions),
                "decision_reason_counts": dict(Counter(row["decision_reason_code"] for row in decisions)),
                "selected_orders": result.selected_orders, "submitted_orders": result.submitted_orders,
                "decision_evidence_sha256": file_checksum(result.run_directory / "decisions.json")})
            decision_evidence.extend({"simulated_at_utc": clock().isoformat(), "symbol": row["symbol"],
                "horizon": row["prediction"]["primary_horizon"], "prediction_id": row["prediction"]["prediction_id"],
                "forecast_probability": row["prediction"]["calibrated_probability"],
                "target_window_start": row["prediction"]["target_window_start"],
                "target_window_end": row["prediction"]["target_window_end"],
                "reason": row["decision_reason_code"], "policy_fingerprint": row["policy_fingerprint"]}
                for row in decisions)
            return result

        with ExitStack() as stack:
            stack.enter_context(patch.object(socket.socket, "connect", forbid("network_connect")))
            stack.enter_context(patch.object(requests.sessions.Session, "request", forbid("http_request")))
            stack.enter_context(patch.object(runtime, "SchwabSession", forbid("real_broker_construction")))
            stack.enter_context(patch.object(runtime, "capture_portfolio_state", capture))
            stack.enter_context(patch.object(runtime, "load_current_enrichment_model", forbid("runtime_learner_load")))
            stack.enter_context(patch.object(worker, "load_current_enrichment_model", forbid("session_learner_load")))
            preflight = worker._independent_forecast_preflight(isolated, action_date=pd.Timestamp("2026-09-08").date())
            assert preflight["status"] == "READY", preflight
            assert preflight["all_execution_windows_qualified"] is True
            assert preflight["execution_window_count"] == 133 and preflight["bullish_entry_windows"] == []
            result = worker.run_independent_stock_session(isolated, execute=True, clock=clock, sleep=clock.sleep,
                runner=runner, reporter=lambda message: None, sizing_policy="fixed-horizon-budget-v1")
        assert result["status"] == "SESSION_FINISHED" and result["calls"] == 13
        assert result["orders_submitted"] == 0 and result["unclosed_allocations"] is False
        assert len(batches) == 13 and not forbidden
        ledger_path = isolated / runtime.LEDGER_RELATIVE_PATH
        ledger_snapshot = asdict(HorizonLedger(ledger_path, SIMULATED_ACCOUNT).snapshot()) if ledger_path.exists() else {
            "allocations": (), "reservations": (), "persistent_blocks": ()}
        assert not ledger_snapshot["allocations"] and not ledger_snapshot["reservations"]
        assert all(not count for name, count in broker.calls.items() if name.startswith(("forbidden", "unexpected")))
        slots = sorted(path.name for path in (isolated / "state/independent-stock-trader/entry-slots").glob("*.json"))
        after = protected_inventory(source, run)
        assert before == after, "Production publication, controls or entry slots changed during rehearsal"
        assert code_hashes == {name: file_checksum(REPOSITORY / name) for name in code_hashes}, "Execution code changed during rehearsal"
        evidence = {
            "schema_version": "fixed-budget-native-session-rehearsal-v1", "status": "PASS",
            "simulation_only": True, "native_publication_modified": False, "source_run": str(run),
            "source_manifest_sha256": file_checksum(run / "manifest.json"),
            "source_receipt_sha256": file_checksum(run / "receipt.json"),
            "source_forecasts_sha256": file_checksum(run / "forecasts.parquet"),
            "source_model_reports_sha256": file_checksum(run / "model-reports.json"),
            "source_copy_file_count": len(copied_hashes), "source_copy_bytes": sum(path.stat().st_size for path in run.rglob("*") if path.is_file()),
            "pointer_origin": pointer_origin, "source_copy_all_files_byte_identical": True,
            "sizing_policy": "fixed-horizon-budget-v1", "native_session_result": result,
            "preflight": {key: preflight[key] for key in ("status", "reason", "all_execution_windows_qualified", "execution_window_count", "bullish_entry_windows")},
            "simulated_account": {"account_equity": 100000., "available_cash": 96000., "gross_exposure": 4000.,
                "quote_bid_and_ask": 100., "manual_shares_not_owned_by_horizons": MANUAL_SHARES,
                "all_evidence_observed_at_simulated_runtime_clock": True},
            "simulated_execution_branch": True, "real_orders": 0, "selected_orders": 0,
            "broker_calls": dict(broker.calls), "forbidden_operations": dict(forbidden), "learner_loads": 0,
            "production_protected_files_unchanged": True, "production_entry_slots_created": 0,
            "isolated_entry_slots": slots, "isolated_final_ledger_snapshot": ledger_snapshot,
            "manual_shares_adopted": 0, "native_entry_attempts": len(batches),
            "batches": batches, "decisions": decision_evidence,
            "rehearsal_script_sha256": file_checksum(Path(__file__)),
            "code_sha256": code_hashes,
            "limitations": "Simulated quotes and account evidence; this is not a broker connectivity, fill, profitability, or live activation test.",
        }
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(evidence, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps({"status": evidence["status"], "evidence": str(output), "native_entry_attempts": len(batches),
                      "selected_orders": 0, "real_orders": 0, "learner_loads": 0, "manual_shares_adopted": 0}))


if __name__ == "__main__":
    main()
