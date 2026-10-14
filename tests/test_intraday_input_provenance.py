"""Saved reference metadata fixtures; no training, providers or live stores."""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
import sqlite3
from types import SimpleNamespace

import pytest

from app.ui import intraday_data
from app.ui.intraday_data import reference_input_label, reference_input_provenance


REFERENCE = "2026-10-14T22:30:00+00:00"
SOURCE = "databento:EQUS.MINI:ohlcv-1m"


def forecast(**changes):
    row = {"forecast_id": "forecast-a", "pc": "Atlas", "symbol": "AMZN", "cycle_id": "cycle-a",
           "reference_at": REFERENCE, "source_id": SOURCE, "source_dataset": "EQUS.MINI",
           "configured_count": 16, "successful_count": 16, "ensemble_status": "full",
           "probability_not_down": .6385371914801821, "outcome_status": "pending", "models": [],
           "availability_basis": "observed", "issued_at": "2026-10-14T22:55:40+00:00",
           "target_open": "2026-10-14T23:00:00+00:00", "target_close": "2026-10-14T23:15:00+00:00"}
    row.update(changes)
    return row


def provenance(observed=0, synthetic=15, **changes):
    value = {"reference_at": REFERENCE, "observed_constituents": observed,
             "synthetic_constituents": synthetic, "synthetic": synthetic > 0,
             "no_trade": synthetic == 15, "source_id": SOURCE, "availability_kind": "actual"}
    value.update(changes)
    return value


def candle(observed=0, synthetic=15, **changes):
    value = provenance(observed, synthetic)
    value["opened_at"] = value.pop("reference_at")
    value.update(changes)
    return value


def write_snapshot(path, snapshot, *, linked_symbol="AMZN", linked_cycle="cycle-a", snapshot_id="snapshot-a"):
    with sqlite3.connect(path) as connection:
        connection.executescript("""
            CREATE TABLE snapshots(id TEXT PRIMARY KEY, payload TEXT NOT NULL);
            CREATE TABLE symbol_inputs(cycle_id TEXT,symbol TEXT,snapshot_id TEXT,PRIMARY KEY(cycle_id,symbol));
        """)
        connection.execute("INSERT INTO snapshots VALUES(?,?)", (snapshot_id, json.dumps(snapshot)))
        connection.execute("INSERT INTO symbol_inputs VALUES(?,?,?)", (linked_cycle, linked_symbol, snapshot_id))


@pytest.mark.parametrize("observed,synthetic", [(0, 15), (14, 1), (15, 0)])
def test_future_saved_summary_separates_input_counts_from_model_success(observed, synthetic, monkeypatch):
    def forbid_connection(*args, **kwargs):
        pytest.fail("A saved forecast summary must not read its training snapshot")
    monkeypatch.setattr(intraday_data.sqlite3, "connect", forbid_connection)
    row = forecast(reference_provenance=provenance(observed, synthetic))
    original = deepcopy(row)
    result = reference_input_provenance(row, database_path="unused.sqlite3")
    assert result["status"] == "known" and result["basis"] == "saved_forecast"
    assert reference_input_label(result) == (
        f"Input candle: {observed} observed minutes · {synthetic} confirmed no-trade minutes")
    assert row == original and row["successful_count"] == 16


def test_saved_summary_matches_same_reference_in_another_aware_timezone():
    row = forecast(reference_provenance=provenance(reference_at="2026-10-14T15:30:00-07:00"))
    assert reference_input_provenance(row)["status"] == "known"


@pytest.mark.parametrize("observed,synthetic", [(0.0, 15.0), (14.0, 1.0), (15.0, 0.0)])
def test_integral_counts_from_concatenated_history_are_preserved_without_rounding(observed, synthetic, tmp_path):
    path = tmp_path / "float-counts.sqlite3"
    write_snapshot(path, {"candles": [candle(observed, synthetic)]})
    result = reference_input_provenance(forecast(), database_path=path)
    assert result["status"] == "known"
    assert type(result["observed_constituents"]) is type(result["synthetic_constituents"]) is int
    assert result["observed_constituents"] == observed and result["synthetic_constituents"] == synthetic


@pytest.mark.parametrize("changes", [
    {"observed_constituents": True}, {"observed_constituents": "0"},
    {"observed_constituents": None}, {"observed_constituents": -1, "synthetic_constituents": 16},
    {"observed_constituents": .5, "synthetic_constituents": 14.5},
    {"observed_constituents": float("nan")}, {"observed_constituents": float("inf")},
    {"observed_constituents": 1, "synthetic_constituents": 15},
    {"observed_constituents": 15, "synthetic_constituents": 0, "synthetic": True},
    {"no_trade": False}, {"synthetic": 1}, {"no_trade": None},
    {"reference_at": "2026-10-14T22:15:00+00:00"},
    {"reference_at": "2026-10-14T22:30:00"}, {"source_id": "another-source"},
    {"reference_at": "9999-12-31T23:30:00-07:00"},
])
def test_bad_counts_or_identity_are_unknown_and_do_not_borrow_snapshot(tmp_path, changes):
    path = tmp_path / "inputs.sqlite3"
    write_snapshot(path, {"inference": {"snapshot": {"candles": [candle()]}}})
    row = forecast(reference_provenance=provenance(**changes))
    result = reference_input_provenance(row, database_path=path)
    assert result["status"] == "unknown"
    assert result["observed_constituents"] is result["synthetic_constituents"] is None
    assert reference_input_label(result) == "Input candle: minute provenance unknown"


@pytest.mark.parametrize("saved", [None, [], "bad", {}])
def test_malformed_present_summary_stays_unknown(saved):
    assert reference_input_provenance(forecast(reference_provenance=saved))["status"] == "unknown"


def test_legacy_exact_linked_snapshot_extracts_only_small_metadata_without_mutation(tmp_path, monkeypatch):
    path = tmp_path / "inputs.sqlite3"
    snapshot = {"X": [["TRAINING_MATRIX_MUST_NOT_LEAVE_SQLITE"]] * 3000,
                "y": [0, 1] * 1500, "inference": {"snapshot": {"candles": [candle()]}},
                "candles": [candle()]}
    write_snapshot(path, snapshot)
    before = hashlib.sha256(path.read_bytes()).hexdigest()
    original_loads = json.loads
    deserialized = []
    def guarded_loads(raw, *args, **kwargs):
        assert "TRAINING_MATRIX_MUST_NOT_LEAVE_SQLITE" not in raw
        deserialized.append(raw)
        return original_loads(raw, *args, **kwargs)
    monkeypatch.setattr(intraday_data.json, "loads", guarded_loads)
    result = reference_input_provenance(forecast(snapshot_hash="snapshot-a"), database_path=path)
    assert result["status"] == "known" and result["basis"] == "immutable_snapshot"
    assert result["observed_constituents"] == 0 and result["synthetic_constituents"] == 15
    assert deserialized and all(len(raw) < 400 for raw in deserialized)
    assert hashlib.sha256(path.read_bytes()).hexdigest() == before


@pytest.mark.parametrize("layout", ["outer_only", "compact_missing_counts"])
def test_legacy_outer_reference_candle_is_used_when_compact_counts_were_not_recorded(tmp_path, layout):
    snapshot = {"candles": [candle(14, 1)]}
    if layout == "compact_missing_counts":
        compact = candle(14, 1)
        compact.pop("observed_constituents")
        compact.pop("synthetic_constituents")
        snapshot["inference"] = {"snapshot": {"candles": [compact]}}
    path = tmp_path / "legacy.sqlite3"
    write_snapshot(path, snapshot)
    result = reference_input_provenance(forecast(), database_path=path)
    assert result["status"] == "known"
    assert reference_input_label(result) == "Input candle: 14 observed minutes · 1 confirmed no-trade minutes"


@pytest.mark.parametrize("changes", [
    {"snapshot_hash": "another-snapshot"}, {"cycle_id": "another-cycle"},
    {"symbol": "AAPL"}, {"reference_at": "2026-10-14T22:45:00+00:00"},
    {"source_id": "another-source"},
])
def test_snapshot_link_and_reference_identity_prevent_neighboring_candle_substitution(tmp_path, changes):
    path = tmp_path / "linked.sqlite3"
    write_snapshot(path, {"candles": [candle()]})
    assert reference_input_provenance(forecast(**changes), database_path=path)["status"] == "unknown"


def test_missing_store_is_never_created_and_missing_snapshot_schema_is_unknown(tmp_path):
    absent = tmp_path / "missing" / "inputs.sqlite3"
    assert reference_input_provenance(forecast(), database_path=absent)["status"] == "unknown"
    assert not absent.exists() and not absent.parent.exists()
    empty = tmp_path / "empty.sqlite3"
    with sqlite3.connect(empty) as connection:
        connection.execute("CREATE TABLE unrelated(value TEXT)")
    assert reference_input_provenance(forecast(), database_path=empty)["status"] == "unknown"


def test_malformed_snapshot_json_or_absent_candle_counts_remain_unknown(tmp_path):
    malformed = tmp_path / "malformed.sqlite3"
    write_snapshot(malformed, {})
    with sqlite3.connect(malformed) as connection:
        connection.execute("UPDATE snapshots SET payload='{bad'")
    assert reference_input_provenance(forecast(), database_path=malformed)["status"] == "unknown"
    absent_counts = tmp_path / "unknown.sqlite3"
    write_snapshot(absent_counts, {"candles": [{"opened_at": REFERENCE, "synthetic": True, "no_trade": True}]})
    assert reference_input_provenance(forecast(), database_path=absent_counts)["status"] == "unknown"


def test_historical_synthetic_counts_do_not_claim_actual_no_trade_confirmation():
    result = reference_input_provenance(forecast(reference_provenance=provenance(
        14, 1, availability_kind="assumed_historical_close")))
    assert result["status"] == "known"
    label = reference_input_label(result)
    assert "1 synthetic minutes" in label and "confirmation not recorded" in label
    assert "confirmed no-trade minutes" not in label


@pytest.mark.parametrize("saved,label", [
    (provenance(), "Input candle: 0 observed minutes · 15 confirmed no-trade minutes"),
    (provenance(15, 0), "Input candle: 15 observed minutes · 0 confirmed no-trade minutes"),
    (None, "Input candle: minute provenance unknown"),
])
def test_actual_consensus_projection_keeps_input_and_model_success_separate(saved, label, monkeypatch):
    from app.ui import intraday
    row = forecast(reference_provenance=saved)
    text = {}
    view = SimpleNamespace(
        models_frame=SimpleNamespace(winfo_children=lambda: []), consensus_rows={"choice": row},
        consensus_choice=SimpleNamespace(get=lambda: "choice"), local_pc="Atlas",
        symbol=SimpleNamespace(get=lambda: "AMZN"), database_paths={},
        model_research={"status": "missing", "message": "No saved research"},
        research_status=SimpleNamespace(configure=lambda **values: None), research_table=object(),
        consensus_value=SimpleNamespace(configure=lambda **values: text.update(consensus=values["text"])),
        consensus_meta=SimpleNamespace(configure=lambda **values: text.update(metadata=values["text"])),
    )
    monkeypatch.setattr(intraday, "_replace", lambda *args: None)
    intraday.IntradayTab._render_consensus(view)
    assert text["consensus"] == "63.9% ensemble probability · not-down"
    assert "16/16 enabled models succeeded · full ensemble" in text["metadata"]
    assert label in text["metadata"]
    assert "receipt availability observed" in text["metadata"]
    assert row == forecast(reference_provenance=saved)

