"""Scoring polls route verified small projections, then validate selected originals."""
import json
import pytest

from ml.intraday import live_session
from ml.intraday.prediction import ContractError
from ml.intraday.store import ScoringForecastReader, ShadowStore
from tests.test_intraday_store import candle, forecast


def saved(store, **changes):
    return store.save_forecast(forecast(mode="live", pc="Scout", **changes))


def read_counter(store, monkeypatch):
    calls = []
    original = store.forecast_by_id

    def read(identity, **kwargs):
        calls.append(identity)
        return original(identity, **kwargs)

    monkeypatch.setattr(store, "forecast_by_id", read)
    return calls


def run_scoring(store, *, pauses, histories=None, source=None, clock=lambda: "2026-10-09T00:01Z"):
    turns = []

    def pause(seconds):
        turns.append(seconds)
        pauses(len(turns))

    live_session.scoring_loop(store, histories=histories or {}, provider=None, journal=None,
        session_id="projection-test", stopped=lambda: len(turns) >= 2,
        pause=pause, clock=clock, pc="Scout", history_source=source)
    assert turns == [3, 3]


def test_unchanged_poll_never_rereads_model_payload_and_keeps_order_scope(tmp_path, monkeypatch):
    store = ShadowStore(tmp_path / "score.sqlite3")
    first = saved(store, models=[{"name": "a", "status": "success", "probability_not_down": .7,
        "large_model_evidence": "x" * 100000}, {"name": "b", "status": "success", "probability_not_down": .74}])
    foreign = store.save_forecast(forecast(mode="live", pc="Atlas", cycle_id="foreign"))
    second = saved(store, cycle_id="second", symbol="MSFT")
    reads = read_counter(store, monkeypatch)
    reader = ScoringForecastReader(store, pc="Scout")
    values = list(reader)
    assert [f["forecast_id"] for f in values] == [first["forecast_id"], second["forecast_id"]]
    assert foreign["forecast_id"] not in reads
    assert len(reads) == 2
    assert list(reader) == values
    assert len(reads) == 2
    assert all("models" not in value for value in values)
    assert reader._cache_bytes < 2048
    assert "large_model_evidence" not in repr(reader._cache)
    assert reader.original(values[0]) == first
    assert len(reads) == 3


def test_projection_preserves_route_fields_and_detaches_mutable_values(tmp_path):
    store = ShadowStore(tmp_path / "score.sqlite3")
    original = saved(store, dataset_manifest={"native_interval_contract": "native", "unneeded": "x" * 100000},
        reference_provenance={"forward_fill_contract": "carry", "unneeded": [1, 2]},
        feature_version="feature", native_interval_contract="native")
    reader = ScoringForecastReader(store, pc="Scout")
    projection = list(reader)[0]
    assert projection["dataset_manifest"] == {"native_interval_contract": "native"}
    assert projection["reference_provenance"] == {"forward_fill_contract": "carry"}
    assert projection["feature_version"] == "feature"
    assert projection["native_interval_contract"] == "native"
    assert reader.original(projection) == original
    projection["dataset_manifest"]["native_interval_contract"] = "changed"
    assert list(reader)[0]["dataset_manifest"] == {"native_interval_contract": "native"}
    with pytest.raises(ContractError, match="changed before outcome mutation"):
        reader.original(projection)


def test_projection_preserves_absence_none_and_empty_nested_fields(tmp_path):
    store = ShadowStore(tmp_path / "score.sqlite3")
    saved(store)
    saved(store, cycle_id="none", dataset_manifest=None, reference_provenance=None)
    saved(store, cycle_id="empty", dataset_manifest={}, reference_provenance={})
    projections = list(ScoringForecastReader(store, pc="Scout"))
    assert "dataset_manifest" not in projections[0] and "reference_provenance" not in projections[0]
    assert projections[1]["dataset_manifest"] is None and projections[1]["reference_provenance"] is None
    assert projections[2]["dataset_manifest"] == {} and projections[2]["reference_provenance"] == {}


def test_new_rows_committed_during_poll_are_seen_next_poll(tmp_path, monkeypatch):
    store = ShadowStore(tmp_path / "score.sqlite3")
    first = saved(store)
    original = store.forecast_by_id
    inserted = []

    def read(identity, **kwargs):
        if not inserted:
            inserted.append(saved(store, cycle_id="concurrent", symbol="MSFT"))
        return original(identity, **kwargs)

    monkeypatch.setattr(store, "forecast_by_id", read)
    reader = ScoringForecastReader(store, pc="Scout")
    assert [f["forecast_id"] for f in reader] == [first["forecast_id"]]
    assert [f["forecast_id"] for f in reader] == [first["forecast_id"], inserted[0]["forecast_id"]]


@pytest.mark.parametrize("column", ["symbol", "cycle_id", "reference_at", "issued_at"])
def test_changed_indexed_identity_is_revalidated_before_routing(tmp_path, column):
    store = ShadowStore(tmp_path / "score.sqlite3")
    original = saved(store)
    reader = ScoringForecastReader(store, pc="Scout")
    list(reader)
    with store.connect() as db:
        db.execute("UPDATE forecasts SET " + column + "=? WHERE id=?", ("changed", original["forecast_id"]))
    with pytest.raises(ContractError, match="indexed identity"):
        list(reader)


def test_deleted_rows_are_pruned_and_rowid_reuse_is_not_a_highwater_mark(tmp_path, monkeypatch):
    store = ShadowStore(tmp_path / "score.sqlite3")
    old = saved(store)
    reader = ScoringForecastReader(store, pc="Scout")
    list(reader)
    with store.connect() as db:
        old_rowid = db.execute("SELECT rowid FROM forecasts").fetchone()[0]
        db.execute("DELETE FROM forecasts WHERE id=?", (old["forecast_id"],))
    assert list(reader) == [] and reader._cache_bytes == 0
    new = saved(store, cycle_id="replacement")
    with store.connect() as db:
        assert db.execute("SELECT rowid FROM forecasts").fetchone()[0] == old_rowid
    reads = read_counter(store, monkeypatch)
    assert [f["forecast_id"] for f in reader] == [new["forecast_id"]]
    assert reads == [new["forecast_id"]]


@pytest.mark.parametrize("change", ["payload", "index", "delete"])
def test_selected_original_revalidated_after_projection_before_mutation(tmp_path, change):
    store = ShadowStore(tmp_path / "score.sqlite3")
    original = saved(store)
    reader = ScoringForecastReader(store, pc="Scout")
    projection = list(reader)[0]
    with store.connect() as db:
        if change == "payload":
            raw = db.execute("SELECT payload FROM forecasts WHERE id=?", (original["forecast_id"],)).fetchone()[0]
            altered = raw.replace('"probability_not_down":0.7', '"probability_not_down":0.8', 1)
            assert altered != raw and len(altered) == len(raw)
            db.execute("UPDATE forecasts SET payload=? WHERE id=?", (altered, original["forecast_id"]))
        elif change == "index":
            db.execute("UPDATE forecasts SET symbol='MSFT' WHERE id=?", (original["forecast_id"],))
        else:
            db.execute("DELETE FROM forecasts WHERE id=?", (original["forecast_id"],))
    with pytest.raises(ContractError):
        store.observe(reader.original(projection), as_of="2026-10-09T00:01Z", candle=candle())
    assert store.revisions(original["forecast_id"]) == []


def test_cache_both_entry_and_byte_limits_are_hard_bounds(tmp_path, monkeypatch):
    store = ShadowStore(tmp_path / "score.sqlite3")
    originals = [saved(store, cycle_id=str(i)) for i in range(4)]
    monkeypatch.setattr(ScoringForecastReader, "MAX_ENTRIES", 2)
    reader = ScoringForecastReader(store, pc="Scout")
    assert len(list(reader)) == 4 and len(reader._cache) == 2
    assert [f["forecast_id"] for f in reader] == [f["forecast_id"] for f in originals]
    monkeypatch.setattr(ScoringForecastReader, "MAX_BYTES", 1)
    smaller = ScoringForecastReader(store, pc="Scout")
    assert len(list(smaller)) == 4 and not smaller._cache and smaller._cache_bytes == 0


def test_pending_historical_target_retries_every_poll_without_model_rescan(tmp_path, monkeypatch):
    store = ShadowStore(tmp_path / "score.sqlite3")
    original = saved(store)
    reads = read_counter(store, monkeypatch)
    attempts = []

    class Incoming:
        def bind_cycle(self, identity):
            assert identity == "score:" + original["forecast_id"]

        def get(self, symbol):
            attempts.append(symbol)
            raise ContractError("History reference lies outside this feed's latched eligible day")

    run_scoring(store, pauses=lambda _: None, histories={"AAPL": object()}, source=lambda ref: Incoming())
    assert attempts == ["AAPL", "AAPL"] and reads == [original["forecast_id"]]
    assert store.revisions(original["forecast_id"]) == []
    with store.connect() as db:
        retries = [json.loads(row[0]) for row in db.execute("SELECT payload FROM events WHERE kind='target_scoring_retry'")]
    assert len(retries) == 2
    assert all(r["forecast_id"] == original["forecast_id"] and "latched eligible day" in r["message"] for r in retries)


def test_clock_advance_and_new_forecast_remain_eligible_without_restarting_reader(tmp_path):
    store = ShadowStore(tmp_path / "score.sqlite3")
    first = saved(store)
    now = ["2026-10-08T17:44Z"]
    store.record_candle(candle())
    inserted = []

    def pause(turn):
        if turn == 1:
            now[0] = "2026-10-09T00:01Z"
            inserted.append(saved(store, cycle_id="new", symbol="MSFT"))
            store.record_candle(candle(symbol="MSFT"))

    run_scoring(store, pauses=pause, clock=lambda: now[0])
    assert store.revisions(first["forecast_id"])[-1]["status"] == "scored"
    assert store.revisions(inserted[0]["forecast_id"])[-1]["status"] == "scored"


def test_late_genuine_correction_is_scored_again_with_original_forecast(tmp_path):
    store = ShadowStore(tmp_path / "score.sqlite3")
    original = saved(store)
    store.record_candle(candle(close="101"))

    def pause(turn):
        if turn == 1:
            store.record_candle(candle(close="99", available_at="2026-10-08T18:00:00Z"))

    run_scoring(store, pauses=pause)
    revisions = store.revisions(original["forecast_id"])
    assert [r["target_not_down"] for r in revisions] == [True, False]
    assert store.forecast_by_id(original["forecast_id"]) == original


def test_selected_payload_corruption_cannot_be_hidden_by_a_warm_poll(tmp_path, monkeypatch):
    store = ShadowStore(tmp_path / "score.sqlite3")
    original = saved(store)
    turns = []

    def pause(_):
        turns.append(True)
        with store.connect() as db:
            raw = db.execute("SELECT payload FROM forecasts").fetchone()[0]
            db.execute("UPDATE forecasts SET payload=?", (raw.replace('"reference_open":"100"', '"reference_open":"200"'),))
        store.record_candle(candle())

    with pytest.raises(ContractError, match="hash or indexed identity"):
        live_session.scoring_loop(store, histories={}, provider=None, journal=None, session_id="corrupt",
            stopped=lambda: len(turns) > 1, pause=pause, pc="Scout", clock=lambda: "2026-10-09T00:01Z")
    assert store.revisions(original["forecast_id"]) == []


def test_legacy_invalidation_revalidates_forecast_before_outcome_write(tmp_path, monkeypatch):
    from ml.intraday import data
    store = ShadowStore(tmp_path / "score.sqlite3")
    original = saved(store)
    with monkeypatch.context() as legacy:
        legacy.setattr(data, "synthetic_continuity_verified", lambda _: True)
        previous = store.observe(original, as_of="2026-10-08T18:00Z", candle=candle(
            high="100", low="100", volume=0, synthetic=True, no_trade=True,
            evidence_json='{"legacy_confirmations":2}'))
    revisions = store.revisions
    invalidations = []

    def corrupt_after_projection(identity):
        result = revisions(identity)
        with store.connect() as db:
            raw = db.execute("SELECT payload FROM forecasts WHERE id=?", (identity,)).fetchone()[0]
            db.execute("UPDATE forecasts SET payload=? WHERE id=?", (
                raw.replace('"reference_open":"100"', '"reference_open":"200"'), identity))
        return result

    monkeypatch.setattr(store, "revisions", corrupt_after_projection)
    monkeypatch.setattr(store, "invalidate_unverified_outcome", lambda *args, **kwargs: invalidations.append(args))
    with pytest.raises(ContractError, match="hash or indexed identity"):
        live_session.scoring_loop(store, histories={}, provider=None, journal=None, session_id="corrupt-audit",
            stopped=lambda: False, pause=lambda _: pytest.fail("should reject before next poll"),
            pc="Scout", clock=lambda: "2026-10-09T00:01Z")
    assert invalidations == [] and revisions(original["forecast_id"]) == [previous]
