"""Opt-in native tail: export frozen sources, then prepare one account plan.

Transport delivers reviewed artifact pins only. Cash and ownership are read
afresh from the coordinator's account and consolidated native ledger.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import os
from pathlib import Path
import time
import uuid

from filelock import FileLock
import pandas as pd

from ml.account_gameplan.config import VERSION as CONFIG_VERSION, load_account_config, verify_cutover
from ml.account_gameplan.planner import aware, encoded, publish_account_plan, read_account_plan, sha
from ml.account_gameplan.sources import _json, export_source_bundle, read_source_bundle
from ml.artifacts import file_checksum

VERSION = "account-gameplan-preparation-v1"


def _clock():
    return datetime.now(timezone.utc)


def _config(root, expected):
    config = load_account_config(root)
    if config is None or config.fingerprint != expected:
        raise ValueError("Account preparation binding changed or is missing")
    return config


def _before(clock, deadline):
    now = aware(clock())
    if now >= deadline:
        raise ValueError("ACCOUNT_PREPARATION_DEADLINE_PASSED")
    return now


def _local(root, value):
    candidate = Path(value)
    path = (root / candidate).resolve()
    if not path.is_relative_to(root):
        raise ValueError("Account input must be a locally transported artifact inside this datastore")
    return path


def _immutable(path, value):
    raw = encoded(value)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        if path.read_bytes() != raw:
            raise ValueError("An immutable account preparation record already has different contents")
    else:
        temporary = path.with_name(f".{path.name}-{uuid.uuid4().hex}.tmp")
        try:
            with temporary.open("xb") as stream:
                stream.write(raw)
                stream.flush()
                os.fsync(stream.fileno())
            # A hard-link commit is atomic and refuses overwrite, so a
            # concurrent reader never sees an empty/partially written registry.
            try:
                os.link(temporary, path)
            except FileExistsError:
                if path.read_bytes() != raw:
                    raise ValueError("An immutable account preparation record advanced independently")
        finally:
            temporary.unlink(missing_ok=True)
    return sha(raw)


def _invalidate_new_record(path, value, *, reason):
    """Invalidate only these newly committed bytes; preserve other writers."""
    expected = encoded(value)
    failed = {**value, "status": "FAILED", "failure_reason": reason}
    evidence = path.with_name(f"{path.stem}-failed-{uuid.uuid4().hex}.json")
    _immutable(evidence, {"failed_record_sha256": sha(expected), "record": value, "failure_reason": reason})
    if not path.exists() or path.read_bytes() != expected:
        return
    temporary = path.with_name(f".{path.name}-failed-{uuid.uuid4().hex}.tmp")
    try:
        temporary.write_bytes(encoded(failed))
        if path.exists() and path.read_bytes() == expected:
            os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _sources(root, config, day, records):
    fields = {"producer_id", "bundle_path", "manifest_sha256", "symbols"}
    if (not isinstance(records, list) or len(records) != 2
            or any(not isinstance(item, dict) or set(item) != fields for item in records)
            or {item["producer_id"] for item in records} != set(config.participants)):
        raise ValueError("Exact two-producer account source registry required")
    result = []
    for record in records:
        producer = record["producer_id"]
        if sorted(record["symbols"]) != sorted(config.participants[producer]):
            raise ValueError("Transported source symbols differ from reviewed account binding")
        result.append(read_source_bundle(_local(root, record["bundle_path"]),
            expected_manifest_sha256=record["manifest_sha256"], expected_producer=producer,
            expected_symbols=config.participants[producer], expected_action_date=day))
    return result


def register_inputs(root, *, action_date, sources, expected_config):
    """Register explicitly transported, complete source pins; never copy or fetch."""
    root = Path(root).resolve()
    config = _config(root, expected_config)
    verified = _sources(root, config, action_date, sources)
    records = [{"producer_id": source.metadata["producer_id"], "bundle_path": source.path.relative_to(root).as_posix(),
                "manifest_sha256": source.manifest_sha256, "symbols": list(source.metadata["symbols"])}
               for source in sorted(verified, key=lambda item: item.metadata["producer_id"])]
    document = {"schema_version": VERSION, "action_date": action_date, "sources": records}
    path = root / f"state/account-gameplan/inputs/{action_date}.json"
    if _config(root, expected_config) != config:
        raise ValueError("Account configuration advanced during input registration")
    _immutable(path, document)
    return document


def _native_snapshot(root, config, *, observed_at):
    from ml.gameplan_trade_snapshot import _capture_trade_planning_snapshot, _ownership
    checked = False
    def ownership(identity, snapshot):
        nonlocal checked
        if identity != config.account_fingerprint:
            raise ValueError("Account preparation broker identity differs from configured account")
        checked = True
        return _ownership(root, config.symbols, identity, snapshot["held_shares"], snapshot["observed_at"])
    result = _capture_trade_planning_snapshot(root, requested=config.symbols, observed_at=observed_at,
                                             explicit_universe=True, ownership_reader=ownership)
    if not checked or result.get("status") != "OBSERVED" or result.get("ownership", {}).get("safe_for_planning") is not True:
        raise ValueError("Fresh matching-account union ownership snapshot is unavailable")
    return {**result, "account_fingerprint": config.account_fingerprint}


def _selected(root, path, config, day, sources):
    selection = _json(path.read_bytes())
    if (selection.get("status") != "SELECTED" or selection.get("schema_version") != CONFIG_VERSION or selection.get("config_sha256") != config.fingerprint
            or selection.get("action_date") != day or selection.get("producer_id") != config.machine_id):
        raise ValueError("Existing account selection belongs to another source configuration")
    current = selection["current"]
    run = _local(root, current["run_path"])
    if run.parent != root / "ml/account-gameplan-runs" or current.get("action_date") != day:
        raise ValueError("Existing account selection source path or session differs")
    plan = read_account_plan(run, expected_manifest_sha256=current["manifest_sha256"])
    expected = {source.metadata["producer_id"]: (source.manifest_sha256, source.metadata["source_receipt_sha256"])
                for source in sources}
    actual = {source["producer_id"]: (source["bundle_manifest_sha256"], source["source_receipt_sha256"])
              for source in plan.report["sources"]}
    if plan.report["action_date"] != day or actual != expected:
        raise ValueError("Existing account selection differs from the explicit source pins")
    return selection, plan


def _select(root, selection, config, deadline, clock, previous_latest):
    dated = root / f"ml/account-gameplan-by-date/{selection['action_date']}/run.json"
    latest = root / "ml/account-gameplan-latest/run.json"
    latest.parent.mkdir(parents=True, exist_ok=True)
    _before(clock, deadline)
    if _config(root, config.fingerprint) != config:
        raise ValueError("Account configuration changed before selection")
    verify_cutover(root, config)
    if (latest.read_bytes() if latest.exists() else None) != previous_latest:
        raise ValueError("Account latest selection advanced independently")
    if dated.exists() and dated.read_bytes() != encoded(selection):
        raise ValueError("This session already has a different immutable account selection")
    new_dated = not dated.exists()
    temporary = latest.with_name(f".run-{uuid.uuid4().hex}.tmp")
    try:
        temporary.write_bytes(encoded(selection))
        _before(clock, deadline)
        if _config(root, config.fingerprint) != config:
            raise ValueError("Account configuration changed before latest selection")
        verify_cutover(root, config)
        if (latest.read_bytes() if latest.exists() else None) != previous_latest:
            raise ValueError("Account latest selection advanced independently")
        os.replace(temporary, latest)
        # The dated pointer grants source selection to execution. Commit it
        # last: a failed final guard may expose an informational UI report but
        # cannot make a new session executable.
        _before(clock, deadline)
        if _config(root, config.fingerprint) != config:
            raise ValueError("Account configuration changed before dated selection")
        verify_cutover(root, config)
        _immutable(dated, selection)
        try:
            _before(clock, deadline)
            if _config(root, config.fingerprint) != config:
                raise ValueError("Account configuration changed during dated selection")
            verify_cutover(root, config)
        except Exception:
            if new_dated:
                _invalidate_new_record(dated, selection, reason="DATED_SELECTION_POST_COMMIT_GUARD_FAILED")
            raise
    finally:
        temporary.unlink(missing_ok=True)


def run_preparation(root, *, gameplan_run, trade_plan_run, deadline, expected_config,
                    clock=None, sleeper=None, snapshot_capture=None, progress=None):
    """Called only by the configured native tail under its existing owner."""
    root = Path(root).resolve()
    clock, sleeper = clock or _clock, sleeper or time.sleep
    config = _config(root, expected_config)
    source_run, trade_run = _local(root, gameplan_run), _local(root, trade_plan_run)
    receipt = _json((source_run / "receipt.json").read_bytes())
    day = receipt["action_date"]
    deadline = aware(deadline)
    opening = pd.Timestamp(f"{day} 04:00", tz="America/Los_Angeles").tz_convert("UTC")
    if deadline != opening:
        raise ValueError("Account preparation must preserve the original action-session 04:00 deadline")
    _before(clock, deadline)
    lock_path = root / "state/account-gameplan/preparation.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with FileLock(str(lock_path), timeout=0):
        previous_latest_path = root / "ml/account-gameplan-latest/run.json"
        previous_latest = previous_latest_path.read_bytes() if previous_latest_path.exists() else None
        source_hash = file_checksum(source_run / "receipt.json")
        destination = root / f"ml/account-gameplan-source-runs/{source_hash}"
        outgoing = root / f"state/account-gameplan/outgoing/{day}/{source_hash}.json"
        if destination.exists():
            saved_pin = _json(outgoing.read_bytes())["manifest_sha256"] if outgoing.exists() else file_checksum(destination / "manifest.json")
            source = read_source_bundle(destination, expected_manifest_sha256=saved_pin,
                expected_producer=config.machine_id, expected_symbols=config.participants[config.machine_id], expected_action_date=day)
            if (source.metadata["source_receipt_sha256"] != source_hash
                    or source.metadata["trade_plan_receipt_sha256"] != file_checksum(trade_run / "receipt.json")):
                raise ValueError("Saved source export differs from the pinned completed native preparation")
        else:
            source = export_source_bundle(root, gameplan_run=source_run, trade_plan_run=trade_run,
                destination=destination, producer_id=config.machine_id, expected_symbols=config.participants[config.machine_id])
        if aware(source.metadata["trade_plan_completed_at"]) > _before(clock, deadline):
            raise ValueError("Native source completion is in the future")
        ready = {"schema_version": VERSION, "status": "SOURCE_READY", "action_date": day,
            "producer_id": config.machine_id, "config_sha256": config.fingerprint,
            "source_receipt_sha256": source_hash, "bundle_path": destination.relative_to(root).as_posix(),
            "manifest_sha256": source.manifest_sha256, "symbols": list(source.metadata["symbols"]),
            "orders_placed": 0, "broker_orders_enabled": False}
        new_outgoing = not outgoing.exists()
        _immutable(outgoing, ready)
        try:
            _before(clock, deadline)
            if _config(root, expected_config) != config:
                raise ValueError("Account configuration changed during source-ready publication")
        except Exception:
            if new_outgoing:
                _invalidate_new_record(outgoing, ready, reason="SOURCE_READY_POST_COMMIT_GUARD_FAILED")
            raise
        if config.role == "producer" or config.activation["status"] != "ACTIVE":
            return {**ready, "source_ready_receipt": outgoing.relative_to(root).as_posix(), "selection_changed": False}
        verify_cutover(root, config)
        registry_path = root / f"state/account-gameplan/inputs/{day}.json"
        while not registry_path.exists():
            now = _before(clock, deadline)
            if _config(root, expected_config) != config:
                raise ValueError("Account configuration changed while waiting for sources")
            if progress:
                progress({"status": "WAITING_FOR_ACCOUNT_SOURCES", "action_date": day, "deadline_at": deadline.isoformat()})
            sleeper(min(30.0, (deadline - now).total_seconds()))
        registry_raw = registry_path.read_bytes()
        registry = _json(registry_raw)
        if (set(registry) != {"schema_version", "action_date", "sources"}
                or registry.get("schema_version") != VERSION or registry.get("action_date") != day):
            raise ValueError("Account source registry has a different schema or session")
        sources = _sources(root, config, day, registry["sources"])
        local = next(item for item in sources if item.metadata["producer_id"] == config.machine_id)
        if local.manifest_sha256 != source.manifest_sha256:
            raise ValueError("Account registry does not pin this completed local source")
        dated = root / f"ml/account-gameplan-by-date/{day}/run.json"
        if dated.exists():
            selection, plan = _selected(root, dated, config, day, sources)
        elif previous_latest is not None and _json(previous_latest).get("action_date") == day:
            # Recover a UI-only commit interrupted before the final immutable
            # execution selection, without recapturing/rebuilding the plan.
            selection, plan = _selected(root, previous_latest_path, config, day, sources)
        else:
            _before(clock, deadline)
            verify_cutover(root, config)
            capture = snapshot_capture or _native_snapshot
            snapshot = capture(root, config, observed_at=_before(clock, deadline).isoformat())
            if snapshot.get("account_fingerprint") != config.account_fingerprint:
                raise ValueError("Fresh account snapshot identity differs")
            observed = _before(clock, deadline)
            name = observed.strftime("%Y%m%dT%H%M%S.%fZ") + "-" + uuid.uuid4().hex[:8]
            plan = publish_account_plan(root / "ml/account-gameplan-runs" / name, sources, snapshot,
                                        observed_at=observed, clock=clock)
            selection = {"schema_version": CONFIG_VERSION, "status": "SELECTED", "config_sha256": config.fingerprint,
                "action_date": day, "producer_id": config.machine_id, "current": {
                    "run_path": plan.path.relative_to(root).as_posix(), "manifest_sha256": plan.manifest_sha256,
                    "action_date": day}}
        if registry_path.read_bytes() != registry_raw:
            raise ValueError("Account source registry changed before selection")
        unchanged = previous_latest == encoded(selection) and dated.exists()
        if not unchanged:
            _select(root, selection, config, deadline, clock, previous_latest)
        else:
            _before(clock, deadline)
            if _config(root, expected_config) != config:
                raise ValueError("Account configuration changed before existing selection reuse")
            verify_cutover(root, config)
        return {**ready, "status": "COMPLETE", "current": selection["current"],
                "direction_projection_status": plan.report["direction_projection_status"], "selection_changed": not unchanged}


def main(argv=None):
    import json
    from datafetching.parquet_store import resolve_datastore_dir
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--datastore", type=Path)
    parser.add_argument("--datastore-target")
    for name in ("gameplan-run", "trade-plan-run", "deadline", "expected-config"):
        parser.add_argument(f"--{name}", required=True)
    args = parser.parse_args(argv)
    root = resolve_datastore_dir(root_dir=args.datastore, target=args.datastore_target)
    result = run_preparation(root, gameplan_run=args.gameplan_run, trade_plan_run=args.trade_plan_run,
        deadline=args.deadline, expected_config=args.expected_config,
        progress=lambda event: print(json.dumps(event), flush=True))
    print(json.dumps(result, sort_keys=True), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
