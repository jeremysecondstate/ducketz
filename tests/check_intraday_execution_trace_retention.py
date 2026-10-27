"""Offline exact-source timing checks without importing application or broker code."""
from __future__ import annotations

import argparse
import ast
import hashlib
import json
import math
from pathlib import Path
import socket
import sqlite3
import sys
from types import SimpleNamespace


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path,
        default=Path(__file__).resolve().parents[1] / "ml/intraday/account_execution.py")
    args = parser.parse_args()
    source = args.source.resolve()
    raw = source.read_bytes()
    tree = ast.parse(raw)
    network = []

    def denied(*args, **kwargs):
        network.append(True)
        raise AssertionError("Timing regression must remain offline")

    socket.socket.connect = socket.socket.connect_ex = socket.create_connection = denied
    socket.getaddrinfo = denied
    clock = SimpleNamespace(value=0.)
    class ContractError(Exception): pass
    class AuthorityError(Exception): pass
    env = dict(math=math, time=SimpleNamespace(monotonic=lambda: clock.value),
        sqlite3=sqlite3, ContractError=ContractError, AuthorityError=AuthorityError,
        _SAFE_SUBMISSION_REASONS=frozenset(), _SQLITE_ERROR_CONSTANTS={},
        _AUTHORITY_RENEWAL_FAILURE=object(), lease_acquisition_failure_evidence=lambda error: None)
    selected = [node for node in tree.body if isinstance(node, (ast.ClassDef, ast.FunctionDef))
        and node.name in {"_ExecutionTrace", "_ExecutionFailure", "execution_failure_evidence", "_submission_failure_evidence"}]
    assert len(selected) == 4
    exec(compile(ast.Module(body=selected, type_ignores=[]), str(source), "exec"), env)
    Trace = env["_ExecutionTrace"]

    def fresh(at=0.):
        clock.value = at
        return Trace()

    def early_98_seconds_survive_tail_eviction():
        trace = fresh()
        clock.value = 98.
        trace.advance("newer_forecast_scan")
        for _ in range(40):
            clock.value += .5
            trace.advance("newer_forecast_scan")
        saved = trace.snapshot()
        assert len(saved["execution_phases"]) == 32
        assert saved["execution_phase_summary"]["dropped_tail_count"] == 9
        assert saved["execution_phase_summary"]["completed_stages"]["account_entry"] == dict(count=1, elapsed_count=1, total_seconds=98., maximum_seconds=98.)
        assert saved["execution_phase_summary"]["completed_stages"]["newer_forecast_scan"] == dict(count=40, elapsed_count=40, total_seconds=20., maximum_seconds=.5)
        assert sum(value["total_seconds"] for value in saved["execution_phase_summary"]["completed_stages"].values()) == saved["execution_elapsed_seconds"] == 118.

    def repeated_stage_count_total_maximum():
        trace = fresh()
        trace.advance("sizing")
        for delay in (1., 2., 3., 4.):
            clock.value += delay
            trace.advance("sizing")
        assert trace.snapshot()["execution_phase_summary"]["completed_stages"]["sizing"] == dict(count=4, elapsed_count=4, total_seconds=10., maximum_seconds=4.)

    def completed_exclusive_intervals_exclude_nested_and_current():
        trace = fresh()
        clock.value = 2.
        trace.advance("sizing")
        clock.value = 5.
        trace.lease_acquisition = dict(schema_version="fixture", attempt=1, stage="complete", elapsed_seconds=100., stage_elapsed_seconds=100., diagnostics_unavailable=False, phases=[])
        env["receipt_recovery_details_evidence"] = lambda evidence: None
        saved = trace.snapshot()
        assert sum(value["total_seconds"] for value in saved["execution_phase_summary"]["completed_stages"].values()) == 2.
        assert saved["execution_stage_elapsed_seconds"] == 3.
        assert saved["execution_elapsed_seconds"] == 5.
        assert saved["lease_acquisition"]["elapsed_seconds"] == 100.

    def snapshots_are_detached_and_do_not_accumulate():
        trace = fresh()
        clock.value = 1.
        trace.advance("sizing")
        saved = trace.snapshot()
        assert saved == trace.snapshot()
        saved["execution_phase_summary"]["completed_stages"]["account_entry"]["total_seconds"] = 999.
        saved["execution_phases"][0]["elapsed_seconds"] = 999.
        assert trace.snapshot()["execution_phase_summary"]["completed_stages"]["account_entry"]["total_seconds"] == 1.
        assert trace.snapshot()["execution_phases"][0]["elapsed_seconds"] == 1.

    def unique_unknown_names_are_bounded_and_sanitized():
        trace = fresh()
        for index in range(1000):
            clock.value += 1.
            trace.advance("private-account-value-" + str(index))
        saved = trace.snapshot()
        assert set(saved["execution_phase_summary"]["completed_stages"]) == {"account_entry", "other"}
        assert saved["execution_phase_summary"]["completed_stages"]["other"]["count"] == 999
        assert saved["execution_phase_summary"]["stages_overflow"] is True
        assert len(saved["execution_phases"]) == 32 and saved["execution_phase_summary"]["dropped_tail_count"] == 968
        assert "private-account" not in json.dumps(saved["execution_phase_summary"]["completed_stages"])
        assert len(saved["execution_phase_summary"]["completed_stages"]) <= len(Trace._PHASE_TOTAL_STAGES) + 1

    def invalid_clock_retains_legacy_none_and_marks_unavailable():
        trace = fresh(float("nan"))
        trace.advance("sizing")
        saved = trace.snapshot()
        assert saved["execution_elapsed_seconds"] is None
        assert saved["execution_phases"][0]["elapsed_seconds"] is None
        assert saved["execution_diagnostics_unavailable"] is True
        assert saved["execution_phase_summary"]["completed_stages"]["account_entry"] == dict(count=1, elapsed_count=0, total_seconds=0., maximum_seconds=0.)

    def backwards_clock_retains_existing_clamp_behavior():
        trace = fresh(5.)
        clock.value = 4.
        trace.advance("sizing")
        saved = trace.snapshot()
        assert saved["execution_elapsed_seconds"] == 0.
        assert saved["execution_phases"][0]["elapsed_seconds"] == 0.
        assert saved["execution_phase_summary"]["completed_stages"]["account_entry"]["total_seconds"] == 0.
        assert "execution_diagnostics_unavailable" not in saved

    def clock_exception_does_not_change_trace_transition():
        trace = fresh()
        def unavailable(): raise RuntimeError("synthetic diagnostic clock")
        prior = env["time"].monotonic
        env["time"].monotonic = unavailable
        try:
            trace.advance("sizing")
            saved = trace.snapshot()
        finally:
            env["time"].monotonic = prior
        assert saved["execution_stage"] == "sizing" and saved["execution_diagnostics_unavailable"] is True
        assert saved["execution_phase_summary"]["completed_stages"]["account_entry"]["count"] == 1

    def exception_snapshot_retains_cause_and_early_totals():
        trace = fresh()
        clock.value = 98.
        trace.advance("newer_forecast_scan")
        try:
            raise RuntimeError("private-provider-content")
        except RuntimeError as error:
            trace.remember(error)
            saved = env["execution_failure_evidence"](error)
            assert saved["execution_stage"] == "newer_forecast_scan"
            assert saved["root_cause"] == {"error_type": "RuntimeError"}
            assert saved["execution_phase_summary"]["completed_stages"]["account_entry"]["total_seconds"] == 98.
            assert "private-provider-content" not in json.dumps(saved)

    def new_scan_marker_precedes_same_execution_call():
        owner = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == "IntradayAccountExecutor")
        step = next(node for node in owner.body if isinstance(node, ast.FunctionDef) and node.name == "step")
        body = next(node for node in step.body if isinstance(node, ast.With)).body
        call = next(index for index,node in enumerate(body) if isinstance(node, ast.Assign)
            and isinstance(node.value, ast.Call) and isinstance(node.value.func, ast.Attribute) and node.value.func.attr == "_newer_forecasts")
        marker = body[call-1]
        assert isinstance(marker, ast.Expr) and isinstance(marker.value, ast.Call)
        assert marker.value.func.attr == "_execution_stage" and marker.value.args[0].value == "newer_forecast_scan"
        assert body[call-2].value.func.attr == "_capture"

    def output_remains_json_primitives_with_fixed_summary_keys():
        trace = fresh()
        clock.value = .125
        trace.advance("newer_forecast_scan")
        saved = trace.snapshot()
        encoded = json.dumps(saved, allow_nan=False)
        assert json.loads(encoded) == saved
        assert saved["execution_phase_summary"]["stages_overflow"] is False
        for stage, summary in saved["execution_phase_summary"]["completed_stages"].items():
            assert stage in Trace._PHASE_TOTAL_STAGES
            assert set(summary) == {"count", "elapsed_count", "total_seconds", "maximum_seconds"}

    def marker_only_targets_existing_optional_trace():
        owner = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == "IntradayAccountExecutor")
        method = next(node for node in owner.body if isinstance(node, ast.FunctionDef) and node.name == "_execution_stage")
        namespace = {}
        exec(compile(ast.Module(body=[method], type_ignores=[]), str(source), "exec"), namespace)
        state = SimpleNamespace(_execution_trace=None)
        assert namespace["_execution_stage"](state, "newer_forecast_scan") is None
        assert vars(state) == {"_execution_trace": None}
        calls = []
        state._execution_trace = SimpleNamespace(advance=calls.append)
        assert namespace["_execution_stage"](state, "newer_forecast_scan") is None
        assert calls == ["newer_forecast_scan"]
        assert set(vars(state)) == {"_execution_trace"}

    tests = [value for name,value in locals().copy().items() if callable(value) and name in {
        "early_98_seconds_survive_tail_eviction", "repeated_stage_count_total_maximum",
        "completed_exclusive_intervals_exclude_nested_and_current", "snapshots_are_detached_and_do_not_accumulate",
        "unique_unknown_names_are_bounded_and_sanitized", "invalid_clock_retains_legacy_none_and_marks_unavailable",
        "backwards_clock_retains_existing_clamp_behavior", "clock_exception_does_not_change_trace_transition",
        "exception_snapshot_retains_cause_and_early_totals", "new_scan_marker_precedes_same_execution_call",
        "output_remains_json_primitives_with_fixed_summary_keys", "marker_only_targets_existing_optional_trace"}]
    results = []
    for test in tests:
        try:
            test()
            results.append(dict(test=test.__name__, passed=True))
        except Exception as error:
            results.append(dict(test=test.__name__, passed=False, error_type=type(error).__name__))
    assert network == []
    assert not any(name.startswith(("ml.", "app.", "datafetching.")) for name in sys.modules)
    print(json.dumps(dict(source=str(source), sha256=hashlib.sha256(raw).hexdigest(),
        passed=sum(item["passed"] for item in results), total=len(results), results=results,
        network_attempts=len(network), application_imports=0), sort_keys=True))
    return 0 if all(item["passed"] for item in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
