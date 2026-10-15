"""Nightly orchestration checks use temporary local observations, no providers."""
from dataclasses import replace
import hashlib
import json
from pathlib import Path
import sqlite3
from types import SimpleNamespace

import numpy as np
import pandas as pd
import pytest

from ml.intraday.data import History, SessionBounds, build_dataset
from ml.intraday.models import ModelSpec
from ml.intraday.prediction import ContractError, STEP, TARGET_VERSION
from ml.intraday import supervisor

NOW = pd.Timestamp("2026-10-14T08:00:00+00:00")
PRIOR = SessionBounds("2026-10-13", "2026-10-13T08:00:00+00:00", "2026-10-14T00:00:00+00:00")


def history(symbol="AAPL"):
    rows = []
    for day in ("2026-10-09", "2026-10-12", "2026-10-13"):
        start = pd.Timestamp(day + "T08:00:00+00:00")
        for i in range(64):
            opened = start + i * STEP
            price = 100 + np.sin(i * .7)
            rows.append({"symbol": symbol, "opened_at": opened, "available_at": opened + STEP,
                "availability_kind": "assumed_historical_close", "open": price,
                "high": price + 1, "low": price - 1, "close": price + .1 * np.cos(i),
                "volume": 100 + i, "source_id": "databento:EQUS.MINI:ohlcv-1m", "source_dataset": "EQUS.MINI",
                "session_id": day, "session_open": start.isoformat(),
                "session_close": (start + pd.Timedelta(hours=16)).isoformat(), "synthetic": False})
    manifest = {"symbol": symbol, "source_dataset": "EQUS.MINI", "availability_kind": "assumed_historical_close",
                "last_minute_open": "2026-10-13T23:59:00+00:00", "files": []}
    frame = pd.DataFrame(rows)
    frame.attrs["intraday_history"] = manifest
    return History(frame, manifest)


def bindings(tmp_path, symbols=("AAPL",)):
    watch = tmp_path / "watchlist.local.txt"
    watch.write_text("\n".join(symbols) + "\n")
    profile = tmp_path / "local-profile.json"
    profile.write_text(json.dumps({"actor": "Atlas", "machine": "pc-original", "symbols": list(symbols),
                                  "symbol_profile_path": str(watch)}))
    return profile, watch


def refreshed(_root, symbols, **_kwargs):
    return {symbol: {"status": "refreshed", "source_dataset": "EQUS.MINI",
                     "requested_through": NOW.isoformat()} for symbol in symbols}


def chosen(dataset, **_kwargs):
    return SimpleNamespace(selected_models=(ModelSpec("regularized", family="logistic", parameters={"C": .05},
                                                    probability_calibration="sigmoid"),),
        report={"status": "selected", "training_rows": len(dataset.y)},
        holdout_start=dataset.reference_at[-30].isoformat())


def run(tmp_path, *, symbols=("AAPL",), refresh=refreshed, selector=chosen, loader=None):
    profile, watch = bindings(tmp_path, symbols)
    return supervisor.supervise(datastore=tmp_path, profile=profile, watchlist=watch,
        refresh=refresh, selector=selector, history_loader=loader or (lambda root, symbol, **kw: history(symbol)),
        clock=lambda: NOW)


def test_full_snapshot_keeps_freshest_mature_labels_after_last_inference_reference():
    source = history()
    dataset = supervisor.training_snapshot(source, cutoff=NOW)
    last = source.frame.opened_at.max()
    assert dataset.reference_at.max() == last - 2 * STEP
    assert dataset.label_available_at.max() == last + STEP
    assert dataset.target_close_at.max() == last + STEP
    assert dataset.manifest["freshest_candle_open"] == last.isoformat()
    live = build_dataset(source.frame, cutoff=NOW, reference_at=dataset.inference["reference_at"])
    assert dataset.reference_at.max() > live.reference_at.max()
    assert dataset.manifest["training_inputs"] == ["OHLCV"]


def test_training_snapshot_excludes_unavailable_labels_and_future_features():
    source = history()
    cutoff = source.frame.opened_at.iloc[-8] + STEP
    dataset = supervisor.training_snapshot(source, cutoff=cutoff)
    assert (dataset.label_available_at <= cutoff).all()
    assert (dataset.target_close_at <= cutoff).all()
    assert dataset.manifest["freshest_candle_open"] == source.frame.opened_at.iloc[-8].isoformat()
    delayed = history()
    delayed.frame.loc[30, "available_at"] = delayed.frame.available_at.iloc[31] + STEP
    filtered = supervisor.training_snapshot(delayed, cutoff=NOW)
    assert delayed.frame.opened_at.iloc[31] not in filtered.reference_at


@pytest.mark.parametrize("synthetic_policy", ["include", "exclude"])
def test_training_snapshot_records_actual_feature_receipt_after_target_close(synthetic_policy):
    source = history()
    reference = source.frame.opened_at.iloc[40]
    feature_receipt = reference + 4 * STEP
    source.frame.loc[40, "available_at"] = feature_receipt
    source.frame.loc[40, "availability_kind"] = "actual"
    dataset = supervisor.training_snapshot(source, cutoff=NOW, synthetic_policy=synthetic_policy)
    position = dataset.reference_at.get_loc(reference)
    assert dataset.target_close_at[position] < feature_receipt
    assert dataset.label_available_at[position] == feature_receipt
    # A fitting boundary after target close but before the real feature receipt
    # must still exclude this row when purging information availability.
    fit_cutoff = reference + 3 * STEP + pd.Timedelta(minutes=1)
    assert not (dataset.label_available_at[position] <= fit_cutoff)
    earlier = supervisor.training_snapshot(source, cutoff=fit_cutoff, synthetic_policy=synthetic_policy)
    assert reference not in earlier.reference_at


def test_training_snapshot_preserves_missing_intervals_and_rejects_wrong_targets():
    source = history()
    missing = source.frame.opened_at.iloc[-10]
    source = replace(source, frame=source.frame.drop(index=len(source.frame) - 10))
    dataset = supervisor.training_snapshot(source, cutoff=NOW)
    assert missing - 2 * STEP not in dataset.reference_at
    assert missing not in dataset.reference_at
    assert missing not in source.frame.opened_at.to_list()


def test_training_snapshot_synthetic_counts_include_fresh_closed_session_labels():
    source = history()
    source.frame.loc[len(source.frame) - 1, "synthetic"] = True
    included = supervisor.training_snapshot(source, cutoff=NOW, synthetic_policy="include")
    excluded = supervisor.training_snapshot(source, cutoff=NOW, synthetic_policy="exclude")
    assert included.manifest["synthetic_labels_included"] == 1
    assert excluded.manifest["synthetic_labels_excluded"] == 1
    assert excluded.manifest["synthetic_labels_included"] == 0
    assert len(included.y) == len(excluded.y) + 1


def test_previous_session_calendar_respects_weekends_and_pacific_night():
    assert supervisor.latest_completed_session(NOW).session_id == "2026-10-13"
    monday = supervisor.latest_completed_session("2026-10-12T08:00:00+00:00")
    assert monday.session_id == "2026-10-09"


def test_success_atomically_activates_portable_config_and_records_manual_start(tmp_path):
    report = run(tmp_path)
    assert report["status"] == "complete"
    assert report["symbols"]["AAPL"]["status"] == "activated"
    assert report["broker_calls"] == 0 and report["trader_started"] is False
    assert report["night"] == "2026-10-14"
    active = json.loads((tmp_path / supervisor.ACTIVE_PATH).read_text())
    assert active["actor"] == "Atlas" and active["target_version"] == TARGET_VERSION
    symbol = active["symbols"]["AAPL"]
    assert symbol["models"][0]["parameters"] == {"C": .05}
    assert symbol["models"][0]["probability_calibration"] == "sigmoid"
    assert symbol["feature_names"] == list(supervisor.training_snapshot(history(), cutoff=NOW).feature_names)
    from ml.intraday.selected_config import load_selected_config
    loaded = load_selected_config(tmp_path / supervisor.ACTIVE_PATH, pc="Atlas", symbols=("AAPL",),
                                  source_dataset="EQUS.MINI", as_of=NOW)
    assert loaded["AAPL"]["models"] == symbol["models"]
    events = [json.loads(line) for line in (tmp_path / "intraday/model-supervision/logs/events.jsonl").read_text().splitlines()]
    assert events[-1]["kind"] == "run_completed"
    assert events[1]["kind"] == "refresh_completed"
    assert not list(tmp_path.rglob("*.pending"))


def test_refresh_error_preserves_incumbent_and_other_symbols_continue(tmp_path):
    run(tmp_path, symbols=("AAPL", "NVDA"))
    active_path = tmp_path / supervisor.ACTIVE_PATH
    old = json.loads(active_path.read_text())["symbols"]["AAPL"]
    stop = tmp_path / "intraday/manual-session.json"
    stop.write_text('{"manual_stop":true}')
    def refresh(root, symbols, **kwargs):
        result = refreshed(root, symbols, **kwargs)
        result["AAPL"] = {"status": "error", "failure_type": "RuntimeError"}
        return result
    report = run(tmp_path, symbols=("AAPL", "NVDA"), refresh=refresh)
    assert report["status"] == "partial"
    assert report["symbols"]["AAPL"]["incumbent_retained"] is True
    assert report["symbols"]["NVDA"]["status"] == "activated"
    assert json.loads(active_path.read_text())["symbols"]["AAPL"] == old
    assert stop.read_text() == '{"manual_stop":true}'


def test_no_supported_candidate_retains_incumbent_without_stopping_nightly(tmp_path):
    run(tmp_path)
    before = (tmp_path / supervisor.ACTIVE_PATH).read_bytes()
    def no_candidate(dataset, **kwargs):
        return SimpleNamespace(selected_models=(), report={"status": "no_supported_candidate"},
                               holdout_start=dataset.reference_at[-30].isoformat())
    report = run(tmp_path, selector=no_candidate)
    assert report["status"] == "complete"
    assert report["symbols"]["AAPL"]["status"] == "incumbent_retained"
    assert (tmp_path / supervisor.ACTIVE_PATH).read_bytes() == before


def test_same_night_retries_preserve_inspected_holdout_boundary(tmp_path):
    seen = []
    def selector(dataset, previous_holdout_start=None, **kwargs):
        seen.append(previous_holdout_start)
        return chosen(dataset)
    run(tmp_path, selector=selector)
    run(tmp_path, selector=selector)
    assert seen[0] is None
    assert seen[1] == chosen(supervisor.training_snapshot(history(), cutoff=NOW)).holdout_start


def test_provider_coverage_error_does_not_train_stale_data(tmp_path):
    called = []
    def old(root, symbols, **kwargs):
        return {symbol: {"status": "refreshed", "requested_through": "2026-10-12T00:00:00+00:00"} for symbol in symbols}
    report = run(tmp_path, refresh=old, selector=lambda *a, **kw: called.append(True))
    assert report["status"] == "partial" and called == []
    assert not (tmp_path / supervisor.ACTIVE_PATH).exists()


def test_atomic_activation_error_never_publishes_failed_symbol_with_next_symbol(tmp_path, monkeypatch):
    run(tmp_path, symbols=("AAPL", "NVDA"))
    active_path = tmp_path / supervisor.ACTIVE_PATH
    before = json.loads(active_path.read_text())["symbols"]["AAPL"]
    real = supervisor._atomic_json
    attempts = []
    def writer(path, value):
        if Path(path) == active_path:
            attempts.append(value)
            if len(attempts) == 1:
                raise OSError("fixture disk failure")
        return real(path, value)
    monkeypatch.setattr(supervisor, "_atomic_json", writer)
    report = run(tmp_path, symbols=("AAPL", "NVDA"))
    assert report["symbols"]["AAPL"]["status"] == "error"
    assert report["symbols"]["NVDA"]["status"] == "activated"
    assert json.loads(active_path.read_text())["symbols"]["AAPL"] == before


def forecast_store(tmp_path):
    path = tmp_path / "intraday/shadow.sqlite3"
    path.parent.mkdir(parents=True)
    db = sqlite3.connect(path)
    db.executescript("CREATE TABLE forecasts(id TEXT,pc TEXT,symbol TEXT,payload TEXT);"
                     "CREATE TABLE outcomes(forecast_id TEXT,payload TEXT);")
    base = {"reference_at": "2026-10-13T12:00:00+00:00", "reference_open": 100,
        "target_open": "2026-10-13T12:30:00+00:00", "target_close": "2026-10-13T12:45:00+00:00",
        "mode": "live", "source_id": "databento:EQUS.MINI:ohlcv-1m", "source_dataset": "EQUS.MINI",
        "probability_not_down": .75, "models": [{"name": "gaussian_nb", "status": "success", "probability_not_down": 1.0}]}
    db.execute("INSERT INTO forecasts VALUES(?,?,?,?)", ("own", "Atlas", "AAPL", json.dumps(base)))
    db.execute("INSERT INTO forecasts VALUES(?,?,?,?)", ("peer", "Scout", "AAPL", json.dumps(base)))
    db.execute("INSERT INTO forecasts VALUES(?,?,?,?)", ("replay", "Atlas", "AAPL", json.dumps({**base, "mode": "historical_replay"})))
    db.execute("INSERT INTO outcomes VALUES(?,?)", ("own", json.dumps({"status": "scored", "target_not_down": False})))
    db.commit()
    db.close()
    return path


def test_previous_results_read_only_own_live_predictions_and_actual_fill_cashflow(tmp_path):
    path = forecast_store(tmp_path)
    before = path.read_bytes()
    ledger = tmp_path / "intraday/execution/holdings.sqlite3"
    ledger.parent.mkdir(parents=True)
    with sqlite3.connect(ledger) as db:
        db.executescript("CREATE TABLE allocations(id TEXT,symbol TEXT,horizon TEXT);"
            "CREATE TABLE reservations(id TEXT,allocation TEXT,side TEXT);"
            "CREATE TABLE fills(reservation TEXT,quantity INT,price TEXT,executed_at TEXT);")
        db.execute("INSERT INTO allocations VALUES('a','AAPL','15m')")
        db.execute("INSERT INTO reservations VALUES('r','a','BUY')")
        db.execute("INSERT INTO fills VALUES('r',2,'100','2026-10-13T12:01:00+00:00')")
    result = supervisor.session_results(tmp_path, "Atlas", "AAPL", PRIOR)
    assert result["forecasts"] == 1
    assert result["models"]["gaussian_nb"]["brier"] == 1.0
    assert result["ensemble"]["brier"] == .75 ** 2
    assert result["execution"]["buy_notional"] == 200 and result["execution"]["realized_pnl"] is None
    assert path.read_bytes() == before


def test_historical_reconstruction_never_overwrites_missing_live_receipts(tmp_path):
    path = forecast_store(tmp_path)
    with sqlite3.connect(path) as db:
        db.execute("DELETE FROM outcomes")
    before = path.read_bytes()
    result = supervisor.session_results(tmp_path, "Atlas", "AAPL", PRIOR, history=history())
    assert result["historical_reconstructions"] == 1 and result["recorded_outcomes"] == 0
    assert result["ensemble"]["scored"] == 1
    assert path.read_bytes() == before


def test_bad_execution_history_is_logged_but_does_not_gate_model_preparation(tmp_path):
    path = tmp_path / "intraday/execution/holdings.sqlite3"
    path.parent.mkdir(parents=True)
    path.write_bytes(b"fixture damaged ledger")
    report = run(tmp_path)
    assert report["symbols"]["AAPL"]["status"] == "activated"
    assert report["symbols"]["AAPL"]["previous_session_results"]["status"] == "error"


def test_refresh_reuses_overlap_native_minute_and_persistence_apis(tmp_path, monkeypatch):
    from app.services import databento_market_data
    from datafetching import databento_fetch
    calls = []
    spec = SimpleNamespace(schema="ohlcv-1m", frequency="1m")
    class Provider:
        dataset = "EQUS.MINI"
        def __init__(self, **kwargs):
            calls.append(("provider", kwargs))
        def native_specs(self):
            return (spec,)
    monkeypatch.setattr(databento_market_data, "DatabentoMarketDataProvider", Provider)
    monkeypatch.setattr(databento_fetch, "_load_repository_environment", lambda: None)
    coverage = SimpleNamespace(start=NOW - pd.Timedelta(days=1), end=NOW)
    def fetch(provider, symbols, profile, store):
        calls.append(("fetch", symbols, profile))
        return {symbol: [(spec, [object()], None, coverage, None)] for symbol in symbols}
    def persist(symbol, store, **kwargs):
        calls.append(("persist", symbol, kwargs["run_derived"], kwargs["profile"]))
        return SimpleNamespace(data_files=2, error_files=0)
    monkeypatch.setattr(databento_fetch, "_fetch_native_results_many", fetch)
    monkeypatch.setattr(databento_fetch, "_persist_native_results", persist)
    result = supervisor.refresh_ohlcv(tmp_path, ("AAPL", "NVDA"), clock=lambda: NOW)
    assert calls[0] == ("provider", {"native_schemas": ("ohlcv-1m",)})
    assert calls[1] == ("fetch", ("AAPL", "NVDA"), "continuation")
    assert all(not call[2] for call in calls if call[0] == "persist")
    assert result["AAPL"]["requested_through"] == NOW.isoformat()
    assert result["NVDA"]["status"] == "refreshed"


def test_refresh_persistence_failure_isolated_to_own_symbol(tmp_path, monkeypatch):
    from app.services import databento_market_data
    from datafetching import databento_fetch
    spec = SimpleNamespace(schema="ohlcv-1m", frequency="1m")
    class Provider:
        dataset = "EQUS.MINI"
        def __init__(self, **kwargs):
            pass
        def native_specs(self):
            return (spec,)
    coverage = SimpleNamespace(start=NOW - pd.Timedelta(days=1), end=NOW)
    monkeypatch.setattr(databento_market_data, "DatabentoMarketDataProvider", Provider)
    monkeypatch.setattr(databento_fetch, "_load_repository_environment", lambda: None)
    monkeypatch.setattr(databento_fetch, "_fetch_native_results_many", lambda provider, symbols, profile, store:
        {symbol: [(spec, [], None, coverage, None)] for symbol in symbols})
    seen = []
    def persist(symbol, store, **kwargs):
        seen.append(symbol)
        if symbol == "AAPL":
            raise OSError("Corrupt AAPL fixture parquet")
        return SimpleNamespace(data_files=2, error_files=0)
    monkeypatch.setattr(databento_fetch, "_persist_native_results", persist)
    result = supervisor.refresh_ohlcv(tmp_path, ("AAPL", "NVDA"), clock=lambda: NOW)
    assert seen == ["AAPL", "NVDA"]
    assert result["AAPL"]["status"] == "error" and result["AAPL"]["failure_type"] == "OSError"
    assert result["AAPL"]["message"] == "Corrupt AAPL fixture parquet"
    assert result["NVDA"]["status"] == "refreshed"


def test_invalid_lookback_rejected_before_data_mutation(tmp_path):
    profile, watch = bindings(tmp_path)
    called = []
    with pytest.raises(ContractError, match="positive integer"):
        supervisor.supervise(datastore=tmp_path, profile=profile, watchlist=watch,
            config={"lookback_days": .5}, refresh=lambda *a, **kw: called.append(True), clock=lambda: NOW)
    assert called == []


def test_broken_selection_state_is_preserved_and_nightly_work_continues(tmp_path):
    path = tmp_path / supervisor.STATE_PATH
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"schema_version": supervisor.VERSION, "actor": "Scout", "nights": {}}))
    before = path.read_bytes()
    report = run(tmp_path)
    assert report["symbols"]["AAPL"]["status"] == "activated"
    backup = Path(report["metadata_repairs"]["state"]["preserved_path"])
    assert backup.read_bytes() == before
    assert json.loads(path.read_text())["actor"] == "Atlas"


def test_broken_active_preserved_without_stalling_refresh_and_activation(tmp_path):
    path = tmp_path / supervisor.ACTIVE_PATH
    path.parent.mkdir(parents=True)
    path.write_bytes(b"{broken JSON fixture")
    report = run(tmp_path)
    backup = Path(report["metadata_repairs"]["active"]["preserved_path"])
    assert backup.read_bytes() == b"{broken JSON fixture"
    assert report["symbols"]["AAPL"]["status"] == "activated"
    assert json.loads(path.read_text())["symbols"]["AAPL"]["max_training_rows"] == 3000


@pytest.mark.parametrize("broken_entry", [None, {"selection_id": "broken"},
    {"selection_id": "broken", "selected_at": "invalid", "models": []},
    {"selection_id": "broken", "selected_at": NOW.isoformat(), "models": [{"name": "bad", "training_max_rows": 0}]}])
def test_malformed_active_symbol_preserves_unaffected_incumbents_and_original_evidence(tmp_path, broken_entry):
    symbols = ("AAPL", "NVDA", "IONQ")
    run(tmp_path, symbols=symbols)
    path = tmp_path / supervisor.ACTIVE_PATH
    active = json.loads(path.read_text())
    old = {symbol: active["symbols"][symbol] for symbol in ("AAPL", "IONQ")}
    active["symbols"]["NVDA"] = broken_entry
    path.write_text(json.dumps(active))
    before = path.read_bytes()

    def refresh(root, symbols, **kwargs):
        result = refreshed(root, symbols, **kwargs)
        for symbol in old:
            result[symbol] = {"status": "error", "failure_type": "FixtureRefreshError"}
        return result

    report = run(tmp_path, symbols=symbols, refresh=refresh)
    assert report["symbols"]["NVDA"]["status"] == "activated"
    saved = json.loads(path.read_text())
    for symbol, incumbent in old.items():
        assert saved["symbols"][symbol] == incumbent
        assert report["symbols"][symbol]["incumbent_retained"] is True
    repair = report["metadata_repairs"]["active"]
    assert set(repair["symbol_errors"]) == {"NVDA"}
    assert repair["symbol_errors"]["NVDA"]["failure_type"]
    assert Path(repair["preserved_path"]).read_bytes() == before
    assert repair["sha256"] == hashlib.sha256(before).hexdigest()
    events = [json.loads(line) for line in (tmp_path / "intraday/model-supervision/logs/events.jsonl").read_text().splitlines()]
    recovered = [event for event in events if event["kind"] == "metadata_recovered"][-1]
    assert recovered["symbol_errors"] == repair["symbol_errors"]
    assert recovered["sha256"] == repair["sha256"]


def test_failure_details_redact_credentials(monkeypatch):
    monkeypatch.setenv("DATABENTO_API_KEY", "private-example-credential")
    result = supervisor._failure(RuntimeError("request rejected private-example-credential token=secret-test-data"))
    assert result["failure_type"] == "RuntimeError"
    assert "request rejected" in result["message"]
    assert "private-example-credential" not in result["message"] and "secret-test-data" not in result["message"]
