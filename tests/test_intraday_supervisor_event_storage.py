"""Exact portable supervisor storage checks: no fitting, fetching or traders."""
from copy import deepcopy
from datetime import datetime, timezone
import ast
import hashlib
import json
import os
from pathlib import Path
import re
import tempfile

import pytest

ROOT = Path(__file__).resolve().parents[1]
META = ROOT / "docs/development/patches/intraday-supervisor-event-storage.json"


def checked_projection():
    meta = json.loads(META.read_bytes())
    patch = (ROOT / meta["patch_path"]).read_bytes()
    assert hashlib.sha256(patch).hexdigest() == meta["patch_sha256"]
    hunks = checked_hunks(patch)
    assert meta["target"] == "ml/intraday/supervisor.py" and meta["operation"] == "modify"
    old = ast.parse(b"\n".join(b"".join(h["old_lines"]) for h in hunks))
    new = ast.parse(b"\n".join(b"".join(h["new_lines"]) for h in hunks))
    digest = lambda node: hashlib.sha256(ast.dump(node, include_attributes=False).encode()).hexdigest()
    for tree, recorded in ((old, meta["before_ast"]), (new, meta["after_ast"])):
        assert {node.name: digest(node) for node in tree.body} == recorded
    restored = deepcopy(new)
    owner = next(n for n in restored.body if isinstance(n, ast.ClassDef))
    methods = [n for n in owner.body if isinstance(n, ast.FunctionDef)]
    assert [n.name for n in methods] == ["__init__", "event", "symbol_result"]
    owner.body.remove(methods[-1])
    run = restored.body[-1]
    assert run.name == "supervise"
    calls = [n for n in ast.walk(run) if isinstance(n, ast.Call)
             and isinstance(n.func, ast.Attribute) and n.func.attr == "symbol_result"]
    assert len(calls) == 1
    assert ast.dump(calls[0], include_attributes=False) == ast.dump(
        ast.parse("audit.symbol_result(report, symbol)").body[0].value,
        include_attributes=False)
    loop = next(n for n in run.body if isinstance(n, ast.For)
                and isinstance(n.target, ast.Name) and n.target.id == "symbol")
    assert isinstance(loop.body[-1], ast.Expr) and loop.body[-1].value is calls[0]
    loop.body.pop()
    scientific = [n for n in loop.body if isinstance(n, ast.Try)][-1]
    scientific.body.append(ast.parse('audit.event("symbol_completed", symbol=symbol, result=item)').body[0])
    scientific.handlers[0].body.append(ast.parse('audit.event("symbol_failed", symbol=symbol, result=item)').body[0])
    assert ast.dump(restored, include_attributes=False) == ast.dump(old, include_attributes=False)
    source = ROOT / meta["target"]
    if source.is_file():
        raw = source.read_bytes()
        assert hashlib.sha256(raw).hexdigest() == meta["local_after_sha256"]
        local = ast.parse(raw)
        assert {node.name: digest(node) for node in local.body
                if isinstance(node, (ast.FunctionDef, ast.ClassDef))
                and node.name in meta["after_ast"]} == meta["after_ast"]
    return new


def checked_hunks(raw):
    """Strict two-hunk package parser; accepts only the single owned target."""
    lines = raw.splitlines(keepends=True)
    assert lines[:2] == [b"--- a/ml/intraday/supervisor.py\n", b"+++ b/ml/intraday/supervisor.py\n"]
    index, hunks, old_end, new_end = 2, [], 0, 0
    while index < len(lines):
        match = re.fullmatch(rb"@@ -(\d+),(\d+) \+(\d+),(\d+) @@\n", lines[index])
        assert match is not None
        old_start, old_count, new_start, new_count = map(int, match.groups())
        assert old_start > old_end and new_start > new_end
        index += 1
        old, new = [], []
        while len(old) < old_count or len(new) < new_count:
            assert index < len(lines)
            marker, body = lines[index][:1], lines[index][1:]
            assert marker in (b" ", b"-", b"+")
            if marker != b"+":
                old.append(body)
            if marker != b"-":
                new.append(body)
            index += 1
        assert (len(old), len(new)) == (old_count, new_count)
        old_end, new_end = old_start + old_count - 1, new_start + new_count - 1
        hunks.append(dict(old_lines=old, new_lines=new))
    assert len(hunks) == 2
    return hunks


@pytest.fixture
def audit_code():
    tree = checked_projection()
    names = {"_json", "_hash", "_atomic_json", "AuditLog"}
    code = ast.Module(body=[node for node in tree.body if node.name in names], type_ignores=[])
    namespace = dict(json=json, hashlib=hashlib, os=os, Path=Path, tempfile=tempfile,
                     utc=lambda value: value, VERSION="intraday-model-supervision-v1")
    exec(compile(code, "verified-supervisor-event-storage.patch", "exec"), namespace)
    return namespace


def item(symbol="AAPL", *, failed=False):
    value = {"status": "error" if failed else "activated", "incumbent_retained": failed,
             "selection_id": "original-selection-" + symbol,
             "training_snapshot": {"observations": ["private-fixture-source-" + symbol] * 3000},
             "selection": {"source": {"retained": list(range(3000))},
                           "candidate_scores": [{"probability": .5001, "target": "original"}]}}
    if failed:
        value.update(failure_type="FixtureError", message="Original diagnostic failure")
    return value


def report():
    return {"schema_version": "intraday-model-supervision-v1", "run_id": "20261029T010000Z-111111111111",
            "actor": "Atlas", "source_dataset": "EQUS.MINI", "symbols": {"AAPL": item()},
            "previous_session": {"session_id": "original-session"}}


def audit(audit_code, tmp_path):
    return audit_code["AuditLog"](tmp_path, report()["run_id"],
        lambda: datetime(2026, 10, 29, 1, tzinfo=timezone.utc))


def test_exact_patch_changes_only_symbol_storage_boundary():
    checked_projection()


@pytest.mark.parametrize("failed", [False, True])
def test_compact_event_references_exact_durable_full_result(audit_code, tmp_path, failed):
    original = report()
    original["symbols"]["AAPL"] = item(failed=failed)
    before = deepcopy(original)
    log = audit(audit_code, tmp_path)
    event = log.symbol_result(original, "AAPL")
    saved = json.loads((tmp_path / event["report_file"]).read_bytes())
    assert original == before
    assert saved == {**before, "status": "running"}
    assert event["kind"] == ("symbol_failed" if failed else "symbol_completed")
    assert event["result_path"] == ["symbols", "AAPL"]
    assert event["result_sha256"] == audit_code["_hash"](saved["symbols"]["AAPL"])
    assert "selection" not in event["result"] and "training_snapshot" not in event["result"]
    assert len(audit_code["_json"](event)) < 1200
    assert len(audit_code["_json"](before["symbols"]["AAPL"])) > 100_000
    assert not list(tmp_path.glob("*.pending"))


def test_completed_symbol_stays_exact_through_next_symbol_and_final_report(audit_code, tmp_path):
    original = report()
    log = audit(audit_code, tmp_path)
    first = log.symbol_result(original, "AAPL")
    first_bytes = audit_code["_json"](original["symbols"]["AAPL"])
    original["symbols"]["NVDA"] = item("NVDA")
    second = log.symbol_result(original, "NVDA")
    original.update(status="complete", completed_at="original-completion-clock")
    expected = (audit_code["_json"](original) + "\n").encode()
    path = tmp_path / first["report_file"]
    audit_code["_atomic_json"](path, original)
    assert path.read_bytes() == expected
    saved = json.loads(expected)
    for event in (first, second):
        result = saved
        for key in event["result_path"]:
            result = result[key]
        assert audit_code["_hash"](result) == event["result_sha256"]
    assert audit_code["_json"](saved["symbols"]["AAPL"]) == first_bytes
    assert len(list(tmp_path.glob("*.json"))) == 1
    assert len((tmp_path / "events.jsonl").read_bytes()) < 2400


def test_interruption_after_checkpoint_preserves_completed_diagnostics(audit_code, tmp_path, monkeypatch):
    log = audit(audit_code, tmp_path)
    original = report()
    def interrupted(*args, **kwargs):
        path = tmp_path / (log.run_id + ".json")
        assert json.loads(path.read_bytes())["symbols"] == original["symbols"]
        raise SystemExit("fixture interruption after durable checkpoint")
    monkeypatch.setattr(log, "event", interrupted)
    with pytest.raises(SystemExit):
        log.symbol_result(original, "AAPL")
    assert json.loads((tmp_path / (log.run_id + ".json")).read_bytes())["status"] == "running"
    assert not (tmp_path / "events.jsonl").exists()


def test_event_failure_keeps_checkpoint_and_original_log_prefix(audit_code, tmp_path, monkeypatch):
    log = audit(audit_code, tmp_path)
    path = tmp_path / "events.jsonl"
    prior = b'{"original":"historical-event"}\n'
    path.write_bytes(prior)
    def failed(*args, **kwargs):
        raise OSError("fixture audit append failure")
    monkeypatch.setattr(log, "event", failed)
    with pytest.raises(OSError):
        log.symbol_result(report(), "AAPL")
    assert json.loads((tmp_path / (log.run_id + ".json")).read_bytes())["symbols"] == report()["symbols"]
    assert path.read_bytes() == prior


def test_checkpoint_failure_cannot_publish_completed_event(audit_code, tmp_path, monkeypatch):
    log = audit(audit_code, tmp_path)
    events = []
    monkeypatch.setattr(log, "event", lambda *a, **kw: events.append((a, kw)))
    def failed(*args, **kwargs):
        raise OSError("fixture checkpoint failure")
    monkeypatch.setitem(audit_code, "_atomic_json", failed)
    with pytest.raises(OSError):
        log.symbol_result(report(), "AAPL")
    assert not events and not list(tmp_path.iterdir())


def test_new_events_append_without_rewriting_historical_diagnostics(audit_code, tmp_path):
    log = audit(audit_code, tmp_path)
    path = tmp_path / "events.jsonl"
    prior = b'{"kind":"symbol_completed","result":{"legacy":"retained-full-evidence"}}\n'
    path.write_bytes(prior)
    log.symbol_result(report(), "AAPL")
    assert path.read_bytes().startswith(prior)
    assert len(path.read_bytes().splitlines()) == 2
