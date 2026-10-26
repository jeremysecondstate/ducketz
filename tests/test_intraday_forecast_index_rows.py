"""Execution index batching preserves canonical rows and source failures."""
import json
import sqlite3
from contextlib import contextmanager
import pytest
from ml.intraday.forecast_index_reads import forecast_index_rows

QUERY = ("SELECT f.id,f.pc,f.symbol,f.cycle_id,f.reference_at,f.issued_at,r.generation "
    "FROM forecasts f LEFT JOIN execution_forecast_revisions r ON r.forecast_id=f.id WHERE "
    "f.pc=? AND f.symbol=? ORDER BY f.rowid")

@pytest.fixture
def db():
    connection = sqlite3.connect(":memory:")
    connection.execute("CREATE TABLE forecasts(id PRIMARY KEY,pc,symbol,cycle_id,reference_at,issued_at)")
    connection.execute("CREATE TABLE execution_forecast_revisions(forecast_id,generation)")
    yield connection
    connection.close()

def put(db, key, *, revision=0, **changes):
    value = dict(id=key, pc="Atlas", symbol="AAPL", cycle_id="c-" + key,
                 reference_at="2026-10-26T17:00:00Z", issued_at="2026-10-26T17:00:01Z")
    value.update(changes)
    db.execute("INSERT INTO forecasts VALUES(?,?,?,?,?,?)", tuple(value.values()))
    if revision != "missing":
        db.execute("INSERT INTO execution_forecast_revisions VALUES(?,?)", (value["id"], revision))

def read(db):
    return forecast_index_rows(db, ["f.pc=?", "f.symbol=?"], ["Atlas", "AAPL"])

def test_exact_order_scopes_duplicates_missing_revision_and_empty(db):
    assert read(db) == []
    put(db, "z-last-name-first-row")
    put(db, "a-first-name-second-row", revision="missing")
    put(db, "unicode-ÃƒÂ©Ã¢â‚¬ÂºÃ‚Âª\0", reference_at="2026-10-26T10:00:00-07:00")
    put(db, "peer", pc="Scout")
    put(db, "other", symbol="MU")
    db.execute("INSERT INTO execution_forecast_revisions VALUES(?,?)", ("z-last-name-first-row", 5))
    assert read(db) == db.execute(QUERY, ["Atlas", "AAPL"]).fetchall()

@pytest.mark.parametrize("changes", [{"id": b"blob-id"}, {"cycle_id": b"blob"},
    {"issued_at": 3}, {"reference_at": None}, {"cycle_id": 1.2}])
def test_irregular_storage_preserves_canonical_values(db, changes):
    put(db, "value", **changes)
    trace=[]; db.set_trace_callback(trace.append)
    assert read(db) == db.execute(QUERY, ["Atlas", "AAPL"]).fetchall()
    assert any(statement.startswith("SELECT f.id") for statement in trace)

@pytest.mark.parametrize("revision", [None, -1, 3, 2.5, "5", b"blob"])
def test_irregular_revision_keeps_original_exact_value(db, revision):
    put(db, "value", revision=revision)
    assert read(db) == db.execute(QUERY, ["Atlas", "AAPL"]).fetchall()

class Proxy:
    def __init__(self, db, raw=None, unavailable=False, rollback=False):
        self.db,self.raw,self.unavailable,self.rollback=db,raw,unavailable,rollback
        self.calls=[]
    @property
    def in_transaction(self): return self.db.in_transaction
    def execute(self, query, parameters):
        self.calls.append(query)
        if query.startswith("SELECT json_group_array"):
            if self.rollback:
                self.db.rollback()
                raise sqlite3.OperationalError("interrupted pinned source")
            if self.unavailable:
                raise sqlite3.OperationalError("no such function: json_group_array")
            return self.db.execute("SELECT ?", (self.raw,))
        return self.db.execute(query, parameters)

@pytest.mark.parametrize("raw", [None, "broken", "{}", "[1]", "[[1]]", "[[1,2,3,4,5,6,7]]"])
def test_invalid_aggregate_returns_same_canonical_reader(db, raw):
    put(db, "value")
    proxy=Proxy(db,raw=raw)
    assert read(proxy) == db.execute(QUERY,["Atlas","AAPL"]).fetchall()
    assert len(proxy.calls)==2

def test_json_unavailable_keeps_canonical_read(db):
    put(db,"value")
    proxy=Proxy(db,unavailable=True)
    assert read(proxy)==db.execute(QUERY,["Atlas","AAPL"]).fetchall()
    assert len(proxy.calls)==2

def test_interrupted_pinned_snapshot_never_replaced(db):
    put(db,"value")
    proxy=Proxy(db,rollback=True)
    with pytest.raises(sqlite3.OperationalError,match="interrupted pinned source"):read(proxy)
    assert len(proxy.calls)==1 and not db.in_transaction

def test_same_read_transaction_pins_canonical_inventory(tmp_path):
    path=tmp_path/"source.sqlite3"
    first=sqlite3.connect(path);first.execute("PRAGMA journal_mode=WAL")
    first.execute("CREATE TABLE forecasts(id PRIMARY KEY,pc,symbol,cycle_id,reference_at,issued_at)")
    first.execute("CREATE TABLE execution_forecast_revisions(forecast_id,generation)")
    put(first,"before");first.commit();first.execute("BEGIN")
    expected=first.execute(QUERY,["Atlas","AAPL"]).fetchall()
    second=sqlite3.connect(path);put(second,"after");second.commit()
    assert read(first)==expected
    first.rollback()
    assert len(read(first))==2
    second.close();first.close()

def test_invalid_utf8_retains_canonical_error(db):
    put(db,"value")
    db.execute("UPDATE forecasts SET issued_at=CAST(x'80' AS TEXT)")
    for reader in (read,lambda c:c.execute(QUERY,["Atlas","AAPL"]).fetchall()):
        with pytest.raises(sqlite3.OperationalError):reader(db)


def test_normal_inventory_crosses_one_sqlite_result_row_without_fallback(db):
    for n in range(310):put(db,str(n),revision=n)
    trace=[];db.set_trace_callback(trace.append)
    result=read(db)
    assert len(result)==310
    assert len(trace)==1 and trace[0].startswith("SELECT json_group_array")

@pytest.mark.parametrize("factory", ["bytes", "row"])
def test_custom_factories_retain_original_returned_types(db,factory):
    put(db,"value")
    if factory=="bytes":db.text_factory=bytes
    else:db.row_factory=sqlite3.Row
    actual=read(db);canonical=db.execute(QUERY,["Atlas","AAPL"]).fetchall()
    assert actual==canonical and type(actual[0]) is type(canonical[0])
    if factory=="bytes":assert isinstance(actual[0][0],bytes)

def test_canonical_sql_failure_preserved(db):
    db.execute("DROP TABLE execution_forecast_revisions")
    for reader in (read,lambda c:c.execute(QUERY,["Atlas","AAPL"]).fetchall()):
        with pytest.raises(sqlite3.OperationalError,match="no such table: execution_forecast_revisions"):reader(db)

def test_malformed_aggregate_after_snapshot_loss_does_not_read_fresh_source(db):
    put(db,"value")
    class LostSnapshot(Proxy):
        def execute(self,query,parameters):
            if query.startswith("SELECT json_group_array"):
                self.calls.append(query);self.db.rollback()
                return self.db.execute("SELECT ?",("broken",))
            return super().execute(query,parameters)
    proxy=LostSnapshot(db)
    with pytest.raises(ValueError):read(proxy)
    assert len(proxy.calls)==1 and not db.in_transaction


def test_valid_aggregate_after_snapshot_loss_does_not_reuse_former_proofs(db):
    put(db,"value")
    expected=db.execute(QUERY,["Atlas","AAPL"]).fetchall()
    class LostSnapshot(Proxy):
        def execute(self,query,parameters):
            if query.startswith("SELECT json_group_array"):
                self.calls.append(query);self.db.rollback()
                return self.db.execute("SELECT ?",(json.dumps(expected),))
            return super().execute(query,parameters)
    proxy=LostSnapshot(db)
    with pytest.raises(sqlite3.OperationalError,match="lost pinned source snapshot"):read(proxy)
    assert len(proxy.calls)==1 and not db.in_transaction

def test_store_inventory_uses_batched_reader_and_revalidates_changed_original(tmp_path,monkeypatch):
    from ml.intraday import forecast_index_reads
    from ml.intraday.store import ShadowStore
    from ml.intraday.prediction import ContractError
    from tests.test_intraday_store import forecast as original
    store=ShadowStore(tmp_path/"source.sqlite3")
    first=store.save_forecast(original(cycle_id="first",mode="live"))
    second=store.save_forecast(original(cycle_id="second",mode="live"))
    actual=forecast_index_reads.forecast_index_rows;calls=[]
    def tracked(*args,**kwargs):
        calls.append(True);return actual(*args,**kwargs)
    monkeypatch.setattr(forecast_index_reads,"forecast_index_rows",tracked)
    assert [v["forecast_id"] for v in store.execution_forecast_headers(pc="Atlas",symbol="AAPL")]==[first["forecast_id"],second["forecast_id"]]
    assert len(calls)==1
    assert len(store.execution_forecast_headers(pc="Atlas",symbol="AAPL"))==2
    assert len(calls)==2
    with store.connect() as connection:
        connection.execute("UPDATE forecasts SET payload=json_set(payload,'$.probability_not_down',0.73) WHERE id=?",(first["forecast_id"],))
    with pytest.raises(ContractError,match="hash or indexed identity"):
        store.execution_forecast_headers(pc="Atlas",symbol="AAPL")
    assert len(calls)==3
