"""Current Stats retain immutable science without estimated-price dependencies."""
import json
import socket

import pandas as pd
import pytest

from ml.artifacts import file_checksum, verify_manifest, write_manifest
from ml.gameplan_actuals_review import (_match_trade_rows, _saved_trade_plan, _observed_interval, compare_forecasts,
    publish_completed_session_review)
from ml.gameplan_execution_policy import current_gameplan_policy_metadata, direction_only_rows
from ml.stock_direction_policy import (STOCK_DIRECTION_POLICY_VERSION, SCOUT_STOCK_DIRECTION_POLICY_VERSION,
    saved_direction_policy_summary)
from test_gameplan_actuals_review import forecasts, prices, publication_case


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def denied(*args, **kwargs):
        pytest.fail("Stats regression must remain offline")
    monkeypatch.setattr(socket.socket, "connect", denied)
    monkeypatch.setattr(socket, "create_connection", denied)


@pytest.mark.parametrize("seconds", [1, 60, 300])
def test_stats_uses_the_real_native_source_interval(seconds):
    assert _observed_interval(pd.DataFrame({"observation_interval_seconds": [seconds, seconds]})) == pd.Timedelta(seconds=seconds)
    assert _observed_interval(pd.DataFrame()) == pd.Timedelta(minutes=1)


@pytest.mark.parametrize("seconds", [[60, 300], [2, 2], [None, 60], ["invalid"]])
def test_stats_rejects_mixed_or_invalid_native_intervals(seconds):
    with pytest.raises(ValueError, match="one native observation interval"):
        _observed_interval(pd.DataFrame({"observation_interval_seconds": seconds}))


@pytest.mark.parametrize("version", [None, STOCK_DIRECTION_POLICY_VERSION, SCOUT_STOCK_DIRECTION_POLICY_VERSION])
def test_current_stats_separate_execution_call_from_recorded_scientific_score(version):
    science = forecasts().iloc[:1].assign(calibrated_probability=.48, raw_probability=.481234567890123,
        direction="NO_EDGE", model_status="RESEARCH_NOT_PROMOTED", frozen_at="2026-09-09T10:00Z")
    if version is not None:
        science["direction_policy_version"] = version
        science["direction_up_threshold"] = .5001
        science["direction_down_threshold"] = .4999
    original = science.copy(deep=True)
    trades, _ = direction_only_rows(science, action_date="2026-09-09")
    result = compare_forecasts(science, prices(), observed_at="2026-09-10T00:00Z", trade_rows=trades)
    pd.testing.assert_frame_equal(science, original)
    row = result.iloc[0]
    assert row.direction == "NO_EDGE" and pd.isna(row.direction_correct)
    assert row.execution_direction == "BEARISH"
    assert row.execution_direction_policy_version == STOCK_DIRECTION_POLICY_VERSION
    assert row.execution_direction_up_threshold == .5001 and row.execution_direction_down_threshold == .4999
    assert row.calibrated_probability == .48 and row.raw_probability == original.iloc[0].raw_probability
    assert row.frozen_at == "2026-09-09T10:00Z"
    assert row.model_brier_score == pytest.approx((.48 - 1) ** 2)
    policy = saved_direction_policy_summary(result.to_dict("records"))
    if version is None:
        assert pd.isna(row.direction_policy_version)
        assert policy["unrecorded_direction_policy_forecasts"] == 1 and policy["direction_policy_versions"] == []
    else:
        assert row.direction_policy_version == version and policy["direction_policy_versions"] == [version]
    assert pd.isna(row.entry_price_error) and pd.isna(row.entry_price_in_range)


@pytest.mark.parametrize("field,value", [("calibrated_probability", .480000000001),
    ("raw_probability", .47), ("frozen_at", "2026-09-09T10:01Z"),
    ("source_saved_direction", "BULLISH"), ("direction_up_threshold", .51),
    ("direction_based_action", "HOLD"), ("direction_based_trade_quantity", .001)])
def test_current_stats_reject_tampered_science_or_execution_derivation(field, value):
    science = forecasts().assign(calibrated_probability=.48, raw_probability=.47,
        direction="NO_EDGE", model_status="RESEARCH_NOT_PROMOTED", frozen_at="2026-09-09T10:00Z")
    trades, _ = direction_only_rows(science, action_date="2026-09-09")
    # Keep this mutation meaningful for the existing saved raw probability.
    trades.loc[trades.index[0], field] = .471 if field == "raw_probability" else value
    with pytest.raises(ValueError, match="changed a frozen forecast"):
        _match_trade_rows(science, trades)


def install_current_trade_fixture(case):
    science_path = case.original.run_directory / "forecasts.parquet"
    science = pd.read_parquet(science_path).assign(calibrated_probability=.48, direction="NO_EDGE",
        model_status="RESEARCH_NOT_PROMOTED")
    science.to_parquet(science_path, index=False)
    trades, _ = direction_only_rows(science, action_date="2026-09-09")
    run = case.original_trade
    trades.to_parquet(run / "trade-plan.parquet", index=False)
    (run / "planning-price-path.json").unlink()
    receipt = json.loads((run / "receipt.json").read_text())
    metadata = current_gameplan_policy_metadata("2026-09-09")
    source = case.original.run_directory.relative_to(case.root).as_posix()
    write_manifest(run, run_timestamp=pd.Timestamp("2026-09-09T00:00Z"), input_files=[],
        output_files=["trade-plan.parquet"], configuration={**metadata,
            "source_gameplan_run": source,
            "source_receipt_sha256": file_checksum(case.original.run_directory / "receipt.json")})
    receipt.update(metadata, manifest_sha256=file_checksum(run / "manifest.json"))
    (run / "receipt.json").write_text(json.dumps(receipt), encoding="utf-8")
    return science


def test_saved_current_trade_plan_reads_and_publishes_stats_without_price_path(publication_case, monkeypatch):
    from ml import gameplan_actuals_review
    case = publication_case
    science = install_current_trade_fixture(case)
    protected = {path: path.read_bytes() for path in (case.original.run_directory / "forecasts.parquet",
        case.original.run_directory / "receipt.json", case.original_trade / "trade-plan.parquet")}
    original_json = gameplan_actuals_review._json
    def checked(path):
        assert path.name != "planning-price-path.json"
        return original_json(path)
    monkeypatch.setattr(gameplan_actuals_review, "_json", checked)
    def unavailable(*args, **kwargs):
        raise RuntimeError("Synthetic private execution summary unavailability")
    monkeypatch.setattr("ml.gameplan_execution_report.publish_execution_summary", unavailable)
    selected, trades, path = _saved_trade_plan(case.root, case.original)
    assert selected == case.original_trade and path is None
    assert trades.calibrated_probability.eq(.48).all()
    run = publish_completed_session_review(case.root, action_date="2026-09-09",
        price_loader=case.loader, clock=lambda: pd.Timestamp("2026-09-10T04:05Z"))
    manifest = verify_manifest(run)
    assert not any("planning-price" in name for name in manifest["input_files"])
    report = json.loads((run / "report.json").read_text())
    result = pd.read_parquet(run / "forecast-results.parquet")
    assert report["status"] == "COMPLETE" and report["unrecorded_direction_policy_forecasts"] == len(science)
    assert result.direction.eq("NO_EDGE").all() and result.execution_direction.eq("BEARISH").all()
    assert result.direction_policy_version.isna().all()
    assert all(path.read_bytes() == before for path, before in protected.items())


def test_saved_current_trade_plan_rejects_manifest_receipt_recipe_disagreement(publication_case):
    case = publication_case
    install_current_trade_fixture(case)
    path = case.original_trade / "receipt.json"
    receipt = json.loads(path.read_text())
    receipt["execution_quantity_basis"] = "obsolete_estimated_quantity"
    path.write_text(json.dumps(receipt), encoding="utf-8")
    with pytest.raises(ValueError, match="inconsistent recipe"):
        _saved_trade_plan(case.root, case.original)
