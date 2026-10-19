"""Intention chronology reuses validated headers without hiding changed evidence."""
from datetime import timedelta, timezone
import json

import pytest

from ml.intraday.intentions import IntentionStore
from ml.intraday.prediction import ContractError, digest, encoded, instant
from ml.intraday.store import ShadowStore
from ml.intraday.technical_sizing import SizingResult
from test_intraday_store import forecast


def saved(store, cycle, *, offset=0, zone=None, **changes):
    raw = forecast(cycle_id=cycle, **{"mode": "live", **changes})
    for field in ("reference_at", "target_open", "target_close", "issued_at",
                  "information_available_at", "data_through"):
        value = instant(raw[field]) + timedelta(minutes=offset)
        raw[field] = (value.astimezone(zone) if zone else value).isoformat()
    return store.save_forecast(raw)


def size(intents, original):
    value = dict(forecast_id=original["forecast_id"], symbol=original["symbol"], side="BUY",
                 status="sized", desired_quantity=4,
                 calculated_at=(instant(original["issued_at"]) + timedelta(seconds=1)).isoformat())
    return intents.save_sizing(original, SizingResult(digest(value), encoded(value)))


def test_repeated_chronology_reuses_only_selected_validated_headers(tmp_path, monkeypatch):
    store = ShadowStore(tmp_path / "intentions.sqlite3")
    intents = IntentionStore(store)
    current = saved(store, "current")
    older = saved(store, "older", offset=-15)
    same = saved(store, "same", zone=timezone(timedelta(hours=-7)))
    later = saved(store, "later", offset=15)
    shadow = saved(store, "shadow", mode="shadow")
    peer = saved(store, "peer", pc="Scout")
    other = saved(store, "other", symbol="AMZN")
    unsized = saved(store, "unsized", offset=30)
    for value in (current, older, same, later, shadow, peer, other):
        size(intents, value)
    # Irrelevant evidence must not enter this selected chronology read.
    with store.connect() as db:
        for value in (peer, other, unsized):
            db.execute("UPDATE forecasts SET payload='not json' WHERE id=?", (value["forecast_id"],))
    validated = []
    original = store._forecast_value

    def tracked(row):
        validated.append(row[0])
        return original(row)

    monkeypatch.setattr(store, "_forecast_value", tracked)
    expected = [same["forecast_id"], later["forecast_id"]]
    assert intents.processed_at_or_after(current) == expected
    assert validated == [value["forecast_id"] for value in (current, older, same, later, shadow)]
    validated.clear()
    assert intents.processed_at_or_after(current) == expected
    assert validated == []
    # A new sized forecast is validated on first use, without re-decoding history.
    added = saved(store, "added", offset=45)
    size(intents, added)
    validated.clear()
    assert intents.processed_at_or_after(current) == expected + [added["forecast_id"]]
    assert validated == [added["forecast_id"]]


@pytest.mark.parametrize("damage", ["payload", "indexed_reference", "indexed_issue"])
def test_warm_chronology_revalidates_changed_selected_evidence(tmp_path, damage):
    store = ShadowStore(tmp_path / "changed.sqlite3")
    intents = IntentionStore(store)
    current = saved(store, "current")
    later = saved(store, "later", offset=15)
    size(intents, later)
    assert intents.processed_at_or_after(current) == [later["forecast_id"]]
    with store.connect() as db:
        if damage == "payload":
            db.execute("UPDATE forecasts SET payload=? WHERE id=?",
                       (json.dumps(dict(later, probability_not_down=.99)), later["forecast_id"]))
        else:
            column = "reference_at" if damage == "indexed_reference" else "issued_at"
            db.execute(f"UPDATE forecasts SET {column}=? WHERE id=?",
                       (current[column], later["forecast_id"]))
    with pytest.raises(ContractError, match="forecast evidence hash or indexed identity"):
        intents.processed_at_or_after(current)


def test_empty_selected_headers_do_not_read_or_validate_history(tmp_path, monkeypatch):
    store = ShadowStore(tmp_path / "empty.sqlite3")
    intents = IntentionStore(store)
    current = saved(store, "current")
    with store.connect() as db:
        db.execute("UPDATE forecasts SET payload='not json'")

    def forbidden(row):
        pytest.fail("Empty intentions decoded unrelated history")

    monkeypatch.setattr(store, "_forecast_value", forbidden)
    assert intents.processed_at_or_after(current) == []
    assert store.execution_forecast_headers(pc="Atlas", symbol=current["symbol"], forecast_ids=[]) == []
