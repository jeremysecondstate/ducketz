"""Batch chronology IDs without changing the canonical scope or source errors."""
from contextlib import contextmanager
from datetime import timedelta
import sqlite3
import sys
from types import SimpleNamespace

import pytest

from ml.intraday.account_execution import IntentionStore
from ml.intraday.prediction import ContractError, TARGET_VERSION, digest, encoded, instant
from ml.intraday.store import ShadowStore
from ml.intraday.technical_sizing import SizingResult


execution = sys.modules[IntentionStore.__module__]


def saved(store, cycle, *, reference="2026-10-08T17:00:00+00:00", **changes):
    at = instant(reference)
    value = dict(pc="Scout", symbol="AAPL", cycle_id=cycle, reference_at=reference,
        reference_open="100", target_open=(at + timedelta(minutes=30)).isoformat(),
        target_close=(at + timedelta(minutes=45)).isoformat(),
        issued_at=(at + timedelta(minutes=16, seconds=2)).isoformat(),
        information_available_at=(at + timedelta(minutes=15, seconds=3)).isoformat(),
        data_through=(at + timedelta(minutes=15)).isoformat(), target_version=TARGET_VERSION,
        mode="live", availability_basis="observed", probability_not_down=.72,
        configured_count=2, successful_count=2, ensemble_status="full",
        models=[dict(name="a", status="success", probability_not_down=.7),
                dict(name="b", status="success", probability_not_down=.74)])
    return store.save_forecast(value | changes)


def size(intents, original):
    value = dict(forecast_id=original["forecast_id"], symbol=original["symbol"], side="BUY",
                 status="sized", desired_quantity=4, calculated_at=original["issued_at"])
    return intents.save_sizing(original, SizingResult(digest(value), encoded(value)))


QUERY = ("SELECT f.id FROM forecasts f JOIN intraday_intentions i ON i.forecast_id=f.id "
         "WHERE f.pc=? AND f.symbol=? "
         "AND julianday(f.reference_at)>=julianday(?)-1.0/86400 ORDER BY f.rowid")
CURRENT = dict(forecast_id="current", pc="Scout", symbol="AAPL",
               reference_at="2026-10-08T17:00:00+00:00")
PARAMETERS = (CURRENT["pc"], CURRENT["symbol"], CURRENT["reference_at"])


class Cursor:
    def __init__(self, raw, owner):
        self.raw, self.owner = raw, owner

    def __iter__(self):
        for row in self.raw:
            self.owner.result_rows += 1
            yield row

    def fetchone(self):
        row = self.raw.fetchone()
        self.owner.result_rows += row is not None
        return row


class Connection:
    def __init__(self, db, *, aggregate=None, unavailable=False, rollback=False):
        self.db, self.aggregate = db, aggregate
        self.unavailable, self.rollback = unavailable, rollback
        self.calls, self.result_rows = [], 0

    @property
    def text_factory(self):
        return self.db.text_factory

    @property
    def row_factory(self):
        return self.db.row_factory

    @property
    def in_transaction(self):
        return self.db.in_transaction

    def execute(self, query, parameters=()):
        self.calls.append(query)
        if query.startswith("SELECT json_group_array"):
            if self.rollback:
                self.db.rollback()
                raise sqlite3.OperationalError("interrupted pinned source")
            if self.unavailable:
                raise sqlite3.OperationalError("no such function: json_group_array")
            if self.aggregate is not None:
                return Cursor(self.db.execute("SELECT ?", (self.aggregate,)), self)
        return Cursor(self.db.execute(query, parameters), self)


@pytest.fixture
def db():
    value = sqlite3.connect(":memory:")
    value.execute("CREATE TABLE forecasts(id,pc,symbol,reference_at)")
    value.execute("CREATE TABLE intraday_intentions(forecast_id)")
    yield value
    value.close()


def put(db, key, *, pc="Scout", symbol="AAPL", reference=None, sized=True):
    db.execute("INSERT INTO forecasts VALUES(?,?,?,?)",
               (key, pc, symbol, reference or CURRENT["reference_at"]))
    if sized:
        db.execute("INSERT INTO intraday_intentions VALUES(?)", (key,))


def reader(db):
    return execution._processed_intention_ids(db, CURRENT)


def process(connection):
    @contextmanager
    def connect():
        yield connection

    selected = []

    def headers(**scope):
        assert (scope["pc"], scope["symbol"]) == ("Scout", "AAPL")
        selected.extend(scope["forecast_ids"])
        return [dict(forecast_id=key, mode="live", reference_at=CURRENT["reference_at"])
                for key in scope["forecast_ids"]]

    intentions = object.__new__(IntentionStore)
    intentions.store = SimpleNamespace(connect=connect, execution_forecast_headers=headers)
    result = intentions.processed_at_or_after(CURRENT)
    return result, selected


def test_1083_scoped_ids_cross_one_result_row_with_same_chronology(db):
    for number in range(1083):
        put(db, f"sized-{number:04d}")
    put(db, "peer", pc="Atlas")
    put(db, "other", symbol="MU")
    put(db, "older", reference="2026-10-08T16:45:00Z")
    put(db, "unsized", sized=False)
    expected = [row[0] for row in db.execute(QUERY, PARAMETERS)]
    connection = Connection(db)
    result, selected = process(connection)
    assert result == selected == expected
    # Charge actual result-row crossings, not wall time or arbitrary sleeps.
    assert connection.result_rows == 1
    assert len(connection.calls) == 1


def test_scope_offsets_order_duplicates_and_empty(db):
    assert reader(db) == []
    put(db, "z-first", reference="2026-10-08T10:00:00-07:00")
    put(db, "a-second")
    put(db, "a-second")
    put(db, "unicode-雪\0")
    put(db, "old", reference="2026-10-08T16:59:58Z")
    put(db, "prefilter-only", reference="2026-10-08T16:59:59.500000Z")
    assert reader(db) == [row[0] for row in db.execute(QUERY, PARAMETERS)]


@pytest.mark.parametrize("key", [b"blob", 23, 1.5, None])
def test_irregular_storage_retains_canonical_values(db, key):
    put(db, key)
    connection = Connection(db)
    assert reader(connection) == [row[0] for row in db.execute(QUERY, PARAMETERS)]


@pytest.mark.parametrize("factory", ["bytes", "row"])
def test_custom_factories_use_canonical_reader(db, factory):
    put(db, "value")
    if factory == "bytes":
        db.text_factory = bytes
    else:
        db.row_factory = sqlite3.Row
    connection = Connection(db)
    assert reader(connection) == [row[0] for row in db.execute(QUERY, PARAMETERS)]
    assert connection.calls == [QUERY]


@pytest.mark.parametrize("raw", ["broken", "{}", "[null]", "[23]", "[[\"value\"]]"])
def test_invalid_optional_aggregate_uses_canonical_reader(db, raw):
    put(db, "value")
    connection = Connection(db, aggregate=raw)
    assert reader(connection) == ["value"]
    assert len(connection.calls) == 2
    assert connection.calls[-1] == QUERY


def test_unavailable_json_preserves_canonical_result(db):
    put(db, "value")
    connection = Connection(db, unavailable=True)
    assert reader(connection) == ["value"]
    assert connection.calls[-1] == QUERY


def test_invalid_utf8_preserves_canonical_error(db):
    db.execute("INSERT INTO forecasts VALUES(CAST(x'80' AS TEXT),'Scout','AAPL',?)",
               (CURRENT["reference_at"],))
    db.execute("INSERT INTO intraday_intentions SELECT id FROM forecasts")
    for read in (reader, lambda c: [row[0] for row in c.execute(QUERY, PARAMETERS)]):
        with pytest.raises(sqlite3.OperationalError):
            read(db)


def test_interrupted_pinned_snapshot_never_reopens_reader(db):
    put(db, "value")
    db.commit()
    db.execute("BEGIN")
    connection = Connection(db, rollback=True)
    with pytest.raises(sqlite3.OperationalError, match="interrupted pinned source"):
        reader(connection)
    assert len(connection.calls) == 1
    assert not db.in_transaction


def test_missing_source_table_keeps_source_error():
    with sqlite3.connect(":memory:") as db:
        with pytest.raises(sqlite3.OperationalError, match="no such table: forecasts"):
            reader(db)


def test_derived_reader_keeps_frozen_header_scope_and_exact_instants(tmp_path, monkeypatch):
    store = ShadowStore(tmp_path / "originals.sqlite3")
    intents = IntentionStore(store)
    current = saved(store, "current")
    later = saved(store, "later", reference="2026-10-08T17:15:00Z")
    same = saved(store, "same", reference="2026-10-08T10:00:00-07:00")
    older = saved(store, "older", reference="2026-10-08T16:45:00Z")
    shadow = saved(store, "shadow", mode="shadow")
    unsized = saved(store, "unsized")
    for value in (current, later, same, older, shadow):
        size(intents, value)
    with store.connect() as source:
        source.execute("UPDATE forecasts SET payload='broken unrelated JSON' WHERE id IN (?,?)",
                       (older["forecast_id"], unsized["forecast_id"]))
    validated = []
    original = store._forecast_value

    def tracked(row):
        validated.append(row[0])
        return original(row)

    monkeypatch.setattr(store, "_forecast_value", tracked)
    assert intents.processed_at_or_after(current) == [later["forecast_id"], same["forecast_id"]]
    assert validated == [value["forecast_id"] for value in (current, later, same, shadow)]
    validated.clear()
    assert intents.processed_at_or_after(current) == [later["forecast_id"], same["forecast_id"]]
    assert validated == []
    with store.connect() as source:
        source.execute("UPDATE forecasts SET payload=? WHERE id=?",
                       (encoded(later | {"probability_not_down": .99}), later["forecast_id"]))
    with pytest.raises(ContractError, match="hash or indexed identity changed"):
        intents.processed_at_or_after(current)


@pytest.mark.parametrize("microseconds, expected", [(-1, False), (0, True), (1, True)])
def test_derived_reader_keeps_python_inclusive_boundary(tmp_path, microseconds, expected):
    store = ShadowStore(tmp_path / "boundary.sqlite3")
    intents = IntentionStore(store)
    candidate = saved(store, "candidate")
    size(intents, candidate)
    current = candidate | {"forecast_id": "different-current", "reference_at":
        (instant(candidate["reference_at"]) - timedelta(microseconds=microseconds)).isoformat()}
    assert intents.processed_at_or_after(current) == ([candidate["forecast_id"]] if expected else [])
