"""Chronology selects and validates only possible sized fencing forecasts."""
from datetime import timedelta

import pytest

from ml.intraday.intentions import IntentionStore
from ml.intraday.prediction import ContractError, digest, encoded, instant
from ml.intraday.store import ShadowStore
from ml.intraday.technical_sizing import SizingResult
from tests.test_intraday_store import forecast


def saved(store, cycle, *, reference="2026-10-08T17:00:00+00:00", **changes):
    at = instant(reference)
    return store.save_forecast(forecast(
        pc="Scout", mode="live", cycle_id=cycle, reference_at=reference,
        target_open=(at + timedelta(minutes=30)).isoformat(),
        target_close=(at + timedelta(minutes=45)).isoformat(),
        issued_at=(at + timedelta(minutes=16, seconds=2)).isoformat(),
        information_available_at=(at + timedelta(minutes=15, seconds=3)).isoformat(),
        data_through=(at + timedelta(minutes=15)).isoformat(), **changes))


def size(intents, f):
    value = dict(forecast_id=f["forecast_id"], symbol=f["symbol"], side="BUY",
                 status="sized", desired_quantity=4, calculated_at=f["issued_at"])
    return intents.save_sizing(f, SizingResult(digest(value), encoded(value)))


def setup(tmp_path):
    store = ShadowStore(tmp_path / "evidence.sqlite3")
    return store, IntentionStore(store)


def record_validated(monkeypatch, store):
    validated = []
    original = store._forecast_value

    def check(row):
        validated.append(row[0])
        return original(row)

    monkeypatch.setattr(store, "_forecast_value", check)
    monkeypatch.setattr(store, "forecasts", lambda **_: pytest.fail("all-history materialization"))
    return validated


def test_inclusive_chronology_and_forecast_order_not_sizing_order(tmp_path, monkeypatch):
    store, intents = setup(tmp_path)
    current = saved(store, "current")
    later = saved(store, "later", reference="2026-10-08T17:15:00Z")
    equal = saved(store, "equal")
    older = saved(store, "older", reference="2026-10-08T16:45:00Z")
    unsized = saved(store, "unsized", reference="2026-10-08T17:30:00Z")
    # An initial unsized forecast intent is not a completed sizing decision.
    assert store.forecast("unsized", "AAPL") == unsized
    for f in (equal, older, current, later):
        size(intents, f)
    validated = record_validated(monkeypatch, store)
    assert intents.processed_at_or_after(current) == [later["forecast_id"], equal["forecast_id"]]
    assert validated == [current["forecast_id"], later["forecast_id"], equal["forecast_id"]]


@pytest.mark.parametrize("reference", [
    "2026-10-08T10:00:00-07:00", "2026-10-08T19:00:00+02:00",
    "2026-10-08T17:00:00Z",
])
def test_equal_reference_offsets_remain_inclusive(tmp_path, reference):
    store, intents = setup(tmp_path)
    current = saved(store, "current")
    equal = saved(store, "equal", reference=reference)
    size(intents, equal)
    assert intents.processed_at_or_after(current) == [equal["forecast_id"]]


def test_offset_timestamps_compare_instants_not_lexical_order(tmp_path):
    store, intents = setup(tmp_path)
    current = saved(store, "current")
    # Lexically earlier but actually later; the opposite is an older reference.
    later = saved(store, "later", reference="2026-10-08T10:15:00-07:00")
    older = saved(store, "older", reference="2026-10-08T18:45:00+02:00")
    for f in (later, older):
        size(intents, f)
    assert intents.processed_at_or_after(current) == [later["forecast_id"]]


@pytest.mark.parametrize("delta_microseconds, expected", [(-1, False), (0, True), (1, True)])
def test_exact_python_boundary_after_conservative_sql_prefilter(tmp_path, delta_microseconds, expected):
    store, intents = setup(tmp_path)
    candidate = saved(store, "candidate")
    size(intents, candidate)
    # The caller normally has a quarter-hour reference; use the direct reader
    # boundary to prove the SQL prefilter does not make subsecond decisions.
    current = candidate | {"forecast_id": "another-current-id", "reference_at":
        (instant(candidate["reference_at"]) - timedelta(microseconds=delta_microseconds)).isoformat()}
    assert intents.processed_at_or_after(current) == ([candidate["forecast_id"]] if expected else [])


@pytest.mark.parametrize("changes", [{"pc": "Atlas"}, {"symbol": "DBX"}, {"mode": "shadow"}])
def test_other_pc_symbol_and_shadow_do_not_fence(tmp_path, changes):
    store, intents = setup(tmp_path)
    current = saved(store, "current")
    kwargs = {"pc": "Scout", "mode": "live", **changes}
    # Avoid duplicate keyword defaults in the common helper.
    value = forecast(cycle_id="other", **kwargs)
    other = store.save_forecast(value)
    size(intents, other)
    assert intents.processed_at_or_after(current) == []


def test_no_candidates_does_not_decode_unrelated_historical_payloads(tmp_path, monkeypatch):
    store, intents = setup(tmp_path)
    current = saved(store, "current")
    for index in range(40):
        old = saved(store, f"old-{index}", reference="2026-10-08T16:45:00Z",
                    retained_model_evidence="x" * 10000)
        size(intents, old)
        saved(store, f"unsized-{index}", retained_model_evidence="x" * 10000)
    validated = record_validated(monkeypatch, store)
    assert intents.processed_at_or_after(current) == []
    assert validated == []


def test_unrelated_payload_corruption_is_not_an_all_history_integrity_scan(tmp_path, monkeypatch):
    store, intents = setup(tmp_path)
    current = saved(store, "current")
    old = saved(store, "old", reference="2026-10-08T16:45:00Z")
    size(intents, old)
    unsized = saved(store, "unsized")
    with store.connect() as db:
        db.execute("UPDATE forecasts SET payload='broken unrelated JSON' WHERE id IN (?,?)",
                   (old["forecast_id"], unsized["forecast_id"]))
    validated = record_validated(monkeypatch, store)
    assert intents.processed_at_or_after(current) == []
    assert validated == []


def test_candidate_original_digest_is_validated(tmp_path):
    store, intents = setup(tmp_path)
    current = saved(store, "current")
    candidate = saved(store, "candidate")
    size(intents, candidate)
    with store.connect() as db:
        db.execute("UPDATE forecasts SET payload=? WHERE id=?",
                   (encoded(candidate | {"probability_not_down": .99}), candidate["forecast_id"]))
    with pytest.raises(ContractError, match="hash or indexed identity changed"):
        intents.processed_at_or_after(current)


@pytest.mark.parametrize("field, value", [
    ("cycle_id", "wrong-cycle"),
    ("issued_at", "2026-10-08T17:17:00+00:00"),
    ("reference_at", "2026-10-08T17:15:00+00:00"),
])
def test_selected_candidate_index_corruption_is_validated(tmp_path, field, value):
    store, intents = setup(tmp_path)
    current = saved(store, "current")
    candidate = saved(store, "candidate")
    size(intents, candidate)
    with store.connect() as db:
        db.execute(f"UPDATE forecasts SET {field}=? WHERE id=?", (value, candidate["forecast_id"]))
    with pytest.raises(ContractError, match="hash or indexed identity changed"):
        intents.processed_at_or_after(current)


@pytest.mark.parametrize("field, source", [("pc", "Atlas"), ("symbol", "DBX")])
def test_changed_index_cannot_admit_a_foreign_original(tmp_path, field, source):
    store, intents = setup(tmp_path)
    current = saved(store, "current")
    candidate = store.save_forecast(forecast(pc="Scout", mode="live", cycle_id="foreign") | {field: source})
    size(intents, candidate)
    with store.connect() as db:
        db.execute(f"UPDATE forecasts SET {field}=? WHERE id=?", (current[field], candidate["forecast_id"]))
    with pytest.raises(ContractError, match="hash or indexed identity changed"):
        intents.processed_at_or_after(current)


@pytest.mark.parametrize("kind", ["current", "shadow"])
def test_current_and_shadow_originals_validate_before_result_filtering(tmp_path, kind):
    store, intents = setup(tmp_path)
    current = saved(store, "current")
    selected = current if kind == "current" else store.save_forecast(
        forecast(pc="Scout", mode="shadow", cycle_id="shadow"))
    size(intents, selected)
    with store.connect() as db:
        db.execute("UPDATE forecasts SET cycle_id='changed' WHERE id=?", (selected["forecast_id"],))
    with pytest.raises(ContractError, match="hash or indexed identity changed"):
        intents.processed_at_or_after(current)
