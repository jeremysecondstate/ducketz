"""Execution chronology retains immutable history without model-tree decodes."""
from datetime import timedelta
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
import socket
from types import SimpleNamespace

import pytest

from ml.intraday.account_execution import IntradayAccountExecutor
from ml.intraday.prediction import ContractError, digest, encoded, instant
from ml.intraday.store import ShadowStore
from test_intraday_account_execution import env
from test_intraday_store import forecast


@pytest.fixture(autouse=True)
def deny_network(monkeypatch):
    def denied(*args, **kwargs):
        pytest.fail("Execution chronology checks must not contact providers")
    monkeypatch.setattr(socket.socket, "connect", denied)
    monkeypatch.setattr(socket, "create_connection", denied)


def put(store, name, *, offset=0, **changes):
    value = forecast(cycle_id=name, mode="live")
    for field in ("reference_at", "target_open", "target_close", "issued_at",
                  "information_available_at", "data_through"):
        value[field] = (instant(value[field]) + timedelta(minutes=offset)).isoformat()
    value.update(changes)
    value["forecast_id"] = digest(value)
    with store.connect() as db:
        db.execute("INSERT INTO forecasts VALUES(?,?,?,?,?,?,?)",
            (*(value[field] for field in ("forecast_id", "pc", "symbol", "cycle_id", "reference_at", "issued_at")),
             encoded(value)))
    return value


def owner(store, start="2026-10-08T17:15:00Z"):
    return SimpleNamespace(store=store, actor="Atlas", session_started_at=instant(start))


def original_selection(executor, current):
    return [f["forecast_id"] for f in executor.store.forecasts(pc=executor.actor, symbol=current["symbol"])
            if f["symbol"] == current["symbol"] and f["mode"] == "live"
            and f["ensemble_status"] == "full"
            and instant(f["issued_at"]) >= executor.session_started_at
            and (instant(f["reference_at"]), instant(f["issued_at"])) >
                (instant(current["reference_at"]), instant(current["issued_at"]))]


def selected(executor, current):
    return IntradayAccountExecutor._newer_forecasts(executor, current)


def spy(store, monkeypatch):
    decoded = []
    validator = store._forecast_value
    def checked(row):
        decoded.append(row[0])
        return validator(row)
    monkeypatch.setattr(store, "_forecast_value", checked)
    return decoded


def test_ordered_scope_and_timezone_selection_matches_original_without_model_decodes(tmp_path, monkeypatch):
    store = ShadowStore(tmp_path / "history.sqlite3")
    current = put(store, "current")
    later = put(store, "later-first", offset=30)
    put(store, "old", offset=-60)
    same_ref = put(store, "same-reference-later-issue", issued_at="2026-10-08T10:16:03-07:00")
    equivalent = put(store, "same-instants", reference_at="2026-10-08T10:00:00-07:00",
                     issued_at="2026-10-08T10:16:02-07:00")
    second = put(store, "later-second", offset=15)
    put(store, "before-session", offset=30, issued_at="2026-10-08T17:14:59Z")
    put(store, "shadow", offset=45, mode="shadow")
    put(store, "incomplete", offset=45, ensemble_status="partial")
    put(store, "peer", offset=45, pc="Scout")
    put(store, "other-symbol", offset=45, symbol="AMZN")
    for index in range(12):
        put(store, "large-history-" + str(index), offset=-15 * (index + 1),
            model_diagnostics={"large": "retained-model-tree" * 10000, "tiny": 1e-7, "unicode": "雪"})
    executor = owner(store)
    decoded = spy(store, monkeypatch)
    expected = original_selection(executor, current)
    assert expected == [later["forecast_id"], same_ref["forecast_id"], second["forecast_id"]]
    assert equivalent["forecast_id"] not in expected and len(decoded) == 21
    decoded.clear()
    assert selected(executor, current) == expected
    assert len(decoded) == 21  # First observation retains full validation.
    decoded.clear()
    assert selected(executor, current) == expected
    assert decoded == []
    with store.connect() as db:
        assert db.execute("SELECT count(*) FROM forecasts").fetchone()[0] == 23


@pytest.mark.parametrize("encoding", ["canonical", "indent", "unicode", "reordered"])
def test_semantically_equal_encodings_keep_selection_and_validation(tmp_path, monkeypatch, encoding):
    store = ShadowStore(tmp_path / "encoding.sqlite3")
    current = put(store, "current")
    later = put(store, "later", offset=15, details={"unicode": "雪", "tiny": 1e-7})
    payload = dict(reversed(list(later.items()))) if encoding == "reordered" else later
    raw = json.dumps(payload, sort_keys=encoding != "reordered", ensure_ascii=encoding != "unicode",
                     indent=2 if encoding == "indent" else None, separators=(",", ":"))
    with store.connect() as db:
        db.execute("UPDATE forecasts SET payload=? WHERE id=?", (raw, later["forecast_id"]))
    executor = owner(store)
    expected = original_selection(executor, current)
    decoded = spy(store, monkeypatch)
    assert selected(executor, current) == expected == [later["forecast_id"]]
    assert decoded == [current["forecast_id"], later["forecast_id"]]
    decoded.clear()
    assert selected(executor, current) == expected and decoded == []


@pytest.mark.parametrize("old", [False, True])
@pytest.mark.parametrize("damage", ["body", "forecast_id", "indexed_reference", "indexed_issue", "indexed_cycle"])
def test_corruption_in_selected_or_obsolete_history_is_not_hidden(tmp_path, old, damage):
    store = ShadowStore(tmp_path / "corrupt.sqlite3")
    current = put(store, "current")
    other = put(store, "other", offset=-60 if old else 15)
    selected(owner(store), current)  # Corruption must invalidate a warm cache.
    with store.connect() as db:
        if damage == "body":
            db.execute("UPDATE forecasts SET payload=json_set(payload,'$.probability_not_down',0.99) WHERE id=?",
                       (other["forecast_id"],))
        elif damage == "forecast_id":
            db.execute("UPDATE forecasts SET payload=json_set(payload,'$.forecast_id','wrong') WHERE id=?",
                       (other["forecast_id"],))
        else:
            column = {"indexed_reference": "reference_at", "indexed_issue": "issued_at", "indexed_cycle": "cycle_id"}[damage]
            db.execute(f"UPDATE forecasts SET {column}=? WHERE id=?", ("different", other["forecast_id"]))
    for select in (original_selection, selected):
        with pytest.raises(ContractError, match="hash or indexed identity"):
            select(owner(store), current)


@pytest.mark.parametrize("payload,exception", [("not json", json.JSONDecodeError), ("[]", ContractError)])
def test_malformed_history_keeps_original_error(tmp_path, payload, exception):
    store = ShadowStore(tmp_path / "malformed.sqlite3")
    current = put(store, "current")
    old = put(store, "old", offset=-60)
    with store.connect() as db:
        db.execute("UPDATE forecasts SET payload=? WHERE id=?", (payload, old["forecast_id"]))
    for select in (original_selection, selected):
        with pytest.raises(exception):
            select(owner(store), current)


@pytest.mark.parametrize("changes,expected_error", [
    ({"issued_at": "not-a-timestamp"}, ContractError),
    ({"reference_at": "2026-10-08T17:15:00"}, ContractError),
    ({"mode": None}, None),
    ({"ensemble_status": 1}, None),
    ({"mode": "shadow", "issued_at": "not-a-timestamp"}, None),
])
def test_irregular_signed_headers_keep_original_short_circuit_semantics(tmp_path, changes, expected_error):
    store = ShadowStore(tmp_path / "headers.sqlite3")
    current = put(store, "current")
    put(store, "other", offset=15, **changes)
    if expected_error:
        for select in (original_selection, selected):
            with pytest.raises(expected_error):
                select(owner(store), current)
    else:
        assert selected(owner(store), current) == original_selection(owner(store), current) == []


def test_projected_chronology_runs_at_submission_without_duplicate_order(env, monkeypatch):
    current = env.forecast()
    calls = []
    method = env.store.execution_forecast_headers
    def observed(**kwargs):
        calls.append(kwargs)
        return method(**kwargs)
    monkeypatch.setattr(env.store, "execution_forecast_headers", observed)
    result = env.executor.step(current)
    assert result["disposition"] == "submitted"
    assert len(calls) == 2 and all(c == {"pc": "Atlas", "symbol": current["symbol"]} for c in calls)
    assert env.executor.step(current)["disposition"] == "recover_existing_intention"
    assert len(env.broker.submissions) == 1


def test_raw_signed_noncanonical_history_cannot_replace_semantic_identity(tmp_path):
    store = ShadowStore(tmp_path / "raw-signed.sqlite3")
    current = put(store, "current")
    invalid = dict(reversed(list(forecast(cycle_id="bad", mode="live").items())))
    raw = json.dumps(invalid, separators=(",", ":"))
    invalid["forecast_id"] = hashlib.sha256(raw.encode()).hexdigest()
    assert invalid["forecast_id"] != digest({k: v for k, v in invalid.items() if k != "forecast_id"})
    with store.connect() as db:
        db.execute("INSERT INTO forecasts VALUES(?,?,?,?,?,?,?)",
            (*(invalid[k] for k in ("forecast_id", "pc", "symbol", "cycle_id", "reference_at", "issued_at")),
             json.dumps(invalid, separators=(",", ":"))))
    for select in (original_selection, selected):
        with pytest.raises(ContractError, match="hash or indexed identity"):
            select(owner(store), current)


def test_valid_blob_and_returned_header_mutation_preserve_originals(tmp_path):
    store = ShadowStore(tmp_path / "blob.sqlite3")
    current = put(store, "current")
    later = put(store, "later", offset=15)
    headers = store.execution_forecast_headers(pc="Atlas", symbol="AAPL")
    headers[1]["mode"] = "shadow"
    assert selected(owner(store), current) == [later["forecast_id"]]
    with store.connect() as db:
        db.execute("UPDATE forecasts SET payload=CAST(payload AS BLOB) WHERE id=?", (later["forecast_id"],))
    assert selected(owner(store), current) == original_selection(owner(store), current) == [later["forecast_id"]]


def test_header_cache_is_bounded_under_concurrent_readers(tmp_path):
    store = ShadowStore(tmp_path / "bound.sqlite3")
    def read(index):
        value = forecast(cycle_id="cache-" + str(index))
        value["forecast_id"] = digest(value)
        row = (*(value[k] for k in ("forecast_id", "pc", "symbol", "cycle_id", "reference_at", "issued_at")), encoded(value))
        header, _ = store._forecast_projection(row)
        assert header["forecast_id"] == value["forecast_id"]
    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(read, range(4400)))
    assert len(store._forecast_header_cache) <= 4096
