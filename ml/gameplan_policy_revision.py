"""Reproject an accepted Gameplan under a new policy using its saved inputs.

This local correction preserves the original forecasts, account observation and
accepted artifacts. It writes a new immutable plan and replaces only the dated
selection using a compare-and-swap. It does not obtain quotes or place orders.
"""
from __future__ import annotations

from copy import deepcopy
import hashlib
import json
from pathlib import Path

import pandas as pd

from datafetching.runtime_lock import exclusive_runtime_lock
from ml.joint_capital_plan import (
    _date, _hash, _json_bytes, _ordinary_path, _time, _validate_plan_digest,
    compose_joint_plan, content_sha256, load_joint_plan, load_owner_package,
)
from ml.stock_direction_policy import (STOCK_DIRECTION_POLICY_VERSION, BULLISH_PROBABILITY,
                                      BEARISH_PROBABILITY, stock_direction)
from ml.stock_trader.contracts import StockTraderPolicy
from ml.stock_trader.gameplan_execution import _saved_abstention, _validated_instructions
from ml.stock_trader.publication import _write_json_atomic


VERSION = "gameplan-policy-revision-v1"
RUNS = "ml/joint-gameplan-runs"
POINTERS = "ml/joint-gameplan-by-date"


def _read(path: Path, expected_sha256: str | None = None):
    raw = path.read_bytes()
    if expected_sha256 is not None and hashlib.sha256(raw).hexdigest() != _hash(expected_sha256, "file"):
        raise ValueError("Saved revision input differs from its selected file hash")
    return raw, json.loads(raw)


def _selected(root, pointer, expected):
    raw, binding = _read(pointer)
    run = (root / binding.get("run_path", "")).resolve()
    if run.parent != (root / RUNS).resolve():
        raise ValueError("Selected policy revision escapes its immutable directory")
    _, receipt = _read(run / "accepted-plan.json")
    if binding != receipt or binding.get("plan_sha256") != expected:
        raise ValueError("Accepted plan selection changed or differs from its receipt")
    plan = load_joint_plan(run, run / "joint-plan.json", expected_sha256=expected)
    if (binding.get("schema_version") != "accepted-joint-gameplan-v1"
            or plan.get("status") != "COMPLETE"
            or binding.get("action_date") != plan.get("action_date")
            or binding.get("account_scope_sha256") != plan.get("account_scope_sha256")
            or binding.get("executor_owner") != plan.get("sole_executor_owner")
            or binding.get("local_actor") not in plan.get("input_bindings", {})):
        raise ValueError("Accepted revision source has inconsistent local bindings")
    sources = plan["input_bindings"]
    if (set(binding.get("owner_packages", {})) != set(sources)
            or set(binding.get("owner_universes", {})) != set(sources)
            or any(binding["owner_packages"][owner] != source["package_sha256"]
                   or binding["owner_universes"][owner] != source["frozen_symbols"]
                   for owner, source in sources.items())
            or sorted(binding.get("execution_symbols", ())) != sorted(
                symbol for source in sources.values() for symbol in source["frozen_symbols"])):
        raise ValueError("Accepted revision source changed its original producer bindings")
    return raw, binding, plan


def _seal(plan):
    plan.pop("plan_sha256", None)
    plan.pop("plan_id", None)
    views = plan.pop("owner_views")
    identity = "joint-capital:" + content_sha256(plan)
    plan["plan_id"] = identity
    plan["owner_views"] = {owner: {**view, "plan_id": identity} for owner, view in views.items()}
    plan["plan_sha256"] = content_sha256(plan)
    _validate_plan_digest(plan, plan["plan_sha256"])


def revise_accepted_gameplan(datastore_root: Path, *, action_date: str,
                            expected_plan_sha256: str, snapshot_path: Path,
                            snapshot_file_sha256: str, owner_package_paths: dict,
                            owner_package_file_sha256: dict, revised_at: str,
                            policy: StockTraderPolicy | None = None) -> Path:
    """Apply an explicit local policy correction, retaining the original basis.

    ``revised_at`` is the actual correction time. ``as_of`` and account snapshot
    time retain their original meanings; a saved balance is never called live.
    Repeating the same request is idempotent. A changed selection is not replaced.
    """
    root, day = Path(datastore_root).resolve(), _date(action_date)
    expected = _hash(expected_plan_sha256, "parent plan")
    revised = _time(revised_at, "revision time")
    parent_run = root / RUNS / expected
    parent = load_joint_plan(parent_run, parent_run / "joint-plan.json", expected_sha256=expected)
    parent_revision = parent.get("policy_revision", {})
    ancestors = list(parent_revision.get("ancestor_plan_sha256", ()))
    if parent_revision.get("parent_plan_sha256") is not None:
        ancestors.append(parent_revision["parent_plan_sha256"])
    ancestors = list(dict.fromkeys([*ancestors, expected]))
    for ancestor in ancestors:
        _hash(ancestor, "ancestor plan")
    revision = {"schema_version": VERSION, "parent_plan_sha256": expected,
                "ancestor_plan_sha256": ancestors,
                "direction_policy_version": STOCK_DIRECTION_POLICY_VERSION,
                "direction_up_threshold": BULLISH_PROBABILITY,
                "direction_down_threshold": BEARISH_PROBABILITY,
                "revised_at": revised.isoformat(),
                "snapshot_file_sha256": _hash(snapshot_file_sha256, "snapshot file"),
                "owner_package_file_sha256": {owner: _hash(digest, "owner file")
                    for owner, digest in sorted(owner_package_file_sha256.items())},
                "account_basis": "ORIGINAL_SAVED_OBSERVATION_NOT_CURRENT_BROKER_STATE"}
    revision["revision_id"] = content_sha256(revision)
    pointer = root / POINTERS / day / "run.json"
    _, current = _read(pointer)
    if current.get("policy_revision") == revision:
        _read(Path(snapshot_path).resolve(), snapshot_file_sha256)
        if set(owner_package_paths) != set(owner_package_file_sha256):
            raise ValueError("Revision retry changed the selected owner inputs")
        for owner, path in owner_package_paths.items():
            _read(Path(path).resolve(), owner_package_file_sha256[owner])
        _, _, previous_result = _selected(root, pointer, current["plan_sha256"])
        _validated_instructions(pd.DataFrame(previous_result["forecasts"]))
        return root / current["run_path"]
    pointer_bytes, binding, original = _selected(root, pointer, expected)
    if original["action_date"] != day or revised < _time(binding["accepted_at"], "original acceptance"):
        raise ValueError("Revision date or time precedes the selected accepted plan")
    active_policy = policy or StockTraderPolicy()
    active_policy.validate()
    if active_policy.fingerprint != original["risk_policy_fingerprint"]:
        raise ValueError("A direction revision must preserve the original capital policy")
    owners = set(binding["owner_packages"])
    if set(owner_package_paths) != owners or set(owner_package_file_sha256) != owners:
        raise ValueError("Revision requires both exact original owner packages")
    snapshot_path = Path(snapshot_path).resolve()
    _, snapshot = _read(snapshot_path, snapshot_file_sha256)
    if (content_sha256(snapshot) != original["account_snapshot_sha256"]
            or snapshot.get("observed_at") != original["account_snapshot_observed_at"]):
        raise ValueError("Revision snapshot differs from the original saved observation")
    packages, selected_files = [], [(snapshot_path, snapshot_file_sha256)]
    for owner in sorted(owners):
        path = Path(owner_package_paths[owner]).resolve()
        _read(path, owner_package_file_sha256[owner])
        package = load_owner_package(path.parent, path,
                                     expected_sha256=binding["owner_packages"][owner])
        if (package["owner_id"] != owner or package["action_date"] != day
                or package["frozen_symbols"] != binding["owner_universes"][owner]
                or any(package.get(key) != value for key, value in original["input_bindings"][owner].items())):
            raise ValueError("Revision package differs from the original selected producer")
        packages.append(package)
        selected_files.append((path, owner_package_file_sha256[owner]))
    plan = compose_joint_plan(packages, snapshot, action_date=day,
        expected_universes=binding["owner_universes"], expected_package_sha256=binding["owner_packages"],
        account_scope_sha256=binding["account_scope_sha256"], as_of=original["as_of"],
        executor_owner=binding["executor_owner"], policy=active_policy)
    original_rows = {row["id"]: row for row in original["forecasts"]}
    if set(original_rows) != {row["id"] for row in plan["forecasts"]}:
        raise ValueError("Policy revision must preserve all original forecast identities")
    for row in plan["forecasts"]:
        previous = original_rows[row["id"]]
        for key in ("calibrated_probability", "symbol", "model_group", "target_window_start", "target_window_end"):
            if row[key] != previous[key]:
                raise ValueError("Policy revision changed an original prediction")
        row["source_direction"] = row["direction"]
        row["source_direction_policy_version"] = row.get("direction_policy_version")
        row["source_direction_up_threshold"] = row.get("direction_up_threshold")
        row["source_direction_down_threshold"] = row.get("direction_down_threshold")
        row["direction"] = "NO_EDGE" if _saved_abstention(row) else stock_direction(row["calibrated_probability"])
        row["direction_policy_version"] = STOCK_DIRECTION_POLICY_VERSION
        row["direction_up_threshold"] = BULLISH_PROBABILITY
        row["direction_down_threshold"] = BEARISH_PROBABILITY
    plan["policy_revision"] = revision
    plan["limitations"].append("Policy correction uses the original saved account observation; live execution must use actual current quotes, cash and holdings.")
    _validated_instructions(pd.DataFrame(plan["forecasts"]))
    _seal(plan)
    run = root / RUNS / plan["plan_sha256"]
    revised_binding = {**deepcopy(binding), "plan_sha256": plan["plan_sha256"],
                       "run_path": run.relative_to(root).as_posix(), "accepted_at": revised.isoformat(),
                       "policy_revision": revision}
    with exclusive_runtime_lock(root / "locks/joint-gameplan-adoption.lock", process_name="gameplan-policy-revision"):
        if pointer.read_bytes() != pointer_bytes:
            raise ValueError("Accepted plan selection changed during policy revision")
        for path, digest in selected_files:
            _read(path, digest)
        run.mkdir(parents=True, exist_ok=True)
        for name, value in (("joint-plan.json", plan), ("accepted-plan.json", revised_binding)):
            target, data = run / name, _json_bytes(value)
            _ordinary_path(target, root / RUNS, missing_leaf=True)
            if target.exists():
                if target.read_bytes() != data:
                    raise ValueError("Immutable policy revision artifact already differs")
            else:
                with target.open("xb") as stream:
                    stream.write(data)
        history = run / "previous-accepted-plan.json"
        if history.exists() and history.read_bytes() != pointer_bytes:
            raise ValueError("Policy revision parent receipt already differs")
        if not history.exists():
            with history.open("xb") as stream:
                stream.write(pointer_bytes)
        _write_json_atomic(pointer, revised_binding)
    return run
