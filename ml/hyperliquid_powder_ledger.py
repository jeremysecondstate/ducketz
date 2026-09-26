"""Durable live-order intent and reconciliation evidence; no exchange calls.

This ledger is separate from PaperLedger and does not invent portfolio balances,
fills, cashflows or P/L. An intent is committed before submission; UNKNOWN is a
reconciliation obligation, never permission to submit again.
"""
from __future__ import annotations

from contextlib import contextmanager
import hashlib
import json
import math
from pathlib import Path
import sqlite3
import time
from uuid import uuid4


STATES = frozenset({"PREPARED", "SUBMITTING", "UNKNOWN", "OPEN", "FILLED", "CANCELED", "REJECTED"})
TERMINAL_STATES = frozenset({"FILLED", "CANCELED", "REJECTED"})
_TRANSITIONS = {
    "PREPARED": {"SUBMITTING", "REJECTED"},
    "SUBMITTING": {"UNKNOWN", "OPEN", "FILLED", "CANCELED", "REJECTED"},
    "UNKNOWN": {"UNKNOWN", "OPEN", "FILLED", "CANCELED", "REJECTED"},
    "OPEN": {"OPEN", "UNKNOWN", "FILLED", "CANCELED"},
    "FILLED": {"FILLED"}, "CANCELED": {"CANCELED"}, "REJECTED": {"REJECTED"},
}
_SCHEMA = "hyperliquid-powder-intents-v1"


def _json(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _number(value, *, positive=False):
    if isinstance(value, bool):
        raise ValueError("Expected a finite numeric value")
    try:
        result = float(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError("Expected a finite numeric value") from exc
    if not math.isfinite(result) or positive and result <= 0:
        raise ValueError("Expected a finite positive value" if positive else "Expected a finite value")
    return result


def _object(value, name):
    if not isinstance(value, dict):
        raise ValueError(f"{name} must be an object")
    return json.loads(_json(value))


def _intent(row):
    if row is None:
        return None
    value = dict(row)
    for key in ("request", "decision", "result"):
        value[key] = json.loads(value.pop(key + "_json"))
    return value


def _fill(row):
    value = dict(row)
    payload = json.loads(value.pop("data_json"))
    return {**payload, **value}


def _event(row):
    value = dict(row)
    value["details"] = json.loads(value.pop("details_json"))
    return value


def _metadata(connection):
    rows = {row[0]: json.loads(row[1]) for row in connection.execute(
        "SELECT key,value_json FROM metadata WHERE key IN ('binding','baseline')")}
    return {"binding": rows.get("binding"), "baseline": rows.get("baseline")}


class PowderLedger:
    def __init__(self, path, *, clock=time.time):
        self.path = Path(path).resolve()
        self.clock = clock
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(self.path, timeout=2.0, isolation_level=None)
        self.connection.row_factory = sqlite3.Row
        try:
            tables = {row[0] for row in self.connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if tables and "metadata" not in tables:
                raise ValueError("Refusing to initialize Powder in a different ledger/database")
            if "metadata" in tables:
                schema = self.connection.execute("SELECT value_json FROM metadata WHERE key='schema'").fetchone()
                if schema is None or json.loads(schema[0]) != _SCHEMA:
                    raise ValueError("Unsupported Powder ledger schema")
            self.connection.execute("PRAGMA journal_mode=WAL")
            self.connection.execute("PRAGMA synchronous=FULL")
            self.connection.execute("PRAGMA foreign_keys=ON")
            self.connection.executescript("""
                CREATE TABLE IF NOT EXISTS metadata (key TEXT PRIMARY KEY,value_json TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS observations (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,recorded_at REAL NOT NULL,data_json TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS intents (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,cloid TEXT UNIQUE,account TEXT NOT NULL,
                    state TEXT NOT NULL,created_at REAL NOT NULL,updated_at REAL NOT NULL,
                    request_json TEXT NOT NULL,reason TEXT NOT NULL,decision_json TEXT NOT NULL,
                    result_json TEXT NOT NULL DEFAULT '{}');
                CREATE TABLE IF NOT EXISTS intent_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,intent_id INTEGER REFERENCES intents(id),
                    created_at REAL NOT NULL,event TEXT NOT NULL,details_json TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS fills (
                    account TEXT NOT NULL,tid TEXT NOT NULL,intent_id INTEGER NOT NULL REFERENCES intents(id),
                    symbol TEXT NOT NULL,quantity REAL NOT NULL,price REAL NOT NULL,fee REAL NOT NULL,
                    data_json TEXT NOT NULL,PRIMARY KEY(account,tid));
                CREATE TABLE IF NOT EXISTS cooldowns (
                    account TEXT NOT NULL,coin TEXT NOT NULL,timestamp REAL NOT NULL,
                    PRIMARY KEY(account,coin));
                CREATE INDEX IF NOT EXISTS intents_state ON intents(state);
                CREATE INDEX IF NOT EXISTS fills_intent ON fills(intent_id);
            """)
            with self._transaction():
                self.connection.execute("INSERT OR IGNORE INTO metadata VALUES ('schema',?)", (_json(_SCHEMA),))
                self.connection.execute("INSERT OR IGNORE INTO metadata VALUES ('nonce',?)", (_json(uuid4().hex),))
        except BaseException:
            self.connection.close()
            raise

    @contextmanager
    def _transaction(self):
        self.connection.execute("BEGIN IMMEDIATE")
        try:
            yield
            self.connection.commit()
        except BaseException:
            self.connection.rollback()
            raise

    def _event(self, intent_id, event, details):
        self.connection.execute("INSERT INTO intent_events(intent_id,created_at,event,details_json) VALUES (?,?,?,?)",
                                (intent_id, _number(self.clock()), event, _json(details)))

    def activate(self, binding, baseline):
        binding, baseline = _object(binding, "binding"), _object(baseline, "baseline")
        if not binding or not baseline:
            raise ValueError("Activation requires explicit account/config binding and an actual baseline")
        with self._transaction():
            current = _metadata(self.connection)
            if current["binding"] is not None:
                if current["binding"] != binding:
                    raise ValueError("Powder activation binding changed; refusing account/config reassignment")
                return
            self.connection.execute("INSERT INTO metadata VALUES ('binding',?)", (_json(binding),))
            self.connection.execute("INSERT INTO metadata VALUES ('baseline',?)", (_json(baseline),))
            self._event(None, "activated", {"binding": binding})

    activation = activate

    def metadata(self):
        return _metadata(self.connection)

    def record_observation(self, observation):
        observation = _object(observation, "observation")
        with self._transaction():
            self.connection.execute("INSERT INTO observations(recorded_at,data_json) VALUES (?,?)",
                                    (_number(self.clock()), _json(observation)))

    def latest_observation(self):
        row = self.connection.execute("SELECT data_json FROM observations ORDER BY id DESC LIMIT 1").fetchone()
        return json.loads(row[0]) if row else None

    def prepare(self, request, reason, decision):
        request, decision = _object(request, "request"), _object(decision, "decision")
        if not isinstance(request.get("account"), str) or not request["account"]:
            raise ValueError("An intent requires an account")
        if not isinstance(request.get("symbol"), str) or not request["symbol"]:
            raise ValueError("An intent requires an explicit market symbol")
        if type(request.get("is_buy")) is not bool or type(request.get("reduce_only")) is not bool:
            raise ValueError("Intent side and reduce_only must be explicit booleans")
        _number(request.get("size"), positive=True)
        _number(request.get("limit_price"), positive=True)
        if not isinstance(reason, str) or not reason:
            raise ValueError("An intent requires a reason")
        now = _number(self.clock())
        with self._transaction():
            if _metadata(self.connection)["binding"] is None:
                raise ValueError("Activate and pin account binding before preparing an intent")
            cursor = self.connection.execute("""INSERT INTO intents(account,state,created_at,updated_at,
                request_json,reason,decision_json) VALUES (?,?,?,?,?,?,?)""",
                (request["account"], "PREPARED", now, now, _json(request), reason, _json(decision)))
            identity = cursor.lastrowid
            nonce = json.loads(self.connection.execute("SELECT value_json FROM metadata WHERE key='nonce'").fetchone()[0])
            cloid = "0x" + hashlib.sha256(f"{nonce}:{request['account']}:{identity}".encode()).hexdigest()[:32]
            self.connection.execute("UPDATE intents SET cloid=? WHERE id=?", (cloid, identity))
            self._event(identity, "prepared", {"cloid": cloid})
        return self.get_intent(identity)

    def get_intent(self, identity):
        row = self.connection.execute("SELECT * FROM intents WHERE id=?", (identity,)).fetchone()
        if row is None:
            raise ValueError("Unknown Powder intent")
        return _intent(row)

    def mark_submitting(self, identity):
        with self._transaction():
            intent = self.get_intent(identity)
            if intent["state"] != "PREPARED":
                raise ValueError("Only a PREPARED intent can be submitted; unresolved intents must be reconciled")
            self.connection.execute("UPDATE intents SET state='SUBMITTING',updated_at=? WHERE id=?",
                                    (_number(self.clock()), identity))
            self._event(identity, "submitting", {})

    def mark_unknown(self, identity, detail):
        self.resolve(identity, {"state": "UNKNOWN", "detail": str(detail)})

    def resolve(self, identity, result):
        result = _object(result, "reconciliation result")
        state = result.get("state")
        if state not in STATES or state in {"PREPARED", "SUBMITTING"}:
            raise ValueError("Reconciliation requires an explicit exchange/evidence state")
        new_fills = result.get("fills", [])
        if not isinstance(new_fills, list):
            raise ValueError("Reconciled fills must be a list")
        with self._transaction():
            intent = self.get_intent(identity)
            if state not in _TRANSITIONS[intent["state"]]:
                raise ValueError(f"Invalid intent transition: {intent['state']} -> {state}")
            request = intent["request"]
            if new_fills and (intent["state"] == "PREPARED" or state == "REJECTED"):
                raise ValueError("An unsubmitted/rejected intent cannot contain fills")
            for original in new_fills:
                fill = _object(original, "fill")
                if fill.get("account") != request["account"]:
                    raise ValueError("Fill account does not match its intent")
                tid = fill.get("tid")
                if isinstance(tid, bool) or not isinstance(tid, (str, int)) or not str(tid).strip():
                    raise ValueError("Actual exchange fills require a stable tid")
                symbol = fill.get("symbol", request["symbol"])
                if symbol != request["symbol"]:
                    raise ValueError("Fill symbol does not match its intent")
                quantity = _number(fill.get("quantity"))
                price, fee = _number(fill.get("price"), positive=True), _number(fill.get("fee"))
                if quantity == 0 or (quantity > 0) != request["is_buy"]:
                    raise ValueError("Actual fill quantity must have the intent's nonzero signed side")
                fill.update(account=request["account"], tid=str(tid), symbol=symbol,
                            quantity=quantity, price=price, fee=fee)
                existing = self.connection.execute("SELECT * FROM fills WHERE account=? AND tid=?",
                                                   (request["account"], str(tid))).fetchone()
                if existing:
                    previous = _fill(existing)
                    keys = ("account", "tid", "symbol", "quantity", "price", "fee", "fee_token", "oid")
                    if existing["intent_id"] != identity or any(previous.get(k) != fill.get(k) for k in keys):
                        raise ValueError("Conflicting exchange evidence for an already recorded fill")
                    continue
                self.connection.execute("INSERT INTO fills VALUES (?,?,?,?,?,?,?,?)",
                    (request["account"], str(tid), identity, symbol, quantity, price, fee, _json(fill)))
            filled = abs(self.connection.execute("SELECT COALESCE(SUM(quantity),0) FROM fills WHERE intent_id=?",
                                                 (identity,)).fetchone()[0])
            requested = _number(request["size"], positive=True)
            tolerance = max(1e-10, requested * 1e-8)
            if filled > requested + tolerance:
                raise ValueError("Reconciled fills exceed the immutable requested quantity")
            if state == "FILLED" and (filled == 0 or abs(filled - requested) > tolerance):
                raise ValueError("FILLED requires actual exchange fill evidence for the complete requested quantity")
            if state == "REJECTED" and filled:
                raise ValueError("A rejected intent cannot have recorded fills")
            previous = intent["result"]
            combined = {**previous, **result}
            self.connection.execute("UPDATE intents SET state=?,updated_at=?,result_json=? WHERE id=?",
                                    (state, _number(self.clock()), _json(combined), identity))
            self._event(identity, "reconciled", result)

    def pending(self):
        return [_intent(row) for row in self.connection.execute(
            "SELECT * FROM intents WHERE state NOT IN ('FILLED','CANCELED','REJECTED') ORDER BY id")]

    def list_intents(self, limit=500):
        return [_intent(row) for row in self.connection.execute("SELECT * FROM intents ORDER BY id DESC LIMIT ?",
                                                               (_limit(limit),))]

    def fills(self, limit=500):
        return [_fill(row) for row in self.connection.execute("SELECT * FROM fills ORDER BY rowid DESC LIMIT ?",
                                                             (_limit(limit),))]

    def fill_totals(self):
        totals = {}
        for row in self.connection.execute("SELECT account,symbol,SUM(quantity) FROM fills GROUP BY account,symbol"):
            totals.setdefault(row[0], {})[row[1]] = row[2]
        return totals

    def fill_notionals(self):
        totals = {}
        for row in self.connection.execute("SELECT account,symbol,SUM(quantity*price) FROM fills GROUP BY account,symbol"):
            totals.setdefault(row[0], {})[row[1]] = row[2]
        return totals

    def inventory_totals(self):
        """Actual owned inventory changes, including base-token spot fees.

        Trade quantity remains unchanged in fill_totals for execution evidence
        and VWAP. Clear Pond is the spot account in this release; a fee charged
        in its traded base asset reduces inventory regardless of buy/sell side.
        """
        aliases = {"UBTC": "BTC", "UETH": "ETH", "UZEC": "ZEC"}
        totals = {}
        for row in self.connection.execute("SELECT * FROM fills"):
            fill = _fill(row)
            delta = fill["quantity"]
            if fill["account"] == "clearpond":
                token = str(fill.get("fee_token") or "").upper()
                base = aliases.get(token, token)
                symbol = aliases.get(fill["symbol"], fill["symbol"])
                if base == symbol:
                    delta -= fill["fee"]
                elif token != "USDC":
                    raise ValueError("Unknown spot fee currency; inventory reconciliation is unavailable")
            account = totals.setdefault(fill["account"], {})
            account[fill["symbol"]] = account.get(fill["symbol"], 0) + delta
        return totals

    def events(self, limit=500):
        return [_event(row) for row in self.connection.execute("SELECT * FROM intent_events ORDER BY id DESC LIMIT ?",
                                                              (_limit(limit),))]

    def get_cooldown(self, account, coin):
        row = self.connection.execute("SELECT timestamp FROM cooldowns WHERE account=? AND coin=?", (account, coin)).fetchone()
        return row[0] if row else None

    def set_cooldown(self, account, coin, timestamp):
        timestamp = _number(timestamp)
        if not isinstance(account, str) or not account or not isinstance(coin, str) or not coin:
            raise ValueError("Cooldown requires account and coin")
        with self._transaction():
            self.connection.execute("""INSERT INTO cooldowns VALUES (?,?,?) ON CONFLICT(account,coin)
                DO UPDATE SET timestamp=MAX(timestamp,excluded.timestamp)""", (account, coin, timestamp))

    def close(self):
        self.connection.close()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()


def _limit(value):
    return max(1, min(int(value), 500))


def read_status(path, *, limit=500):
    """Bounded, read-only UI projection. Never constructs or activates a ledger."""
    path = Path(path).resolve()
    result = {"status": "missing", "metadata": {"binding": None, "baseline": None},
              "latest_observation": None, "intents": [], "fills": [], "events": [], "pending": [],
              "counts": {"intents": 0, "fills": 0, "pending": 0}}
    if not path.is_file():
        return result
    connection = None
    try:
        connection = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=.2, isolation_level=None)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA query_only=ON")
        deadline = time.monotonic() + 1
        connection.set_progress_handler(lambda: int(time.monotonic() > deadline), 2000)
        connection.execute("BEGIN")
        schema = connection.execute("SELECT value_json FROM metadata WHERE key='schema'").fetchone()
        if schema is None or json.loads(schema[0]) != _SCHEMA:
            raise ValueError("Unsupported Powder ledger schema")
        result["metadata"] = _metadata(connection)
        row = connection.execute("SELECT data_json FROM observations ORDER BY id DESC LIMIT 1").fetchone()
        result["latest_observation"] = json.loads(row[0]) if row else None
        bound = _limit(limit)
        result["intents"] = [_intent(row) for row in connection.execute("SELECT * FROM intents ORDER BY id DESC LIMIT ?", (bound,))]
        result["fills"] = [_fill(row) for row in connection.execute("SELECT * FROM fills ORDER BY rowid DESC LIMIT ?", (bound,))]
        result["events"] = [_event(row) for row in connection.execute("SELECT * FROM intent_events ORDER BY id DESC LIMIT ?", (bound,))]
        result["pending"] = [_intent(row) for row in connection.execute(
            "SELECT * FROM intents WHERE state NOT IN ('FILLED','CANCELED','REJECTED') ORDER BY id LIMIT ?", (bound,))]
        result["counts"] = {
            "intents": connection.execute("SELECT COUNT(*) FROM intents").fetchone()[0],
            "fills": connection.execute("SELECT COUNT(*) FROM fills").fetchone()[0],
            "pending": connection.execute("SELECT COUNT(*) FROM intents WHERE state NOT IN ('FILLED','CANCELED','REJECTED')").fetchone()[0],
        }
        result["status"] = "ready" if result["metadata"]["binding"] is not None else "not_activated"
    except (OSError, sqlite3.DatabaseError, ValueError, TypeError) as exc:
        result.update(status="error", error=f"{type(exc).__name__}: {exc}")
    finally:
        if connection is not None:
            connection.set_progress_handler(None, 0)
            connection.rollback()
            connection.close()
    return result
