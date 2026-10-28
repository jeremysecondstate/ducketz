"""Authenticated scalar proofs survive only a SQLite read interruption.

All records, keys and source identities are synthetic. No research payload,
provider, broker or application state is loaded by these portable tests.
"""
import hashlib
import importlib.util
import json
from pathlib import Path
import socket
import sqlite3
from types import SimpleNamespace

import pytest


SOURCE = Path(__file__).resolve().parents[1] / "ml/intraday/sizing_header_cache.py"
SPEC = importlib.util.spec_from_file_location("partial_sizing_cache_under_test", SOURCE)
cache = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(cache)


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    def denied(*args, **kwargs):
        pytest.fail("Scalar-header regressions must remain offline")
    monkeypatch.setattr(socket.socket, "connect", denied)
    monkeypatch.setattr(socket, "create_connection", denied)
    # This key belongs only to a temporary synthetic cache. HMAC validation
    # remains real; Windows account secrets and DPAPI are never accessed.
    monkeypatch.setattr(cache.DurableSizingHeaders, "_protect_secret",
                        staticmethod(lambda raw, **kwargs: raw))


@pytest.fixture
def inventory(tmp_path):
    source = tmp_path / "original.sqlite3"
    source.write_bytes(b"immutable synthetic source sentinel")
    helper = cache.DurableSizingHeaders(source, budget_seconds=5)
    identity = (str(source), 7, 11, 38, "synthetic-source-epoch")
    secret = b"K" * 32
    assert len(secret) == 32
    helper.key_path.write_bytes(b"ducketz-sizing-cache-key-v1\0" + secret)
    helper.key_path.chmod(0o600)
    with sqlite3.connect(helper.path) as db:
        for sql in cache._SCHEMA.values():
            db.execute(sql)
        db.execute("INSERT INTO sizing_header_cache_meta VALUES(1,?)", (b"E" * 16,))
        context = cache.DurableSizingHeaders._context(db)
        entries = {}
        for i in range(867):
            signature = (f"forecast-{i:04}", f"intention-{i:04}", i)
            projection = dict(forecast_id=signature[0], intention_id=signature[1],
                              side="BUY", desired_quantity=i+1, calculated_at=f"calculated-{i}")
            raw = cache._encoded(dict(contract=cache._CONTRACT,
                identity=list(identity), signature=list(signature),
                context=list(context), projection=projection)).encode()
            key = cache.DurableSizingHeaders._key(identity, signature, context)
            entries[key] = (signature, projection, raw,
                            cache.DurableSizingHeaders._authenticate(secret, raw))
        allowed = {key: (entry[2], entry[3]) for key, entry in entries.items()}
        db.create_function("sizing_header_cache_write_allowed", 3,
                           lambda key, raw, mac: int(allowed.get(key) == (raw, mac)))
        db.executemany("INSERT INTO sizing_header_cache_rows VALUES(?,?,?)",
                       [(key, *allowed[key]) for key in entries])
    return SimpleNamespace(source=source, helper=helper, identity=identity,
        secret=secret, entries=entries,
        signatures=[entry[0] for entry in entries.values()])


def pin_reader(monkeypatch, inventory, *, after=17, failure="interrupt",
               transform=None, close_failure=False):
    """Interrupt the actual SQLite cursor after a validated prefix.

    The connection uses a real progress-handler cancellation, so the raised
    exception carries SQLite's real SQLITE_INTERRUPT code. Other injected
    errors test the intentionally stricter non-interruption behavior.
    """
    state = SimpleNamespace(yielded=[], errors=[], closed=False)
    class Cursor:
        def __init__(self, cursor, db):
            self.cursor, self.db = cursor, db
        def __iter__(self):
            return self
        def __next__(self):
            if len(state.yielded) == after:
                if failure == "interrupt":
                    self.db.set_progress_handler(lambda: 1, 1)
                else:
                    error = {
                        "busy": sqlite3.OperationalError("database is locked"),
                        "corrupt": sqlite3.DatabaseError("synthetic corruption"),
                        "generic_operational": sqlite3.OperationalError("interrupted"),
                        "io": OSError("synthetic cache read failure"),
                        "value": ValueError("synthetic invalid record"),
                    }[failure]
                    state.errors.append(error)
                    raise error
            try:
                row = next(self.cursor)
            except sqlite3.Error as error:
                state.errors.append(error)
                raise
            if transform is not None:
                row = transform(len(state.yielded), row)
            state.yielded.append(row)
            return row
    class Connection:
        def __init__(self):
            self.db = sqlite3.connect(inventory.helper.path, timeout=0)
        def execute(self, sql, *args):
            cursor = self.db.execute(sql, *args)
            if "FROM sizing_header_cache_rows WHERE" in sql:
                return Cursor(cursor, self.db)
            return cursor
        def close(self):
            self.db.close()
            state.closed = True
            if close_failure:
                raise sqlite3.OperationalError("synthetic post-close failure")
    monkeypatch.setattr(inventory.helper, "_connect", Connection)
    return state


def read(inventory):
    return inventory.helper.load_many(inventory.identity, inventory.signatures)


def expected_prefix(inventory, state):
    return {inventory.entries[key][0]: inventory.entries[key][1]
            for key, _, _ in state.yielded}


@pytest.mark.parametrize("after", [0, 1, 17, 400, 866])
def test_real_sqlite_interrupt_retains_only_authenticated_prefix(tmp_path, monkeypatch, inventory, after):
    before_source = inventory.source.read_bytes()
    before_cache = hashlib.sha256(inventory.helper.path.read_bytes()).hexdigest()
    state = pin_reader(monkeypatch, inventory, after=after)
    result = read(inventory)
    assert len(state.yielded) == after
    assert state.errors and state.errors[-1].sqlite_errorcode == sqlite3.SQLITE_INTERRUPT
    assert result == expected_prefix(inventory, state)
    assert len(result) == after and state.closed
    assert inventory.source.read_bytes() == before_source
    assert hashlib.sha256(inventory.helper.path.read_bytes()).hexdigest() == before_cache


@pytest.mark.parametrize("failure", ["busy", "corrupt", "generic_operational", "io", "value"])
def test_other_read_errors_discard_prefix(monkeypatch, inventory, failure):
    state = pin_reader(monkeypatch, inventory, failure=failure)
    assert read(inventory) == {}
    assert len(state.yielded) == 17 and state.closed


@pytest.mark.parametrize("damage", ["mac", "contract", "identity", "signature",
                                    "context", "projection", "extra_field"])
def test_interrupt_never_retains_invalid_record(monkeypatch, inventory, damage):
    bad_key = []
    def changed(index, row):
        if index != 3:
            return row
        key, raw, mac = row
        bad_key.append(key)
        if damage == "mac":
            return key, raw, b"X" * 32
        record = json.loads(raw)
        if damage in {"contract", "identity", "signature", "context"}:
            record[damage] = "changed"
        elif damage == "projection":
            record["projection"]["desired_quantity"] = -1
        else:
            record["extra"] = "unsupported"
        raw = cache._encoded(record).encode()
        return key, raw, cache.DurableSizingHeaders._authenticate(inventory.secret, raw)
    state = pin_reader(monkeypatch, inventory, transform=changed)
    result = read(inventory)
    expected = expected_prefix(inventory, state)
    del expected[inventory.entries[bad_key[0]][0]]
    assert result == expected and len(result) == 16
    assert state.errors[-1].sqlite_errorcode == sqlite3.SQLITE_INTERRUPT


def test_authenticated_malformed_json_discards_prefix(monkeypatch, inventory):
    def malformed(index, row):
        if index != 3:
            return row
        raw = b"not JSON"
        return row[0], raw, cache.DurableSizingHeaders._authenticate(inventory.secret, raw)
    state = pin_reader(monkeypatch, inventory, transform=malformed)
    assert read(inventory) == {} and len(state.yielded) == 4 and state.closed


def test_interrupt_before_context_returns_no_proof(monkeypatch, inventory):
    connections = []
    def interrupted_connection():
        db = sqlite3.connect(inventory.helper.path, timeout=0)
        db.set_progress_handler(lambda: 1, 1)
        connections.append(db)
        return db
    monkeypatch.setattr(inventory.helper, "_connect", interrupted_connection)
    assert read(inventory) == {}
    with pytest.raises(sqlite3.ProgrammingError, match="closed"):
        connections[0].execute("SELECT 1")


def test_close_error_cannot_discard_authenticated_interrupt_prefix(monkeypatch, inventory):
    state = pin_reader(monkeypatch, inventory, close_failure=True)
    assert read(inventory) == expected_prefix(inventory, state) and state.closed


def test_nonpositive_budget_does_not_open_cache(monkeypatch, inventory):
    inventory.helper._remaining = 0
    monkeypatch.setattr(inventory.helper, "_connect",
                        lambda: pytest.fail("Disabled cache was opened"))
    assert read(inventory) == {}


def test_ordinary_budget_exhaustion_keeps_valid_prefix(monkeypatch, inventory):
    state = pin_reader(monkeypatch, inventory, after=900)
    clock = SimpleNamespace(now=0.)
    monkeypatch.setattr(cache, "time", SimpleNamespace(monotonic=lambda: clock.now))
    authenticate = inventory.helper._authenticate
    def measured(secret, raw):
        mac = authenticate(secret, raw)
        if len(state.yielded) == 17:
            clock.now = 5.
        return mac
    monkeypatch.setattr(inventory.helper, "_authenticate", measured)
    result = read(inventory)
    # The cursor fetches one additional row before the existing time check.
    expected = expected_prefix(inventory, state)
    del expected[inventory.entries[state.yielded[-1][0]][0]]
    assert result == expected and len(result) == 17 and state.closed


def test_full_uninterrupted_inventory_remains_identical(monkeypatch, inventory):
    state = pin_reader(monkeypatch, inventory, after=900)
    assert read(inventory) == {v[0]: v[1] for v in inventory.entries.values()}
    assert len(state.yielded) == 867 and not state.errors and state.closed


@pytest.mark.parametrize("changed", ["source_identity", "revision", "intention_identity", "schema", "epoch"])
def test_changed_source_or_cache_identity_rejects_old_proofs(monkeypatch, inventory, changed):
    if changed == "source_identity":
        inventory.identity = (*inventory.identity[:-1], "different-source-epoch")
    elif changed in {"revision", "intention_identity"}:
        position = 2 if changed == "revision" else 1
        inventory.signatures = [tuple((value + 1 if position == 2 else value + "-changed")
             if index == position else value for index, value in enumerate(signature))
             for signature in inventory.signatures]
    else:
        with sqlite3.connect(inventory.helper.path) as db:
            if changed == "schema":
                db.execute("CREATE TABLE sizing_header_cache_unexpected(value)")
            else:
                db.execute("DROP TRIGGER sizing_header_cache_meta_update")
                db.execute("UPDATE sizing_header_cache_meta SET epoch=?", (b"N" * 16,))
    state = pin_reader(monkeypatch, inventory, after=900)
    assert read(inventory) == {} and state.closed
