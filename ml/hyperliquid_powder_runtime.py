"""Explicitly activated live IOC consumer; imports and inspection never arm it.

--check observes public/account state without signing or writing a ledger.
Every execution session requires BOTH --activate and --execute. Stopping ends
new submissions; it never flattens inventory or continues risk management.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import signal
import threading
import time

from datafetching.hyperliquid_candles import INTERVAL_MS
from ml.hyperliquid_model_config import load_config as load_model_config
from ml.hyperliquid_paper_policy import load_config as load_paper_config, target_notionals, should_rebalance

ACCOUNTS = ("alex", "jeremy", "clearpond")
DEFAULT_CONFIG_PATH = Path(__file__).resolve().parents[1] / "configs" / "hyperliquid-powder.json"
PENDING = {"PREPARED", "SUBMITTING", "UNKNOWN", "OPEN"}


class SafetyHalt(RuntimeError):
    """State changed outside this activation's reconciled execution history."""


def _number(value, name, *, positive=False):
    if type(value) not in (int, float) or not math.isfinite(value) or (positive and value <= 0):
        raise ValueError(f"{name} must be a finite {'positive ' if positive else ''}number")
    return float(value)


def _unique(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"Duplicate Powder setting: {key}")
        result[key] = value
    return result


def _json(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False, default=str)


@dataclass(frozen=True)
class PowderConfig:
    paper_config: Path
    mode: str = "powder"
    max_order_notional: float = 500.0
    poll_seconds: float = 30.0

    def __post_init__(self):
        if self.mode != "powder":
            raise ValueError("Powder configuration requires mode='powder'")
        if not 0 < _number(self.max_order_notional, "max_order_notional") <= 500:
            raise ValueError("max_order_notional must be positive and at most $500")
        _number(self.poll_seconds, "poll_seconds", positive=True)
        object.__setattr__(self, "paper_config", Path(self.paper_config).resolve())


def load_config(path=DEFAULT_CONFIG_PATH):
    path = Path(path).resolve()
    values = json.loads(path.read_text(encoding="utf-8-sig"), object_pairs_hook=_unique,
                        parse_constant=lambda value: (_ for _ in ()).throw(ValueError(f"Invalid number {value}")))
    if not isinstance(values, dict):
        raise ValueError("Powder settings must be an object")
    if set(values) != {"version", "mode", "paper_config", "max_order_notional", "poll_seconds"}:
        raise ValueError("Powder settings require exactly version, mode, paper_config, max_order_notional, poll_seconds")
    version = values.pop("version")
    if type(version) is not int or version != 1 or values.get("mode") != "powder":
        raise ValueError("Powder settings require version 1 and mode powder")
    raw = values["paper_config"]
    if not isinstance(raw, str) or not raw.strip():
        raise ValueError("paper_config must be a nonempty path")
    values["paper_config"] = Path(raw) if Path(raw).is_absolute() else path.parent / raw
    return PowderConfig(**values)


def _settings(path):
    config = load_config(path)
    policy = load_paper_config(config.paper_config)
    model = load_model_config(policy.model_config)
    markets = model.load_markets()
    if markets.output_root != policy.data_root:
        raise ValueError("Powder policy and model data roots differ")
    document = {"powder": asdict(config), "policy": asdict(policy),
                "models": asdict(model), "markets": asdict(markets)}
    digest = hashlib.sha256(_json(document).encode()).hexdigest()
    return config, policy, model, markets, digest


def _utc(now):
    return datetime.fromtimestamp(now, timezone.utc).isoformat()


def _atomic(path, payload):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        temporary.write_text(_json(payload), encoding="utf-8")
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def read_status(config_path=DEFAULT_CONFIG_PATH):
    """No directories, lock probes, activation, credentials or ledger creation."""
    _, policy, _, _, _ = _settings(config_path)
    path = policy.data_root / "_powder" / "_runtime" / "status.json"
    if not path.exists():
        return {"mode": "powder", "state": "NOT_ACTIVATED", "execution_enabled": False}
    result = json.loads(path.read_text(encoding="utf-8"))
    result["saved_status_only"] = True
    return result


class PowderRuntime:
    def __init__(self, config_path=DEFAULT_CONFIG_PATH, *, broker, ledger=None,
                 clock=time.time, forecast_reader=None):
        self.config_path = Path(config_path).resolve()
        self.config, self.policy, self.model, self.markets, self.digest = _settings(self.config_path)
        self.symbols, self.interval = tuple(self.markets.symbols), self.markets.interval
        self.horizon = self.model.horizons_bars[0]
        self.horizon_seconds = INTERVAL_MS[self.interval] * self.horizon / 1000
        self.root = self.policy.data_root / "_powder"
        self.control = self.root / "_runtime"
        self.broker, self.ledger, self.clock = broker, ledger, clock
        if forecast_reader is None:
            from ml.hyperliquid_forecast_reader import read_forecast
            forecast_reader = read_forecast
        self.forecast_reader = forecast_reader
        self.stopped = threading.Event()
        self.binding = None
        self._activated = False
        self._ready = False
        self._halt_reason = None
        self.forecast_errors = {}
        self.risk_limit_violations = []
        self.broker.pre_submit_check = self._pre_submit_check

    def _status(self, state, reason="", error=None):
        result = {"mode": "powder", "state": state, "updated_at_utc": _utc(self.clock()),
                  "pid": os.getpid(), "execution_enabled": self._activated and state not in {"HALTED", "STOPPED"},
                  "reason": reason, "last_error": error, "policy_digest": self.digest,
                  "forecast_errors": self.forecast_errors, "risk_limit_violations": self.risk_limit_violations}
        _atomic(self.control / "status.json", result)
        return result

    def _stop_requested(self):
        return self.stopped.is_set() or (self.control / "stop.request").exists()

    def _identities(self):
        identities = self.broker.preflight()
        if not isinstance(identities, dict) or set(identities) != set(ACCOUNTS):
            raise SafetyHalt("Exactly the three configured account identities are required")
        if any(not isinstance(value, str) or not value for value in identities.values()):
            raise SafetyHalt("Account identity is unavailable")
        if len(set(identities.values())) != len(ACCOUNTS):
            raise SafetyHalt("Powder accounts must bind to distinct owners")
        return identities

    def _observation(self):
        observed = self.broker.observe()
        now = self.clock()
        age = now - _number(observed.get("observed_at"), "observed_at")
        if not -5 <= age <= self.policy.max_quote_age_seconds:
            raise SafetyHalt("Account observation is stale or future dated")
        if observed.get("identities") != self.binding["identities"]:
            raise SafetyHalt("Observed account identities differ from the activation")
        if observed.get("open_orders"):
            raise SafetyHalt("Unreconciled open orders prevent Powder execution")
        if set(observed.get("accounts", {})) != set(ACCOUNTS):
            raise SafetyHalt("An account observation is missing")
        for account, values in observed["accounts"].items():
            for field in ("equity", "available_cash", "gross"):
                _number(values.get(field), f"{account}.{field}")
            if values["gross"] < 0 or values["available_cash"] < 0:
                raise SafetyHalt("Negative gross exposure or available cash")
            if set(values.get("positions", {})) != set(self.symbols):
                raise SafetyHalt("Unknown holdings or incomplete configured market observations")
            for symbol, position in values["positions"].items():
                quantity = _number(position.get("quantity"), f"{account}.{symbol}.quantity")
                _number(position.get("mark"), f"{account}.{symbol}.mark", positive=True)
                if position.get("entry_price") is not None:
                    _number(position["entry_price"], f"{account}.{symbol}.entry_price", positive=True)
                if position.get("kind") != ("spot" if account == "clearpond" else "perp"):
                    raise SafetyHalt("Position market kind violates its account role")
                if account == "alex" and quantity > 0 or account != "alex" and quantity < 0:
                    raise SafetyHalt("Position direction violates its account role")
            passive = values.get("passive_positions", {})
            if not isinstance(passive, dict) or set(passive) - set(self.symbols) or account == "clearpond" and passive:
                raise SafetyHalt("Unknown or misplaced passive holdings")
            for symbol, position in passive.items():
                if _number(position.get("quantity"), "passive quantity") < 0:
                    raise SafetyHalt("Passive inventory cannot be negative")
                _number(position.get("mark"), "passive mark", positive=True)
        return observed

    def _forecasts(self):
        result, errors = {}, {}
        for symbol in self.symbols:
            try:
                forecast, sigma = self.forecast_reader(symbol, self.clock(), self.policy, self.interval, self.horizon)
                if self.policy.require_qualified_forecasts and not forecast["qualified"]:
                    raise ValueError("Research forecasts are excluded by the bound policy")
                result[symbol] = (forecast, sigma)
            except (OSError, ValueError, KeyError, TypeError) as exc:
                errors[symbol] = str(exc)
        self.forecast_errors = errors
        return result, errors

    def check(self):
        identities = self._identities()
        self.binding = {"mode": "powder", "network": self.broker.network,
                        "identities": identities, "policy_digest": self.digest,
                        "symbols": self.symbols, "interval": self.interval, "horizon": self.horizon}
        observed = self._observation()
        forecasts, errors = self._forecasts()
        return {"mode": "powder", "state": "NOT_READY" if errors else "CHECKED", "execution_enabled": False,
                "binding": self.binding, "observation": observed,
                "forecast_symbols": list(forecasts), "forecast_errors": errors}

    def activate(self, *, execute=False):
        if execute is not True:
            raise SafetyHalt("An explicit execution session is required")
        if not getattr(self.broker, "network", None):
            raise SafetyHalt("Exchange network binding is missing")
        self._ready = False
        self._halt_reason = None
        self.binding = {"mode": "powder", "network": self.broker.network,
                        "identities": self._identities(), "policy_digest": self.digest,
                        "symbols": self.symbols, "interval": self.interval, "horizon": self.horizon}
        if self.ledger is None:
            from ml.hyperliquid_powder_ledger import PowderLedger
            self.ledger = PowderLedger(self.root / "ledger.sqlite3", clock=self.clock)
        existing = self.ledger.metadata()
        if existing.get("binding") and _json(existing["binding"]) != _json(self.binding):
            raise SafetyHalt("Persisted activation binding differs; refusing to adopt a different session")
        for intent in self.ledger.pending():
            if intent["state"] == "PREPARED":
                self.ledger.resolve(intent["id"], {"state": "REJECTED", "fills": [], "reason": "abandoned_without_submission"})
        self._activated = True
        if not self._reconcile():
            return self._status("BLOCKED", "Pending intent requires exchange reconciliation")
        _, errors = self._forecasts()
        if errors:
            self._activated = False
            return self._status("NOT_READY", "Every configured market requires an eligible forecast before activation")
        observed = self._observation()
        self.ledger.activate(self.binding, observed)
        self._accept_observation(observed)
        self._ready = True
        return self._status("RUNNING", "Explicit session activated; existing inventory adopted without orders")

    def _reconcile(self):
        for intent in self.ledger.pending():
            if intent["state"] == "PREPARED":
                raise SafetyHalt("Unexpected prepared intent outside activation")
            try:
                result = self.broker.reconcile(intent)
                self.ledger.resolve(intent["id"], result)
            except Exception as exc:
                self.ledger.mark_unknown(intent["id"], f"Reconciliation failed: {type(exc).__name__}: {exc}")
        return not self.ledger.pending()

    def _accept_observation(self, observed):
        previous = self.ledger.latest_observation() or self.ledger.metadata().get("baseline")
        totals, notionals = self.ledger.fill_totals(), self.ledger.fill_notionals()
        inventory = self.ledger.inventory_totals()
        entries = {}
        for account, values in observed["accounts"].items():
            entries[account] = {}
            if previous:
                old_passive = previous["accounts"][account].get("passive_positions", {})
                new_passive = values.get("passive_positions", {})
                for symbol in set(old_passive) | set(new_passive):
                    if not math.isclose(old_passive.get(symbol, {}).get("quantity", 0),
                                        new_passive.get(symbol, {}).get("quantity", 0), rel_tol=1e-8, abs_tol=1e-9):
                        raise SafetyHalt(f"Unexpected external passive position change: {account}/{symbol}")
            for symbol, position in values["positions"].items():
                current = position["quantity"]
                old_position = previous["accounts"][account]["positions"][symbol] if previous else position
                old = old_position["quantity"]
                prior_total = (previous or {}).get("_owned_fill_totals", {}).get(account, {}).get(symbol, 0)
                delta = totals.get(account, {}).get(symbol, 0) - prior_total
                prior_inventory = (previous or {}).get("_owned_inventory_totals", {}).get(account, {}).get(symbol, 0)
                inventory_delta = inventory.get(account, {}).get(symbol, 0) - prior_inventory
                if previous and not math.isclose(current, old + inventory_delta, rel_tol=1e-8, abs_tol=1e-9):
                    raise SafetyHalt(f"Unexpected external position change: {account}/{symbol}")
                entry = position.get("entry_price")
                if entry is None:
                    entry = (previous or {}).get("_risk_entries", {}).get(account, {}).get(symbol)
                    if entry is None and old:
                        entry = old_position["mark"]  # Adoption risk reference, never historical P/L.
                    if delta and abs(current) > abs(old):
                        prior_cost = previous.get("_owned_fill_notionals", {}).get(account, {}).get(symbol, 0) if previous else 0
                        price = (notionals.get(account, {}).get(symbol, 0) - prior_cost) / delta
                        entry = (abs(old) * (entry or old_position["mark"]) + abs(delta) * price) / abs(current)
                entries[account][symbol] = entry if current else None
        observed = {**observed, "_owned_fill_totals": totals, "_owned_fill_notionals": notionals,
                    "_owned_inventory_totals": inventory, "_risk_entries": entries}
        self.ledger.record_observation(observed)
        return observed

    def _plan(self, observed, forecasts, errors):
        accounts = observed["accounts"]
        equity = sum(values["equity"] for values in accounts.values())
        gross = sum(values["gross"] for values in accounts.values())
        baseline = self.ledger.metadata()["baseline"]
        candidates = []
        passive_gross = {symbol: sum(v.get("passive_positions", {}).get(symbol, {}).get("quantity", 0)
                                    * v.get("passive_positions", {}).get(symbol, {}).get("mark", 0)
                                    for v in accounts.values()) for symbol in self.symbols}
        symbol_gross = {symbol: passive_gross[symbol] + sum(abs(v["positions"][symbol]["quantity"]) * v["positions"][symbol]["mark"]
                                                        for v in accounts.values()) for symbol in self.symbols}
        violations = [f"account:{a}" for a, v in accounts.items() if v["gross"] > max(0, v["equity"]) * self.policy.account_utilization + 1e-8]
        if gross > max(0, equity) * self.policy.pool_gross_fraction + 1e-8:
            violations.append("pool")
        violations.extend(f"symbol:{s}" for s in self.symbols if symbol_gross[s] > max(0, equity) * self.policy.per_symbol_gross_fraction + 1e-8)
        self.risk_limit_violations = violations
        for symbol in self.symbols:
            current = {a: accounts[a]["positions"][symbol]["quantity"] * accounts[a]["positions"][symbol]["mark"] for a in ACCOUNTS}
            forecast, sigma = forecasts.get(symbol, (None, None))
            plan = target_notionals(forecast["p_not_down"], sigma, max(0, equity), current,
                                   max(0, gross - sum(map(abs, current.values()))), self.policy) if forecast else {
                                       "targets": dict.fromkeys(ACCOUNTS, 0.0), "details": {"reason": errors.get(symbol, "forecast_unavailable")}}
            managed_target = sum(map(abs, plan["targets"].values()))
            symbol_room = max(0, equity * self.policy.per_symbol_gross_fraction - passive_gross[symbol])
            if managed_target > symbol_room:
                plan["targets"] = {a: value * symbol_room / managed_target for a, value in plan["targets"].items()}
            for account in ACCOUNTS:
                values, position = accounts[account], accounts[account]["positions"][symbol]
                target, reason, priority = plan["targets"][account], "signal_rebalance", 3
                entry = observed["_risk_entries"][account][symbol]
                stopped = bool(current[account] and entry and math.copysign(1, current[account]) * (position["mark"] / entry - 1) <= -self.policy.stop_loss_fraction)
                if stopped:
                    self.ledger.set_cooldown(account, symbol, self.clock() + self.horizon_seconds)
                    target, reason, priority = 0.0, "stop_loss", 0
                elif (self.ledger.get_cooldown(account, symbol) or 0) > self.clock():
                    target, reason, priority = 0.0, "stop_cooldown", 1
                elif not forecast:
                    target, reason, priority = 0.0, "forecast_unavailable", 2
                other = max(0, values["gross"] - abs(current[account]))
                capacity = max(0, values["equity"] * self.policy.account_utilization - other)
                target = math.copysign(min(abs(target), capacity), target)
                over_cap = (values["gross"] > max(0, values["equity"]) * self.policy.account_utilization
                            or gross > max(0, equity) * self.policy.pool_gross_fraction
                            or symbol_gross[symbol] > max(0, equity) * self.policy.per_symbol_gross_fraction)
                reduction = abs(target) < abs(current[account])
                if reduction:
                    priority = min(priority, 1 if over_cap else 2)
                    if over_cap and reason == "signal_rebalance":
                        reason = "risk_cap"
                else:
                    if violations:
                        continue  # Inherited passive exposure may be unmanageable; never add risk over a limit.
                    reserve = max(0, baseline["accounts"][account]["equity"]) * self.policy.reserve_cash_fraction
                    cash_room = max(0, values["available_cash"] - reserve)
                    fee = self.policy.spot_fee_rate if account == "clearpond" else self.policy.perp_fee_rate
                    increase = min(max(0, abs(target) - abs(current[account])), cash_room / (1 + fee + .002))
                    target = math.copysign(abs(current[account]) + increase, target)
                if not should_rebalance(current[account], target, self.policy, force_reduce=stopped or over_cap):
                    continue
                candidates.append((priority, -abs(target - current[account]), account, symbol,
                                   target - current[account], reduction, reason,
                                   {"forecast": forecast, "policy": plan["details"], "current_notional": current[account], "target_notional": target}))
        return sorted(candidates, key=lambda item: item[:4])

    def _pre_submit_check(self, request, actual):
        """Replan after the broker's network reads and immediately before signing."""
        if not self._activated or self._stop_requested():
            raise SafetyHalt("Execution stopped before signing")
        try:
            if _settings(self.config_path)[-1] != self.digest:
                raise SafetyHalt("Bound configuration changed before signing")
            if actual.get("identities") != self.binding["identities"] or self.broker.network != self.binding["network"]:
                raise SafetyHalt("Account/network binding changed before signing")
            observed = self._accept_observation(actual)
        except Exception as exc:
            self._activated = False
            self._halt_reason = f"Pre-submit binding/inventory validation failed: {type(exc).__name__}: {exc}"
            raise SafetyHalt(self._halt_reason) from None
        forecasts, errors = self._forecasts()
        candidates = self._plan(observed, forecasts, errors)
        candidate, blocked_reduction = None, False
        for item in candidates:
            if blocked_reduction and not item[5]:
                break
            try:
                self.broker.make_order(item[2], item[3], item[4], item[5], observed)
            except ValueError:
                blocked_reduction |= item[5]
                continue
            candidate = item
            break
        if candidate is None:
            raise SafetyHalt("Fresh policy has no executable adjustment")
        _, _, account, symbol, delta, reduction, _, _ = candidate
        if request["account"] != account or request["symbol"] != symbol or request["is_buy"] != (delta > 0):
            raise SafetyHalt("Fresh risk/forecast priorities changed; replan before submission")
        position = observed["accounts"][account]["positions"][symbol]
        if request["size"] * position["mark"] > abs(delta) + 1e-7:
            raise SafetyHalt("Rounded order exceeds the freshly marked adjustment")
        if request["reduce_only"] != (reduction and account != "clearpond"):
            raise SafetyHalt("Fresh position target changed the order's risk direction")
        if not reduction:
            if errors or forecasts[symbol][0].get("_valid_until_epoch", 0) <= self.clock():
                raise SafetyHalt("Fresh forecast validation disallows increased exposure")
            # An artifact may change within the same still-fresh horizon. Exact
            # policy replanning above decides whether this rounded order remains safe.
            values = observed["accounts"][account]
            reserve = max(0, self.ledger.metadata()["baseline"]["accounts"][account]["equity"]) * self.policy.reserve_cash_fraction
            fee = self.policy.spot_fee_rate if account == "clearpond" else self.policy.perp_fee_rate
            if request["size"] * request["limit_price"] * (1 + fee) > max(0, values["available_cash"] - reserve) + 1e-7:
                raise SafetyHalt("Fresh collateral cannot cover the bounded order and reserve")
        # Artifact reads and replanning above can take time; stop/config intent
        # must still hold when control returns to the broker's signing boundary.
        try:
            if _settings(self.config_path)[-1] != self.digest:
                raise SafetyHalt("Bound configuration changed during final forecast validation")
        except Exception as exc:
            self._activated = False
            self._halt_reason = f"Final configuration validation failed: {type(exc).__name__}: {exc}"
            raise SafetyHalt(self._halt_reason) from None
        if self._stop_requested():
            raise SafetyHalt("Execution stopped during final forecast validation")

    def tick(self):
        if not self._activated:
            raise SafetyHalt("Runtime is not activated")
        if self._stop_requested():
            return self._status("STOPPED", "Stop requested; inventory was not flattened")
        if _settings(self.config_path)[-1] != self.digest:
            raise SafetyHalt("Bound configuration changed during execution")
        if self._identities() != self.binding["identities"] or self.broker.network != self.binding["network"]:
            raise SafetyHalt("Account/network binding changed during execution")
        if not self._reconcile():
            return self._status("BLOCKED", "Unknown/open intent blocks every submission")
        if not self._ready:
            _, errors = self._forecasts()
            if errors:
                self._activated = False
                return self._status("NOT_READY", "Pending reconciliation completed; forecasts are not ready for activation")
            self._ready = True
        observed = self._accept_observation(self._observation())
        forecasts, errors = self._forecasts()
        skipped = []
        blocked_reduction = False
        for _, _, account, symbol, delta, reduction, reason, decision in self._plan(observed, forecasts, errors):
            if blocked_reduction and not reduction:
                break
            try:
                request = self.broker.make_order(account, symbol, delta, reduction, observed)
            except ValueError as exc:
                skipped.append(f"{account}/{symbol}: {exc}")
                blocked_reduction |= reduction
                continue
            size = _number(request.get("size"), "order size", positive=True)
            price = _number(request.get("limit_price"), "limit price", positive=True)
            if (request.get("account") != account or request.get("symbol") != symbol or request.get("tif") != "Ioc"
                    or type(request.get("is_buy")) is not bool or request["is_buy"] != (delta > 0)
                    or type(request.get("reduce_only")) is not bool or request["reduce_only"] != (reduction and account != "clearpond")
                    or size * price > self.config.max_order_notional + 1e-7 or size * price < 10 - 1e-8):
                raise SafetyHalt("Broker order does not match the bounded IOC plan")
            position = observed["accounts"][account]["positions"][symbol]
            if size * position["mark"] > abs(delta) + 1e-7:
                raise SafetyHalt("Order exceeds the planned adjustment")
            if reduction and size > abs(position["quantity"]) + 1e-9:
                raise SafetyHalt("A reduction cannot cross through zero")
            if not reduction:
                fresh, errors = self._forecasts()
                if errors or _json(fresh.get(symbol)) != _json(forecasts.get(symbol)):
                    return self._status("RUNNING", "Forecast changed or expired before preparation; replan on next tick")
                request["forecast_valid_until"] = _number(fresh[symbol][0].get("_valid_until_epoch"), "validated forecast expiry", positive=True)
                if self.clock() >= request["forecast_valid_until"]:
                    return self._status("RUNNING", "Forecast expired before preparation; replan on next tick")
                account_values = observed["accounts"][account]
                reserve = max(0, self.ledger.metadata()["baseline"]["accounts"][account]["equity"]) * self.policy.reserve_cash_fraction
                fee = self.policy.spot_fee_rate if account == "clearpond" else self.policy.perp_fee_rate
                if size * price * (1 + fee) > max(0, account_values["available_cash"] - reserve) + 1e-7:
                    raise SafetyHalt("Order exceeds cash available after the bound reserve and fee")
            if self._stop_requested():
                return self._status("STOPPED", "Stopped before order preparation")
            intent = self.ledger.prepare(request, reason, decision)
            if self._stop_requested():
                self.ledger.resolve(intent["id"], {"state": "REJECTED", "fills": [], "reason": "stop_before_submission"})
                return self._status("STOPPED", "Stopped before submission")
            self.ledger.mark_submitting(intent["id"])
            intent = self.ledger.get_intent(intent["id"])
            if self._stop_requested():
                self.ledger.resolve(intent["id"], {"state": "REJECTED", "fills": [], "reason": "stop_before_submission"})
                return self._status("STOPPED", "Stopped at submission boundary")
            from ml.hyperliquid_powder_exchange import PreSubmitRejected
            try:
                acknowledgement = self.broker.submit(intent)
                response = acknowledgement.get("response", {}) if isinstance(acknowledgement, dict) else {}
                statuses = response.get("data", {}).get("statuses") if isinstance(response, dict) and isinstance(response.get("data"), dict) else None
                if (isinstance(acknowledgement, dict) and acknowledgement.get("status") == "ok" and response.get("type") == "order"
                        and isinstance(statuses, list) and len(statuses) == 1 and isinstance(statuses[0], dict)
                        and set(statuses[0]) == {"error"} and isinstance(statuses[0]["error"], str)):
                    self.ledger.resolve(intent["id"], {"state": "REJECTED", "fills": [], "reason": statuses[0]["error"]})
                # Every other ACK is deliberately not an execution record.
            except PreSubmitRejected as exc:
                self.ledger.resolve(intent["id"], {"state": "REJECTED", "fills": [], "reason": str(exc)})
            except Exception as exc:
                self.ledger.mark_unknown(intent["id"], f"Submit outcome unknown: {type(exc).__name__}: {exc}")
            self._reconcile()
            if not self._activated:
                return self._status("HALTED", "Manual investigation required; no automatic restart", self._halt_reason)
            if self._stop_requested():
                return self._status("STOPPED", "Stop requested at submission boundary; inventory was not flattened")
            return self._status("BLOCKED" if self.ledger.pending() else "RUNNING",
                                "Submitted one IOC; next tick re-observes and replans")
        return self._status("RUNNING", "; ".join(skipped) or "No executable adjustment; risk and forecasts reviewed")

    def run(self, *, execute=False, max_cycles=None):
        try:
            result = self.activate(execute=execute)
            if result["state"] == "NOT_READY":
                return result
            count = 0
            while not self._stop_requested():
                result = self.tick()
                if result["state"] in {"STOPPED", "NOT_READY", "HALTED"}:
                    return result
                count += 1
                if max_cycles is not None and count >= max_cycles:
                    break
                self.stopped.wait(self.config.poll_seconds)
            return self._status("STOPPED", "Execution session ended; positions remain and risk checks stopped")
        except Exception as exc:
            return self._status("HALTED", "Manual investigation required; no automatic restart", f"{type(exc).__name__}: {exc}")
        finally:
            self._activated = False
            if self.ledger is not None:
                self.ledger.close()


@contextmanager
def _execution_environment():
    names = ("HYPERLIQUID_ENABLE_LIVE_ORDERS", "HYPERLIQUID_ENABLE_POWDER")
    saved = {name: os.environ.get(name) for name in names}
    try:
        for name in names:
            os.environ[name] = "true"
        yield
    finally:
        for name, value in saved.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value


def _dispatch(args):
    if args.status:
        result = read_status(args.config)
    elif args.stop:
        _, policy, _, _, _ = _settings(args.config)
        _atomic(policy.data_root / "_powder" / "_runtime" / "stop.request", {"requested_at_utc": _utc(time.time())})
        result = {"mode": "powder", "state": "STOP_REQUESTED", "execution_enabled": False}
    else:
        from ml.hyperliquid_powder_exchange import PowderExchange
        config, policy, _, markets, _ = _settings(args.config)
        kwargs = dict(max_order_notional=config.max_order_notional,
                      max_quote_age_seconds=policy.max_quote_age_seconds, slippage_bps=policy.slippage_bps)
        if args.check:
            result = PowderRuntime(args.config, broker=PowderExchange(markets.symbols, **kwargs)).check()
        else:
            from ml.hyperliquid_powder_lock import account_ownership
            with account_ownership() as token:
                with _execution_environment():
                    runtime = PowderRuntime(args.config, broker=PowderExchange(markets.symbols, ownership_token=token, **kwargs))
                    # An explicit new activation consumes an old completed stop request.
                    (runtime.control / "stop.request").unlink(missing_ok=True)
                    signal.signal(signal.SIGINT, lambda *_: runtime.stopped.set())
                    signal.signal(signal.SIGTERM, lambda *_: runtime.stopped.set())
                    result = runtime.run(execute=True)
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG_PATH)
    controls = parser.add_mutually_exclusive_group()
    controls.add_argument("--check", action="store_true")
    controls.add_argument("--status", action="store_true")
    controls.add_argument("--stop", action="store_true")
    parser.add_argument("--activate", action="store_true")
    parser.add_argument("--execute", action="store_true")
    args = parser.parse_args(argv)
    if (args.check or args.status or args.stop) and (args.activate or args.execute):
        parser.error("Inspection/stop controls cannot be combined with activation")
    if not (args.check or args.status or args.stop) and not (args.activate and args.execute):
        parser.error("Execution requires BOTH --activate and --execute for every session")
    try:
        result = _dispatch(args)
    except Exception as exc:
        result = {"mode": "powder", "state": "NOT_READY", "execution_enabled": False,
                  "reason": "Configuration, account preflight or ownership validation failed",
                  "error_type": type(exc).__name__}
    print(json.dumps(result, indent=2, default=str))
    return 1 if result.get("state") in {"HALTED", "BLOCKED", "NOT_READY"} else 0


if __name__ == "__main__":
    raise SystemExit(main())
