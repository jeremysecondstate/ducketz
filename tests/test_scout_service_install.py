"""Exact-byte local composition preserves legacy installations and recipes."""
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

from tools import scout_release as releases
from tools import scout_service_install as install


def make_release(tmp_path):
    root = tmp_path / "legacy"
    source = root / "source"
    source.mkdir(parents=True)
    inventory = {}
    for relative, raw in {"model.py": b"VALUE = 'original-model'\n", "unowned.py": b"UNOWNED = 1\n",
                          "ml/intraday/live_session.py": b"# previous model-only lifecycle\n",
                          **{"tools/" + name: ("# " + name + "\n").encode() for name in install.BOOTSTRAP_FILES}}.items():
        path = source / relative
        path.parent.mkdir(exist_ok=True, parents=True)
        path.write_bytes(raw)
        inventory[relative] = releases.sha256(raw)
    recipe = root / "recipe.json"
    recipe.write_text(json.dumps({"pc": "Scout", "symbols": ["SYM" + str(i) for i in range(11)],
        "frozen_model": {"hyperparameters": [9, 17, 33], "missing_data_policy": "preserve"}}), encoding="utf-8")
    releases.atomic_json(root / "manifest.json", {"source_root": str(source), "lifecycle_protocol": releases.PROTOCOL,
        "files": inventory, "recipe_path": str(recipe), "recipe_file_sha256": releases.sha256(recipe.read_bytes()),
        "original_repository": str(tmp_path / "original")})
    return releases.validate_release(root, releases.sha256((root / "manifest.json").read_bytes()))


def specification(tmp_path):
    previous = make_release(tmp_path)
    source = tmp_path / "reviewed"
    source.mkdir()
    (source / "model.py").write_bytes(b"VALUE = 'reviewed-service-adapter'\n")
    (source / "service.py").write_bytes(b"SERVICE = True\n")
    ops = [{"path": "model.py", "operation": "modify",
        "before_sha256": previous["manifest"]["files"]["model.py"], "sha256": releases.sha256((source / "model.py").read_bytes())},
        {"path": "service.py", "operation": "add", "before_sha256": None,
         "sha256": releases.sha256((source / "service.py").read_bytes())}]
    return previous, dict(predecessor_root=previous["release_root"], predecessor_manifest_sha256=previous["manifest_sha256"],
        reviewed_source=source, operations=ops, destination=tmp_path / "independent",
        completion_record="offline-fixture-completion", checks=[{"command": ["python", "-B", "-m", "pytest"],
            "exit_code": 0, "output_sha256": "1" * 64, "completed_at": "2026-10-27T00:00:00+00:00"}])


def test_compose_preserves_private_recipe_unowned_source_and_legacy_selection(tmp_path):
    previous, spec = specification(tmp_path)
    legacy_selector = tmp_path / "legacy-selected.json"
    releases.select_release(legacy_selector, previous["release_root"], previous["manifest_sha256"])
    before = legacy_selector.read_bytes()
    selected = install.compose_release(**spec)
    assert Path(selected["recipe_path"]).read_bytes() == Path(previous["recipe_path"]).read_bytes()
    assert selected["recipe_file_sha256"] == previous["recipe_file_sha256"]
    assert (Path(selected["source_root"]) / "unowned.py").read_bytes() == b"UNOWNED = 1\n"
    assert (Path(previous["source_root"]) / "model.py").read_bytes() == b"VALUE = 'original-model'\n"
    assert legacy_selector.read_bytes() == before
    with pytest.raises(ValueError, match="different controller protocol"):
        releases.validate_release(selected["release_root"], selected["manifest_sha256"])
    independent_selector = tmp_path / "independent-selected.json"
    installed = install.select_independent_release(independent_selector, selected)
    assert installed["lifecycle_protocol"] == install.PROTOCOL
    assert releases.resolve_selection(legacy_selector)["lifecycle_protocol"] == releases.PROTOCOL
    assert selected["manifest"]["independent_services_installation"]["trader_started"] is False


def test_independent_selector_cannot_replace_v1_selector(tmp_path):
    previous, spec = specification(tmp_path)
    selector = tmp_path / "legacy-selected.json"
    releases.select_release(selector, previous["release_root"], previous["manifest_sha256"])
    before = selector.read_bytes()
    selected = install.compose_release(**spec)
    with pytest.raises(ValueError, match="preserve the legacy"):
        install.select_independent_release(selector, selected, expected_selection_sha256=releases.sha256(before))
    assert selector.read_bytes() == before


def test_rotation_capability_requires_supported_complete_source(tmp_path):
    _, spec = specification(tmp_path)
    with pytest.raises(ValueError, match="Unsupported trading rotation"):
        install.compose_release(**spec, trading_rotation_version="unknown")
    with pytest.raises(ValueError, match="complete worker/controller"):
        install.compose_release(**spec, trading_rotation_version="scout-trading-rotation-v1")
    assert not Path(spec["destination"]).exists()


def rotation_specification(tmp_path):
    previous, spec = specification(tmp_path)
    for name in ("ml/intraday/trading_rotation.py", "ml/intraday/live_session.py",
                 "tools/scout_service_controller.py"):
        path = Path(spec["reviewed_source"]) / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"# offline reviewed rotation integration fixture\n")
        old_hash = previous["manifest"]["files"].get(name)
        spec["operations"].append({"path": name, "operation": "add" if old_hash is None else "modify",
            "before_sha256": old_hash, "sha256": releases.sha256(path.read_bytes())})
    return previous, spec


def test_rotation_capability_is_pinned_without_changing_recipe(tmp_path):
    previous, spec = rotation_specification(tmp_path)
    selected = install.compose_release(**spec, trading_rotation_version="scout-trading-rotation-v1")
    assert selected["manifest"]["trading_rotation_version"] == "scout-trading-rotation-v1"
    assert Path(selected["recipe_path"]).read_bytes() == Path(previous["recipe_path"]).read_bytes()
    assert selected["lifecycle_protocol"] == install.PROTOCOL


@pytest.mark.parametrize("missing", ["ml/intraday/live_session.py", "tools/scout_service_controller.py"])
@pytest.mark.parametrize("unchanged_operation", [False, True])
def test_initial_rotation_rejects_old_integration_even_when_filenames_exist(tmp_path, missing, unchanged_operation):
    previous, spec = rotation_specification(tmp_path)
    operation = next(op for op in spec["operations"] if op["path"] == missing)
    if unchanged_operation:
        raw = (Path(previous["source_root"]) / missing).read_bytes()
        (Path(spec["reviewed_source"]) / missing).write_bytes(raw)
        operation["sha256"] = releases.sha256(raw)
    else:
        spec["operations"].remove(operation)
    with pytest.raises(ValueError, match="Initial trading rotation requires reviewed changed"):
        install.compose_release(**spec, trading_rotation_version="scout-trading-rotation-v1")
    assert not Path(spec["destination"]).exists()


def successor_specification(spec, selected, tmp_path, operations):
    return {**spec, "predecessor_root": selected["release_root"],
        "predecessor_manifest_sha256": selected["manifest_sha256"], "predecessor_protocol": install.PROTOCOL,
        "destination": tmp_path / "successor", "operations": operations}


def test_compatible_successor_inherits_rotation_capability(tmp_path):
    _, spec = rotation_specification(tmp_path)
    first = install.compose_release(**spec, trading_rotation_version="scout-trading-rotation-v1")
    raw = b"VALUE = 'later-compatible-fix'\n"
    (Path(spec["reviewed_source"]) / "model.py").write_bytes(raw)
    operations = [{"path": "model.py", "operation": "modify", "before_sha256": first["manifest"]["files"]["model.py"],
                   "sha256": releases.sha256(raw)}]
    second = install.compose_release(**successor_specification(spec, first, tmp_path, operations))
    assert second["manifest"]["trading_rotation_version"] == "scout-trading-rotation-v1"
    assert second["recipe_file_sha256"] == first["recipe_file_sha256"]


@pytest.mark.parametrize("missing", ["ml/intraday/trading_rotation.py", "ml/intraday/live_session.py",
                                     "tools/scout_service_controller.py"])
def test_capable_successor_cannot_delete_required_rotation_source(tmp_path, missing):
    _, spec = rotation_specification(tmp_path)
    first = install.compose_release(**spec, trading_rotation_version="scout-trading-rotation-v1")
    operations = [{"path": missing, "operation": "delete", "before_sha256": first["manifest"]["files"][missing],
                   "sha256": None}]
    second = successor_specification(spec, first, tmp_path, operations)
    with pytest.raises(ValueError, match="complete worker/controller"):
        install.compose_release(**second)
    assert not Path(second["destination"]).exists()
    assert releases.sha256((Path(first["source_root"]) / missing).read_bytes()) == first["manifest"]["files"][missing]


@pytest.mark.parametrize("fault", ["before_hash", "after_hash", "operation", "duplicate", "path", "checks"])
def test_changed_or_unreviewed_operation_cannot_install(tmp_path, fault):
    previous, spec = specification(tmp_path)
    if fault == "before_hash":
        spec["operations"][0]["before_sha256"] = "wrong"
    elif fault == "after_hash":
        (Path(spec["reviewed_source"]) / "model.py").write_bytes(b"later-unreviewed-mutation")
    elif fault == "operation":
        spec["operations"][0]["operation"] = "add"
    elif fault == "duplicate":
        spec["operations"].append(spec["operations"][0])
    elif fault == "path":
        spec["operations"][0]["path"] = "../outside.py"
    else:
        spec["checks"][0]["exit_code"] = 1
    with pytest.raises(ValueError):
        install.compose_release(**spec)
    assert not Path(spec["destination"]).exists()
    assert releases.validate_release(previous["release_root"], previous["manifest_sha256"])


def test_reviewed_deletion_is_explicit_and_keeps_predecessor(tmp_path):
    previous, spec = specification(tmp_path)
    spec["operations"].append({"path": "unowned.py", "operation": "delete",
        "before_sha256": previous["manifest"]["files"]["unowned.py"], "sha256": None})
    selected = install.compose_release(**spec)
    assert "unowned.py" not in selected["manifest"]["files"]
    assert not (Path(selected["source_root"]) / "unowned.py").exists()
    assert (Path(previous["source_root"]) / "unowned.py").exists()


def test_bootstrap_is_pinned_and_has_no_activation_effect(tmp_path):
    previous, spec = specification(tmp_path)
    selected = install.compose_release(**spec)
    selector = tmp_path / "independent-selected.json"
    bootstrap = install.install_bootstrap(selected=selected, destination=tmp_path / "bootstrap", repository=tmp_path,
        datastore=tmp_path / "private", selector=selector, completion_record="offline-completion")
    assert not selector.exists()
    assert not (tmp_path / "private").exists()
    assert bootstrap["trader_started"] is False and bootstrap["system_1a_started"] is False
    root = Path(bootstrap["bootstrap_root"])
    for name, expected in bootstrap["files"].items():
        assert releases.sha256((root / name).read_bytes()) == expected
    assert b"--live" in (root / "launch.py").read_bytes()
    assert bootstrap["protocol"] == install.PROTOCOL


def test_selection_compare_and_swap_covers_independent_installers(tmp_path):
    _, spec = specification(tmp_path)
    selected = install.compose_release(**spec)
    selector = tmp_path / "independent-selected.json"
    install.select_independent_release(selector, selected)
    before = selector.read_bytes()
    with pytest.raises(ValueError, match="changed since installer review"):
        install.select_independent_release(selector, selected, expected_selection_sha256="wrong")
    assert selector.read_bytes() == before


def test_complete_directory_publication_retries_transient_windows_sharing(tmp_path, monkeypatch):
    _, spec = specification(tmp_path)
    replace = install.os.replace
    attempts = []
    def sharing_then_success(source, destination):
        if Path(source).is_dir():
            attempts.append(Path(source))
            assert not Path(destination).exists()
            assert (Path(source) / "manifest.json").exists()
            if len(attempts) < 3:
                error = PermissionError("temporary scanner handle")
                error.winerror = 32
                raise error
        return replace(source, destination)
    monkeypatch.setattr(install.os, "replace", sharing_then_success)
    selected = install.compose_release(**spec)
    assert len(attempts) == 3 and len(set(attempts)) == 1
    assert Path(selected["release_root"]).exists()


def data_fixture(tmp_path):
    previous, spec = specification(tmp_path)
    source = Path(spec["reviewed_source"])
    real_release = Path(releases.__file__).read_bytes()
    additions = {"app/__init__.py": b"", "app/services/__init__.py": b"",
        "app/services/schwab_token_store.py": b"# offline token-store pointer fixture\n",
        "ml/__init__.py": b"", "ml/intraday/__init__.py": b"",
        "ml/intraday/scout_services.py": b'''def run_data_service(*, repository, datastore, release):
    from pathlib import Path
    root = Path(datastore)
    root.mkdir(parents=True, exist_ok=True)
    print("fixture-data-status")
    (root / "selected-source.txt").write_text(__file__)
    return {"status": "STOPPED", "provider_calls": 0, "trading_started": False}
'''}
    for relative, raw in {**additions, "tools/scout_release.py": real_release}.items():
        path = source / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(raw)
        before = previous["manifest"]["files"].get(relative)
        spec["operations"].append({"path": relative, "operation": "modify" if before else "add",
            "before_sha256": before, "sha256": releases.sha256(raw)})
    selected = install.compose_release(**spec)
    selector = tmp_path / "independent-selected.json"
    install.select_independent_release(selector, selected)
    result = install.install_data_bootstrap(selected=selected, destination=tmp_path / "data-bootstrap",
        repository=tmp_path / "original", datastore=tmp_path / "data-private", selector=selector,
        completion_record="offline-data-fixture")
    return selected, result


def test_data_bootstrap_check_never_imports_or_runs_services(tmp_path):
    selected, bootstrap = data_fixture(tmp_path)
    result = subprocess.run([sys.executable, "-B", bootstrap["launch_path"], "--check"],
        cwd=tmp_path, capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr
    checked = json.loads(result.stdout)
    assert checked["release"] == selected["manifest_sha256"]
    assert checked["system_1a_started"] is False and checked["trading_started"] is False
    assert not (tmp_path / "data-private").exists()
    assert bootstrap["trading_activation_supported"] is False


def test_data_bootstrap_imports_only_verified_selected_source(tmp_path):
    selected, bootstrap = data_fixture(tmp_path)
    shadow = tmp_path / "untrusted-checkout/ml/intraday"
    shadow.mkdir(parents=True)
    (shadow / "scout_services.py").write_text("raise AssertionError('wrong source')", encoding="utf-8")
    result = subprocess.run([sys.executable, "-B", bootstrap["launch_path"], "--data-live"],
        cwd=tmp_path, env=dict(os.environ, PYTHONPATH=str(tmp_path / "untrusted-checkout")),
        capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr
    actual = Path((tmp_path / "data-private/selected-source.txt").read_text()).resolve()
    assert actual.is_relative_to(Path(selected["source_root"]))
    assert json.loads(result.stdout)["trading_started"] is False
    assert "fixture-data-status" in (tmp_path / "data-private/intraday/services/data.stdout.log").read_text()
    assert (tmp_path / "data-private/intraday/services/data.stderr.log").exists()


def test_data_bootstrap_rejects_trading_flag_and_unmanifested_bytecode(tmp_path):
    _, bootstrap = data_fixture(tmp_path)
    result = subprocess.run([sys.executable, "-B", bootstrap["launch_path"], "--live"],
        cwd=tmp_path, capture_output=True, text=True, timeout=30)
    assert result.returncode == 2
    (Path(bootstrap["bootstrap_root"]) / "unexpected.pyc").write_bytes(b"unreviewed")
    result = subprocess.run([sys.executable, "-B", bootstrap["launch_path"], "--check"],
        cwd=tmp_path, capture_output=True, text=True, timeout=30)
    assert result.returncode != 0 and "source inventory changed" in result.stderr
    assert not (tmp_path / "data-private").exists()
