"""Offline current-session scoring priority with canonical provenance readers."""
import pytest

from ml.intraday import live_session
from ml.intraday.prediction import ContractError, encoded
from ml.intraday.scoring_candles import ScoringCandleReader
from ml.intraday.store import ScoringForecastReader, ShadowStore
from tests.test_intraday_store import candle, forecast


def own(store, pc, session, **changes):
    value = forecast(mode="live", pc=pc, **changes)
    store.begin_cycle(value["cycle_id"], dict(pc=pc, mode="live", experiment=session,
        symbols=[value["symbol"]], reference_at=value["reference_at"],
        configuration_hash="fixture", started_at=value["issued_at"]))
    return store.save_forecast(value)


def old(store, pc, symbol):
    return own(store, pc, "older-session", symbol=symbol, cycle_id="old-" + symbol,
        reference_at="2026-10-08T16:45:00+00:00", data_through="2026-10-08T17:00:00+00:00",
        issued_at="2026-10-08T17:01:00+00:00", target_open="2026-10-08T17:15:00+00:00",
        information_available_at="2026-10-08T17:00:03+00:00", target_close="2026-10-08T17:30:00+00:00")


@pytest.mark.parametrize("pc", ["Atlas", "Scout"])
def test_current_ready_target_scores_before_unrelated_model_inventory(tmp_path, monkeypatch, pc):
    store = ShadowStore(tmp_path / "ready-first.sqlite3")
    older = old(store, pc, "OLD")
    ready = own(store, pc, "manual", symbol="READY", cycle_id="ready")
    store.record_candle(candle(symbol="READY", close="101"))
    original = store.forecast_by_id
    def read(identity, **kwargs):
        if identity == older["forecast_id"]:
            assert store.revisions(ready["forecast_id"]), "Ready own target must precede unrelated model validation"
        return original(identity, **kwargs)
    monkeypatch.setattr(store, "forecast_by_id", read)
    calls, done = [], []
    class Incoming:
        def bind_cycle(self, key):
            assert key == "score:" + older["forecast_id"]
        def get(self, symbol):
            calls.append((symbol, bool(store.revisions(ready["forecast_id"]))))
            raise ContractError("Retained historical exact target is unavailable")
    live_session.scoring_loop(store, histories={"OLD": object()}, provider=None, journal=None,
        session_id="manual", pc=pc, history_source=lambda _: Incoming(),
        stopped=lambda: bool(done), clock=lambda: "2026-10-08T18:00Z", pause=lambda _: done.append(True))
    assert calls == [("OLD", True)]
    assert store.revisions(ready["forecast_id"])[-1]["target_not_down"] is True
    assert store.revisions(older["forecast_id"]) == []


@pytest.mark.parametrize("pc", ["Atlas", "Scout"])
def test_current_target_arrival_scores_between_historical_fetches(tmp_path, pc):
    store = ShadowStore(tmp_path / "ready-between.sqlite3")
    for symbol in ("OLD1", "OLD2"):
        old(store, pc, symbol)
    ready = own(store, pc, "manual", symbol="READY", cycle_id="ready")
    now = ["2026-10-08T17:44Z"]
    calls, done = [], []
    class Incoming:
        def bind_cycle(self, key):
            pass
        def get(self, symbol):
            calls.append((symbol, bool(store.revisions(ready["forecast_id"]))))
            if symbol == "OLD1":
                now[0] = "2026-10-08T17:46Z"
                store.record_candle(candle(symbol="READY", close="101"))
            raise ContractError("Retained historical exact target is unavailable")
    live_session.scoring_loop(store, histories={symbol: object() for symbol in ("OLD1", "OLD2")},
        provider=None, journal=None, session_id="manual", pc=pc, history_source=lambda _: Incoming(),
        stopped=lambda: bool(done), clock=lambda: now[0], pause=lambda _: done.append(True))
    assert calls == [("OLD1", False), ("OLD2", True)]
    assert store.revisions(ready["forecast_id"])[-1]["status"] == "scored"


def run_once(store, *, pc="Atlas", stopped=None, clock=lambda: "2026-10-08T18:00Z"):
    done = []
    live_session.scoring_loop(store, histories={}, provider=None, journal=None,
        session_id="manual", pc=pc, stopped=stopped or (lambda: bool(done)),
        clock=clock, pause=lambda _: done.append(True))


def test_cold_unrelated_candle_payload_is_not_read_before_ready_score(tmp_path, monkeypatch):
    store = ShadowStore(tmp_path / "candle-priority.sqlite3")
    ready = own(store, "Atlas", "manual", symbol="READY", cycle_id="ready")
    store.record_candle(candle(symbol="OLD", evidence_json='{"large":"' + "x" * 100000 + '"}'))
    store.record_candle(candle(symbol="READY", close="101"))
    original, calls = ScoringCandleReader._read, []
    def read(self, row):
        calls.append(row[2])
        if row[2] == "OLD":
            assert store.revisions(ready["forecast_id"]), "Cold unrelated candle must follow ready target"
        return original(self, row)
    monkeypatch.setattr(ScoringCandleReader, "_read", read)
    run_once(store)
    assert "OLD" in calls, "Ordinary historical candle scan remains in operation"
    assert store.revisions(ready["forecast_id"])[-1]["status"] == "scored"


@pytest.mark.parametrize("pc", ["Atlas", "Scout"])
def test_scoped_reader_keeps_actor_session_and_ordinary_inventory(tmp_path, pc):
    store = ShadowStore(tmp_path / "scope.sqlite3")
    current = own(store, pc, "manual", cycle_id="current")
    historical = own(store, pc, "older", symbol="OLD", cycle_id="old")
    other = own(store, "Scout" if pc == "Atlas" else "Atlas", "manual", cycle_id="foreign")
    reader = ScoringForecastReader(store, pc=pc, session_id="manual")
    assert [row["forecast_id"] for row in reader] == [current["forecast_id"]]
    assert [row["forecast_id"] for row in ScoringForecastReader(store, pc=pc)] == [current["forecast_id"], historical["forecast_id"]]
    assert other["forecast_id"] not in {row["forecast_id"] for row in reader}
    with store.connect() as db:
        db.execute("UPDATE cycles SET pc=? WHERE id='current'", ("Scout" if pc == "Atlas" else "Atlas",))
    assert list(reader) == []
    assert not reader._cache and reader._cache_bytes == 0


@pytest.mark.parametrize("change", [
    {"source_id": "foreign"}, {"source_dataset": "foreign"},
    {"available_at": "2026-10-08T18:01:00Z"}, {"interval_seconds": 60},
    {"synthetic": True, "evidence_json": '{"legacy_confirmations":2}'},
])
def test_ready_priority_preserves_source_availability_and_continuity_guards(tmp_path, change):
    store = ShadowStore(tmp_path / "guards.sqlite3")
    ready = own(store, "Atlas", "manual", source_id="source", source_dataset="EQUS.MINI")
    store.record_candle(candle(**{"source_dataset": "EQUS.MINI", **change}))
    run_once(store)
    assert store.revisions(ready["forecast_id"]) == []


def test_native_forecast_cannot_use_legacy_target_through_priority(tmp_path):
    from ml.intraday.native_interval import CONTRACT
    store = ShadowStore(tmp_path / "native.sqlite3")
    ready = own(store, "Atlas", "manual", native_interval_contract=CONTRACT)
    store.record_candle(candle())
    run_once(store)
    assert store.revisions(ready["forecast_id"]) == []


@pytest.mark.parametrize("damage", ["forecast", "candle", "revision"])
def test_priority_revalidates_originals_and_revisions_before_write(tmp_path, damage):
    store = ShadowStore(tmp_path / "corrupt.sqlite3")
    ready = own(store, "Atlas", "manual")
    store.observe(ready, as_of="2026-10-08T17:46Z")
    store.record_candle(candle(close="101"))
    with store.connect() as db:
        if damage == "forecast":
            db.execute("UPDATE forecasts SET payload=json_set(payload,'$.reference_open','999')")
        elif damage == "candle":
            db.execute("UPDATE candles SET payload=json_set(payload,'$.close','999')")
        else:
            db.execute("UPDATE outcomes SET payload=json_set(payload,'$.status','wrong')")
    with pytest.raises(ContractError, match="hash|indexed identity"):
        run_once(store)


def test_priority_keeps_original_policy_and_revision_ancestry_with_offset_targets(tmp_path, monkeypatch):
    store = ShadowStore(tmp_path / "revision.sqlite3")
    ready = own(store, "Atlas", "manual", target_open="2026-10-08T10:30:00-07:00",
        reference_at="2026-10-08T10:00:00-07:00", probability_not_down=.5001,
        models=[dict(name=name, status="success", probability_not_down=.5001) for name in ("a", "b")])
    earlier = store.observe(ready, as_of="2026-10-08T17:46Z", candle=candle(close="99"))
    store.record_candle(candle(close="101", available_at="2026-10-08T10:58:00-07:00"))
    done, observe = [], store.observe
    def observed(*args, **kwargs):
        result = observe(*args, **kwargs)
        done.append(True)
        return result
    monkeypatch.setattr(store, "observe", observed)
    run_once(store, stopped=lambda: bool(done))
    revisions = store.revisions(ready["forecast_id"])
    assert revisions[0] == earlier and len(revisions) == 2
    assert revisions[-1]["target_not_down"] is True
    assert store.forecast_by_id(ready["forecast_id"]) == ready


def test_stop_during_priority_drain_does_not_begin_full_inventory(tmp_path, monkeypatch):
    store = ShadowStore(tmp_path / "stop.sqlite3")
    ready = own(store, "Atlas", "manual")
    store.record_candle(candle())
    done, observe = [], store.observe
    def observed(*args, **kwargs):
        result = observe(*args, **kwargs)
        done.append(True)
        return result
    original = ScoringForecastReader.__iter__
    def selected(self):
        assert self.session_id is not None, "Stopped drain must not begin historical model inventory"
        yield from original(self)
    monkeypatch.setattr(store, "observe", observed)
    monkeypatch.setattr(ScoringForecastReader, "__iter__", selected)
    run_once(store, stopped=lambda: bool(done))
    assert store.revisions(ready["forecast_id"])[-1]["status"] == "scored"


def test_opt_in_concurrent_skip_preserves_newer_row_and_ordinary_chronology(tmp_path):
    store = ShadowStore(tmp_path / "concurrent.sqlite3")
    ready = own(store, "Atlas", "manual")
    later = store.observe(ready, as_of="2026-10-08T18:01Z", candle=candle(close="99"))
    assert store.observe(ready, as_of="2026-10-08T18:00Z", candle=candle(close="101"), skip_newer=True) is None
    with pytest.raises(ContractError, match="backdated"):
        store.observe(ready, as_of="2026-10-08T18:00Z", candle=candle(close="101"))
    assert store.revisions(ready["forecast_id"]) == [later]
    with store.connect() as db:
        db.execute("UPDATE forecasts SET payload=json_set(payload,'$.reference_open','999')")
    with pytest.raises(ContractError, match="hash|indexed identity"):
        store.observe(ready, as_of="2026-10-08T18:00Z", candle=candle(), skip_newer=True)


def test_priority_race_retains_exact_newer_observation(tmp_path, monkeypatch):
    store = ShadowStore(tmp_path / "race.sqlite3")
    ready = own(store, "Atlas", "manual")
    store.record_candle(candle(close="101"))
    observed, original, done = [], store.observe, []
    def raced(forecast, **kwargs):
        assert kwargs["skip_newer"] is True
        observed.append(original(forecast, as_of="2026-10-08T18:01Z", candle=candle(close="99")))
        result = original(forecast, **kwargs)
        assert result is None
        done.append(True)
        return result
    monkeypatch.setattr(store, "observe", raced)
    run_once(store, stopped=lambda: bool(done))
    assert store.revisions(ready["forecast_id"]) == observed


def test_warm_historical_inventory_does_not_repeat_current_target_drains(tmp_path, monkeypatch):
    store = ShadowStore(tmp_path / "bounded-drain.sqlite3")
    for n in range(256):
        old(store, "Atlas", "OLD" + str(n))
    ready = own(store, "Atlas", "manual", symbol="READY", cycle_id="ready")
    store.record_candle(candle(symbol="READY"))
    reader, drains, reads, polls = ScoringForecastReader.__iter__, [], [], []
    original = store.forecast_by_id
    def selected(self):
        if self.session_id is not None:
            drains.append(self.session_id)
        yield from reader(self)
    def read(*args, **kwargs):
        reads.append(args[0])
        return original(*args, **kwargs)
    monkeypatch.setattr(ScoringForecastReader, "__iter__", selected)
    monkeypatch.setattr(store, "forecast_by_id", read)
    live_session.scoring_loop(store, histories={}, provider=None, journal=None,
        session_id="manual", stopped=lambda: len(polls) >= 2,
        clock=lambda: "2026-10-08T18:00Z", pause=lambda _: polls.append(len(reads)))
    assert drains == ["manual", "manual"], "Historical rows must not multiply full current-session drains"
    assert polls[0] == polls[1], "Warm unchanged polls must not reload original model trees"
    assert store.revisions(ready["forecast_id"])[-1]["status"] == "scored"
