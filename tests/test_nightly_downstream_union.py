"""Common late-recovery routes retain causal inputs and exact publication roles."""
import json
from pathlib import Path
from types import SimpleNamespace

import pandas as pd
import pytest

from ml.artifacts import file_checksum, verify_manifest, write_manifest
from ml.gameplan_trade_planning import publish_trade_plan
from ml.nightly_recovery import make_recovery, verify_recovery
from test_account_gameplan_producer_prices import producer_case
from test_gameplan_archive_seconds import native_headers, fixtures, START
from test_nightly_recovery import configured


def test_late_action_date_preserves_information_and_actual_archive_acquisition_clocks(configured, monkeypatch):
    import ml.nightly_gameplan as gameplan
    import ml.gameplan_archive_features as archive
    root = Path(configured["datastore"])
    loop = root / "loop"
    loop.mkdir()
    samples = pd.DataFrame({"symbol": ["AAPL", "MSFT"], "feature": [1., 2.]})
    samples.to_parquet(loop / "samples.parquet")
    samples.to_parquet(loop / "predictions.parquet")
    monkeypatch.setattr(gameplan, "read_current_publication", lambda _: SimpleNamespace(run_directory=loop, manifest={}))
    monkeypatch.setattr(gameplan, "_configured_symbols", lambda *a: ("AAPL", "MSFT"))
    monkeypatch.setattr(gameplan, "_feature_columns", lambda *a: ("feature",))
    monkeypatch.setattr("ml.gameplan_model_feedback.load_feedback_review", lambda *a, **k: {})
    now, cutoff = pd.Timestamp("2026-10-09T14:30Z"), pd.Timestamp("2026-10-09T10:59:59.999999Z")
    def sources(*args, **kwargs):
        assert kwargs["available_at"] == cutoff
        return pd.DataFrame({"symbol": ["AAPL", "MSFT"],
            "action_date": [pd.Timestamp("2026-10-09").date()] * 2,
            "source_action_start": [pd.Timestamp("2026-10-09T11:00Z")] * 2,
            "source_feature_cutoff": [pd.Timestamp("2026-10-09T00:00Z")] * 2})
    monkeypatch.setattr(gameplan, "select_prior_session_sources", sources)
    def archive_sources(*args, **kwargs):
        assert kwargs["available_at"] == cutoff
        assert kwargs["evidence_available_at"] == now
        raise RuntimeError("Verified separate archive and information clocks")
    monkeypatch.setattr(archive, "load_archive_feature_sources", archive_sources)
    with pytest.raises(RuntimeError, match="separate archive"):
        gameplan.run_nightly_gameplan_once(root, run_timestamp=now, stock_only=True,
            independent_stock_horizons=True, model_feedback=root / "proposal.json",
            late_action_date="2026-10-09", archive_history=True, stock_price_source="xnas-itch-archive-v1")


def test_mixed_preparation_recovery_routes_are_rejected_before_reading_data(tmp_path):
    from ml.nightly_gameplan import run_nightly_gameplan_once
    with pytest.raises(ValueError, match="Choose one"):
        run_nightly_gameplan_once(tmp_path, stock_only=True, independent_stock_horizons=True,
            recovery_spec=tmp_path / "must-not-read.json", late_action_date="2026-10-09")


def test_late_archive_acquisition_does_not_include_future_seconds_or_unfinished_minutes(tmp_path, native_headers):
    from ml.gameplan_archive_seconds import verify_second_minute_overlap
    paths = fixtures(tmp_path)
    original = {p: file_checksum(p) for folder in paths for p in folder.iterdir()}
    cutoff = START + pd.Timedelta(seconds=30)
    with pytest.raises(ValueError, match="after cutoff"):
        verify_second_minute_overlap(tmp_path, symbols=("COST",), available_at=cutoff)
    report, _ = verify_second_minute_overlap(tmp_path, symbols=("COST",), available_at=cutoff,
                                           evidence_available_at="2026-09-23T01:00Z")
    assert report["available_at"] == cutoff.isoformat()
    assert report["evidence_available_at"] == pd.Timestamp("2026-09-23T01:00Z").isoformat()
    data = report["by_symbol"]["COST"]
    assert data["second_rows_after_cutoff"] == data["minute_rows_after_cutoff"] == 1
    assert data["overlap_minutes"] == 0
    assert {p: file_checksum(p) for p in original} == original
    with pytest.raises(ValueError, match="precedes"):
        verify_second_minute_overlap(tmp_path, symbols=("COST",), available_at=cutoff,
                                     evidence_available_at=START)


def late_source(case, mode):
    manifest = json.loads((case.source / "manifest.json").read_text())
    receipt = json.loads((case.source / "receipt.json").read_text())
    if mode == "normal":
        return
    frame = pd.read_parquet(case.source / "forecasts.parquet")
    frame["frozen_at"] = pd.Timestamp("2026-10-05T12:00Z")
    frame.to_parquet(case.source / "forecasts.parquet", index=False)
    receipt["published_at"] = "2026-10-05T12:10:00Z"
    if mode == "atlas_late":
        manifest["configuration"].update(publication_mode="LATE_RECOVERY", late_action_date="2026-10-05")
    else:
        path = case.root / "original-late-preparation.json"
        path.write_text(json.dumps(make_recovery({"actor": "Scout", "datastore": str(case.root)},
            "2026-10-05T12:00Z", reason="Original reviewed late research preparation")))
        evidence = verify_recovery(case.root, path, "2026-10-05T12:10Z")
        manifest["configuration"].update(late_preparation=evidence,
            training_information_cutoff=evidence["authorization"]["training_information_cutoff"])
    write_manifest(case.source, run_timestamp=manifest["run_timestamp"], input_files=[],
        output_files=list(manifest["output_files"]), configuration=manifest["configuration"], datastore_root=case.root)
    receipt["manifest_checksum_sha256"] = file_checksum(case.source / "manifest.json")
    (case.source / "receipt.json").write_text(json.dumps(receipt))


def continuation(case):
    from ml.preparation_deadline import RECOVERY_VERSION
    record = {"schema_version": RECOVERY_VERSION, "scope": "PINNED_STOCK_PLANNING_AND_ACTUALS_ONLY",
        "operator_authorized": True, "orders_authorized": False,
        "authorization_text": "Finish this already accepted planning tail", "authorization_source": "offline fixture",
        "gameplan_run": case.source.relative_to(case.root).as_posix(),
        "gameplan_receipt_sha256": file_checksum(case.source / "receipt.json"), "action_date": "2026-10-05",
        "original_session_deadline_at": "2026-10-05T11:00Z", "original_deadline_at": "2026-10-05T19:00Z",
        "approved_at": "2026-10-05T19:10Z", "expires_at": "2026-10-05T21:00Z"}
    path = case.root / "tail-continuation.json"
    path.write_text(json.dumps(record))
    return path, record


@pytest.mark.parametrize("mode", ["normal", "atlas_late", "scout_late"])
def test_exact_continuation_plans_accepted_forecasts_across_recovery_routes(producer_case, mode):
    research = True
    case = producer_case
    late_source(case, mode)
    path, record = continuation(case)
    before = {p: file_checksum(p) for p in case.source.iterdir()}
    kwargs = {"late_action_date": "2026-10-05"} if mode == "atlas_late" else {}
    run = publish_trade_plan(case.root, gameplan_run=case.source, deadline="2026-10-05T19:00Z",
        deadline_exception=path, research_producer_only=research, account_producer_only=not research,
        clock=lambda: pd.Timestamp("2026-10-05T19:20Z"), price_loader=lambda *a, **k: (pd.DataFrame(), (), {}), **kwargs)
    saved = json.loads((run / "report.json").read_text())
    assert pd.Timestamp(saved["deadline_at"]) == pd.Timestamp("2026-10-05T11:00Z")
    assert pd.Timestamp(saved["effective_deadline_at"]) == pd.Timestamp(record["expires_at"])
    assert saved["publication_mode"] == ("RESEARCH_PRODUCER_SOURCE" if research else "ACCOUNT_PRODUCER_SOURCE")
    assert not (run / "account-snapshot.json").exists()
    assert {p: file_checksum(p) for p in before} == before
    from app.ui.gameplan_data import load_gameplan
    view = load_gameplan(case.root)
    assert view.run_directory == run and not view.projection_available
    assert all(row.quantity is None for row in view.forecasts)


@pytest.mark.parametrize("fault", ["source", "session", "deadline", "expired", "ordinary"])
def test_late_normal_source_requires_exact_continuation_not_arbitrary_deadline(producer_case, fault):
    case = producer_case
    path, record = continuation(case)
    if fault == "source": record["gameplan_receipt_sha256"] = "0" * 64
    if fault == "session": record["original_session_deadline_at"] = "2026-10-05T12:00Z"
    if fault == "deadline": record["original_deadline_at"] = "2026-10-05T18:00Z"
    if fault == "expired": record["expires_at"] = "2026-10-05T19:15Z"
    if fault == "ordinary": record["schema_version"] = "operator-preparation-deadline-exception-v1"
    path.write_text(json.dumps(record))
    with pytest.raises(ValueError):
        publish_trade_plan(case.root, gameplan_run=case.source, deadline="2026-10-05T19:00Z",
            deadline_exception=path, research_producer_only=True, clock=lambda: pd.Timestamp("2026-10-05T19:20Z"),
            price_loader=lambda *a, **k: pytest.fail("Invalid continuation reached prices"))
