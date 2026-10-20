"""Checkpoint replay uses preserved native evidence, never network recovery."""
from copy import deepcopy
from datetime import datetime, timezone, timedelta
import hashlib
import json
from pathlib import Path
import socket

import pytest

from ml.intraday.market_collection import canonical, _sha
from ml.intraday import market_collection_schwab_stream as stream
from ml.intraday.market_state import StreamState, VERSION as STATE_VERSION


NOW = datetime(2026, 10, 19, 12, 45, 30, tzinfo=timezone.utc)


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("Checkpoint verification must not call a provider")
    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)


def legacy(tmp_path, *, roll=False, late=False):
    stamp = NOW.isoformat()
    identity_at = NOW + timedelta(seconds=1) if late else NOW-timedelta(seconds=1)
    natives = [{"key": "/ES", "10": int(identity_at.timestamp()*1000), "34": "/ESZ26", "1": 10, "2": 11}]
    if roll:
        natives.append({"key": "/ES", "10": int(identity_at.timestamp()*1000), "34": "/ESH27"})
    natives.append({"key": "/ES", "10": int(NOW.timestamp()*1000), "1": 10.25, "2": 11.25})
    raw = canonical({"received_at": stamp, "message": {"data": [{
        "service": "LEVELONE_FUTURES", "timestamp": int(NOW.timestamp()*1000), "content": natives}]}})+b"\n"
    raw_path = tmp_path/"native.jsonl"
    raw_path.write_bytes(raw)
    state = StreamState("fixture-connection")
    for sequence, native in enumerate(natives, 1):
        row = stream.normalize("LEVELONE_FUTURES", native, receipt_at=stamp, scope={"/ES": None})
        row.update(sequence=sequence, stream_message_timestamp=stamp,
            identity_basis="connection-local received market record sequence; not an exchange sequence")
        state.observe("LEVELONE_FUTURES", row)
    checkpoint = {"version": STATE_VERSION, "connection_id": "fixture-connection", "completed": True,
        "disconnected_at": stamp, "reusable_on_reconnect": False,
        "states": state.checkpoint({"LEVELONE_FUTURES": ["/ES"]}, ended_at=stamp,
            healthy=True, transport_confirmed_at=stamp)}
    checkpoint_path = tmp_path/"native.state.json"
    checkpoint_path.write_bytes(canonical(checkpoint))
    projected = deepcopy(checkpoint)
    row = projected["states"]["LEVELONE_FUTURES"][0]
    if roll or late:
        row["root_symbol"] = None
    else:
        row["symbol"] = "/ESZ26"
    evidence = {"mode": "current_stream_snapshot", "projection_version": stream.VERSION,
        "normalization_version": stream.NORMALIZATION_VERSION, "completed": True,
        "state_version": STATE_VERSION, "connection_id": "fixture-connection",
        "service_results": {"LEVELONE_FUTURES": {"acknowledged_scope": ["/ES"]}},
        "state_checkpoint_path": str(checkpoint_path), "state_checkpoint_sha256": _sha(projected),
        "raw_path": str(raw_path), "raw_sha256": hashlib.sha256(raw).hexdigest(), "raw_bytes": len(raw),
        "chain_bootstrap_receipts": {}}
    return evidence, checkpoint, projected


def original_files(tmp_path):
    return {str(p): p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()}


@pytest.mark.parametrize("options", [{}, {"roll": True}, {"late": True}])
def test_exact_legacy_projection_recovery_is_read_only_and_causal(tmp_path, options):
    evidence, checkpoint, _ = legacy(tmp_path, **options)
    before = original_files(tmp_path)
    recovered = stream._verify_bootstrap(evidence)
    assert recovered["mode"] == "legacy_futures_projection_alias"
    assert recovered["original_checkpoint_sha256"] == _sha(checkpoint)
    assert recovered["recorded_checkpoint_sha256"] == evidence["state_checkpoint_sha256"]
    assert recovered["raw_sha256"] == evidence["raw_sha256"]
    assert recovered["original_artifacts_preserved"] is True
    assert original_files(tmp_path) == before


def test_valid_checkpoint_does_not_require_legacy_raw(tmp_path):
    evidence, checkpoint, _ = legacy(tmp_path)
    evidence["state_checkpoint_sha256"] = _sha(checkpoint)
    Path(evidence["raw_path"]).unlink()
    assert stream._verify_bootstrap(evidence) is None


@pytest.mark.parametrize("damage", ["raw_missing", "raw_bytes", "raw_hash", "raw_tamper", "raw_schema",
    "raw_future_scope", "raw_bound", "checkpoint_price", "checkpoint_connection", "evidence_connection",
    "checkpoint_version", "checkpoint_incomplete", "expected_digest", "causality", "roll_identity",
    "checkpoint_symbol", "checkpoint_active_contract", "checkpoint_root", "checkpoint_chart",
    "checkpoint_ack_scope"])
def test_legacy_recovery_rejects_unrelated_damage(tmp_path, damage):
    evidence, checkpoint, _ = legacy(tmp_path)
    raw_path = Path(evidence["raw_path"])
    if damage == "raw_missing":
        raw_path.unlink()
    elif damage == "raw_bytes":
        evidence["raw_bytes"] += 1
    elif damage == "raw_hash":
        evidence["raw_sha256"] = "0"*64
    elif damage == "raw_tamper":
        raw_path.write_bytes(raw_path.read_bytes()+b" ")
    elif damage in {"raw_schema", "raw_future_scope", "causality", "roll_identity"}:
        value = json.loads(raw_path.read_bytes())
        if damage == "raw_schema":
            value["message"]["data"] = "invalid"
        elif damage == "raw_future_scope":
            value["message"]["data"][0]["content"][0]["34"] = "/NQZ26"
        elif damage == "causality":
            value["received_at"] = (NOW+timedelta(seconds=1)).isoformat()
        else:
            value["message"]["data"][0]["content"].append({"key": "/ES", "10": int(NOW.timestamp()*1000), "34": "/ESH27"})
        raw = canonical(value)+b"\n"
        raw_path.write_bytes(raw)
        evidence.update(raw_bytes=len(raw), raw_sha256=hashlib.sha256(raw).hexdigest())
    elif damage == "raw_bound":
        evidence["raw_bytes"] = 64*1024*1024+1
    elif damage == "evidence_connection":
        evidence["connection_id"] = "other"
    elif damage == "expected_digest":
        evidence["state_checkpoint_sha256"] = "0"*64
    elif damage == "checkpoint_ack_scope":
        evidence["service_results"]["LEVELONE_FUTURES"]["acknowledged_scope"] = []
    elif damage == "checkpoint_chart":
        checkpoint["states"]["CHART_FUTURES"] = [{"provider_symbol": "/ES"}]
        Path(evidence["state_checkpoint_path"]).write_bytes(canonical(checkpoint))
    else:
        field = {"checkpoint_price": "bid", "checkpoint_connection": "connection_id",
            "checkpoint_version": "version", "checkpoint_incomplete": "completed", "checkpoint_symbol": "symbol",
            "checkpoint_active_contract": "active_contract_symbol", "checkpoint_root": "root_symbol"}[damage]
        target = checkpoint["states"]["LEVELONE_FUTURES"][0] if field in {
            "bid", "symbol", "active_contract_symbol", "root_symbol"} else checkpoint
        target[field] = False if field == "completed" else 11.0 if field == "bid" else "changed"
        Path(evidence["state_checkpoint_path"]).write_bytes(canonical(checkpoint))
    before = original_files(tmp_path)
    with pytest.raises((ValueError, OSError)):
        stream._verify_bootstrap(evidence)
    assert original_files(tmp_path) == before


def test_legacy_recovery_still_verifies_chain_archives(tmp_path):
    evidence, _, _ = legacy(tmp_path)
    archive = tmp_path/"chain.json"
    archive.write_bytes(b"{}")
    evidence["chain_bootstrap_receipts"] = {"DOCU": {"path": str(archive), "bytes": 2, "sha256": "0"*64}}
    with pytest.raises(ValueError, match="archive checksum"):
        stream._verify_bootstrap(evidence)


@pytest.mark.parametrize("damaged", [None, "hash", "json"])
def test_saved_response_reuses_verified_legacy_data_without_writing(tmp_path, monkeypatch, damaged):
    evidence, checkpoint, projected = legacy(tmp_path)
    evidence["service_results"]["LEVELONE_FUTURES"].update(status="COMPLETE", reason="fixture")
    requests = stream.stream_requests([], futures=["/ES"])
    request = requests[0]
    start, end = NOW-timedelta(minutes=15), NOW
    identity = _sha({"start": start, "end": end, "normalization_version": stream.NORMALIZATION_VERSION,
        "bootstrap_version": stream.BOOTSTRAP_VERSION, "scope_version": stream.SCOPE_VERSION,
        "scope_limits": stream.OPTION_SCOPE_LIMITS, "requests": [r.identity for r in requests]})
    response = tmp_path/"responses"/start.date().isoformat()/("schwab-stream-"+identity+".json")
    saved = {"rows": projected["states"], "evidence": evidence}
    saved["response_sha256"] = "0"*64 if damaged == "hash" else _sha(saved)
    response.parent.mkdir(parents=True)
    response.write_bytes(b"{" if damaged == "json" else canonical(saved))
    def forbidden(*args, **kwargs):
        pytest.fail("Saved response replay must not fetch or replace artifacts")
    monkeypatch.setattr(stream, "_atomic", forbidden)
    if damaged:
        monkeypatch.setattr(stream, "_verify_bootstrap", forbidden)
    collector = stream.SchwabStreamCollector(tmp_path, requests, capture_fn=forbidden, chain_fetch=forbidden)
    before = original_files(tmp_path)
    if damaged:
        with pytest.raises(ValueError):
            collector(None, request, start, end, 100)
    else:
        result = collector(None, request, start, end, 100)
        assert result.status == "COMPLETE"
        assert result.records == projected["states"]["LEVELONE_FUTURES"]
        assert result.evidence["checkpoint_verification"]["original_checkpoint_sha256"] == _sha(checkpoint)
        assert "checkpoint_verification" not in json.loads(response.read_bytes())["evidence"]
    assert original_files(tmp_path) == before
