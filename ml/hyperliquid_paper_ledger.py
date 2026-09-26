"""Transactional virtual-money ledger for the three Hyperliquid paper accounts.

SQLite is authoritative. Orders passed here already contain simulated execution
prices. This module performs no network requests and has no account credentials.
"""
from __future__ import annotations

from datetime import datetime, timezone
import json
import math
from pathlib import Path
import re
import sqlite3
from uuid import uuid4

import pandas as pd

from ml.hyperliquid_model_artifacts import _atomic_parquet


ACCOUNT_ROLES = {"alex": "short_perp", "jeremy": "long_perp", "clearpond": "spot"}
_EPS = 1e-9


def _number(value, name, *, positive=False, nonnegative=False):
    if isinstance(value, bool):
        raise ValueError(f"{name} must be a finite number.")
    try:
        result = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} must be a finite number.") from exc
    if not math.isfinite(result) or positive and result <= 0 or nonnegative and result < 0:
        raise ValueError(f"Invalid {name}.")
    return result


def _utc(value):
    if isinstance(value, (float, int)) and not isinstance(value, bool):
        stamp = datetime.fromtimestamp(_number(value, "now"), timezone.utc)
    else:
        stamp = pd.Timestamp(value)
        if pd.isna(stamp) or stamp.tzinfo is None:
            raise ValueError("Timestamps must have an explicit timezone.")
    return stamp.astimezone(timezone.utc).isoformat()


def _json(value):
    return json.dumps(value, sort_keys=True, allow_nan=False, separators=(",", ":"))


def _account(value):
    if value not in ACCOUNT_ROLES:
        raise ValueError(f"Unknown virtual account: {value}")
    return value


def _coin(value):
    if not isinstance(value, str) or not re.fullmatch(r"[A-Z0-9]{1,20}", value):
        raise ValueError("Use uppercase coin symbols without path separators.")
    return value


class PaperLedger:
    """One persisted pool, with independent cash and positions per virtual account."""

    def __init__(self, path, initial_cash=None, *, initial_positions=(), initial_marks=None,
                 now=None, metadata=None, max_gross_leverage=1.0, open_existing=False):
        self.path = Path(path).resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.max_gross_leverage = _number(max_gross_leverage, "max_gross_leverage", positive=True)
        explicit_seed = initial_cash is not None
        seed = dict(initial_cash if initial_cash is not None else {account: 10000.0 for account in ACCOUNT_ROLES})
        if set(seed) != set(ACCOUNT_ROLES):
            raise ValueError("Initial cash must specify exactly alex, jeremy, and clearpond.")
        seed = {account: _number(value, "initial_cash", nonnegative=True) for account, value in seed.items()}
        self.connection = sqlite3.connect(self.path, isolation_level=None, timeout=30)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA journal_mode=WAL")
        self.connection.execute("PRAGMA foreign_keys=ON")
        self.connection.executescript("""
            CREATE TABLE IF NOT EXISTS accounts (
                account TEXT PRIMARY KEY, initial_cash REAL NOT NULL, cash REAL NOT NULL,
                realized_pnl REAL NOT NULL DEFAULT 0, fees REAL NOT NULL DEFAULT 0,
                funding REAL NOT NULL DEFAULT 0, net_transfers REAL NOT NULL DEFAULT 0,
                initial_equity REAL NOT NULL, initial_unrealized_pnl REAL NOT NULL DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS positions (
                account TEXT NOT NULL REFERENCES accounts(account), coin TEXT NOT NULL,
                kind TEXT NOT NULL, quantity REAL NOT NULL, avg_entry REAL NOT NULL,
                PRIMARY KEY (account,coin,kind)
            );
            CREATE TABLE IF NOT EXISTS cycles (
                cycle_id TEXT PRIMARY KEY, timestamp_utc TEXT NOT NULL, result_json TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS fills (
                fill_id TEXT PRIMARY KEY, cycle_id TEXT NOT NULL, timestamp_utc TEXT NOT NULL,
                account TEXT NOT NULL, coin TEXT NOT NULL, kind TEXT NOT NULL,
                quantity REAL NOT NULL, price REAL NOT NULL, notional REAL NOT NULL,
                fee REAL NOT NULL, realized_pnl REAL NOT NULL, forecast_id TEXT,
                model_id TEXT, reason TEXT, details_json TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS transfers (
                transfer_id TEXT PRIMARY KEY, cycle_id TEXT NOT NULL, timestamp_utc TEXT NOT NULL,
                from_account TEXT NOT NULL, to_account TEXT NOT NULL, amount REAL NOT NULL,
                reason TEXT, details_json TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS funding (
                funding_id TEXT PRIMARY KEY, cycle_id TEXT NOT NULL, timestamp_utc TEXT NOT NULL,
                account TEXT NOT NULL, coin TEXT NOT NULL, amount REAL NOT NULL,
                rate REAL, details_json TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS decisions (
                decision_id TEXT PRIMARY KEY, cycle_id TEXT NOT NULL, timestamp_utc TEXT NOT NULL,
                account TEXT, coin TEXT, action TEXT, reason TEXT, forecast_id TEXT,
                model_id TEXT, details_json TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS equity (
                cycle_id TEXT NOT NULL, timestamp_utc TEXT NOT NULL, account TEXT NOT NULL,
                cash REAL NOT NULL, equity REAL NOT NULL, free_cash REAL NOT NULL,
                realized_pnl REAL NOT NULL, unrealized_pnl REAL NOT NULL, fees REAL NOT NULL,
                funding REAL NOT NULL, total_pnl REAL NOT NULL, gross_exposure REAL NOT NULL,
                PRIMARY KEY (cycle_id,account)
            );
            CREATE TABLE IF NOT EXISTS events (
                event_id TEXT PRIMARY KEY, cycle_id TEXT NOT NULL, timestamp_utc TEXT NOT NULL,
                event_type TEXT NOT NULL, account TEXT, coin TEXT, details_json TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS initial_positions (
                account TEXT NOT NULL, coin TEXT NOT NULL, kind TEXT NOT NULL,
                quantity REAL NOT NULL, avg_entry REAL NOT NULL, mark_price REAL NOT NULL,
                timestamp_utc TEXT NOT NULL, details_json TEXT NOT NULL,
                PRIMARY KEY(account,coin,kind)
            );
            CREATE TABLE IF NOT EXISTS seed (
                singleton INTEGER PRIMARY KEY CHECK(singleton=1), details_json TEXT NOT NULL
            );
        """)
        self.connection.execute("BEGIN IMMEDIATE")
        try:
            existing = self.connection.execute("SELECT account, initial_cash FROM accounts").fetchall()
            seed_exists = self.connection.execute("SELECT 1 FROM seed WHERE singleton=1").fetchone()
            if open_existing and (not existing or not seed_exists):
                raise ValueError("Existing paper database has no completed opening seed; refusing to create default balances.")
            if existing:
                if explicit_seed and any(not math.isclose(seed[row["account"]], row["initial_cash"], abs_tol=_EPS) for row in existing):
                    raise ValueError("Existing paper balances use different initial cash; use a new ledger for a new experiment.")
            else:
                self.connection.executemany(
                    "INSERT INTO accounts(account,initial_cash,cash,initial_equity) VALUES (?,?,?,?)",
                    [(account, amount, amount, amount) for account, amount in seed.items()],
                )
                seed_time = _utc(datetime.now(timezone.utc) if now is None else now)
                seed_marks = self._marks(initial_marks or {})
                seeded_positions = []
                for item in initial_positions:
                    account, coin, kind = _account(item["account"]), _coin(item["coin"]), item["kind"]
                    quantity = _number(item["quantity"], "initial quantity")
                    entry = _number(item.get("average_entry", item.get("avg_entry")), "initial entry", positive=True)
                    if kind not in ("spot", "perp") or abs(quantity) <= _EPS:
                        raise ValueError("Initial positions require a supported kind and nonzero quantity.")
                    if (kind == "spot" and quantity < 0 or kind == "perp" and
                            (account == "clearpond" or account == "alex" and quantity > 0 or account == "jeremy" and quantity < 0)):
                        raise ValueError("Initial position violates the account direction.")
                    mark = seed_marks[f"{kind}:{coin}"]
                    self.connection.execute("INSERT INTO positions VALUES (?,?,?,?,?)", (account, coin, kind, quantity, entry))
                    position = {**item, "quantity": quantity, "avg_entry": entry, "mark_price": mark,
                                "timestamp_utc": seed_time, "passive": kind == "spot" and account != "clearpond"}
                    self.connection.execute("INSERT INTO initial_positions VALUES (?,?,?,?,?,?,?,?)",
                                            (account, coin, kind, quantity, entry, mark, seed_time, _json(position)))
                    seeded_positions.append(position)
                seeded_state = self.state(seed_marks)
                for account, values in seeded_state["accounts"].items():
                    self.connection.execute("UPDATE accounts SET initial_equity=?,initial_unrealized_pnl=? WHERE account=?",
                                            (values["equity"], values["unrealized_pnl"], account))
                self.connection.execute("INSERT INTO seed VALUES (1,?)", (_json({
                    "timestamp_utc": seed_time, "cash": seed, "positions": seeded_positions,
                    "marks": seed_marks, "metadata": metadata or {},
                    "baseline_equity": {account: values["equity"] for account, values in seeded_state["accounts"].items()},
                }),))
            self.connection.commit()
        except Exception:
            self.connection.rollback()
            self.connection.close()
            raise

    def close(self):
        self.connection.close()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()

    @staticmethod
    def _marks(marks):
        result = {}
        for key, price in marks.items():
            if not isinstance(key, str) or ":" not in key:
                raise ValueError("Marks must use keys such as perp:BTC or spot:BTC.")
            kind, coin = key.split(":", 1)
            if kind not in ("perp", "spot"):
                raise ValueError("Unsupported mark kind.")
            _coin(coin)
            result[key] = _number(price, f"mark {key}", positive=True)
        return result

    def state(self, marks):
        marks = self._marks(marks)
        accounts = {}
        for row in self.connection.execute("SELECT * FROM accounts ORDER BY account"):
            account = dict(row)
            account["role"] = ACCOUNT_ROLES[account["account"]]
            account["positions"] = []
            account["unrealized_pnl"] = 0.0
            account["gross_exposure"] = 0.0
            account["equity"] = account["cash"]
            accounts[account["account"]] = account
        for row in self.connection.execute("SELECT * FROM positions ORDER BY account,kind,coin"):
            position = dict(row)
            market = f"{position['kind']}:{position['coin']}"
            if market not in marks:
                raise ValueError(f"Missing mark for held position: {market}")
            mark = marks[market]
            quantity = position["quantity"]
            unrealized = quantity * (mark - position["avg_entry"])
            gross = abs(quantity) * mark
            position.update(market=market, mark_price=mark, unrealized_pnl=unrealized,
                            passive=position["kind"] == "spot" and position["account"] != "clearpond",
                            notional=gross, side="long" if quantity > 0 else "short")
            account = accounts[position["account"]]
            account["positions"].append(position)
            account["unrealized_pnl"] += unrealized
            account["gross_exposure"] += gross
            account["equity"] += quantity * mark if position["kind"] == "spot" else unrealized
        numeric = ("initial_cash", "initial_equity", "initial_unrealized_pnl", "cash", "equity", "free_cash", "realized_pnl", "unrealized_pnl",
                   "fees", "funding", "net_transfers", "total_pnl", "gross_exposure", "net_realized_pnl")
        for account in accounts.values():
            margin = account["gross_exposure"] / self.max_gross_leverage if account["role"] != "spot" else 0.0
            available = account["equity"] - margin if account["role"] != "spot" else account["cash"]
            account["free_cash"] = max(0.0, min(account["cash"], available))
            account["total_pnl"] = account["equity"] - account["initial_equity"] - account["net_transfers"]
            account["net_realized_pnl"] = account["realized_pnl"] + account["funding"] - account["fees"]
        pooled = {key: sum(account[key] for account in accounts.values()) for key in numeric}
        pooled["positions"] = [position for account in accounts.values() for position in account["positions"]]
        return {"accounts": accounts, "pooled": pooled}

    def history(self, table):
        """Read a known public journal table in insertion order."""
        if table not in {"fills", "transfers", "funding", "equity", "decisions", "events", "initial_positions", "cycles"}:
            raise ValueError("Unsupported paper history table.")
        return [dict(row) for row in self.connection.execute(f"SELECT * FROM {table} ORDER BY rowid")]

    def inventory(self):
        """Return currently held quantities, including markets removed from config."""
        return [dict(row) for row in self.connection.execute(
            "SELECT account,coin,kind,quantity,avg_entry FROM positions ORDER BY account,kind,coin"
        )]

    def has_cycle(self, cycle_id):
        """Check the primary key without loading historical cycle JSON payloads."""
        if not isinstance(cycle_id, str) or not cycle_id:
            raise ValueError("A cycle requires a stable nonempty identity.")
        return self.connection.execute("SELECT 1 FROM cycles WHERE cycle_id=?", (cycle_id,)).fetchone() is not None

    def seed(self):
        """Return the immutable initial inventory and performance baseline."""
        row = self.connection.execute("SELECT details_json FROM seed WHERE singleton=1").fetchone()
        return json.loads(row["details_json"])

    def _event(self, cycle_id, now, event_type, payload, account=None, coin=None):
        self.connection.execute("INSERT INTO events VALUES (?,?,?,?,?,?,?)",
                                (uuid4().hex, cycle_id, now, event_type, account, coin, _json(payload)))

    def _fill(self, cycle_id, now, order):
        account, coin = _account(order["account"]), _coin(order["coin"])
        kind = order["kind"]
        if kind != ("spot" if account == "clearpond" else "perp"):
            raise ValueError("Order kind violates the virtual account's role.")
        quantity = _number(order["quantity"], "quantity")
        if abs(quantity) <= _EPS:
            raise ValueError("A fill quantity must be nonzero.")
        price = _number(order["price"], "price", positive=True)
        fee_rate = _number(order.get("fee_rate", 0.0), "fee_rate", nonnegative=True)
        if fee_rate >= 1:
            raise ValueError("Fee rate must be less than one.")
        previous = self.connection.execute("SELECT * FROM positions WHERE account=? AND coin=? AND kind=?",
                                           (account, coin, kind)).fetchone()
        old_quantity = previous["quantity"] if previous else 0.0
        old_entry = previous["avg_entry"] if previous else 0.0
        new_quantity = old_quantity + quantity
        if abs(new_quantity) <= _EPS:
            new_quantity = 0.0
        if account == "alex" and new_quantity > 0 or account != "alex" and new_quantity < 0:
            raise ValueError("A fill would flip into a position forbidden for this account.")
        realized = 0.0
        if old_quantity == 0 or old_quantity * quantity > 0:
            avg_entry = (abs(old_quantity) * old_entry + abs(quantity) * price) / abs(new_quantity)
        else:
            realized = min(abs(old_quantity), abs(quantity)) * (price - old_entry) * (1 if old_quantity > 0 else -1)
            avg_entry = old_entry
        fee = abs(quantity) * price * fee_rate
        cash_delta = realized - fee if kind == "perp" else -quantity * price - fee
        self.connection.execute("UPDATE accounts SET cash=cash+?, realized_pnl=realized_pnl+?, fees=fees+? WHERE account=?",
                                (cash_delta, realized, fee, account))
        if new_quantity == 0:
            self.connection.execute("DELETE FROM positions WHERE account=? AND coin=? AND kind=?", (account, coin, kind))
        else:
            self.connection.execute("INSERT INTO positions VALUES (?,?,?,?,?) ON CONFLICT(account,coin,kind) "
                                    "DO UPDATE SET quantity=excluded.quantity,avg_entry=excluded.avg_entry",
                                    (account, coin, kind, new_quantity, avg_entry))
        result = {**order, "fill_id": uuid4().hex, "cycle_id": cycle_id, "timestamp_utc": now,
                  "quantity": quantity, "price": price, "notional": abs(quantity) * price,
                  "fee": fee, "realized_pnl": realized, "quantity_after": new_quantity,
                  "avg_entry_after": avg_entry if new_quantity else None}
        self.connection.execute("INSERT INTO fills VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (
            result["fill_id"], cycle_id, now, account, coin, kind, quantity, price,
            result["notional"], fee, realized, order.get("forecast_id"), order.get("model_id"),
            order.get("reason"), _json(result),
        ))
        self._event(cycle_id, now, "fill", result, account, coin)
        return result

    def _transfer(self, cycle_id, now, transfer, marks):
        donor = _account(transfer["from_account"])
        receiver = _account(transfer["to_account"])
        amount = _number(transfer["amount"], "transfer amount", positive=True)
        if donor == receiver:
            raise ValueError("A transfer must move cash between different virtual accounts.")
        if transfer.get("fee", 0) != 0:
            raise ValueError("Virtual cash transfers have no fee.")
        if amount > self.state(marks)["accounts"][donor]["free_cash"] + _EPS:
            raise ValueError("The transfer exceeds the donor's free cash or collateral.")
        self.connection.execute("UPDATE accounts SET cash=cash-?,net_transfers=net_transfers-? WHERE account=?", (amount, amount, donor))
        self.connection.execute("UPDATE accounts SET cash=cash+?,net_transfers=net_transfers+? WHERE account=?", (amount, amount, receiver))
        result = {**transfer, "transfer_id": uuid4().hex, "cycle_id": cycle_id, "timestamp_utc": now, "amount": amount}
        self.connection.execute("INSERT INTO transfers VALUES (?,?,?,?,?,?,?,?)", (
            result["transfer_id"], cycle_id, now, donor, receiver, amount, transfer.get("reason"), _json(result),
        ))
        self._event(cycle_id, now, "transfer", result, donor)
        return result

    def _funding(self, cycle_id, now, funding, marks):
        account, coin = _account(funding["account"]), _coin(funding["coin"])
        if account == "clearpond":
            raise ValueError("Spot holdings do not receive perpetual funding.")
        hour = pd.Timestamp(now).floor("h").isoformat()
        funding_id = funding.get("funding_id", f"{hour}:{account}:{coin}")
        if not isinstance(funding_id, str) or not funding_id:
            raise ValueError("Funding requires a nonempty stable identity.")
        existing = self.connection.execute("SELECT * FROM funding WHERE funding_id=?", (funding_id,)).fetchone()
        if existing:
            if (existing["account"], existing["coin"]) != (account, coin):
                raise ValueError("Funding identity was already used for another account or coin.")
            return {**json.loads(existing["details_json"]), "duplicate": True}
        position = self.connection.execute("SELECT quantity FROM positions WHERE account=? AND coin=? AND kind='perp'",
                                           (account, coin)).fetchone()
        rate = _number(funding["rate"], "funding rate") if funding.get("rate") is not None else None
        if "amount" in funding:
            amount = _number(funding["amount"], "funding amount")
        elif rate is not None:
            if not position:
                raise ValueError("Rate-derived funding requires a held perpetual position.")
            amount = -position["quantity"] * marks[f"perp:{coin}"] * rate
        else:
            raise ValueError("Funding requires a known signed amount or rate.")
        result = {**funding, "funding_id": funding_id, "cycle_id": cycle_id,
                  "timestamp_utc": now, "amount": amount, "rate": rate, "duplicate": False}
        self.connection.execute("UPDATE accounts SET cash=cash+?,funding=funding+? WHERE account=?", (amount, amount, account))
        self.connection.execute("INSERT INTO funding VALUES (?,?,?,?,?,?,?,?)", (
            funding_id, cycle_id, now, account, coin, amount, rate, _json(result),
        ))
        self._event(cycle_id, now, "funding", result, account, coin)
        return result

    def execute_cycle(self, cycle_id, now, marks, orders, transfers=(), funding=(), decisions=()):
        """Commit one complete paper decision, or roll every part back on failure."""
        if not isinstance(cycle_id, str) or not cycle_id:
            raise ValueError("A cycle requires a stable nonempty identity.")
        now = _utc(now)
        marks = self._marks(marks)
        self.connection.execute("BEGIN IMMEDIATE")
        try:
            existing = self.connection.execute("SELECT result_json FROM cycles WHERE cycle_id=?", (cycle_id,)).fetchone()
            if existing:
                self.connection.commit()
                return {**json.loads(existing["result_json"]), "duplicate": True}
            transfers_rows = [self._transfer(cycle_id, now, item, marks) for item in transfers]
            funding_rows = [self._funding(cycle_id, now, item, marks) for item in funding]
            before_orders = self.state(marks)
            old_positions = {(p["account"], p["coin"], p["kind"]): p["quantity"] for p in before_orders["pooled"]["positions"]}
            fills = [self._fill(cycle_id, now, item) for item in orders]
            for item in decisions:
                if item.get("account") is not None:
                    _account(item["account"])
                if item.get("coin") is not None:
                    _coin(item["coin"])
                self.connection.execute("INSERT INTO decisions VALUES (?,?,?,?,?,?,?,?,?,?)", (
                    uuid4().hex, cycle_id, now, item.get("account"), item.get("coin"),
                    item.get("action"), item.get("reason"), item.get("forecast_id"), item.get("model_id"), _json(item),
                ))
                self._event(cycle_id, now, "decision", item, item.get("account"), item.get("coin"))
            state = self.state(marks)
            for account in state["accounts"].values():
                account_fills = [fill for fill in fills if fill["account"] == account["account"]]
                reductions_only = bool(account_fills)
                for fill in account_fills:
                    key = (fill["account"], fill["coin"], fill["kind"])
                    previous_quantity = old_positions.get(key, 0)
                    delta = fill["quantity"]
                    reductions_only &= previous_quantity * delta < 0 and abs(delta) <= abs(previous_quantity) + _EPS
                    old_positions[key] = previous_quantity + delta
                if account["cash"] < -_EPS and account_fills and not (reductions_only and account["role"] != "spot"):
                    raise ValueError("A paper account cannot borrow cash.")
                if (account_fills and account["role"] != "spot" and not reductions_only
                        and account["gross_exposure"] > account["equity"] * self.max_gross_leverage + _EPS):
                    raise ValueError("A perpetual account exceeds its gross exposure/collateral limit.")
            for account, values in [*state["accounts"].items(), ("pooled", state["pooled"])]:
                self.connection.execute("INSERT INTO equity VALUES (?,?,?,?,?,?,?,?,?,?,?,?)", (
                    cycle_id, now, account, values["cash"], values["equity"], values["free_cash"],
                    values["realized_pnl"], values["unrealized_pnl"], values["fees"], values["funding"],
                    values["total_pnl"], values["gross_exposure"],
                ))
            result = {"cycle_id": cycle_id, "timestamp_utc": now, "duplicate": False,
                      "fills": fills, "transfers": transfers_rows, "funding": funding_rows, "state": state}
            self._event(cycle_id, now, "cycle", {"fill_count": len(fills), "transfer_count": len(transfers_rows),
                                              "pooled_equity": state["pooled"]["equity"]})
            self.connection.execute("INSERT INTO cycles VALUES (?,?,?)", (cycle_id, now, _json(result)))
            self.connection.commit()
            return result
        except Exception:
            self.connection.rollback()
            raise

    def export(self, directory):
        """Write inspectable projections; an export failure cannot undo the ledger."""
        directory = Path(directory).resolve()
        directory.mkdir(parents=True, exist_ok=True)
        paths = {}
        self.connection.execute("BEGIN")
        try:
            frames = {table: pd.read_sql_query(f"SELECT * FROM {table} ORDER BY rowid", self.connection)
                      for table in ("events", "fills", "transfers", "funding", "equity", "decisions", "initial_positions")}
            self.connection.commit()
        except Exception:
            self.connection.rollback()
            raise
        for name, frame in frames.items():
            path = directory / f"{name}.parquet"
            _atomic_parquet(path, frame)
            paths[name] = str(path)
        return paths
