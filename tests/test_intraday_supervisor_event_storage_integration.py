"""Local composed supervisor checks reuse its existing provider-free fixtures."""
import hashlib
import importlib.util
import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]

if not ((ROOT / 'ml/intraday/supervisor.py').is_file()
        and (ROOT / 'tests/test_intraday_supervisor.py').is_file()):
    pytest.skip('Composed local supervisor fixtures absent; portable storage checks run separately',
                allow_module_level=True)


def fixture_module():
    spec = importlib.util.spec_from_file_location("supervisor_storage_existing_fixtures",
        ROOT / "tests/test_intraday_supervisor.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_append_error_after_durable_success_cannot_relabel_original_result(tmp_path, monkeypatch):
    fixture = fixture_module()
    original_event = fixture.supervisor.AuditLog.event
    appended = []
    def failure_after_append(self, kind, **details):
        event = original_event(self, kind, **details)
        if kind == "symbol_completed":
            appended.append(event)
            raise OSError("fixture failure after durable event append")
        return event
    monkeypatch.setattr(fixture.supervisor.AuditLog, "event", failure_after_append)
    with pytest.raises(OSError, match="after durable"):
        fixture.run(tmp_path)
    directory = tmp_path / "intraday/model-supervision/logs"
    events = [json.loads(line) for line in (directory / "events.jsonl").read_text().splitlines()]
    completed = [event for event in events if event["kind"] == "symbol_completed"]
    assert len(completed) == len(appended) == 1
    assert not [event for event in events if event["kind"] == "symbol_failed"]
    saved = json.loads((directory / completed[0]["report_file"]).read_bytes())
    assert saved["status"] == "running"
    result = saved["symbols"]["AAPL"]
    assert result["status"] == "activated" and "failure_type" not in result
    assert fixture.supervisor._hash(result) == completed[0]["result_sha256"]
    active = json.loads((tmp_path / fixture.supervisor.ACTIVE_PATH).read_bytes())
    assert result["selection_id"] == active["symbols"]["AAPL"]["selection_id"]


def test_final_report_science_and_compact_event_references_match(tmp_path):
    fixture = fixture_module()
    report = fixture.run(tmp_path, symbols=("AAPL", "NVDA"))
    directory = tmp_path / "intraday/model-supervision/logs"
    path = directory / (report["run_id"] + ".json")
    assert path.read_bytes() == (fixture.supervisor._json(report) + "\n").encode()
    events = [json.loads(line) for line in (directory / "events.jsonl").read_text().splitlines()]
    symbols = [event for event in events if event["kind"] == "symbol_completed"]
    assert len(symbols) == 2
    for event in symbols:
        item = report["symbols"][event["symbol"]]
        assert event["result_sha256"] == fixture.supervisor._hash(item)
        assert event["result"]["selection_id"] == item["selection_id"]
        assert "selection" not in event["result"] and "training_snapshot" not in event["result"]
    assert report["status"] == "complete"
    assert report["trader_started"] is False and report["broker_calls"] == 0
