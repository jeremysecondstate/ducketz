"""Synthetic real-SQLite watchdog hypothesis; no production paths or networking."""
import socket
import sqlite3
from types import SimpleNamespace

import pytest

from ml.intraday import forecast_header_cache as cache


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    def denied(*args, **kwargs):
        pytest.fail("Budget hypothesis must remain offline")
    monkeypatch.setattr(socket.socket, "connect", denied)
    monkeypatch.setattr(socket, "create_connection", denied)
    monkeypatch.setattr(cache.DurableForecastHeaders, "_protect_secret", staticmethod(lambda raw, **kw: raw))


@pytest.fixture
def inventory(tmp_path):
    source = tmp_path / "synthetic-source.sqlite3"
    source.write_bytes(b"source sentinel")
    helper = cache.DurableForecastHeaders(source, budget_seconds=.25)
    identity = (str(source), 7, 11, 38, "synthetic-epoch")
    secret = b"K" * 32
    helper.key_path.write_bytes(b"ducketz-header-cache-key-v1\0" + secret)
    helper.key_path.chmod(0o600)
    signatures, expected = [], {}
    with sqlite3.connect(helper.path) as db:
        for sql in cache._SCHEMA.values():
            db.execute(sql)
        db.execute("INSERT INTO forecast_header_cache_meta VALUES(1,?)", (b"E" * 16,))
        context = helper._context(db)
        records = {}
        for index in range(920):
            signature = (f"forecast-{index:04}", "Atlas", "AAPL", f"cycle-{index}", f"reference-{index}", f"issue-{index}", index)
            projection = dict(zip(cache._INDEXED, signature[:6]), mode="live", target_open=f"open-{index}", target_close=f"close-{index}")
            raw = cache._encoded(dict(contract=cache._CONTRACT, identity=list(identity), signature=list(signature), context=list(context), projection=projection)).encode()
            key = helper._key(identity, signature, context)
            records[key] = raw, helper._authenticate(secret, raw)
            signatures.append(signature)
            expected[signature] = projection
        db.create_function("forecast_header_cache_write_allowed", 3, lambda key, raw, mac: int(records.get(key) == (raw, mac)))
        db.executemany("INSERT INTO forecast_header_cache_rows VALUES(?,?,?)", [(key, *value) for key, value in records.items()])
    return SimpleNamespace(source=source, helper=helper, identity=identity, signatures=signatures, expected=expected)


def clocked(monkeypatch, inventory, *, key_time=0., authentication_time=0.):
    clock = SimpleNamespace(now=0., authentications=0)
    monkeypatch.setattr(cache, "time", SimpleNamespace(monotonic=lambda: clock.now))
    original_key = inventory.helper._key
    def key(*args):
        result = original_key(*args)
        clock.now += key_time
        return result
    original_auth = inventory.helper._authenticate
    def authenticate(*args):
        result = original_auth(*args)
        clock.now += authentication_time
        clock.authentications += 1
        return result
    monkeypatch.setattr(inventory.helper, "_key", key)
    monkeypatch.setattr(inventory.helper, "_authenticate", authenticate)
    return clock


def test_configured_budget_covers_key_generation_before_sql_step(monkeypatch, inventory):
    before = inventory.source.read_bytes()
    clock = clocked(monkeypatch, inventory, key_time=.08 / 920)
    report = {}
    result = inventory.helper.load_many(inventory.identity, inventory.signatures, diagnostics=report)
    assert result == inventory.expected, report
    assert report["reason"] == "complete"
    assert clock.now == pytest.approx(.08)
    assert inventory.source.read_bytes() == before


def test_configured_budget_covers_authenticated_row_validation(monkeypatch, inventory):
    clock = clocked(monkeypatch, inventory, key_time=.00001, authentication_time=.00015)
    report = {}
    result = inventory.helper.load_many(inventory.identity, inventory.signatures, diagnostics=report)
    assert result == inventory.expected, report
    assert report["reason"] == "complete"
    assert clock.now == pytest.approx(.1472)


def test_total_budget_still_stops_and_retains_only_authenticated_prefix(monkeypatch, inventory):
    clock = clocked(monkeypatch, inventory, authentication_time=.001)
    report = {}
    result = inventory.helper.load_many(inventory.identity, inventory.signatures, diagnostics=report)
    assert 0 < len(result) < 920
    assert all(inventory.expected[key] == value for key, value in result.items())
    assert report["reason"] in {"budget_exhausted", "sqlite_interrupt"}
    assert report["validated_count"] == report["returned_count"] == len(result)
    assert report["discarded_count"] == report["rejected_count"] == 0
    assert clock.now <= .251


def test_zero_budget_opens_no_sqlite_connection(monkeypatch, inventory):
    inventory.helper._remaining = 0.
    monkeypatch.setattr(inventory.helper, "_connect", lambda: pytest.fail("Disabled budget opened cache"))
    report = {}
    assert inventory.helper.load_many(inventory.identity, inventory.signatures, diagnostics=report) == {}
    assert report["reason"] == "budget_disabled"
