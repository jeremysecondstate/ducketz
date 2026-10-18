from datetime import datetime, timezone, timedelta
from pathlib import Path
import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor

import pandas as pd
import pytest

from ml.intraday.market_collection import VERSION, canonical, snapshot_digest, load_snapshots
from ml.intraday.market_collection_index import (
    SnapshotRef, ReceiptIndexRequired, initialize_receipt_index, index_receipt,
    rebuild_receipt_index, selected_receipt_entries, legacy_snapshots,
)

NOW = pd.Timestamp("2026-10-16T20:00:00Z")


def save(root, identity, *, symbol="SDGR", source="source-a", receipt=NOW,
         available=None, status="COMPLETE", indexed=True, content=1):
    value = {"schema_version":VERSION, "snapshot_id":identity, "source_id":source,
        "source_kind":"stock_quote", "symbol":symbol, "provider":"fmp", "dataset":"stable", "schema":"quote",
        "receipt_at":pd.Timestamp(receipt).isoformat(), "available_at":pd.Timestamp(available or receipt).isoformat(),
        "status":status, "records":[{"symbol":symbol,"bid":content}], "evidence":{}}
    value["snapshot_sha256"] = snapshot_digest(value)
    path = root/"snapshots"/pd.Timestamp(receipt).date().isoformat()/identity/(identity+".json")
    path.parent.mkdir(parents=True,exist_ok=True)
    path.write_bytes(canonical(value))
    if indexed:
        index_receipt(root,path,value)
    return value,path


def test_indexed_decisions_only_read_selected_symbol_source_and_latest_status(tmp_path,monkeypatch):
    initialize_receipt_index(tmp_path)
    save(tmp_path,"old",receipt=NOW-timedelta(minutes=30))
    save(tmp_path,"first",receipt=NOW-timedelta(minutes=20))
    save(tmp_path,"second",receipt=NOW-timedelta(minutes=10),status="ERROR")
    save(tmp_path,"new",receipt=NOW+pd.Timedelta(1,"ns"))
    save(tmp_path,"global",symbol="*",source="global-source",receipt=NOW-timedelta(minutes=15))
    # Unrelated histories include invalid JSON: indexed reads never open them.
    for number in range(300):
        _,path=save(tmp_path,"other-"+str(number),symbol="GLOB" if number%2 else "SDGR",
            source="unselected-source",receipt=NOW-timedelta(minutes=30))
        path.write_text("not json")
    reads=[]
    original=Path.read_text
    def read(path,*args,**kwargs):
        reads.append(path)
        return original(path,*args,**kwargs)
    monkeypatch.setattr(Path,"read_text",read)
    result=load_snapshots(tmp_path,symbol="SDGR",cutoff=NOW,since=NOW-timedelta(days=1),
        decision_cutoffs=[NOW-timedelta(minutes=16),NOW], source_ids={"stock_quote":["source-a","global-source"]})
    assert {row["snapshot_id"] for row in result} == {"first","second","global"}
    assert len(reads)==3 and next(row for row in result if row["snapshot_id"]=="second")["status"]=="ERROR"


def test_latest_clock_conflicts_and_one_nanosecond_visibility_are_preserved(tmp_path):
    initialize_receipt_index(tmp_path)
    save(tmp_path,"a",receipt=NOW)
    save(tmp_path,"conflict",receipt=NOW,content=2)
    save(tmp_path,"plus-one",receipt=NOW+pd.Timedelta(1,"ns"))
    save(tmp_path,"future-available",receipt=NOW,available=NOW+pd.Timedelta(2,"ns"))
    def read(cutoff):
        return load_snapshots(tmp_path,symbol="SDGR",cutoff=cutoff,
            decision_cutoffs=[cutoff],source_ids=["source-a"])
    assert {row["snapshot_id"] for row in read(NOW)}=={"a","conflict"}
    assert [row["snapshot_id"] for row in read(NOW+pd.Timedelta(1,"ns"))]==["plus-one"]
    assert [row["snapshot_id"] for row in read(NOW+pd.Timedelta(2,"ns"))]==["future-available"]


def test_lazy_headers_do_not_read_payloads_and_verify_on_demand(tmp_path,monkeypatch):
    initialize_receipt_index(tmp_path)
    saved,path=save(tmp_path,"one")
    original=Path.read_text
    reads=[]
    def read(path,*args,**kwargs):
        reads.append(path)
        return original(path,*args,**kwargs)
    monkeypatch.setattr(Path,"read_text",read)
    refs=load_snapshots(tmp_path,symbol="SDGR",cutoff=NOW,decision_cutoffs=[NOW],source_ids=["source-a"],lazy=True)
    assert len(refs)==1 and isinstance(refs[0],SnapshotRef)
    assert refs[0]["snapshot_sha256"]==saved["snapshot_sha256"] and reads==[]
    refs[0].verify_unchanged()
    assert reads==[] and refs[0].load_verified()==saved and reads==[path]
    assert refs[0].load_verified()==saved and reads==[path,path]
    path.write_bytes(path.read_bytes()+b" ")
    with pytest.raises(ValueError,match="changed"):
        refs[0].verify_unchanged()


def test_index_metadata_and_full_content_mismatch_fail_closed(tmp_path):
    initialize_receipt_index(tmp_path)
    saved,path=save(tmp_path,"one")
    with sqlite3.connect(tmp_path/"receipts.sqlite3") as connection:
        connection.execute("UPDATE receipts SET sha256='wrong'")
    with pytest.raises(ValueError,match="header"):
        load_snapshots(tmp_path,symbol="SDGR",cutoff=NOW,lazy=True)


def test_legacy_fallback_is_bounded_and_explicit_migration_preserves_files(tmp_path):
    saved,path=save(tmp_path,"legacy",indexed=False)
    before=path.read_bytes()
    assert load_snapshots(tmp_path,symbol="SDGR",cutoff=NOW)==(saved,)
    initialize_receipt_index(tmp_path)
    assert selected_receipt_entries(tmp_path,symbol="SDGR",cutoff=NOW,since=NOW-timedelta(days=1)) is None
    save(tmp_path,"second",indexed=False)
    with pytest.raises(ReceiptIndexRequired):
        legacy_snapshots(tmp_path,symbol="SDGR",cutoff=NOW,since=NOW-timedelta(days=1),max_files=1)
    with pytest.raises(ReceiptIndexRequired):
        rebuild_receipt_index(tmp_path,max_files=1)
    result=rebuild_receipt_index(tmp_path,max_files=2)
    assert result["indexed_files"]==2 and result["inserted_rows"]==2 and path.read_bytes()==before
    assert len(load_snapshots(tmp_path,symbol="SDGR",cutoff=NOW))==2
    assert rebuild_receipt_index(tmp_path,max_files=2)["inserted_rows"]==0


def test_concurrent_append_index_and_readers_preserve_all_receipts(tmp_path):
    initialize_receipt_index(tmp_path)
    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(lambda n:save(tmp_path,"record-"+str(n),receipt=NOW-pd.Timedelta(n,"ns")),range(24)))
    rows=load_snapshots(tmp_path,symbol="SDGR",cutoff=NOW)
    assert len(rows)==24 and len({row["snapshot_id"] for row in rows})==24
    before=(tmp_path/"receipts.sqlite3").stat().st_mtime_ns
    assert len(load_snapshots(tmp_path,symbol="SDGR",cutoff=NOW,lazy=True))==24
    assert (tmp_path/"receipts.sqlite3").stat().st_mtime_ns==before


def test_orphan_after_index_failure_recovers_without_provider_replay(tmp_path,monkeypatch):
    from ml.intraday.market_collection import QuarterHourMarketCollector,SourceRequest,CollectionResult
    import ml.intraday.market_collection_index as module
    calls=[]
    source=SourceRequest("fmp","stock_quote","SDGR","stable","quote")
    collector=QuarterHourMarketCollector(tmp_path,symbols=["SDGR"],requests=[source],
        adapters={"fmp":lambda *a:(calls.append(1) or CollectionResult([]))},clock=lambda:NOW)
    collector.enqueue(NOW-timedelta(minutes=15))
    original=module.index_receipt
    def failure(*args):
        raise RuntimeError("simulated index interruption")
    monkeypatch.setattr(module,"index_receipt",failure)
    with pytest.raises(RuntimeError,match="interruption"):
        collector.step()
    monkeypatch.setattr(module,"index_receipt",original)
    collector.step()
    assert len(calls)==1 and len(load_snapshots(tmp_path,symbol="SDGR",cutoff=NOW))==1


def test_index_seek_plan_uses_scope_source_clock_index(tmp_path):
    initialize_receipt_index(tmp_path)
    save(tmp_path,"one")
    with sqlite3.connect(tmp_path/"receipts.sqlite3") as connection:
        plan=connection.execute("EXPLAIN QUERY PLAN SELECT snapshot_id FROM receipts INDEXED BY receipt_seek WHERE symbol=? AND source_id=? AND available_ns<=? ORDER BY available_ns DESC,receipt_ns DESC LIMIT 1",("SDGR","source-a",NOW.value)).fetchall()
    assert any("SEARCH" in row[3] and "receipt_seek" in row[3] for row in plan)
