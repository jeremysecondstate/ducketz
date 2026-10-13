"""Host-local 15m execution bindings and accounting, independent of Gameplan.

Each PC has its own namespace. Broker account capacity is observed at manual
start and before orders; another trader's process, plan or ledger is never read.
Legacy mixed-ledger evidence stays at its original paths and is not replayed.
"""
from dataclasses import dataclass
from decimal import Decimal, ROUND_CEILING
import json
from pathlib import Path
import re

from .prediction import ContractError, digest
from ml.account_gameplan.authority import BrokerReservation

VERSION = "independent-local-15m-binding-v1"
CONFIG_PATH = Path("intraday/execution/config.json")
NATIVE_PATH = Path("intraday/execution/holdings.sqlite3")
AUTHORITY_PATH = Path("intraday/execution/authority.sqlite3")
LOCK_PATH = Path("locks/intraday-execution.lock")


@dataclass(frozen=True)
class LocalExecutionBinding:
    actor: str
    machine_id: str
    symbols: tuple[str, ...]
    account_fingerprint: str
    fingerprint: str

    @property
    def stream(self):
        return self.actor.lower() + "-15m"

    @property
    def coordinator_id(self):
        return self.machine_id

    @property
    def participants(self):
        return {self.machine_id: self.symbols}


def validate_binding(value):
    required = {"schema_version", "actor", "machine", "symbols", "account_fingerprint",
                "manual_start_only", "legacy_history_policy"}
    if (not isinstance(value, dict) or set(value) != required or value["schema_version"] != VERSION
            or value["actor"] not in {"Atlas", "Scout"}
            or value["machine"] != {"Atlas": "pc-original", "Scout": "pc-new"}[value["actor"]]
            or value["manual_start_only"] is not True
            or value["legacy_history_policy"] != "PRESERVE_ORIGINALS_NEW_BROKER_CAPACITY_EPOCH"
            or not re.fullmatch(r"[a-f0-9]{64}", str(value["account_fingerprint"]))):
        raise ContractError("Explicit independent local 15m binding required")
    symbols = value["symbols"]
    if (not isinstance(symbols, list) or not symbols or len(set(symbols)) != len(symbols)
            or any(not isinstance(s, str) or not re.fullmatch(r"[A-Z][A-Z0-9.\-]{0,14}", s) for s in symbols)):
        raise ContractError("Exact local 15m symbols required")
    return LocalExecutionBinding(value["actor"], value["machine"], tuple(symbols),
                                 value["account_fingerprint"], digest(value))


def load_local_binding(datastore):
    path = Path(datastore) / CONFIG_PATH
    if path.stat().st_size > 65536:
        raise ContractError("Oversized local 15m binding")
    return validate_binding(json.loads(path.read_text(encoding="utf-8")))


from ml.stock_trader.horizon_ledger import OwnOrderLedger as IntradayLedger


def whole(value):
    if isinstance(value, bool):
        raise ContractError("EXACT_WHOLE_BROKER_QUANTITY_REQUIRED")
    number = Decimal(str(value))
    if not number.is_finite() or number < 0 or number != number.to_integral_value():
        raise ContractError("EXACT_WHOLE_BROKER_QUANTITY_REQUIRED")
    return int(number)


def broker_pending(portfolio):
    rows = portfolio.broker_working_orders
    if rows is None:
        raise ContractError("EXACT_ACCOUNT_WIDE_ORDER_IDENTITIES_REQUIRED")
    pending = []
    for row in rows:
        if (row.get("status")!="CURRENT" or row.get("asset_type") not in {"EQUITY","ETF","STOCK"}
                or row.get("instruction") not in {"BUY","SELL"}):
            raise ContractError("UNSUPPORTED_OR_INCOMPLETE_ACCOUNT_WORKING_ORDER")
        quantity = whole(row["remaining_quantity"])
        if quantity<=0:
            raise ContractError("WORKING_ORDER_REMAINING_QUANTITY_REQUIRED")
        price = Decimal(str(row["limit_price"]))
        if not price.is_finite() or price<=0:
            raise ContractError("WORKING_ORDER_LIMIT_PRICE_REQUIRED")
        side, symbol = row["instruction"],row["symbol"]
        quote = portfolio.quotes.get(symbol)
        valuation = (max(price,Decimal(str(quote.ask))) if quote is not None else price).quantize(Decimal(".01"), rounding=ROUND_CEILING)
        pending.append(BrokerReservation(str(row["order_id"]),symbol,side,quantity,
            whole(row["filled_quantity"]),str(row["reserved_cash"]),
            str(quantity*valuation if side=="BUY" else Decimal(0))))
    return tuple(pending)

