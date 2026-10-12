"""Explicit offline account-plan operations. No runtime activation or acquisition."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import sys

from ml.account_gameplan.accuracy import VERSION as ACCURACY_VERSION, aggregate_accuracy, read_source_accuracy
from ml.account_gameplan.planner import encoded, publish_account_plan, read_account_plan, sha
from ml.account_gameplan.sources import _json, export_source_bundle, read_source_bundle


def _document(path):
    return _json(Path(path).read_bytes())


def _records(path, key, fields):
    document = _document(path)
    records = document.get(key)
    if (set(document) != {key} or not isinstance(records, list) or len(records) != 2
            or any(not isinstance(record, dict) or set(record) != set(fields) for record in records)):
        raise ValueError(f"{key} registry must contain exactly two explicit complete records")
    identities = [record["producer_id"] for record in records]
    if any(not isinstance(identity, str) for identity in identities) or len(set(identities)) != 2:
        raise ValueError(f"{key} registry repeats or omits a producer")
    return records


def _sources(path):
    records = _records(path, "sources", ("producer_id", "bundle_path", "manifest_sha256", "symbols"))
    # Resolve relative artifact paths against this explicit registry, never
    # against an implicit current/latest source on either machine.
    base = Path(path).resolve().parent
    return [read_source_bundle(base / record["bundle_path"], expected_manifest_sha256=record["manifest_sha256"],
                expected_producer=record["producer_id"], expected_symbols=record["symbols"]) for record in records]


def _result_summary(run, manifest_hash, *, status, **details):
    return {"status": status, "run_path": str(Path(run).resolve()), "manifest_sha256": manifest_hash,
            "orders_placed": 0, "broker_orders_enabled": False, "activation_changed": False, **details}


def _save_accuracy(output, report):
    output = Path(output).resolve()
    output.mkdir(parents=True, exist_ok=False)
    report_raw = encoded(report)
    (output / "report.json").write_bytes(report_raw)
    manifest = {"schema_version": ACCURACY_VERSION, "action_date": report["action_date"],
        "sources": report["sources"], "output_files": {"report.json": sha(report_raw)}}
    manifest_raw = encoded(manifest)
    (output / "manifest.json").write_bytes(manifest_raw)
    (output / "receipt.json").write_bytes(encoded({"schema_version": ACCURACY_VERSION, "status": "COMPLETE",
        "action_date": report["action_date"], "manifest_sha256": sha(manifest_raw),
        "orders_placed": 0, "broker_orders_enabled": False}))
    return _result_summary(output, sha(manifest_raw), status="COMPLETE", totals=report["totals"],
                           promoted_totals=report["promoted_totals"])


def parser():
    result = argparse.ArgumentParser(description=__doc__)
    commands = result.add_subparsers(dest="command", required=True)
    export = commands.add_parser("export-source", help="Copy explicit verified frozen native inputs into a new portable bundle")
    for name in ("datastore-root", "gameplan-run", "trade-plan-run", "output", "producer"):
        export.add_argument(f"--{name}", required=True)
    export.add_argument("--symbols", nargs="+", required=True)
    plan = commands.add_parser("plan", help="Project both explicit producers using one saved account snapshot")
    for name in ("sources-json", "snapshot-json", "observed-at", "output"):
        plan.add_argument(f"--{name}", required=True)
    verify = commands.add_parser("verify", help="Verify an explicitly pinned combined account plan")
    verify.add_argument("--run", required=True)
    verify.add_argument("--manifest-sha256", required=True)
    source = commands.add_parser("verify-source", help="Verify an explicitly pinned portable producer bundle")
    for name in ("bundle", "manifest-sha256", "producer"):
        source.add_argument(f"--{name}", required=True)
    source.add_argument("--symbols", nargs="+", required=True)
    source.add_argument("--action-date")
    accuracy = commands.add_parser("accuracy", help="Combine the two explicit original-source outcome counts")
    for name in ("sources-json", "results-json", "output"):
        accuracy.add_argument(f"--{name}", required=True)
    view = commands.add_parser("select-view", help="Select a verified locally transported report for the producer UI only")
    for name in ("datastore-root", "run", "manifest-sha256", "expected-config", "expected-previous-pointer-sha256"):
        view.add_argument(f"--{name}", required=True,
                          help="Use 'absent' to require no previous view pointer" if name == "expected-previous-pointer-sha256" else None)
    register = commands.add_parser("register-inputs", help="Register exact locally transported source pins without copying or fetching")
    for name in ("datastore-root", "action-date", "sources", "expected-config"):
        register.add_argument(f"--{name}", required=True)
    register_accuracy = commands.add_parser("register-accuracy", help="Register exact transported original result pins without copying or fetching")
    for name in ("datastore-root", "action-date", "results", "expected-config"):
        register_accuracy.add_argument(f"--{name}", required=True)
    return result


def main(argv=None, *, clock=None):
    """The optional Python clock is for offline tests; no CLI clock bypass exists."""
    args = parser().parse_args(argv)
    try:
        if args.command == "export-source":
            bundle = export_source_bundle(Path(args.datastore_root), gameplan_run=Path(args.gameplan_run),
                trade_plan_run=Path(args.trade_plan_run), destination=Path(args.output),
                producer_id=args.producer, expected_symbols=args.symbols)
            summary = _result_summary(bundle.path, bundle.manifest_sha256, status="EXPORTED",
                producer_id=bundle.metadata["producer_id"], action_date=bundle.metadata["action_date"],
                forecast_rows=len(bundle.forecasts), deployment_status=bundle.metadata["deployment_status"])
        elif args.command == "verify-source":
            bundle = read_source_bundle(Path(args.bundle), expected_manifest_sha256=args.manifest_sha256,
                expected_producer=args.producer, expected_symbols=args.symbols, expected_action_date=args.action_date)
            summary = _result_summary(bundle.path, bundle.manifest_sha256, status="VERIFIED",
                producer_id=bundle.metadata["producer_id"], action_date=bundle.metadata["action_date"], forecast_rows=len(bundle.forecasts))
        elif args.command in {"plan", "verify"}:
            if args.command == "plan":
                sources = _sources(args.sources_json)
                snapshot = _document(args.snapshot_json)
                plan = publish_account_plan(Path(args.output), sources, snapshot, observed_at=args.observed_at,
                    clock=clock or (lambda: datetime.now(timezone.utc)))
            else:
                plan = read_account_plan(Path(args.run), expected_manifest_sha256=args.manifest_sha256)
            summary = _result_summary(plan.path, plan.manifest_sha256,
                status="PUBLISHED" if args.command == "plan" else "VERIFIED", action_date=plan.report["action_date"],
                forecast_rows=len(plan.rows), direction_projection_status=plan.report["direction_projection_status"])
        elif args.command == "select-view":
            from ml.account_gameplan.views import select_view
            summary = select_view(Path(args.datastore_root), run=Path(args.run), manifest_sha256=args.manifest_sha256,
                expected_config=args.expected_config,
                expected_previous_pointer_sha256=None if args.expected_previous_pointer_sha256 == "absent"
                else args.expected_previous_pointer_sha256)
        elif args.command == "register-inputs":
            from ml.account_gameplan.preparation import register_inputs
            records = _records(args.sources, "sources", ("producer_id", "bundle_path", "manifest_sha256", "symbols"))
            saved = register_inputs(Path(args.datastore_root), action_date=args.action_date, sources=records,
                                    expected_config=args.expected_config)
            summary = {"status": "REGISTERED", "action_date": args.action_date, "registry": saved,
                       "orders_placed": 0, "broker_orders_enabled": False, "activation_changed": False}
        elif args.command == "register-accuracy":
            from ml.account_gameplan.review import register_inputs, _RESULT_FIELDS
            records = _records(args.results, "results", _RESULT_FIELDS)
            saved = register_inputs(Path(args.datastore_root), action_date=args.action_date, results=records,
                                    expected_config=args.expected_config)
            summary = {"status": "REGISTERED", "action_date": args.action_date, "registry": saved,
                       "orders_placed": 0, "broker_orders_enabled": False, "activation_changed": False}
        else:
            sources = _sources(args.sources_json)
            specs = _records(args.results_json, "results", ("producer_id", "run_path", "manifest_sha256"))
            registry = {source.metadata["producer_id"]: source for source in sources}
            if set(registry) != {spec["producer_id"] for spec in specs}:
                raise ValueError("Accuracy results differ from the explicitly selected source producers")
            base = Path(args.results_json).resolve().parent
            results = [read_source_accuracy(base / spec["run_path"], bundle=registry[spec["producer_id"]],
                        expected_manifest_sha256=spec["manifest_sha256"]) for spec in specs]
            summary = _save_accuracy(args.output, aggregate_accuracy(sources, results))
        print(json.dumps(summary, sort_keys=True, allow_nan=False))
        return 0
    except (OSError, ValueError, TypeError, KeyError, AttributeError, RuntimeError) as exc:
        print(json.dumps({"status": "ERROR", "error": type(exc).__name__, "detail": str(exc),
                          "orders_placed": 0, "broker_orders_enabled": False, "activation_changed": False}), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
