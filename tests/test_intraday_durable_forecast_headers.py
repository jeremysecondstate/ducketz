"""Cold execution can reuse validated scalar headers without trusting changed originals."""
import json
import socket
import sqlite3

import pytest

from ml.intraday.prediction import ContractError
from ml.intraday.store import ShadowStore
from test_intraday_execution_forecast_projection import owner, put, selected, spy


@pytest.fixture(autouse=True)
def deny_network(monkeypatch):
    def denied(*args, **kwargs):
        pytest.fail("Durable-header regressions must not contact a provider or broker")
    monkeypatch.setattr(socket.socket, "connect", denied)
    monkeypatch.setattr(socket, "create_connection", denied)


def cache_path(source):
    return source.with_name(source.name + ".forecast-headers.sqlite3")


def populated(path):
    store = ShadowStore(path)
    current = put(store, "current")
    old = put(store, "large-old", offset=-60,
              model_diagnostics={"tree": "retained-original" * 100000})
    later = put(store, "later", offset=15)
    assert selected(owner(store), current) == [later["forecast_id"]]
    return current, old, later


def test_cold_instance_reuses_exact_validated_history_without_model_reads(tmp_path, monkeypatch):
    path = tmp_path / "history.sqlite3"
    current, old, later = populated(path)
    assert cache_path(path).is_file()
    cold = ShadowStore(path)
    decoded = spy(cold, monkeypatch)
    assert selected(owner(cold), current) == [later["forecast_id"]]
    assert decoded == []
    # Returning a caller-owned header must not poison a later cold cache hit.
    cold.execution_forecast_headers(pc="Atlas", symbol="AAPL")[0]["mode"] = "shadow"
    another = ShadowStore(path)
    assert selected(owner(another), current) == [later["forecast_id"]]
    assert another.forecast(old["cycle_id"], "AAPL") == old


@pytest.mark.parametrize("field,earlier", [
    ("issued_at", "2026-10-08T17:00:00Z"),
    ("reference_at", "2026-10-08T16:00:00Z"),
])
def test_valid_earlier_indexed_clock_cannot_hide_applicable_original(tmp_path, field, earlier):
    path = tmp_path / "clock.sqlite3"
    current, _, later = populated(path)
    with sqlite3.connect(path) as db:
        db.execute("UPDATE forecasts SET " + field + "=? WHERE id=?", (earlier, later["forecast_id"]))
    with pytest.raises(ContractError, match="hash or indexed identity"):
        selected(owner(ShadowStore(path)), current)


def test_changed_obsolete_payload_cannot_reuse_durable_validation(tmp_path):
    path = tmp_path / "changed.sqlite3"
    current, old, _ = populated(path)
    with sqlite3.connect(path) as db:
        raw = db.execute("SELECT payload FROM forecasts WHERE id=?", (old["forecast_id"],)).fetchone()[0]
        changed = raw.replace('"probability_not_down":0.72', '"probability_not_down":0.73')
        assert changed != raw and len(changed) == len(raw)
        db.execute("UPDATE forecasts SET payload=? WHERE id=?", (changed, old["forecast_id"]))
    with pytest.raises(ContractError, match="hash or indexed identity"):
        selected(owner(ShadowStore(path)), current)


@pytest.mark.parametrize("failure", ["missing", "malformed", "directory"])
def test_unusable_cache_falls_back_to_original_reader(tmp_path, monkeypatch, failure):
    path = tmp_path / "fallback.sqlite3"
    current, _, later = populated(path)
    cache = cache_path(path)
    cache.unlink()
    if failure == "malformed":
        cache.write_bytes(b"not a SQLite cache")
    elif failure == "directory":
        cache.mkdir()
    cold = ShadowStore(path)
    decoded = spy(cold, monkeypatch)
    assert selected(owner(cold), current) == [later["forecast_id"]]
    assert len(decoded) == 3


def test_replacing_original_database_invalidates_durable_headers(tmp_path):
    path = tmp_path / "source.sqlite3"
    current, _, later = populated(path)
    replacement = tmp_path / "replacement.sqlite3"
    other = ShadowStore(replacement)
    for name, offset in (("current", 0), ("large-old", -60), ("later", 15)):
        put(other, name, offset=offset)
    with sqlite3.connect(replacement) as db:
        db.execute("UPDATE forecasts SET payload='broken' WHERE id=?", (later["forecast_id"],))
    db.close()
    with other.connect() as db:
        db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    with ShadowStore(path).connect() as db:
        db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    replacement.replace(path)
    with pytest.raises(json.JSONDecodeError):
        selected(owner(ShadowStore(path)), current)


def test_external_cache_write_cannot_forge_a_validated_projection(tmp_path):
    path = tmp_path / "cache-tamper.sqlite3"
    current, _, later = populated(path)
    cache = cache_path(path)
    with sqlite3.connect(cache) as db:
        tables = [r[0] for r in db.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")]
        assert tables
        for table in tables:
            # Exact cache schema/UDF guards must reject external writes, or a
            # changed row must become a miss that is fully validated again.
            quoted = '"' + table.replace('"', '""') + '"'
            try:
                db.execute("DELETE FROM " + quoted)
            except sqlite3.DatabaseError:
                db.rollback()
    assert selected(owner(ShadowStore(path)), current) == [later["forecast_id"]]


def test_unrelated_append_preserves_old_durable_headers(tmp_path,monkeypatch):
 path=tmp_path/'append.sqlite3';current,old,later=populated(path)
 new=put(ShadowStore(path),'new',offset=30)
 cold=ShadowStore(path);decoded=spy(cold,monkeypatch)
 assert selected(owner(cold),current)==[later['forecast_id'],new['forecast_id']]
 assert decoded==[new['forecast_id']]


@pytest.mark.parametrize('failure',['missing','malformed','protection'])
def test_key_unavailable_is_only_a_cache_miss(tmp_path,monkeypatch,failure):
 from ml.intraday.forecast_header_cache import DurableForecastHeaders
 path=tmp_path/'key.sqlite3';current,old,later=populated(path);helper=DurableForecastHeaders(path)
 helper.key_path.unlink()
 if failure=='malformed':helper.key_path.write_bytes(b'not a protected cache key')
 elif failure=='protection':
  def denied(*a,**k):raise OSError('synthetic optional key protection failure')
  monkeypatch.setattr(DurableForecastHeaders,'_protect_secret',staticmethod(denied))
 cold=ShadowStore(path);decoded=spy(cold,monkeypatch)
 assert selected(owner(cold),current)==[later['forecast_id']]
 assert len(decoded)==3


def test_busy_cache_does_not_hold_source_or_wait_for_cache(tmp_path,monkeypatch):
 path=tmp_path/'busy.sqlite3';current,old,later=populated(path)
 cache=sqlite3.connect(cache_path(path),timeout=0);cache.execute('BEGIN EXCLUSIVE')
 try:
  cold=ShadowStore(path);decoded=spy(cold,monkeypatch)
  assert selected(owner(cold),current)==[later['forecast_id']]
  assert len(decoded)==3
 finally:cache.rollback();cache.close()


def test_forged_exact_schema_cache_cannot_replace_semantic_validation(tmp_path,monkeypatch):
 import hashlib
 from ml.intraday.forecast_header_cache import DurableForecastHeaders,_SCHEMA
 path=tmp_path/'forged.sqlite3';current,old,later=populated(path)
 cache=cache_path(path);db=sqlite3.connect(cache)
 try:
  version=db.execute('PRAGMA schema_version').fetchone()[0]
  rows=db.execute('SELECT cache_key,payload,record_mac FROM forecast_header_cache_rows').fetchall()
  key,raw,mac=next(row for row in rows if json.loads(row[1])['projection']['forecast_id']==later['forecast_id'])
  record=json.loads(raw);record['projection']['mode']='shadow'
  forged=json.dumps(record,sort_keys=True,separators=(',',':')).encode()
  db.execute('DROP TRIGGER forecast_header_cache_rows_update')
  db.execute('UPDATE forecast_header_cache_rows SET payload=?,record_mac=? WHERE cache_key=?',(forged,hashlib.sha256(forged).digest(),key))
  db.execute(_SCHEMA['forecast_header_cache_rows_update']);db.execute('PRAGMA schema_version='+str(version));db.commit()
 finally:db.close()
 cold=ShadowStore(path);decoded=spy(cold,monkeypatch)
 assert selected(owner(cold),current)==[later['forecast_id']]
 assert later['forecast_id'] in decoded


def test_replacement_between_open_and_read_disables_optional_cache(tmp_path,monkeypatch):
 path=tmp_path/'raced.sqlite3';current,old,later=populated(path)
 store=ShadowStore(path)
 with store.connect() as db:
  db.execute('BEGIN')
  # Existing source snapshot and rows stay valid; simulate uncertainty of the
  # opened-handle identity instead of replacing an open Windows SQLite file.
  store._connection_source_identities[id(db)]=None
  decoded=spy(store,monkeypatch)
  assert len(store._revision_forecast_headers(db,['f.pc=?','f.symbol=?'],['Atlas','AAPL']))==3
  assert len(decoded)==3


def test_cache_close_failure_cannot_abort_original_read(tmp_path,monkeypatch):
 from ml.intraday.forecast_header_cache import DurableForecastHeaders
 path=tmp_path/'close.sqlite3';current,old,later=populated(path)
 original=DurableForecastHeaders._connect
 class CloseFailure:
  def __init__(self,db):self.db=db
  def __getattr__(self,name):return getattr(self.db,name)
  def close(self):self.db.close();raise sqlite3.OperationalError('synthetic cache close failure')
 monkeypatch.setattr(DurableForecastHeaders,'_connect',lambda self:CloseFailure(original(self)))
 assert selected(owner(ShadowStore(path)),current)==[later['forecast_id']]


def test_replacement_with_identical_epoch_and_revision_still_invalidates_cache(tmp_path):
 import shutil
 from ml.intraday.store import _EXECUTION_FORECAST_SCHEMA
 path=tmp_path/'identical-source.sqlite3';current,old,later=populated(path)
 source=ShadowStore(path)
 with source.connect() as db:db.execute('PRAGMA wal_checkpoint(TRUNCATE)')
 replacement=tmp_path/'identical-replacement.sqlite3';shutil.copy2(path,replacement)
 db=sqlite3.connect(replacement)
 try:
  version=db.execute('PRAGMA schema_version').fetchone()[0]
  db.execute('DROP TRIGGER execution_forecast_update')
  db.execute("UPDATE forecasts SET payload='broken' WHERE id=?",(later['forecast_id'],))
  db.execute(_EXECUTION_FORECAST_SCHEMA['execution_forecast_update'])
  db.execute('PRAGMA schema_version='+str(version));db.commit()
 finally:db.close()
 replacement.replace(path)
 with pytest.raises(json.JSONDecodeError):selected(owner(ShadowStore(path)),current)


def test_oversized_forged_cache_blob_is_bounded_before_python_materialization(tmp_path,monkeypatch):
 from ml.intraday.forecast_header_cache import _SCHEMA
 path=tmp_path/'oversized.sqlite3';current,old,later=populated(path)
 db=sqlite3.connect(cache_path(path))
 try:
  version=db.execute('PRAGMA schema_version').fetchone()[0]
  key=next(k for k,raw in db.execute('SELECT cache_key,payload FROM forecast_header_cache_rows') if json.loads(raw)['projection']['forecast_id']==later['forecast_id'])
  db.execute('PRAGMA ignore_check_constraints=ON');db.execute('DROP TRIGGER forecast_header_cache_rows_update')
  db.execute('UPDATE forecast_header_cache_rows SET payload=zeroblob(10000000) WHERE cache_key=?',(key,))
  db.execute(_SCHEMA['forecast_header_cache_rows_update']);db.execute('PRAGMA schema_version='+str(version));db.commit()
 finally:db.close()
 cold=ShadowStore(path);decoded=spy(cold,monkeypatch)
 assert selected(owner(cold),current)==[later['forecast_id']]
 assert later['forecast_id'] in decoded


def test_path_stat_failure_after_semantic_validation_skips_only_cache_save(tmp_path,monkeypatch):
 from pathlib import Path
 path=tmp_path/'stat.sqlite3';store=ShadowStore(path);current=put(store,'current');later=put(store,'later',offset=15)
 original_stat=Path.stat;validator=store._forecast_value;validated=[]
 def checked(row):
  result=validator(row);validated.append(row[0]);return result
 def unavailable_after_validation(self,*a,**k):
  if self==path and validated:raise PermissionError('synthetic path disappeared after source snapshot opened')
  return original_stat(self,*a,**k)
 monkeypatch.setattr(store,'_forecast_value',checked);monkeypatch.setattr(Path,'stat',unavailable_after_validation)
 assert selected(owner(store),current)==[later['forecast_id']]
 assert len(validated)==2


def test_three_hundred_row_fresh_inventory_reuses_default_budget(tmp_path,monkeypatch):
 path=tmp_path/'representative.sqlite3';store=ShadowStore(path);current=put(store,'current')
 for index in range(1,301):put(store,'history-'+str(index),offset=index*15)
 assert len(store.execution_forecast_headers(pc='Atlas',symbol='AAPL',cache_io_budget_seconds=5))==301
 cold=ShadowStore(path);decoded=spy(cold,monkeypatch)
 assert len(cold.execution_forecast_headers(pc='Atlas',symbol='AAPL'))==301
 assert decoded==[]


def test_oversized_protected_key_is_never_provisioned(tmp_path,monkeypatch):
 from ml.intraday.forecast_header_cache import DurableForecastHeaders
 path=tmp_path/'oversized-key.sqlite3';store=ShadowStore(path);current=put(store,'current');later=put(store,'later',offset=15)
 monkeypatch.setattr(DurableForecastHeaders,'_protect_secret',staticmethod(lambda *a,**k:b'X'*70000))
 assert selected(owner(store),current)==[later['forecast_id']]
 assert not DurableForecastHeaders(path).key_path.exists()


def test_optional_source_resolve_failure_only_disables_cache(tmp_path,monkeypatch):
 from pathlib import Path
 path=tmp_path/'resolve.sqlite3';current,old,later=populated(path);cold=ShadowStore(path);decoded=spy(cold,monkeypatch)
 original=Path.resolve
 def unavailable(self,*a,**k):
  if self==path:raise PermissionError('synthetic optional cache path resolution failure')
  return original(self,*a,**k)
 monkeypatch.setattr(Path,'resolve',unavailable)
 assert selected(owner(cold),current)==[later['forecast_id']]
 assert len(decoded)==3
