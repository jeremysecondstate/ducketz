"""Actual local file mutations and final serialized forecast limits; no network."""
from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path

import pytest

from ml.intraday import immutable_file_witness, service_handoff
from ml.intraday.prediction import ContractError, digest, encoded
from ml.intraday.runtime import ResearchConfig, ShadowRunner
from ml.intraday.scout_services import PreparedHistories
from ml.intraday.service_handoff import ReadySpool, execution_envelope
from tests.test_intraday_runtime import trained
from tests.test_scout_service_handoff import prepared


def _change_same_length(path):
    info = path.stat()
    raw = path.read_bytes()
    changed = bytearray(raw)
    changed[len(changed) // 2] ^= 1
    path.write_bytes(changed)
    os.utime(path, ns=(info.st_atime_ns, info.st_mtime_ns))
    assert path.stat().st_size == info.st_size
    assert path.stat().st_mtime_ns == info.st_mtime_ns


@pytest.mark.parametrize("field", ["object_path", "indicator_object_path"])
@pytest.mark.parametrize("legacy", [False, True])
def test_same_length_edit_with_restored_mtime_cannot_reuse_execution_proof(tmp_path, field, legacy):
    forecast, _, _, _, store, envelope, _ = prepared(tmp_path)
    if not legacy:
        forecast["execution_integrity_witnesses"] = {
            path_key: service_handoff._verified_file_witness(envelope[path_key], envelope[hash_key])
            for path_key, hash_key in (("object_path", "snapshot_hash"),
                                       ("indicator_object_path", "indicator_snapshot_hash"))}
    forecast["execution_input_hash"] = digest(envelope)
    assert execution_envelope(forecast["snapshot_hash"], envelope, forecast)
    _change_same_length(Path(envelope[field]))
    with pytest.raises(ContractError, match="evidence changed"):
        execution_envelope(forecast["snapshot_hash"], envelope, forecast)


def test_new_witness_checks_files_without_reading_large_scientific_objects(tmp_path, monkeypatch):
    store, envelope, forecast = _published(tmp_path)
    def forbidden(*args, **kwargs):
        pytest.fail("An unchanged v2 execution proof must not read the retained matrix")
    monkeypatch.setattr(service_handoff, "_verified_file_witness", forbidden)
    assert execution_envelope(forecast["snapshot_hash"], envelope, forecast)


def test_additive_proofs_preserve_pinned_older_trader_witness_contract(tmp_path):
    store, envelope, forecast = _published(tmp_path)
    # Existing readers compare these five fields and hash the entire envelope.
    # Stronger proofs cannot replace their list or rewrite frozen input bytes.
    assert len(envelope["object_witness"]) == len(envelope["indicator_object_witness"]) == 5
    assert service_handoff.file_witness(envelope["object_path"]) == envelope["object_witness"]
    assert service_handoff.file_witness(envelope["indicator_object_path"]) == envelope["indicator_object_witness"]
    assert service_handoff.file_witness(forecast["scientific_forecast_path"]) == forecast["scientific_forecast_witness"]
    assert digest(envelope) == forecast["execution_input_hash"]
    assert store.envelope(forecast["cycle_id"], "AAPL")[1] == envelope
    assert set(forecast["execution_integrity_witnesses"]) == {"object_path", "indicator_object_path"}


def test_replaced_file_cannot_reuse_execution_proof_even_with_identical_bytes(tmp_path):
    forecast, _, _, _, _, envelope, _ = prepared(tmp_path)
    forecast["execution_input_hash"] = digest(envelope)
    path = Path(envelope["object_path"])
    replacement = path.with_suffix(".replacement")
    replacement.write_bytes(path.read_bytes())
    os.replace(replacement, path)
    with pytest.raises(ContractError, match="evidence changed"):
        execution_envelope(forecast["snapshot_hash"], envelope, forecast)


@pytest.mark.skipif(os.name != "nt", reason="Windows native change marker")
def test_unavailable_native_change_marker_fails_closed(tmp_path, monkeypatch):
    forecast, _, _, _, _, envelope, _ = prepared(tmp_path)
    forecast["execution_input_hash"] = digest(envelope)
    def unavailable(fd):
        raise OSError("fixture native metadata unavailable")
    monkeypatch.setattr(immutable_file_witness, "_windows_change_time", unavailable)
    with pytest.raises(OSError, match="metadata unavailable"):
        execution_envelope(forecast["snapshot_hash"], envelope, forecast)


def _published(tmp_path):
    forecast, _, _, _, store, envelope, cutoff = prepared(tmp_path)
    spool = ReadySpool(tmp_path, pc="Scout", symbols=["AAPL"], recipe_hash="fixture", clock=lambda:cutoff)
    spool.publish(symbol="AAPL", reference_at=forecast["reference_at"], envelope=envelope, source_session="data")
    runner = ShadowRunner(store=store, pc="Scout", symbols=["AAPL"], clock=lambda:cutoff,
        config=ResearchConfig(models=("prerecorded",)), trainer=trained)
    result = runner.run_cycle(PreparedHistories(spool, store, forecast["reference_at"], clock=lambda:cutoff),
                             reference_at=forecast["reference_at"], mode="live", experiment="manual-fixture")
    return store, envelope, result["forecasts"]["AAPL"]


@pytest.mark.parametrize("legacy", [False, True])
def test_scientific_forecast_same_length_corruption_is_rejected(tmp_path, legacy):
    store, envelope, forecast = _published(tmp_path)
    if legacy:
        forecast.pop("scientific_forecast_integrity")
    assert execution_envelope(forecast["snapshot_hash"], envelope, forecast)
    _change_same_length(Path(forecast["scientific_forecast_path"]))
    with pytest.raises(ContractError, match="evidence changed"):
        execution_envelope(forecast["snapshot_hash"], envelope, forecast)


def test_changed_retained_inputs_cannot_publish_a_new_forecast(tmp_path):
    store, envelope, saved = _published(tmp_path)
    original = store.scientific_forecast(saved)
    original["cycle_id"] = "changed-input"
    store.attach("changed-input", "AAPL", envelope)
    _change_same_length(Path(envelope["object_path"]))
    with pytest.raises(ContractError, match="evidence changed"):
        store.save_forecast(original)
    with store.connect() as db:
        assert not db.execute("SELECT 1 FROM forecasts WHERE cycle_id='changed-input'").fetchone()


def test_bound_applies_to_exact_final_serialization_before_any_forecast_commit(tmp_path, monkeypatch):
    # Isolate the serialization boundary from inode/time digit lengths and
    # object-file size changes. Native file integrity is covered above.
    monkeypatch.setattr(service_handoff, "file_witness", lambda path: [0, 0, 0, 0, 0])
    monkeypatch.setattr(service_handoff, "_verified_file_witness", lambda *args: {"fixture": "stable metadata"})
    store, envelope, saved = _published(tmp_path)
    original = store.scientific_forecast(saved)
    original["diagnostic_padding"] = ""
    original["cycle_id"] = "bound0"
    store.attach("bound0", "AAPL", envelope)
    baseline = store.save_forecast(original)
    padding = 65536 - len(encoded(baseline).encode())
    assert padding > 0
    original["cycle_id"] = "bound1"
    original["diagnostic_padding"] = "x" * padding
    store.attach("bound1", "AAPL", envelope)
    at_limit = store.save_forecast(original)
    assert len(encoded(at_limit).encode()) == 65536
    # Include forecast ID, stored direction/classification and every other
    # field: the actual INSERT text must match the returned representation.
    with store.connect() as db:
        raw = db.execute("SELECT payload FROM forecasts WHERE cycle_id='bound1'").fetchone()[0]
        before = db.execute("SELECT count(*) FROM forecasts").fetchone()[0]
        before_intents = db.execute("SELECT count(*) FROM intents").fetchone()[0]
    assert raw == encoded(at_limit)
    original["cycle_id"] = "bound2"
    original["diagnostic_padding"] += "x"
    store.attach("bound2", "AAPL", envelope)
    with pytest.raises(ContractError, match="bounded 64KiB"):
        store.save_forecast(original)
    with store.connect() as db:
        assert db.execute("SELECT count(*) FROM forecasts").fetchone()[0] == before
        assert db.execute("SELECT count(*) FROM intents").fetchone()[0] == before_intents
    # The complete failed-to-project scientific output is still retained.
    scientific = deepcopy(original)
    scientific["execution_input_hash"] = digest(envelope)
    path = store.object_root / "objects" / (digest(scientific) + ".json")
    assert hashlib.sha256(path.read_bytes()).hexdigest() == digest(scientific)
