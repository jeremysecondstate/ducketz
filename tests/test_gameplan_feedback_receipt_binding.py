"""Feedback must retain the Stats receipt identity validated by its first read."""
import json

import pytest

from gameplan_stats_fixture import write_review
from ml import gameplan_model_feedback as feedback
from ml.artifacts import file_checksum
from test_gameplan_model_feedback import NOW, proposal


@pytest.mark.parametrize("operation", ["prepare", "save", "load_latest", "load_frozen"])
@pytest.mark.parametrize("reissue_at", ["after_load", "after_hashes"])
def test_coherent_stats_reissue_cannot_replace_loaded_feedback_evidence(
    tmp_path, monkeypatch, operation, reissue_at,
):
    stats = write_review(tmp_path)
    run = feedback.prepare_feedback(tmp_path, now=NOW)
    response = proposal(run)
    saved = None
    if operation.startswith("load_"):
        saved = feedback.save_feedback_review(tmp_path, run, response, now=NOW)
    originals = {p: p.read_bytes() for p in run.iterdir() if p.is_file()}
    original_receipt = file_checksum(stats / "receipt.json")
    real_load = feedback.load_gameplan_stats
    real_checksum = feedback.file_checksum
    loaded = []
    replacement_receipts = []

    def reissue():
        # Same outcomes and run, but a new valid publication identity.
        receipt_path = stats / "receipt.json"
        receipt = json.loads(receipt_path.read_text())
        receipt["fixture_reissue"] = "coherent replacement after the selected read"
        receipt_path.write_text(json.dumps(receipt), encoding="utf-8")
        replacement = real_checksum(receipt_path)
        for pointer_path in (
            tmp_path / "ml/gameplan-actuals-review-latest/run.json",
            tmp_path / "ml/gameplan-actuals-review-by-date/2026-09-11/run.json",
        ):
            pointer = json.loads(pointer_path.read_text())
            pointer["current"]["receipt_sha256"] = replacement
            pointer_path.write_text(json.dumps(pointer), encoding="utf-8")
        replacement_review = real_load(tmp_path)
        assert replacement_review.receipt_sha256 == replacement != original_receipt
        assert replacement_review.outcomes == loaded[0].outcomes
        replacement_receipts.append(replacement)

    def load_then_reissue(*args, **kwargs):
        review = real_load(*args, **kwargs)
        assert review.receipt_sha256 == original_receipt
        loaded.append(review)
        if reissue_at == "after_load":
            reissue()
        return review

    def checksum_then_reissue(path):
        digest = real_checksum(path)
        if reissue_at == "after_hashes" and path == stats / "report.json":
            reissue()
        return digest

    monkeypatch.setattr(feedback, "load_gameplan_stats", load_then_reissue)
    monkeypatch.setattr(feedback, "file_checksum", checksum_then_reissue)
    output = tmp_path / "new-feedback"
    with pytest.raises(ValueError, match="Stats receipt changed"):
        if operation == "prepare":
            feedback.prepare_feedback(tmp_path, now=NOW, output=output)
        elif operation == "save":
            feedback.save_feedback_review(tmp_path, run, response, now=NOW)
        else:
            feedback.load_feedback_review(
                tmp_path, saved, require_latest_stats=operation == "load_latest",
            )
    assert len(loaded) == len(replacement_receipts) == 1
    assert not output.exists()
    assert {p: p.read_bytes() for p in run.iterdir() if p.is_file()} == originals
