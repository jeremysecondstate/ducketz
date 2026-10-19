"""Local receipt catalog read/write concurrency; no providers or operating state."""
from contextlib import closing, contextmanager
from datetime import datetime, timedelta, timezone
import json
import multiprocessing
from pathlib import Path
import sqlite3
from types import SimpleNamespace

import pytest

from ml.intraday.market_inputs import MarketInputCollector, MarketInputStore, SOURCE_FIELDS, read_response


NOW = datetime(2026, 10, 19, 17, 0, tzinfo=timezone.utc)


def _store(root):
    store = MarketInputStore(root)
    store.producer_runtime = {"offline_fixture": True}
    return store


def _append(store, offset):
    stamp = NOW + timedelta(seconds=offset)
    return store.append(symbol="AAPL", source="schwab_quote",
        started_at=stamp - timedelta(seconds=1), received_at=stamp,
        payload={"bidPrice": 100 + offset, "askPrice": 101 + offset},
        features={"last": 100 + offset})


def _rows(database):
    with closing(sqlite3.connect(database.as_uri() + "?mode=ro", uri=True)) as db:
        return db.execute("SELECT id,object_path FROM receipts ORDER BY received_at").fetchall()


def _reader(database):
    reader = sqlite3.connect(database.as_uri() + "?mode=ro", uri=True)
    reader.execute("BEGIN")
    assert reader.execute("SELECT count(*) FROM receipts").fetchone() == (1,)
    return reader


def _process_append(root, offset, result, start=None):
    try:
        if start is not None:
            start.wait(timeout=10)
        receipt = _append(_store(root), offset)
        result.put(("appended", receipt["id"]))
    except BaseException as exc:
        result.put((type(exc).__name__, str(exc)))


def test_catalog_writer_commits_while_an_existing_read_snapshot_remains_open(tmp_path):
    store = _store(tmp_path)
    original = _append(store, 0)
    with closing(_reader(store.database)) as reader:
        added = _append(store, 1)
        assert reader.execute("SELECT count(*) FROM receipts").fetchone() == (1,)
        assert [row[0] for row in _rows(store.database)] == [original["id"], added["id"]]
        reader.rollback()
    assert read_response(store.root, original["object_path"])["payload"]["bidPrice"] == 100
    assert read_response(store.root, added["object_path"])["payload"]["bidPrice"] == 101


def test_existing_delete_catalog_migrates_without_rewriting_receipts_or_objects(tmp_path):
    store = _store(tmp_path)
    original = _append(store, 0)
    object_path = Path(original["object_path"])
    original_bytes = object_path.read_bytes()
    original_row = _rows(store.database)[0]
    with closing(sqlite3.connect(store.database)) as db:
        assert db.execute("PRAGMA journal_mode=DELETE").fetchone() == ("delete",)
    replacement_store = _store(tmp_path)
    added = _append(replacement_store, 1)
    with closing(sqlite3.connect(store.database.as_uri() + "?mode=ro", uri=True)) as db:
        assert db.execute("PRAGMA journal_mode").fetchone() == ("wal",)
    assert _rows(store.database) == [original_row, (added["id"], str(Path(added["object_path"]).relative_to(store.root)))]
    assert object_path.read_bytes() == original_bytes


def test_read_only_catalog_queries_do_not_migrate_existing_delete_database(tmp_path):
    store = _store(tmp_path)
    _append(store, 0)
    with closing(sqlite3.connect(store.database)) as db:
        assert db.execute("PRAGMA journal_mode=DELETE").fetchone() == ("delete",)
    observer = _store(tmp_path)
    assert observer.latest_receipt("AAPL", "schwab_quote") == NOW
    assert observer.join("AAPL", [NOW], cutoff=NOW)["provenance"][0]["sources"]["schwab_quote"]["eligible"]
    with closing(sqlite3.connect(store.database.as_uri() + "?mode=ro", uri=True)) as db:
        assert db.execute("PRAGMA journal_mode").fetchone() == ("delete",)


def test_independent_processes_append_during_the_same_read_snapshot(tmp_path):
    store = _store(tmp_path)
    original = _append(store, 0)
    context = multiprocessing.get_context("spawn")
    result = context.Queue()
    processes = [context.Process(target=_process_append, args=(str(tmp_path), offset, result))
                 for offset in (1, 2)]
    with closing(_reader(store.database)) as reader:
        for process in processes:
            process.start()
        try:
            for process in processes:
                process.join(timeout=20)
                assert process.exitcode == 0
            outcomes = [result.get(timeout=5) for _ in processes]
            assert [outcome[0] for outcome in outcomes] == ["appended", "appended"]
            assert reader.execute("SELECT count(*) FROM receipts").fetchone() == (1,)
            assert {row[0] for row in _rows(store.database)} == {original["id"], *(value for _, value in outcomes)}
        finally:
            reader.rollback()
            for process in processes:
                if process.is_alive():
                    process.terminate()
                process.join(timeout=5)
            result.close()


def test_independent_processes_can_initialize_the_same_new_catalog(tmp_path):
    context = multiprocessing.get_context("spawn")
    result = context.Queue()
    start = context.Barrier(2)
    processes = [context.Process(target=_process_append, args=(str(tmp_path), offset, result, start))
                 for offset in (1, 2)]
    for process in processes:
        process.start()
    try:
        for process in processes:
            process.join(timeout=20)
            assert process.exitcode == 0
        outcomes = [result.get(timeout=5) for _ in processes]
        assert [outcome[0] for outcome in outcomes] == ["appended", "appended"]
        assert {row[0] for row in _rows(_store(tmp_path).database)} == {value for _, value in outcomes}
    finally:
        for process in processes:
            if process.is_alive():
                process.terminate()
            process.join(timeout=5)
        result.close()


def test_busy_first_migration_retains_raw_evidence_and_retries_after_reader_finishes(tmp_path, monkeypatch):
    store = _store(tmp_path)
    original = _append(store, 0)
    with closing(sqlite3.connect(store.database)) as db:
        assert db.execute("PRAGMA journal_mode=DELETE").fetchone() == ("delete",)
    original_connect = sqlite3.connect

    def short_connection(database, *args, **kwargs):
        if database == store.database:
            kwargs["timeout"] = .05
        return original_connect(database, *args, **kwargs)

    with closing(_reader(store.database)) as reader:
        monkeypatch.setattr(sqlite3, "connect", short_connection)
        with pytest.raises(sqlite3.OperationalError, match="database is locked") as failure:
            _append(store, 1)
        assert failure.value.sqlite_errorcode == sqlite3.SQLITE_BUSY
        assert _rows(store.database) == [(original["id"], str(Path(original["object_path"]).relative_to(store.root)))]
        preserved = {path: path.read_bytes() for path in store.root.rglob("*.json")
                     if not path.is_relative_to(store.pending_catalog)}
        assert len(list((store.root / "objects").rglob("*.json"))) == 2
        reader.rollback()
    recovered = _append(store, 1)
    assert len(_rows(store.database)) == 2
    assert all(path.read_bytes() == content for path, content in preserved.items())
    assert read_response(store.root, recovered["object_path"])["payload"]["bidPrice"] == 101


def test_journal_retry_uses_remaining_wait_budget_then_restores_writer_timeout(tmp_path, monkeypatch):
    from ml.intraday import market_inputs
    store = _store(tmp_path)
    _append(store, 0)
    original_connect = sqlite3.connect
    timeouts, sleeps, attempts = [], [], []
    clocks = iter((0., 0., 4.9, 4.99))

    class Connection:
        def __init__(self, connection):
            self.connection = connection

        def __getattr__(self, name):
            return getattr(self.connection, name)

        def execute(self, sql, *args):
            if sql.startswith("PRAGMA busy_timeout="):
                timeouts.append(int(sql.split("=")[1]))
            if sql == "PRAGMA journal_mode=WAL":
                attempts.append(sql)
                if len(attempts) == 1:
                    error = sqlite3.OperationalError("database is locked")
                    error.sqlite_errorcode = sqlite3.SQLITE_BUSY
                    raise error
            return self.connection.execute(sql, *args)

    monkeypatch.setattr(sqlite3, "connect", lambda *args, **kwargs: Connection(original_connect(*args, **kwargs)))
    monkeypatch.setattr(market_inputs, "time", SimpleNamespace(monotonic=lambda: next(clocks), sleep=sleeps.append))
    _append(store, 1)
    assert len(attempts) == 2 and sleeps == [.01]
    assert timeouts[0] == timeouts[-1] == 5000
    assert 0 <= timeouts[1] <= 10


def _pending(store, receipt):
    """Synthetic crash after durable intent, before the catalog commit."""
    with closing(sqlite3.connect(store.database)) as db:
        db.execute("DELETE FROM receipts WHERE id=?", (receipt["id"],))
        db.commit()
    marker = store.pending_catalog / (receipt["id"] + ".json")
    marker.write_text(json.dumps({"version": "market-catalog-pending-v1", "id": receipt["id"],
        "object_path": Path(receipt["object_path"]).relative_to(store.root).as_posix()},
        sort_keys=True, separators=(",", ":")), encoding="utf-8")
    return marker


def test_real_busy_response_is_indexed_after_restart_without_refetch_or_backdating(tmp_path, monkeypatch):
    collector = MarketInputCollector(tmp_path, ["AAPL"], clock=lambda: NOW)
    collector.store.producer_runtime = {"offline_fixture": True}
    _append(collector.store, -1)
    with closing(sqlite3.connect(collector.store.database)) as db:
        assert db.execute("PRAGMA journal_mode=DELETE").fetchone() == ("delete",)
    calls = []
    with closing(_reader(collector.store.database)) as reader:
        with pytest.raises(sqlite3.OperationalError, match="database is locked"):
            collector._capture("AAPL", "fmp_quote", {"endpoint": "quote", "params": {"symbol": "AAPL"}},
                lambda: calls.append("fixture") or [{"symbol": "AAPL", "price": 101, "timestamp": NOW.timestamp()}],
                lambda payload, at: {"price": 101})
        markers = list(collector.store.pending_catalog.glob("*.json"))
        assert len(markers) == 1 and collector._retry_ledger.due_keys() == ()
        marker = json.loads(markers[0].read_bytes())
        target = collector.store.root / marker["object_path"]
        envelope = json.loads(target.read_bytes())
        payload = collector.store.root / envelope["payload_ref"]["path"]
        preserved = {target: target.read_bytes(), payload: payload.read_bytes()}
        reader.rollback()
    restarted = MarketInputCollector(tmp_path, ["AAPL"], clock=lambda: NOW + timedelta(hours=1))
    monkeypatch.setattr(restarted, "_rebuild_retry", lambda *args: pytest.fail("Recovery refetched a provider"))
    monkeypatch.setattr(restarted.store, "capture_producer", lambda: pytest.fail("Recovery replaced producer provenance"))
    recovered = restarted.recover_pending()
    assert recovered["errors"] == [] and recovered["catalog_recovery"] == {"attempted": 1, "indexed": 1}
    assert recovered["receipts"][0]["id"] == target.stem
    assert recovered["receipts"][0]["received_at"] == NOW.isoformat()
    assert recovered["receipts"][0]["producer_runtime"] == envelope["producer_runtime"]
    assert not markers[0].exists() and calls == ["fixture"]
    assert all(path.read_bytes() == raw for path, raw in preserved.items())
    joined = restarted.store.join("AAPL", [NOW - timedelta(microseconds=1), NOW], cutoff=NOW)
    assert not joined["provenance"][0]["sources"].get("fmp_quote", {}).get("eligible", False)
    assert joined["provenance"][1]["sources"]["fmp_quote"]["eligible"] is True
    assert restarted.recover_pending() is None
    assert len(_rows(restarted.store.database)) == 2


@pytest.mark.parametrize("source", ["db_equity", "schwab_stream_options", "fmp_5min"])
def test_retained_stream_and_rest_receipts_use_same_durable_index_path(tmp_path, source):
    store = _store(tmp_path)
    receipt = store.append(symbol="AAPL", source=source, started_at=NOW, received_at=NOW,
        payload={"original": source}, features={SOURCE_FIELDS[source][0]: 1})
    marker = _pending(store, receipt)
    retained = Path(receipt["object_path"]).read_bytes()
    observer = _store(tmp_path)
    assert observer.latest_receipt("AAPL", source) is None
    assert not observer.join("AAPL", [NOW], cutoff=NOW)["provenance"][0]["sources"].get(source)
    assert marker.exists()
    result = observer.recover_pending_receipts()
    assert result["errors"] == [] and [r["id"] for r in result["receipts"]] == [receipt["id"]]
    assert Path(receipt["object_path"]).read_bytes() == retained and not marker.exists()


def test_exact_index_accepts_equivalent_json_but_retains_intent_for_conflicting_row(tmp_path):
    store = _store(tmp_path)
    receipt = _append(store, 0)
    with closing(sqlite3.connect(store.database)) as db:
        equivalent = json.dumps(dict(reversed(list(receipt["features"].items()))), indent=2)
        db.execute("UPDATE receipts SET features=? WHERE id=?", (equivalent, receipt["id"]))
        db.commit()
    store.index_retained_receipt(receipt["object_path"])
    with closing(sqlite3.connect(store.database)) as db:
        assert db.execute("SELECT features FROM receipts").fetchone()[0] == equivalent
        conflict = {**receipt["features"], "last": 999}
        db.execute("UPDATE receipts SET features=? WHERE id=?", (json.dumps(conflict), receipt["id"]))
        db.commit()
    with pytest.raises(ValueError, match="catalog row differs"):
        store.index_retained_receipt(receipt["object_path"])
    assert (store.pending_catalog / (receipt["id"] + ".json")).exists()
    with closing(sqlite3.connect(store.database)) as db:
        assert json.loads(db.execute("SELECT features FROM receipts").fetchone()[0]) == conflict


def test_corrupt_high_marker_cannot_starve_lower_receipts_with_unordered_single_item_passes(tmp_path, monkeypatch):
    from ml.intraday import market_inputs
    store = _store(tmp_path)
    receipts = [_append(store, offset) for offset in (0, 1)]
    markers = [_pending(store, receipt) for receipt in receipts]
    corrupt = store.pending_catalog / ("f" * 64 + ".json")
    corrupt.write_text("{broken", encoding="utf-8")
    original_scandir = market_inputs.os.scandir

    @contextmanager
    def unordered(path):
        with original_scandir(path) as entries:
            yield iter(sorted(entries, key=lambda entry: entry.name, reverse=True))

    monkeypatch.setattr(market_inputs.os, "scandir", unordered)
    store._catalog_recovery_cursor = max(marker.name for marker in markers)
    failed = store.recover_pending_receipts(max_receipts=1)
    assert failed["attempted"] == 1 and failed["errors"][0]["error_type"] == "JSONDecodeError"
    successful = [store.recover_pending_receipts(max_receipts=1) for _ in markers]
    assert {row["id"] for result in successful for row in result["receipts"]} == {r["id"] for r in receipts}
    assert all(result["attempted"] == 1 and result["errors"] == [] for result in successful)
    assert corrupt.read_text() == "{broken" and not any(marker.exists() for marker in markers)


@pytest.mark.parametrize("damage", ["envelope", "payload", "outside_path", "marker_identity"])
def test_invalid_pending_evidence_is_retained_with_explicit_error(tmp_path, damage):
    store = _store(tmp_path)
    receipt = _append(store, 0)
    marker = _pending(store, receipt)
    target = Path(receipt["object_path"])
    if damage == "envelope":
        target.write_bytes(target.read_bytes() + b" ")
    elif damage == "payload":
        payload = store.root / receipt["payload_ref"]["path"]
        payload.write_bytes(b"x" * payload.stat().st_size)
    else:
        intent = json.loads(marker.read_bytes())
        if damage == "outside_path":
            outside = tmp_path / target.name
            outside.write_bytes(target.read_bytes())
            intent["object_path"] = str(outside)
        else:
            intent["id"] = "0" * 64
        marker.write_text(json.dumps(intent), encoding="utf-8")
    before = marker.read_bytes()
    result = store.recover_pending_receipts()
    assert result["receipts"] == [] and len(result["errors"]) == 1
    assert result["errors"][0]["error_type"] == "ValueError"
    assert marker.read_bytes() == before and _rows(store.database) == []


def _process_recover(root, result, start):
    try:
        start.wait(timeout=10)
        recovered = _store(root).recover_pending_receipts()
        result.put({"ids": [receipt["id"] for receipt in recovered["receipts"]], "errors": recovered["errors"]})
    except BaseException as exc:
        result.put({"failure": type(exc).__name__, "message": str(exc)})


def test_independent_processes_can_complete_the_same_pending_intent_idempotently(tmp_path):
    store = _store(tmp_path)
    receipt = _append(store, 0)
    marker = _pending(store, receipt)
    raw = Path(receipt["object_path"]).read_bytes()
    context = multiprocessing.get_context("spawn")
    result, start = context.Queue(), context.Barrier(2)
    processes = [context.Process(target=_process_recover, args=(str(tmp_path), result, start)) for _ in range(2)]
    for process in processes:
        process.start()
    try:
        for process in processes:
            process.join(timeout=20)
            assert process.exitcode == 0
        outcomes = [result.get(timeout=5) for _ in processes]
        assert all(outcome.get("errors") == [] for outcome in outcomes)
        assert {identity for outcome in outcomes for identity in outcome["ids"]} == {receipt["id"]}
        assert len(_rows(store.database)) == 1 and not marker.exists()
        assert Path(receipt["object_path"]).read_bytes() == raw
    finally:
        for process in processes:
            if process.is_alive():
                process.terminate()
            process.join(timeout=5)
        result.close()


@pytest.mark.parametrize("failed_symbol", ["AAPL", "AMZN"])
def test_wildcard_batch_intent_survives_child_failure_and_restart_without_refetch(tmp_path, monkeypatch, failed_symbol):
    symbols = ["AAPL", "AMZN", "COST"]
    collector = MarketInputCollector(tmp_path, symbols, clock=lambda: NOW)
    collector.store.producer_runtime = {"offline_original_producer": True}
    payload = {symbol: {"symbol": symbol, "realtime": True, "quote": {
        "symbol": symbol, "bidPrice": 100 + index, "askPrice": 101 + index,
        "lastPrice": 100.5 + index, "quoteTime": NOW.timestamp() * 1000,
        "tradeTime": NOW.timestamp() * 1000}} for index, symbol in enumerate(symbols)}
    batch = collector.store.append(symbol="*", source="schwab_quote", started_at=NOW, received_at=NOW,
        payload=payload, features={}, status="empty", request={"endpoint": "quotes", "symbols": symbols})
    parent_marker = collector.store.pending_catalog / (batch["id"] + ".json")
    assert parent_marker.exists()
    original = {Path(batch["object_path"]): Path(batch["object_path"]).read_bytes(),
                collector.store.root / batch["payload_ref"]["path"]:
                    (collector.store.root / batch["payload_ref"]["path"]).read_bytes()}
    original_connect = sqlite3.connect
    failed = []

    class FailChildInsert:
        def __init__(self, connection):
            self.connection = connection

        def __getattr__(self, name):
            return getattr(self.connection, name)

        def execute(self, sql, parameters=()):
            if sql.startswith("INSERT OR IGNORE INTO receipts") and parameters[1] == failed_symbol and not failed:
                failed.append(True)
                raise sqlite3.OperationalError("fixture child catalog is locked")
            return self.connection.execute(sql, parameters)

    with monkeypatch.context() as patch:
        patch.setattr(sqlite3, "connect", lambda *args, **kwargs: FailChildInsert(original_connect(*args, **kwargs)))
        first = collector.recover_pending()
    assert first["errors"][0]["error_type"] == "OperationalError" and parent_marker.exists()
    assert len(list(collector.store.pending_catalog.glob("*.json"))) == 2
    child_bytes = {path: path.read_bytes() for path in (collector.store.root / "objects").rglob("*.json")}
    restarted = MarketInputCollector(tmp_path, symbols, clock=lambda: NOW + timedelta(hours=1))
    monkeypatch.setattr(restarted, "_rebuild_retry", lambda *args: pytest.fail("Recovery refetched quote batch"))
    monkeypatch.setattr(restarted.store, "capture_producer", lambda: pytest.fail("Recovery changed original producer"))
    result = restarted.recover_pending()
    assert result["errors"] == [] and not list(restarted.store.pending_catalog.glob("*.json"))
    assert all(path.read_bytes() == raw for path, raw in {**original, **child_bytes}.items())
    assert len(_rows(restarted.store.database)) == len(symbols) + 1
    for index, symbol in enumerate(symbols):
        joined = restarted.store.join(symbol, [NOW - timedelta(microseconds=1), NOW], cutoff=NOW)
        assert "schwab_quote" not in joined["provenance"][0]["sources"]
        source = joined["provenance"][1]["sources"]["schwab_quote"]
        assert source["eligible"] and source["received_at"] == NOW.isoformat()
        receipt = read_response(restarted.store.root, source["object_path"])
        assert receipt["features"]["last"] == 100.5 + index
        assert receipt["producer_runtime"] == batch["producer_runtime"]
        assert receipt["request"]["batch_receipt_id"] == batch["id"]
    assert restarted.recover_pending() is None


def test_global_context_projection_reuses_original_receipt_clock_and_producer(tmp_path, monkeypatch):
    collector = MarketInputCollector(tmp_path, ["AAPL", "AMZN"], clock=lambda: NOW)
    collector.store.producer_runtime = {"original_macro_fixture": True}
    source = "fmp_macro_gdp"
    batch = collector.store.append(symbol="*", source=source, started_at=NOW, received_at=NOW,
        payload={"observed": 2}, features={SOURCE_FIELDS[source][0]: 2}, request={"endpoint": "economic-indicators"})
    original = Path(batch["object_path"]).read_bytes()
    restarted = MarketInputCollector(tmp_path, ["AAPL", "AMZN"], clock=lambda: NOW + timedelta(hours=1))
    monkeypatch.setattr(restarted, "_rebuild_retry", lambda *args: pytest.fail("Recovery refetched global context"))
    monkeypatch.setattr(restarted.store, "capture_producer", lambda: pytest.fail("Recovery changed producer"))
    result = restarted.recover_pending()
    assert result["errors"] == [] and len(_rows(restarted.store.database)) == 3
    assert not list(restarted.store.pending_catalog.glob("*.json"))
    for symbol in restarted.symbols:
        joined = restarted.store.join(symbol, [NOW], cutoff=NOW)
        used = joined["provenance"][0]["sources"][source]
        receipt = read_response(restarted.store.root, used["object_path"])
        assert used["eligible"] and receipt["received_at"] == batch["received_at"]
        assert receipt["producer_runtime"] == batch["producer_runtime"]
        assert receipt["request"]["global_receipt_id"] == batch["id"]
    assert restarted.recover_pending() is None and Path(batch["object_path"]).read_bytes() == original
