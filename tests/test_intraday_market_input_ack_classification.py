"""Offline ACK classification must not invent provider entitlement diagnoses."""
from datetime import datetime, timezone

import pytest

from ml.intraday.market_input_schwab_stream import SchwabMarketStream


NOW = datetime(2026, 10, 17, 15, 0, tzinfo=timezone.utc)


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    import requests
    import websocket

    def forbidden(*args, **kwargs):
        raise AssertionError("ACK fixtures must not call providers")

    monkeypatch.setattr(requests.Session, "request", forbidden)
    monkeypatch.setattr(websocket, "create_connection", forbidden)


def ingest_ack(tmp_path, code, service):
    captured = []
    stream = SchwabMarketStream(tmp_path, ["AAPL"], clock=lambda: NOW,
        persist_raw=lambda payload, metadata: captured.append((payload, metadata)) or {"id": "offline-ack"})
    stream._pending["7"] = (service, "SUBS", ["fixture-key"])
    stream._acks[service] = {"status": "pending", "requested": 1, "keys": [], "codes": []}
    stream.ingest({"response": [{"requestid": "7", "service": service,
        "command": "SUBS", "content": {"code": code}}]}, received_at=NOW)
    return stream, captured


@pytest.mark.parametrize("service", ["LEVELONE_EQUITIES", "OPTIONS_BOOK", "LEVELONE_FUTURES"])
@pytest.mark.parametrize("code", [-1, 1, 3, 12, 20, 24, 30, 401, 429, 999])
def test_generic_service_rejection_retains_code_without_entitlement_claim(tmp_path, service, code):
    stream, captured = ingest_ack(tmp_path, code, service)
    acknowledgement = stream._acks[service]
    assert acknowledgement["status"] == "request_rejected"
    assert acknowledgement["keys"] == []
    assert acknowledgement["codes"] == [code]
    assert "7" not in stream._pending
    assert stream._ack_history["7"]["code"] == code
    assert captured[0][0]["response"] == [{"requestid": "7", "service": service,
        "command": "SUBS", "code": code}]
    assert captured[0][1]["received_at"] == NOW.isoformat()


@pytest.mark.parametrize("code,status,keys", [
    (0, "ok", ["fixture-key"]),
    (19, "capacity_blocked", []),
])
def test_success_and_existing_capacity_classification_are_unchanged(tmp_path, code, status, keys):
    stream, captured = ingest_ack(tmp_path, code, "OPTIONS_BOOK")
    assert stream._acks["OPTIONS_BOOK"]["status"] == status
    assert stream._acks["OPTIONS_BOOK"]["keys"] == keys
    assert stream._acks["OPTIONS_BOOK"]["codes"] == [code]
    assert captured[0][0]["response"][0]["code"] == code
