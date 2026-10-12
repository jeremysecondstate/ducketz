"""Scientific input contract tests use observations, never provider access."""
from dataclasses import replace
import hashlib
import json
from types import SimpleNamespace

import numpy as np
import pandas as pd
import pytest

from ml.intraday.data import (
    AcquisitionJournal, IntervalResponse, SessionBounds, aggregate_minutes,
    build_dataset, causal_features, equity_sessions, exact_target,
    fetch_databento_interval_attempt, fetch_databento_interval_attempts, resolve_minute_candle,
    recover_databento_candle,
    load_local_history, next_acquisition, recover_interval, resolve_interval,
    validate_candles,
)
from ml.intraday.prediction import ContractError, STEP, UnresolvedPolicy


def archive_partition(tmp_path, monkeypatch, *, frame=None, name="2026-05-11_to_2026-05-12", natives=None):
    """Local provider-format fixtures, including independently retained native rows."""
    from ml.intraday import data
    directory = tmp_path/"market-data/databento/us-equities/XNAS.ITCH/ohlcv-1m/AAPL/windows"/name
    directory.mkdir(parents=True)
    if frame is None:
        frame = candles(45, minutes=True).rename(columns={"opened_at": "ts_event"})
        frame = frame[["ts_event", "symbol", "open", "high", "low", "close", "volume"]]
    frame = frame.copy()
    frame.to_parquet(directory/"normalized.parquet", index=False)
    (directory/"provider.dbn.zst").write_bytes(b"independent native DBN fixture")
    manifest = {"schema_version": "databento-cold-start-partition-v1",
        "request": {"dataset": "XNAS.ITCH", "schema": "ohlcv-1m", "symbol_scope": ["AAPL"],
                    "stype_in": "raw_symbol", "storage_contract": "generic", "request_id": name,
                    "start": "2026-05-11", "end": "2026-05-12"},
        "normalized": {"path": "normalized.parquet", "row_count": len(frame), "timestamp_column": "ts_event",
                       "earliest_timestamp": frame.ts_event.min().isoformat(), "latest_timestamp": frame.ts_event.max().isoformat()},
        "raw": {"path": "provider.dbn.zst"}}
    for kind in ("raw", "normalized"):
        path = directory/manifest[kind]["path"]
        manifest[kind].update(checksum_sha256=hashlib.sha256(path.read_bytes()).hexdigest(), size_bytes=path.stat().st_size)
    (directory/"manifest.json").write_text(json.dumps(manifest))
    receipt = {"schema_version": "databento-cold-start-receipt-v1", "request_id": name,
        "manifest_checksum_sha256": hashlib.sha256((directory/"manifest.json").read_bytes()).hexdigest(),
        "normalized_checksum_sha256": manifest["normalized"]["checksum_sha256"],
        "raw_checksum_sha256": manifest["raw"]["checksum_sha256"]}
    (directory/"receipt.json").write_text(json.dumps(receipt))
    natives = {} if natives is None else natives
    natives[directory/"provider.dbn.zst"] = frame.copy()
    monkeypatch.setattr(data, "_archive_native_frame", lambda path, **kwargs: natives[path].copy())
    return directory, natives


def test_xnas_archive_native_prices_provenance_and_missing_minutes(tmp_path, monkeypatch):
    frame = candles(45, minutes=True).rename(columns={"opened_at": "ts_event"})
    frame = frame[["ts_event", "symbol", "open", "high", "low", "close", "volume"]].drop(index=17)
    archive_partition(tmp_path, monkeypatch, frame=frame)
    history = load_local_history(tmp_path, "AAPL", source_dataset="XNAS.ITCH")
    assert len(history.frame) == 2
    assert history.manifest["aggregation"]["incomplete_intervals"] == 1
    assert history.manifest["price_basis_evidence"] == "all_OHLCV_equal_native_DBN_float_decode"
    assert history.frame.source_dataset.eq("XNAS.ITCH").all()
    assert history.frame.availability_kind.eq("assumed_historical_close").all()
    assert not history.frame.synthetic.any()
    assert len(history.manifest["files"]) == 4
    # Canonical selection never silently falls back to the archive.
    with pytest.raises(ContractError, match="No existing canonical"):
        load_local_history(tmp_path, "AAPL")


@pytest.mark.parametrize("filename", ["receipt.json", "manifest.json", "normalized.parquet", "provider.dbn.zst"])
def test_xnas_archive_missing_or_changed_provenance_rejected(tmp_path, monkeypatch, filename):
    directory, _ = archive_partition(tmp_path, monkeypatch)
    path = directory/filename
    if filename == "receipt.json":
        path.unlink()
    else:
        path.write_bytes(path.read_bytes()+b"changed")
    with pytest.raises(ContractError):
        load_local_history(tmp_path, "AAPL", source_dataset="XNAS.ITCH")


def test_xnas_archive_rejects_rehashed_adjusted_prices(tmp_path, monkeypatch):
    directory, natives = archive_partition(tmp_path, monkeypatch)
    natives[directory/"provider.dbn.zst"]["open"] *= 2
    with pytest.raises(ContractError, match="unadjusted native"):
        load_local_history(tmp_path, "AAPL", source_dataset="XNAS.ITCH")


def test_xnas_archive_verified_isolated_cold_partition(tmp_path, monkeypatch):
    directory, _ = archive_partition(tmp_path, monkeypatch)
    mp, rp = directory/"manifest.json", directory/"receipt.json"
    manifest, receipt = json.loads(mp.read_text()), json.loads(rp.read_text())
    manifest["request"]["storage_contract"] = "isolated-cold-start"
    mp.write_text(json.dumps(manifest))
    receipt["manifest_checksum_sha256"] = hashlib.sha256(mp.read_bytes()).hexdigest()
    rp.write_text(json.dumps(receipt))
    assert len(load_local_history(tmp_path, "AAPL", source_dataset="XNAS.ITCH").frame) == 3


@pytest.mark.parametrize("invalid", ["null_price", "bad_range", "negative_volume"])
def test_xnas_invalid_native_row_is_gap_and_excludes_whole_interval(tmp_path, monkeypatch, invalid):
    frame = candles(45, minutes=True).rename(columns={"opened_at": "ts_event"})
    frame = frame[["ts_event", "symbol", "open", "high", "low", "close", "volume"]]
    if invalid == "null_price": frame.loc[17, ["open", "high", "low", "close"]] = np.nan
    if invalid == "bad_range": frame.loc[17, "high"] = 1.0
    if invalid == "negative_volume": frame.loc[17, "volume"] = -1.0
    archive_partition(tmp_path, monkeypatch, frame=frame)
    history = load_local_history(tmp_path, "AAPL", source_dataset="XNAS.ITCH")
    assert history.manifest["source_minute_rows"] == 45
    assert history.manifest["minute_rows"] == 44
    assert history.manifest["invalid_native_rows_excluded"] == 1
    assert history.manifest["invalid_native_observations"][0]["opened_at"] == frame.ts_event.iloc[17].isoformat()
    assert history.manifest["aggregation"]["incomplete_intervals"] == 1
    assert len(history.frame) == 2
    assert not history.frame.opened_at.eq(frame.ts_event.iloc[15]).any()
    assert not history.frame.synthetic.any()


@pytest.mark.parametrize("mismatch", ["symbol", "naive", "off_minute", "dataset"])
def test_xnas_archive_exact_identity_and_timestamps(tmp_path, monkeypatch, mismatch):
    frame = candles(45, minutes=True).rename(columns={"opened_at": "ts_event"})
    frame = frame[["ts_event", "symbol", "open", "high", "low", "close", "volume"]]
    if mismatch == "symbol": frame["symbol"] = "WRONG"
    if mismatch == "naive": frame["ts_event"] = frame.ts_event.dt.tz_localize(None)
    if mismatch == "off_minute": frame["ts_event"] += pd.Timedelta(seconds=1)
    directory, _ = archive_partition(tmp_path, monkeypatch, frame=frame)
    if mismatch == "dataset":
        mp, rp = directory/"manifest.json", directory/"receipt.json"
        manifest, receipt = json.loads(mp.read_text()), json.loads(rp.read_text())
        manifest["request"]["dataset"] = "EQUS.MINI"
        mp.write_text(json.dumps(manifest))
        receipt["manifest_checksum_sha256"] = hashlib.sha256(mp.read_bytes()).hexdigest()
        rp.write_text(json.dumps(receipt))
    with pytest.raises(ContractError):
        load_local_history(tmp_path, "AAPL", source_dataset="XNAS.ITCH")


@pytest.mark.parametrize("conflict", [False, True])
def test_xnas_archive_duplicate_observations(tmp_path, monkeypatch, conflict):
    first, natives = archive_partition(tmp_path, monkeypatch)
    frame = natives[first/"provider.dbn.zst"].copy()
    if conflict: frame["volume"] += 1
    archive_partition(tmp_path, monkeypatch, frame=frame, name="second", natives=natives)
    if conflict:
        with pytest.raises(ContractError, match="Conflicting archive"):
            load_local_history(tmp_path, "AAPL", source_dataset="XNAS.ITCH")
    else:
        assert len(load_local_history(tmp_path, "AAPL", source_dataset="XNAS.ITCH").frame) == 3


def test_xnas_archive_rejects_source_mutation_while_reading(tmp_path, monkeypatch):
    from ml.intraday import data
    directory, _ = archive_partition(tmp_path, monkeypatch)
    read = pd.read_parquet
    def changed(path):
        result = read(path)
        receipt = directory/"receipt.json"
        receipt.write_text(receipt.read_text()+" ")
        return result
    monkeypatch.setattr(data.pd, "read_parquet", changed)
    with pytest.raises(ContractError, match="Concurrent source mutation"):
        load_local_history(tmp_path, "AAPL", source_dataset="XNAS.ITCH")


@pytest.mark.parametrize("field,value", [("dataset", "EQUS.MINI"), ("schema", "ohlcv-1s"), ("symbols", ["OTHER"])])
def test_xnas_archive_native_decoder_checks_identity(monkeypatch, field, value):
    import sys
    from ml.intraday.data import _archive_native_frame
    saved = SimpleNamespace(dataset="XNAS.ITCH", schema="ohlcv-1m", symbols=["AAPL"],
                            start=pd.Timestamp("2026-05-11", tz="UTC"), end=pd.Timestamp("2026-05-12", tz="UTC"))
    setattr(saved, field, value)
    # Exercise metadata admission without importing the optional network client.
    monkeypatch.setitem(sys.modules, "databento", SimpleNamespace(DBNStore=SimpleNamespace(from_file=lambda path: saved)))
    with pytest.raises(ContractError, match="raw DBN identity"):
        _archive_native_frame("unused", symbol="AAPL", request={"start":"2026-05-11", "end":"2026-05-12"})


def test_aggregation_session_index_preserves_cross_utc_midnight():
    frame = candles(30, start="2026-05-11T23:45:00+00:00", minutes=True)
    session = SessionBounds("2026-05-11", "2026-05-11T23:30:00+00:00", "2026-05-12T00:30:00+00:00")
    aggregated, _ = aggregate_minutes(frame, sessions=[session])
    assert len(aggregated) == 2
    assert aggregated.session_id.eq("2026-05-11").all()


def candles(count=42, *, start="2026-05-11T13:30:00+00:00", minutes=False):
    first = pd.Timestamp(start)
    duration = pd.Timedelta(minutes=1 if minutes else 15)
    rows = []
    for index in range(count):
        op = first + index * duration
        price = 100.0 + (index % 7) / 10
        rows.append(dict(symbol="AAPL", opened_at=op, available_at=op + duration,
            availability_kind="actual", open=price, high=price+1, low=price-1,
            close=price+0.1, volume=100+index, source_id="test:EQUS.MINI", source_dataset="EQUS.MINI",
            session_id="2026-05-11", session_open=first.isoformat(),
            session_close=(first + pd.Timedelta(hours=12)).isoformat(), synthetic=False,
            no_trade=False, evidence_json="{}"))
    return pd.DataFrame(rows)


def response(attempt, *, status="success", rows=(), covered=True, proof="provider covered interval contains no trades",
             start="2026-05-11T14:00:00+00:00", minutes=15, wait=0):
    op = pd.Timestamp(start)
    cl = op + pd.Timedelta(minutes=minutes)
    return IntervalResponse(str(attempt), "AAPL", "test:EQUS.MINI", "EQUS.MINI", op.isoformat(), cl.isoformat(),
        (cl+pd.Timedelta(seconds=4*attempt+wait)).isoformat(),
        (cl+pd.Timedelta(seconds=4*attempt+1+wait)).isoformat(), status, tuple(rows),
        op.isoformat() if covered else None, cl.isoformat() if covered else None, proof)


def bounds():
    return SessionBounds("2026-05-11", "2026-05-11T13:30:00+00:00", "2026-05-11T23:00:00+00:00")


def test_exact_target_equality_and_maturity():
    frame = candles()
    reference = frame.opened_at.iloc[32]
    cutoff = reference + STEP + pd.Timedelta(seconds=30)
    # Latest potentially mature label: reference30 -> target32 closes at33.
    frame.loc[32, "close"] = frame.loc[30, "open"]
    target, close = exact_target(reference)
    assert target == reference + 2 * STEP and close == reference + 3 * STEP
    data = build_dataset(frame, cutoff=cutoff, reference_at=reference)
    assert data.reference_at[-1] == frame.opened_at.iloc[30]
    assert data.target_close_at[-1] == frame.opened_at.iloc[33]
    assert data.y[-1] == 1
    assert max(data.label_available_at) <= cutoff
    assert reference not in data.reference_at


def test_label_observation_must_be_available_not_merely_closed():
    frame = candles()
    reference = frame.opened_at.iloc[32]
    cutoff = reference + STEP + pd.Timedelta(seconds=30)
    # Target31 for reference29 is delayed. It is not a matured label.
    frame.loc[31, "available_at"] = cutoff + pd.Timedelta(hours=1)
    data = build_dataset(frame, cutoff=cutoff, reference_at=reference)
    assert frame.opened_at.iloc[29] not in data.reference_at
    assert np.isfinite(data.X).all()


def test_future_candles_do_not_change_features_or_snapshot():
    frame = candles()
    reference = frame.opened_at.iloc[32]
    cutoff = reference + STEP + pd.Timedelta(seconds=30)
    first = build_dataset(frame, cutoff=cutoff, reference_at=reference)
    changed = frame.copy()
    for field in ("open", "high", "low", "close", "volume"):
        changed.loc[33:, field] *= 50
    second = build_dataset(changed, cutoff=cutoff, reference_at=reference)
    np.testing.assert_array_equal(first.X, second.X)
    np.testing.assert_array_equal(first.y, second.y)
    np.testing.assert_array_equal(first.inference_X, second.inference_X)
    assert first.inference["snapshot_sha256"] == second.inference["snapshot_sha256"]


def test_historical_features_do_not_use_earlier_observation_received_later():
    frame = candles()
    reference = frame.opened_at.iloc[39]
    cutoff = reference + STEP
    frame.loc[22, "available_at"] = frame.opened_at.iloc[30] + STEP
    data = build_dataset(frame, cutoff=cutoff, reference_at=reference)
    assert frame.opened_at.iloc[23] not in data.reference_at
    assert frame.opened_at.iloc[30] in data.reference_at


def test_late_computation_does_not_move_cycle_reference():
    frame = candles()
    reference = frame.opened_at.iloc[32]
    early = build_dataset(frame, cutoff=reference + STEP, reference_at=reference)
    late = build_dataset(frame, cutoff=reference + 3*STEP, reference_at=reference)
    assert early.inference == late.inference
    assert late.inference["target_open"] == (reference+2*STEP).isoformat()


def test_synthetic_training_is_explicit_and_separately_counted():
    frame = candles()
    frame.loc[30, "synthetic"] = True
    reference = frame.opened_at.iloc[32]
    cutoff = reference+STEP
    with pytest.raises(UnresolvedPolicy, match="Synthetic"):
        build_dataset(frame, cutoff=cutoff, reference_at=reference)
    included = build_dataset(frame, cutoff=cutoff, reference_at=reference, synthetic_policy="include")
    excluded = build_dataset(frame, cutoff=cutoff, reference_at=reference, synthetic_policy="exclude")
    assert len(included.y) > len(excluded.y)
    assert excluded.manifest["synthetic_labels_excluded"] == len(included.y)-len(excluded.y)


def test_offset_is_quarter_boundary_and_dst_safe():
    now = "2026-10-12T10:00:30-07:00"
    assert next_acquisition(now, offset_seconds=30) == pd.Timestamp("2026-10-12T10:15:30-07:00")
    assert next_acquisition("2026-10-12T10:00:29-07:00", offset_seconds=30) == pd.Timestamp(now)
    assert next_acquisition("2026-11-01T01:59:31-07:00", offset_seconds=30) == pd.Timestamp("2026-11-01T01:00:30-08:00")
    with pytest.raises(ContractError):
        next_acquisition("2026-10-12T10:00:00", offset_seconds=30)


def test_success_empty_without_coverage_or_proof_is_not_no_trade():
    prior = candles(2).iloc[-1].to_dict()
    for kwargs in ({"covered": False}, {"proof": None}, {"status": "temporary_error"}, {"status": "unavailable_coverage"}):
        result = resolve_interval([response(1, **kwargs), response(2, **kwargs)], session=bounds(), previous=prior)
        assert result["status"] == "retry"
        assert result["candle"] is None


def test_no_trade_uses_last_close_not_prior_ohlc_and_evidence():
    prior = candles(2).iloc[-1].to_dict()
    result = resolve_interval([response(1), response(2, wait=2)], session=bounds(), previous=prior)
    candle = result["candle"]
    assert result["status"] == "synthetic_no_trade"
    assert candle["open"] == candle["high"] == candle["low"] == candle["close"] == prior["close"]
    assert candle["volume"] == 0
    assert candle["prior_price_opened_at"] == prior["opened_at"].isoformat()
    assert len(json.loads(candle["evidence_json"])) == 2
    assert candle["availability_kind"] == "actual"
    assert resolve_interval([response(1), response(2, wait=2)], session=bounds(), previous=None)["status"] == "missing"


def test_no_fill_outside_session_and_no_target_across_closure():
    attempts = [response(1, start="2026-05-11T23:00:00+00:00"), response(2, start="2026-05-11T23:00:00+00:00")]
    assert resolve_interval(attempts, session=bounds(), previous=candles(2).iloc[-1].to_dict())["status"] == "closed"
    frame = candles(45)
    frame["session_close"] = "2026-05-12T00:45:00+00:00"
    reference = frame.opened_at.iloc[-1]
    with pytest.raises(UnresolvedPolicy, match="closure"):
        build_dataset(frame, cutoff=reference+STEP, reference_at=reference)
    # XNAS holiday has no eligible session envelope.
    sessions = equity_sessions("2026-12-25T00:00:00+00:00", "2026-12-26T00:00:00+00:00")
    assert all(item.session_id != "2026-12-25" for item in sessions)


def test_constituent_fill_changes_open_and_preserves_provenance():
    frame = candles(15, minutes=True)
    # Leading no-trade minute has earlier close, rather than first trade open.
    frame.loc[0, ["open", "high", "low", "close"]] = 99.0
    frame.loc[0, "volume"] = 0
    frame.loc[0, ["synthetic", "no_trade"]] = True
    aggregate, report = aggregate_minutes(frame, sessions=[bounds()])
    assert len(aggregate) == 1 and report["full_intervals"] == 1
    row = aggregate.iloc[0]
    assert row.open == 99 and row.low == 99
    assert row.synthetic and not row.no_trade
    assert row.synthetic_open and not row.synthetic_close
    assert row.observed_constituents == 14 and row.synthetic_constituents == 1
    assert len(json.loads(row.evidence_json)["constituents"]) == 15
    empty, missing = aggregate_minutes(frame.drop(index=3), sessions=[bounds()])
    assert empty.empty and missing["incomplete_intervals"] == 1


def test_recovery_journal_immutable_and_survives_interruption(tmp_path):
    journal = AcquisitionJournal(tmp_path/"attempts.sqlite")
    first = response(1, status="temporary_error")
    journal.save(first)
    journal.save(first)
    with pytest.raises(ContractError, match="immutable"):
        journal.save(replace(first, error="changed"))
    attempts = iter([response(2), response(3, wait=2)])
    sleeps = []
    result = recover_interval(lambda: next(attempts), journal=journal, session=bounds(),
                              previous=candles(2).iloc[-1].to_dict(), sleep=sleeps.append,
                              slot=dict(symbol=first.symbol, source_id=first.source_id,
                                        interval_open=first.interval_open, interval_close=first.interval_close))
    assert result["status"] == "synthetic_no_trade" and sleeps == [4.0]
    restored = AcquisitionJournal(tmp_path/"attempts.sqlite")
    assert len(restored.responses(symbol=first.symbol, source_id=first.source_id,
                                 interval_open=first.interval_open, interval_close=first.interval_close)) == 3


def test_conflicting_revisions_and_future_availability_rejected():
    frame = candles(25)
    with pytest.raises(ContractError, match="immutable"):
        validate_candles(pd.concat([frame, frame.iloc[:1]], ignore_index=True))
    frame.loc[0, "available_at"] = frame.loc[0, "opened_at"]
    with pytest.raises(ContractError, match="precedes"):
        validate_candles(frame)


def test_history_reads_existing_format_requires_raw_dataset_and_never_mtime(tmp_path):
    folder = tmp_path/"stocks/AAPL/bars/1m/databento/normalized"
    folder.mkdir(parents=True)
    path = folder/"AAPL_source_100d_1m_ohlcv-1m_1m.parquet"
    frame = candles(30, minutes=True).rename(columns={"opened_at": "timestamp"})
    frame.loc[:, ["timestamp", "open", "high", "low", "close", "volume"]].to_parquet(path)
    raw = folder.parent/"raw"/(path.stem+"_raw.parquet")
    with pytest.raises(ContractError, match="provenance"):
        load_local_history(tmp_path, "AAPL")
    raw.parent.mkdir()
    pd.DataFrame(dict(provider_dataset=["EQUS.MINI"], source_schema=["ohlcv-1m"],
                      symbol=["AAPL"], price_basis=["unadjusted_market_scale"])).to_parquet(raw)
    loaded = load_local_history(tmp_path, "AAPL", availability_offset_seconds=5)
    assert len(loaded.frame) == 2
    assert loaded.frame.availability_kind.eq("assumed_historical_close").all()
    assert loaded.frame.available_at.iloc[0] == frame.timestamp.iloc[0]+STEP+pd.Timedelta(seconds=5)
    assert len(loaded.manifest["files"]) == 2
    pd.DataFrame(dict(provider_dataset=["XNAS.ITCH"], source_schema=["ohlcv-1m"],
                      symbol=["AAPL"], price_basis=["unadjusted_market_scale"])).to_parquet(raw)
    with pytest.raises(ContractError, match="identity"):
        load_local_history(tmp_path, "AAPL")


def test_features_are_causal_and_missing_elapsed_time_remains_visible():
    frame = candles(40)
    early = causal_features(frame.iloc[:30]).iloc[-1]
    full = causal_features(frame).iloc[29]
    np.testing.assert_array_equal(early, full)
    gapped = frame.drop(index=28).reset_index(drop=True)
    features = causal_features(gapped)
    assert features.loc[frame.opened_at.iloc[29], "elapsed_minutes_previous"] == 30


def test_native_minute_adapter_checks_advertised_coverage_and_batches():
    class Provider:
        dataset = "EQUS.MINI"
        requests = []
        def native_specs(self):
            return [SimpleNamespace(schema="ohlcv-1m", frequency="1m")]
        def dataset_range(self):
            return {"not": "used_directly"}
        def available_range_for_schema(self, schema, *, dataset_range):
            return SimpleNamespace(start=pd.Timestamp("2026-05-11T13:00:00Z"), end=pd.Timestamp("2026-05-11T15:00:00Z"))
        def fetch_native_bars_range(self, symbols, spec, *, start, end, available_range):
            self.requests.append((tuple(symbols), start, end))
            rows = [SimpleNamespace(timestamp=row.opened_at, open=row.open, high=row.high,
                                    low=row.low, close=row.close, volume=row.volume)
                    for row in candles(15, start="2026-05-11T14:00:00Z", minutes=True).itertuples()]
            return {s: (rows, None) for s in symbols}, SimpleNamespace(start=start, end=end)
    provider = Provider()
    clock = lambda: pd.Timestamp("2026-05-11T14:16:00Z")
    responses = fetch_databento_interval_attempts(provider, symbols=["AAPL", "AMZN"],
        opened_at="2026-05-11T14:00:00Z", attempt_id="batch", clock=clock)
    assert len(provider.requests) == 1
    assert provider.requests[0][0] == ("AAPL", "AMZN")
    assert all(r.covered() and r.status == "success" and len(r.rows) == 15 for r in responses.values())
    result = resolve_minute_candle([responses["AAPL"]], session=bounds(), previous=None)
    assert result["status"] == "observed" and result["candle"]["availability_kind"] == "actual"
    lag = fetch_databento_interval_attempt(provider, symbol="AAPL", opened_at="2026-05-11T15:00:00Z",
        attempt_id="lag", clock=lambda: pd.Timestamp("2026-05-11T15:16:00Z"))
    assert lag.status == "unavailable_coverage" and len(provider.requests) == 1


def test_constituent_omissions_need_exact_repeated_response_and_wait():
    native = candles(15, start="2026-05-11T14:00:00Z", minutes=True)
    rows = tuple({key: row[key] for key in ("opened_at", "open", "high", "low", "close", "volume")}
                 | {"opened_at": row.opened_at.isoformat()} for _, row in native.drop(index=[0, 14]).iterrows())
    first, second = response(1, rows=rows), response(2, rows=rows, wait=2)
    previous = candles(2).iloc[-1].to_dict()
    assert resolve_minute_candle([first], session=bounds(), previous=previous)["status"] == "retry"
    result = resolve_minute_candle([first, second], session=bounds(), previous=previous)
    assert result["status"] == "mixed_synthetic"
    assert result["candle"]["synthetic_constituents"] == 2
    assert result["candle"]["open"] == previous["close"]
    assert result["candle"]["close"] == native.close.iloc[13]
    assert result["constituents"][14]["volume"] == 0
    # An overlapping request, rather than the exact cycle, cannot be admitted.
    with pytest.raises(ContractError, match="identical"):
        resolve_minute_candle([first, replace(second, interval_open="2026-05-11T13:45:00Z")],
                              session=bounds(), previous=previous)


def test_retry_after_and_failures_cannot_confirm_no_trade():
    first = replace(response(1), retry_after_seconds=30)
    too_soon = response(2, wait=2)
    prior = candles(2).iloc[-1].to_dict()
    assert resolve_minute_candle([first, too_soon], session=bounds(), previous=prior)["status"] == "retry"
    enough_wait = response(3, wait=40)
    assert resolve_minute_candle([first, enough_wait], session=bounds(), previous=prior)["status"] == "synthetic_no_trade"
    failed = replace(enough_wait, status="temporary_error")
    assert resolve_minute_candle([first, failed], session=bounds(), previous=prior)["status"] == "retry"


def test_live_histories_genuine_availability_integration_without_order_side_effects(tmp_path):
    from ml.intraday.data import History
    from ml.intraday.runtime import LiveHistories
    from ml.intraday.store import ShadowStore
    reference = pd.Timestamp("2026-05-11T20:00:00Z")
    frame = candles(26)
    frame["availability_kind"] = "assumed_historical_close"
    frame["source_id"] = "databento:EQUS.MINI:ohlcv-1m"
    original = frame.copy(deep=True)
    class Provider:
        dataset = "EQUS.MINI"
        reads = 0
        def native_specs(self):
            return [SimpleNamespace(schema="ohlcv-1m", frequency="1m")]
        def dataset_range(self):
            return {}
        def available_range_for_schema(self, *args, **kwargs):
            return SimpleNamespace(start=reference-pd.Timedelta(days=1), end=reference+pd.Timedelta(hours=1))
        def fetch_native_bars_range(self, symbols, spec, *, start, end, available_range):
            self.reads += 1
            assert tuple(symbols) == ("AAPL",) and pd.Timestamp(start) == reference and pd.Timestamp(end) == reference+STEP
            native = candles(15, start=reference.isoformat(), minutes=True)
            bars = [SimpleNamespace(timestamp=row.opened_at, open=row.open, high=row.high,
                                    low=row.low, close=row.close, volume=row.volume) for row in native.itertuples()]
            return {"AAPL": (bars, None)}, SimpleNamespace(start=start, end=end)
        def submit_order(self, *args, **kwargs):
            pytest.fail("A read-only forecast must never call broker order submission")
    provider = Provider()
    store = ShadowStore(tmp_path/"shadow.sqlite3")
    histories = {"AAPL": History(frame, {"source_dataset": "EQUS.MINI"})}
    live = LiveHistories(histories=histories, provider=provider, store=store,
        journal=AcquisitionJournal(tmp_path/"acquisition.sqlite3"), reference_at=reference.isoformat(),
        clock=lambda: (reference+STEP+pd.Timedelta(seconds=30)).isoformat(), sleep=lambda seconds: pytest.fail("No gap expected"))
    live.bind_cycle("live-test")
    result = live.get("AAPL")
    pd.testing.assert_frame_equal(frame, original)
    current = result.frame.loc[result.frame.opened_at.eq(reference)].iloc[0]
    assert current.availability_kind == "actual" and current.available_at == reference+STEP+pd.Timedelta(seconds=30)
    dataset = build_dataset(result.frame, cutoff=current.available_at, reference_at=reference, synthetic_policy="include")
    assert dataset.inference["availability_kind"] == "actual"
    assert dataset.manifest["availability_kinds"] == ["actual", "assumed_historical_close"]
    assert provider.reads == 1
    with store.connect() as db:
        assert db.execute("SELECT COUNT(*) FROM candles").fetchone()[0] == 1
    restarted_histories = {"AAPL": History(original, {"source_dataset": "EQUS.MINI"})}
    LiveHistories(histories=restarted_histories, provider=provider, store=store,
        journal=AcquisitionJournal(tmp_path/"acquisition.sqlite3"), reference_at=(reference+STEP).isoformat())
    restored = restarted_histories["AAPL"].frame
    assert restored.loc[restored.opened_at.eq(reference)].availability_kind.eq("actual").all()
    assert provider.reads == 1  # Recovery restores durable receipt evidence without another price request.
    provider.dataset = "XNAS.ITCH"
    with pytest.raises(ContractError, match="single historical source"):
        LiveHistories(histories=restarted_histories, provider=provider, store=store,
                      journal=AcquisitionJournal(tmp_path/"acquisition.sqlite3"), reference_at=reference.isoformat())
    assert provider.reads == 1


def test_invalid_native_rows_cannot_turn_into_no_trade_after_provider_parsing():
    class Provider:
        dataset = "EQUS.MINI"
        def native_specs(self):
            return [SimpleNamespace(schema="ohlcv-1m", frequency="1m")]
        def dataset_range(self):
            return {}
        def available_range_for_schema(self, *args, **kwargs):
            return SimpleNamespace(start=pd.Timestamp("2026-05-11T13:00:00Z"), end=pd.Timestamp("2026-05-11T15:00:00Z"))
        def fetch_native_bars_range(self, symbols, spec, *, start, end, available_range):
            raw = pd.DataFrame({"ts_event": [start], "open": [None], "close": [None]})
            return {s: ([], raw) for s in symbols}, SimpleNamespace(start=start, end=end)
    kwargs = dict(opened_at="2026-05-11T14:00:00Z", attempt_id="invalid",
                  clock=lambda: pd.Timestamp("2026-05-11T14:16:00Z"))
    with pytest.raises(ContractError, match="invalid data is not no-trade"):
        fetch_databento_interval_attempt(Provider(), symbol="AAPL", **kwargs)
    with pytest.raises(ContractError, match="invalid data is not no-trade"):
        fetch_databento_interval_attempts(Provider(), symbols=["AAPL", "AMZN"], **kwargs)


@pytest.mark.parametrize("kind", ["generic", "databento"])
def test_restart_honors_persisted_sixty_second_retry_after_before_first_request(tmp_path, kind):
    first = replace(response(1, status="temporary_error"), retry_after_seconds=60,
                    source_id="databento:EQUS.MINI:ohlcv-1m")
    journal = AcquisitionJournal(tmp_path/"restart.sqlite3")
    journal.save(first)
    # Restart ten seconds into a sixty-second provider wait. Neither a new
    # provider request nor a generic callback may execute during the remaining
    # fifty seconds, even though no in-memory retry loop survived.
    now = [pd.Timestamp(first.request_ended_at)+pd.Timedelta(seconds=10)]
    due = pd.Timestamp(first.request_ended_at)+pd.Timedelta(seconds=60)
    waits, calls = [], []
    def clock():
        return now[0].isoformat()
    def sleep(seconds):
        waits.append(seconds)
        now[0] += pd.Timedelta(seconds=seconds)
    restarted = AcquisitionJournal(tmp_path/"restart.sqlite3")
    if kind == "generic":
        def fetch():
            assert now[0] >= due
            calls.append(now[0])
            return replace(first, attempt_id="after-restart", status="success", retry_after_seconds=0,
                request_started_at=clock(), request_ended_at=clock(),
                rows=({"opened_at": first.interval_open, "open": 100, "high": 101,
                       "low": 99, "close": 100, "volume": 10},))
        result = recover_interval(fetch, journal=restarted, session=bounds(), previous=None,
            slot=dict(symbol=first.symbol, source_id=first.source_id, interval_open=first.interval_open,
                      interval_close=first.interval_close), sleep=sleep, clock=clock)
    else:
        class Provider:
            dataset = "EQUS.MINI"
            def native_specs(self):
                return [SimpleNamespace(schema="ohlcv-1m", frequency="1m")]
            def dataset_range(self):
                assert now[0] >= due
                calls.append(now[0])
                return {}
            def available_range_for_schema(self, *args, **kwargs):
                return SimpleNamespace(start=pd.Timestamp(first.interval_open), end=pd.Timestamp(first.interval_close))
            def fetch_native_bars_range(self, symbols, spec, *, start, end, available_range):
                native = candles(15, start=first.interval_open, minutes=True)
                bars = [SimpleNamespace(timestamp=r.opened_at, open=r.open, high=r.high,
                        low=r.low, close=r.close, volume=r.volume) for r in native.itertuples()]
                return {"AAPL": (bars, None)}, SimpleNamespace(start=start, end=end)
        result = recover_databento_candle(Provider(), symbol="AAPL", opened_at=first.interval_open,
            journal=restarted, session=bounds(), previous=None, sleep=sleep, clock=clock)
    assert result["status"] == "observed"
    assert len(calls) == 1 and calls[0] == due
    assert sum(waits) == 50 and all(0 < seconds <= 4 for seconds in waits)


def test_journal_closes_connections_deterministically(tmp_path, monkeypatch):
    import ml.intraday.data as module
    actual_connect = module.sqlite3.connect
    connections = []
    class TracedConnection(module.sqlite3.Connection):
        closed = False
        def close(self):
            self.closed = True
            super().close()
    def tracked_connect(*args, **kwargs):
        connection = actual_connect(*args, factory=TracedConnection, **kwargs)
        connections.append(connection)
        return connection
    monkeypatch.setattr(module.sqlite3, "connect", tracked_connect)
    journal = AcquisitionJournal(tmp_path/"closing.sqlite3")
    attempt = response(1)
    journal.save(attempt)
    journal.responses(symbol=attempt.symbol, source_id=attempt.source_id,
                      interval_open=attempt.interval_open, interval_close=attempt.interval_close)
    with pytest.raises(ContractError, match="immutable"):
        journal.save(replace(attempt, error="changed after save"))
    assert len(connections) == 4 and all(connection.closed for connection in connections)
    for connection in connections:
        with pytest.raises(module.sqlite3.ProgrammingError, match="closed"):
            connection.execute("SELECT 1")
