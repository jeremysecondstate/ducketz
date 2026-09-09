"""Informational, identifier-free Gameplan cash, quote and inventory evidence.

This module never submits orders, starts a trader, reconciles inventory, or
publishes controls. Broker authentication can renew its existing token cache.
"""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation, ROUND_FLOOR
from pathlib import Path
import json
import re
import sqlite3
import time

from app.services.schwab import SchwabSession
from app.services.schwab_policy_inputs import normalize_schwab_policy_inputs
from ml.stock_trader.contracts import STOCK_TRADER_SYMBOLS, finite, utc
from ml.stock_trader.state import capture_portfolio_state
from ml.stock_trader.runtime import (
    DEFAULT_BROKER_STATE_RETRY_DELAY_SECONDS,
    DEFAULT_BROKER_STATE_RETRY_MAX_ATTEMPTS,
    DEFAULT_BROKER_STATE_RETRY_MAX_SECONDS,
    _BrokerStateCaptureFailure,
    _capture_portfolio_state_with_retry,
)


_LEDGER_PATH = Path("state/independent-stock-trader/holdings.sqlite3")
_OPEN = {"RESERVED", "SUBMITTED", "UNKNOWN", "WORKING", "PARTIAL"}
_STATUSES = _OPEN | {"FILLED", "CANCELLED", "REJECTED"}
_HORIZONS = {"1h", "4h", "1d", "1w"}
_BALANCES = {
    "cash_balance": "cashBalance",
    "settled_cash": "settledCash",
    "cash_available_for_trading": "cashAvailableForTrading",
}


class _CachedReads:
    def __init__(self, account, orders, quotes):
        self.account, self.orders, self.quotes = account, orders, quotes

    def get_account(self):
        return self.account

    def get_open_orders(self):
        return self.orders

    def get_equity_quotes(self, _symbols):
        return self.quotes


def _timestamp(value):
    if value is None or isinstance(value, bool):
        return None
    try:
        if isinstance(value, (int, float)):
            # Schwab quote/trade times are Unix milliseconds, not capture times.
            return datetime.fromtimestamp(value / 1000, timezone.utc).isoformat()
        return utc(value).isoformat()
    except (ValueError, TypeError, OverflowError, OSError):
        return None


def _quote(raw, observed):
    row = raw if isinstance(raw, Mapping) else {}
    def number(*keys):
        return next((n for key in keys if not isinstance(row.get(key), bool)
                     and (n := finite(row.get(key))) is not None and n > 0), None)

    bid, ask = number("bidPrice", "bid"), number("askPrice", "ask")
    last, mark = number("lastPrice", "last"), number("mark", "markPrice")
    quote_time = _timestamp(row.get("quoteTime", row.get("quoteTimeInLong")))
    trade_time = _timestamp(row.get("tradeTime", row.get("tradeTimeInLong")))
    midpoint = (bid + ask) / 2 if bid is not None and ask is not None and ask >= bid else None
    reference, source, stamp = None, None, None
    if last is not None and trade_time is not None and utc(trade_time) <= utc(observed):
        reference, source, stamp = last, "SCHWAB_LAST_TRADE", trade_time
    elif midpoint is not None and quote_time is not None and utc(quote_time) <= utc(observed):
        reference, source, stamp = midpoint, "SCHWAB_BID_ASK_MIDPOINT", quote_time
    return {
        "bid": bid, "ask": ask, "last": last, "mark": mark,
        "quote_time": quote_time, "trade_time": trade_time,
        "price_reference": reference, "price_reference_source": source,
        "price_reference_time": stamp, "observed_at": observed,
        "status": "REFERENCE_ONLY" if reference is not None else "PRICE_TIME_UNAVAILABLE",
        "live_executable": False,
    }


def _ownership_unavailable(code):
    return {"status": "UNAVAILABLE", "safe_for_planning": False,
            "active_allocations": [], "blocked_symbols": [], "reason_codes": [code],
            "account_matches": None, "current_broker_reconciliation_performed": False}


def _position_economics_failure(raw, row):
    """Require genuine portfolio dollars, without pricing option contracts.

    A directly reported option marketValue already states its entire position
    exposure in dollars. Missing an option unit price/multiplier must not cause
    a fabricated contract price, or hide otherwise observed dollar exposure.
    """
    if not isinstance(raw, Mapping) or not isinstance(row, Mapping):
        return "POSITION_ROW_UNSTRUCTURED"
    asset = row.get("asset_type")
    if asset not in {"EQUITY", "STOCK", "COLLECTIVE_INVESTMENT", "OPTION"}:
        return "POSITION_ASSET_TYPE_UNKNOWN"
    quantity, market_value = finite(row.get("net_quantity")), finite(row.get("market_value"))
    if not row.get("symbol"):
        return "POSITION_SYMBOL_UNAVAILABLE"
    if quantity is None:
        return "POSITION_NET_QUANTITY_UNAVAILABLE"
    if market_value is None:
        return "POSITION_MARKET_VALUE_UNAVAILABLE"
    if quantity * market_value < 0 or (quantity == 0 and market_value != 0):
        return "POSITION_QUANTITY_VALUE_SIGN_CONFLICT"
    candidates = row.get("stock_policy_identity_candidates", {})
    if any(len(candidates.get(key, [])) != 1 for key in ("symbols", "asset_types")):
        return "POSITION_SYMBOL_OR_ASSET_AMBIGUOUS"
    if asset == "OPTION":
        if not row.get("underlying_symbol") or row.get("option_fields_complete") is not True:
            return "OPTION_IDENTITY_UNAVAILABLE_OR_AMBIGUOUS"
        instrument = raw.get("instrument", {})
        for source in (raw, instrument):
            for key in ("multiplier", "contractMultiplier"):
                if key in source and (finite(source[key]) is None or finite(source[key]) <= 0):
                    return "OPTION_EXPLICIT_MULTIPLIER_INVALID"
    if row.get("stock_policy_fields_complete") is True:
        return None
    if asset not in {"OPTION", "COLLECTIVE_INVESTMENT"}:
        return "POSITION_QUANTITY_OR_VALUATION_UNAVAILABLE_OR_CONFLICTING"
    if finite(raw.get("marketValue")) is None:
        return "DIRECT_OPTION_OR_ETF_MARKET_VALUE_REQUIRED"
    source = str(row.get("source_ref"))
    allowed = ({
        f"{source} did not provide enough data to normalize price.",
        f"{source} cannot reconcile option price and market value without an explicit positive contract multiplier.",
    } if asset == "OPTION" else {
        f"{source} reports unreviewed asset type COLLECTIVE_INVESTMENT; the stock-policy "
        "normalizer cannot classify it as stock or a defined option position."
    })
    reasons = row.get("stock_policy_unavailable_reasons")
    if isinstance(reasons, list) and bool(reasons) and all(reason in allowed for reason in reasons):
        return None
    return "POSITION_QUANTITY_OR_VALUATION_UNAVAILABLE_OR_CONFLICTING"


def _position_economics_complete(raw, row):
    return _position_economics_failure(raw, row) is None


def _whole(value):
    number = Decimal(str(value))
    if not number.is_finite() or number < 0 or number != number.to_integral_value():
        raise ValueError("INVALID_LEDGER_QUANTITY")
    return int(number)


def _allocation_timestamp(value):
    # Ledger expiry is a recorded instant, never a date, naive local clock or
    # missing value that utc(None) could replace with the current time.
    if not isinstance(value, str):
        return None
    try:
        stamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if stamp.tzinfo is None or stamp.utcoffset() is None:
            return None
        return utc(stamp).isoformat()
    except (ValueError, TypeError, OverflowError):
        return None


def _ownership(root, symbols, identity, held, observed):
    path = Path(root).resolve() / _LEDGER_PATH
    if not path.is_file():
        return _ownership_unavailable("OWNERSHIP_DATABASE_MISSING")
    if not isinstance(identity, str) or re.fullmatch(r"[a-f0-9]{64}", identity) is None:
        return _ownership_unavailable("BROKER_ACCOUNT_FINGERPRINT_UNAVAILABLE")
    # Never use immutable=1: a live writer's WAL must remain visible. Avoid
    # opening a WAL database that would need its shared-memory sidecar created.
    if Path(str(path) + "-wal").exists() and not Path(str(path) + "-shm").exists():
        return _ownership_unavailable("OWNERSHIP_WAL_READ_STATE_UNAVAILABLE")
    try:
        connection = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=2)
        try:
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA query_only=ON")
            connection.execute("BEGIN")
            metadata = dict(connection.execute("SELECT key,value FROM metadata"))
            if metadata.get("version") != "independent-stock-horizon-ledger-v1":
                return _ownership_unavailable("OWNERSHIP_SCHEMA_UNSUPPORTED")
            if metadata.get("account") != identity:
                result = _ownership_unavailable("OWNERSHIP_ACCOUNT_MISMATCH")
                result["account_matches"] = False
                return result
            allocations = connection.execute(
                "SELECT id,account,symbol,horizon,start,end,status FROM allocations"
            ).fetchall()
            reservations = connection.execute(
                "SELECT allocation,side,quantity,price,filled,status FROM reservations"
            ).fetchall()
            blocks = connection.execute("SELECT symbol FROM blocks").fetchall()
            latest = connection.execute(
                "SELECT rowid AS sequence,observed_at,ready,payload,owned FROM snapshots ORDER BY observed_at DESC LIMIT 1"
            ).fetchone()
            assignments, releases = [], []
            if connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='inventory_assignments'").fetchone():
                assignments = connection.execute("""SELECT i.*,s.rowid AS snapshot_sequence
                    FROM inventory_assignments i JOIN snapshots s ON s.id=i.snapshot_id""").fetchall()
                if connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='inventory_assignment_releases'").fetchone():
                    releases = connection.execute("""SELECT r.*,s.rowid AS snapshot_sequence
                        FROM inventory_assignment_releases r JOIN snapshots s ON s.id=r.snapshot_id""").fetchall()
        finally:
            connection.close()
        reasons, active, by_id = set(), [], {}
        for item in allocations:
            if (item["account"] != identity or item["symbol"] not in symbols
                    or item["horizon"] not in _HORIZONS
                    or item["status"] not in {"ACTIVE", "CLOSED"}):
                raise ValueError("INVALID_LEDGER_ALLOCATION")
            start, end = _allocation_timestamp(item["start"]), _allocation_timestamp(item["end"])
            if start is None or end is None or utc(end) <= utc(start):
                return _ownership_unavailable("OWNERSHIP_ALLOCATION_TIME_INVALID")
            by_id[item["id"]] = {"symbol": item["symbol"], "horizon": item["horizon"],
                "owned_shares": 0, "reserved_buy_shares": 0, "reserved_sell_shares": 0,
                "target_start": start, "target_end": end,
                "status": item["status"]}
        assignment_map = {item["id"]: item for item in assignments}
        net_transfers = {symbol: 0 for symbol in symbols}
        released_by_id = {}
        for item in assignments:
            allocation = by_id.get(item["allocation"])
            if allocation is None:
                raise ValueError("INVALID_EXISTING_STOCK_ASSIGNMENT")
            quantity = _whole(item["quantity"])
            allocation["owned_shares"] += quantity
            if latest and item["snapshot_sequence"] >= latest["sequence"]:
                net_transfers[allocation["symbol"]] += quantity
        for item in releases:
            assignment = assignment_map.get(item["assignment"])
            if assignment is None:
                raise ValueError("INVALID_EXISTING_STOCK_RELEASE")
            quantity = _whole(item["quantity"])
            released_by_id[item["assignment"]] = released_by_id.get(item["assignment"], 0) + quantity
            if released_by_id[item["assignment"]] > _whole(assignment["quantity"]):
                raise ValueError("EXISTING_STOCK_RELEASE_EXCEEDS_ASSIGNMENT")
            allocation = by_id[assignment["allocation"]]
            allocation["owned_shares"] -= quantity
            if latest and item["snapshot_sequence"] >= latest["sequence"]:
                net_transfers[allocation["symbol"]] -= quantity
        for item in reservations:
            allocation = by_id.get(item["allocation"])
            if allocation is None or item["side"] not in {"BUY", "SELL"} or item["status"] not in _STATUSES:
                raise ValueError("INVALID_LEDGER_RESERVATION")
            quantity, filled = _whole(item["quantity"]), _whole(item["filled"])
            if quantity <= 0 or filled > quantity:
                raise ValueError("INVALID_LEDGER_FILL")
            allocation["owned_shares"] += filled * (1 if item["side"] == "BUY" else -1)
            if item["status"] in _OPEN:
                key = "reserved_buy_shares" if item["side"] == "BUY" else "reserved_sell_shares"
                allocation[key] += quantity - filled
                reasons.add("PENDING_LEDGER_RESERVATIONS_REQUIRE_RECONCILIATION")
        owned = {symbol: 0 for symbol in symbols}
        active_keys = set()
        for item in by_id.values():
            if item["owned_shares"] < 0 or item["reserved_sell_shares"] > item["owned_shares"]:
                reasons.add("OWNERSHIP_QUANTITY_INCONSISTENT")
            owned[item["symbol"]] += item["owned_shares"]
            if item["status"] == "ACTIVE":
                key = (item["symbol"], item["horizon"])
                if key in active_keys:
                    reasons.add("DUPLICATE_ACTIVE_HORIZON")
                active_keys.add(key)
                active.append(item)
            elif item["owned_shares"] or item["reserved_buy_shares"] or item["reserved_sell_shares"]:
                reasons.add("CLOSED_ALLOCATION_HAS_INVENTORY")
        if any(finite(held.get(symbol)) is None or float(held[symbol]) + 1e-8 < value
               for symbol, value in owned.items()):
            reasons.add("BROKER_SHARES_BELOW_HORIZON_INVENTORY")
        blocked = sorted({row["symbol"] for row in blocks if row["symbol"] in symbols})
        if blocks:
            reasons.add("PERSISTENT_OWNERSHIP_BLOCKS")
        if latest is None:
            reasons.add("SAVED_RECONCILIATION_MISSING")
        else:
            if latest["ready"] != 1:
                reasons.add("LAST_SAVED_RECONCILIATION_NOT_READY")
            saved_at = _timestamp(latest["observed_at"])
            if saved_at is None or utc(saved_at) > utc(observed):
                reasons.add("SAVED_RECONCILIATION_TIME_INVALID")
            previous = json.loads(latest["payload"])["held_shares"]
            previous_owned = json.loads(latest["owned"])
            if not isinstance(previous, dict) or not isinstance(previous_owned, dict):
                raise ValueError("INVALID_SAVED_OWNERSHIP")
            for symbol in symbols:
                if symbol not in previous:
                    reasons.add("SAVED_RECONCILIATION_SYMBOL_MISSING")
                    continue
                before, before_owned = finite(previous[symbol]), finite(previous_owned.get(symbol, 0))
                if before is None or before_owned is None or before < 0 or before_owned < 0:
                    raise ValueError("INVALID_SAVED_OWNERSHIP_QUANTITY")
                actual_change = float(held[symbol]) - before
                tracked_change = owned[symbol] - before_owned - net_transfers[symbol]
                if actual_change - tracked_change < -1e-8:
                    reasons.add("UNEXPLAINED_SHARE_REDUCTION_SINCE_SAVED_RECONCILIATION")
        return {"status": "OBSERVED_CONSISTENT" if not reasons else "REVIEW_REQUIRED",
            "safe_for_planning": not reasons, "account_matches": True,
            "active_allocations": sorted(active, key=lambda item: (item["symbol"], item["horizon"])),
            "blocked_symbols": blocked, "owned_shares": owned, "reason_codes": sorted(reasons),
            "last_saved_reconciliation_at": _timestamp(latest["observed_at"]) if latest else None,
            "last_saved_reconciliation_ready": latest["ready"] == 1 if latest else None,
            "current_broker_reconciliation_performed": False}
    except (sqlite3.Error, ValueError, TypeError, KeyError, InvalidOperation, OSError):
        return _ownership_unavailable("OWNERSHIP_READ_OR_CONSISTENCY_CHECK_FAILED")


def capture_trade_planning_snapshot(
    datastore_root: Path, *, symbols: Sequence[str], session=None, observed_at=None,
) -> dict:
    """Read one sanitized, cash-only planning snapshot without publishing state.

    Unknown cash remains ``None``. A planning consumer must treat it as a zero
    allocation budget. Quote reference times always come from Schwab, never the
    capture timestamp; this afterhours snapshot confers no execution authority.
    """
    requested = tuple(dict.fromkeys(str(value).strip().upper() for value in symbols))
    if not requested or any(symbol not in STOCK_TRADER_SYMBOLS for symbol in requested):
        raise ValueError("Trade planning requires configured production symbols")
    timestamp = utc(observed_at).isoformat()
    result = {"schema_version": "gameplan-trade-planning-snapshot-v1",
        "authority": "INFORMATIONAL_READ_ONLY", "observed_at": timestamp,
        "status": "UNAVAILABLE", "reason_codes": [], "account_equity": None,
        "available_cash": None, "broker_available_cash": None, "gross_exposure": None,
        "balances": {key: None for key in _BALANCES}, "cash_status": "UNAVAILABLE",
        "cash_reason_codes": [], "reserved_cash": None, "held_shares": {},
        "symbol_exposure": {}, "stock_market_value_by_symbol": {}, "other_symbol_exposure": {},
        "pending_buy_shares": {}, "pending_sell_shares": {},
        "working_order_count": None, "quotes": {},
        "ownership": _ownership_unavailable("BROKER_SNAPSHOT_UNAVAILABLE"),
        "orders_placed": 0, "orders_enabled": False,
        "broker_data_http_methods": ["GET"]}
    component = "AUTHENTICATION"
    try:
        broker = session if session is not None else SchwabSession()
        def attempt(attempt_at):
            nonlocal component
            try:
                component = "AUTHENTICATION"
                identity = broker.prepare_read_snapshot()
                stable_identity = broker.stable_account_fingerprint()
                component = "ACCOUNT"
                account = broker.get_account()
                component = "WORKING_ORDERS"
                orders = broker.get_open_orders()
                component = "QUOTES"
                quotes = broker.get_equity_quotes(requested)
                component = "ACCOUNT_IDENTITY"
                broker.verify_read_snapshot(identity)
                if stable_identity != broker.stable_account_fingerprint():
                    raise ValueError("BROKER_ACCOUNT_CHANGED")
                if not identity or not isinstance(orders, list) or not isinstance(quotes, Mapping):
                    raise ValueError("INVALID_BROKER_SNAPSHOT")
                component = "POSITION_ECONOMICS"
                normalized = normalize_schwab_policy_inputs(account, orders, observed_at=utc(attempt_at).to_pydatetime())
                position_rows = normalized["positions"].get("items")
                raw_account = account.get("securitiesAccount") or account
                raw_positions = raw_account.get("positions")
                if (not isinstance(position_rows, list) or not isinstance(raw_positions, list)
                        or len(position_rows) != len(raw_positions)):
                    result["position_validation_failures"] = [{"reason_code": "POSITION_ROW_SET_UNAVAILABLE_OR_INCOMPLETE"}]
                    raise ValueError("POSITION_ECONOMICS_UNAVAILABLE_OR_AMBIGUOUS")
                failures = [{"position_index": index, "reason_code": reason}
                    for index, (raw, row) in enumerate(zip(raw_positions, position_rows))
                    if (reason := _position_economics_failure(raw, row)) is not None]
                if failures:
                    result["position_validation_failures"] = failures
                    raise ValueError("POSITION_ECONOMICS_UNAVAILABLE_OR_AMBIGUOUS")
                component = "PORTFOLIO_ECONOMICS"
                portfolio = capture_portfolio_state(
                    _CachedReads(account, orders, quotes), observed_at=attempt_at, parallel=False,
                )
                return portfolio, normalized, account, quotes, stable_identity
            except Exception as exc:
                # Fixed local labels give the existing retry receipt a safe
                # failure component without serializing broker exception text.
                exc.stock_trader_operation = component
                raise

        captured, completed_at, retry_metadata = _capture_portfolio_state_with_retry(
            broker, observed_at=timestamp, parallel=False,
            retry_delay_seconds=DEFAULT_BROKER_STATE_RETRY_DELAY_SECONDS,
            maximum_retry_seconds=DEFAULT_BROKER_STATE_RETRY_MAX_SECONDS,
            maximum_attempts=DEFAULT_BROKER_STATE_RETRY_MAX_ATTEMPTS,
            sleep=time.sleep, monotonic=time.monotonic, capture_snapshot=attempt,
        )
        portfolio, normalized, account, quotes, stable_identity = captured
        timestamp = utc(completed_at).isoformat()
        result["observed_at"] = timestamp
        result["broker_state_capture"] = retry_metadata
        values, working = normalized["account_values"], normalized["working_orders"]
        raw_account = account.get("securitiesAccount", account)
        raw_balances = raw_account.get("currentBalances", {})
        balances = {key: finite(values.get(key)) for key in _BALANCES}
        cash_reasons = []
        if any(raw_key in raw_balances and (balances[key] is None or isinstance(raw_balances[raw_key], bool))
               for key, raw_key in _BALANCES.items()):
            cash_reasons.append("REPORTED_CASH_BALANCE_INVALID")
        literal_cash = [balances[key] for key in ("cash_balance", "settled_cash") if balances[key] is not None]
        if not literal_cash:
            cash_reasons.append("LITERAL_CASH_BALANCE_UNAVAILABLE")
        reserved = finite(working.get("reserved_cash"))
        if working.get("status") != "CURRENT" or reserved is None or reserved < 0:
            cash_reasons.append("ACCOUNT_WIDE_PENDING_RESERVES_UNAVAILABLE")
            reserved = None
        cash = None
        if not cash_reasons:
            candidates = [Decimal(str(value)) for value in balances.values() if value is not None]
            cash = float(max(Decimal(0), min(min(candidates) - Decimal(str(reserved)),
                        Decimal(str(portfolio.available_cash)))).quantize(Decimal("0.01"), rounding=ROUND_FLOOR))
        result.update({"status": "OBSERVED" if not cash_reasons else "OBSERVED_WITH_LIMITATIONS",
            "account_equity": portfolio.account_equity, "available_cash": cash,
            "broker_available_cash": portfolio.available_cash, "gross_exposure": portfolio.gross_exposure,
            "balances": balances, "cash_status": "CASH_ONLY_BOUNDED" if cash is not None else "UNAVAILABLE",
            "cash_reason_codes": cash_reasons, "reserved_cash": reserved,
            "cash_policy": "MIN_REPORTED_LITERAL_CASH_AND_CASH_TRADING_BALANCE_LESS_ALL_PENDING_RESERVES_CAPPED_BY_BROKER_CAPACITY",
            "working_order_count": portfolio.working_order_count,
            "quotes": {symbol: _quote(quotes.get(symbol), timestamp) for symbol in requested}})
        for key in ("held_shares", "symbol_exposure", "pending_buy_shares", "pending_sell_shares"):
            source = getattr(portfolio, key)
            result[key] = {symbol: source[symbol] for symbol in requested}
        component = "POSITION_EXPOSURE_ATTRIBUTION"
        stock_values = {symbol: Decimal(0) for symbol in requested}
        other_values = {symbol: Decimal(0) for symbol in requested}
        for position in normalized["positions"]["items"]:
            # Match native PortfolioState: gross exposure is absolute reported
            # position value, attributed to the exact underlying. This is a
            # valuation decomposition, never a claim of available sell shares.
            symbol = str(position["symbol"]).upper()
            underlying = str(position.get("underlying_symbol") or symbol).upper()
            value = abs(Decimal(str(position["market_value"])))
            if position["asset_type"] in {"EQUITY", "STOCK"} and symbol in stock_values:
                stock_values[symbol] += value
            elif underlying in other_values:
                other_values[underlying] += value
        for symbol in requested:
            total = stock_values[symbol] + other_values[symbol]
            if abs(total - Decimal(str(result["symbol_exposure"][symbol]))) > Decimal("0.000001"):
                raise ValueError("POSITION_EXPOSURE_ATTRIBUTION_INCONSISTENT")
        result["stock_market_value_by_symbol"] = {symbol: float(value) for symbol, value in stock_values.items()}
        result["other_symbol_exposure"] = {symbol: float(value) for symbol, value in other_values.items()}
        result["position_exposure_basis"] = "ABSOLUTE_REPORTED_POSITION_MARKET_VALUE_MATCHING_NATIVE_GROSS_EXPOSURE"
        result["ownership"] = _ownership(datastore_root, requested, stable_identity, result["held_shares"], timestamp)
    except Exception as exc:
        # Broker exceptions may contain account URLs or order identifiers.
        # Neither their strings nor payloads are suitable for this artifact.
        result["status"] = "UNAVAILABLE"
        result["available_cash"] = None
        result["cash_status"] = "UNAVAILABLE"
        result["reason_codes"] = ["BROKER_SNAPSHOT_READ_OR_VALIDATION_FAILED"]
        result["failed_component"] = component
        if isinstance(exc, _BrokerStateCaptureFailure):
            result["broker_state_capture"] = exc.metadata
    return result


__all__ = ["capture_trade_planning_snapshot"]
