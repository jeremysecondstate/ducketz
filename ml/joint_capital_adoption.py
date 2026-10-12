"""Select a frozen combined plan locally, without activating trading or transport.

The acceptance record binds the session, producer packages, account scope and
local machine role. It selects instructions; existing operator controls and
current broker reconciliation still own execution authority.
"""
from __future__ import annotations

import argparse
import json
from datetime import date
from pathlib import Path

import pandas as pd

from datafetching.runtime_lock import exclusive_runtime_lock
from ml.joint_capital_plan import load_joint_plan, _validate_plan_digest, _json_bytes

VERSION = "accepted-joint-gameplan-v1"
RUNS = "ml/joint-gameplan-runs"
POINTERS = "ml/joint-gameplan-by-date"


def _day(value):
    return date.fromisoformat(str(value)).isoformat()


def _verify(plan, binding):
    _validate_plan_digest(plan, binding["plan_sha256"])
    if (binding.get("schema_version") != VERSION or plan.get("status") != "COMPLETE"
            or plan["action_date"] != binding["action_date"]
            or plan["account_scope_sha256"] != binding["account_scope_sha256"]
            or plan["sole_executor_owner"] != binding["executor_owner"]
            or binding["local_actor"] not in plan["input_bindings"]):
        raise ValueError("Combined plan session, account or machine binding disagrees")
    owners = plan["input_bindings"]
    if set(owners) != set(binding["owner_packages"]) or set(owners) != set(binding["owner_universes"]):
        raise ValueError("Combined plan must bind both selected producers")
    symbols = []
    for owner, source in owners.items():
        if (source["package_sha256"] != binding["owner_packages"][owner]
                or set(source["frozen_symbols"]) != set(binding["owner_universes"][owner])):
            raise ValueError("Combined plan source package or research universe changed")
        symbols.extend(source["frozen_symbols"])
    if len(symbols) != len(set(symbols)) or set(symbols) != set(binding["execution_symbols"]):
        raise ValueError("Combined execution universe must be the disjoint producer union")
    frame = pd.DataFrame(plan["forecasts"])
    from ml.stock_trader.gameplan_execution import _validated_instructions
    _validated_instructions(frame)
    if (frame.empty or frame.id.duplicated().any() or set(frame.symbol) != set(symbols)
            or not frame.action_date.eq(plan["action_date"]).all()):
        raise ValueError("Combined forecast identities, symbols or dates disagree")
    # This also verifies event/forecast quantities, clocks, horizons and fallback
    # attribution using the same contract that the existing Gameplan tab uses.
    from app.ui.gameplan_data import _forecast, _actions
    _actions(plan["ledger"], tuple(_forecast(row) for row in plan["forecasts"]), plan["action_date"])


def accepted_sessions(root: Path) -> tuple[str, ...]:
    return tuple(sorted((_day(path.parent.name) for path in (Path(root) / POINTERS).glob("*/run.json")), reverse=True))


def read_accepted_joint_plan(root: Path, action_date: str):
    """Return (plan, binding, immutable run) or None when nothing is selected."""
    root = Path(root).resolve()
    pointer = root / POINTERS / _day(action_date) / "run.json"
    if not pointer.exists():
        return None
    binding = json.loads(pointer.read_text(encoding="utf-8"))
    run = (root / str(binding.get("run_path", ""))).resolve()
    if run.parent != (root / RUNS).resolve():
        raise ValueError("Accepted combined plan escapes its immutable directory")
    receipt = json.loads((run / "accepted-plan.json").read_text(encoding="utf-8"))
    if binding != receipt or binding["action_date"] != action_date:
        raise ValueError("Combined plan acceptance pointer and receipt disagree")
    plan = load_joint_plan(run, run / "joint-plan.json", expected_sha256=binding["plan_sha256"])
    _verify(plan, binding)
    return plan, binding, run


def accept_joint_plan(datastore_root: Path, plan: dict, *, expected_sha256: str,
                      expected_package_sha256: dict, expected_universes: dict,
                      account_scope_sha256: str, local_actor: str, executor_owner: str,
                      action_date: str, accepted_at: str) -> Path:
    """Install only explicitly selected frozen bytes. A session selection is immutable.

    Retry is idempotent. A different plan for the same session is intentionally
    not silently substituted after execution may have consumed its instructions.
    """
    root, day = Path(datastore_root).resolve(), _day(action_date)
    stamp = pd.Timestamp(accepted_at)
    if pd.isna(stamp) or stamp.tzinfo is None or stamp < pd.Timestamp(plan["as_of"]):
        raise ValueError("Acceptance requires a zoned timestamp after synthesis")
    run = root / RUNS / expected_sha256
    binding = {"schema_version": VERSION, "action_date": day, "plan_sha256": expected_sha256,
        "run_path": run.relative_to(root).as_posix(), "accepted_at": stamp.isoformat(),
        "owner_packages": dict(expected_package_sha256), "owner_universes": dict(expected_universes),
        "execution_symbols": sorted(symbol for values in expected_universes.values() for symbol in values),
        "account_scope_sha256": account_scope_sha256, "local_actor": local_actor,
        "executor_owner": executor_owner, "orders_placed": 0, "activation_changed": False}
    _verify(plan, binding)
    with exclusive_runtime_lock(root / "locks/joint-gameplan-adoption.lock", process_name="joint-gameplan-adoption"):
        previous = read_accepted_joint_plan(root, day)
        if previous:
            if previous[1]["plan_sha256"] != expected_sha256:
                raise ValueError("This session already has a different accepted combined plan")
            for key in ("owner_packages", "owner_universes", "account_scope_sha256", "local_actor", "executor_owner"):
                if previous[1][key] != binding[key]:
                    raise ValueError("Acceptance retry changed its local authority or source bindings")
            return previous[2]
        run.mkdir(parents=True, exist_ok=True)
        for name, value in (("joint-plan.json", plan), ("accepted-plan.json", binding)):
            path, data = run / name, _json_bytes(value)
            if path.exists():
                if path.read_bytes() != data:
                    raise ValueError("An immutable combined-plan artifact already differs")
            else:
                with path.open("xb") as stream:
                    stream.write(data)
        from ml.stock_trader.publication import _write_json_atomic
        _write_json_atomic(root / POINTERS / day / "run.json", binding)
    return run


def assert_accepted_execution(root, run, *, action_date, account_scope_sha256=None):
    selected = read_accepted_joint_plan(root, action_date)
    if selected is None:
        return False
    _, binding, expected = selected
    if run is None or Path(run).resolve() != expected:
        raise ValueError("The loaded source differs from the accepted combined plan")
    if binding["local_actor"] != binding["executor_owner"]:
        raise ValueError("This machine displays the combined plan but is not its execution owner")
    if account_scope_sha256 is not None and binding["account_scope_sha256"] != account_scope_sha256:
        raise ValueError("Combined plan and live broker account fingerprints differ")
    return True


def verify_local_adoption_profile(spec: dict, local_profile: Path) -> None:
    """Bind an operator CLI request to this checkout's installed PC identity."""
    if not isinstance(spec, dict) or not isinstance(spec.get("expected_universes"), dict):
        raise ValueError("Adoption requires explicit producer research universes")
    if not local_profile.is_absolute():
        raise ValueError("Local profile must be an explicit absolute path")
    profile = json.loads(local_profile.read_text(encoding="utf-8"))
    if not isinstance(profile, dict) or profile.get("actor") not in ("Scout", "Atlas"):
        raise ValueError("Local profile has no recognized PC identity")
    checkout = profile.get("checkout")
    if (not isinstance(checkout, str) or not Path(checkout).is_absolute()
            or Path(checkout).resolve() != Path(__file__).resolve().parents[1]):
        raise ValueError("Local profile belongs to a different checkout")
    actor = profile["actor"].lower()
    if spec.get("local_actor") != actor:
        raise ValueError("Adoption actor differs from this PC's local profile")
    symbols = profile.get("symbols")
    expected = spec.get("expected_universes", {}).get(actor)
    if (not isinstance(symbols, list) or not symbols
            or any(not isinstance(symbol, str) or not symbol for symbol in symbols)
            or len(symbols) != len(set(symbols)) or not isinstance(expected, list)
            or any(not isinstance(symbol, str) or not symbol for symbol in expected)
            or len(expected) != len(set(expected)) or set(expected) != set(symbols)):
        raise ValueError("Adoption research universe differs from this PC's local profile")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--spec", type=Path, required=True, help="Exact local adoption specification; never incoming executable instructions")
    parser.add_argument("--local-profile", type=Path, required=True, help="This checkout's installed private coordination profile")
    args = parser.parse_args(argv)
    spec = json.loads(args.spec.read_text(encoding="utf-8"))
    verify_local_adoption_profile(spec, args.local_profile)
    plan = load_joint_plan(Path(spec.pop("plan_root")), Path(spec.pop("plan_path")), expected_sha256=spec["expected_sha256"])
    run = accept_joint_plan(Path(spec.pop("datastore_root")), plan, **spec)
    print(json.dumps({"status": "ACCEPTED_LOCALLY", "run_path": str(run), "orders_placed": 0}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
