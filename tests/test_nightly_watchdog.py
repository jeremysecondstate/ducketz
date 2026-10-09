"""Offline watchdog tests: process adapter and deduplicated durable decisions."""
import json
from pathlib import Path
import subprocess
from types import SimpleNamespace
import pytest

from tools import nightly_watchdog as watchdog


def setup(monkeypatch, tmp_path):
    config = {"state_root": str(tmp_path), "repository": str(tmp_path)}
    monkeypatch.setattr(watchdog.workflow, "load_config", lambda p: config)
    monkeypatch.setattr(watchdog.workflow, "verify_installation", lambda c: None)
    return tmp_path / "config.json"


def test_duplicate_wakes_do_not_repeat_unchanged_notice(monkeypatch, tmp_path):
    config = setup(monkeypatch, tmp_path)
    calls = []
    def runner(command, **kwargs):
        calls.append((command, kwargs))
        return SimpleNamespace(returncode=0, stdout=json.dumps({"status": "BUSY", "action_date": "2026-10-12"}), stderr="")
    first = watchdog.run_once(config, runner=runner, now="2026-10-10T04:05:00Z")
    second = watchdog.run_once(config, runner=runner, now="2026-10-10T04:10:00Z")
    assert first["changed"] and not second["changed"]
    assert len((tmp_path / "watchdog/events.jsonl").read_text().splitlines()) == 1
    assert calls[0][0][-1] == "--dispatch"
    assert calls[0][1]["timeout"] == 90
    assert "trader" not in " ".join(calls[0][0])


def test_bad_dispatch_output_retains_actionable_failure(monkeypatch, tmp_path):
    config = setup(monkeypatch, tmp_path)
    result = watchdog.run_once(config, runner=lambda *a, **k: SimpleNamespace(returncode=1, stdout="", stderr="missing local dependency"))
    assert result["decision"]["status"] == "DISPATCH_FAILED"
    assert result["decision"]["error"] == "missing local dependency"
    assert result["decision"]["owner"] == "datastore"
    assert result["decision"]["corrective_action"]
    assert json.loads((tmp_path / "watchdog/last.json").read_text())["fingerprint"] == result["fingerprint"]


def test_launcher_timeout_is_durable_and_has_owner(monkeypatch, tmp_path):
    config = setup(monkeypatch, tmp_path)
    def runner(*args, **kwargs):
        raise subprocess.TimeoutExpired(args[0], 90)
    result = watchdog.run_once(config, runner=runner)
    assert result["decision"]["status"] == "DISPATCH_FAILED"
    assert result["decision"]["owner"] == "datastore"
    assert (tmp_path / "watchdog/events.jsonl").exists()


def test_changed_nested_failure_gets_new_notice(monkeypatch, tmp_path):
    config = setup(monkeypatch, tmp_path)
    def runner(fingerprint):
        return lambda *a, **k: SimpleNamespace(returncode=0, stderr="", stdout=json.dumps(
            {"status": "REPAIR_REQUIRED", "failure": {"fingerprint": fingerprint, "owner": "model"}}))
    first = watchdog.run_once(config, runner=runner("first"))
    second = watchdog.run_once(config, runner=runner("second"))
    assert first["changed"] and second["changed"]
    assert len((tmp_path / "watchdog/events.jsonl").read_text().splitlines()) == 2


def test_failed_dispatch_uses_native_responsibility_owner(monkeypatch, tmp_path):
    config = setup(monkeypatch, tmp_path)
    monkeypatch.setattr(watchdog.workflow, "load_config", lambda p: {
        "state_root": str(tmp_path), "repository": str(tmp_path),
        "responsibility_owners": {"model": "native-model-task"}})
    result = watchdog.run_once(config, runner=lambda *a, **k: SimpleNamespace(
        returncode=1, stderr="", stdout=json.dumps({"status": "FAILED", "responsibility": "model"})))
    assert result["decision"]["owner"] == "native-model-task"
    assert result["decision"]["exit_code"] == 1
    assert result["decision"]["corrective_action"]


def test_failed_dispatch_preserves_specific_repair_owner(monkeypatch, tmp_path):
    config = setup(monkeypatch, tmp_path)
    result = watchdog.run_once(config, runner=lambda *a, **k: SimpleNamespace(
        returncode=1, stderr="", stdout=json.dumps({"status": "FAILED", "owner": "owned-repair",
            "corrective_action": "Resume frozen installation"})))
    assert result["decision"]["owner"] == "owned-repair"
    assert result["decision"]["corrective_action"] == "Resume frozen installation"


def test_invalid_configuration_failure_has_durable_native_owner(monkeypatch, tmp_path):
    config = tmp_path / "config.json"
    config.write_text(json.dumps({"responsibility_owners": {"datastore": "native-datastore-task"},
                                 "state_root": "do-not-trust-this-path"}))
    monkeypatch.setattr(watchdog.workflow, "load_config", lambda p: (_ for _ in ()).throw(ValueError("invalid local configuration")))
    monkeypatch.setattr(watchdog.subprocess, "run", lambda *a, **k: pytest.fail("Invalid config cannot dispatch"))
    assert watchdog.main(["--config", str(config)]) == 1
    status = json.loads((tmp_path / "watchdog/bootstrap-status.json").read_text())
    assert status["owner"] == "native-datastore-task" and status["disposition"] == "OPEN"
    assert Path(status["evidence"]).is_file()


def test_duplicate_bootstrap_failure_preserves_original_evidence(tmp_path):
    config = tmp_path / "missing-config.json"
    first = watchdog._bootstrap_failure(config, ValueError("pinned install changed"))
    evidence = Path(first["evidence"]).read_bytes()
    second = watchdog._bootstrap_failure(config, ValueError("pinned install changed"))
    assert first["changed"] and not second["changed"]
    assert Path(second["evidence"]).read_bytes() == evidence


def test_verified_bootstrap_resolution_preserves_original_failure(tmp_path):
    config = tmp_path / "config.json"
    failure = watchdog._bootstrap_failure(config, ValueError("configuration unavailable"))
    evidence = Path(failure["evidence"]).read_bytes()
    watchdog._resolve_bootstrap_failure(config)
    assert json.loads((tmp_path / "watchdog/bootstrap-status.json").read_text())["disposition"] == "RESOLVED"
    assert Path(failure["evidence"]).read_bytes() == evidence
