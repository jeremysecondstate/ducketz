"""Bound scoring decodes while retaining immutable evidence and corrections."""
import json
import hashlib
import time
from types import SimpleNamespace

import pandas as pd
import pytest

from ml.intraday import live_session, store as store_module
from ml.intraday.prediction import ContractError, digest, instant
from ml.intraday.store import ShadowStore
from tests.test_intraday_store import candle, forecast


NOW = "2026-10-08T18:00:00Z"


def selection(store, candidates=(), **kwargs):
    by_target = {}
    for value in candidates:
        by_target.setdefault((value["symbol"], instant(value["opened_at"])), []).append(value)
    return list(store.scoring_forecasts(pc="Atlas", observed_candidates=by_target, as_of=NOW, **kwargs))


def spy_decodes(store, monkeypatch):
    original = store._forecast_value
    decoded = []
    def checked(row):
        decoded.append(row[0])
        return original(row)
    monkeypatch.setattr(store, "_forecast_value", checked)
    return decoded


def test_large_terminal_history_decodes_only_unresolved_and_corrected(tmp_path, monkeypatch):
    store = ShadowStore(tmp_path / "scoring.sqlite3")
    saved = []
    targets = []
    for index in range(8):
        symbol = "S" + str(index)
        value = store.save_forecast(forecast(symbol=symbol, mode="live", cycle_id=symbol,
            model_diagnostics={"large": "retained-model-metadata" * 10000, "tiny": 1e-7, "unicode": "\u96ea"}))
        saved.append(value)
        targets.append(candle(symbol=symbol))
        if index < 7:
            store.observe(value, as_of=NOW, candle=targets[-1])
    decoded = spy_decodes(store, monkeypatch)
    # Reproduce the previous full inventory decode independently of timing.
    assert len(store.forecasts(pc="Atlas")) == 8
    assert decoded == [value["forecast_id"] for value in saved]
    decoded.clear()
    assert selection(store, targets) == [saved[-1]]
    assert decoded == [value["forecast_id"] for value in saved]
    decoded.clear()
    assert selection(store, targets) == [saved[-1]]
    assert decoded == [saved[-1]["forecast_id"]]
    corrected = candle(symbol="S2", close="99", available_at="2026-10-08T17:58:00+00:00")
    decoded.clear()
    assert selection(store, [*targets, corrected]) == [saved[2], saved[-1]]
    assert decoded == [saved[2]["forecast_id"], saved[-1]["forecast_id"]]
    store.observe(saved[2], as_of=NOW, candle=corrected)
    assert selection(store, [*targets, corrected]) == [saved[-1]]


@pytest.mark.parametrize("encoding", ["canonical", "indent", "unicode", "reordered"])
def test_alternate_json_encodings_fall_back_without_losing_validation(tmp_path, monkeypatch, encoding):
    store = ShadowStore(tmp_path / "encoding.sqlite3")
    value = store.save_forecast(forecast(mode="live", diagnostics={"float": 1e-7, "unicode": "\u96ea"}))
    target = candle()
    store.observe(value, as_of=NOW, candle=target)
    if encoding != "canonical":
        payload = dict(reversed(list(value.items()))) if encoding == "reordered" else value
        raw = json.dumps(payload, sort_keys=encoding != "reordered", ensure_ascii=encoding != "unicode",
                         indent=2 if encoding == "indent" else None, separators=(",", ":"))
        with store.connect() as db:
            db.execute("UPDATE forecasts SET payload=? WHERE id=?", (raw, value["forecast_id"]))
    decoded = spy_decodes(store, monkeypatch)
    assert selection(store, [target]) == []
    assert decoded == [value["forecast_id"]]  # Every cold encoding is validated.
    decoded.clear()
    assert selection(store, [target]) == [] and decoded == []


@pytest.mark.parametrize("damage", ["body", "index", "forecast_id", "old_outcome"])
def test_terminal_selection_does_not_hide_corrupted_evidence(tmp_path, damage):
    store = ShadowStore(tmp_path / "corrupt.sqlite3")
    value = store.save_forecast(forecast(mode="live"))
    initial = store.observe(value, as_of="2026-10-08T17:46:00Z")
    store.observe(value, as_of=NOW, candle=candle())
    assert selection(store, [candle()]) == []  # Warm cached headers cannot hide damage.
    with store.connect() as db:
        if damage == "body":
            db.execute("UPDATE forecasts SET payload=json_set(payload,'$.reference_open','999')")
        elif damage == "index":
            db.execute("UPDATE forecasts SET reference_at='2026-10-08T16:45:00+00:00'")
        elif damage == "forecast_id":
            db.execute("UPDATE forecasts SET payload=json_set(payload,'$.forecast_id','wrong')")
        else:
            db.execute("UPDATE outcomes SET payload=json_set(payload,'$.status','wrong') WHERE id=?",
                       (initial["revision_id"],))
    with pytest.raises(ContractError, match="hash or indexed identity"):
        selection(store, [candle()])


def test_live_due_scope_preserves_source_population_and_future_receipts(tmp_path):
    store = ShadowStore(tmp_path / "scope.sqlite3")
    live = store.save_forecast(forecast(mode="live", source_id="source", source_dataset="EQUS.MINI"))
    store.observe(live, as_of=NOW, unavailable_reason="retained unavailable")
    store.save_forecast(forecast(mode="shadow", cycle_id="shadow"))
    store.save_forecast(forecast(mode="live", pc="Scout", cycle_id="peer"))
    store.save_forecast(forecast(mode="live", cycle_id="future", reference_at="2026-10-08T18:00:00+00:00",
        target_open="2026-10-08T18:30:00+00:00", target_close="2026-10-08T18:45:00+00:00",
        data_through="2026-10-08T18:15:00+00:00", issued_at="2026-10-08T18:16:00+00:00"))
    assert selection(store) == []
    matching = candle(source_dataset="EQUS.MINI")
    for changes in ({"source_id": "other"}, {"source_dataset": "other"}, {"population_contract": "other"},
                    {"available_at": "2026-10-08T18:01:00+00:00"}):
        assert selection(store, [{**matching, **changes}]) == []
    assert selection(store, [matching]) == [live]
    # An unavailable newer revision does not obscure the available correction.
    assert selection(store, [matching, {**matching, "available_at": "2026-10-08T18:01:00+00:00"}]) == [live]


def test_manual_stop_and_future_observation_do_not_claim_completion(tmp_path, monkeypatch):
    store = ShadowStore(tmp_path / "stop.sqlite3")
    value = store.save_forecast(forecast(mode="live"))
    target = candle()
    store.observe(value, as_of="2026-10-08T18:01:00Z", candle=target)
    decoded = spy_decodes(store, monkeypatch)
    assert selection(store, [target], stopped=lambda: True) == []
    assert decoded == []
    # Preserve observe()'s no-backdating error even for the same target candle.
    assert selection(store, [target]) == [value]
    with pytest.raises(ContractError, match="backdated"):
        store.observe(value, as_of=NOW, candle=target)


def test_malformed_json_uses_existing_full_validation_error(tmp_path):
    store = ShadowStore(tmp_path / "malformed.sqlite3")
    store.save_forecast(forecast(mode="live"))
    with store.connect() as db:
        db.execute("UPDATE forecasts SET payload='not json'")
    with pytest.raises(json.JSONDecodeError):
        selection(store)


def test_noncanonical_raw_hash_is_not_a_semantic_forecast_identity(tmp_path):
    store = ShadowStore(tmp_path / "raw-hash.sqlite3")
    invalid = dict(reversed(list(forecast(mode="live", target_close="2026-10-08T18:45:00Z").items())))
    invalid["forecast_id"] = hashlib.sha256(json.dumps(invalid, separators=(",", ":")).encode()).hexdigest()
    with store.connect() as db:
        db.execute("INSERT INTO forecasts VALUES(?,?,?,?,?,?,?)",
            (*(invalid[k] for k in ("forecast_id", "pc", "symbol", "cycle_id", "reference_at", "issued_at")),
             json.dumps(invalid, separators=(",", ":"))))
    with pytest.raises(ContractError, match="hash or indexed identity"):
        store.forecasts(pc="Atlas")
    with pytest.raises(ContractError, match="hash or indexed identity"):
        selection(store)


def test_scoring_changed_valid_blob_revalidates_and_preserves_selection(tmp_path, monkeypatch):
    store = ShadowStore(tmp_path / "blob.sqlite3")
    value = store.save_forecast(forecast(mode="live"))
    store.observe(value, as_of=NOW, candle=candle())
    assert selection(store, [candle()]) == []
    with store.connect() as db:
        db.execute("UPDATE forecasts SET payload=CAST(payload AS BLOB)")
    decoded = spy_decodes(store, monkeypatch)
    assert selection(store, [candle()]) == []
    assert decoded == [value["forecast_id"]]


def test_scoring_loop_matches_full_inventory_outcomes_events_and_acquisition(tmp_path, monkeypatch):
    """Run old full-inventory selection and the new path on identical evidence."""
    monkeypatch.setattr(store_module, "utcnow", lambda: NOW)
    results, measurements = {}, {}
    for strategy in ("old_full_inventory", "selected_work"):
        store = ShadowStore(tmp_path / (strategy + ".sqlite3"))
        forecasts, targets = {}, {}
        # Keep this reproduction bounded while retaining model-payload cost.
        for symbol in ("UNRESOLVED", "UNCHANGED", "CORRECTED", "SOURCE", "POPULATION", "FUTURE", "UNAVAILABLE"):
            forecasts[symbol] = store.save_forecast(forecast(mode="live", symbol=symbol, cycle_id=symbol,
                source_id="source", source_dataset="EQUS.MINI",
                population_contract="intraday-multi-provider-recovery-v1" if symbol == "POPULATION" else None,
                diagnostics={"retained_model_metadata": "immutable evidence " * 2000}))
            targets[symbol] = candle(symbol=symbol, source_dataset="EQUS.MINI")
        for symbol in ("UNCHANGED", "CORRECTED"):
            store.observe(forecasts[symbol], as_of="2026-10-08T17:46:00Z", candle=targets[symbol])
        store.record_candle({**targets["CORRECTED"], "close": "99", "available_at": "2026-10-08T17:58:00+00:00"})
        store.record_candle({**targets["SOURCE"], "source_id": "different source"})
        store.record_candle(targets["POPULATION"])
        store.record_candle({**targets["FUTURE"], "available_at": "2026-10-08T18:01:00+00:00"})
        store.observe(forecasts["UNAVAILABLE"], as_of="2026-10-08T17:46:00Z",
                      unavailable_reason="retained source unavailable")
        calls, passes, decoded = [], [], []
        validator = store._forecast_value
        def checked(row):
            decoded.append(row[0])
            return validator(row)
        monkeypatch.setattr(store, "_forecast_value", checked)
        if strategy == "old_full_inventory":
            # The original loop's only changed operation was this complete
            # forecast inventory read before its existing scoring branches.
            monkeypatch.setattr(store, "scoring_forecasts", lambda **kwargs: store.forecasts(pc=kwargs["pc"]))
        class Incoming:
            def __init__(self, **kwargs):
                self.reference = kwargs["reference_at"]
                self.population = kwargs["population_contract"]
            def bind_cycle(self, key):
                self.key = key
            def get(self, symbol):
                calls.append({"symbol": symbol, "reference_at": self.reference,
                              "population_contract": self.population, "cycle": self.key})
                if symbol != "UNRESOLVED":
                    raise ContractError("Fixture exact target remains unavailable")
                target = dict(targets[symbol], opened_at=pd.Timestamp(targets[symbol]["opened_at"]))
                return SimpleNamespace(frame=pd.DataFrame([target]))
        monkeypatch.setattr(live_session, "LiveHistories", Incoming)
        start = time.perf_counter()
        live_session.scoring_loop(store, histories={symbol: object() for symbol in forecasts},
            provider=object(), journal=object(), session_id="manual-fixture", stopped=lambda: len(passes) == 2,
            clock=lambda: NOW, pause=lambda seconds: passes.append(seconds))
        measurements[strategy] = {"wall_seconds": time.perf_counter() - start,
                                  "full_forecast_decodes": len(decoded)}
        with store.connect() as db:
            events = db.execute("SELECT cycle_id,symbol,recorded_at,kind,payload FROM events ORDER BY id").fetchall()
        results[strategy] = {"outcomes": {symbol: store.revisions(value["forecast_id"])
                                          for symbol, value in forecasts.items()},
                             "events": events, "provider_calls": calls, "passes": passes}
    assert results["selected_work"] == results["old_full_inventory"]
    assert [call["symbol"] for call in results["selected_work"]["provider_calls"]] == [
        "UNRESOLVED", "SOURCE", "POPULATION", "FUTURE", "SOURCE", "POPULATION", "FUTURE"]
    assert measurements["selected_work"]["full_forecast_decodes"] < measurements["old_full_inventory"]["full_forecast_decodes"]
    print(json.dumps({"bounded_scoring_differential": measurements, "exact_results_equal": True}, sort_keys=True))
