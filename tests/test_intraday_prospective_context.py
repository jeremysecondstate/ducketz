from dataclasses import asdict
from pathlib import Path
import hashlib
import json

import pytest

from ml.intraday import prospective_context as worker
from ml.intraday.market_collection import (CollectionResult, QuarterHourMarketCollector,
    SourceRequest, QUEUE_VERSION, configuration_digest)
from ml.intraday.market_collection_schwab_stream import stream_requests


def fixture(tmp_path):
    source = Path(worker.__file__).resolve().parents[2]
    requests = [*stream_requests(["TEST"]),
        SourceRequest("schwab", "stock_quote", "TEST", "marketdata-v1", "quote", "quotes"),
        SourceRequest("schwab", "options", "TEST", "marketdata-v1", "chain-greeks-full", "chains"),
        SourceRequest("fmp", "cross_asset", "*", "stable", "commodity-quote", "quote", {"symbol": "BZUSD"}),
        SourceRequest("fmp", "cross_asset", "*", "stable", "commodity-quote", "quote", {"symbol": "CLUSD"}),
        SourceRequest("fmp", "cross_asset", "*", "stable", "commodity-quote", "quote", {"symbol": "NGUSD"})]
    config = {"schema_version": QUEUE_VERSION, "pc": "scout", "symbols": ["TEST"],
        "requests": [asdict(r) for r in requests], "source_ids": [r.source_id for r in requests]}
    config["configuration_sha256"] = configuration_digest(config)
    path = tmp_path / "intraday/scout/market-data/configuration.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps(config))
    binding = {"schema_version": worker.VERSION, "datastore": str(tmp_path), "pc": "scout",
        "symbols": ["TEST"], "source_root": str(source), "source_sha256": {
            "ml/intraday/" + name + ".py": worker._sha(source / "ml/intraday" / (name + ".py"))
            for name in worker.REQUIRED_SOURCES}, "configuration_sha256": config["configuration_sha256"],
        "configuration_file_sha256": worker._sha(path), "state_root": str(tmp_path / "context-worker"),
        "start_session": "2026-10-19", "poll_seconds": 30}
    return binding, config, requests


def counting_factory(calls):
    def adapter(request, start, end, maximum):
        calls.append(request.identity)
        return CollectionResult([], "COMPLETE", "Fixture empty response")
    def factory(*args, **kwargs):
        return QuarterHourMarketCollector(*args, **kwargs, adapters={"schwab": adapter, "fmp": adapter})
    return factory


def test_closed_session_never_constructs_provider_or_collects(tmp_path):
    binding, _, _ = fixture(tmp_path)
    def forbidden(*args, **kwargs):
        raise AssertionError("Closed market constructed a provider collector")
    host = worker.ContextWorker(binding, clock=lambda: "2026-10-18T16:00Z", collector_factory=forbidden)
    result = host.tick()
    assert result["status"] == "CLOSED_OR_NO_DUE_SLOT"
    assert result["provider_calls_this_tick"] == 0
    assert not (tmp_path / "intraday/scout/market-data/queue.json").exists()
    assert (host.root / "health.json").exists()


def test_exact_normal_subset_keeps_denied_commodity_identity_out(tmp_path):
    _, config, _ = fixture(tmp_path)
    selected = worker.normal_requests(config)
    assert len(selected) == 9
    assert [r.params["symbol"] for r in selected if r.provider == "fmp"] == ["BZUSD"]
    assert set(r.source_id for r in selected).issubset(config["source_ids"])
    config["requests"] = [r for r in config["requests"] if r["schema"] != "OPTIONS_BOOK"]
    with pytest.raises(ValueError, match="lacks exact"):
        worker.normal_requests(config)


def test_collects_once_then_restart_reuses_immutable_outcome_and_exact_blocks(tmp_path):
    binding, config, requests = fixture(tmp_path)
    calls = []
    factory = counting_factory(calls)
    collector = factory(tmp_path / "intraday/scout/market-data", symbols=["TEST"], requests=requests,
        clock=lambda: "2026-10-19T13:30:01Z", configuration_sha256=config["configuration_sha256"])
    with collector.lock:
        state = collector._state()
        state["source_blocks"] = {r.identity: {"reason": "Exact prior HTTP 402"} for r in requests
            if r.params.get("symbol") in {"CLUSD", "NGUSD"}}
        worker._atomic(collector.state_path, state)
    blocked = state["source_blocks"]
    host = worker.ContextWorker(binding, clock=lambda: "2026-10-19T13:30:01Z", collector_factory=factory)
    result = host.tick()
    assert result["requests"] == 9 and len(calls) == 9
    assert result["status"] == "LIMITED"  # empty response is no instrument baseline
    assert all(r["records"] == 0 and r["status"] == "EMPTY" for r in result["results"])
    path = next((host.root / "outcomes").glob("*.json"))
    before = path.read_bytes()
    restarted = worker.ContextWorker(binding, clock=lambda: "2026-10-19T13:30:31Z", collector_factory=factory)
    restarted.tick()
    assert len(calls) == 9 and path.read_bytes() == before
    assert collector._state()["source_blocks"] == blocked


def test_orphan_receipts_recover_without_repeat_provider_calls(tmp_path, monkeypatch):
    binding, _, _ = fixture(tmp_path)
    calls = []
    original = worker._save_new
    def interrupt(path, value):
        if path.parent.name == "outcomes":
            raise OSError("Fixture interruption after immutable receipts")
        original(path, value)
    monkeypatch.setattr(worker, "_save_new", interrupt)
    host = worker.ContextWorker(binding, clock=lambda: "2026-10-19T13:30:01Z", collector_factory=counting_factory(calls))
    with pytest.raises(OSError):
        host.tick()
    assert len(calls) == 9
    monkeypatch.setattr(worker, "_save_new", original)
    restarted = worker.ContextWorker(binding, clock=lambda: "2026-10-19T13:30:31Z", collector_factory=counting_factory(calls))
    result = restarted.tick()
    assert len(calls) == 9
    assert all(r["recovered"] for r in result["results"])


@pytest.mark.parametrize("restart_at", ["2026-10-19T13:30:31Z", "2026-10-19T13:36Z"])
def test_crash_before_index_queue_commit_repairs_projections_and_denial(tmp_path, monkeypatch, restart_at):
    import sqlite3
    from ml.intraday import market_collection_index
    binding, config, _ = fixture(tmp_path)
    calls = []
    index = market_collection_index.index_receipt
    def interrupted_index(*args, **kwargs):
        raise OSError("Crash after snapshot before receipt index")
    monkeypatch.setattr(market_collection_index, "index_receipt", interrupted_index)
    def denied(request, *args):
        calls.append(request.identity)
        return CollectionResult([], "NOT_AUTHORIZED", "Fixture exact denial")
    def factory(*args, **kwargs):
        return QuarterHourMarketCollector(*args, **kwargs, adapters={"schwab": denied, "fmp": denied})
    host = worker.ContextWorker(binding, clock=lambda: "2026-10-19T13:30:01Z", collector_factory=factory)
    original = worker._save_new
    def interrupt_outcome(path, value):
        if path.parent.name == "outcomes":
            raise OSError("Crash before final outcome")
        original(path, value)
    monkeypatch.setattr(worker, "_save_new", interrupt_outcome)
    with pytest.raises(OSError):
        host.tick()
    assert len(calls) == 9
    monkeypatch.setattr(market_collection_index, "index_receipt", index)
    monkeypatch.setattr(worker, "_save_new", original)
    recovered = worker.ContextWorker(binding, clock=lambda: restart_at, collector_factory=factory)
    result = recovered.tick()
    if "results" not in result:
        outcome = worker._read(recovered.root / "outcomes/20261019T133000Z.json")
        results = outcome["recovered_results"]
        assert outcome["status"] == "MISSED" and result["provider_calls_this_tick"] == 0
    else:
        results = result["results"]
    assert all(r["status"] == "NOT_AUTHORIZED" and r["recovered"] for r in results)
    assert len(calls) == 9
    root = tmp_path / "intraday/scout/market-data"
    state = worker._read(root / "queue.json")
    assert len(state["source_blocks"]) == 9
    assert all(task["attempts"] == 1 and len(task["snapshots"]) == 1 for task in state["tasks"].values())
    with sqlite3.connect(root / "receipts.sqlite3") as db:
        assert db.execute("SELECT count(*) FROM receipts").fetchone()[0] == 9


def test_missed_and_interrupted_slots_never_trigger_late_acquisition(tmp_path):
    binding, _, requests = fixture(tmp_path)
    calls = []
    host = worker.ContextWorker(binding, clock=lambda: "2026-10-19T13:36Z", collector_factory=counting_factory(calls))
    attempt = host.root / "attempts/20261019T133000Z"
    attempt.mkdir(parents=True)
    (attempt / "prior.json").write_text("{}")
    result = host.tick()
    assert not calls and result["provider_calls_this_tick"] == 0
    outcome = worker._read(host.root / "outcomes/20261019T133000Z.json")
    assert outcome["status"] == "MISSED"
    assert outcome["reason"] == "interrupted_collection_no_completed_slot_outcome"
    assert outcome["historical_backfill"] is False


def test_source_or_configuration_mutation_blocks_before_provider_calls(tmp_path):
    binding, _, _ = fixture(tmp_path)
    calls = []
    binding["source_sha256"]["ml/intraday/market_state.py"] = "0" * 64
    host = worker.ContextWorker(binding, clock=lambda: "2026-10-19T13:30Z", collector_factory=counting_factory(calls))
    with pytest.raises(ValueError, match="source binding changed"):
        host.tick()
    assert not calls


def test_outcome_tampering_fails_closed_without_reacquiring(tmp_path):
    binding, _, _ = fixture(tmp_path)
    calls = []
    host = worker.ContextWorker(binding, clock=lambda: "2026-10-19T13:30:01Z", collector_factory=counting_factory(calls))
    host.tick()
    path = next((host.root / "outcomes").glob("*.json"))
    saved = worker._read(path)
    saved["status"] = "COMPLETE"
    path.write_text(json.dumps(saved))
    with pytest.raises(ValueError, match="checksum changed"):
        host.tick()
    assert len(calls) == 9


def test_interrupted_claim_without_receipt_is_missing_not_retried(tmp_path):
    binding, config, _ = fixture(tmp_path)
    calls = []
    host = worker.ContextWorker(binding, clock=lambda: "2026-10-19T13:30:01Z", collector_factory=counting_factory(calls))
    request = worker.normal_requests(config)[0]
    marker = host.root / "attempts/20261019T133000Z" / (request.identity + ".json")
    worker._save_new(marker, {"status": "STARTED"})
    result = host.tick()
    assert len(calls) == 8
    assert next(r for r in result["results"] if r["request_identity"] == request.identity)["status"] == "INTERRUPTED_NO_RECEIPT"


def test_all_binding_versions_share_one_worker_ownership_lock(tmp_path):
    from filelock import FileLock
    binding, _, _ = fixture(tmp_path)
    host = worker.ContextWorker(binding, clock=lambda: "2026-10-19T13:30:01Z")
    with FileLock(str(host.owner_lock), timeout=0):
        changed = worker.ContextWorker({**binding, "poll_seconds": 10})
        assert changed.tick()["status"] == "BUSY"


def test_holiday_and_short_session_schedule_has_no_outside_rth_slots():
    from datetime import date
    assert worker.regular_slots(date(2026, 12, 25), date(2026, 12, 25)) == []
    slots = worker.regular_slots(date(2026, 11, 27), date(2026, 11, 27))
    assert len(slots) == 14
    assert slots[0].isoformat() == "2026-11-27T14:30:00+00:00"
    assert slots[-1].isoformat() == "2026-11-27T17:45:00+00:00"


def test_binding_rejects_wrong_owner_poll_bounds_or_missing_source(tmp_path):
    binding, _, _ = fixture(tmp_path)
    path = tmp_path / "binding.json"
    for change in ({"pc": "atlas"}, {"poll_seconds": 31}, {"source_sha256": {}}):
        path.write_text(json.dumps({**binding, **change}))
        with pytest.raises(ValueError):
            worker.load_binding(path)
