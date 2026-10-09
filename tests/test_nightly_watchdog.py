"""Offline watchdog tests: process adapter and deduplicated durable decisions."""
import json
import subprocess
from types import SimpleNamespace

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
