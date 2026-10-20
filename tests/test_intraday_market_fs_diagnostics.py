from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import socket

import pytest
import requests

from ml.intraday import market_collection as collection
from ml.intraday import market_collection_schwab_stream as stream
from ml.intraday.market_collection_adapters import SchwabAdapter


START = datetime(2026, 10, 20, 16, 0, tzinfo=timezone.utc)
END = START + timedelta(minutes=15)
NOW = END + timedelta(seconds=10)
STREAM_ID = "a" * 32


@pytest.fixture(autouse=True)
def deny_network(monkeypatch):
    def denied(*args, **kwargs):
        raise AssertionError("Filesystem regression must not use a provider or broker")
    monkeypatch.setattr(requests.sessions.Session, "request", denied)
    monkeypatch.setattr(socket, "create_connection", denied)


def request():
    return collection.SourceRequest(provider="schwab", source_kind="stock_bars",
        symbol="SDGR", dataset="streamer", schema="CHART_EQUITY", endpoint="stream",
        params={"projection_version": stream.VERSION, "mode": "current_stream_snapshot",
                "fields": list(range(stream.FIELDS["CHART_EQUITY"])), "capture_seconds": 5})


def make_collector(root, adapter, *, clock=lambda: NOW):
    result = collection.QuarterHourMarketCollector(root, symbols=["SDGR"],
        requests=[request()], adapters={"schwab": adapter}, clock=clock)
    result.enqueue(START)
    return result


def response_path(root):
    value = collection._sha({"start": START, "end": END,
        "normalization_version": stream.NORMALIZATION_VERSION,
        "bootstrap_version": stream.BOOTSTRAP_VERSION, "scope_version": stream.SCOPE_VERSION,
        "scope_limits": stream.OPTION_SCOPE_LIMITS, "requests": [request().identity]})
    return root / "responses" / START.date().isoformat() / ("schwab-stream-" + value + ".json")


def save_response(root, evidence):
    path = response_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    value = {"rows": {}, "evidence": evidence}
    value["response_sha256"] = collection._sha(value)
    path.write_bytes(collection.canonical(value))
    return path


def diagnostic(receipt):
    value = receipt["evidence"]["filesystem_diagnostics"]
    assert value["schema_version"] == "intraday-local-filesystem-exception-v1"
    assert value["exception_chain"]
    return value


@pytest.mark.parametrize("boundary,expected_role", [
    ("response_read", "stream_response"),
    ("checkpoint_read", "stream_checkpoint"),
    ("bootstrap_read", "stream_chain_bootstrap"),
    ("session_initialization", "stream_checkpoint"),
    ("response_publication", "stream_response"),
])
def test_real_missing_file_boundaries_preserve_error_and_precise_safe_role(
        tmp_path, monkeypatch, boundary, expected_role):
    root = tmp_path / "collection"
    root.mkdir()
    checkpoint = root / "raw" / "2026-10-20" / ("schwab-stream-" + STREAM_ID + ".state.json")
    bootstrap = root / "raw" / "2026-10-20" / ("schwab-chain-bootstrap-" + STREAM_ID + ".json")
    adapter = SchwabAdapter(root=root, requests=[request()], session_factory=lambda: object())
    if boundary == "response_read":
        target = response_path(root)
        original_exists = Path.exists
        monkeypatch.setattr(Path, "exists", lambda path: True if path == target else original_exists(path))
    elif boundary == "checkpoint_read":
        save_response(root, {"state_checkpoint_path": str(checkpoint), "state_checkpoint_sha256": "b" * 64})
    elif boundary == "bootstrap_read":
        save_response(root, {"chain_bootstrap_receipts": {"SDGR": {"path": str(bootstrap), "bytes": 1}}})
    elif boundary == "session_initialization":
        def missing_session():
            checkpoint.read_bytes()
        adapter.session_factory = missing_session
    else:
        adapter.stream.capture_fn = lambda *args, **kwargs: ({}, {"service_results": {}})
        original_replace = Path.replace
        def remove_before_publication(path, destination):
            if destination == response_path(root):
                path.unlink()
            return original_replace(path, destination)
        monkeypatch.setattr(Path, "replace", remove_before_publication)
    col = make_collector(root, adapter)
    receipt = col.step()[0]
    assert receipt["status"] == "ERROR"
    assert receipt["record_count"] == 0
    assert receipt["snapshot_sha256"] == collection.snapshot_digest(receipt)
    chain = diagnostic(receipt)["exception_chain"]
    assert chain[0]["type"] == "FileNotFoundError"
    assert chain[0]["errno"] == 2
    paths = [entry[key] for entry in chain for key in ("filename", "filename2") if key in entry]
    assert any(path["role"] == expected_role and "relative_path" in path for path in paths)
    assert all(str(tmp_path) not in json.dumps(entry) for entry in chain)
    saved = next(iter(json.loads(col.state_path.read_bytes())["tasks"].values()))
    assert saved["attempts"] == 1 and saved["status"] == "ERROR"
    assert collection.instant(saved["due_at"]) > NOW
    assert col.step() == []


@pytest.mark.parametrize("suppress", [False, True])
def test_wrapped_filesystem_cause_preserves_original_evidence_and_suppressed_context(tmp_path, suppress):
    root = tmp_path / "collection"
    missing = root / "raw" / "2026-10-20" / ("schwab-stream-" + STREAM_ID + ".state.json")
    original_evidence = {"bootstrap_version": "kept", "native_evidence": {"hash": "saved"}}
    def adapter(*args):
        try:
            missing.read_bytes()
        except OSError as error:
            failure = collection.CollectionFailure("UNAVAILABLE", "Safe native failure", original_evidence)
            if suppress:
                raise failure from None
            raise failure from error
    receipt = make_collector(root, adapter).step()[0]
    assert receipt["status"] == "UNAVAILABLE" and receipt["reason"] == "Safe native failure"
    assert receipt["evidence"]["native_evidence"] == original_evidence["native_evidence"]
    assert original_evidence == {"bootstrap_version": "kept", "native_evidence": {"hash": "saved"}}
    chain = diagnostic(receipt)["exception_chain"]
    assert [entry["type"] for entry in chain] == ["CollectionFailure", "FileNotFoundError"]
    assert chain[1]["relation"] == ("context" if suppress else "cause")
    assert chain[0]["context_suppressed"] is True


@pytest.mark.parametrize("frame_filename", ["token=PRIVATE_SENTINEL_68ac.py", "PRIVATE_SENTINEL_68ac.py"])
def test_private_external_paths_messages_urls_and_frame_paths_are_not_saved(tmp_path, frame_filename):
    root = tmp_path / "collection"
    secret = "PRIVATE_SENTINEL_68ac"
    outside = tmp_path / (secret + "-tokens.json")
    code = compile("def " + secret + "(path):\n    raise FileNotFoundError(2, 'https://host/?apikey=" + secret + "', path)\n",
                   "C:/private/" + frame_filename, "exec")
    namespace = {}
    exec(code, namespace)
    def adapter(*args):
        namespace[secret](str(outside))
    receipt = make_collector(root, adapter).step()[0]
    detail = diagnostic(receipt)
    text = json.dumps(receipt)
    assert secret not in text and "https://host" not in text
    assert str(tmp_path) not in json.dumps(detail)
    name = detail["exception_chain"][0]["filename"]
    assert name["role"] == "opaque_path" and len(name["path_sha256"]) == 64
    assert "relative_path" not in name
    assert all("/" not in frame["file"] and "\\" not in frame["file"]
               for entry in detail["exception_chain"] for frame in entry["frames"])


def test_nonfilesystem_provider_errors_keep_existing_evidence_shape(tmp_path):
    def adapter(*args):
        raise requests.exceptions.ConnectionError("https://host/?apikey=PRIVATE_SENTINEL")
    receipt = make_collector(tmp_path, adapter).step()[0]
    assert receipt["status"] == "ERROR" and receipt["evidence"] == {}
    assert b"PRIVATE_SENTINEL" not in collection.canonical(receipt)


def test_windows_filesystem_codes_remain_numeric_and_error_policy_is_preserved(tmp_path):
    error = PermissionError(13, "Private native denial", str(tmp_path / "private-token-cache.json"))
    error.winerror = 5
    def adapter(*args):
        raise error
    receipt = make_collector(tmp_path / "collection", adapter).step()[0]
    assert (receipt["status"], receipt["reason"]) == collection.classify_error(error)
    entry = diagnostic(receipt)["exception_chain"][0]
    assert entry["errno"] == 13 and entry["winerror"] == 5
    assert entry["filename"]["role"] == "opaque_path"
    assert "private-token-cache" not in json.dumps(receipt)


def test_filesystem_diagnostics_are_bounded_and_keep_provider_owned_keys(tmp_path):
    root = tmp_path / "collection"
    missing = root / "raw" / "2026-10-20" / ("schwab-stream-" + STREAM_ID + ".state.json")
    def recursive(depth):
        if depth:
            return recursive(depth - 1)
        missing.read_bytes()
    def adapter(*args):
        try:
            recursive(20)
        except OSError as error:
            error.__cause__ = error
            raise collection.CollectionFailure("ERROR", "Original failure",
                {"filesystem_diagnostics": {"provider_owned": True}}) from error
    receipt = make_collector(root, adapter).step()[0]
    assert receipt["evidence"]["filesystem_diagnostics"] == {"provider_owned": True}
    detail = receipt["evidence"]["adapter_filesystem_diagnostics"]
    assert len(detail["exception_chain"]) <= 6 and detail["chain_truncated"]
    assert all(len(entry["frames"]) <= 8 for entry in detail["exception_chain"])
    assert any(entry["frames_truncated"] for entry in detail["exception_chain"])
