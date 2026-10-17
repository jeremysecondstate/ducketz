"""Recovery adds real receipts without revising an old recipe or old prices."""
import hashlib
import json

import pandas as pd
import pytest

from ml.intraday.data import History, SessionBounds, validate_candles
from ml.intraday.native_interval import (
    RECOVERY_CONTRACT, _candle, build_native_interval_dataset,
    completed_native_interval,
)
from ml.intraday.native_recovery import merge_recovered_history
from ml.intraday.prediction import ContractError

OPEN = pd.Timestamp("2026-05-11T14:00Z")
RECEIVED = pd.Timestamp("2026-10-17T18:00Z")
SESSION = SessionBounds("2026-05-11", "2026-05-11T08:00Z", "2026-05-12T00:00Z")


def row(at, price=100):
    return dict(opened_at=at.isoformat(), open=float(price), high=float(price + 2),
                low=float(price - 1), close=float(price + 1), volume=10.)


def original():
    coverage = dict(kind="historical_native_raw_range", status="success", symbol="TEST",
        source_dataset="EQUS.MINI", start=SESSION.opened_at, end=SESSION.closed_at,
        observation_basis="canonical_native_file_after_successful_fetch_merges",
        raw_sha256="a" * 64, normalized_sha256="b" * 64)
    candle = _candle(symbol="TEST", source_dataset="EQUS.MINI", opened=OPEN,
        available=OPEN + pd.Timedelta(minutes=15), kind="assumed_historical_close",
        session=SESSION, rows=[row(OPEN)], coverage=coverage)
    return History(validate_candles(pd.DataFrame([candle])), {"candle_rows": 1})


def installed(tmp_path, monkeypatch, observations):
    import ml.intraday.native_recovery as recovery
    folder = tmp_path / "intraday/scout/minute-recovery"
    request_dir = folder / "attempt"
    request_dir.mkdir(parents=True)
    request = dict(dataset="EQUS.MINI", schema="ohlcv-1m", symbols=["TEST"],
        start=SESSION.opened_at, end=SESSION.closed_at, stype_in="raw_symbol")
    receipt = dict(request=request, request_id=hashlib.sha256(json.dumps(request, sort_keys=True).encode()).hexdigest(),
        status="success", started_at="2026-10-17T17:59:00Z", received_at=RECEIVED.isoformat(), rows=len(observations))
    entry = {"path": "attempt/receipt.json"}
    for name, key in (("receipt.json", "receipt_sha256"), ("raw.dbn", "raw_sha256"), ("native.parquet", "normalized_sha256")):
        payload = json.dumps(receipt).encode() if name == "receipt.json" else name.encode()
        (request_dir / name).write_bytes(payload)
        entry[key] = hashlib.sha256(payload).hexdigest()
    frame = pd.DataFrame([{**r, "symbol": "TEST"} for r in observations])
    frame["opened_at"] = pd.to_datetime(frame.opened_at, utc=True)
    monkeypatch.setattr(recovery, "_verified_receipt", lambda *args: (receipt, frame))
    (folder / "installed.json").write_text(json.dumps(dict(contract=RECOVERY_CONTRACT,
        pc="Scout", symbols=["TEST"], receipts=[entry])))
    return folder


def test_receipt_adds_new_quarter_with_actual_clock_and_no_legacy_opt_in(tmp_path, monkeypatch):
    installed(tmp_path, monkeypatch, [row(OPEN), row(OPEN + pd.Timedelta(minutes=15))])
    history = merge_recovered_history(original(), root=tmp_path, symbol="TEST", pc="Scout")
    added = history.frame.loc[history.frame.availability_kind.eq("actual_historical_recovery")].iloc[0].to_dict()
    assert added["available_at"] == RECEIVED
    assert pd.Timestamp(added["feature_cutoff_at"]) == OPEN + pd.Timedelta(minutes=30)
    assert completed_native_interval(added)
    assert history.manifest["recovery"]["new_quarters"] == 1
    assert history.frame.iloc[0].availability_kind == "assumed_historical_close"
    with pytest.raises(ContractError, match="explicit local recipe opt-in"):
        build_native_interval_dataset(history.frame, cutoff=RECEIVED, reference_at=OPEN)


def test_exact_repeat_does_not_change_existing_receipt_or_snapshot(tmp_path, monkeypatch):
    installed(tmp_path, monkeypatch, [row(OPEN)])
    before = original()
    after = merge_recovered_history(before, root=tmp_path, symbol="TEST", pc="Scout")
    pd.testing.assert_frame_equal(before.frame, after.frame)
    assert after.manifest["recovery"]["identical_snapshots"] == 1


def test_extra_observation_replaces_only_complete_compatible_snapshot(tmp_path, monkeypatch):
    installed(tmp_path, monkeypatch, [row(OPEN), row(OPEN + pd.Timedelta(minutes=7), 105)])
    history = merge_recovered_history(original(), root=tmp_path, symbol="TEST", pc="Scout")
    candle = history.frame.iloc[0]
    assert candle.observed_constituents == 2 and candle.close == 106
    assert candle.available_at == RECEIVED
    assert history.manifest["recovery"]["revised_quarters"] == 1


@pytest.mark.parametrize("observations", [[row(OPEN, 99)], [row(OPEN + pd.Timedelta(minutes=1))]])
def test_conflicting_or_omitted_original_is_retained(tmp_path, monkeypatch, observations):
    installed(tmp_path, monkeypatch, observations)
    before = original()
    after = merge_recovered_history(before, root=tmp_path, symbol="TEST", pc="Scout")
    pd.testing.assert_frame_equal(before.frame, after.frame)
    assert len(after.manifest["recovery"]["conflicts_retained"]) == 1


def test_mutated_immutable_payload_is_rejected_before_cached_read(tmp_path, monkeypatch):
    folder = installed(tmp_path, monkeypatch, [row(OPEN)])
    (folder / "attempt/raw.dbn").write_bytes(b"changed")
    with pytest.raises(ContractError, match="immutable bytes changed"):
        merge_recovered_history(original(), root=tmp_path, symbol="TEST", pc="Scout")


def test_tampered_recovery_clock_fails_candle_validation(tmp_path, monkeypatch):
    installed(tmp_path, monkeypatch, [row(OPEN), row(OPEN + pd.Timedelta(minutes=7))])
    candle = merge_recovered_history(original(), root=tmp_path, symbol="TEST", pc="Scout").frame.iloc[0].to_dict()
    candle["available_at"] = OPEN + pd.Timedelta(minutes=15)
    assert not completed_native_interval(candle)


def test_manifest_cannot_escape_private_recovery_directory(tmp_path, monkeypatch):
    folder = installed(tmp_path, monkeypatch, [row(OPEN)])
    path = folder / "installed.json"
    value = json.loads(path.read_text())
    value["receipts"][0]["path"] = "../outside/receipt.json"
    path.write_text(json.dumps(value))
    with pytest.raises(ContractError, match="escapes"):
        merge_recovered_history(original(), root=tmp_path, symbol="TEST", pc="Scout")


def live_receipt(tmp_path, attempt, delay):
    directory = tmp_path / "intraday/scout/minute-fetches" / attempt
    directory.mkdir(parents=True)
    native = row(OPEN)
    pd.DataFrame([{**native, "symbol": "TEST"}]).rename(columns={"opened_at": "ts_event"}).to_parquet(directory / "raw-000.parquet", index=False)
    request = dict(pc="Scout", symbols=["TEST"], dataset="EQUS.MINI", schema="ohlcv-1m",
        start=OPEN.isoformat(), end=(OPEN + pd.Timedelta(minutes=15)).isoformat(),
        requested_at=(OPEN + pd.Timedelta(minutes=15, seconds=delay)).isoformat(),
        observed_at=(OPEN + pd.Timedelta(minutes=15, seconds=delay + 1)).isoformat(),
        range_policy="single_completed_quarter")
    bar = {**{k: native[k] for k in ("open", "high", "low", "close", "volume")},
        "timestamp": OPEN.isoformat(), "symbol": "TEST", "source": "databento", "timeframe": "1m"}
    response = dict(version="intraday-native-minute-persistence-v1", attempt_id=attempt,
        request=request, selected_range={k: request[k] for k in ("start", "end", "schema")},
        symbols=[dict(symbol="TEST", bars=[bar], raw=dict(file="raw-000.parquet", rows=1,
            sha256=hashlib.sha256((directory / "raw-000.parquet").read_bytes()).hexdigest()))])
    persistence = dict(version=response["version"], pc="Scout", status="complete",
        start=request["start"], end=request["end"], symbols={"TEST": {"status": "complete"}})
    entry = {"path": f"minute-fetches/{attempt}/response.json"}
    for name, value, key in (("response.json", response, "response_sha256"),
                             ("persistence.json", persistence, "persistence_sha256")):
        payload = json.dumps(value).encode()
        (directory / name).write_bytes(payload)
        entry[key] = hashlib.sha256(payload).hexdigest()
    return entry


@pytest.mark.parametrize("count", [1, 2])
def test_pinned_live_receipts_keep_original_actual_clock_and_sparse_wait(tmp_path, monkeypatch, count):
    folder = installed(tmp_path, monkeypatch, [row(OPEN)])
    manifest = json.loads((folder / "installed.json").read_text())
    manifest["actual_receipts"] = [live_receipt(tmp_path, f"live-{i}", 2 + i * 5) for i in range(count)]
    (folder / "installed.json").write_text(json.dumps(manifest))
    history = merge_recovered_history(original(), root=tmp_path, symbol="TEST", pc="Scout")
    evidence = history.manifest["actual_receipt_overlay"]
    if count == 1:
        assert evidence["actual_candles_restored"] == 0
        assert history.frame.iloc[0].availability_kind == "assumed_historical_close"
    else:
        assert evidence["actual_candles_restored"] == 1
        assert history.frame.iloc[0].availability_kind == "actual"
        assert history.frame.iloc[0].available_at == OPEN + pd.Timedelta(minutes=15, seconds=8)


def test_pinned_live_receipt_does_not_hide_raw_mutation(tmp_path, monkeypatch):
    folder = installed(tmp_path, monkeypatch, [row(OPEN)])
    manifest = json.loads((folder / "installed.json").read_text())
    manifest["actual_receipts"] = [live_receipt(tmp_path, "live-0", 2)]
    (folder / "installed.json").write_text(json.dumps(manifest))
    (tmp_path / "intraday/scout/minute-fetches/live-0/raw-000.parquet").write_bytes(b"changed")
    with pytest.raises(ContractError, match="Actual minute raw bytes changed"):
        merge_recovered_history(original(), root=tmp_path, symbol="TEST", pc="Scout")


def test_older_pinned_live_receipts_preserve_a_newer_actual_snapshot(tmp_path, monkeypatch):
    folder = installed(tmp_path, monkeypatch, [row(OPEN)])
    manifest = json.loads((folder / "installed.json").read_text())
    manifest["actual_receipts"] = [live_receipt(tmp_path, f"live-{i}", 2 + i * 5) for i in range(2)]
    (folder / "installed.json").write_text(json.dumps(manifest))
    first = merge_recovered_history(original(), root=tmp_path, symbol="TEST", pc="Scout")
    candle = first.frame.iloc[0].to_dict()
    evidence = json.loads(candle["evidence_json"])
    evidence["coverage"].update(attempt_id="newer",
        request_started_at=(OPEN + pd.Timedelta(minutes=15, seconds=12)).isoformat(),
        request_ended_at=(OPEN + pd.Timedelta(minutes=15, seconds=13)).isoformat())
    candle["available_at"] = OPEN + pd.Timedelta(minutes=15, seconds=13)
    candle["evidence_json"] = json.dumps(evidence)
    assert completed_native_interval(candle)
    newer = History(validate_candles(pd.DataFrame([candle])), first.manifest)
    final = merge_recovered_history(newer, root=tmp_path, symbol="TEST", pc="Scout")
    pd.testing.assert_frame_equal(final.frame, newer.frame)
    assert len(final.manifest["actual_receipt_overlay"]["preserved_newer_actual"]) == 1
