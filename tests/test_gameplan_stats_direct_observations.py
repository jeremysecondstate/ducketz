"""Native original observations update Stats without importing a peer ledger."""
from copy import deepcopy
import json
from pathlib import Path
import socket

import pandas as pd
import pytest

from ml.artifacts import file_checksum
from ml.joint_capital_plan import content_sha256
import ml.gameplan_stats_outcome_revision as revision
import test_gameplan_stats_outcome_revision as saved

DAY = "2026-10-23"
NOW = "2026-10-27T00:30:00Z"


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    def deny(*a, **k):
        raise AssertionError("Direct scientific Stats made a network call")
    monkeypatch.setattr(socket.socket, "connect", deny)
    monkeypatch.setattr(socket, "create_connection", deny)


@pytest.fixture
def direct(tmp_path):
    original = saved.forecast("AAPL", "1d@D+2")
    original.update(model_group="1d", target_role="FUTURE_RESEARCH",
        target_window_start="2026-10-26T11:00:00Z", target_window_end="2026-10-27T00:00:00Z")
    source_row = {key: value for key, value in original.items() if key not in revision._OUTCOMES}
    source_row.update(target_price_source_contract="observed-multi-source-stock-v2",
        target_price_dataset="OBSERVED_SOURCE_SELECTION_V2")
    package = dict(schema_version="joint-capital-owner-package-v1", owner_id="atlas", action_date=DAY,
        source_hashes=dict(forecasts_sha256="a"*64), forecasts=[source_row],
        content_sha256=dict(forecasts=content_sha256([source_row])))
    package["package_sha256"] = content_sha256(package)
    source = tmp_path/"original-plan.json"
    saved.save(source, package)
    root = tmp_path/"datastore"
    parent = saved.base(root, rows=[original, saved.forecast("DBX", "4h@12:00", status="EVALUATED")])
    raw = tmp_path/"retained-native-fixture.json"
    saved.save(raw, dict(provider="fmp", fixture=True, entry_price=100, exit_price=110))
    digest = file_checksum(raw)
    def point(timestamp, opened, closed):
        return dict(timestamp=timestamp, interval_seconds=300, recorded_at="2026-10-27T00:05:00Z",
            input_sha256=digest, open=opened, high=max(opened,closed)+1, low=min(opened,closed)-1,
            close=closed, volume=10, native_flags={})
    proof = dict(schema_version="stock-original-native-observations-v1", source_plan_file_sha256=file_checksum(source),
        observed_at="2026-10-27T00:10:00Z", source_inputs_sha256=[digest], missing_original_ids=[],
        observations=[dict(id=original["id"], source="fmp:historical-chart:5min", dataset="FMP.HISTORICAL_CHART_5MIN",
            entry=point("2026-10-26T11:00:00Z",100,100), exit=point("2026-10-26T23:55:00Z",109,110))])
    proof_path = tmp_path/"observation-proof.json"
    saved.save(proof_path, proof)
    return dict(root=root, parent=parent, source=source, proof=proof_path, original=source_row,
        receipt=dict(sha256=digest), output=tmp_path/"revision.json")

def export(case, **changes):
    args = dict(base_run=case["parent"], expected_base_receipt_sha256=file_checksum(case["parent"]/"receipt.json"),
        source_plan=case["source"], expected_source_plan_sha256=file_checksum(case["source"]),
        forecast_ids=[case["original"]["id"]], observation_proof=case["proof"],
        expected_observation_proof_sha256=file_checksum(case["proof"]), producer="Atlas",
        destination=case["output"], reviewed_at=NOW)
    args.update(changes)
    return revision.export_observed_revision(case["root"], **args)


def adopt(case):
    return revision.adopt_revision(case["root"], package=case["output"], expected_sha256=file_checksum(case["output"]),
        base_run=case["parent"], expected_base_receipt_sha256=file_checksum(case["parent"]/"receipt.json"), reviewed_at=NOW)


def test_native_scoring_export_adoption_and_retry_preserve_old_history(direct):
    before = pd.read_parquet(direct["parent"]/"forecast-results.parquet")
    pointer = direct["root"]/"ml/gameplan-actuals-review-latest/run.json"
    old_pointer = pointer.read_bytes()
    export(direct)
    payload = json.loads(direct["output"].read_bytes())
    assert payload["schema_version"] == revision.DIRECT_REVISION_VERSION
    assert payload["source_evaluation"] is None
    proof = payload["source_observations"]
    assert proof["source_plan_file_sha256"] == file_checksum(direct["source"])
    assert proof["source_inputs_sha256"] == [direct["receipt"]["sha256"]]
    update = payload["updates"][0]
    assert update["id"] == direct["original"]["id"] and update["actual_return"] == pytest.approx(.1)
    assert update["model_brier_score"] == pytest.approx(.36) and update["direction_correct"] is False
    assert pointer.read_bytes() == old_pointer
    assert not (direct["root"]/"ml/gameplan-evaluation-latest").exists()
    assert not (direct["root"]/"state").exists()
    run = adopt(direct)
    assert adopt(direct) == run
    after = pd.read_parquet(run/"forecast-results.parquet")
    pd.testing.assert_frame_equal(after.iloc[[1]], before.iloc[[1]], check_exact=True, check_dtype=False)
    frozen = [c for c in before if c not in revision._OUTCOMES]
    pd.testing.assert_frame_equal(after[frozen], before[frozen], check_exact=True)
    assert json.loads((run/"report.json").read_bytes())["outcome_revision_source_observations"] == proof
    assert not (direct["root"]/"ml/gameplan-evaluation-latest").exists()


def test_wrong_selected_source_hash_does_not_publish(direct):
    with pytest.raises(ValueError, match="exact selected hash"):
        export(direct, expected_source_plan_sha256="f"*64)
    assert not direct["output"].exists()


def test_wrong_observation_artifact_hash_does_not_publish(direct):
    with pytest.raises(ValueError, match="observation artifact"):
        export(direct, expected_observation_proof_sha256="f"*64)
    assert not direct["output"].exists()


@pytest.mark.parametrize("field,value", [
    ("volume", 0), ("open", True), ("high", 90), ("low", -1),
    ("interval_seconds", 60), ("recorded_at", "2026-10-27T01:00:00Z"),
    ("timestamp", "2026-10-27T00:10:00Z"), ("native_flags", {"synthetic": True}),
    ("native_flags", {"no_trade": "false"}), ("input_sha256", "0"*64),
])
def test_invalid_native_point_never_becomes_a_score(direct, field, value):
    proof = json.loads(direct["proof"].read_bytes())
    proof["observations"][0]["exit"][field] = value
    saved.save(direct["proof"], proof)
    with pytest.raises(ValueError): export(direct)
    assert not direct["output"].exists()


def test_native_endpoint_outside_original_boundary_is_not_scored(direct):
    proof = json.loads(direct["proof"].read_bytes())
    proof["observations"][0]["exit"]["timestamp"] = "2026-10-26T23:49:00Z"
    saved.save(direct["proof"], proof)
    with pytest.raises(ValueError, match="original target boundary"):
        export(direct)
    assert not direct["output"].exists()


@pytest.mark.parametrize("field,value", [
    ("calibrated_probability", .7), ("direction", "BULLISH"),
    ("target_window_end", "2026-10-28T00:00:00Z"),
    ("direction_policy_version", "stock-direction-5001-4999-v3"),
])
def test_changed_original_even_with_valid_package_digest_is_rejected(direct, field, value):
    payload = json.loads(direct["source"].read_bytes())
    selected = next(r for r in payload["forecasts"] if r["id"] == direct["original"]["id"])
    selected[field] = value
    payload["content_sha256"]["forecasts"] = content_sha256(payload["forecasts"])
    payload["package_sha256"] = content_sha256({k: v for k, v in payload.items() if k != "package_sha256"})
    saved.save(direct["source"], payload)
    with pytest.raises(ValueError):
        export(direct)
    assert not direct["output"].exists()


@pytest.mark.parametrize("clock", ["2026-10-26T23:59:00Z", "2026-10-27T00:03:00Z"])
def test_future_target_or_later_received_native_evidence_cannot_be_scored(direct, clock):
    with pytest.raises(ValueError):
        export(direct, reviewed_at=clock)
    assert not direct["output"].exists()


@pytest.mark.parametrize("mutation", ["input_hash", "source_contract", "source", "score", "private_path", "duplicate_ids"])
def test_direct_portable_provenance_tampering_is_rejected(direct, mutation):
    export(direct)
    payload = json.loads(direct["output"].read_bytes())
    proof = payload["source_observations"]
    if mutation == "input_hash": proof["source_inputs_sha256"] = ["0"*64]
    elif mutation == "source_contract":
        proof["price_source_contract"] = "observed-multi-source-stock-v1"
        proof["price_dataset"] = "OBSERVED_SOURCE_SELECTION_V1"
    elif mutation == "source": payload["updates"][0]["target_observation_source"] = "fake:source"
    elif mutation == "score": payload["updates"][0]["actual_return"] = .9
    elif mutation == "private_path": proof["source_plan_file_sha256"] = "C:/private/source.json"
    else: proof["original_ids"].append(proof["original_ids"][0])
    saved.save(direct["output"], payload)
    pointer = direct["root"]/"ml/gameplan-actuals-review-latest/run.json"
    before = pointer.read_bytes()
    with pytest.raises(ValueError): adopt(direct)
    assert pointer.read_bytes() == before
