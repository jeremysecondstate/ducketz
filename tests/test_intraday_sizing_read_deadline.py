"""Causal synthetic read-deadline checks; no account, provider or live stores."""
from pathlib import Path
from types import SimpleNamespace
import hashlib
import importlib.util
import json
import sqlite3

import pytest


ROOT = Path(__file__).resolve().parents[1]


def module(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    value = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(value)
    return value


def synthetic(tmp_path, cache):
    source = tmp_path / "synthetic.sqlite3"
    source.write_bytes(b"original immutable synthetic source")
    helper = cache.DurableSizingHeaders(source)
    cache.DurableSizingHeaders._protect_secret = staticmethod(lambda raw, **kwargs: raw)
    secret = b"Q" * 32
    helper.key_path.write_bytes(b"ducketz-sizing-cache-key-v1\0" + secret)
    helper.key_path.chmod(0o600)
    identity = (str(source), 7, 11, 38, "synthetic-source-epoch")
    entries = {}
    with sqlite3.connect(helper.path) as db:
        for sql in cache._SCHEMA.values():
            db.execute(sql)
        db.execute("INSERT INTO sizing_header_cache_meta VALUES(1,?)", (b"E" * 16,))
        context = helper._context(db)
        for i in range(1200):
            signature = (f"forecast-{i:04}", f"intention-{i:04}", i)
            projection = dict(forecast_id=signature[0], intention_id=signature[1],
                              side="BUY", desired_quantity=i+1, calculated_at=f"calculated-{i}")
            raw = cache._encoded(dict(contract=cache._CONTRACT, identity=list(identity),
                signature=list(signature), context=list(context), projection=projection)).encode()
            key = helper._key(identity, signature, context)
            entries[key] = (signature, projection, raw, helper._authenticate(secret, raw))
        allowed = {key: (x[2], x[3]) for key, x in entries.items()}
        db.create_function("sizing_header_cache_write_allowed", 3,
            lambda key, raw, mac: int(allowed.get(key) == (raw, mac)))
        db.executemany("INSERT INTO sizing_header_cache_rows VALUES(?,?,?)",
            [(key, *allowed[key]) for key in entries])
    return SimpleNamespace(source=source, helper=helper, identity=identity,
        entries=entries, signatures=[x[0] for x in entries.values()])


class Clock:
    def __init__(self):
        self.at = 100.
    def monotonic(self):
        return self.at
    def advance(self, amount):
        self.at += amount


def delay(cache, clock, *, authentication=.00005):
    cache.time = SimpleNamespace(monotonic=clock.monotonic)
    original_key, original_auth = cache.DurableSizingHeaders._key, cache.DurableSizingHeaders._authenticate
    def key(*args):
        clock.advance(.00001)
        return original_key(*args)
    def authenticate(*args):
        clock.advance(authentication)
        return original_auth(*args)
    cache.DurableSizingHeaders._key = staticmethod(key)
    cache.DurableSizingHeaders._authenticate = staticmethod(authenticate)


def frozen(inv):
    return {str(p): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in (inv.source, inv.helper.path, inv.helper.key_path)}


def test_single_fifty_ms_connection_deadline_truncates_valid_whole_budget(tmp_path):
    outcomes = {}
    for name in ("baseline", "candidate"):
        cache = module(ROOT / "ml/intraday/sizing_header_cache.py", "causal_deadline_" + name)
        fixture_dir = tmp_path / name
        fixture_dir.mkdir()
        inv = synthetic(fixture_dir, cache)
        before = frozen(inv)
        clock = Clock()
        delay(cache, clock)
        if name == "baseline":
            # Exact previous connection deadline, with all remaining candidate
            # validation and load semantics identical for this causal contrast.
            def previous_connection():
                db = sqlite3.connect(inv.helper.path, timeout=0)
                deadline = cache.time.monotonic() + min(.05, max(0, inv.helper._remaining))
                db.set_progress_handler(lambda: int(cache.time.monotonic() > deadline), 1000)
                return db
            inv.helper._connect = previous_connection
        report = {}
        found = inv.helper.load_many(inv.identity, inv.signatures, diagnostics=report)
        expected = {x[0]: x[1] for x in inv.entries.values()}
        assert all(expected[k] == v for k, v in found.items())
        assert frozen(inv) == before
        assert not hasattr(inv.helper, "_load_deadline")
        outcomes[name] = (found, report, clock.at - 100)
    before, after = outcomes["baseline"], outcomes["candidate"]
    assert 0 < len(before[0]) < 1200
    assert before[1]["reason"] == "sqlite_interrupt"
    assert before[1]["sqlite_errorcode"] == sqlite3.SQLITE_INTERRUPT
    assert len(after[0]) == 1200 and after[1]["reason"] == "complete"
    assert after[1]["returned_count"] == after[1]["validated_count"] == 1200
    assert before[2] < .25 and .07 < after[2] < .25
    (tmp_path / "causal-deadline-result.json").write_text(json.dumps({
        key: {"returned_count": len(value[0]), "diagnostics": value[1],
              "controlled_elapsed_seconds": value[2]}
        for key, value in outcomes.items()}, indent=2), encoding="utf-8")


def test_candidate_retains_absolute_whole_operation_budget(tmp_path):
    cache = module(ROOT / "ml/intraday/sizing_header_cache.py", "causal_total_budget")
    inv = synthetic(tmp_path, cache)
    before = frozen(inv)
    clock = Clock()
    delay(cache, clock, authentication=.00025)
    report = {}
    found = inv.helper.load_many(inv.identity, inv.signatures, diagnostics=report)
    assert 0 < len(found) < 1200
    assert report["reason"] in {"sqlite_interrupt", "budget_exhausted"}
    assert .25 <= clock.at - 100 < .2503
    assert inv.helper._remaining <= 0
    assert frozen(inv) == before and not hasattr(inv.helper, "_load_deadline")
    expected = {x[0]: x[1] for x in inv.entries.values()}
    assert all(expected[k] == v for k, v in found.items())


def test_candidate_writer_connection_keeps_fifty_ms_deadline(tmp_path):
    cache = module(ROOT / "ml/intraday/sizing_header_cache.py", "causal_writer_guard")
    inv = synthetic(tmp_path, cache)
    clock = Clock()
    cache.time = SimpleNamespace(monotonic=clock.monotonic)
    db = inv.helper._connect()
    try:
        clock.advance(.06)
        with pytest.raises(sqlite3.OperationalError) as caught:
            db.execute("WITH RECURSIVE n(x) AS (VALUES(1) UNION ALL SELECT x+1 FROM n WHERE x<10000) SELECT sum(x) FROM n").fetchone()
        assert caught.value.sqlite_errorcode == sqlite3.SQLITE_INTERRUPT
    finally:
        db.close()


def test_candidate_load_deadline_is_removed_after_connection_failure(tmp_path):
    cache = module(ROOT / "ml/intraday/sizing_header_cache.py", "causal_connection_failure")
    inv = synthetic(tmp_path, cache)
    def failed():
        assert inv.helper._load_deadline > cache.time.monotonic()
        raise sqlite3.OperationalError("synthetic connection failure")
    inv.helper._connect = failed
    report = {}
    assert inv.helper.load_many(inv.identity, inv.signatures, diagnostics=report) == {}
    assert report["reason"] == "sqlite_error"
    assert not hasattr(inv.helper, "_load_deadline")
