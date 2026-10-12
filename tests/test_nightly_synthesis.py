from copy import deepcopy
import json
from pathlib import Path

import pytest

from app.ui.gameplan_data import load_gameplan
from app.ui.gameplan_stats_data import load_gameplan_stats
from ml.artifacts import file_checksum
from ml.gameplan_stats_handoff import export_stats_package
from ml.joint_capital_adoption import assert_accepted_execution
from ml.joint_capital_plan import publish_owner_package
from ml.nightly_synthesis import VERSION, preflight_synthesis, run_synthesis
from tests.gameplan_stats_fixture import forecast, write_review
from tests.test_joint_capital_plan import package, snapshot, DAY, NOW, SCOPE
from tests.test_gameplan_stats_handoff import baseline_package


@pytest.fixture
def specification(tmp_path):
    sources = tmp_path / "sources"
    sources.mkdir()
    owners = {}
    for actor, symbol in (("atlas", "AAPL"), ("scout", "ABCL")):
        payload = package(actor, symbol)
        plan_path = publish_owner_package(sources, payload)
        stats_root = tmp_path / f"stats-{actor}"
        write_review(stats_root, [forecast(symbol, session="2026-09-08")], session="2026-09-08")
        stats_path = export_stats_package(stats_root, producer=actor.title(), symbols=[symbol],
                                           destination=sources / f"{actor}-stats.json")
        owners[actor] = {"symbols": [symbol],
            "plan_package": {"root": str(sources), "path": str(plan_path), "file_sha256": file_checksum(plan_path),
                             "package_sha256": payload["package_sha256"]},
            "stats_package": {"path": str(stats_path), "file_sha256": file_checksum(stats_path)}}
    account = sources / "private-snapshot.json"
    account.write_text(json.dumps(snapshot()))
    return {"schema_version": VERSION, "completion_id": "20260909-synthesis-fixture", "datastore_root": str(tmp_path / "local"),
        "state_root": str(tmp_path / "state"), "local_actor": "scout", "executor_owner": "atlas",
        "action_date": DAY, "review_session": "2026-09-08", "account_scope_sha256": SCOPE,
        "as_of": NOW, "accepted_at": NOW, "snapshot": {"path": str(account), "file_sha256": file_checksum(account)},
        "owners": owners}


def journal(spec):
    return Path(spec["state_root"]) / spec["completion_id"] / "state.json"


def test_preflight_verifies_everything_without_outputs(specification):
    result = preflight_synthesis(specification, now=NOW)
    assert result["plan"]["status"] == "COMPLETE"
    assert set(result["plan"]["input_bindings"]) == {"atlas", "scout"}
    assert not Path(specification["state_root"]).exists()
    assert not Path(specification["datastore_root"]).exists()


@pytest.mark.parametrize("missing", [("scout",), ("atlas", "scout")])
def test_synthesis_accepts_verified_baselines_and_preserves_missing_history(specification, tmp_path, missing):
    spec = specification
    for actor in missing:
        path, _ = baseline_package(tmp_path, actor.title(), spec["owners"][actor]["symbols"],
            session=spec["review_session"], reviewed_at=NOW)
        spec["owners"][actor]["stats_package"] = {"path": str(path), "file_sha256": file_checksum(path)}
    result = run_synthesis(spec, now=NOW)
    assert result["joint_ready"] and result["ui_ready"] and not result["execution_authorized"]
    review = load_gameplan_stats(Path(spec["datastore_root"]))
    report = json.loads((review.run_directory / "report.json").read_text())
    assert report["missing_history_owners"] == [actor.title() for actor in missing]
    assert review.metrics().total == (0 if len(missing) == 2 else 1)
    assert run_synthesis(spec, now=NOW) == result


def test_scout_combined_ui_completion_is_local_and_idempotent(specification):
    spec = specification
    result = run_synthesis(spec, now=NOW)
    assert result["status"] == "JOINT_READY_LOCAL" and result["ui_ready"] and result["joint_ready"]
    assert result["peer_verified"] is False and result["execution_authorized"] is False
    assert result["orders_placed"] == 0 and result["activation_changed"] is False
    root = Path(spec["datastore_root"])
    assert set(load_gameplan(root).symbols) == {"AAPL", "ABCL"}
    assert set(load_gameplan_stats(root).symbols) == {"AAPL", "ABCL"}
    with pytest.raises(ValueError, match="not its execution owner"):
        assert_accepted_execution(root, result["ui_evidence"]["plan_run"], action_date=DAY, account_scope_sha256=SCOPE)
    assert not (root / "controls").exists()
    receipt = Path(result["receipt_path"])
    before = receipt.read_bytes()
    runs = tuple((root / "ml/gameplan-actuals-review-runs").iterdir())
    assert run_synthesis(spec, now=NOW) == result
    assert receipt.read_bytes() == before
    assert tuple((root / "ml/gameplan-actuals-review-runs").iterdir()) == runs


def test_invalid_stats_prevents_either_local_adoption(specification):
    spec = specification
    path = Path(spec["owners"]["scout"]["stats_package"]["path"])
    payload = json.loads(path.read_text())
    payload["rows"][0]["model_brier_score"] = .99
    path.write_text(json.dumps(payload))
    spec["owners"]["scout"]["stats_package"]["file_sha256"] = file_checksum(path)
    with pytest.raises(ValueError, match="probability error"):
        run_synthesis(spec, now=NOW)
    root = Path(spec["datastore_root"])
    assert not (root / "ml/joint-gameplan-by-date").exists()
    assert not (root / "ml/gameplan-actuals-review-latest").exists()


def test_partial_adoption_resumes_without_replacing_the_plan(specification, monkeypatch):
    from ml import nightly_synthesis as module
    spec = specification
    original = module._adopt_stats
    def stop_local(root, *args):
        if root == Path(spec["datastore_root"]):
            raise RuntimeError("fixture interruption after plan adoption")
        return original(root, *args)
    monkeypatch.setattr(module, "_adopt_stats", stop_local)
    with pytest.raises(RuntimeError, match="fixture interruption"):
        run_synthesis(spec, now=NOW)
    state = json.loads(journal(spec).read_text())
    assert state["status"] == "PARTIAL_ADOPTION"
    plan = Path(state["steps"]["plan_adopted"]) / "accepted-plan.json"
    before = plan.read_bytes()
    monkeypatch.setattr(module, "_adopt_stats", original)
    completed = run_synthesis(spec, now=NOW)
    assert completed["status"] == "JOINT_READY_LOCAL" and plan.read_bytes() == before


def test_crash_after_stats_pointer_reuses_that_exact_stats_run(specification, monkeypatch):
    from ml import nightly_synthesis as module
    spec = specification
    original = module._adopt_stats
    def adopt_then_fail(root, *args):
        run = original(root, *args)
        if root == Path(spec["datastore_root"]):
            raise RuntimeError("fixture crash before journal checkpoint")
        return run
    monkeypatch.setattr(module, "_adopt_stats", adopt_then_fail)
    with pytest.raises(RuntimeError, match="fixture crash"):
        run_synthesis(spec, now=NOW)
    review = load_gameplan_stats(Path(spec["datastore_root"]))
    monkeypatch.setattr(module, "_adopt_stats", original)
    result = run_synthesis(spec, now=NOW)
    assert result["ui_evidence"]["stats_run"] == str(review.run_directory)


def test_crash_after_plan_pointer_is_recorded_as_partial_adoption(specification, monkeypatch):
    from ml import nightly_synthesis as module
    spec = specification
    original = module._adopt_plan
    def adopt_then_fail(root, *args):
        run = original(root, *args)
        if root == Path(spec["datastore_root"]):
            raise RuntimeError("fixture crash before plan checkpoint")
        return run
    monkeypatch.setattr(module, "_adopt_plan", adopt_then_fail)
    with pytest.raises(RuntimeError, match="fixture crash"):
        run_synthesis(spec, now=NOW)
    assert json.loads(journal(spec).read_text())["status"] == "PARTIAL_ADOPTION"
    monkeypatch.setattr(module, "_adopt_plan", original)
    assert run_synthesis(spec, now=NOW)["status"] == "JOINT_READY_LOCAL"


@pytest.mark.parametrize("damage,message", [
    ("snapshot", "reviewed SHA"), ("session", "precede the selected"),
    ("actor", "sole executor"), ("scope", "account scope"),
])
def test_explicit_selection_and_role_guards(specification, damage, message):
    spec = specification
    if damage == "snapshot":
        spec["snapshot"]["file_sha256"] = "f" * 64
    elif damage == "session":
        spec["review_session"] = "2026-09-04"
    elif damage == "actor":
        spec["executor_owner"] = "scout"
    else:
        spec["account_scope_sha256"] = "f" * 64
    with pytest.raises(ValueError, match=message):
        preflight_synthesis(spec, now=NOW)


def test_retry_spec_change_is_rejected_before_mutation(specification):
    spec = specification
    run_synthesis(spec, now=NOW)
    before = journal(spec).read_bytes()
    changed = deepcopy(spec)
    changed["accepted_at"] = "2026-09-09T10:01:00Z"
    with pytest.raises(ValueError, match="exact local specification"):
        run_synthesis(changed, now=NOW)
    assert journal(spec).read_bytes() == before


def test_changed_selected_package_does_not_reuse_completed_receipt(specification):
    spec = specification
    run_synthesis(spec, now=NOW)
    path = Path(spec["owners"]["scout"]["plan_package"]["path"])
    path.write_bytes(path.read_bytes() + b" ")
    with pytest.raises(ValueError, match="reviewed SHA"):
        run_synthesis(spec, now=NOW)


@pytest.mark.parametrize("damage,message", [
    ("rows", "rows differ from their selected source"),
    ("coverage", "coverage or provenance differs"),
    ("provenance", "coverage or provenance differs"),
])
def test_reused_combined_stats_must_match_original_sources_not_just_claimed_hashes(specification, damage, message):
    import pandas as pd
    from ml.artifacts import write_manifest
    spec = specification
    result = run_synthesis(spec, now=NOW)
    root = Path(spec["datastore_root"])
    run = Path(result["ui_evidence"]["stats_run"])
    if damage == "rows":
        frame = pd.read_parquet(run / "forecast-results.parquet")
        frame.loc[0, "actual_return"] = -.02  # Still valid bearish/Brier evidence, but different from the source.
        frame.to_parquet(run / "forecast-results.parquet", index=False)
    else:
        report = json.loads((run / "report.json").read_text())
        if damage == "coverage":
            report["missing_history_owners"] = ["Scout"]
        else:
            report["producers"]["Scout"]["source_report_sha256"] = "a" * 64
        (run / "report.json").write_text(json.dumps(report))
    manifest = json.loads((run / "manifest.json").read_text())
    write_manifest(run, run_timestamp=manifest["run_timestamp"], input_files=[],
                   output_files=list(manifest["output_files"]), configuration=manifest["configuration"])
    receipt = json.loads((run / "receipt.json").read_text())
    receipt["manifest_sha256"] = file_checksum(run / "manifest.json")
    (run / "receipt.json").write_text(json.dumps(receipt))
    for path in (root / "ml/gameplan-actuals-review-latest/run.json",
                 root / "ml/gameplan-actuals-review-by-date/2026-09-08/run.json"):
        pointer = json.loads(path.read_text())
        pointer["current"]["receipt_sha256"] = file_checksum(run / "receipt.json")
        path.write_text(json.dumps(pointer))
    assert load_gameplan_stats(root).metrics().accuracy == 1
    with pytest.raises(ValueError, match=message):
        run_synthesis(spec, now=NOW)


@pytest.fixture
def cli_inputs(specification, tmp_path, monkeypatch):
    from ml import nightly_synthesis as module
    checkout = tmp_path / "checkout"
    profile = checkout / "scratch/cross-pc/local-profile.json"
    profile.parent.mkdir(parents=True)
    definition = {"contract_version": "cross-pc-v2", "actor": "Scout", "checkout": str(checkout), "symbols": ["ABCL"]}
    profile.write_text(json.dumps(definition))
    spec_path = tmp_path / "synthesis-spec.json"
    spec_path.write_text(json.dumps(specification))
    monkeypatch.setattr(module, "__file__", str(checkout / "ml/nightly_synthesis.py"))
    return module, spec_path, profile, definition


@pytest.mark.parametrize("validate_only", [False, True])
def test_cli_binds_valid_local_profile_before_dispatch(cli_inputs, monkeypatch, validate_only):
    module, spec_path, profile, _ = cli_inputs
    calls = []
    def execute(spec):
        calls.append(spec["local_actor"])
        return {"status": "JOINT_READY_LOCAL", "completion_id": spec["completion_id"], "ui_ready": True,
                "joint_ready": True, "receipt_path": "fixture-only", "orders_placed": 0}
    def validate(spec):
        calls.append(spec["local_actor"])
        return {"plan": {"plan_sha256": "a" * 64}}
    monkeypatch.setattr(module, "run_synthesis", execute)
    monkeypatch.setattr(module, "preflight_synthesis", validate)
    arguments = ["--spec", str(spec_path), "--local-profile", str(profile)]
    assert module.main(arguments + (["--validate-only"] if validate_only else [])) == 0
    assert calls == ["scout"]


@pytest.mark.parametrize("damage,message", [
    ("actor", "belongs to Scout"), ("symbols", "local symbols differ"),
    ("checkout", "this checkout's installed"), ("uninstalled_profile", "this checkout's installed"),
    ("relative_profile", "absolute local paths"),
])
def test_cli_profile_mismatch_never_composes_or_adopts(cli_inputs, monkeypatch, damage, message):
    module, spec_path, profile, definition = cli_inputs
    if damage == "actor":
        spec = json.loads(spec_path.read_text())
        spec["local_actor"] = "atlas"
        spec_path.write_text(json.dumps(spec))
    elif damage == "symbols":
        definition["symbols"] = ["DOCU"]
        profile.write_text(json.dumps(definition))
    elif damage == "checkout":
        definition["checkout"] = str(profile.parent / "other-checkout")
        profile.write_text(json.dumps(definition))
    elif damage == "uninstalled_profile":
        other = profile.with_name("other-profile.json")
        other.write_bytes(profile.read_bytes())
        profile = other
    else:
        profile = Path("scratch/cross-pc/local-profile.json")
    monkeypatch.setattr(module, "preflight_synthesis", lambda *a, **kw: pytest.fail("Invalid profile cannot compose"))
    monkeypatch.setattr(module, "run_synthesis", lambda *a, **kw: pytest.fail("Invalid profile cannot adopt"))
    with pytest.raises(ValueError, match=message):
        module.main(["--spec", str(spec_path), "--local-profile", str(profile)])


def test_cli_requires_local_profile_argument(cli_inputs, monkeypatch):
    module, spec_path, _, _ = cli_inputs
    monkeypatch.setattr(module, "run_synthesis", lambda *a, **kw: pytest.fail("Missing profile cannot adopt"))
    with pytest.raises(SystemExit) as exc:
        module.main(["--spec", str(spec_path)])
    assert exc.value.code == 2


def test_atlas_cannot_compose_even_with_valid_selected_inputs(specification):
    specification["local_actor"] = "atlas"
    with pytest.raises(ValueError, match="belongs to Scout"):
        preflight_synthesis(specification, now=NOW)
    assert not Path(specification["datastore_root"]).exists()
    assert not Path(specification["state_root"]).exists()
