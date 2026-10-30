"""Offline frozen-evidence storage checks; trainers/providers are fixture doubles."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json
import hashlib

import numpy as np
import pytest

from ml.intraday.prediction import ContractError, digest, encoded
from ml.intraday.snapshot_references import (
    model_market_input_metadata, resolve_model_market_input_metadata,
)


def original_snapshot(rows=512):
    ref = datetime(2026, 10, 8, 17, tzinfo=timezone.utc)
    return {
        "X": [[float(index % 2)] for index in range(rows)],
        "y": [index % 2 for index in range(rows)],
        "inference_X": [[.25]], "feature_names": ["x"],
        "label_available_at": [(ref-timedelta(minutes=30)).isoformat()] * rows,
        "reference_at": [(ref-timedelta(minutes=90)).isoformat()] * rows,
        "target_close_at": [(ref-timedelta(minutes=45)).isoformat()] * rows,
        "cutoff": (ref+timedelta(minutes=15)).isoformat(),
        "inference": {"symbol": "AAPL", "reference_at": ref.isoformat(),
            "reference_open": "100", "information_available_at": (ref+timedelta(minutes=15)).isoformat(),
            "availability_kind": "actual"},
        "manifest": {"synthetic_policy": "include"}, "candles": [],
        "market_inputs": {
            "version": "original-feature-contract", "training_rows": rows,
            "training": [{"decision_at": str(index), "sources": {"source": {
                "received_at": "original-clock", "receipt": "x" * 600}}} for index in range(rows)],
            "inference": {"sources": {"source": {"receipt": "original-inference"}}},
            "training_nonmissing_rows_by_feature": {"x": rows},
            "missing_encoding": "original", "receipt_rule": "original",
        },
    }


def test_training_history_is_retained_once_and_resolves_exact_original_metadata():
    snapshot = original_snapshot()
    original = encoded(snapshot)
    compact = model_market_input_metadata(snapshot, digest(snapshot))
    assert "training" not in compact
    assert len(encoded(compact)) < len(encoded(snapshot["market_inputs"])) / 100
    assert resolve_model_market_input_metadata(compact, snapshot) == snapshot["market_inputs"]
    assert encoded(snapshot) == original
    assert compact["inference"] == snapshot["market_inputs"]["inference"]
    assert compact["training_nonmissing_rows_by_feature"] == {"x": 512}
    assert resolve_model_market_input_metadata(snapshot["market_inputs"], {}) == snapshot["market_inputs"]
    assert model_market_input_metadata({"market_inputs": None}, "unused") is None


@pytest.mark.parametrize("field,value", [
    ("snapshot_hash", "0" * 64), ("field", "X"), ("rows", True),
    ("rows", 511), ("sha256", "f" * 64), ("schema", "unknown"),
])
def test_reference_rejects_changed_identity_counts_or_provenance(field, value):
    snapshot = original_snapshot()
    compact = deepcopy(model_market_input_metadata(snapshot, digest(snapshot)))
    compact["training_reference"][field] = value
    with pytest.raises(ContractError):
        resolve_model_market_input_metadata(compact, snapshot)


def test_reference_rejects_changed_frozen_rows_or_inline_policy():
    snapshot = original_snapshot()
    compact = deepcopy(model_market_input_metadata(snapshot, digest(snapshot)))
    changed = deepcopy(snapshot)
    changed["market_inputs"]["training"][0]["decision_at"] = "different"
    with pytest.raises(ContractError):
        resolve_model_market_input_metadata(compact, changed)
    compact["receipt_rule"] = "different"
    with pytest.raises(ContractError):
        resolve_model_market_input_metadata(compact, snapshot)
    with pytest.raises(ContractError):
        model_market_input_metadata(snapshot, "0" * 64)


def test_runtime_preserves_inputs_targets_predictions_and_original_receipts(tmp_path, monkeypatch):
    from ml.intraday.runtime import ShadowRunner, ResearchConfig
    from ml.intraday.store import ShadowStore
    import ml.market_provenance

    # A fixed provenance fixture avoids machine-dependent source inspection.
    monkeypatch.setattr(ml.market_provenance, "capture_runtime", lambda *a, **k: {"sha256": "a" * 64})
    snapshot = original_snapshot()
    before = encoded(snapshot)
    identity = digest(snapshot)
    ref = datetime(2026, 10, 8, 17, tzinfo=timezone.utc)
    fitted_metadata = []

    def trainer(X, y, inference_X, *, config, dataset_metadata, **kwargs):
        np.testing.assert_array_equal(X, snapshot["X"])
        np.testing.assert_array_equal(y, snapshot["y"])
        np.testing.assert_array_equal(inference_X, snapshot["inference_X"])
        assert kwargs["feature_names"] == snapshot["feature_names"]
        assert kwargs["label_available_at"] == snapshot["label_available_at"]
        assert kwargs["target_close_at"] == snapshot["target_close_at"]
        assert kwargs["training_cutoff"] == snapshot["cutoff"]
        assert tuple(config) == ("fixture_a", "fixture_b")
        fitted_metadata.append(dataset_metadata)
        return {"outputs": [{"name": name, "status": "success", "probability_not_down": .6001,
            "metadata": {"dataset_metadata": dataset_metadata}} for name in config],
            "configured_count": 2, "successful_count": 2, "complete": True,
            "probability_not_down": .6001, "training_seconds": 0., "inference_seconds": 0.}

    store = ShadowStore(tmp_path / "intraday/system-1b/models.sqlite3",
        snapshot_directory=tmp_path / "intraday/system-1a/ready/snapshots", chunked_snapshots=True)
    runner = ShadowRunner(store=store, pc="Atlas", symbols=("AAPL",),
        config=ResearchConfig(models=("fixture_a", "fixture_b")), trainer=trainer,
        prepared_source=lambda *args: (identity, snapshot), clock=lambda: ref+timedelta(minutes=16))
    result = runner.run_cycle({}, reference_at=ref, mode="live")
    assert not result["failures"]
    forecast = result["forecasts"]["AAPL"]
    assert forecast["probability_not_down"] == .6001
    assert forecast["target_close"] == (ref+timedelta(minutes=45)).isoformat()
    assert forecast["snapshot_hash"] == identity
    assert encoded(snapshot) == before
    assert store.inputs(result["cycle_id"], "AAPL") == (identity, snapshot)
    metadata = fitted_metadata[0]["market_inputs"]
    assert resolve_model_market_input_metadata(metadata, snapshot) == snapshot["market_inputs"]
    with store.connect() as db:
        raw = db.execute("SELECT payload FROM events WHERE kind='models_completed'").fetchone()[0]
        frozen = db.execute("SELECT payload FROM snapshots WHERE id=?", (identity,)).fetchone()[0]
    assert len(raw) < 5000  # Full receipt history would exceed 700 KB for two models.
    assert len(frozen) < 200
    assert json.loads(raw)["result"]["outputs"][0]["probability_not_down"] == .6001


def test_compacted_event_reads_exact_original_full_fitted_evidence(tmp_path):
    from ml.intraday.split_execution import read_model_event, seal_evidence, MODEL_EVENT_REFERENCE
    fitted = {"outputs": [{"probability_not_down": .5001, "metadata": {"original": [1, 2, 3]}}],
              "successful_count": 1, "target_version": "original-target"}
    original = {"attempt": "original-attempt", "result": fitted}
    original_raw = encoded(original).encode()
    reference = seal_evidence(tmp_path, {"fitted": fitted, "snapshot_hash": "a" * 64})
    compact = {"schema": MODEL_EVENT_REFERENCE, "attempt": original["attempt"],
        "original_sha256": hashlib.sha256(original_raw).hexdigest(),
        "original_bytes": len(original_raw), "full_models": reference}
    assert read_model_event(original_raw, evidence_root=tmp_path) == original
    assert encoded(read_model_event(encoded(compact), evidence_root=tmp_path)).encode() == original_raw
    for changes in ({"attempt": "changed"}, {"original_sha256": "0" * 64},
                    {"original_bytes": True}, {"original_bytes": len(original_raw) + 1},
                    {"schema": "unknown"}, {"extra": "unbound"}):
        with pytest.raises(ContractError):
            read_model_event(encoded({**compact, **changes}), evidence_root=tmp_path)
    (tmp_path / reference["file"]).write_bytes(b"changed")
    with pytest.raises(ContractError):
        read_model_event(encoded(compact), evidence_root=tmp_path)
