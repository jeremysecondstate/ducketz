from datetime import datetime, timedelta, timezone
import hashlib
import json

from ml.intraday.market_inputs import MODEL_FEATURE_NAMES, MarketInputStore, augment_snapshot

T = datetime(2026, 10, 16, 16, tzinfo=timezone.utc)


def test_failed_symbol_retry_preserves_completed_selection_and_full_bindings(tmp_path):
    from test_intraday_supervisor import bindings, history, chosen, refreshed, NOW
    from ml.intraday import supervisor
    profile, watch = bindings(tmp_path, ("AAPL", "AMZN"))
    kwargs = dict(datastore=tmp_path, profile=profile, watchlist=watch, refresh=refreshed,
                  history_loader=lambda root, symbol, **kw: history(symbol), selector=chosen, clock=lambda: NOW)
    supervisor.supervise(**kwargs)
    active = tmp_path/supervisor.ACTIVE_PATH
    before = json.loads(active.read_text())["symbols"]["AMZN"]
    requested = []
    def retry_refresh(root, symbols, **kw):
        requested.extend(symbols)
        return refreshed(root, symbols, **kw)
    report = supervisor.supervise(**{**kwargs, "refresh": retry_refresh}, only_symbols=("AAPL",))
    assert requested == ["AAPL"] and list(report["symbols"]) == ["AAPL"]
    assert report["configured_symbols"] == ["AAPL", "AMZN"]
    assert json.loads(active.read_text())["symbols"]["AMZN"] == before
    import pytest
    from ml.intraday.prediction import ContractError
    for invalid in ((), ("OTHER",), ("AAPL", "AAPL"), "AAPL"):
        with pytest.raises(ContractError):
            supervisor.supervise(**kwargs, only_symbols=invalid)


def test_historical_overlay_never_changes_inference_or_original_receipts(tmp_path):
    store = MarketInputStore(tmp_path)
    store.append(symbol="AAPL", source="db_cme_es", started_at=T, received_at=T,
                 payload={"native": True}, features={"spread_relative": .1})
    names = list(MODEL_FEATURE_NAMES)
    spread, depth = [names.index("market_db_cme_es_"+field) for field in ("spread_relative", "depth_imbalance")]
    class History:
        def join(self, decisions, *, cutoff, purpose):
            assert decisions == [T] and purpose == "retrospective_training"
            row = [0.] * len(names)
            row[spread:spread+2], row[depth:depth+2] = [.2, 1], [.3, 1]
            return {"rows": [row], "feature_names": names, "version": "test-reconstruction",
                    "artifact": {"sha256": "a"*64}, "receipt_rule": "reconstructed", "provenance": [{"sources": {}}]}
    snapshot = {"X": [[1]], "reference_at": [(T-timedelta(minutes=15)).isoformat()],
                "inference_X": [[2]], "feature_names": ["price"], "manifest": {}}
    joined = augment_snapshot(snapshot, store, symbol="AAPL", inference_at=T, cutoff=T, historical=History())
    assert joined["X"][0][spread+1:spread+3] == [.1, 1]
    assert joined["X"][0][depth+1:depth+3] == [.3, 1]
    assert joined["inference_X"][0][depth+1:depth+3] == [0, 0]
    assert snapshot["X"] == [[1]]
    assert joined["manifest"]["feature_contract"]["historical_market_inputs"] == "test-reconstruction"


def test_receipt_retains_loaded_producer_and_public_rules(tmp_path):
    store = MarketInputStore(tmp_path)
    receipt = store.append(symbol="AAPL", source="fmp_quote", started_at=T, received_at=T,
                           payload={"price": 1}, features={"price": 1})
    ref = receipt["producer_runtime"]
    raw = (store.root/ref["path"]).read_bytes()
    assert hashlib.sha256(raw).hexdigest() == ref["sha256"]
    producer = json.loads(raw)
    assert producer["runtime"]["modules"]["ml.intraday.market_inputs"]["status"] == "captured"
    assert producer["rules"]["source_fields"]["fmp_quote"]
    assert "environment" not in producer


def test_manual_market_worker_retries_between_quarters_without_new_collection():
    from ml.intraday.market_input_worker import run_market_input_worker
    calls, events, ticks = [], [], []
    class Collector:
        def collect(self, reference):
            calls.append("collect")
            return {"reference": reference.isoformat()}
        def recover_pending(self):
            calls.append("retry")
            return {"recovered": 1}
    run_market_input_worker(Collector(), clock=lambda: T, stopped=lambda: len(ticks) >= 3,
        pause=lambda seconds: ticks.append(seconds), reference_for=lambda now: now,
        event=lambda kind, value: events.append(kind))
    assert calls == ["collect", "retry", "retry"]
    assert events == ["market_inputs_collected", "market_inputs_retry_completed", "market_inputs_retry_completed"]


def test_missing_historical_context_keeps_price_forecast_inputs(tmp_path):
    class Missing:
        def join(self, *args, **kwargs):
            raise FileNotFoundError("missing private archive")
    snapshot = {"X": [[1]], "reference_at": [(T-timedelta(minutes=15)).isoformat()],
                "inference_X": [[2]], "feature_names": ["price"], "manifest": {}}
    out = augment_snapshot(snapshot, MarketInputStore(tmp_path), symbol="AAPL", inference_at=T,
                           cutoff=T, historical=Missing())
    assert out["X"][0][0] == 1 and out["inference_X"][0][0] == 2
    assert out["market_inputs"]["historical_reconstruction"]["read_status"]["status"] == "unavailable"


def test_live_empty_source_uses_real_alternate_and_restores_catalog(tmp_path, monkeypatch):
    import pandas as pd
    from test_intraday_recovered_population import primary, assessment, bar, START, NOW
    from ml.intraday.data import History, AcquisitionJournal
    from ml.intraday.runtime import LiveHistories
    from ml.intraday.store import ShadowStore
    from ml.intraday.recovered_population import POPULATION_CONTRACT, load_recovery_candles
    import ml.intraday.data as data
    import ml.intraday.gap_recovery as gap
    empty = pd.DataFrame(columns=list(primary()))
    for name in ("opened_at", "available_at"):
        empty[name] = pd.Series(dtype="datetime64[ns, UTC]")
    empty["synthetic"] = pd.Series(dtype=bool)
    histories = {"TEST": History(empty, {"source_dataset": "EQUS.MINI", "population_contract": POPULATION_CONTRACT})}
    calls = []
    def native(provider, **kw):
        calls.append(kw["population_contract"])
        return {"status": "unavailable", "reason": "source_empty", "final": True, "candle": None}
    monkeypatch.setattr(data, "recover_databento_candle_step", native)
    monkeypatch.setattr(gap.GapRecovery, "recover", lambda *args: {"status": "recovered", "assessment": assessment([bar()])})
    provider = type("Provider", (), {"dataset": "EQUS.MINI"})()
    store = ShadowStore(tmp_path/"intraday/shadow.sqlite3")
    incoming = LiveHistories(histories=histories, provider=provider, store=store,
        journal=AcquisitionJournal(tmp_path/"intraday/acquisition.sqlite3"), reference_at=START,
        clock=lambda: NOW, bounded=True, population_contract=POPULATION_CONTRACT)
    updated = incoming.get("TEST")
    assert calls == ["intraday-verified-source-observations-v1"]
    assert updated.frame.iloc[-1].source_dataset == "MULTI_PROVIDER"
    assert updated.frame.iloc[-1].native_source_id == "schwab:native"
    assert not updated.frame.iloc[-1].synthetic
    saved = load_recovery_candles(tmp_path, "TEST", cutoff=NOW)
    assert len(saved) == 1 and saved[0]["observed_constituents"] == 1
