"""Real local immutable handoffs; no provider, estimator or broker effects."""
from dataclasses import asdict
from datetime import timedelta
import json
import sqlite3
import threading

import pytest

from ml.intraday.prediction import ContractError, STEP, TARGET_VERSION, digest
from ml.intraday.runtime import ResearchConfig
from ml.intraday.split_handoff import ReadySnapshots
from tests.test_intraday_runtime import REF


def fixture(symbol="AAPL", *, reference=REF, symbols=("AAPL", "IONQ"), experiment="data-atlas-day"):
    configuration = asdict(ResearchConfig(models=("fixture",), symbol_workers=2))
    configuration = json.loads(json.dumps(configuration))
    cycle = dict(pc="Atlas", symbols=list(symbols), reference_at=reference.isoformat(), mode="live",
        experiment=experiment, configuration=configuration, configuration_hash=digest(configuration),
        started_at=(reference+STEP+timedelta(seconds=30)).isoformat(), training_cutoff=(reference+STEP+timedelta(seconds=30)).isoformat(),
        supervised_model_policy=None, status="preparing")
    cycle_id = digest({key: cycle[key] for key in ("pc", "reference_at", "mode", "experiment", "configuration_hash")})
    snapshot = dict(X=[[0., 1.], [1., 0.]], y=[0, 1], inference_X=[[1., 2.]], feature_names=["first", "second"],
        reference_at=[(reference-4*STEP).isoformat(), (reference-3*STEP).isoformat()],
        target_close_at=[(reference-STEP).isoformat(), reference.isoformat()],
        label_available_at=[(reference-STEP).isoformat(), reference.isoformat()],
        cutoff=(reference+STEP+timedelta(seconds=30)).isoformat(),
        inference=dict(symbol=symbol, reference_at=reference.isoformat(), reference_open=100,
            information_available_at=(reference+STEP+timedelta(seconds=2)).isoformat(), availability_kind="actual"),
        manifest=dict(target_version=TARGET_VERSION, training_rows=2),
        dataset_scope=dict(synthetic_policy="exclude", lookback_days=None),
        source_manifest=dict(source_dataset="EQUS.MINI", immutable_receipt="fixture"),
        model_selection=dict(models=[{"name":"fixture", "resolved_recipe":"original"}], selection="selected_manifest"),
        candles=[dict(symbol=symbol, opened_at=reference.isoformat(), available_at=(reference+STEP+timedelta(seconds=2)).isoformat())])
    return cycle_id, cycle, snapshot


def store(tmp_path, *, now=None):
    return ReadySnapshots(tmp_path / "handoff", actor="Atlas", symbols=("AAPL", "IONQ"),
        clock=lambda: now or REF+STEP+timedelta(seconds=31))


def publish(owner, symbol="AAPL", **kwargs):
    key, cycle, snapshot = fixture(symbol, **kwargs)
    record = owner.publish(key, symbol, cycle, (digest(snapshot), snapshot))
    return record, cycle, snapshot


def test_roundtrip_preserves_exact_science_and_deadline(tmp_path):
    owner = store(tmp_path)
    record, cycle, snapshot = publish(owner)
    ready = owner.ready(REF+STEP+timedelta(seconds=31), configuration_hash=cycle["configuration_hash"])
    assert ready == [record]
    saved_cycle, saved_snapshot = owner.load(record)
    assert saved_snapshot == snapshot
    assert saved_cycle["configuration"] == cycle["configuration"]
    assert record["snapshot_hash"] == digest(snapshot)
    assert record["target_close"] == (REF+3*STEP).isoformat()
    assert record["training_cutoff"] == snapshot["cutoff"]
    assert record["recipe_sha256"] == digest(snapshot["model_selection"])
    assert record["feature_order_sha256"] == digest(["first", "second"])
    assert record["producer_session"] == cycle["experiment"]
    assert not list(owner.root.rglob("*.partial"))


def test_no_ack_required_bounded_latest_queue_retains_all_evidence(tmp_path):
    owner = store(tmp_path)
    records = []
    for index in range(8):
        owner.clock = lambda index=index: REF+(index+1)*STEP+timedelta(seconds=31)
        for symbol in owner.symbols:
            record, cycle, _ = publish(owner, symbol, reference=REF+index*STEP)
            records.append(record)
    with owner.connect() as db:
        assert db.execute("SELECT COUNT(*) FROM ready").fetchone()[0] == 2
    ready = owner.ready(REF+8*STEP+timedelta(seconds=31), configuration_hash=cycle["configuration_hash"])
    assert len(ready) == 2
    assert {r["reference_at"] for r in ready} == {(REF+7*STEP).isoformat()}
    assert len(list((owner.root / "records").rglob("*.json"))) == 16
    assert owner.load(records[0])[1]["feature_names"] == ["first", "second"]
    assert owner.ready(REF+11*STEP, configuration_hash=cycle["configuration_hash"]) == []
    publish(owner)  # Late publication cannot move the live cursor backward.
    with owner.connect() as db:
        assert all(row[0] == (REF+7*STEP).isoformat() for row in db.execute("SELECT reference_at FROM ready"))


def test_restart_republish_uses_original_ready_time_and_ignores_operational_progress(tmp_path):
    owner = store(tmp_path)
    record, cycle, snapshot = publish(owner)
    cycle.update(status="prepared", failures={}, attempt_count=8)
    later = store(tmp_path, now=REF+2*STEP)
    assert later.publish(record["cycle_id"], "AAPL", cycle, snapshot) == record
    assert later.measurements["index_seconds"] >= 0


@pytest.mark.parametrize("change", ["symbol", "actor", "order", "labels", "future_label", "future_target", "actual", "reference", "availability", "cutoff", "target", "scope"])
def test_rejects_mismatched_or_noncausal_preparation(tmp_path, change):
    owner = store(tmp_path)
    key, cycle, snapshot = fixture()
    if change == "symbol": snapshot["inference"]["symbol"] = "IONQ"
    if change == "actor": cycle["pc"] = "Scout"
    if change == "order": snapshot["feature_names"].append("missing_column")
    if change == "labels": snapshot["y"][0] = 2
    if change == "future_label": snapshot["label_available_at"][0] = (REF+3*STEP).isoformat()
    if change == "future_target": snapshot["target_close_at"][0] = (REF+3*STEP).isoformat()
    if change == "actual": snapshot["inference"]["availability_kind"] = "assumed"
    if change == "reference": snapshot["inference"]["reference_at"] = (REF+STEP).isoformat()
    if change == "availability": snapshot["inference"]["information_available_at"] = (REF+3*STEP).isoformat()
    if change == "cutoff": snapshot["cutoff"] = (REF+3*STEP).isoformat()
    if change == "target": snapshot["manifest"]["target_version"] = "changed"
    if change == "scope": cycle["symbols"] = ["AAPL"]
    with pytest.raises(ContractError): owner.publish(key, "AAPL", cycle, snapshot)
    with owner.connect() as db:
        assert db.execute("SELECT COUNT(*) FROM ready").fetchone()[0] == 0


def test_partial_publication_never_advertises_work_and_retry_recovers(tmp_path, monkeypatch):
    owner = store(tmp_path)
    real = owner._write
    def crash(kind, *args):
        if kind == "records": raise OSError("fixture interrupted publication")
        return real(kind, *args)
    monkeypatch.setattr(owner, "_write", crash)
    with pytest.raises(OSError): publish(owner)
    with owner.connect() as db:
        assert db.execute("SELECT COUNT(*) FROM ready").fetchone()[0] == 0
    monkeypatch.setattr(owner, "_write", real)
    record, cycle, _ = publish(owner)
    assert owner.ready(REF+STEP+timedelta(seconds=31), configuration_hash=cycle["configuration_hash"]) == [record]


def test_index_failure_after_complete_claim_recovers_without_new_identity(tmp_path, monkeypatch):
    owner = store(tmp_path)
    real = owner.connect
    from contextlib import contextmanager
    @contextmanager
    def unavailable():
        raise sqlite3.OperationalError("fixture index busy")
        yield
    monkeypatch.setattr(owner, "connect", unavailable)
    with pytest.raises(sqlite3.OperationalError): publish(owner)
    claim = next((owner.root/"claims").rglob("*.json")).read_bytes()
    monkeypatch.setattr(owner, "connect", real)
    record, _, _ = publish(owner)
    assert json.loads(claim)["record_sha256"] == record["record_sha256"]


def test_first_claim_rejects_later_inputs_without_losing_original(tmp_path):
    owner = store(tmp_path)
    record, cycle, snapshot = publish(owner)
    snapshot["X"][0][0] = 99
    with pytest.raises(ContractError, match="already claimed"):
        owner.publish(record["cycle_id"], "AAPL", cycle, snapshot)
    assert owner.load(record)[1]["X"][0][0] == 0


@pytest.mark.parametrize("kind", ["snapshots", "cycles", "records"])
def test_changed_or_truncated_object_detected_on_every_consumer_read(tmp_path, kind):
    owner = store(tmp_path)
    record, _, _ = publish(owner)
    key = {"snapshots":"snapshot_hash", "cycles":"cycle_hash", "records":"record_sha256"}[kind]
    path = owner._path(kind, record[key])
    raw = path.read_bytes()
    path.write_bytes(raw[:-1]+b" ")  # same size change, even if mtime restored
    with pytest.raises(ContractError, match="changed immutable"):
        owner.load(record)


def test_bad_symbol_index_does_not_hold_up_unrelated_ready_symbol(tmp_path):
    owner = store(tmp_path)
    bad, cycle, _ = publish(owner)
    good, _, _ = publish(owner, "IONQ")
    owner._path("records", bad["record_sha256"]).write_bytes(b"partial")
    assert owner.ready(REF+STEP+timedelta(seconds=31), configuration_hash=cycle["configuration_hash"]) == [good]
    assert owner.rejected == [{"symbol":"AAPL", "error_type":"ContractError"}]


def test_index_cannot_relabel_reference_or_configuration(tmp_path):
    owner = store(tmp_path)
    _, cycle, _ = publish(owner)
    with owner.connect() as db:
        db.execute("UPDATE ready SET reference_at=?", ((REF+STEP).isoformat(),))
    assert owner.ready(REF+STEP+timedelta(seconds=31), configuration_hash=cycle["configuration_hash"]) == []
    assert owner.rejected


def test_resource_override_can_reuse_exact_science_but_changed_science_cannot(tmp_path):
    owner = store(tmp_path)
    record, cycle, _ = publish(owner)
    changed = {**cycle["configuration"], "symbol_workers": 1}
    now = REF+STEP+timedelta(seconds=31)
    assert owner.ready(now, configuration_hash=digest(changed)) == []
    science = digest({key: value for key, value in changed.items() if key != "symbol_workers"})
    assert owner.ready(now, configuration_hash=digest(changed), scientific_configuration_hash=science) == [record]
    changed["synthetic_policy"] = "exclude"
    assert owner.ready(now, configuration_hash=digest(changed), scientific_configuration_hash=digest(
        {key: value for key, value in changed.items() if key != "symbol_workers"})) == []


def test_slow_snapshot_storage_holds_no_live_queue_transaction(tmp_path, monkeypatch):
    owner = store(tmp_path)
    publish(owner, "IONQ")
    entered, release = threading.Event(), threading.Event()
    real = owner._write
    def delayed(kind, *args):
        if kind == "snapshots":
            entered.set()
            assert release.wait(10)
        return real(kind, *args)
    monkeypatch.setattr(owner, "_write", delayed)
    errors = []
    def producer():
        try: publish(owner)
        except BaseException as exc: errors.append(exc)
    thread = threading.Thread(target=producer)
    thread.start()
    try:
        assert entered.wait(10)
        # A separate connection can read/write small pointers during big I/O.
        with owner.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            assert db.execute("SELECT COUNT(*) FROM ready").fetchone()[0] == 1
    finally:
        release.set(); thread.join(10)
    assert not errors and not thread.is_alive()
