import json
from datetime import datetime

import pytest

from app.ui.gameplan_stats_data import load_gameplan_stats
from ml.artifacts import verify_manifest
from ml.gameplan_actuals_review import publish_completed_session_review
from ml.gameplan_stats_handoff import export_stats_package


def _publish(root, monkeypatch):
    monkeypatch.setattr("ml.gameplan_actuals_review._latest_saved_gameplan", lambda *args: None)
    return publish_completed_session_review(root, action_date="2026-10-14", reviewed_at="2026-10-15T01:00:00Z",
        clock=lambda: datetime.fromisoformat("2026-10-15T01:00:00+00:00"))


def test_execution_publication_runs_after_durable_prediction_stats(tmp_path, monkeypatch):
    observed = []

    def publish_execution(root, session, *, observed_at):
        review = load_gameplan_stats(root, session)
        verify_manifest(review.run_directory)
        assert (root / f"ml/gameplan-actuals-review-by-date/{session}/Gameplan-results.md").is_file()
        observed.append((root, session, observed_at.isoformat()))
        destination = root / f"state/gameplan-execution-reports/{session}/summary.json"
        destination.parent.mkdir(parents=True)
        destination.write_text(json.dumps({"private": "PRIVATE_EXECUTION_SENTINEL", "order_quantity": 17}))
        return destination

    monkeypatch.setattr("ml.gameplan_execution_report.publish_execution_summary", publish_execution)
    run = _publish(tmp_path, monkeypatch)
    assert observed == [(tmp_path, "2026-10-14", "2026-10-15T01:00:00+00:00")]
    assert "execution" not in (run / "manifest.json").read_text().lower()
    assert "PRIVATE_EXECUTION_SENTINEL" not in (run / "report.json").read_text()
    exported = export_stats_package(tmp_path, producer="Atlas", symbols=("EXAMPLE",),
                                    destination=tmp_path / "exports/forecast-only.json", action_date="2026-10-14")
    package = exported.read_text()
    assert "PRIVATE_EXECUTION_SENTINEL" not in package and "order_quantity" not in package
    assert not list(run.glob("*execution*"))


@pytest.mark.parametrize("exception", [OSError("PRIVATE_FILE_PATH"), ValueError("PRIVATE_ORDER_ID"), RuntimeError("PRIVATE_BROKER_REASON")])
def test_execution_failure_cannot_fail_prediction_or_leak_private_error(tmp_path, monkeypatch, caplog, exception):
    def fail(*args, **kwargs):
        raise exception

    monkeypatch.setattr("ml.gameplan_execution_report.publish_execution_summary", fail)
    run = _publish(tmp_path, monkeypatch)
    verify_manifest(run)
    review = load_gameplan_stats(tmp_path, "2026-10-14")
    assert review.run_directory == run and review.metrics().total == 0
    assert "prediction Stats remain published" in caplog.text and "PRIVATE_" not in caplog.text
    assert not (tmp_path / "state/gameplan-execution-reports").exists()


def test_missing_execution_ledger_publishes_private_coverage_state(tmp_path, monkeypatch):
    from app.ui.gameplan_execution_data import load_gameplan_execution

    run = _publish(tmp_path, monkeypatch)
    verify_manifest(run)
    snapshot = load_gameplan_execution(tmp_path, "2026-10-14")
    assert snapshot.state == "missing" and snapshot.counts is None
    assert snapshot.report_path.is_file()
    assert snapshot.report["session_date"] == "2026-10-14"
