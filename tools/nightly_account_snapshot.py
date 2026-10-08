"""Atlas-only read-only account snapshot for an explicitly private nightly exchange.

The exchange coordinator owns the production opt-in flag. This adapter verifies
local account authority, performs no order or ledger writes, and returns only
the budget, inventory and horizon ownership used by the joint planner.
"""
from __future__ import annotations

from decimal import Decimal
import json
import math
from pathlib import Path
import re
from time import monotonic

import pandas as pd

from ml.artifacts import utc_timestamp
from ml.joint_capital_handoff import _object, _read
from ml.joint_capital_plan import _validate_snapshot
from ml import nightly_workflow


VERSION = "nightly-private-account-snapshot-v1"
MAXIMUM_CAPTURE_AGE_SECONDS = 60
_MONEY = ("account_equity", "available_cash", "reserved_cash", "gross_exposure")
_MAPS = ("held_shares", "symbol_exposure", "stock_market_value_by_symbol", "other_symbol_exposure",
         "pending_buy_shares", "pending_sell_shares")
_QUANTITIES = ("owned_shares", "reserved_buy_shares", "reserved_sell_shares")


def _path(value) -> Path:
    if (not isinstance(value, str) or not Path(value).is_absolute()
            or value.startswith(("\\\\", "//"))):
        raise ValueError("Snapshot bindings require explicit absolute local paths")
    return Path(value).resolve()


def _hash(value) -> str:
    if not isinstance(value, str) or re.fullmatch(r"[a-f0-9]{64}", value) is None:
        raise ValueError("Snapshot requires an exact account or allocation digest")
    return value


def _stamp(value) -> pd.Timestamp:
    if not isinstance(value, (str, pd.Timestamp)):
        raise ValueError("Snapshot evidence requires an explicit zoned timestamp")
    stamp = pd.Timestamp(value)
    if pd.isna(stamp) or stamp.tzinfo is None:
        raise ValueError("Snapshot evidence requires an explicit zoned timestamp")
    return stamp.tz_convert("UTC")


def _number(value, *, whole=False, positive=False):
    if (type(value) not in (int, float) or not math.isfinite(value) or value < 0
            or (positive and value <= 0) or (whole and value != int(value))):
        raise ValueError("Snapshot contains an unavailable or invalid account quantity")
    return value


def _bindings(config):
    if config.get("actor") != "Atlas" or config.get("private_exchange_authorized") is not True:
        raise ValueError("Private account capture requires explicitly authorized Atlas exchange")
    # Scout's installed source deliberately omits Atlas's account subsystem.
    from ml.account_gameplan.config import CONFIG, load_account_config
    from ml.stock_trader.state import validated_symbols
    scope = _hash(config.get("account_scope_sha256"))
    owners = config.get("owners")
    if not isinstance(owners, dict) or set(owners) != {"atlas", "scout"}:
        raise ValueError("Snapshot requires exactly Atlas and Scout owner partitions")
    partitions = {actor: tuple(validated_symbols(values)) for actor, values in owners.items()}
    if (any(len(values) != 11 for values in partitions.values())
            or set(partitions["atlas"]) & set(partitions["scout"])):
        raise ValueError("Snapshot requires two disjoint eleven-symbol owner partitions")
    paths = {name: _path(config.get(name)) for name in
             ("workflow_config", "local_profile", "coordination_active", "account_config")}
    frozen = {path: _read(path, path.parent) for path in paths.values()}
    workflow = nightly_workflow.load_config(paths["workflow_config"])
    repository = Path(__file__).resolve().parents[1]
    root = _path(workflow["datastore"])
    profile = _object(frozen[paths["local_profile"]])
    if (workflow["actor"] != "Atlas" or _path(workflow["repository"]) != repository
            or paths["local_profile"] != repository / "scratch/cross-pc/local-profile.json"
            or _path(workflow["local_profile"]) != paths["local_profile"]
            or _path(workflow["coordination_active"]) != paths["coordination_active"]
            or paths["coordination_active"] != repository / "scratch/cross-pc/active.json"
            or profile.get("actor") != "Atlas" or profile.get("machine") != "pc-original"
            or profile.get("contract_version") != "cross-pc-v2"
            or profile.get("symbols") != list(partitions["atlas"])
            or _path(profile.get("checkout")) != repository
            or paths["account_config"] != root / CONFIG):
        raise ValueError("Snapshot must use this Atlas checkout's installed local bindings")
    nightly_workflow.verify_installation(workflow)
    account = load_account_config(root)
    if (account is None or account.machine_id != "pc-original" or account.coordinator_id != "pc-original"
            or account.account_fingerprint != scope
            or set(account.participants["pc-original"]) != set(partitions["atlas"])
            or set(account.participants["pc-new"]) != set(partitions["scout"])):
        raise ValueError("Snapshot account identity or complete owner partitions differ")
    return root, workflow, account, frozen


def _project(captured, *, symbols, scope, observed):
    """Allowlist private planning data without exporting diagnostic free text."""
    from ml.account_gameplan.joint_bridge import joint_snapshot
    if (not isinstance(captured, dict) or captured.get("status") != "OBSERVED"
            or captured.get("cash_status") != "CASH_ONLY_BOUNDED"
            or captured.get("orders_placed") != 0 or type(captured.get("orders_placed")) is not int
            or captured.get("orders_enabled") is not False
            or captured.get("account_fingerprint") != scope):
        raise ValueError("Account snapshot is incomplete, unbound or not read-only")
    snapshot = joint_snapshot(captured)
    stamp = _stamp(snapshot.get("observed_at"))
    if not 0 <= (observed - stamp).total_seconds() <= MAXIMUM_CAPTURE_AGE_SECONDS:
        raise ValueError("Account snapshot is stale or future-dated")
    result = {"schema_version": VERSION, "authority": "INFORMATIONAL_READ_ONLY", "observed_at": stamp.isoformat(),
              "account_scope_sha256": scope, "status": "OBSERVED", "cash_status": "CASH_ONLY_BOUNDED",
              "orders_placed": 0, "orders_enabled": False}
    for name in _MONEY:
        result[name] = _number(snapshot.get(name), positive=name == "account_equity")
    for name in _MAPS:
        values = snapshot.get(name)
        if not isinstance(values, dict) or set(values) != set(symbols):
            raise ValueError("Snapshot quantities must cover the exact complete account universe")
        result[name] = {symbol: _number(values[symbol]) for symbol in symbols}
    quotes = snapshot.get("quotes")
    if not isinstance(quotes, dict) or set(quotes) != set(symbols):
        raise ValueError("Snapshot quote coverage differs from its account universe")
    result["quotes"] = {}
    for symbol in symbols:
        if not isinstance(quotes[symbol], dict):
            raise ValueError("Snapshot quote evidence is malformed")
        ask = quotes[symbol].get("ask")
        if ask is not None:
            _number(ask, positive=True)
        if result["pending_buy_shares"][symbol] and ask is None:
            raise ValueError("Pending buys require their observed account exposure price")
        result["quotes"][symbol] = {"ask": ask}
    ownership = snapshot.get("ownership")
    if (not isinstance(ownership, dict) or ownership.get("status") != "OBSERVED_CONSISTENT"
            or ownership.get("safe_for_planning") is not True or ownership.get("account_matches") is not True
            or ownership.get("blocked_symbols") != [] or ownership.get("reason_codes") != []
            or ownership.get("last_saved_reconciliation_ready") is not True
            or ownership.get("current_broker_reconciliation_performed") is not False):
        raise ValueError("Account horizon ownership is incomplete or requires reconciliation")
    saved_at = _stamp(ownership.get("last_saved_reconciliation_at"))
    if saved_at > stamp:
        raise ValueError("Account ownership reconciliation is future-dated")
    owned = ownership.get("owned_shares")
    allocations = ownership.get("active_allocations")
    if not isinstance(owned, dict) or set(owned) != set(symbols) or not isinstance(allocations, list):
        raise ValueError("Account horizon ownership coverage is incomplete")
    selected, totals, reserved, identities, routes = [], dict.fromkeys(symbols, 0), dict.fromkeys(symbols, 0), set(), set()
    for item in allocations:
        if not isinstance(item, dict):
            raise ValueError("Account horizon allocation is malformed")
        identity = _hash(item.get("allocation_id_sha256"))
        symbol, horizon = item.get("symbol"), item.get("horizon")
        if (symbol not in symbols or horizon not in {"1h", "4h", "1d", "1w"}
                or item.get("status") != "ACTIVE" or identity in identities or (symbol, horizon) in routes):
            raise ValueError("Account horizon allocation identity or universe differs")
        quantities = {name: _number(item.get(name), whole=True) for name in _QUANTITIES}
        start, end = _stamp(item.get("target_start")), _stamp(item.get("target_end"))
        if end <= start or quantities["reserved_sell_shares"] > quantities["owned_shares"]:
            raise ValueError("Account horizon allocation quantities or targets disagree")
        selected.append({"allocation_id_sha256": identity, "symbol": symbol, "horizon": horizon,
            "status": "ACTIVE", **quantities, "target_start": start.isoformat(), "target_end": end.isoformat()})
        totals[symbol] += quantities["owned_shares"]
        reserved[symbol] += quantities["reserved_sell_shares"]
        identities.add(identity)
        routes.add((symbol, horizon))
    for symbol in symbols:
        if (_number(owned[symbol], whole=True) != totals[symbol]
                or reserved[symbol] > result["pending_sell_shares"][symbol]
                or totals[symbol] + result["pending_sell_shares"][symbol] - reserved[symbol] > result["held_shares"][symbol]):
            raise ValueError("Horizon inventory and pending sales do not reconcile with current holdings")
        exposure = Decimal(str(result["symbol_exposure"][symbol]))
        attributed = sum(Decimal(str(result[name][symbol])) for name in ("stock_market_value_by_symbol", "other_symbol_exposure"))
        if abs(exposure - attributed) > Decimal("0.01"):
            raise ValueError("Snapshot stock and other exposure attribution disagrees")
    if sum(Decimal(str(value)) for value in result["symbol_exposure"].values()) > Decimal(str(result["gross_exposure"])) + Decimal("0.01"):
        raise ValueError("Account gross exposure omits positions in the joint universe")
    result["ownership"] = {"status": "OBSERVED_CONSISTENT", "safe_for_planning": True, "account_matches": True,
        "active_allocations": sorted(selected, key=lambda item: (item["symbol"], item["horizon"])),
        "blocked_symbols": [], "owned_shares": totals, "reason_codes": [],
        "last_saved_reconciliation_at": saved_at.isoformat(), "last_saved_reconciliation_ready": True,
        "current_broker_reconciliation_performed": False}
    # Invoke the same account/snapshot validator consumed by composition, with
    # its strongest allocation-identity check, before returning any export.
    _validate_snapshot(result, set(symbols), scope, observed, MAXIMUM_CAPTURE_AGE_SECONDS, fallback=True)
    json.dumps(result, allow_nan=False)
    return result


def capture_snapshot(config: dict, now=None) -> dict:
    """Read Atlas's verified union account once; never persist or activate it."""
    root, workflow, account, frozen = _bindings(config)
    from ml.account_gameplan.preparation import _native_snapshot
    started, timer = utc_timestamp(now), monotonic()
    try:
        captured = _native_snapshot(root, account, observed_at=started.isoformat())
    except Exception:
        # Native failures may contain account identifiers or provider messages.
        raise ValueError("Account-wide read-only snapshot capture failed") from None
    completed = started + pd.Timedelta(seconds=max(0, monotonic() - timer))
    for path, original in frozen.items():
        if _read(path, path.parent) != original:
            raise ValueError("Local account snapshot bindings changed during capture")
    nightly_workflow.verify_installation(workflow)
    return _project(captured, symbols=account.symbols, scope=config["account_scope_sha256"], observed=completed)
