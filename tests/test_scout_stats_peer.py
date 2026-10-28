"""Offline synthetic archives for the pinned Scout display reader."""
from __future__ import annotations

from collections import defaultdict
import hashlib
import json

import pytest

from app.ui.scout_stats_data import execution_summary, prediction_summary
from app.ui.scout_stats_peer import load_peer_stats, peer_stats_sessions, verify_export


def _write(path, value):
    raw = json.dumps(value, sort_keys=True, allow_nan=False).encode()
    path.write_bytes(raw)
    return {"bytes": len(raw), "sha256": hashlib.sha256(raw).hexdigest()}


def _summaries(payload):
    forecasts, orders = payload["snapshot"]["forecasts"], payload["snapshot"]["orders"]
    payload["prediction_summary"] = prediction_summary(forecasts)
    payload["execution_summary"] = execution_summary(orders)
    fg, og = defaultdict(list), defaultdict(list)
    for row in forecasts:
        fg[row["symbol"]].append(row)
    for row in orders:
        og[row["symbol"]].append(row)
    payload["per_symbol"] = {symbol: {"prediction": prediction_summary(fg[symbol]),
                                     "execution": execution_summary(og[symbol])}
                             for symbol in set(fg) | set(og)}


def seal_export(directory, payload, manifest):
    """Rewrite synthetic bytes/pins after an intentional fixture mutation."""
    manifest["files"]["snapshot.json"] = _write(directory / "snapshot.json", payload)
    manifest["files"]["report.html"] = {"bytes": len((directory / "report.html").read_bytes()),
                                        "sha256": hashlib.sha256((directory / "report.html").read_bytes()).hexdigest()}
    pin = _write(directory / "manifest.json", manifest)
    _write(directory / "receipt.json", dict(session=payload["session"], completion_id=payload["completion_id"],
        manifest_sha256=pin["sha256"], snapshot_sha256=manifest["files"]["snapshot.json"]["sha256"],
        verified_at="2026-10-28T02:00:00Z"))
    return directory


def export_fixture(root, *, session="2026-10-27", completion_id="20261028T013247Z-synthetic-scout-display",
                   generated_at="2026-10-28T01:32:47Z", probability=.8):
    """Create synthetic display evidence, with no real operating information."""
    directory = root / "intraday/scout/display-exports/v1" / completion_id
    directory.mkdir(parents=True)
    forecast = dict(forecast_id="synthetic-forecast", pc="Scout", symbol="TEST", mode="live",
        target_version="intraday-15m-open-to-third-close-not-down-v1", supported_target=True,
        reference_at=f"{session}T16:00:00Z", issued_at=f"{session}T16:10:00Z",
        target_open=f"{session}T16:15:00Z", target_close=f"{session}T17:00:00Z",
        probability_not_down=probability, outcome_status="scored", target_not_down=True,
        outcome_observed_at=f"{session}T17:01:00Z", unavailable_reason=None)
    order = dict(request_id="synthetic-request", forecast_id="synthetic-forecast", broker_order_id="synthetic-order",
        symbol="TEST", side="BUY", status="FILLED", submission_state="ACCEPTED", quantity=3, filled_quantity=3,
        reference_at=f"{session}T16:00:00Z", issued_at=f"{session}T16:10:00Z", observed_at=f"{session}T16:11:00Z",
        fills=[dict(fill_id="synthetic-fill", quantity=3, price="100.25", executed_at=f"{session}T16:10:30Z")])
    common = dict(producer="Scout", recipient="Atlas", session=session, generated_at=generated_at,
                  completion_id=completion_id, source_files={"app/ui/scout_stats_data.py": "a" * 64,
                                                             "app/ui/scout_stats_widgets.py": "b" * 64})
    payload = dict(common, schema_version="scout-15m-display-export-v1", snapshot=dict(session=session,
        status="read", message="Saved synthetic Scout evidence", prediction_status="read", execution_status="read",
        forecasts=[forecast], orders=[order], warnings=[]))
    _summaries(payload)
    manifest = dict(common, schema_version="scout-15m-display-delivery-v1", machine="pc-new", files={})
    (directory / "report.html").write_text("<p>Synthetic display evidence</p>", encoding="utf-8")
    return seal_export(directory, payload, manifest)


def read_export(directory):
    return (json.loads((directory / "snapshot.json").read_text()),
            json.loads((directory / "manifest.json").read_text()))


def test_archive_discovery_and_verified_roundtrip(tmp_path):
    directory = export_fixture(tmp_path)
    assert peer_stats_sessions(tmp_path) == ("2026-10-27",)
    payload = load_peer_stats(tmp_path, "2026-10-27")
    assert payload["prediction_summary"]["accuracy"] == 1
    assert payload["execution_summary"]["filled_quantity"] == 3
    assert payload["snapshot"]["orders"][0]["fills"][0]["price"] == "100.25"
    assert verify_export(directory) == payload
    assert load_peer_stats(tmp_path, "2026-10-26") is None


@pytest.mark.parametrize("filename", ["manifest.json", "snapshot.json", "report.html"])
def test_pinned_archives_reject_mutated_bytes(tmp_path, filename):
    directory = export_fixture(tmp_path)
    with (directory / filename).open("ab") as handle:
        handle.write(b" ")
    with pytest.raises(ValueError, match="hash differs"):
        load_peer_stats(tmp_path, "2026-10-27")


@pytest.mark.parametrize("mutation", ["recipient", "machine", "session", "extra_field", "summary", "symbol_summary",
                                      "duplicate_forecast", "duplicate_request", "duplicate_fill", "wrong_session", "nan"])
def test_semantic_validation_even_after_resealing(tmp_path, mutation):
    directory = export_fixture(tmp_path)
    payload, manifest = read_export(directory)
    if mutation == "recipient":
        payload["recipient"] = manifest["recipient"] = "Scout"
    elif mutation == "machine":
        manifest["machine"] = "pc-original"
    elif mutation == "session":
        payload["session"] = "2026-10-26"
    elif mutation == "extra_field":
        payload["snapshot"]["orders"][0]["account_number"] = "synthetic forbidden field"
    elif mutation == "summary":
        payload["execution_summary"]["filled_quantity"] = 999
    elif mutation == "symbol_summary":
        payload["per_symbol"]["TEST"]["prediction"]["correct"] = 999
    elif mutation == "duplicate_forecast":
        payload["snapshot"]["forecasts"] *= 2
        _summaries(payload)
    elif mutation == "duplicate_request":
        payload["snapshot"]["orders"] *= 2
        _summaries(payload)
    elif mutation == "duplicate_fill":
        payload["snapshot"]["orders"][0]["fills"] *= 2
        _summaries(payload)
    elif mutation == "wrong_session":
        payload["snapshot"]["forecasts"][0]["reference_at"] = "2026-10-26T16:00:00Z"
    else:
        payload["snapshot"]["forecasts"][0]["probability_not_down"] = "NaN"
    seal_export(directory, payload, manifest)
    with pytest.raises(ValueError, match="Scout display export"):
        verify_export(directory)


def test_latest_export_selected_without_combining_rows(tmp_path):
    export_fixture(tmp_path)
    export_fixture(tmp_path, completion_id="20261028T023247Z-synthetic-newer-display", generated_at="2026-10-28T02:32:47Z", probability=.2)
    payload = load_peer_stats(tmp_path, "2026-10-27")
    assert payload["prediction_summary"]["accuracy"] == 0
    assert len(payload["snapshot"]["forecasts"]) == 1
    assert peer_stats_sessions(tmp_path) == ("2026-10-27",)


def test_unimported_directory_is_not_discovered(tmp_path):
    directory = export_fixture(tmp_path)
    (directory / "receipt.json").unlink()
    assert peer_stats_sessions(tmp_path) == ()
    assert load_peer_stats(tmp_path, "2026-10-27") is None


def test_manifest_path_escape_is_rejected(tmp_path):
    directory = export_fixture(tmp_path)
    payload, manifest = read_export(directory)
    manifest["files"]["../outside.json"] = manifest["files"]["snapshot.json"]
    seal_export(directory, payload, manifest)
    with pytest.raises(ValueError, match="fields"):
        load_peer_stats(tmp_path, "2026-10-27")


def test_json_duplicate_keys_are_rejected(tmp_path):
    directory = export_fixture(tmp_path)
    (directory / "manifest.json").write_bytes(b'{"machine":"pc-new","machine":"pc-original"}')
    with pytest.raises(ValueError, match="invalid JSON"):
        verify_export(directory)
