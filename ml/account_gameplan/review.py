"""Native tail handoff for exact original-source accuracy; never infer peer outcomes."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import uuid

from filelock import FileLock
import pandas as pd

from ml.account_gameplan.accuracy import VERSION as ACCURACY_VERSION, aggregate_accuracy, read_source_accuracy
from ml.account_gameplan.cli import _save_accuracy
from ml.account_gameplan.config import verify_cutover, local_path
from ml.account_gameplan.planner import aware, encoded, sha
from ml.account_gameplan.preparation import _before, _clock, _config, _immutable, _invalidate_new_record, _local
from ml.account_gameplan.sources import _json, _safe_outputs, export_source_bundle, read_source_bundle
from ml.artifacts import file_checksum, verify_manifest
from ml.gameplan_actuals_review import previous_action_date

VERSION = "account-gameplan-review-v1"
_RESULT_FIELDS = {"producer_id", "bundle_path", "bundle_manifest_sha256", "symbols",
                  "result_path", "result_manifest_sha256"}


def _native_result(root, successor, successor_receipt, deadline, now):
    pointer_path = root / "ml/gameplan-actuals-review-latest/run.json"
    pointer_raw = pointer_path.read_bytes()
    pointer = _json(pointer_raw)
    current = pointer["current"]
    run = local_path(root, current["run_path"], "ml/gameplan-actuals-review-runs")
    receipt_raw = (run / "receipt.json").read_bytes()
    receipt = _json(receipt_raw)
    if current.get("receipt_sha256") != sha(receipt_raw):
        raise ValueError("Native actuals receipt differs from its current pin")
    manifest_hash = file_checksum(run / "manifest.json")
    manifest = _json((run / "manifest.json").read_bytes())
    _safe_outputs(run, manifest)
    verify_manifest(run)
    if not {"report.json", "forecast-results.parquet"}.issubset(manifest["output_files"]):
        raise ValueError("Native actuals report and outcomes must be manifest-bound")
    report = _json((run / "report.json").read_bytes())
    day = previous_action_date(successor_receipt["action_date"])
    successor_ref = successor.relative_to(root).as_posix()
    expected_input = successor_ref + "/receipt.json"
    inputs = [row for row in manifest.get("input_files", [])
              if str(row.get("path", "")).replace("\\", "/") == expected_input]
    if (pointer.get("schema_version") != "gameplan-actuals-review-v1"
            or current.get("action_date") != day or receipt.get("action_date") != day
            or receipt.get("schema_version") != "gameplan-actuals-review-v1" or receipt.get("status") != "COMPLETE"
            or receipt.get("run_path") != current["run_path"] or receipt.get("manifest_sha256") != manifest_hash
            or receipt.get("orders_placed") != 0 or receipt.get("broker_orders_enabled") is not False
            or report.get("schema_version") != "gameplan-actuals-review-v1" or report.get("status") != "COMPLETE"
            or report.get("action_date") != day or report.get("successor_action_date") != successor_receipt["action_date"]
            or report.get("successor_gameplan_run") != successor_ref
            or manifest.get("configuration", {}).get("successor_gameplan_run") != successor_ref
            or manifest.get("configuration", {}).get("action_date") != day
            or aware(report["deadline_at"]) != deadline or aware(report["reviewed_at"]) > now
            or report.get("orders_placed") != 0 or report.get("broker_orders_enabled") is not False
            or len(inputs) != 1 or inputs[0].get("status") != "present"
            or inputs[0].get("checksum_sha256") != file_checksum(successor / "receipt.json")):
        raise ValueError("Native actuals are not complete for the exact pinned successor and original deadline")
    return run, report, {"run_path": current["run_path"], "receipt_sha256": sha(receipt_raw),
                         "manifest_sha256": manifest_hash}, (pointer_path, pointer_raw)


def _read_registry(root, config, day, document):
    if (set(document) != {"schema_version", "action_date", "results"}
            or document["schema_version"] != VERSION or document["action_date"] != day
            or not isinstance(document["results"], list) or len(document["results"]) != 2
            or any(not isinstance(row, dict) or set(row) != _RESULT_FIELDS for row in document["results"])
            or {row["producer_id"] for row in document["results"]} != set(config.participants)):
        raise ValueError("Exact two-producer original accuracy registry is required")
    sources, results = [], []
    for row in sorted(document["results"], key=lambda item: item["producer_id"]):
        producer = row["producer_id"]
        if not set(row["symbols"]).issubset(config.participants[producer]):
            raise ValueError("Saved source universe is outside its reviewed producer partition")
        source = read_source_bundle(_local(root, row["bundle_path"]),
            expected_manifest_sha256=row["bundle_manifest_sha256"], expected_producer=producer,
            expected_symbols=row["symbols"], expected_action_date=day)
        result = read_source_accuracy(_local(root, row["result_path"]), bundle=source,
                                     expected_manifest_sha256=row["result_manifest_sha256"])
        sources.append(source)
        results.append(result)
    return sources, results


def register_inputs(root, *, action_date, results, expected_config):
    """Seal exact transported original results; no acquisition or activation."""
    root = Path(root).resolve()
    config = _config(root, expected_config)
    document = {"schema_version": VERSION, "action_date": action_date, "results": results}
    sources, outcomes = _read_registry(root, config, action_date, document)
    originals = {row["producer_id"]: row for row in results}
    records = [{"producer_id": source.metadata["producer_id"], "symbols": list(source.metadata["symbols"]),
        "bundle_path": source.path.relative_to(root).as_posix(), "bundle_manifest_sha256": source.manifest_sha256,
        "result_path": _local(root, originals[outcome["producer_id"]]["result_path"]).relative_to(root).as_posix(),
        "result_manifest_sha256": outcome["result_manifest_sha256"]} for source, outcome in zip(sources, outcomes)]
    document = {"schema_version": VERSION, "action_date": action_date, "results": records}
    if _config(root, expected_config) != config:
        raise ValueError("Account configuration advanced during accuracy input verification")
    path = root / f"state/account-gameplan/accuracy-inputs/{action_date}.json"
    created = not path.exists()
    _immutable(path, document)
    try:
        if _config(root, expected_config) != config:
            raise ValueError("Account configuration advanced during accuracy input registration")
    except Exception:
        if created:
            _invalidate_new_record(path, document, reason="ACCURACY_INPUT_REGISTRATION_BINDING_CHANGED")
        raise
    return document


def _existing_summary(root, selection, report):
    run = local_path(root, selection["current"]["run_path"], "ml/account-gameplan-accuracy-runs")
    manifest_raw = (run / "manifest.json").read_bytes()
    manifest = _json(manifest_raw)
    receipt = _json((run / "receipt.json").read_bytes())
    report_raw = (run / "report.json").read_bytes()
    if (sha(manifest_raw) != selection["current"]["manifest_sha256"]
            or manifest.get("schema_version") != ACCURACY_VERSION or receipt.get("schema_version") != ACCURACY_VERSION
            or manifest.get("action_date") != report["action_date"]
            or manifest.get("output_files") != {"report.json": sha(report_raw)}
            or manifest.get("sources") != report["sources"] or _json(report_raw) != report
            or receipt.get("manifest_sha256") != sha(manifest_raw) or receipt.get("status") != "COMPLETE"
            or receipt.get("action_date") != report["action_date"] or receipt.get("orders_placed") != 0
            or receipt.get("broker_orders_enabled") is not False):
        raise ValueError("Existing combined accuracy differs from the exact original source pins")
    return run


def _restore_pointer(path, previous, installed):
    """Undo only this invocation's exact pointer bytes, preserving later writers."""
    current = path.read_bytes() if path.exists() else None
    if current == previous:
        return
    if current != installed:
        raise RuntimeError("Accuracy pointer rollback refused independently changed bytes: " + str(path))
    if previous is None:
        path.unlink()
        return
    temporary = path.with_name(".restore-" + uuid.uuid4().hex + ".tmp")
    try:
        with temporary.open("xb") as stream:
            stream.write(previous)
        if path.read_bytes() != installed:
            raise RuntimeError("Accuracy pointer advanced before exact rollback")
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def run_account_review(root, *, successor_gameplan_run, deadline, expected_config, clock=None, sleeper=None):
    """Return explicit pending status instead of fabricating absent peer coverage.

    A pending retry is bound to the same successor and native original result.
    ``sleeper`` is accepted for native orchestration compatibility; this tail
    never waits for transport and never calls a broker, provider or model.
    """
    from ml.nightly_gameplan import read_gameplan_run
    root, clock = Path(root).resolve(), clock or _clock
    config = _config(root, expected_config)
    successor = _local(root, successor_gameplan_run)
    if successor.parent != root / "ml/nightly-gameplan-runs":
        raise ValueError("Successor is outside its native immutable run root")
    successor_run = read_gameplan_run(root, successor)
    successor_hash = file_checksum(successor / "receipt.json")
    successor_receipt = successor_run.receipt
    # Resolve the exchange-session Pacific opening through its actual timezone.
    expected_deadline = pd.Timestamp(successor_receipt["action_date"], tz="America/Los_Angeles") + pd.Timedelta(hours=4)
    deadline = aware(deadline)
    if deadline != expected_deadline:
        raise ValueError("Account review must retain the successor's original 04:00 deadline")
    now = _before(clock, deadline)
    lock = root / "state/account-gameplan/review.lock"
    lock.parent.mkdir(parents=True, exist_ok=True)
    with FileLock(str(lock), timeout=0):
        native, report, native_ref, native_pointer = _native_result(root, successor, successor_receipt, deadline, now)
        day = report["action_date"]
        attempt = {"schema_version": VERSION, "config_sha256": config.fingerprint,
            "producer_id": config.machine_id, "action_date": day, "successor_receipt_sha256": successor_hash,
            "successor_gameplan_run": successor.relative_to(root).as_posix(), "native_result": native_ref}
        _immutable(root / f"state/account-gameplan/accuracy-attempts/{successor_hash}.json", attempt)
        additional_pins = []
        def guard():
            _before(clock, deadline)
            if (_config(root, expected_config) != config or native_pointer[0].read_bytes() != native_pointer[1]
                    or file_checksum(successor / "receipt.json") != successor_hash
                    or file_checksum(native / "receipt.json") != native_ref["receipt_sha256"]
                    or file_checksum(native / "manifest.json") != native_ref["manifest_sha256"]
                    or any(file_checksum(path) != checksum for path, checksum in additional_pins)):
                raise ValueError("Local accuracy source or configuration advanced during review")
        ready = {**attempt, "orders_placed": 0, "broker_orders_enabled": False, "activation_changed": False}
        if not report.get("source_gameplan_run") or not report.get("source_trade_plan_path"):
            guard()
            unavailable = {**ready, "status": "UNAVAILABLE_LOCAL_SOURCE", "reason": "Native actuals have no saved original Gameplan and matching trade plan."}
            _immutable(root / f"state/account-gameplan/accuracy-pending/{day}/{native_ref['receipt_sha256']}.json", unavailable)
            guard()
            return unavailable
        original = local_path(root, report["source_gameplan_run"], "ml/nightly-gameplan-runs")
        trade = _local(root, report["source_trade_plan_path"])
        if trade.parent != root / "ml/gameplan-trade-plan-runs":
            raise ValueError("Original trade plan is outside its immutable native root")
        original_hash = file_checksum(original / "receipt.json")
        original_config = _json((original / "manifest.json").read_bytes())["configuration"]
        symbols = original_config["symbols"]
        if not set(symbols).issubset(config.participants[config.machine_id]):
            raise ValueError("Original native universe differs from reviewed producer ownership")
        destination = root / "ml/account-gameplan-source-runs" / original_hash
        if not destination.exists():
            export_source_bundle(root, gameplan_run=original, trade_plan_run=trade, destination=destination,
                                 producer_id=config.machine_id, expected_symbols=symbols)
        bundle = read_source_bundle(destination, expected_manifest_sha256=file_checksum(destination / "manifest.json"),
            expected_producer=config.machine_id, expected_symbols=symbols, expected_action_date=day)
        if (bundle.metadata["source_receipt_sha256"] != original_hash
                or bundle.metadata["trade_plan_receipt_sha256"] != file_checksum(trade / "receipt.json")):
            raise ValueError("Saved original bundle differs from native actuals source and frozen trade plan")
        additional_pins.extend([(original / "receipt.json", original_hash),
            (trade / "receipt.json", bundle.metadata["trade_plan_receipt_sha256"]),
            (bundle.path / "manifest.json", bundle.manifest_sha256)])
        read_source_accuracy(native, bundle=bundle, expected_manifest_sha256=native_ref["manifest_sha256"])
        guard()
        if file_checksum(original / "receipt.json") != original_hash:
            raise ValueError("Original native source changed during accuracy handoff")
        ready.update(status="SOURCE_READY", bundle_path=bundle.path.relative_to(root).as_posix(),
            bundle_manifest_sha256=bundle.manifest_sha256, source_receipt_sha256=original_hash, symbols=list(symbols))
        outgoing = root / f"state/account-gameplan/accuracy-outgoing/{day}/{native_ref['receipt_sha256']}.json"
        _immutable(outgoing, ready)
        guard()
        if config.role == "producer" or config.activation["status"] != "ACTIVE":
            return ready
        verify_cutover(root, config)
        registry_path = root / f"state/account-gameplan/accuracy-inputs/{day}.json"
        if not registry_path.exists():
            pending = {**ready, "status": "PENDING_PEER_RESULT", "reason": "Both exact original-source result pins have not arrived."}
            _immutable(root / f"state/account-gameplan/accuracy-pending/{day}/{native_ref['receipt_sha256']}.json", pending)
            guard()
            verify_cutover(root, config)
            return pending
        registry_raw = registry_path.read_bytes()
        sources, results = _read_registry(root, config, day, _json(registry_raw))
        local = next(item for item in results if item["producer_id"] == config.machine_id)
        if (local["result_manifest_sha256"] != native_ref["manifest_sha256"]
                or local["result_receipt_sha256"] != native_ref["receipt_sha256"]
                or local["source_bundle_manifest_sha256"] != bundle.manifest_sha256):
            raise ValueError("Accuracy registry substituted this successor's exact local native result")
        combined = aggregate_accuracy(sources, results)
        combined.update(config_sha256=config.fingerprint, successor_gameplan_run=attempt["successor_gameplan_run"],
            successor_receipt_sha256=successor_hash, local_native_result=native_ref,
            broker_orders_enabled=False, execution_authority="INFORMATIONAL_ACCOUNT_ACCURACY")
        dated = root / f"ml/account-gameplan-accuracy-by-date/{day}/run.json"
        latest = root / "ml/account-gameplan-accuracy-latest/run.json"
        previous_latest = latest.read_bytes() if latest.exists() else None
        previous_dated = dated.read_bytes() if dated.exists() else None
        if dated.exists():
            selection = _json(dated.read_bytes())
            if (selection.get("schema_version") != VERSION or selection.get("config_sha256") != config.fingerprint
                    or selection.get("action_date") != day):
                raise ValueError("Existing combined accuracy selection belongs to another binding")
            _existing_summary(root, selection, combined)
        else:
            _before(clock, deadline)
            output = root / "ml/account-gameplan-accuracy-runs" / (now.strftime("%Y%m%dT%H%M%S.%fZ") + "-" + uuid.uuid4().hex[:8])
            summary = _save_accuracy(output, combined)
            selection = {"schema_version": VERSION, "action_date": day, "config_sha256": config.fingerprint,
                "current": {"run_path": output.relative_to(root).as_posix(), "manifest_sha256": summary["manifest_sha256"]}}
            _existing_summary(root, selection, combined)
        # Re-read all explicit evidence before advancing the account reader.
        _read_registry(root, config, day, _json(registry_raw))
        _before(clock, deadline)
        if (_config(root, expected_config) != config or registry_path.read_bytes() != registry_raw
                or native_pointer[0].read_bytes() != native_pointer[1]
                or file_checksum(successor / "receipt.json") != successor_hash
                or (latest.read_bytes() if latest.exists() else None) != previous_latest):
            raise ValueError("Account accuracy sources or selection advanced during review")
        verify_cutover(root, config)
        latest.parent.mkdir(parents=True, exist_ok=True)
        temporary = latest.with_name(".run-" + uuid.uuid4().hex + ".tmp")
        installed = encoded(selection)
        try:
            _immutable(dated, selection)
            temporary.write_bytes(installed)
            guard()
            verify_cutover(root, config)
            if (registry_path.read_bytes() != registry_raw
                    or (latest.read_bytes() if latest.exists() else None) != previous_latest):
                raise ValueError("Account accuracy selection advanced before pointer publication")
            os.replace(temporary, latest)
            guard()
            verify_cutover(root, config)
            if (registry_path.read_bytes() != registry_raw or latest.read_bytes() != installed
                    or dated.read_bytes() != installed):
                raise ValueError("Account accuracy sources or pointers changed during publication")
        except BaseException as exc:
            failures = []
            for path, previous in ((latest, previous_latest), (dated, previous_dated)):
                try:
                    _restore_pointer(path, previous, installed)
                except (OSError, RuntimeError) as failure:
                    failures.append(str(failure))
            if failures:
                raise RuntimeError("Accuracy publication failed; pointer rollback incomplete: " + "; ".join(failures)) from exc
            raise
        finally:
            temporary.unlink(missing_ok=True)
        return {**ready, "status": "COMPLETE", "current": selection["current"], "totals": combined["totals"],
                "promoted_totals": combined["promoted_totals"]}


def main(argv=None):
    from datafetching.parquet_store import resolve_datastore_dir
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--datastore", type=Path)
    parser.add_argument("--datastore-target")
    for name in ("gameplan-run", "deadline", "expected-config"):
        parser.add_argument("--" + name, required=True)
    args = parser.parse_args(argv)
    root = resolve_datastore_dir(root_dir=args.datastore, target=args.datastore_target)
    result = run_account_review(root, successor_gameplan_run=args.gameplan_run, deadline=args.deadline,
                                expected_config=args.expected_config)
    print(json.dumps(result, sort_keys=True, allow_nan=False))
    return 0 if result["status"] in {"COMPLETE", "SOURCE_READY"} else 2


if __name__ == "__main__":
    raise SystemExit(main())
