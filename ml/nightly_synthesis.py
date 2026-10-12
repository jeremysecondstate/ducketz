"""Locally compose and adopt explicitly selected Gameplan and Stats packages.

No discovery, transport, broker calls or activation occurs here. The local
operator selects immutable package hashes and a private account snapshot. Both
UI artifacts are validated in an isolated candidate before local adoption.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import re

import pandas as pd

from app.ui.gameplan_data import load_gameplan
from app.ui.gameplan_stats_data import load_gameplan_stats
from datafetching.runtime_lock import exclusive_runtime_lock
from ml.artifacts import file_checksum, utc_timestamp
from ml.gameplan_actuals_review import _write_json, completed_session_context
from ml.gameplan_stats_handoff import adopt_combined_stats, read_stats_package, combined_stats_target, combine_stats_frames
from ml.joint_capital_adoption import accept_joint_plan, read_accepted_joint_plan
from ml.joint_capital_handoff import _object, _read
from ml.joint_capital_plan import compose_joint_plan, content_sha256, load_owner_package


VERSION = "nightly-gameplan-synthesis-v1"
ACTORS = {"atlas": "Atlas", "scout": "Scout"}
SPEC_FIELDS = {"schema_version", "completion_id", "datastore_root", "state_root", "local_actor",
               "executor_owner", "action_date", "review_session", "account_scope_sha256", "as_of",
               "accepted_at", "snapshot", "owners"}


def _stamp(value, label):
    result = pd.Timestamp(value)
    if pd.isna(result) or result.tzinfo is None:
        raise ValueError(f"{label} needs an explicit timezone")
    return result.tz_convert("UTC")


def _path(value):
    if not isinstance(value, str) or not Path(value).is_absolute():
        raise ValueError("Synthesis paths must be explicit absolute local paths")
    return Path(value)


def _digest(value):
    if not isinstance(value, str) or re.fullmatch(r"[0-9a-f]{64}", value) is None:
        raise ValueError("An exact SHA-256 is required for every selected input")
    return value


def _selected_file(binding: dict, *, extra=()) -> bytes:
    if not isinstance(binding, dict) or set(binding) != {"path", "file_sha256", *extra}:
        raise ValueError("Invalid selected-file binding")
    path = _path(binding["path"])
    data = _read(path, path.parent)
    import hashlib
    if hashlib.sha256(data).hexdigest() != _digest(binding["file_sha256"]):
        raise ValueError("Selected file differs from its reviewed SHA-256")
    return data


def _validate_spec(spec: dict) -> None:
    if not isinstance(spec, dict) or set(spec) != SPEC_FIELDS or spec["schema_version"] != VERSION:
        raise ValueError("Unsupported synthesis specification")
    if not isinstance(spec["completion_id"], str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{7,63}", spec["completion_id"]):
        raise ValueError("A stable 8–64 character synthesis completion ID is required")
    if spec["local_actor"] != "scout" or spec["executor_owner"] != "atlas":
        raise ValueError("Synthesis belongs to Scout and retains Atlas as the sole executor")
    for key in ("datastore_root", "state_root"):
        _path(spec[key])
    _digest(spec["account_scope_sha256"])
    as_of, accepted = _stamp(spec["as_of"], "as_of"), _stamp(spec["accepted_at"], "accepted_at")
    if accepted < as_of:
        raise ValueError("Acceptance cannot precede synthesis")
    context = completed_session_context(as_of, spec["review_session"])
    if context["successor_action_date"] != spec["action_date"]:
        raise ValueError("Stats review session must precede the selected next exchange session")
    if not isinstance(spec["owners"], dict) or set(spec["owners"]) != set(ACTORS):
        raise ValueError("Exactly Atlas and Scout package selections are required")
    for owner in spec["owners"].values():
        if not isinstance(owner, dict) or set(owner) != {"symbols", "plan_package", "stats_package"}:
            raise ValueError("Each owner requires explicit symbols, plan package and Stats package")


def validate_local_profile(spec: dict, profile_path: Path) -> dict:
    """Bind CLI adoption to this checkout's authoritative private PC profile."""
    _validate_spec(spec)
    selected = _path(str(profile_path))
    profile = _object(_read(selected, selected.parent))
    repository = Path(__file__).resolve().parents[1]
    checkout = _path(profile.get("checkout")).resolve()
    if checkout != repository or selected.resolve() != checkout / "scratch/cross-pc/local-profile.json":
        raise ValueError("Synthesis must use this checkout's installed local profile")
    actor = profile.get("actor")
    if profile.get("contract_version") != "cross-pc-v2" or actor not in ACTORS.values():
        raise ValueError("Unsupported local coordination profile")
    local_actor = actor.lower()
    if spec["local_actor"] != local_actor:
        raise ValueError("Synthesis local actor differs from the installed PC profile")
    symbols = profile.get("symbols")
    if (not isinstance(symbols, list) or not symbols or len(set(symbols)) != len(symbols)
            or tuple(symbols) != tuple(spec["owners"][local_actor]["symbols"])):
        raise ValueError("Synthesis local symbols differ from the installed PC profile")
    return profile


def preflight_synthesis(spec: dict, *, now=None) -> dict:
    """Verify both selected package sets and compose without writing outputs."""
    _validate_spec(spec)
    observed = utc_timestamp(now)
    if _stamp(spec["accepted_at"], "accepted_at") > observed:
        raise ValueError("Selected acceptance time is in the future")
    snapshot = _object(_selected_file(spec["snapshot"]))
    packages, universes, hashes, stats_paths, stats_hashes, stats_symbols = [], {}, {}, {}, {}, {}
    stats_payloads, stats_frames = [], []
    for actor, title in ACTORS.items():
        owner = spec["owners"][actor]
        plan_binding = owner["plan_package"]
        _selected_file(plan_binding, extra=("root", "package_sha256"))
        package = load_owner_package(_path(plan_binding["root"]), _path(plan_binding["path"]),
                                     expected_sha256=_digest(plan_binding["package_sha256"]))
        if package["owner_id"] != actor:
            raise ValueError("Selected plan package has a different producer")
        packages.append(package)
        universes[actor], hashes[actor] = owner["symbols"], plan_binding["package_sha256"]
        stats_binding = owner["stats_package"]
        _selected_file(stats_binding)
        stats_paths[title], stats_hashes[title], stats_symbols[title] = (
            _path(stats_binding["path"]), stats_binding["file_sha256"], owner["symbols"])
        payload, frame = read_stats_package(stats_paths[title], expected_sha256=stats_hashes[title])
        if (payload["producer"] != title or payload["action_date"] != spec["review_session"]
                or tuple(payload["symbols"]) != tuple(owner["symbols"])):
            raise ValueError("Stats producer, completed session or frozen universe differs")
        if _stamp(payload["reviewed_at"], "Stats review") > _stamp(spec["as_of"], "as_of"):
            raise ValueError("Synthesis cannot precede its Stats review")
        stats_payloads.append(payload)
        stats_frames.append(frame)
    plan = compose_joint_plan(packages, snapshot, action_date=spec["action_date"],
        expected_universes=universes, expected_package_sha256=hashes,
        account_scope_sha256=spec["account_scope_sha256"], as_of=spec["as_of"], executor_owner="atlas")
    combined_stats_target(stats_payloads)
    combined = combine_stats_frames(stats_frames)
    if combined.id.duplicated().any() or combined.duplicated(["symbol", "route"]).any():
        raise ValueError("Combined Stats has duplicate forecast identities")
    # File readers validate from explicit paths; confirm none changed between
    # those reads and composition before any candidate or local pointer is made.
    _selected_file(spec["snapshot"])
    for owner in spec["owners"].values():
        _selected_file(owner["plan_package"], extra=("root", "package_sha256"))
        _selected_file(owner["stats_package"])
    return {"plan": plan, "universes": universes, "package_hashes": hashes,
            "stats_paths": stats_paths, "stats_hashes": stats_hashes, "stats_symbols": stats_symbols,
            "stats_frame": combined, "stats_sources": {payload["producer"]: payload for payload in stats_payloads}}


def _matching_stats(root: Path, spec: dict, prepared: dict):
    dated = root / "ml/gameplan-actuals-review-by-date" / spec["review_session"] / "run.json"
    if not dated.exists():
        return None
    review = load_gameplan_stats(root, spec["review_session"])
    report = _object((review.run_directory / "report.json").read_bytes())
    if report.get("review_mode") != "combined-atlas-scout":
        return None
    expected = prepared["stats_hashes"]
    producers = report.get("producers", {})
    if (set(producers) != set(expected)
            or any(producers[actor].get("package_sha256") != digest for actor, digest in expected.items())):
        raise ValueError("This session already has different combined Stats sources")
    sources = prepared["stats_sources"]
    expected_missing = sorted(actor for actor, source in sources.items()
                              if source["coverage_status"] == "NO_SAVED_INDEPENDENT_GAMEPLAN")
    expected_coverage = ("NO_SAVED_INDEPENDENT_GAMEPLAN" if len(expected_missing) == 2
                         else "PARTIAL_SAVED_FORECASTS" if expected_missing else "SAVED_FORECASTS")
    if (report.get("missing_history_owners") != expected_missing
            or report.get("coverage_status") != expected_coverage
            or report.get("probability_target_contract") != combined_stats_target(tuple(sources.values()))
            or any(producers[actor].get(field) != source[field] for actor, source in sources.items()
                   for field in ("symbols", "coverage_status", "probability_target_contract",
                                 "source_receipt_sha256", "source_report_sha256"))):
        raise ValueError("Combined Stats coverage or provenance differs from its selected source packages")
    try:
        pd.testing.assert_frame_equal(
            pd.read_parquet(review.run_directory / "forecast-results.parquet").sort_index(axis=1).reset_index(drop=True),
            prepared["stats_frame"].sort_index(axis=1).reset_index(drop=True), check_dtype=False, check_exact=True)
    except AssertionError as exc:
        raise ValueError("Combined Stats rows differ from their selected source packages") from exc
    return review.run_directory


def _adopt_plan(root: Path, spec: dict, prepared: dict) -> Path:
    return accept_joint_plan(root, prepared["plan"], expected_sha256=prepared["plan"]["plan_sha256"],
        expected_package_sha256=prepared["package_hashes"], expected_universes=prepared["universes"],
        account_scope_sha256=spec["account_scope_sha256"], local_actor=spec["local_actor"], executor_owner="atlas",
        action_date=spec["action_date"], accepted_at=spec["accepted_at"])


def _adopt_stats(root: Path, spec: dict, prepared: dict) -> Path:
    existing = _matching_stats(root, spec, prepared)
    return existing or adopt_combined_stats(root, packages=prepared["stats_paths"],
        expected_symbols=prepared["stats_symbols"], expected_sha256=prepared["stats_hashes"],
        reviewed_at=spec["accepted_at"])


def _verify_ui(root: Path, spec: dict, prepared: dict) -> dict:
    plan = load_gameplan(root)
    stats = load_gameplan_stats(root)
    selected = read_accepted_joint_plan(root, spec["action_date"])
    expected_symbols = set(symbol for values in prepared["universes"].values() for symbol in values)
    stats_run = _matching_stats(root, spec, prepared)
    if (selected is None or selected[0]["plan_sha256"] != prepared["plan"]["plan_sha256"]
            or plan.run_directory != selected[2] or plan.session != spec["action_date"]
            or set(plan.symbols) != expected_symbols or stats.session != spec["review_session"]
            or stats.run_directory != stats_run or set(stats.symbols) - expected_symbols):
        raise ValueError("Default UI readers do not select the exact combined plan and Stats")
    if selected[1]["local_actor"] != spec["local_actor"] or selected[1]["executor_owner"] != "atlas":
        raise ValueError("Accepted plan changed the local execution role")
    paths = [selected[2] / "accepted-plan.json", selected[2] / "joint-plan.json",
             stats.run_directory / "receipt.json", stats.run_directory / "manifest.json",
             stats.run_directory / "forecast-results.parquet"]
    return {"plan_run": str(selected[2]), "stats_run": str(stats.run_directory),
            "files": {str(path): file_checksum(path) for path in paths},
            "plan_sha256": selected[0]["plan_sha256"], "stats_receipt_sha256": file_checksum(stats.run_directory / "receipt.json")}


def run_synthesis(spec: dict, *, now=None) -> dict:
    """Resume one exact local specification through candidate and UI adoption."""
    from ml.nightly_joint_readiness import publish_joint_readiness
    _validate_spec(spec)
    state_root = _path(spec["state_root"])
    state_root.mkdir(parents=True, exist_ok=True)
    spec_hash = content_sha256(spec)
    work = state_root / spec["completion_id"]
    with exclusive_runtime_lock(state_root / "synthesis.lock", process_name="nightly-synthesis"):
        work.mkdir(parents=True, exist_ok=True)
        record = work / "state.json"
        state = _object(record.read_bytes()) if record.exists() else {
            "schema_version": VERSION, "completion_id": spec["completion_id"], "spec_sha256": spec_hash,
            "status": "PREPARING", "steps": {}, "orders_placed": 0, "activation_changed": False}
        if state.get("spec_sha256") != spec_hash:
            raise ValueError("Synthesis retry changed its exact local specification")
        root = _path(spec["datastore_root"])
        if state["status"] == "JOINT_READY_LOCAL":
            prepared = preflight_synthesis(spec, now=now)
            evidence = _verify_ui(root, spec, prepared)
            receipt = _object((work / "receipt.json").read_bytes())
            if receipt["ui_evidence"] != evidence or receipt["spec_sha256"] != spec_hash:
                raise ValueError("Completed synthesis evidence changed")
            publish_joint_readiness(root, spec, receipt)
            return receipt
        _write_json(record, state)
        try:
            prepared = preflight_synthesis(spec, now=now)
            candidate = work / "candidate"
            candidate.mkdir(exist_ok=True)
            _adopt_plan(candidate, spec, prepared)
            _adopt_stats(candidate, spec, prepared)
            state["steps"]["candidate_verified"] = _verify_ui(candidate, spec, prepared)
            state["status"] = "CANDIDATE_VERIFIED"
            _write_json(record, state)
            # Existing sessions must match before either live pointer changes.
            selected = read_accepted_joint_plan(root, spec["action_date"])
            if selected and selected[0]["plan_sha256"] != prepared["plan"]["plan_sha256"]:
                raise ValueError("This session already has a different accepted combined plan")
            _matching_stats(root, spec, prepared)
            plan_run = _adopt_plan(root, spec, prepared)
            state["steps"]["plan_adopted"] = str(plan_run)
            state["status"] = "PARTIAL_ADOPTION"
            _write_json(record, state)
            stats_run = _adopt_stats(root, spec, prepared)
            state["steps"]["stats_adopted"] = str(stats_run)
            _write_json(record, state)
            evidence = _verify_ui(root, spec, prepared)
            receipt = {"schema_version": VERSION, "completion_id": spec["completion_id"], "spec_sha256": spec_hash,
                "status": "JOINT_READY_LOCAL", "joint_ready": True, "ui_ready": True,
                "peer_verified": False, "execution_authorized": False, "activation_changed": False, "orders_placed": 0,
                "local_actor": spec["local_actor"], "executor_owner": "atlas", "action_date": spec["action_date"],
                "review_session": spec["review_session"], "account_scope_sha256": spec["account_scope_sha256"],
                "snapshot_file_sha256": spec["snapshot"]["file_sha256"],
                "account_snapshot_sha256": prepared["plan"]["account_snapshot_sha256"],
                "owner_packages": prepared["package_hashes"], "stats_packages": prepared["stats_hashes"],
                "ui_evidence": evidence, "receipt_path": str(work / "receipt.json")}
            _write_json(work / "receipt.json", receipt)
            state["status"] = "JOINT_READY_LOCAL"
            state.pop("error", None)
            _write_json(record, state)
            publish_joint_readiness(root, spec, receipt)
            return receipt
        except Exception as exc:
            partial = "plan_adopted" in state["steps"]
            if not partial:
                try:
                    adopted = read_accepted_joint_plan(root, spec["action_date"])
                    partial = adopted is not None and adopted[1]["local_actor"] == spec["local_actor"]
                except (OSError, ValueError, KeyError):
                    pass
            state["status"] = "PARTIAL_ADOPTION" if partial else "FAILED"
            state["error"] = f"{type(exc).__name__}: {exc}"
            _write_json(record, state)
            raise


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--spec", type=Path, required=True, help="Explicit locally reviewed selections; never an incoming command")
    parser.add_argument("--local-profile", type=Path, required=True,
                        help="Absolute path to this checkout's installed scratch/cross-pc/local-profile.json")
    parser.add_argument("--validate-only", action="store_true", help="Check selected files without adopting or writing any output")
    args = parser.parse_args(argv)
    spec_path = args.spec.resolve()
    spec = _object(_read(spec_path, spec_path.parent))
    validate_local_profile(spec, args.local_profile)
    if args.validate_only:
        prepared = preflight_synthesis(spec)
        print(json.dumps({"status": "INPUTS_VERIFIED", "plan_sha256": prepared["plan"]["plan_sha256"],
                          "ui_ready": False, "joint_ready": False, "orders_placed": 0}))
    else:
        result = run_synthesis(spec)
        print(json.dumps({key: result[key] for key in ("status", "completion_id", "ui_ready", "joint_ready", "receipt_path", "orders_placed")}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
