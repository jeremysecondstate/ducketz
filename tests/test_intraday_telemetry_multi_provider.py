"""Optional monitoring accepts exact recovered-source and frozen-policy identities."""
import json
import sqlite3

import pytest

from ml.intraday.prediction import ContractError, TARGET_VERSION, digest
from ml.intraday.telemetry import export_packet, import_packet, load_receipt, validate_packet, write_packet


PRODUCED = "2026-10-20T17:47:00+00:00"
RECEIVED = "2026-10-20T17:48:00+00:00"
SOURCE_ID = "multi:databento-schwab-fmp:15m"
POLICIES = ("stock-direction-50-01-v3", "stock-direction-5001-4999-v3")


def source_database(path, *, policy=POLICIES[0], probability=.5001, direction="BULLISH"):
    value = dict(forecast_id=digest({"policy": policy, "probability": probability}),
        pc="Atlas", symbol="AAPL", cycle_id="a" * 64,
        reference_at="2026-10-20T17:00:00+00:00", reference_open="100",
        data_through="2026-10-20T17:15:00+00:00",
        target_open="2026-10-20T17:30:00+00:00", target_close="2026-10-20T17:45:00+00:00",
        information_available_at="2026-10-20T17:15:03+00:00",
        training_cutoff="2026-10-20T17:15:30+00:00", issued_at="2026-10-20T17:16:02+00:00",
        recorded_at="2026-10-20T17:16:03+00:00", target_version=TARGET_VERSION,
        mode="live", availability_basis="observed", probability_not_down=probability,
        configured_count=1, successful_count=1, ensemble_status="full",
        classification="not-down" if probability >= .5 else "down",
        models=[dict(name="logistic", status="success", probability_not_down=probability, version="b" * 64)],
        source_dataset="MULTI_PROVIDER", source_id=SOURCE_ID,
        direction=direction, direction_policy_version=policy,
        direction_up_threshold=.5001, direction_down_threshold=.4999)
    with sqlite3.connect(path) as db:
        db.executescript("CREATE TABLE forecasts(pc TEXT, issued_at TEXT, payload TEXT);"
                         "CREATE TABLE cycles(id TEXT, pc TEXT, started_at TEXT, payload TEXT);"
                         "CREATE TABLE outcomes(forecast_id TEXT, payload TEXT);")
        db.execute("INSERT INTO forecasts VALUES(?,?,?)", (value["pc"], value["issued_at"], json.dumps(value)))
    return value


def resign(packet):
    packet["packet_id"] = digest(packet["payload"])
    return packet


@pytest.mark.parametrize("policy", POLICIES)
@pytest.mark.parametrize("probability,direction", [(.5001, "BULLISH"), (.4999, "BEARISH"), (.5, "NO_EDGE")])
def test_multi_provider_export_receipt_preserves_original_source_policy_and_probability(tmp_path, policy, probability, direction):
    database = tmp_path / "own.sqlite3"
    original = source_database(database, policy=policy, probability=probability, direction=direction)
    original_bytes = database.read_bytes()
    packet = export_packet(database, producer_pc="Atlas", source_version="reviewed-source", produced_at=PRODUCED)
    projected = packet["payload"]["forecasts"][0]
    assert all(projected[key] == value for key, value in original.items())
    assert projected["outcome"] is None
    assert database.read_bytes() == original_bytes
    packet_path = write_packet(packet, tmp_path / "packet.json")
    received = import_packet(packet_path, tmp_path / "receipts", expected_peer="Atlas", local_pc="Scout", received_at=RECEIVED)
    loaded = load_receipt(received["receipt_path"], expected_peer="Atlas", local_pc="Scout", as_of=RECEIVED)
    assert loaded["receipt"]["packet"]["payload"]["forecasts"][0] == projected


@pytest.mark.parametrize("source", [
    {"source_dataset": "MULTI_PROVIDER", "source_id": "databento:EQUS.MINI:ohlcv-1m"},
    {"source_dataset": "EQUS.MINI", "source_id": SOURCE_ID},
    {"source_dataset": "MULTI_PROVIDER", "source_id": "multi:unrecorded:15m"},
    {"source_dataset": "MULTI_PROVIDER", "source_id": "C:/private/history.parquet"},
])
def test_recovered_source_pair_rejects_mismatch_unknown_or_private_identity(tmp_path, source):
    database = tmp_path / "own.sqlite3"
    source_database(database)
    packet = export_packet(database, producer_pc="Atlas", source_version="reviewed-source", produced_at=PRODUCED)
    packet["payload"]["forecasts"][0].update(source)
    with pytest.raises(ContractError, match="source identity"):
        validate_packet(resign(packet))


@pytest.mark.parametrize("field,value", [
    ("direction_policy_version", "stock-direction-unknown"),
    ("direction_up_threshold", .5), ("direction_down_threshold", .5),
    ("direction", "NO_EDGE"),
])
def test_recovered_source_still_enforces_recorded_policy(tmp_path, field, value):
    database = tmp_path / "own.sqlite3"
    source_database(database)
    packet = export_packet(database, producer_pc="Atlas", source_version="reviewed-source", produced_at=PRODUCED)
    packet["payload"]["forecasts"][0][field] = value
    with pytest.raises(ContractError, match="direction policy"):
        validate_packet(resign(packet))
