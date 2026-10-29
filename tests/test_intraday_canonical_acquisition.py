"""Offline proof that daytime and nightly readers share canonical source bars."""

from tests.intraday_shared_storage_support import require_composed_storage
require_composed_storage()
from contextlib import closing
from dataclasses import replace
import hashlib
import json
import sqlite3
from types import SimpleNamespace

import pandas as pd
import pytest

from app.models.market_data import MarketBar
from app.services.market_fetch_specs import databento_analysis_source_specs
from datafetching.parquet_store import ParquetStore
from datafetching.databento_fetch import _persist_native_results
from ml.intraday import canonical_acquisition as subject
from ml.intraday.data import AcquisitionJournal, IntervalResponse, _native_response_snapshot, load_local_history
from ml.intraday.coverage import POPULATION_CONTRACT
from ml.intraday.prediction import ContractError


def attempt(identity="first", received="2026-10-29T14:16:00Z", close=10.5, opened="2026-10-29T14:00:00Z"):
    stamps = pd.date_range(opened, periods=15, freq="min")
    raw = pd.DataFrame(dict(ts_event=stamps, rtype=33, publisher_id=95,
        instrument_id=1234, open=10.0, high=11.0, low=9.0, close=close, volume=3,
        symbol="AAPL"))
    rows = tuple(dict(opened_at=row.ts_event.isoformat(), **{key: getattr(row, key) for key in subject.FIELDS})
                 for row in raw.itertuples())
    return IntervalResponse(identity, "AAPL", subject.SOURCE, "EQUS.MINI", stamps[0].isoformat(),
        (stamps[0] + pd.Timedelta(minutes=15)).isoformat(), received, received,
        "success", rows, stamps[0].isoformat(), (stamps[0] + pd.Timedelta(minutes=15)).isoformat(),
        native_response=_native_response_snapshot(raw))


def setup_journal(root, monkeypatch):
    import ml.market_provenance
    monkeypatch.setattr(ml.market_provenance, "capture_runtime", lambda *args, **kwargs: {"offline": True})
    return AcquisitionJournal(root / "intraday/system-1a/acquisition.sqlite3", reference_writes=True)


def canonical_path(root, raw=False):
    part, suffix = ("raw", "_raw") if raw else ("normalized", "")
    return root / f"stocks/AAPL/bars/1m/databento/{part}/AAPL_{subject.REQUEST}{suffix}.parquet"


def native_result(response):
    spec = next(s for s in databento_analysis_source_specs() if s.schema == "ohlcv-1m")
    bars = [MarketBar("AAPL", "databento", "1m", pd.Timestamp(row["opened_at"]).to_pydatetime(),
                     **{k: row[k] for k in subject.FIELDS}) for row in response.rows]
    return (spec, bars, subject._native_frame(response),
            SimpleNamespace(start=pd.Timestamp(response.interval_open), end=pd.Timestamp(response.interval_close)), None)


def test_original_responses_reused_once_and_actual_clock_survives_loader(tmp_path, monkeypatch):
    journal = setup_journal(tmp_path, monkeypatch)
    journal.save(attempt())
    journal.save(attempt("repeat", "2026-10-29T14:17:00Z"))
    store = ParquetStore(tmp_path)
    result = subject.ingest_saved_attempts(store, ["AAPL"], batch_size=1)
    assert result == dict(attempts=2, minute_rows=30, legacy_conflicts_preserved=0)
    assert len(pd.read_parquet(canonical_path(tmp_path))) == 15
    raw = pd.read_parquet(canonical_path(tmp_path, raw=True))
    assert len(raw) == 15 and set(raw.instrument_id) == {1234}
    assert set(raw.provider_dataset) == {"EQUS.MINI"}
    files = [canonical_path(tmp_path), canonical_path(tmp_path, raw=True)]
    before = [path.read_bytes() for path in files]
    assert subject.ingest_saved_attempts(store, ["AAPL"])["attempts"] == 0
    assert [path.read_bytes() for path in files] == before
    history = load_local_history(tmp_path, "AAPL", population_contract=POPULATION_CONTRACT)
    assert len(history.frame) == 1
    assert history.frame.available_at.iloc[0] == pd.Timestamp("2026-10-29T14:16:00Z")
    assert history.frame.availability_kind.iloc[0] == "actual"
    assert history.manifest["actual_receipt_minute_rows"] == 15
    assert len(list((tmp_path / "intraday/source-objects/responses/payloads").glob("*.json"))) == 1


def test_revisions_and_return_to_old_value_keep_new_receipt_time(tmp_path, monkeypatch):
    journal = setup_journal(tmp_path, monkeypatch)
    store = ParquetStore(tmp_path)
    for identity, received, close in [("a", "14:16", 10.5), ("b", "14:18", 10.75), ("a2", "14:20", 10.5)]:
        journal.save(attempt(identity, f"2026-10-29T{received}:00Z", close))
        subject.ingest_saved_attempts(store, ["AAPL"])
    history = load_local_history(tmp_path, "AAPL", population_contract=POPULATION_CONTRACT)
    assert history.frame.close.iloc[0] == 10.5
    assert history.frame.available_at.iloc[0] == pd.Timestamp("2026-10-29T14:20:00Z")
    journal.save(attempt("late_old", "2026-10-29T14:17:00Z", 10.25))
    subject.ingest_saved_attempts(store, ["AAPL"])
    assert pd.read_parquet(canonical_path(tmp_path)).close.eq(10.5).all()


def test_unknown_legacy_revision_is_preserved_and_wrong_provider_ignored(tmp_path, monkeypatch):
    store = ParquetStore(tmp_path)
    original = attempt()
    _persist_native_results("AAPL", store, provider=SimpleNamespace(dataset="EQUS.MINI"),
        profile="fixture", observed_at=pd.Timestamp(original.request_ended_at).to_pydatetime(),
        native_results=[native_result(original)], run_derived=False)
    journal = setup_journal(tmp_path, monkeypatch)
    journal.save(attempt("conflict", close=10.75))
    journal.save(replace(attempt("other"), source_id="databento:XNAS.ITCH:ohlcv-1m", source_dataset="XNAS.ITCH"))
    before = canonical_path(tmp_path).read_bytes()
    result = subject.ingest_saved_attempts(store, ["AAPL"])
    assert result["legacy_conflicts_preserved"] == 15 and result["minute_rows"] == 0
    assert canonical_path(tmp_path).read_bytes() == before
    history = load_local_history(tmp_path, "AAPL", population_contract=POPULATION_CONTRACT)
    assert history.manifest["actual_receipt_minute_rows"] == 0
    assert history.frame.availability_kind.iloc[0] == "assumed_historical_close"


def test_raw_projection_mismatch_does_not_advance_cursor(tmp_path, monkeypatch):
    journal = setup_journal(tmp_path, monkeypatch)
    response = attempt()
    bad = replace(response, rows=({**response.rows[0], "close": 10.75}, *response.rows[1:]))
    journal.save(bad)
    with pytest.raises(ContractError, match="native values"):
        subject.ingest_saved_attempts(ParquetStore(tmp_path), ["AAPL"])
    assert not canonical_path(tmp_path).exists()
    with sqlite3.connect(tmp_path / subject.INDEX) as db:
        assert db.execute("SELECT count(*) FROM journal_cursors").fetchone()[0] == 0


def test_duplicate_projection_cannot_omit_a_native_minute():
    response = attempt()
    duplicate = replace(response, rows=(response.rows[1], *response.rows[1:]))
    with pytest.raises(ContractError, match="timestamp identities"):
        subject._native_frame(duplicate)


def test_original_response_identity_cannot_be_relabelled_by_journal_slot(tmp_path, monkeypatch):
    from dataclasses import asdict
    journal = setup_journal(tmp_path, monkeypatch)
    response = attempt()
    wrong = replace(response, symbol="MSFT")
    slot = journal.slot(response.symbol, response.source_id, response.interval_open, response.interval_close)
    with sqlite3.connect(journal.path) as db:
        db.execute("INSERT INTO attempts VALUES(?,?,?)", (wrong.attempt_id, slot, json.dumps(asdict(wrong))))
    with pytest.raises(ContractError, match="identity differs"):
        subject.ingest_saved_attempts(ParquetStore(tmp_path), ["AAPL"])
    assert not canonical_path(tmp_path).exists()
    with sqlite3.connect(tmp_path / subject.INDEX) as db:
        assert db.execute("SELECT count(*) FROM journal_cursors").fetchone()[0] == 0


def test_interrupted_upsert_is_retryable_without_backdating(tmp_path, monkeypatch):
    import datafetching.databento_fetch as fetch
    journal = setup_journal(tmp_path, monkeypatch)
    store = ParquetStore(tmp_path)
    journal.save(attempt())
    subject.ingest_saved_attempts(store, ["AAPL"])
    journal.save(attempt("revised", "2026-10-29T14:18:00Z", 10.75))
    real = fetch._persist_native_results
    def fail(*args, **kwargs):
        raise OSError("fixture interrupted upsert")
    monkeypatch.setattr(fetch, "_persist_native_results", fail)
    with pytest.raises(OSError, match="interrupted"):
        subject.ingest_saved_attempts(store, ["AAPL"])
    with pytest.raises(ContractError, match="latest indexed receipt"):
        load_local_history(tmp_path, "AAPL")
    monkeypatch.setattr(fetch, "_persist_native_results", real)
    subject.ingest_saved_attempts(store, ["AAPL"])
    history = load_local_history(tmp_path, "AAPL", population_contract=POPULATION_CONTRACT)
    assert history.frame.close.iloc[0] == 10.75
    assert history.frame.available_at.iloc[0] == pd.Timestamp("2026-10-29T14:18:00Z")


def test_persistence_error_result_or_missing_files_do_not_advance_cursor(tmp_path, monkeypatch):
    import datafetching.databento_fetch as fetch
    journal = setup_journal(tmp_path, monkeypatch)
    journal.save(attempt())
    real = fetch._persist_native_results
    monkeypatch.setattr(fetch, "_persist_native_results", lambda *args, **kwargs:
                        SimpleNamespace(error_files=1, data_files=0))
    with pytest.raises(ContractError, match="persistence returned errors"):
        subject.ingest_saved_attempts(ParquetStore(tmp_path), ["AAPL"])
    with sqlite3.connect(tmp_path / subject.INDEX) as db:
        assert db.execute("SELECT count(*) FROM journal_cursors").fetchone()[0] == 0
    monkeypatch.setattr(fetch, "_persist_native_results", real)
    assert subject.ingest_saved_attempts(ParquetStore(tmp_path), ["AAPL"])["minute_rows"] == 15


def test_nightly_receipt_uses_same_index_and_preserves_actual_time(tmp_path):
    response = attempt(received="2026-10-29T18:00:00Z")
    native = [native_result(response)]
    subject.record_native_refresh(tmp_path, "AAPL", native,
        started_at="2026-10-29T17:59:00Z", received_at=response.request_ended_at)
    _persist_native_results("AAPL", ParquetStore(tmp_path), provider=SimpleNamespace(dataset="EQUS.MINI"),
        profile="continuation", observed_at=pd.Timestamp(response.request_ended_at).to_pydatetime(),
        native_results=native, run_derived=False)
    history = load_local_history(tmp_path, "AAPL", population_contract=POPULATION_CONTRACT)
    assert history.frame.available_at.iloc[0] == pd.Timestamp(response.request_ended_at)
    assert not history.frame.available_at.le(pd.Timestamp("2026-10-29T17:59:00Z")).any()
    with sqlite3.connect(tmp_path / subject.INDEX) as db:
        db.execute("UPDATE revisions SET available_ns=available_ns-1")
    with pytest.raises(ContractError, match="immutable original"):
        load_local_history(tmp_path, "AAPL")


def test_older_nightly_clock_cannot_overwrite_newer_canonical_revision(tmp_path):
    response = attempt(received="2026-10-29T18:00:00Z")
    native = [native_result(response)]
    subject.record_native_refresh(tmp_path, "AAPL", native,
        started_at="2026-10-29T17:59:00Z", received_at=response.request_ended_at)
    _persist_native_results("AAPL", ParquetStore(tmp_path), provider=SimpleNamespace(dataset="EQUS.MINI"),
        profile="continuation", observed_at=pd.Timestamp(response.request_ended_at).to_pydatetime(),
        native_results=native, run_derived=False)
    before = canonical_path(tmp_path).read_bytes()
    stale = attempt("stale", "2026-10-29T17:59:00Z", close=10.75)
    with pytest.raises(ContractError, match="precedes the recorded"):
        subject.record_native_refresh(tmp_path, "AAPL", [native_result(stale)],
            started_at="2026-10-29T17:58:00Z", received_at=stale.request_ended_at)
    assert canonical_path(tmp_path).read_bytes() == before
    history = load_local_history(tmp_path, "AAPL", population_contract=POPULATION_CONTRACT)
    assert history.frame.close.iloc[0] == 10.5
    assert history.frame.available_at.iloc[0] == pd.Timestamp(response.request_ended_at)


def test_nightly_fetch_sees_reused_daytime_canonical_tail_first(tmp_path, monkeypatch):
    from dataclasses import asdict
    import app.services.databento_market_data as provider
    import datafetching.databento_fetch as fetching
    from ml.intraday.supervisor import refresh_ohlcv
    journal = setup_journal(tmp_path, monkeypatch)
    journal.save(attempt())
    # A malformed second symbol must keep its cursor while the first symbol's
    # reuse and the existing provider continuation both proceed.
    bad = replace(attempt(), attempt_id="bad-slot")
    bad_slot = journal.slot("MSFT", bad.source_id, bad.interval_open, bad.interval_close)
    with sqlite3.connect(journal.path) as db:
        db.execute("INSERT INTO attempts VALUES(?,?,?)", (bad.attempt_id, bad_slot, json.dumps(asdict(bad))))
    spec = next(s for s in databento_analysis_source_specs() if s.schema == "ohlcv-1m")
    class Provider:
        dataset = "EQUS.MINI"
        def __init__(self, **kwargs):
            pass
        def native_specs(self):
            return (spec,)
    monkeypatch.setattr(provider, "DatabentoMarketDataProvider", Provider)
    monkeypatch.setattr(fetching, "_load_repository_environment", lambda: None)
    called = []
    def fetch(provider, symbols, profile, store):
        frame = pd.read_parquet(canonical_path(tmp_path))
        called.append(frame.timestamp.max())
        assert tuple(symbols) == ("AAPL", "MSFT")
        return {symbol: [(spec, [], None, SimpleNamespace(start=pd.Timestamp("2026-10-29T14:00:00Z"),
                      end=pd.Timestamp("2026-10-29T14:15:00Z")), None)] for symbol in symbols}
    monkeypatch.setattr(fetching, "_fetch_native_results_many", fetch)
    report = refresh_ohlcv(tmp_path, ["AAPL", "MSFT"], clock=lambda: pd.Timestamp("2026-10-29T14:20:00Z"))
    assert called == [pd.Timestamp("2026-10-29T14:14:00Z")]
    assert report["AAPL"]["retained_intraday_reuse"]["minute_rows"] == 15
    error = report["MSFT"]["retained_intraday_reuse"]["errors"]["MSFT"]
    assert error["failure_type"] == "ContractError" and error["failed_batch_cursor"] == "not_advanced"
    assert report["MSFT"]["status"] == "refreshed"
    with sqlite3.connect(tmp_path / subject.INDEX) as db:
        assert db.execute("SELECT count(*) FROM journal_cursors WHERE symbol='MSFT'").fetchone()[0] == 0
