"""Explicit, audited installation/rebind for a failed late Gameplan publication.

This does not run preparation. It preserves the complete original state and
evidence, reuses only completed Stats, and requires fresh model review/training.
The source install and state transition hold the existing workflow/runtime locks.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
import json
from pathlib import Path
import re

from filelock import FileLock

from datafetching.runtime_lock import exclusive_runtime_lock
from ml import nightly_workflow as workflow
from ml.artifacts import file_checksum, utc_timestamp
from ml.nightly_recovery import verify_saved_recovery

VERSION = "nightly-archive-source-repair-v1"
ALLOWED = frozenset(("ml/gameplan_archive_features.py", "ml/gameplan_archive_seconds.py",
                     "ml/nightly_gameplan.py", "tools/nightly_source_repair.py"))


def _inventory(repository):
    return {p.relative_to(repository).as_posix(): file_checksum(p)
            for d in ("ml", "app", "datafetching")
            for p in sorted((repository / d).rglob("*.py"))}


def _immutable(path, raw):
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        if path.read_bytes() != raw:
            raise ValueError("Existing repair evidence differs; preserve it")
    else:
        with path.open("xb") as output:
            output.write(raw)


def _encoded(value):
    return (json.dumps(value, sort_keys=True, indent=2) + "\n").encode()


@contextmanager
def _locks(config):
    with FileLock(str(Path(config["state_root"]) / "workflow.lock"), timeout=0):
        with exclusive_runtime_lock(Path(config["datastore"]) / ".ducketz-overnight-runtime.lock",
                                    process_name="Reviewed nightly source repair"):
            yield


def _verify_failed(config, state, now):
    if (state.get("status") != "FAILED" or state.get("owner_pid") is not None
            or state.get("current_step") != "train_and_plan"
            or state.get("steps", {}).get("prepare_stats", {}).get("status") != "COMPLETE"
            or state.get("steps", {}).get("local_handoff")):
        raise ValueError("Only failed unpublished preparation with completed Stats can transition")
    workflow._verify_configuration_binding(config, state)
    workflow._verify_symbol_binding(config, state)
    verify_saved_recovery(Path(config["datastore"]), state["recovery"], now,
                          action_date=state["action_date"])
    if state.get("effective_deadline_at") != state["recovery"]["authorization"]["expires_at"]:
        raise ValueError("Recovery expiry differs")
    for step in state["steps"].values():
        if step.get("status") == "COMPLETE":
            workflow._verify_outputs(step["output"])
    native = Path(state["steps"]["train_and_plan"]["native_run"]).resolve()
    if native.parent != (Path(config["datastore"]) / "ml/overnight-runs").resolve():
        raise ValueError("Native attempt escapes the datastore")
    receipt = workflow._json(native / "receipt.json")
    report = workflow._json(native / "stage-report.json")
    if (receipt.get("status") != "FAILED" or report.get("status") != "FAILED"
            or receipt.get("failed_stage") != "gameplan_publication"
            or report.get("failed_stage") != "gameplan_publication"
            or receipt.get("stage_report_checksum_sha256") != file_checksum(native / "stage-report.json")):
        raise ValueError("Failed native publication evidence differs")
    for name, metadata in receipt["logs"].items():
        if Path(name).name != name or file_checksum(native / name) != metadata["checksum_sha256"]:
            raise ValueError("Failed native log evidence differs")


def prepare(config, *, action_date, repair_id, candidate, paths, completion_record,
            checks, reviewed=False, now=None):
    if not reviewed or not completion_record or not checks:
        raise ValueError("Exact source review, completion record and passing check evidence required")
    if not re.fullmatch(r"[A-Za-z0-9_-]{8,100}", repair_id):
        raise ValueError("Invalid repair identity")
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", action_date):
        raise ValueError("Exact action date required")
    if not paths or len(paths) != len(set(paths)) or not set(paths) <= ALLOWED:
        raise ValueError("Repair paths must be the reviewed archive-publication scope")
    observed = utc_timestamp(now)
    repository, candidate = Path(config["repository"]).resolve(), Path(candidate).resolve()
    state_path = Path(config["state_root"]) / "runs" / action_date / "state.json"
    destination = state_path.parent / "source-repairs" / repair_id
    with _locks(config):
        if (destination / "spec.json").exists():
            raise ValueError("Repair already prepared; apply its original specification")
        state = workflow._json(state_path)
        _verify_failed(config, state, observed)
        if state["action_date"] != action_date or state["source_identity"] != workflow.source_identity(repository):
            raise ValueError("Original session/source binding differs")
        records = []
        for name in paths:
            source, target = candidate / name, repository / name
            if source.is_symlink() or target.is_symlink() or not source.is_file():
                raise ValueError("Repair requires regular reviewed source files")
            raw = source.read_bytes()
            before = file_checksum(target) if target.exists() else None
            records.append({"path": name, "before_sha256": before,
                            "after_sha256": file_checksum(source)})
            _immutable(destination / "candidate" / name, raw)
            if target.exists():
                _immutable(destination / "originals" / name, target.read_bytes())
        check_records = []
        for name in checks:
            source = Path(name).resolve()
            raw = source.read_bytes()
            # The caller's actual runner exit code is reviewed separately. Keep
            # the exact result log; it cannot be replaced by a later test claim.
            if b"passed" not in raw or b"FAILED" in raw or b"ERROR " in raw:
                raise ValueError("Passing offline check log required")
            saved = destination / "checks" / str(len(check_records))
            _immutable(saved, raw)
            check_records.append({"path": str(saved), "sha256": file_checksum(saved)})
        _immutable(destination / "before-state.json", state_path.read_bytes())
        spec = {"schema_version": VERSION, "repair_id": repair_id,
                "prepared_at": observed.isoformat(), "completion_record": completion_record,
                "state_path": str(state_path.resolve()), "before_state_sha256": file_checksum(state_path),
                "config": config, "before_source": state["source_identity"],
                "before_files": _inventory(repository), "changes": records, "checks": check_records,
                "retained_steps": ["prepare_stats"],
                "review_policy": "Fresh model review and dependent preparation required; prior evidence retained"}
        _immutable(destination / "spec.json", _encoded(spec))
    return destination / "spec.json"


def apply(config, spec_path, *, reviewed=False, now=None):
    if not reviewed:
        raise ValueError("Reviewed repair required")
    spec_path = Path(spec_path).resolve()
    spec = workflow._json(spec_path)
    if spec.get("schema_version") != VERSION or spec.get("config") != config:
        raise ValueError("Repair configuration binding differs")
    state_path, repository = Path(spec["state_path"]), Path(config["repository"]).resolve()
    expected_parent = Path(config["state_root"]).resolve() / "runs"
    if (state_path.name != "state.json" or state_path.parent.parent.resolve() != expected_parent
            or spec_path.parent.parent != state_path.parent.resolve() / "source-repairs"):
        raise ValueError("Repair state/evidence path differs")
    records = spec["changes"]
    if not records or len({r["path"] for r in records}) != len(records) or not {r["path"] for r in records} <= ALLOWED:
        raise ValueError("Repair file scope differs")
    spec_sha = file_checksum(spec_path)
    with _locks(config):
        state = workflow._json(state_path)
        previous = state.get("source_repairs", [])
        if previous and previous[-1].get("spec_sha256") == spec_sha:
            if workflow.source_identity(repository) != previous[-1]["after_source"]:
                raise ValueError("Installed repaired source changed")
            _immutable(spec_path.parent / "applied.json", _encoded(previous[-1]))
            return {"status": "SOURCE_REPAIR_ALREADY_APPLIED", "repair_id": spec["repair_id"]}
        if (file_checksum(state_path) != spec["before_state_sha256"]
                or file_checksum(spec_path.parent / "before-state.json") != spec["before_state_sha256"]):
            raise ValueError("Saved original preparation changed")
        _verify_failed(config, state, utc_timestamp(now))
        if workflow.source_identity(repository)["commit"] != spec["before_source"]["commit"]:
            raise ValueError("Operating Git revision changed")
        expected = dict(spec["before_files"])
        actual = _inventory(repository)
        for record in records:
            name = record["path"]
            source, target = spec_path.parent / "candidate" / name, repository / name
            if (source.is_symlink() or target.is_symlink() or file_checksum(source) != record["after_sha256"]
                    or (file_checksum(target) if target.exists() else None) not in
                    (record["before_sha256"], record["after_sha256"])):
                raise ValueError("Reviewed source bytes differ")
            if name in actual:
                actual[name] = spec["before_files"][name]
            if name.startswith(("ml/", "app/", "datafetching/")):
                expected[name] = record["after_sha256"]
        if actual != spec["before_files"]:
            raise ValueError("Unrelated application source changed")
        for check in spec["checks"]:
            if file_checksum(Path(check["path"])) != check["sha256"]:
                raise ValueError("Reviewed test evidence changed")
        # Interrupted installation can resume only with the same frozen bytes.
        for record in records:
            target = repository / record["path"]
            target.parent.mkdir(parents=True, exist_ok=True)
            raw = (spec_path.parent / "candidate" / record["path"]).read_bytes()
            if not target.exists() or target.read_bytes() != raw:
                temporary = target.with_name(target.name + ".nightly-repair.tmp")
                temporary.write_bytes(raw)
                temporary.replace(target)
        if _inventory(repository) != expected:
            raise ValueError("Installed source inventory differs")
        after = workflow.source_identity(repository)
        transition = {"repair_id": spec["repair_id"], "spec_path": str(spec_path), "spec_sha256": spec_sha,
                      "before_state_sha256": spec["before_state_sha256"],
                      "before_source": spec["before_source"], "after_source": after,
                      "applied_at": utc_timestamp(now).isoformat(), "retained_steps": ["prepare_stats"]}
        state.setdefault("source_repairs", []).append(transition)
        state["source_identity"] = after
        state["steps"] = {"prepare_stats": state["steps"]["prepare_stats"]}
        state.update(status="READY", current_step="model_review", owner_pid=None)
        state.pop("error", None)
        state.pop("failed_at", None)
        workflow._write(state_path, state)
        _immutable(spec_path.parent / "applied.json", _encoded(transition))
        return {"status": "SOURCE_REPAIR_APPLIED", **transition,
                "preparation": "Not launched; use the normal checked catch-up launcher"}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True, type=Path)
    parser.add_argument("--reviewed", action="store_true")
    modes = parser.add_mutually_exclusive_group(required=True)
    modes.add_argument("--prepare", action="store_true")
    modes.add_argument("--apply", type=Path)
    parser.add_argument("--action-date")
    parser.add_argument("--repair-id")
    parser.add_argument("--candidate", type=Path)
    parser.add_argument("--path", action="append")
    parser.add_argument("--check-log", action="append")
    parser.add_argument("--completion-record")
    args = parser.parse_args(argv)
    config = workflow.load_config(args.config)
    workflow.verify_installation(config)
    if args.prepare:
        result = {"spec": str(prepare(config, action_date=args.action_date, repair_id=args.repair_id,
                  candidate=args.candidate, paths=args.path, completion_record=args.completion_record,
                  checks=args.check_log, reviewed=args.reviewed))}
    else:
        result = apply(config, args.apply, reviewed=args.reviewed)
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
