"""An empty UTC Parquet column can change resolution without changing values."""
import hashlib
import json
import sys
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pandas as pd
import pytest

from ml.intraday import native_recovery as recovery
from ml.intraday.prediction import ContractError


OPEN = pd.Timestamp("2026-05-11T14:00Z")
CLOSE = OPEN + pd.Timedelta(minutes=15)


def native_frame(count=0, unit="s"):
    times = [OPEN + pd.Timedelta(minutes=i) for i in range(count)]
    return pd.DataFrame({
        "ts_event": pd.Series(pd.DatetimeIndex(times, dtype=pd.DatetimeTZDtype(unit=unit, tz=ZoneInfo("UTC")))),
        "rtype": pd.Series([33] * count, dtype="uint8"),
        "publisher_id": pd.Series([1] * count, dtype="uint16"),
        "instrument_id": pd.Series([1234] * count, dtype="uint32"),
        "open": pd.Series([100.] * count, dtype="float64"),
        "high": pd.Series([102.] * count, dtype="float64"),
        "low": pd.Series([99.] * count, dtype="float64"),
        "close": pd.Series([101.] * count, dtype="float64"),
        "volume": pd.Series([10] * count, dtype="uint64"),
        "symbol": pd.Series(["TEST"] * count, dtype="str" if count else "object"),
    })


def prepare(tmp_path, monkeypatch, native, normalized=None, *, metadata=None,
            request_changes=None, receipt_changes=None):
    """Mock only the DBN decode; exercise real hashes, Parquet and validation."""
    normalized = native.copy() if normalized is None else normalized.copy()
    request = dict(dataset="EQUS.MINI", schema="ohlcv-1m", symbols=["TEST"],
                   start=OPEN.isoformat(), end=CLOSE.isoformat(), stype_in="raw_symbol")
    request.update(request_changes or {})
    raw = tmp_path / "raw.dbn"
    raw.write_bytes(b"public synthetic decoder fixture; not provider data")
    parquet = tmp_path / "native.parquet"
    normalized.to_parquet(parquet, index=False)
    sha = lambda path: hashlib.sha256(path.read_bytes()).hexdigest()
    receipt = dict(request=request,
        request_id=hashlib.sha256(json.dumps(request, sort_keys=True).encode()).hexdigest(),
        started_at="2026-10-17T17:59:00Z", received_at="2026-10-17T18:00:00Z",
        status="success", rows=len(native), raw_sha256=sha(raw), normalized_sha256=sha(parquet))
    receipt.update(receipt_changes or {})
    path = tmp_path / "receipt.json"
    path.write_text(json.dumps(receipt), encoding="utf-8")
    saved = SimpleNamespace(dataset="EQUS.MINI", schema="ohlcv-1m", symbols=["TEST"],
        start=OPEN, end=CLOSE,
        to_df=lambda **kwargs: native.set_index("ts_event").copy())
    for key, value in (metadata or {}).items():
        setattr(saved, key, value)
    monkeypatch.setitem(sys.modules, "databento", SimpleNamespace(
        DBNStore=SimpleNamespace(from_file=lambda selected: saved)))
    recovery._verified_receipt.cache_clear()
    args = (str(path), sha(path), sha(raw), sha(parquet))
    return lambda: recovery._verified_receipt(*args), path, raw, parquet


@pytest.mark.parametrize("unit", ["s", "ms", "us", "ns"])
def test_empty_utc_parquet_resolution_roundtrip_retains_zero_rows(tmp_path, monkeypatch, unit):
    original = native_frame(unit=unit)
    read, _, _, _ = prepare(tmp_path, monkeypatch, original)
    receipt, frame = read()
    assert receipt["rows"] == len(frame) == 0
    assert list(frame) == ["symbol", "opened_at", *recovery.FIELDS]
    assert isinstance(frame.opened_at.dtype, pd.DatetimeTZDtype)
    assert str(frame.opened_at.dtype.tz) == "UTC"


@pytest.mark.parametrize("unit", ["ms", "us", "ns"])
def test_empty_equivalent_utc_storage_units_compare_without_adding_rows(unit):
    columns = ["symbol", "ts_event", *recovery.FIELDS]
    a = native_frame(unit="s")[columns]
    b = native_frame(unit=unit)[columns]
    assert recovery._same_native_projection(a, b)
    assert a.empty and b.empty


@pytest.mark.parametrize("field,dtype", [("open", "float32"), ("volume", "int64"),
                                          ("symbol", "string")])
def test_empty_other_dtype_mismatch_remains_rejected(tmp_path, monkeypatch, field, dtype):
    native = native_frame()
    normalized = native.copy()
    normalized[field] = normalized[field].astype(dtype)
    read, _, _, _ = prepare(tmp_path, monkeypatch, native, normalized)
    with pytest.raises(ContractError, match="projection differs"):
        read()


@pytest.mark.parametrize("timestamps", [
    pd.Series([], dtype="datetime64[ns]"),
    pd.Series([], dtype="datetime64[ns, Europe/Paris]"),
    pd.Series([], dtype="object"),
    pd.Series([], dtype="string"),
])
def test_empty_invalid_timestamp_schema_remains_rejected(tmp_path, monkeypatch, timestamps):
    native = native_frame()
    normalized = native.copy()
    normalized["ts_event"] = timestamps
    read, _, _, _ = prepare(tmp_path, monkeypatch, native, normalized)
    with pytest.raises(ContractError, match="projection differs"):
        read()


@pytest.mark.parametrize("column", ["symbol", "ts_event", *recovery.FIELDS])
def test_empty_missing_required_column_remains_rejected(tmp_path, monkeypatch, column):
    native = native_frame()
    read, _, _, _ = prepare(tmp_path, monkeypatch, native, native.drop(columns=column))
    with pytest.raises(ContractError, match="projection differs"):
        read()


def test_nonempty_exact_projection_still_verifies(tmp_path, monkeypatch):
    native = native_frame(2, unit="ns")
    read, _, _, _ = prepare(tmp_path, monkeypatch, native)
    receipt, frame = read()
    assert receipt["rows"] == len(frame) == 2
    assert frame.close.tolist() == [101., 101.]


@pytest.mark.parametrize("field", ["open", "high", "low", "close", "volume", "symbol", "ts_event"])
def test_nonempty_value_mismatch_remains_rejected(tmp_path, monkeypatch, field):
    native = native_frame(2, unit="ns")
    normalized = native.copy()
    if field == "symbol":
        normalized.loc[0, field] = "OTHER"
    elif field == "ts_event":
        normalized.loc[0, field] += pd.Timedelta(seconds=1)
    else:
        normalized.loc[0, field] += 1
    read, _, _, _ = prepare(tmp_path, monkeypatch, native, normalized)
    with pytest.raises(ContractError, match="projection differs"):
        read()


@pytest.mark.parametrize("field,dtype", [("open", "float32"), ("volume", "int64"),
                                          ("symbol", "string"), ("ts_event", "datetime64[ms, UTC]")])
def test_nonempty_dtype_mismatch_remains_rejected(tmp_path, monkeypatch, field, dtype):
    native = native_frame(2, unit="ns")
    normalized = native.copy()
    normalized[field] = normalized[field].astype(dtype)
    read, _, _, _ = prepare(tmp_path, monkeypatch, native, normalized)
    with pytest.raises(ContractError, match="projection differs"):
        read()


@pytest.mark.parametrize("metadata", [
    {"dataset": "OTHER"}, {"schema": "ohlcv-1s"}, {"symbols": ["OTHER"]},
    {"start": OPEN - pd.Timedelta(minutes=15)}, {"end": CLOSE + pd.Timedelta(minutes=15)},
])
def test_empty_native_identity_mismatch_remains_rejected(tmp_path, monkeypatch, metadata):
    read, _, _, _ = prepare(tmp_path, monkeypatch, native_frame(), metadata=metadata)
    with pytest.raises(ContractError, match="DBN identity differs"):
        read()


@pytest.mark.parametrize("changes", [
    {"status": "failed"}, {"request_id": "bad"}, {"raw_sha256": "0" * 64},
    {"normalized_sha256": "0" * 64}, {"started_at": "2026-05-11T13:59:59Z"},
    {"received_at": "2026-10-17T17:58:59Z"},
])
def test_empty_receipt_provenance_or_clock_mismatch_remains_rejected(tmp_path, monkeypatch, changes):
    read, _, _, _ = prepare(tmp_path, monkeypatch, native_frame(), receipt_changes=changes)
    with pytest.raises(ContractError, match="scope or clocks differ"):
        read()


@pytest.mark.parametrize("changes", [
    {"dataset": "OTHER"}, {"schema": "ohlcv-1s"}, {"stype_in": "instrument_id"},
])
def test_empty_request_contract_mismatch_remains_rejected(tmp_path, monkeypatch, changes):
    read, _, _, _ = prepare(tmp_path, monkeypatch, native_frame(), request_changes=changes)
    with pytest.raises(ContractError, match="scope or clocks differ"):
        read()


def test_empty_declared_row_count_mismatch_remains_rejected(tmp_path, monkeypatch):
    read, _, _, _ = prepare(tmp_path, monkeypatch, native_frame(), receipt_changes={"rows": 1})
    with pytest.raises(ContractError, match="projection differs"):
        read()


@pytest.mark.parametrize("name", ["receipt", "raw", "normalized"])
def test_empty_immutable_bytes_mutation_remains_rejected(tmp_path, monkeypatch, name):
    read, receipt, raw, normalized = prepare(tmp_path, monkeypatch, native_frame())
    {"receipt": receipt, "raw": raw, "normalized": normalized}[name].write_bytes(b"changed")
    with pytest.raises(ContractError, match="immutable payload hash differs"):
        read()


def test_nonempty_foreign_native_symbol_remains_rejected(tmp_path, monkeypatch):
    native = native_frame(1, unit="ns")
    native["symbol"] = "OTHER"
    read, _, _, _ = prepare(tmp_path, monkeypatch, native)
    with pytest.raises(ContractError, match="unrequested symbols"):
        read()


@pytest.mark.parametrize("native_unit", ["s", "ms", "us", "ns"])
@pytest.mark.parametrize("normalized_unit", ["s", "ms", "us", "ns"])
def test_empty_stdlib_and_zoneinfo_utc_identity_roundtrip(native_unit, normalized_unit):
    from datetime import timezone
    from zoneinfo import ZoneInfo
    columns = ["symbol", "ts_event", *recovery.FIELDS]
    native = native_frame(unit=native_unit)[columns]
    normalized = native_frame(unit=normalized_unit)[columns]
    native["ts_event"] = native.ts_event.astype(pd.DatetimeTZDtype(unit=native_unit, tz=timezone.utc))
    normalized["ts_event"] = normalized.ts_event.astype(pd.DatetimeTZDtype(unit=normalized_unit, tz=ZoneInfo("UTC")))
    assert recovery._same_native_projection(native, normalized)


def test_empty_spoofed_utc_nonzero_offset_is_rejected():
    from datetime import timedelta, tzinfo
    class NamedUTC(tzinfo):
        def utcoffset(self, dt):
            return timedelta(hours=1)
        def dst(self, dt):
            return timedelta(0)
        def tzname(self, dt):
            return "UTC"
        def __str__(self):
            return "UTC"
    columns = ["symbol", "ts_event", *recovery.FIELDS]
    native = native_frame()[columns]
    normalized = native.copy()
    normalized["ts_event"] = pd.Series([], dtype=pd.DatetimeTZDtype(unit="ms", tz=NamedUTC()))
    assert not recovery._same_native_projection(native, normalized)


def test_verified_empty_receipt_adds_no_target_or_qualifying_candle(tmp_path, monkeypatch):
    import shutil
    from ml.intraday.data import History, SessionBounds, validate_candles
    from ml.intraday.native_interval import _candle
    fixture = tmp_path / "fixture"
    fixture.mkdir()
    read, receipt, raw, normalized = prepare(fixture, monkeypatch, native_frame())
    verified, frame = read()
    assert verified["rows"] == len(frame) == 0
    directory = tmp_path / "intraday/scout/minute-recovery/attempt"
    directory.mkdir(parents=True)
    for path in (receipt, raw, normalized):
        shutil.copyfile(path, directory / path.name)
    entry = dict(path="attempt/receipt.json",
        receipt_sha256=hashlib.sha256(receipt.read_bytes()).hexdigest(),
        raw_sha256=hashlib.sha256(raw.read_bytes()).hexdigest(),
        normalized_sha256=hashlib.sha256(normalized.read_bytes()).hexdigest())
    (directory.parent / "installed.json").write_text(json.dumps(dict(
        contract=recovery.CONTRACT, pc="Scout", symbols=["TEST"], receipts=[entry])))
    session = SessionBounds("2026-05-11", "2026-05-11T08:00Z", "2026-05-12T00:00Z")
    previous = OPEN - pd.Timedelta(minutes=15)
    coverage = dict(kind="historical_native_raw_range", status="success", symbol="TEST",
        source_dataset="EQUS.MINI", start=session.opened_at, end=session.closed_at,
        observation_basis="canonical_native_file_after_successful_fetch_merges",
        raw_sha256="a" * 64, normalized_sha256="b" * 64)
    candle = _candle(symbol="TEST", source_dataset="EQUS.MINI", opened=previous,
        available=OPEN, kind="assumed_historical_close", session=session,
        rows=[dict(opened_at=previous.isoformat(), open=100., high=102., low=99., close=101., volume=10.)],
        coverage=coverage)
    original = History(validate_candles(pd.DataFrame([candle])), {"candle_rows": 1})
    after = recovery.merge_recovered_history(original, root=tmp_path, symbol="TEST", pc="Scout")
    pd.testing.assert_frame_equal(original.frame, after.frame)
    assert not after.frame.opened_at.eq(OPEN).any()
    assert after.manifest["recovery"]["new_quarters"] == 0
    assert after.manifest["recovery"]["revised_quarters"] == 0
    assert after.manifest["recovery"]["sources"][0]["rows"] == 0


def test_verified_empty_source_remains_pending_then_source_empty_without_candle():
    from ml.intraday.data import IntervalResponse, SessionBounds
    from ml.intraday.native_interval import resolve_native_interval
    session = SessionBounds("2026-05-11", "2026-05-11T08:00Z", "2026-05-12T00:00Z")
    def attempt(identity, started, ended):
        return IntervalResponse(identity, "TEST", "databento:EQUS.MINI:ohlcv-1m", "EQUS.MINI",
            OPEN.isoformat(), CLOSE.isoformat(), started, ended, "success", (),
            OPEN.isoformat(), CLOSE.isoformat()).validate()
    first = attempt("first", "2026-10-17T17:59:00Z", "2026-10-17T17:59:01Z")
    second = attempt("second", "2026-10-17T17:59:05Z", "2026-10-17T17:59:06Z")
    single = resolve_native_interval([first], session=session)
    assert single["status"] == "retry" and single["reason"] == "native_observation_snapshot_not_stable"
    assert single["candle"] is None
    pair = resolve_native_interval([first, second], session=session)
    assert pair == dict(status="missing", reason="source_empty_interval", candle=None)
