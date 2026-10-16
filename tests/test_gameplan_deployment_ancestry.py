"""Interrupted ancestry preserves completed evidence without completing its child."""
import json

import pytest

from ml import gameplan_deployment as deployment
from ml.artifacts import file_checksum, verify_manifest
from test_gameplan_deployment_handoff import DAY, DEADLINE, activate, handoff, resumed_tail, write


def _interrupted_ancestor(state):
    original = resumed_tail(state)
    state["report"]["status"] = "INTERRUPTED"
    state["bind_native"]()
    return original


def _seal_report(run, report):
    """Rebind deliberate fixture changes while retaining the original log receipts."""
    receipt_path = run / "receipt.json"
    receipt = json.loads(receipt_path.read_text())
    report_path = run / "stage-report.json"
    write(report_path, report)
    receipt.update(status=report["status"],
                   stage_report_checksum_sha256=file_checksum(report_path),
                   stage_report_size=report_path.stat().st_size)
    write(receipt_path, receipt)


def _attempt_bytes(state):
    folder = state["root"] / "ml/overnight-runs"
    return {path.relative_to(folder).as_posix(): path.read_bytes()
            for path in folder.rglob("*") if path.is_file()}


@pytest.mark.parametrize("ancestor_status", ["FAILED", "CANCELLED", "INTERRUPTED"])
def test_completed_resume_accepts_verified_ancestor_and_preserves_all_evidence(handoff, ancestor_status):
    original = resumed_tail(handoff)
    handoff["report"]["status"] = ancestor_status
    handoff["bind_native"]()
    before = _attempt_bytes(handoff)

    run = activate(handoff)

    report = deployment.read_deployment(handoff["root"], DAY)
    assert report["status"] == "ACTIVE"
    assert report["native_preparation"]["run_path"] == handoff["native"].relative_to(handoff["root"]).as_posix()
    assert json.loads((handoff["native"] / "stage-report.json").read_text())["status"] == "COMPLETE"
    assert json.loads((original / "stage-report.json").read_text())["status"] == ancestor_status
    assert _attempt_bytes(handoff) == before
    assert (handoff["og"].run_directory / "receipt.json").read_bytes() == handoff["og_bytes"]
    bound = {str(item["path"]).replace("\\", "/") for item in verify_manifest(run)["input_files"]}
    assert {"ml/overnight-runs/" + name for name in before}.issubset(bound)


@pytest.mark.parametrize("current_status", ["FAILED", "CANCELLED", "INTERRUPTED", "RUNNING", "TIMED_OUT"])
def test_verified_interrupted_ancestor_never_completes_current_attempt(handoff, current_status):
    _interrupted_ancestor(handoff)
    current = handoff["native"]
    report = json.loads((current / "stage-report.json").read_text())
    report["status"] = current_status
    _seal_report(current, report)
    pointer = deployment._pointer(handoff["root"], DAY)
    before_pointer, before_attempts = pointer.read_bytes(), _attempt_bytes(handoff)

    with pytest.raises(ValueError, match="native preparation receipt is incomplete"):
        activate(handoff)

    assert pointer.read_bytes() == before_pointer
    assert deployment.read_deployment(handoff["root"], DAY)["status"] == "PREPARING"
    assert _attempt_bytes(handoff) == before_attempts


@pytest.mark.parametrize("change, error", [
    ("report_tamper", "native preparation receipt"),
    ("report_checksum", "native preparation receipt"),
    ("report_size", "native preparation receipt"),
    ("receipt_status", "native preparation receipt"),
    ("log_tamper", "native log differs"),
    ("log_checksum", "native log differs"),
    ("log_size", "native log differs"),
    ("pin", "native preparation pin differs"),
    ("deadline", "native preparation receipt"),
    ("completed_stage_time", "invalid time or log evidence"),
    ("inherited_names", "completion names differ"),
    ("cycle", "ancestry contains a cycle"),
])
def test_interrupted_ancestor_retains_integrity_and_ancestry_checks(handoff, change, error):
    original = _interrupted_ancestor(handoff)
    receipt_path = original / "receipt.json"
    receipt = json.loads(receipt_path.read_text())
    report_path = original / "stage-report.json"
    report = json.loads(report_path.read_text())
    log_name = "stock_enrichment_training.log"

    if change == "report_tamper":
        report_path.write_bytes(report_path.read_bytes() + b" ")
    elif change == "log_tamper":
        log = original / log_name
        log.write_bytes(log.read_bytes() + b" ")
    elif change in {"report_checksum", "report_size", "receipt_status", "log_checksum", "log_size"}:
        if change == "report_checksum":
            receipt["stage_report_checksum_sha256"] = "0" * 64
        elif change == "report_size":
            receipt["stage_report_size"] += 1
        elif change == "receipt_status":
            receipt["status"] = "FAILED"
        elif change == "log_checksum":
            receipt["logs"][log_name]["checksum_sha256"] = "0" * 64
        else:
            receipt["logs"][log_name]["size"] += 1
        write(receipt_path, receipt)
    elif change == "inherited_names":
        current = handoff["native"]
        child_report = json.loads((current / "stage-report.json").read_text())
        child_report["completed_stages_from_previous_attempt"] = ["gameplan_publication"]
        _seal_report(current, child_report)
    else:
        if change == "pin":
            report["enrichment_gameplan"]["receipt_sha256"] = "0" * 64
        elif change == "deadline":
            report["deadline_at"] = "2026-09-18T12:00:00+00:00"
        elif change == "completed_stage_time":
            report["stages"][0]["finished_at"] = DEADLINE
        else:
            report["resumed_from"] = str(handoff["native"])
        _seal_report(original, report)

    pointer = deployment._pointer(handoff["root"], DAY)
    before_pointer, before_attempts = pointer.read_bytes(), _attempt_bytes(handoff)
    with pytest.raises(ValueError, match=error):
        activate(handoff)
    assert pointer.read_bytes() == before_pointer
    assert deployment.read_deployment(handoff["root"], DAY)["status"] == "PREPARING"
    assert _attempt_bytes(handoff) == before_attempts
