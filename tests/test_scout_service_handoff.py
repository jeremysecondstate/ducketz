"""Offline immutable service boundary and exact original sizing fixtures."""
from copy import deepcopy
from datetime import timedelta
import json
import sqlite3
import threading
import time

import numpy as np
import pytest

from ml.intraday.prediction import ContractError, STEP, digest, encoded, instant
from ml.intraday.runtime import ResearchConfig, ShadowRunner
from ml.intraday.service_handoff import ReadySpool, ServiceStore, full_snapshot, execution_envelope
from ml.intraday.scout_services import PreparedHistories
from ml.intraday.starter_sizing import size_forecast
from tests.test_intraday_starter_sizing import fixture
from tests.test_intraday_runtime import trained


def prepared(tmp_path, symbol="AAPL", *, probability=.72):
    forecast, snapshot, equity, quote = fixture(symbol=symbol, probability=probability)
    reference = instant(forecast["reference_at"])
    cutoff = instant(snapshot["cutoff"])
    snapshot.update(X=[[0.], [1.], [0.], [1.]], y=[0, 1, 0, 1], inference_X=[[1.]],
                    feature_names=["x"], label_available_at=[(reference-STEP*i).isoformat() for i in range(4, 0, -1)],
                    reference_at=[(reference-STEP*i).isoformat() for i in range(7, 3, -1)],
                    target_close_at=[(reference-STEP*i).isoformat() for i in range(4, 0, -1)],
                    source_manifest={"source": "offline-fixture"}, dataset_scope={"synthetic_policy":"include"})
    snapshot["inference"].update(availability_kind="actual", information_available_at=cutoff.isoformat())
    forecast["snapshot_hash"] = digest(snapshot)
    forecast.pop("forecast_id")
    forecast["forecast_id"] = digest(forecast)
    store = ServiceStore(tmp_path / "shadow.sqlite3", object_root=tmp_path)
    key, original = store.freeze_inputs("prep-"+symbol, symbol, snapshot)
    envelope = store.envelope("prep-"+symbol, symbol)[1]
    assert key == digest(original) == forecast["snapshot_hash"]
    return forecast, snapshot, equity, quote, store, envelope, cutoff


@pytest.mark.parametrize("probability", [.72, .28, .5001, .4999, .5])
def test_compact_execution_preserves_original_indicators_and_sizing(tmp_path, probability):
    forecast, snapshot, equity, quote, store, envelope, cutoff = prepared(tmp_path, probability=probability)
    forecast["execution_input_hash"] = digest(envelope)
    forecast.pop("forecast_id")
    forecast["forecast_id"] = digest(forecast)
    original = size_forecast(forecast, forecast_snapshot=snapshot, account_equity=equity, quote=quote,
        calculated_at=cutoff, maximum_quote_age_seconds=60).payload()
    compact = size_forecast(forecast, forecast_snapshot=envelope, account_equity=equity, quote=quote,
        calculated_at=cutoff, maximum_quote_age_seconds=60).payload()
    compact_indicators = compact.pop("indicator_snapshot")
    original_indicators = original.pop("indicator_snapshot")
    compact.pop("indicator_evidence")
    assert compact == original  # Exact decisions, IDs, terms, sizes and prices.
    assert compact_indicators["features"] == original_indicators["features"]
    assert "candles" not in compact_indicators
    from pathlib import Path
    assert json.loads(Path(envelope["indicator_object_path"]).read_bytes()) == original_indicators
    assert len(encoded(envelope)) < len(encoded(snapshot))
    assert "X" not in envelope and "candles" not in envelope


def test_atomic_publication_never_exposes_partial_manifest(tmp_path, monkeypatch):
    from ml.intraday import service_handoff
    *_, store, envelope, cutoff = prepared(tmp_path)
    spool = ReadySpool(tmp_path, pc="Scout", symbols=["AAPL"], recipe_hash="recipe", clock=lambda:cutoff)
    native = service_handoff.os.replace
    def interrupted(source, target):
        if target.parent.name == "manifests":
            raise OSError("fixture power loss before ready commit")
        return native(source, target)
    monkeypatch.setattr(service_handoff.os, "replace", interrupted)
    with pytest.raises(OSError):
        spool.publish(symbol="AAPL", reference_at=envelope["reference_at"], envelope=envelope, source_session="data-day")
    assert spool.ready("AAPL", envelope["reference_at"]) is None
    assert not list((tmp_path/"manifests").glob("*.tmp"))
    assert full_snapshot(envelope["snapshot_hash"], envelope)


def test_ready_dedup_expiry_bounded_queue_and_no_acknowledgement(tmp_path):
    *_, store, envelope, cutoff = prepared(tmp_path)
    clock = [cutoff]
    spool = ReadySpool(tmp_path, pc="Scout", symbols=["AAPL"], recipe_hash="recipe", clock=lambda:clock[0])
    records = []
    for offset in range(5):
        ref = instant(envelope["reference_at"]) - offset*STEP
        snapshot = full_snapshot(envelope["snapshot_hash"], envelope)
        snapshot["inference"]["reference_at"] = ref.isoformat()
        # Complete immutable source remains retained even when no longer live.
        store.freeze_inputs("cycle-"+str(offset), "AAPL", snapshot)
        revised = store.envelope("cycle-"+str(offset), "AAPL")[1]
        records.append(spool.publish(symbol="AAPL", reference_at=ref, envelope=revised, source_session="data-day"))
    with spool.connect() as db:
        assert db.execute("SELECT count(*) FROM ready WHERE status='ready'").fetchone()[0] <= 2
        assert db.execute("SELECT count(*) FROM ready").fetchone()[0] == 5
    clock[0] += timedelta(hours=2)
    assert spool.ready("AAPL", envelope["reference_at"]) is None
    assert len(list((tmp_path/"manifests").glob("*.json"))) == 5


@pytest.mark.parametrize("mutation", ["matrix", "projection", "manifest", "labels", "availability"])
def test_corruption_and_noncausal_snapshot_cannot_become_ready(tmp_path, mutation):
    *_, store, envelope, cutoff = prepared(tmp_path)
    spool = ReadySpool(tmp_path, pc="Scout", symbols=["AAPL"], recipe_hash="recipe", clock=lambda:cutoff)
    snapshot = full_snapshot(envelope["snapshot_hash"], envelope)
    if mutation == "matrix":
        from pathlib import Path
        Path(envelope["object_path"]).write_bytes(b"{\"X\":[]}")
    elif mutation in {"labels", "availability"}:
        if mutation == "labels":
            snapshot["label_available_at"][0] = (cutoff + STEP).isoformat()
        else:
            snapshot["inference"]["information_available_at"] = (cutoff + STEP).isoformat()
        store.freeze_inputs("changed", "AAPL", snapshot)
        envelope = store.envelope("changed", "AAPL")[1]
    elif mutation == "projection":
        envelope["indicators"]["symbol"] = "other"
        forecast = {"symbol":"AAPL", "reference_at":envelope["reference_at"], "training_cutoff":envelope["cutoff"],
                    "execution_input_hash":"original-bound-hash"}
        with pytest.raises(ContractError):
            execution_envelope(envelope["snapshot_hash"], envelope, forecast)
        return
    else:
        published = spool.publish(symbol="AAPL", reference_at=envelope["reference_at"], envelope=envelope, source_session="data-day")
        with spool.connect() as db:
            raw = db.execute("SELECT payload FROM ready").fetchone()[0]
            changed = json.loads(raw)
            changed["feature_order_hash"] = "changed"
            db.execute("UPDATE ready SET payload=?", (encoded(changed),))
        with pytest.raises(ContractError):
            spool.ready("AAPL", envelope["reference_at"])
        return
    with pytest.raises(ContractError):
        spool.publish(symbol="AAPL", reference_at=envelope["reference_at"], envelope=envelope, source_session="data-day")


def test_unrelated_model_work_continues_while_other_acquisition_missing(tmp_path):
    forecast, snapshot, equity, quote, store, envelope, cutoff = prepared(tmp_path)
    spool = ReadySpool(tmp_path, pc="Scout", symbols=["AAPL", "IONQ"], recipe_hash="recipe", clock=lambda:cutoff)
    spool.publish(symbol="AAPL", reference_at=forecast["reference_at"], envelope=envelope, source_session="data-day")
    published = []
    runner = ShadowRunner(store=store, pc="Scout", symbols=["AAPL", "IONQ"],
        config=ResearchConfig(models=("fixture",), symbol_workers=2), clock=lambda:cutoff,
        trainer=trained, dataset_builder=lambda *a, **k:pytest.fail("1B cannot acquire or prepare"))
    result = runner.run_cycle(PreparedHistories(spool, store, forecast["reference_at"], clock=lambda:cutoff),
        reference_at=forecast["reference_at"], mode="live", experiment="manual-fixture", on_published=published.append)
    assert list(result["forecasts"]) == ["AAPL"]
    assert len(published) == 1 and published[0]["probability_not_down"] == .6
    assert result["status"] == "partial_failure"
    assert result["failures"]["IONQ"]["reason"] == "system_1a_snapshot_pending"
    assert result["failed_symbols"] == 1 and result["successful_symbols"] == 1
    assert "execution_input_hash" in published[0]


def test_data_publication_continues_without_consumer_and_during_slow_training(tmp_path):
    forecast, snapshot, equity, quote, store, envelope, cutoff = prepared(tmp_path)
    spool = ReadySpool(tmp_path, pc="Scout", symbols=["AAPL", "IONQ"], recipe_hash="recipe", clock=lambda:cutoff)
    entered, release = threading.Event(), threading.Event()
    def slow_model():
        entered.set()
        assert release.wait(5)
    worker = threading.Thread(target=slow_model)
    worker.start()
    try:
        assert entered.wait(2)
        for count in range(3):
            # No model consumer, ack, trader status, controller or stop lookup.
            published = spool.publish(symbol="AAPL", reference_at=forecast["reference_at"], envelope=envelope,
                                      source_session="independent-data-"+str(count))
            assert spool.ready("AAPL", forecast["reference_at"])["ready_id"] == published["ready_id"]
        assert worker.is_alive()
    finally:
        release.set()
        worker.join(2)


def test_large_scientific_forecast_is_retained_with_bounded_execution_record(tmp_path):
    forecast, snapshot, _, _, store, envelope, cutoff = prepared(tmp_path)
    snapshot["manifest"]["large_scientific_diagnostics"] = ["evidence" * 1000] * 1000
    store.freeze_inputs("large-prep", "AAPL", snapshot)
    envelope = store.envelope("large-prep", "AAPL")[1]
    spool = ReadySpool(tmp_path, pc="Scout", symbols=["AAPL"], recipe_hash="recipe", clock=lambda:cutoff)
    spool.publish(symbol="AAPL", reference_at=forecast["reference_at"], envelope=envelope, source_session="data")
    runner = ShadowRunner(store=store, pc="Scout", symbols=["AAPL"], clock=lambda:cutoff,
        config=ResearchConfig(models=("prerecorded",)), trainer=trained)
    result = runner.run_cycle(PreparedHistories(spool, store, forecast["reference_at"], clock=lambda:cutoff),
                             reference_at=forecast["reference_at"], mode="live", experiment="manual-fixture")
    saved = result["forecasts"]["AAPL"]
    original = store.scientific_forecast(saved)
    assert original["dataset_manifest"] == snapshot["manifest"]
    assert original["features"] == {"x":1.0}
    assert len(encoded(original).encode()) > 7_000_000
    assert len(encoded(saved).encode()) <= 65536
    assert saved["probability_not_down"] == original["probability_not_down"]
    assert saved["training_cutoff"] == original["training_cutoff"]
    assert saved["target_close"] == original["target_close"]


@pytest.mark.parametrize("changed", ["object_path", "indicator_object_path"])
def test_final_execution_detects_changed_retained_objects_without_matrix_read(tmp_path, changed):
    from pathlib import Path
    forecast, snapshot, _, _, store, envelope, cutoff = prepared(tmp_path)
    forecast["execution_input_hash"] = digest(envelope)
    assert execution_envelope(forecast["snapshot_hash"], envelope, forecast)
    path = Path(envelope[changed])
    path.write_bytes(path.read_bytes() + b" ")
    with pytest.raises(ContractError, match="evidence changed"):
        execution_envelope(forecast["snapshot_hash"], envelope, forecast)


def test_snapshot_recipe_and_feature_contract_identity_cannot_be_relabelled(tmp_path):
    forecast, snapshot, _, _, store, envelope, cutoff = prepared(tmp_path)
    spool = ReadySpool(tmp_path, pc="Scout", symbols=["AAPL"], recipe_hash="selected-file-hash",
                       recipe_identity=digest({"frozen":"local recipe"}), clock=lambda:cutoff)
    with pytest.raises(ContractError, match="local model recipe"):
        spool.publish(symbol="AAPL", reference_at=forecast["reference_at"], envelope=envelope, source_session="data")


def test_database_writer_admission_is_measured_and_bounded(tmp_path):
    *_, store, envelope, cutoff = prepared(tmp_path)
    with store.connect() as writer:
        writer.execute("BEGIN IMMEDIATE")
        begun = time.perf_counter()
        with pytest.raises(sqlite3.OperationalError, match="locked"):
            store.event("fixture", "no_broker_effect", {})
        assert time.perf_counter()-begun < 2
    rows = [json.loads(line) for line in (tmp_path / "database-metrics.jsonl").read_text().splitlines()]
    assert any(row["phase"] in {"writer_admission", "write_statement"} and row["busy"] for row in rows)
