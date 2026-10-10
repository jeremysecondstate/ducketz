"""Scout UI selection changes only a temporary datastore's display pointer."""
from dataclasses import replace
from datetime import date, timedelta
import json
from pathlib import Path
import shutil
from types import SimpleNamespace

from filelock import FileLock, Timeout
import pytest

from ml.account_gameplan import cli, views
from ml.account_gameplan.config import CONFIG, VERSION, digest, load_account_config
from ml.account_gameplan.planner import sha
from test_account_gameplan_config import write_config
from test_account_gameplan_selection import selected


@pytest.fixture
def scout(selected):
    _, plan, cases = selected
    root = cases[1].root
    write_config(root, machine="pc-new")
    run = root / "ml/account-gameplan-runs/transported"
    shutil.copytree(plan.path, run)
    return SimpleNamespace(root=root, run=run, pin=plan.manifest_sha256,
                           config=load_account_config(root), day=plan.report["action_date"])


def invoke(env, **changes):
    args = dict(run=env.run, manifest_sha256=env.pin, expected_config=env.config.fingerprint,
                expected_previous_pointer_sha256=None)
    args.update(changes)
    return views.select_view(env.root, **args)


def pointer(env):
    return env.root / views.POINTER


def test_select_view_verifies_native_artifact_and_renders_only_scout_without_execution_authority(scout):
    from app.ui.gameplan_data import load_gameplan
    env = scout
    before = {p: p.read_bytes() for p in env.root.rglob("*") if p.is_file()}
    result = invoke(env)
    assert result["status"] == "SELECTED" and result["selection_changed"]
    saved = json.loads(pointer(env).read_bytes())
    assert saved == {"schema_version": VERSION, "status": "SELECTED", "config_sha256": env.config.fingerprint,
        "producer_id": "pc-new", "action_date": env.day, "current": {
            "run_path": "ml/account-gameplan-runs/transported", "manifest_sha256": env.pin, "action_date": env.day}}
    assert result["pointer_sha256"] == sha(pointer(env).read_bytes())
    assert not (env.root / "ml/account-gameplan-by-date").exists()
    assert result["execution_selection_changed"] is result["activation_changed"] is result["broker_orders_enabled"] is False
    assert result["orders_placed"] == 0
    assert all(p.read_bytes() == raw for p, raw in before.items())
    view = load_gameplan(env.root)
    assert len(view.forecasts) == 24 and {r.symbol for r in view.forecasts} == {"MU"}


def test_same_exact_view_is_idempotent_only_with_current_pointer_pin(scout):
    env = scout
    first = invoke(env)
    raw = pointer(env).read_bytes()
    with pytest.raises(ValueError, match="Previous view pointer"):
        invoke(env)
    second = invoke(env, expected_previous_pointer_sha256=first["pointer_sha256"])
    assert second["selection_changed"] is False and pointer(env).read_bytes() == raw


@pytest.mark.parametrize("failure", ["coordinator", "missing_config", "wrong_config", "wrong_manifest", "wrong_account", "wrong_membership", "outside", "absent_mismatch"])
def test_wrong_artifact_or_binding_has_no_pointer_side_effect(scout, failure):
    env = scout
    args = {}
    if failure == "coordinator": write_config(env.root, machine="pc-original")
    elif failure == "missing_config": (env.root / CONFIG).unlink()
    elif failure == "wrong_config": args["expected_config"] = "0" * 64
    elif failure == "wrong_manifest": args["manifest_sha256"] = "0" * 64
    elif failure in {"wrong_account", "wrong_membership"}:
        raw = json.loads((env.root / CONFIG).read_bytes())
        if failure == "wrong_account": raw["account_fingerprint"] = "0" * 64
        else: raw["participants"]["pc-new"] = ["TSLA"]
        raw["activation"]["binding_sha256"] = digest({k: v for k, v in raw.items() if k != "activation"})
        (env.root / CONFIG).write_text(json.dumps(raw))
        args["expected_config"] = load_account_config(env.root).fingerprint
    elif failure == "outside": args["run"] = env.root / "transported"
    else: args["expected_previous_pointer_sha256"] = "0" * 64
    with pytest.raises((ValueError, OSError)):
        invoke(env, **args)
    assert not pointer(env).exists()


def test_existing_foreign_or_malformed_pointer_requires_explicit_review(scout):
    env = scout
    pointer(env).parent.mkdir(parents=True)
    pointer(env).write_bytes(b"{}")
    with pytest.raises(ValueError, match="incompatible"):
        invoke(env, expected_previous_pointer_sha256=sha(b"{}"))
    assert pointer(env).read_bytes() == b"{}"


def test_same_date_conflicting_generation_is_rejected_even_with_valid_hash(scout):
    env = scout
    first = invoke(env)
    other = env.run.with_name("different-generation")
    shutil.copytree(env.run, other)
    with pytest.raises(ValueError, match="same-date generation"):
        invoke(env, run=other, expected_previous_pointer_sha256=first["pointer_sha256"])


def advanced_candidate(env, monkeypatch, *, delta):
    """Isolate selection chronology; real source verification has separate tests."""
    original = views._plan
    run = env.run.with_name("another-session")
    shutil.copytree(env.run, run)
    def plan(root, value, pin, cfg):
        result = original(root, value, pin, cfg)
        if result.path == run:
            report = {**result.report, "action_date": (date.fromisoformat(env.day) + timedelta(days=delta)).isoformat()}
            return replace(result, report=report)
        return result
    monkeypatch.setattr(views, "_plan", plan)
    return run


def test_action_date_cannot_move_backwards(scout, monkeypatch):
    env = scout
    first = invoke(env)
    earlier = advanced_candidate(env, monkeypatch, delta=-1)
    with pytest.raises(ValueError, match="downgrade"):
        invoke(env, run=earlier, expected_previous_pointer_sha256=first["pointer_sha256"])


def test_future_report_advances_display_without_dated_execution_selection(scout, monkeypatch):
    env = scout
    first = invoke(env)
    future = advanced_candidate(env, monkeypatch, delta=1)
    result = invoke(env, run=future, expected_previous_pointer_sha256=first["pointer_sha256"])
    assert result["action_date"] > env.day
    assert not (env.root / "ml/account-gameplan-by-date").exists()


@pytest.mark.parametrize("prior", [False, True])
@pytest.mark.parametrize("failure", ["config", "source", "replace_exception"])
def test_postwrite_failure_restores_only_invocation_owned_view(scout, monkeypatch, prior, failure):
    env = scout
    args = {}
    if prior:
        first = invoke(env)
        args.update(expected_previous_pointer_sha256=first["pointer_sha256"],
                    run=advanced_candidate(env, monkeypatch, delta=1))
    before = pointer(env).read_bytes() if prior else None
    original = views.os.replace
    def changed(source, target):
        original(source, target)
        if Path(target) == pointer(env) and ".view-restore-" not in Path(source).name:
            if failure == "config":
                value = json.loads((env.root / CONFIG).read_bytes())
                value["activation"]["status"] = "ACTIVE"
                (env.root / CONFIG).write_text(json.dumps(value))
            elif failure == "source":
                (args.get("run", env.run) / "receipt.json").write_text("{}")
            else:
                raise OSError("replace reported failure after committing")
    monkeypatch.setattr(views.os, "replace", changed)
    with pytest.raises((OSError, ValueError)):
        invoke(env, **args)
    assert (pointer(env).read_bytes() if pointer(env).exists() else None) == before


def test_foreign_postwrite_change_is_preserved_and_rollback_explicitly_incomplete(scout, monkeypatch):
    env = scout
    original = views.os.replace
    def changed(source, target):
        original(source, target)
        if Path(target) == pointer(env):
            pointer(env).write_bytes(b"independent pointer bytes")
    monkeypatch.setattr(views.os, "replace", changed)
    with pytest.raises(RuntimeError, match="rollback incomplete"):
        invoke(env)
    assert pointer(env).read_bytes() == b"independent pointer bytes"


def test_local_lock_serializes_view_writers(scout):
    env = scout
    path = env.root / views.LOCK
    path.parent.mkdir(parents=True, exist_ok=True)
    with FileLock(str(path)):
        with pytest.raises(Timeout):
            invoke(env)
    assert not pointer(env).exists()


def test_cli_select_view_requires_explicit_absence_or_previous_hash(scout, capsys):
    env = scout
    args = ["select-view", "--datastore-root", str(env.root), "--run", str(env.run),
            "--manifest-sha256", env.pin, "--expected-config", env.config.fingerprint,
            "--expected-previous-pointer-sha256", "absent"]
    assert cli.main(args) == 0
    assert json.loads(capsys.readouterr().out)["status"] == "SELECTED"
    assert cli.main(args) == 2
    assert json.loads(capsys.readouterr().err)["status"] == "ERROR"


def test_registry_cli_delegates_exact_explicit_pins_without_transport(tmp_path, monkeypatch, capsys):
    records = [{"producer_id": p, "symbols": [s], "bundle_path": p + "/bundle",
                "manifest_sha256": "b" * 64}
        for p, s in (("pc-original", "AAPL"), ("pc-new", "MU"))]
    document = tmp_path / "registry.json"
    document.write_text(json.dumps({"sources": records}))
    called = []
    def register(root, **kwargs):
        called.append((root, kwargs))
        return {"action_date": "2026-10-05", "sources": kwargs["sources"]}
    monkeypatch.setattr("ml.account_gameplan.preparation.register_inputs", register)
    assert cli.main(["register-inputs", "--datastore-root", str(tmp_path), "--action-date", "2026-10-05",
                     "--sources", str(document), "--expected-config", "a" * 64]) == 0
    assert called == [(tmp_path, {"action_date": "2026-10-05", "sources": records, "expected_config": "a" * 64})]
    assert json.loads(capsys.readouterr().out)["status"] == "REGISTERED"
