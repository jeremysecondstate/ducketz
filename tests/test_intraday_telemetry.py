"""Two local folders exercise transport contracts, never actual remote liveness."""
from copy import deepcopy
import gc
import hashlib
import json
import subprocess
import sys

import pytest

from ml.intraday.prediction import ContractError, TARGET_VERSION, digest
from ml.intraday.store import ShadowStore
from ml.intraday.telemetry import (
    MAX_PACKET_BYTES, PACKET_SCHEMA, RECEIPT_SCHEMA, export_packet, import_packet,
    latest_received, load_receipt, main, validate_packet, write_packet,
)

ISSUE = "2026-10-12T17:16:02+00:00"
PRODUCED = "2026-10-12T17:47:00+00:00"
RECEIVED = "2026-10-12T17:48:00+00:00"


def source_store(path, pc="Atlas", *, scored=True, source_dataset=None, mode="shadow"):
    store = ShadowStore(path)
    source = {"source_dataset": source_dataset, "source_id": f"databento:{source_dataset}:ohlcv-1m"} if source_dataset else {}
    cycle_id = digest({"cycle": 1, "pc": pc}) if mode == "shadow" else digest({"cycle": 1, "pc": pc, "mode": mode})
    cycle = dict(pc=pc, symbols=["AAPL"], reference_at="2026-10-12T17:00:00+00:00",
                 mode=mode, started_at="2026-10-12T17:15:30+00:00", status="running",
                 training_cutoff="2026-10-12T17:15:30+00:00", candle_close="2026-10-12T17:15:00+00:00",
                 scheduled_acquisition="2026-10-12T17:15:30+00:00", configuration_hash="private-config-hash",
                 configuration={"private_value": "do-not-export-config"}, account_id="do-not-export-account")
    store.begin_cycle(cycle_id, cycle)
    store.update_cycle(cycle_id, dict(completed_at="2026-10-12T17:16:04+00:00", status="partial_failure",
                                    successful_symbols=1, failed_symbols=1, total_cycle_seconds=34.0,
                                    failures={"IONQ": {"message": "C:/private/model-password"}},
                                    resources_after={"private": "never-copy-whole-resources"}))
    models = [dict(name="logistic", family="logistic", status="success", probability_not_down=.70,
                   version="a" * 64, training_seconds=1.0, inference_seconds=.01,
                   training_started_at="2026-10-12T17:15:40+00:00", training_completed_at="2026-10-12T17:16:00+00:00",
                   artifact_path="C:/private/fitted-model.joblib", metadata={"features": "do-not-export-feature"}),
              dict(name="forest", status="success", probability_not_down=.74, version="b" * 64),
              dict(name="failed", status="failed", probability_not_down=None, version=None,
                   error={"type": "ValueError", "phase": "training", "message": "do-not-export-exception-secret"})]
    forecast = store.save_forecast(dict(pc=pc, symbol="AAPL", cycle_id=cycle_id,
        reference_at="2026-10-12T17:00:00+00:00", reference_open="100", target_open="2026-10-12T17:30:00+00:00",
        target_close="2026-10-12T17:45:00+00:00", issued_at=ISSUE, recorded_at="2026-10-12T17:16:03+00:00",
        training_cutoff="2026-10-12T17:15:30+00:00", information_available_at="2026-10-12T17:15:03+00:00",
        data_through="2026-10-12T17:15:00+00:00", target_version=TARGET_VERSION,
        mode=mode, availability_basis="observed", probability_not_down=.72,
        configured_count=3, successful_count=2, ensemble_status="incomplete", models=models,
        features={"close": "do-not-export-feature"}, snapshot={"raw": "do-not-export-candle"},
        account_id="do-not-export-account", local_path="C:/private/store.sqlite3", **source),
        intent=dict(desired_quantity=4, adjusted_quantity=0, disposition="do-not-export-capacity"))
    if scored:
        candle = dict(symbol="AAPL", opened_at="2026-10-12T17:30:00+00:00", available_at="2026-10-12T17:45:04+00:00",
                      open="100", high="101", low="99", close="100", volume=100,
                      source_id="do-not-export-full-candle", availability_kind="actual", synthetic=False)
        candle.update(source)
        store.observe(forecast, as_of="2026-10-12T17:46:00+00:00", candle=candle)
    return store, forecast


def packet_file(tmp_path, pc="Atlas", *, produced=PRODUCED, scored=True):
    store, forecast = source_store(tmp_path / pc / "shadow.sqlite3", pc, scored=scored)
    packet = export_packet(store.path, producer_pc=pc, source_version="90704d21801f60426493bd6cc650a7fac38d4f61", produced_at=produced)
    path = write_packet(packet, tmp_path / pc / "outgoing.json")
    return path, packet, store, forecast


def resign(packet):
    packet["packet_id"] = hashlib.sha256(json.dumps(packet["payload"], sort_keys=True, separators=(",", ":"),
                                                  ensure_ascii=True, allow_nan=False).encode()).hexdigest()
    return packet


def test_export_projection_preserves_models_scores_and_excludes_private_fields(tmp_path):
    store, forecast = source_store(tmp_path / "Atlas" / "shadow.sqlite3")
    # Release fixture writer connections and their final WAL checkpoint first;
    # checkpoint activity is independent of the exporter's read-only connection.
    gc.collect()
    before = hashlib.sha256(store.path.read_bytes()).hexdigest()
    packet = export_packet(store.path, producer_pc="Atlas", source_version="release-1", produced_at=PRODUCED)
    assert packet["schema_version"] == PACKET_SCHEMA
    assert before == hashlib.sha256(store.path.read_bytes()).hexdigest()
    value = packet["payload"]["forecasts"][0]
    assert value["forecast_id"] == forecast["forecast_id"]
    assert value["probability_not_down"] == .72 and value["ensemble_status"] == "incomplete"
    assert value["outcome"]["target_not_down"] is True and value["outcome"]["target_close_price"] == "100"
    assert value["models"][-1]["failure_type"] == "ValueError"
    assert packet["payload"]["cycles"][0]["failed_symbol_names"] == ["IONQ"]
    exported = json.dumps(packet)
    for secret in ("do-not-export", "C:/private", "artifact_path", "desired_quantity", "adjusted_quantity",
                   "metadata", "snapshot", "features", "account_id", "resources_after", "configuration"):
        assert secret not in exported


def test_cycle_timing_projection_keeps_mixed_scopes_and_legacy_unspecified(tmp_path):
    store, forecast = source_store(tmp_path / "source.sqlite3")
    timings = {
        "wall-cycle": dict(measurement_scope="wall_elapsed_with_cumulative_attempt_work_v1",
            total_cycle_seconds=930.0, cumulative_attempt_seconds=14.0, wall_elapsed_seconds=930.0,
            wall_exceeds_15m=True, wall_overrun_seconds=30.0),
        "replay-cycle": dict(measurement_scope="cumulative_attempt_work", total_cycle_seconds=6.0,
            cumulative_attempt_seconds=6.0, wall_elapsed_seconds=930.0,
            wall_exceeds_15m=True, wall_overrun_seconds=30.0),
    }
    for identity, fields in timings.items():
        store.begin_cycle(identity, dict(pc="Atlas", reference_at="2026-10-12T17:15:00Z",
            mode="historical_replay" if identity == "replay-cycle" else "live",
            started_at="2026-10-12T17:30:30Z", status="running"))
        store.update_cycle(identity, dict(status="completed", completed_at="2026-10-12T17:46:00Z", **fields))
    packet = export_packet(store.path, producer_pc="Atlas", source_version="timing-release", produced_at=PRODUCED)
    projected = {row["cycle_id"]: row for row in packet["payload"]["cycles"]}
    legacy = projected[forecast["cycle_id"]]
    assert legacy["total_cycle_seconds"] == 34.0
    assert not any(key in legacy for key in next(iter(timings.values())) if key != "total_cycle_seconds")
    for identity, fields in timings.items():
        assert {key: projected[identity][key] for key in fields} == fields
    path = write_packet(packet, tmp_path / "packet.json")
    receipt = import_packet(path, tmp_path / "receipts", expected_peer="Atlas", local_pc="Scout", received_at=RECEIVED)
    loaded = load_receipt(receipt["receipt_path"], expected_peer="Atlas", local_pc="Scout", as_of=RECEIVED)
    assert loaded["receipt"]["packet"]["payload"]["cycles"] == packet["payload"]["cycles"]


@pytest.mark.parametrize("field,value", [
    ("measurement_scope", None), ("measurement_scope", "unknown"), ("measurement_scope", []),
    ("wall_exceeds_15m", 1), ("wall_exceeds_15m", "false"),
    *[(field, value) for field in ("cumulative_attempt_seconds", "wall_elapsed_seconds", "wall_overrun_seconds")
      for value in (-1, True, "1e999", "NaN")],
])
def test_cycle_timing_optional_fields_reject_invalid_values(tmp_path, field, value):
    _, packet, _, _ = packet_file(tmp_path)
    packet["payload"]["cycles"][0][field] = value
    with pytest.raises(ContractError):
        validate_packet(resign(packet))


@pytest.mark.parametrize("value", [0, "0", "0.25", 12.5])
def test_cycle_timing_optional_fields_accept_finite_nonnegative_values_without_inventing_scope(tmp_path, value):
    _, packet, _, _ = packet_file(tmp_path)
    row = packet["payload"]["cycles"][0]
    for field in ("cumulative_attempt_seconds", "wall_elapsed_seconds", "wall_overrun_seconds"):
        row[field] = value
    row["wall_exceeds_15m"] = False
    assert validate_packet(resign(packet))["payload"]["cycles"][0] == row
    assert "measurement_scope" not in row


def test_live_projection_preserves_original_forecast_and_actual_outcome_without_execution_data(tmp_path):
    store, issued = source_store(tmp_path/"atlas-live.sqlite3",mode="live",source_dataset="EQUS.MINI")
    packet = export_packet(store.path,producer_pc="Atlas",source_version="test-live-release",produced_at=PRODUCED)
    row = packet["payload"]["forecasts"][0]
    assert row["mode"] == "live" and row["forecast_id"] == issued["forecast_id"]
    assert row["target_close"] == issued["target_close"] and row["target_version"] == TARGET_VERSION
    assert row["availability_basis"] == "observed" and row["outcome"]["availability_basis"] == "actual"
    assert packet["payload"]["cycles"][0]["mode"] == "live"
    serialized = json.dumps(packet)
    for excluded in ("desired_quantity","adjusted_quantity","submitted_quantity","broker_order_id",
                     "do-not-export","C:/private","snapshot","account_id","features"):
        assert excluded not in serialized
    path = write_packet(packet,tmp_path/"live-packet.json")
    receipt = import_packet(path,tmp_path/"receipts",expected_peer="Atlas",local_pc="Scout",received_at=RECEIVED)
    loaded = load_receipt(receipt["receipt_path"],expected_peer="Atlas",local_pc="Scout",as_of=RECEIVED)
    assert loaded["status"] == "stale" and loaded["latest_forecast_mode"] == "live"
    assert loaded["latest_forecast_issue"] == ISSUE


def test_live_only_freshness_is_observed_forecasting_not_historical_only_or_trading_authority(tmp_path):
    store, issued = source_store(tmp_path/"live.sqlite3",mode="live",scored=False)
    packet = export_packet(store.path,producer_pc="Atlas",source_version="test-live-release",produced_at="2026-10-12T17:17:00+00:00")
    path = write_packet(packet,tmp_path/"live.json")
    received = import_packet(path,tmp_path/"receipts",expected_peer="Atlas",local_pc="Scout",received_at="2026-10-12T17:18:00+00:00")
    loaded = load_receipt(received["receipt_path"],expected_peer="Atlas",local_pc="Scout",as_of="2026-10-12T17:18:00+00:00")
    assert loaded["status"] == "fresh" and loaded["latest_forecast_mode"] == "live"
    assert loaded["receipt"]["packet"]["payload"]["forecasts"][0]["forecast_id"] == issued["forecast_id"]
    assert "execution_authorized" not in loaded


def test_live_shadow_and_replay_remain_distinct_same_symbol_source_cohorts(tmp_path):
    expected = set()
    for mode in ("shadow","live","historical_replay"):
        store, issued = source_store(tmp_path/"modes.sqlite3",mode=mode,scored=False,source_dataset="EQUS.MINI")
        expected.add(issued["forecast_id"])
    packet = export_packet(store.path,producer_pc="Atlas",source_version="test-mode-release",produced_at=PRODUCED)
    rows = packet["payload"]["forecasts"]
    assert len(rows) == 3 and {row["mode"] for row in rows} == {"shadow","live","historical_replay"}
    assert {row["forecast_id"] for row in rows} == expected
    assert {cycle["mode"] for cycle in packet["payload"]["cycles"]} == {"shadow","live","historical_replay"}


@pytest.mark.parametrize("field",["forecast","outcome"])
def test_live_assumed_information_or_outcome_cannot_masquerade_as_actual(tmp_path,field):
    store,_ = source_store(tmp_path/"live.sqlite3",mode="live")
    packet = export_packet(store.path,producer_pc="Atlas",source_version="test-live-release",produced_at=PRODUCED)
    row = packet["payload"]["forecasts"][0]
    if field == "forecast": row["availability_basis"] = "assumed_historical"
    else: row["outcome"]["availability_basis"] = "assumed_historical_close"
    with pytest.raises(ContractError,match="availability"):
        validate_packet(resign(packet))


@pytest.mark.parametrize("dataset", ["EQUS.MINI", "XNAS.ITCH"])
def test_portable_source_provenance_survives_export_and_receipt(tmp_path, dataset):
    store, forecast = source_store(tmp_path / "source.sqlite3", source_dataset=dataset)
    packet = export_packet(store.path, producer_pc="Atlas", source_version="release-source", produced_at=PRODUCED)
    value = packet["payload"]["forecasts"][0]
    assert value["source_dataset"] == dataset
    assert value["source_id"] == f"databento:{dataset}:ohlcv-1m"
    assert value["forecast_id"] == forecast["forecast_id"]
    packet_path = write_packet(packet, tmp_path / "packet.json")
    received = import_packet(packet_path, tmp_path / "received", expected_peer="Atlas", local_pc="Scout", received_at=RECEIVED)
    loaded = load_receipt(received["receipt_path"], expected_peer="Atlas", local_pc="Scout", as_of=RECEIVED)
    assert loaded["receipt"]["packet"]["payload"]["forecasts"][0] == value


def test_latest_forecasts_keep_each_source_dataset_and_legacy_scope(tmp_path):
    store, legacy = source_store(tmp_path / "source.sqlite3", scored=False)
    expected_ids = {legacy["forecast_id"]}
    for dataset in ("EQUS.MINI", "XNAS.ITCH"):
        for revision, issue in enumerate((ISSUE, "2026-10-12T17:17:00+00:00")):
            value = store.save_forecast(dict(legacy, cycle_id=f"{dataset}-{revision}",
                issued_at=issue, recorded_at=issue, source_dataset=dataset, source_id=f"databento:{dataset}:ohlcv-1m"))
        expected_ids.add(value["forecast_id"])
    packet = export_packet(store.path, producer_pc="Atlas", source_version="release-source", produced_at=PRODUCED)
    rows = packet["payload"]["forecasts"]
    assert len(rows) == 3 and {row["forecast_id"] for row in rows} == expected_ids
    assert {row.get("source_dataset") for row in rows} == {None, "EQUS.MINI", "XNAS.ITCH"}
    packet["payload"]["forecasts"].append(deepcopy(rows[0]))
    with pytest.raises(ContractError, match="repeats a latest"):
        validate_packet(resign(packet))


@pytest.mark.parametrize("source", [
    {"source_dataset": "EQUS.MINI"},
    {"source_id": "databento:EQUS.MINI:ohlcv-1m"},
    {"source_dataset": "EQUS.MINI", "source_id": "databento:XNAS.ITCH:ohlcv-1m"},
    {"source_dataset": "EQUS.MINI", "source_id": "C:/private/history.parquet"},
    {"source_dataset": "C:/private", "source_id": "databento:C:/private:ohlcv-1m"},
    {"source_dataset": "OTHER.DATA", "source_id": "databento:OTHER.DATA:ohlcv-1m"},
    {"source_dataset": None, "source_id": None},
])
def test_source_provenance_rejects_partial_unknown_mismatched_and_private_identities(tmp_path, source):
    _, packet, _, _ = packet_file(tmp_path)
    packet["payload"]["forecasts"][0].update(source)
    with pytest.raises(ContractError, match="source identity"):
        validate_packet(resign(packet))


def test_two_pc_reception_is_immutable_and_duplicates_preserve_first_received_at(tmp_path):
    path, packet, _, _ = packet_file(tmp_path)
    peer_root = tmp_path / "Scout" / "receipts"
    first = import_packet(path, peer_root, expected_peer="Atlas", local_pc="Scout", received_at=RECEIVED)
    assert first["receipt"]["schema_version"] == RECEIPT_SCHEMA and not first["duplicate"]
    receipt_path = peer_root / "atlas" / f"{packet['packet_id']}.json"
    before = receipt_path.read_bytes()
    second = import_packet(path, peer_root, expected_peer="Atlas", local_pc="Scout", received_at="2026-10-12T18:00:00+00:00")
    assert second["duplicate"] and second["receipt"]["received_at"] == RECEIVED
    assert receipt_path.read_bytes() == before
    loaded = load_receipt(receipt_path, expected_peer="Atlas", local_pc="Scout", as_of="2026-10-12T18:01:00+00:00")
    assert loaded["status"] == "stale"
    assert loaded["producer_age_seconds"] == 14 * 60 and loaded["receipt_age_seconds"] == 13 * 60
    assert loaded["latest_forecast_issue"] == ISSUE


def test_recent_reception_and_packet_do_not_refresh_old_forecast(tmp_path):
    path, _, _, _ = packet_file(tmp_path)
    result = import_packet(path, tmp_path / "Scout" / "receipts", expected_peer="Atlas", local_pc="Scout", received_at=RECEIVED)
    loaded = load_receipt(result["receipt_path"], expected_peer="Atlas", local_pc="Scout", as_of=RECEIVED)
    assert loaded["producer_age_seconds"] == 60 and loaded["receipt_age_seconds"] == 0
    assert loaded["status"] == "stale" and loaded["forecast_age_seconds"] > 900


def test_fresh_shadow_and_historical_only_are_separate(tmp_path):
    path, _, store, forecast = packet_file(tmp_path, produced="2026-10-12T17:17:00+00:00", scored=False)
    root = tmp_path / "Scout" / "receipts"
    first = import_packet(path, root, expected_peer="Atlas", local_pc="Scout", received_at="2026-10-12T17:18:00+00:00")
    assert load_receipt(first["receipt_path"], expected_peer="Atlas", local_pc="Scout", as_of="2026-10-12T17:18:00+00:00")["status"] == "fresh"
    packet = export_packet(store.path, producer_pc="Atlas", source_version="release-1", produced_at=PRODUCED)
    packet["payload"]["forecasts"][0]["mode"] = "historical_replay"
    packet["payload"]["forecasts"][0]["availability_basis"] = "assumed_historical"
    # A separate scope in this fixture avoids intentional same-forecast conflicts.
    packet["payload"]["forecasts"][0]["forecast_id"] = "c" * 64
    path2 = write_packet(resign(packet), tmp_path / "Atlas" / "historical.json")
    second = import_packet(path2, root, expected_peer="Atlas", local_pc="Scout", received_at=RECEIVED)
    assert load_receipt(second["receipt_path"], expected_peer="Atlas", local_pc="Scout", as_of=RECEIVED)["status"] == "historical_only"


def test_out_of_order_delivery_preserves_newest_original_production(tmp_path):
    old_path, old, _, _ = packet_file(tmp_path)
    new = deepcopy(old)
    new["payload"]["produced_at"] = "2026-10-12T17:49:00+00:00"
    new_path = write_packet(resign(new), tmp_path / "Atlas" / "newer.json")
    root = tmp_path / "Scout" / "receipts"
    import_packet(new_path, root, expected_peer="Atlas", local_pc="Scout", received_at="2026-10-12T17:50:00+00:00")
    import_packet(old_path, root, expected_peer="Atlas", local_pc="Scout", received_at="2026-10-12T17:51:00+00:00")
    latest = latest_received(root, expected_peer="Atlas", local_pc="Scout", as_of="2026-10-12T17:52:00+00:00")
    assert latest["receipt"]["packet"]["payload"]["produced_at"] == "2026-10-12T17:49:00+00:00"
    assert latest["receipt"]["received_at"] == "2026-10-12T17:50:00+00:00"
    assert len(list((root / "atlas").glob("*.json"))) == 2


def test_changed_packet_bytes_unknown_pc_wrong_peer_and_future_are_rejected(tmp_path):
    path, packet, _, _ = packet_file(tmp_path)
    root = tmp_path / "Scout" / "receipts"
    with pytest.raises(ContractError, match="expected peer"):
        import_packet(path, root, expected_peer="Scout", local_pc="Atlas", received_at=RECEIVED)
    invalid = deepcopy(packet)
    invalid["payload"]["source_version"] = "modified-version"
    with pytest.raises(ContractError, match="hash"):
        validate_packet(invalid)
    invalid["payload"]["producer_pc"] = "UnknownPC"
    with pytest.raises(ContractError, match="Unknown monitoring PC"):
        validate_packet(resign(invalid))
    with pytest.raises(ContractError, match="future"):
        import_packet(path, root, expected_peer="Atlas", local_pc="Scout", received_at="2026-10-12T17:46:59+00:00")
    invalid = deepcopy(packet)
    invalid["payload"]["forecasts"][0]["recorded_at"] = "2026-10-13T17:00:00+00:00"
    with pytest.raises(ContractError, match="future"):
        validate_packet(resign(invalid))
    invalid = deepcopy(packet)
    invalid["payload"]["produced_at"] = "2026-10-12T10:47:00-07:00"
    with pytest.raises(ContractError, match="UTC"):
        validate_packet(resign(invalid))


def test_conflicting_forecast_identity_is_rejected_but_outcome_revision_is_append_only(tmp_path):
    path, packet, store, forecast = packet_file(tmp_path)
    root = tmp_path / "Scout" / "receipts"
    first = import_packet(path, root, expected_peer="Atlas", local_pc="Scout", received_at=RECEIVED)
    changed = deepcopy(packet)
    changed["payload"]["produced_at"] = "2026-10-12T17:49:00+00:00"
    f = changed["payload"]["forecasts"][0]
    f["probability_not_down"] = .62
    f["models"][0]["probability_not_down"] = .60
    f["models"][1]["probability_not_down"] = .64
    changed_path = write_packet(resign(changed), tmp_path / "Atlas" / "changed.json")
    with pytest.raises(ContractError, match="immutable peer forecast"):
        import_packet(changed_path, root, expected_peer="Atlas", local_pc="Scout", received_at="2026-10-12T17:50:00+00:00")
    revised = deepcopy(packet)
    revised["payload"]["produced_at"] = "2026-10-12T17:49:00+00:00"
    outcome = revised["payload"]["forecasts"][0]["outcome"]
    outcome.update(revision_id="e" * 64, target_not_down=False, target_close_price="99", observed_at="2026-10-12T17:48:30+00:00")
    revised_path = write_packet(resign(revised), tmp_path / "Atlas" / "revised.json")
    import_packet(revised_path, root, expected_peer="Atlas", local_pc="Scout", received_at="2026-10-12T17:50:00+00:00")
    assert len(list((root / "atlas").glob("*.json"))) == 2
    original = load_receipt(first["receipt_path"], expected_peer="Atlas", local_pc="Scout", as_of="2026-10-12T17:51:00+00:00")
    assert original["receipt"]["packet"]["payload"]["forecasts"][0]["outcome"]["target_not_down"] is True


def test_private_unknown_fields_fake_failed_probabilities_and_target_changes_rejected(tmp_path):
    _, packet, _, _ = packet_file(tmp_path)
    value = deepcopy(packet)
    value["payload"]["forecasts"][0]["private_account"] = "not-admitted"
    with pytest.raises(ContractError, match="private"):
        validate_packet(resign(value))
    value = deepcopy(packet)
    value["payload"]["forecasts"][0]["models"][-1]["probability_not_down"] = .5
    with pytest.raises(ContractError, match="fake probability"):
        validate_packet(resign(value))
    value = deepcopy(packet)
    value["payload"]["forecasts"][0]["target_close"] = "2026-10-12T18:00:00+00:00"
    with pytest.raises(ContractError, match="fixed intraday target"):
        validate_packet(resign(value))


def test_price_equality_uses_exact_values_and_not_float_rounding(tmp_path):
    _, packet, _, _ = packet_file(tmp_path)
    f = packet["payload"]["forecasts"][0]
    f["reference_open"] = "100.000000000000002"
    f["outcome"]["target_close_price"] = "100.000000000000001"
    f["outcome"]["target_not_down"] = False
    assert validate_packet(resign(packet))
    f["outcome"]["target_not_down"] = True
    with pytest.raises(ContractError, match="equality"):
        validate_packet(resign(packet))


def test_concurrent_process_importers_cannot_publish_conflicting_forecast_bytes(tmp_path):
    first_path, packet, _, _ = packet_file(tmp_path)
    changed = deepcopy(packet)
    changed["payload"]["produced_at"] = "2026-10-12T17:49:00+00:00"
    forecast = changed["payload"]["forecasts"][0]
    forecast["probability_not_down"] = .62
    forecast["models"][0]["probability_not_down"] = .6
    forecast["models"][1]["probability_not_down"] = .64
    second_path = write_packet(resign(changed), tmp_path / "Atlas" / "conflicting.json")
    root = tmp_path / "Scout" / "receipts"
    gate = tmp_path / "start.txt"
    script = """
from pathlib import Path
import sys,time
import ml.intraday.telemetry as telemetry
from ml.intraday.prediction import ContractError
original = telemetry._check_conflicts
def slow_scan(*args,**kwargs):
    original(*args,**kwargs)
    time.sleep(.15)
telemetry._check_conflicts = slow_scan
while not Path(sys.argv[3]).exists():
    time.sleep(.01)
try:
    telemetry.import_packet(sys.argv[1],sys.argv[2],expected_peer='Atlas',local_pc='Scout',received_at='2026-10-12T17:50:00+00:00')
    print('accepted')
except ContractError:
    print('rejected')
"""
    processes = [subprocess.Popen([sys.executable, "-c", script, str(path), str(root), str(gate)],
                                  stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
                 for path in (first_path, second_path)]
    try:
        gate.write_text("start", encoding="utf-8")
        output = [process.communicate(timeout=15) for process in processes]
        assert all(process.returncode == 0 for process in processes), output
        assert sorted(stdout.strip() for stdout, _ in output) == ["accepted", "rejected"]
        assert len(list((root / "atlas").glob("*.json"))) == 1
    finally:
        for process in processes:
            if process.poll() is None:
                process.kill()
                process.wait()


def test_explicit_paths_size_duplicate_json_keys_and_missing_database_are_strict(tmp_path):
    path, packet, _, _ = packet_file(tmp_path)
    with pytest.raises(ContractError, match="absolute"):
        write_packet(packet, "relative.json")
    with pytest.raises(ContractError, match="local absolute"):
        write_packet(packet, "https://example.com/packet.json")
    large = tmp_path / "large.json"
    large.write_bytes(b" " * (MAX_PACKET_BYTES + 1))
    with pytest.raises(ContractError, match="bounded size"):
        import_packet(large, tmp_path / "receipts", expected_peer="Atlas", local_pc="Scout", received_at=RECEIVED)
    duplicate = tmp_path / "duplicates.json"
    duplicate.write_text('{"schema_version":"first","schema_version":"second"}', encoding="utf-8")
    with pytest.raises(ContractError, match="Duplicate"):
        import_packet(duplicate, tmp_path / "receipts", expected_peer="Atlas", local_pc="Scout", received_at=RECEIVED)
    absent = tmp_path / "missing.sqlite3"
    with pytest.raises(ContractError, match="missing"):
        export_packet(absent, producer_pc="Atlas", source_version="release-1")
    assert not absent.exists()
    with pytest.raises(FileExistsError):
        write_packet(packet, path)


def test_empty_packet_and_explicit_cli_transport_do_not_create_remote_activity(tmp_path, capsys):
    store = ShadowStore(tmp_path / "Scout" / "shadow.sqlite3")
    output = tmp_path / "Scout" / "empty.json"
    assert main(["export", "--database", str(store.path), "--pc", "Scout", "--source-version", "release-1",
                 "--output", str(output)]) == 0
    assert main(["import", "--packet", str(output), "--receipts-root", str(tmp_path / "Atlas" / "receipts"),
                 "--expected-peer", "Scout", "--local-pc", "Atlas"]) == 0
    receipt_path = next((tmp_path / "Atlas" / "receipts" / "scout").glob("*.json"))
    assert load_receipt(receipt_path, expected_peer="Scout", local_pc="Atlas")["status"] == "empty"
    assert capsys.readouterr().out
