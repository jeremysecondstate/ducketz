"""Rejected option expirations retain evidence without suppressing other scopes."""
from datetime import datetime, timedelta, timezone
import copy
from pathlib import Path

import pytest

from ml.intraday.market_input_schwab_stream import SchwabMarketStream
from ml.intraday.provider_recovery import ProviderRequestError


NOW = datetime(2026, 10, 20, 18, tzinfo=timezone.utc)
DATES = ["2026-10-23", "2026-10-30", "2026-11-06"]


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def denied(*args, **kwargs):
        raise AssertionError("Network is forbidden in this regression")
    monkeypatch.setattr("socket.create_connection", denied)
    monkeypatch.setattr("socket.socket.connect", denied)
    monkeypatch.setattr("socket.socket.connect_ex", denied)


def chain(expiry):
    native = "MU " + expiry.replace("-", "")[2:] + "C00100000"
    return {"symbol": "MU", "status": "SUCCESS",
            "callExpDateMap": {expiry: {"100": [{"symbol": native}]}}, "putExpDateMap": {}}


def stream_fixture(tmp_path, *, rejected=DATES[1]):
    now, calls, receipts = [NOW], [], []
    def persist(payload, metadata):
        receipts.append((copy.deepcopy(payload), copy.deepcopy(metadata)))
        return "receipt:" + str(len(receipts))
    stream = SchwabMarketStream(tmp_path, ["MU"], clock=lambda: now[0], persist_raw=persist)
    def http(endpoint, params):
        calls.append((endpoint, dict(params)))
        if endpoint == "expirationchain":
            return {"expirationList": [{"expirationDate": date} for date in DATES]}
        if "fromDate" not in params:
            raise ProviderRequestError(status=502, cause="protocol.http.TooBigBody Body buffer overflow")
        if params["fromDate"] == rejected:
            raise ProviderRequestError(status=400, cause='{"errors":[{"status":"404","title":"Not Found"}]}')
        return chain(params["fromDate"])
    stream._http = http
    def tick(count=1):
        for _ in range(count):
            stream._refresh_one_chain()
            now[0] += timedelta(seconds=2)
    return stream, now, calls, receipts, tick


def assert_partial(stream, rejected):
    part = stream._chain_partitions["MU"]
    assert part["attempted_complete"] is True
    assert part["coverage_complete"] is False and part["complete"] is False
    assert part["missing_expirations"] == [rejected]
    assert len(part["receipts"]) == 2 and len(part["keys"]) == 2
    assert "MU" not in stream._chains and "MU" not in stream._completed_chains
    return part


def test_rejected_middle_expiry_continues_distinct_scopes_and_retains_raw_cause(tmp_path):
    stream, now, calls, receipts, tick = stream_fixture(tmp_path)
    tick(5)  # Full-chain overflow, discovery, first date, rejected date, final date.
    part = assert_partial(stream, DATES[1])
    rejected, = part["rejected_scopes"].values()
    assert rejected["params"]["fromDate"] == rejected["params"]["toDate"] == DATES[1]
    assert rejected["error"]["http_status"] == 400
    assert '"status":"404"' in rejected["error"]["cause"]
    assert rejected["raw_ref"] == {"callback_receipt": "receipt:4"}
    assert receipts[3][1]["kind"] == "option_chain_attempt"
    original_rejection = copy.deepcopy(rejected)
    tick(3)
    assert len(calls) == 5  # No same-round retry or rediscovery of a failed scope.
    now[0] += timedelta(minutes=15)
    tick(4)  # Fresh discovery and the two successful independent dates.
    assert_partial(stream, DATES[1])
    assert list(stream._chain_partitions["MU"]["rejected_scopes"].values()) == [original_rejection]
    assert sum(params.get("fromDate") == DATES[1] for _, params in calls) == 1
    assert [params.get("fromDate") for endpoint, params in calls if endpoint == "chains"] == [
        None, DATES[0], DATES[1], DATES[2], DATES[0], DATES[2]]
    stream.close()


def test_existing_midway_rejection_resumes_without_reissuing_failed_request(tmp_path):
    stream, now, calls, receipts, tick = stream_fixture(tmp_path)
    tick(3)
    part = copy.deepcopy(stream._chain_partitions["MU"])
    # Reproduce the old durable format: rejected symbol ledger and cursor still
    # pointing at the failed date, with no retained error receipt reference.
    stream._chain_retry.run("schwab_chain:MU", lambda: (_ for _ in ()).throw(
        ProviderRequestError(status=400, cause="backend404 Not Found")), lambda event: None,
        max_attempts=1)
    stream._chain_partitions["MU"] = part
    stream._save_partitions()
    stream.close()
    restarted = SchwabMarketStream(tmp_path, ["MU"], clock=lambda: now[0])
    def http(endpoint, params):
        assert endpoint == "chains" and params["fromDate"] == DATES[2]
        calls.append((endpoint, dict(params)))
        return chain(DATES[2])
    restarted._http = http
    assert not restarted._refresh_one_chain()  # Consume recorded rejection, without provider IO.
    assert len(calls) == 3
    now[0] += timedelta(seconds=2)
    assert not restarted._refresh_one_chain()
    part = assert_partial(restarted, DATES[1])
    rejected, = part["rejected_scopes"].values()
    assert rejected["raw_ref"] is None and rejected["error"]["cause"] == "backend404 Not Found"
    assert len(calls) == 4 and len(part["receipts"]) == 2
    restarted.close()


def test_last_rejection_is_incomplete_after_restart_and_future_round(tmp_path):
    stream, now, calls, receipts, tick = stream_fixture(tmp_path, rejected=DATES[-1])
    tick(5)
    part = assert_partial(stream, DATES[-1])
    assert stream._chain_retry.state("schwab_chain:MU")["status"] == "request_rejected"
    assert stream._chain_retry.due_keys() == ()
    original = copy.deepcopy(part["rejected_scopes"])
    stream.close()
    restarted = SchwabMarketStream(tmp_path, ["MU"], clock=lambda: now[0])
    def http(endpoint, params):
        calls.append((endpoint, dict(params)))
        if endpoint == "expirationchain":
            return {"expirationList": [{"expirationDate": date} for date in DATES]}
        assert params["fromDate"] != DATES[-1]
        return chain(params["fromDate"])
    restarted._http = http
    assert not restarted._refresh_one_chain()  # Same completed attempt remains explicitly partial.
    assert len(calls) == 5
    now[0] += timedelta(minutes=15)
    for _ in range(3):
        assert not restarted._refresh_one_chain()
        now[0] += timedelta(seconds=2)
    assert_partial(restarted, DATES[-1])
    assert restarted._chain_partitions["MU"]["rejected_scopes"] == original
    assert sum(params.get("fromDate") == DATES[-1] for _, params in calls) == 1
    restarted.close()


def test_saved_rejection_before_defer_does_not_reject_next_scope(tmp_path, monkeypatch):
    stream, now, calls, receipts, tick = stream_fixture(tmp_path)
    tick(3)
    def interrupted(*args, **kwargs):
        raise RuntimeError("fixture process interruption after durable rejected scope")
    monkeypatch.setattr(stream._chain_retry, "defer", interrupted)
    tick()
    assert stream._chain_partitions["MU"]["index"] == 2
    assert stream._chain_retry.state("schwab_chain:MU")["status"] == "request_rejected"
    stream.close()
    restarted = SchwabMarketStream(tmp_path, ["MU"], clock=lambda: now[0])
    def http(endpoint, params):
        assert endpoint == "chains" and params["fromDate"] == DATES[2]
        calls.append((endpoint, dict(params)))
        return chain(DATES[2])
    restarted._http = http
    assert not restarted._refresh_one_chain()
    now[0] += timedelta(seconds=2)
    assert not restarted._refresh_one_chain()
    part = assert_partial(restarted, DATES[1])
    assert len(part["rejected_scopes"]) == 1
    assert [params.get("fromDate") for endpoint, params in calls] == [None, None, *DATES]
    restarted.close()


def test_incomplete_refresh_preserves_prior_complete_scope_and_exposes_current_evidence(tmp_path):
    stream, now, calls, receipts, tick = stream_fixture(tmp_path, rejected=None)
    tick(5)
    first = stream._chain_partitions["MU"]
    assert first["attempted_complete"] and first["coverage_complete"] and first["complete"]
    original_keys = copy.deepcopy(stream._chains["MU"])
    original_scope = copy.deepcopy(stream._chain_details["MU"])
    real_http = stream._http
    def rejected_http(endpoint, params):
        if params.get("fromDate") == DATES[1]:
            calls.append((endpoint, dict(params)))
            raise ProviderRequestError(status=400, cause="backend404 Not Found")
        return real_http(endpoint, params)
    stream._http = rejected_http
    now[0] += timedelta(minutes=15)
    tick(4)
    part = stream._chain_partitions["MU"]
    assert part["attempted_complete"] and not part["coverage_complete"]
    assert stream._chains["MU"] == stream._completed_chains["MU"] == original_keys
    assert stream._chain_details["MU"] == original_scope
    record = next(row for row in stream.snapshot() if row["source"] == "schwab_stream_options")
    payload = record["payload"]
    assert payload["chain_scope_evidence"] == original_scope
    assert payload["chain_scope_raw_ref"] == original_scope["raw_ref"]
    assert payload["chain_raw_ref"] != payload["chain_scope_raw_ref"]
    current = payload["chain_partition_evidence"]
    assert current["attempted_complete"] and not current["coverage_complete"]
    assert current["missing_expirations"] == [DATES[1]]
    assert current["successful_partition_count"] == 2
    assert current["discovered_at"] > original_scope["received_at"]
    assert current["latest_success_received_at"] > current["discovered_at"]
    rejection, = current["rejected_scopes"]
    assert rejection["received_at"] > current["discovered_at"]
    assert rejection["raw_ref"] and rejection["error"]["cause"] == "backend404 Not Found"
    stream.close()


def test_partial_partition_segment_manifests_survive_repeated_snapshots(tmp_path):
    stream, now, calls, receipts, tick = stream_fixture(tmp_path)
    stream.persist_raw = None  # Exercise actual temporary raw journals and segment retention.
    for _ in range(5):
        tick()
        stream.snapshot()  # Seal separate segments and exercise ordinary manifest pruning.
    part = assert_partial(stream, DATES[1])
    rejection, = part["rejected_scopes"].values()
    expected = {entry["raw_ref"]["segment_id"] for entry in part["receipts"]}
    expected.add(rejection["raw_ref"]["segment_id"])
    assert len(expected) == 3
    for _ in range(2):
        payload = next(row["payload"] for row in stream.snapshot()
                       if row["source"] == "schwab_stream_options")
        assert expected <= {Path(segment["path"]).stem
                            for segment in payload["raw_segments"]}
        assert expected <= set(stream._segments)
    stream.close()
