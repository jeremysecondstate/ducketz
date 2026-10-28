"""Offline provenance of a worker's bootstrap, independent of later selectors."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import sys
from types import ModuleType, SimpleNamespace

import pytest

_ROOT = Path(__file__).resolve().parents[1]
_WORKER_SOURCE = _ROOT / "tools/trader_worker.py"
_PACKAGED_SOURCE = None
if _WORKER_SOURCE.is_file():
    from tools import trader_worker as worker
else:
    # The public base carries Atlas's differing consumer as reviewed hunks.
    # Exercise the exact packaged functions without requiring private adapters.
    package = json.loads((_ROOT / "docs/development/patches/atlas-intraday-audit.json").read_bytes())
    historical = json.loads((_ROOT / "docs/development/patches/trader-worker-failure-evidence.json").read_bytes())
    extracts = []
    for document, name in ((package, "_record_loaded"), (historical, "_record_failure"), (package, "main")):
        entry = document["extracts"][name]
        if hashlib.sha256(entry["source"].encode()).hexdigest() != entry["sha256"]:
            raise AssertionError("Reviewed worker extract changed: " + name)
        extracts.append(entry["source"])
    _PACKAGED_SOURCE = "\n".join(extracts)
    worker = ModuleType("atlas_packaged_worker_loaded_evidence")
    worker.__dict__.update({"argparse": argparse, "json": json, "os": os, "Path": Path, "sys": sys})
    exec(compile(_PACKAGED_SOURCE, "<reviewed-atlas-worker-extracts>", "exec"), worker.__dict__)


def _sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


@pytest.fixture
def loaded(tmp_path, monkeypatch):
    if _PACKAGED_SOURCE is not None:
        source = tmp_path / "source/tools/trader_worker.py"
        source.parent.mkdir(parents=True)
        source.write_text(_PACKAGED_SOURCE, encoding="utf-8")
        monkeypatch.setattr(worker, "__file__", str(source), raising=False)
    request_path = tmp_path / "forecast-3-exactturn.json"
    request = {"protocol": "manual-trader-renewal-v1", "role": "forecast-3",
               "turn": "exactturn", "generation": 27, "token": "private-token"}
    request_path.write_text(json.dumps(request), encoding="utf-8")
    renewal = SimpleNamespace(request=request, request_path=request_path,
                              envelope={"session": {"session_id": "original-day-session"}},
                              stopped=lambda: False, claim=lambda: None, finish=lambda result: None)
    renewal_module = ModuleType("ml.trader_renewal")
    renewal_module.process_birth = lambda pid: "fixture-worker-birth"
    renewal_module.Renewal = lambda path: renewal
    class PlannedRenewal(BaseException):
        pass
    renewal_module.PlannedRenewal = PlannedRenewal
    renewal_module.PLANNED_EXIT = 75
    host_module = ModuleType("tools.trader_session_host")
    host_module.bind_kernel_lifetime = lambda: None
    monkeypatch.setitem(sys.modules, "ml.trader_renewal", renewal_module)
    monkeypatch.setitem(sys.modules, "tools.trader_session_host", host_module)
    monkeypatch.setenv("DUCKETZ_TRADER_RENEWAL_REQUEST", os.environ.get("DUCKETZ_TRADER_RENEWAL_REQUEST", ""))
    selected = {"generation": "a" * 64, "manifest_sha256": "a" * 64, "runtime_id": "b" * 64,
                "source_root": str(Path(worker.__file__).resolve().parents[1]), "python": sys.executable,
                "manifest": {"source_files": {"tools/trader_worker.py": _sha(worker.__file__)},
                             "runtime": {"manifest_sha256": "b" * 64}},
                "runtime_manifest": {"python_relative": "python/python.exe",
                                     "files": {"python/python.exe": _sha(sys.executable)}}}
    monkeypatch.setattr(worker, "bootstrap", lambda path, **kwargs: selected, raising=False)
    monkeypatch.setattr(worker, "run", lambda context: None, raising=False)
    return renewal, selected, renewal_module


@pytest.mark.parametrize("outcome", ["success", "renewal", "manual-stop", "failure"])
def test_actual_bootstrap_retained_before_lane_work_and_later_selection(loaded, tmp_path, monkeypatch, outcome):
    renewal, selected, module = loaded
    pointer = tmp_path / "active.json"
    pointer.write_text(json.dumps({"generation": "c" * 64}))
    receipt = renewal.request_path.with_suffix(".loaded.json")
    original_request = renewal.request_path.read_bytes()
    finished = []
    renewal.finish = finished.append
    def run(context):
        value = json.loads(receipt.read_bytes())
        assert value["sealed_generation"] == selected["generation"] != json.loads(pointer.read_bytes())["generation"]
        if outcome == "renewal":
            raise module.PlannedRenewal()
        if outcome == "manual-stop":
            context.stopped = lambda: True
            raise InterruptedError("original manual stop")
        if outcome == "failure":
            raise ValueError("original failure")
        return {"status": "COMPLETED"}
    monkeypatch.setattr(worker, "run", run)
    args = ["--generation", str(tmp_path), "--request", str(renewal.request_path)]
    if outcome == "failure":
        with pytest.raises(ValueError, match="original failure"):
            worker.main(args)
    else:
        assert worker.main(args) == (75 if outcome == "renewal" else 0)
    value = json.loads(receipt.read_bytes())
    assert value["diagnostic_only"] is True
    assert value["source_manifest_sha256"] == "a" * 64
    assert value["runtime_id"] == value["runtime_manifest_sha256"] == "b" * 64
    assert value["worker_matches_bootstrap"] and value["python_matches_bootstrap"]
    assert value["worker_pid"] == os.getpid() and value["worker_birth"] == "fixture-worker-birth"
    assert value["generation"] == 27 and value["role"] == "forecast-3"
    assert value["request_sha256"] == hashlib.sha256(original_request).hexdigest()
    assert renewal.request_path.read_bytes() == original_request
    assert "private-token" not in receipt.read_text()
    assert len(finished) == (outcome in {"success", "manual-stop"})


def test_repeated_proof_never_overwrites_original(loaded, tmp_path):
    renewal, selected, _ = loaded
    assert worker._record_loaded(renewal, selected)
    target = renewal.request_path.with_suffix(".loaded.json")
    original = target.read_bytes()
    selected["generation"] = "d" * 64
    assert worker._record_loaded(renewal, selected) is False
    assert target.read_bytes() == original
    assert not list(tmp_path.glob("*.partial"))


def test_evidence_disk_error_preserves_normal_and_planned_results(loaded, monkeypatch):
    renewal, selected, module = loaded
    monkeypatch.setattr(worker.os, "link", lambda *args: (_ for _ in ()).throw(OSError("fixture disk failure")))
    finished = []
    renewal.finish = finished.append
    monkeypatch.setattr(worker, "run", lambda context: {"status": "COMPLETED"})
    args = ["--generation", str(renewal.request_path.parent), "--request", str(renewal.request_path)]
    assert worker.main(args) == 0
    assert finished == [{"status": "COMPLETED"}]
    monkeypatch.setattr(worker, "run", lambda context: (_ for _ in ()).throw(module.PlannedRenewal()))
    assert worker.main(args) == 75
    assert not renewal.request_path.with_suffix(".loaded.json").exists()


def test_mismatched_executing_files_are_truthful_without_authority(loaded):
    renewal, selected, _ = loaded
    selected["manifest"]["source_files"]["tools/trader_worker.py"] = "e" * 64
    selected["runtime_manifest"]["files"]["python/python.exe"] = "f" * 64
    assert worker._record_loaded(renewal, selected)
    value = json.loads(renewal.request_path.with_suffix(".loaded.json").read_bytes())
    assert not value["worker_matches_bootstrap"] and not value["python_matches_bootstrap"]


def test_verify_only_does_not_write_loaded_session_receipt(loaded, monkeypatch, tmp_path):
    renewal, selected, _ = loaded
    monkeypatch.setattr(worker, "_record_loaded", lambda *args: pytest.fail("verification is not a loaded lane"))
    assert worker.main(["--generation", str(tmp_path), "--verify-only"]) == 0
    assert not renewal.request_path.with_suffix(".loaded.json").exists()
