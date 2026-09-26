"""Forward paper trading from recorded forecasts and current public order books.

No live execution adapter exists in this runtime. Setting mode=live is rejected.
"""
from __future__ import annotations

import argparse
from dataclasses import asdict
import hashlib
import json
import math
import os
from pathlib import Path
import re
import signal
import threading
import time

from filelock import FileLock, Timeout
import pandas as pd

from ml.hyperliquid_data_loop import _atomic_json, _utc
from ml.hyperliquid_model_config import load_config as load_model_config
from ml.hyperliquid_paper_policy import load_config, DEFAULT_PAPER_CONFIG_PATH, target_notionals, should_rebalance
from ml.hyperliquid_paper_market import PublicPaperMarket, simulate_fill, consume_fill
from ml.hyperliquid_paper_ledger import PaperLedger
from ml.hyperliquid_paper_seed import mirror_accounts
from ml.hyperliquid_forecast_reader import read_forecast
from datafetching.hyperliquid_candles import INTERVAL_MS

RUN_ID = re.compile(r"\d{8}T\d{6}Z-[a-f0-9]{8}")
ACCOUNTS = ("alex", "jeremy", "clearpond")


def stamp(value):
    result = pd.Timestamp(value)
    if pd.isna(result) or result.tzinfo is None:
        raise ValueError("A timestamp must be timezone-aware.")
    return result.timestamp()


def current_positions(state, coin):
    result = {account: None for account in ACCOUNTS}
    for account, values in state["accounts"].items():
        kind = "spot" if account == "clearpond" else "perp"
        result[account] = next((p for p in values["positions"] if p["coin"] == coin and p["kind"] == kind), None)
    return result


def _policy_checks(config, details):
    """Saved decision evidence only; these values never choose an order."""
    return {
        "policy_reason": details.get("reason"),
        "entry_probability_long": .5 + config.entry_band,
        "entry_probability_short": .5 - config.entry_band,
        "exit_probability_long": .5 + config.exit_band,
        "exit_probability_short": .5 - config.exit_band,
        "minimum_trade_notional": config.min_trade_notional,
        "rebalance_min_delta_fraction": config.rebalance_min_delta_fraction,
        "venue_minimum_fill_notional": 10.0,
        "strategy_direction": details.get("direction"),
    }


def _hold_reason(reason, checks, current, target):
    """Explain an already chosen hold without changing policy or risk gates."""
    if reason in {"stop_loss", "stop_cooldown", "risk_cap"}:
        return reason
    if checks["cooldown_blocks_target"]:
        return "stop_cooldown"
    if reason != "signal_rebalance":
        return reason
    if checks["policy_reason"] in {
        "entry_deadband", "neutral_band", "exit_band",
        "nonpositive_pool_equity", "gross_capacity_exhausted",
    }:
        return checks["policy_reason"]
    if checks["opposing_reduction_unavailable"]:
        return "opposing_reduction_unavailable"
    if current == target == 0 and (
        checks["strategy_direction"] == "long" and checks["account_role"] == "short_perp"
        or checks["strategy_direction"] == "short" and checks["account_role"] != "short_perp"
    ):
        return "opposite_account_direction"
    if checks["cash_limited"]:
        return "insufficient_cash"
    if checks["capacity_limited"]:
        return "account_capacity"
    return "target_unchanged" if current == target else "below_rebalance_threshold"


class PaperRuntime:
    def __init__(self, config_path=DEFAULT_PAPER_CONFIG_PATH, *, market=None, clock=time.time):
        self.config_path = Path(config_path).resolve()
        self.config = load_config(self.config_path)
        self.model_config = load_model_config(self.config.model_config)
        self.markets_config = self.model_config.load_markets()
        if self.markets_config.output_root != self.config.data_root:
            raise ValueError("Paper and model configurations must refer to the same datastore.")
        self.interval = self.markets_config.interval
        self.horizon = self.model_config.horizons_bars[0]
        self.directory = self.config.data_root / "_paper"
        self.control = self.directory / "_runtime"
        self.control.mkdir(parents=True, exist_ok=True)
        self.market = market or PublicPaperMarket(clock=clock)
        self.clock = clock
        self.ledger = None
        self._frames = {}
        self._last_export = 0.0
        self._last_funding_check = 0.0
        self._funding_errors = {}
        self._stopped = threading.Event()
        self._seed_quotes = None
        self._state = {"mode": "paper", "status": "starting", "pid": os.getpid(), "config_path": str(self.config_path)}

    def initialize(self):
        database = self.directory / "ledger.sqlite3"
        if database.exists():
            self.ledger = PaperLedger(database, open_existing=True)
        elif self.config.seed_mode == "mirror":
            seed = mirror_accounts(self.market, self.markets_config.symbols, clock=self.clock)
            self._seed_quotes = seed.pop("quotes")
            self.ledger = PaperLedger(database, **seed)
        else:
            self.ledger = PaperLedger(database, self.config.initial_cash, now=self.clock(),
                                      metadata={"seed_mode": "cash"})
        _atomic_json(self.directory / "opening_snapshot.json", self.ledger.seed())
        settings = {key: str(value) if isinstance(value, Path) else value for key, value in asdict(self.config).items()}
        settings["recipe_version"] = ("direction-volatility-v2-qualified-hold" if self.config.require_qualified_forecasts
                                      else "direction-volatility-v1")
        self.policy_id = hashlib.sha256(json.dumps(settings, sort_keys=True).encode()).hexdigest()[:16]
        policy_path = self.directory / "policies" / f"{self.policy_id}.json"
        policy_path.parent.mkdir(parents=True, exist_ok=True)
        if not policy_path.exists():
            _atomic_json(policy_path, settings)
        _atomic_json(self.directory / "policy.json", {**settings, "policy_id": self.policy_id})
        self._stops = {}
        for row in self.ledger.history("fills"):
            if row["reason"] == "stop_loss":
                self._stops[(row["account"], row["coin"])] = stamp(row["timestamp_utc"])
        path = self.control / "funding_cursor.json"
        self.funding_cursor = json.loads(path.read_text()) if path.exists() else {}

    def _execute(self, key, marks, *, orders=(), transfers=(), funding=(), decisions=()):
        def versioned(rows):
            return [{**row, "policy_id": self.policy_id} for row in rows]
        result = self.ledger.execute_cycle(key, self.clock(), marks, versioned(orders),
                                          transfers=versioned(transfers), funding=versioned(funding), decisions=versioned(decisions))
        return result

    def _features(self, coin, run_id):
        if not RUN_ID.fullmatch(run_id):
            raise ValueError("Invalid local data run identifier.")
        cached = self._frames.get(coin)
        if cached is None or cached[0] != run_id:
            path = self.config.data_root / coin / self.interval / "runs" / run_id / "features.parquet"
            self._frames[coin] = (run_id, pd.read_parquet(path, columns=["close_time", "close", "volatility_log_return_20"]))
        return self._frames[coin][1]

    def _forecast(self, coin, now):
        return read_forecast(coin, now, self.config, self.interval, self.horizon,
                             feature_loader=self._features)

    def _funding(self, marks, now):
        # Use realized exchange rates with the exact completed-candle close as
        # an explicitly estimated oracle proxy. Reconstruct quantity at that
        # settlement from initial inventory and timestamped fills, not today's qty.
        if now - self._last_funding_check < 300:
            return
        self._last_funding_check = now
        seed = self.ledger.seed()
        initial = self.ledger.history("initial_positions")
        fills = self.ledger.history("fills")
        coins = {p["coin"] for p in initial if p["kind"] == "perp"}
        coins.update(p["coin"] for p in fills if p["kind"] == "perp")
        seed_time = stamp(seed["timestamp_utc"])
        for coin in coins:
            try:
                start = int(self.funding_cursor.get(coin, seed_time * 1000)) + 1
                pointer = json.loads((self.config.data_root / coin / self.interval / "latest.json").read_text())
                candles = self._features(coin, pointer["run_id"]).set_index("close_time")["close"]
                rows = self.market.funding_history(coin, start, int(now * 1000))
                for row in rows:
                    settlement = row["time"] / 1000
                    proxy_time = pd.Timestamp(row["time_utc"]).floor("h")
                    price = candles.get(proxy_time)
                    if price is None:
                        raise ValueError("Funding awaits its exact completed-candle price proxy.")
                    for account in ("alex", "jeremy"):
                        quantity = sum(p["quantity"] for p in initial if p["account"] == account and p["coin"] == coin and p["kind"] == "perp")
                        quantity += sum(p["quantity"] for p in fills if p["account"] == account and p["coin"] == coin and p["kind"] == "perp" and stamp(p["timestamp_utc"]) < settlement)
                        if abs(quantity) < 1e-10:
                            continue
                        key = f"funding:{coin}:{row['time']}:{account}"
                        self._execute(key, marks, funding=[{
                            "funding_id": key, "account": account, "coin": coin,
                            "amount": -quantity * float(price) * row["funding_rate"],
                            "rate": row["funding_rate"], "settlement_utc": row["time_utc"],
                            "quantity_at_settlement": quantity, "oracle_proxy": float(price),
                            "oracle_proxy_close_utc": proxy_time.isoformat(),
                            "estimated": True, "reason": "realized_rate_with_completed_perp_close_proxy",
                        }])
                    self.funding_cursor[coin] = row["time"]
                    _atomic_json(self.control / "funding_cursor.json", self.funding_cursor)
                self._funding_errors.pop(coin, None)
            except Exception as exc:
                self._funding_errors[coin] = str(exc)

    def _plan_transfers(self, state, coin, targets, marks):
        cfg = self.config
        available = {a: max(0.0, values["free_cash"] - cfg.reserve_cash_fraction * max(0, values["initial_equity"]))
                     for a, values in state["accounts"].items()}
        donation_room = dict(available)
        extra = {a: 0.0 for a in ACCOUNTS}
        positions = current_positions(state, coin)
        result = []
        for receiver in ACCOUNTS:
            account = state["accounts"][receiver]
            current = abs(positions[receiver]["quantity"]) * marks[f"{'spot' if receiver == 'clearpond' else 'perp'}:{coin}"] if positions[receiver] else 0.0
            if abs(targets[receiver]) <= current:
                continue
            if receiver == "clearpond":
                required = max(0.0, targets[receiver] - current) * (1 + cfg.spot_fee_rate + .002)
                deficit = max(0.0, required - available[receiver])
            else:
                required = (account["gross_exposure"] - current + abs(targets[receiver])) / cfg.account_utilization
                deficit = max(0.0, required - account["equity"] - extra[receiver])
            for donor in ACCOUNTS:
                if donor == receiver or deficit < cfg.transfer_min_amount:
                    continue
                donor_account = state["accounts"][donor]
                safe = min(available[donor], donation_room[donor], max(0.0, donor_account["equity"] + extra[donor]
                                                - donor_account["gross_exposure"] / cfg.account_utilization))
                # A receiving account does not donate the cash it just acquired.
                amount = min(deficit, safe)
                if amount < cfg.transfer_min_amount:
                    continue
                result.append({"from_account": donor, "to_account": receiver, "amount": amount,
                               "reason": "paper_target_collateral_allocation"})
                available[donor] -= amount
                donation_room[donor] -= amount
                extra[donor] -= amount
                available[receiver] += amount
                extra[receiver] += amount
                deficit -= amount
        return result, available, extra

    def _trade_coin(self, coin, quotes, marks, prediction, sigma, error=None):
        cfg, now = self.config, self.clock()
        state = self.ledger.state(marks)
        positions = current_positions(state, coin)
        current = {a: (p["quantity"] * marks[p["market"]] if p else 0.0) for a, p in positions.items()}
        forecast_id = prediction["prediction_id"] if prediction else None
        excluded_forecast = bool(prediction and cfg.require_qualified_forecasts and not prediction["qualified"])
        signal_prediction = None if excluded_forecast else prediction
        hold_without_signal = cfg.require_qualified_forecasts and signal_prediction is None
        key = f"forecast:{forecast_id}" if prediction else f"stale:{coin}:{int(now // 900)}"
        stop_accounts = [a for a, p in positions.items() if p and
                         p["quantity"] * (marks[p["market"]] - p["avg_entry"]) / (abs(p["quantity"]) * p["avg_entry"]) <= -cfg.stop_loss_fraction]
        cooldown = self.horizon * INTERVAL_MS[self.interval] / 1000
        cooldown_accounts = {a for a in ACCOUNTS if current[a] and now - self._stops.get((a, coin), -math.inf) < cooldown}
        over_limit = any(v["gross_exposure"] > max(0, v["equity"]) for a, v in state["accounts"].items() if a != "clearpond")
        if cfg.require_qualified_forecasts:
            pooled_equity = max(0.0, state["pooled"]["equity"])
            managed_gross = sum(abs(n) for n in current.values())
            passive_gross = sum(p["notional"] for p in state["pooled"]["positions"] if p["coin"] == coin and p["passive"])
            gross_capacity = min(max(0.0, pooled_equity * cfg.per_symbol_gross_fraction - passive_gross),
                                 max(0.0, pooled_equity * cfg.pool_gross_fraction
                                     - (state["pooled"]["gross_exposure"] - managed_gross)))
            over_limit = (managed_gross > gross_capacity + 1e-8 or any(
                v["gross_exposure"] > max(0.0, v["equity"]) * cfg.account_utilization + 1e-8
                for v in state["accounts"].values()))
        already_done = self.ledger.has_cycle(key)
        if already_done and not stop_accounts and not over_limit and not (cfg.require_qualified_forecasts and cooldown_accounts):
            return False
        if prediction and not excluded_forecast:
            plan = target_notionals(prediction["p_not_down"], sigma, max(0, state["pooled"]["equity"]), current,
                                   max(0, state["pooled"]["gross_exposure"] - sum(abs(n) for n in current.values())), cfg)
            targets, details = plan["targets"], plan["details"]
        else:
            targets = dict(current) if hold_without_signal else {a: 0.0 for a in ACCOUNTS}
            details = {"reason": error or "unqualified_forecast_excluded"}
            if hold_without_signal:
                details["signal_action"] = "hold_until_qualified_forecast"
            if excluded_forecast:
                # Keep the rejected publication for audit without presenting it
                # as the source of a policy-driven reduction or hold decision.
                details["rejected_forecast"] = dict(prediction)
        proposed_targets = dict(targets)
        for account in stop_accounts:
            targets[account] = 0.0
        for account in ACCOUNTS:
            if now - self._stops.get((account, coin), -math.inf) < cooldown:
                targets[account] = 0.0
        if already_done:
            key = f"risk:{coin}:{int(now // cfg.poll_seconds)}"
            for account in ACCOUNTS:
                if account not in stop_accounts and not (cfg.require_qualified_forecasts and account in cooldown_accounts):
                    targets[account] = current[account]
        cap_accounts = set()
        if cfg.require_qualified_forecasts:
            target_gross = sum(abs(n) for n in targets.values())
            if target_gross > gross_capacity:
                bounded = {a: n * gross_capacity / target_gross for a, n in targets.items()}
                cap_accounts = {a for a in ACCOUNTS if abs(bounded[a]) < abs(current[a]) and abs(bounded[a]) < abs(targets[a])}
                targets = bounded
        if not prediction and not any(current.values()):
            if not self.ledger.has_cycle(key):
                self._execute(key, marks, decisions=[{"coin": coin, "action": "skip",
                    "reason": "qualified_forecast_unavailable" if hold_without_signal else error, "policy": details,
                    "decision_checks": _policy_checks(cfg, details)}])
            return False
        # Only books observed after forecast availability can supply fills.
        quote_issues = {}
        unavailable_reduction = False
        for account in ACCOUNTS:
            kind = "spot" if account == "clearpond" else "perp"
            book = quotes.get(f"{kind}:{coin}")
            issue = None
            if book is None:
                issue = f"No executable {kind} book for {coin}."
            elif signal_prediction and stamp(book["book_time_utc"]) < stamp(signal_prediction["created_at_utc"]):
                issue = "Executable book predates the forecast."
            elif not -5 <= now - stamp(book["book_time_utc"]) <= cfg.max_quote_age_seconds:
                issue = "Executable book is stale or future-dated."
            if issue:
                quote_issues[account] = issue
                unavailable_reduction |= abs(targets[account]) < abs(current[account])
                targets[account] = current[account]
        suppressed_increases = set()
        if unavailable_reduction:
            # Do not assume an unavailable opposing leg has been closed when
            # budgeting a new leg. Other executable reductions can proceed.
            suppressed_increases = {a for a, t in targets.items() if abs(t) > abs(current[a])}
            targets = {a: current[a] if abs(t) > abs(current[a]) else t for a, t in targets.items()}
        transfers, available, extra = self._plan_transfers(state, coin, targets, marks)
        orders, decisions = [], []
        available_books = dict(quotes)
        execution_order = sorted(ACCOUNTS, key=lambda a: 0 if abs(targets[a]) < abs(current[a]) else 1)
        for account in execution_order:
            kind = "spot" if account == "clearpond" else "perp"
            values = state["accounts"][account]
            target = targets[account]
            cash_limited = False
            if kind == "spot":
                target = min(target, current[account] + available[account] / (1 + cfg.spot_fee_rate + .002))
                cash_limited = target < targets[account]
                other = values["gross_exposure"] - abs(current[account])
                capacity = max(0, (values["equity"] + extra[account]) * cfg.account_utilization - other)
                capacity_limited = target > capacity
                target = min(target, capacity)
            else:
                other = values["gross_exposure"] - abs(current[account])
                capacity = max(0, (values["equity"] + extra[account]) * cfg.account_utilization - other)
                capacity_limited = abs(target) > capacity
                target = math.copysign(min(abs(target), capacity), target)
            if cfg.require_qualified_forecasts and abs(target) < abs(current[account]) and abs(target) < abs(targets[account]):
                cap_accounts.add(account)
            reason = ("stop_loss" if account in stop_accounts else
                      "stop_cooldown" if cfg.require_qualified_forecasts and account in cooldown_accounts else
                      "risk_cap" if account in cap_accounts else
                      "unqualified_forecast_excluded" if excluded_forecast else
                      "qualified_forecast_unavailable" if hold_without_signal else error or "signal_rebalance")
            # Flat accounts remain excluded from cooldown_accounts above: that
            # set controls risk retries. Diagnose their blocked entry separately.
            cooldown_end = self._stops.get((account, coin), -math.inf) + cooldown
            cooldown_remaining = max(0.0, cooldown_end - now)
            force_reduce = (account in stop_accounts or over_limit
                            or cfg.require_qualified_forecasts and account in cooldown_accounts)
            rebalance = should_rebalance(current[account], target, cfg, force_reduce=force_reduce)
            checks = {
                **_policy_checks(cfg, details), "trigger_reason": reason,
                "account_role": {"alex": "short_perp", "jeremy": "long_perp", "clearpond": "long_spot"}[account],
                "delta_notional": target - current[account],
                "proposed_target_notional": proposed_targets[account],
                "rebalance_threshold_notional": max(cfg.min_trade_notional, cfg.rebalance_min_delta_fraction * abs(target)),
                "account_capacity_notional": capacity, "available_cash": available[account],
                "cash_limited": cash_limited, "capacity_limited": capacity_limited,
                "opposing_reduction_unavailable": account in suppressed_increases,
                "cooldown_until_utc": _utc(cooldown_end) if cooldown_remaining else None,
                "cooldown_remaining_seconds": cooldown_remaining,
                "cooldown_blocks_target": bool(cooldown_remaining and proposed_targets[account] and target == 0),
                "rebalance_required": rebalance, "rebalance_forced": force_reduce,
            }
            common = {"account": account, "coin": coin, "kind": kind,
                      "forecast_id": signal_prediction["prediction_id"] if signal_prediction else None,
                      "model_id": signal_prediction["model_id"] if signal_prediction else None,
                      "qualified": signal_prediction["qualified"] if signal_prediction else None,
                      "data_run_id": signal_prediction["data_run_id"] if signal_prediction else None,
                      "forecast_created_at_utc": signal_prediction["created_at_utc"] if signal_prediction else None,
                      "p_not_down": signal_prediction["p_not_down"] if signal_prediction else None,
                      "current_notional": current[account], "target_notional": target,
                      "reason": reason, "policy": details}
            decision = {**common, "decision_checks": checks}
            if account in quote_issues:
                decisions.append({**decision, "action": "skip", "reason": quote_issues[account]})
                continue
            if rebalance:
                delta = (target - current[account]) / marks[f"{kind}:{coin}"]
                fill = simulate_fill(available_books[f"{kind}:{coin}"], delta, cfg.slippage_bps,
                                     min_notional=10, now=now)
                if fill["quantity"] and abs(current[account] + fill["quantity"] * marks[f"{kind}:{coin}"]) > abs(current[account]):
                    mark = marks[f"{kind}:{coin}"]
                    fee_rate = cfg.spot_fee_rate if kind == "spot" else cfg.perp_fee_rate
                    side = 1 if delta > 0 else -1
                    unit_cost = max(0.0, fee_rate * fill["price"] + side * (fill["price"] - mark))
                    room = max(0.0, (values["equity"] + extra[account]) * cfg.account_utilization - values["gross_exposure"])
                    maximum_addition = room / (mark + cfg.account_utilization * unit_cost)
                    if abs(fill["quantity"]) > maximum_addition:
                        fill = simulate_fill(available_books[f"{kind}:{coin}"], side * maximum_addition,
                                             cfg.slippage_bps, min_notional=10, now=now)
                checks.update(execution_status=fill["status"], execution_reason=fill.get("reason"))
                decisions.append({**decision, "action": "fill" if fill["quantity"] else "skip",
                                  "reason": reason if fill["quantity"] else fill.get("reason") or fill["status"],
                                  "execution": fill})
                if fill["quantity"]:
                    available_books[f"{kind}:{coin}"] = consume_fill(available_books[f"{kind}:{coin}"], fill)
                    orders.append({**common, **fill, "fee_rate": cfg.spot_fee_rate if kind == "spot" else cfg.perp_fee_rate,
                                   "reason": reason})
            else:
                decisions.append({**decision, "action": "hold",
                                  "reason": _hold_reason(reason, checks, current[account], target)})
        # Risk reductions are processed first; each account retains its own cash.
        orders.sort(key=lambda order: 0 if current[order["account"]] * order["quantity"] < 0 else 1)
        self._execute(key, marks, orders=orders, transfers=transfers, decisions=decisions)
        for order in orders:
            if order["reason"] == "stop_loss":
                self._stops[(order["account"], coin)] = now
        return bool(orders or transfers)

    def tick(self):
        self.markets_config = self.model_config.load_markets()
        seed = self.ledger.seed()
        inherited = {p["coin"] for p in self.ledger.history("initial_positions")}
        held = {p["coin"] for p in self.ledger.inventory()}
        symbols = sorted(set(self.markets_config.symbols) | inherited | held)
        observation = self._seed_quotes or self.market.snapshot(symbols)
        self._seed_quotes = None
        quotes = observation["markets"]
        marks = {key: value["mark"] for key, value in quotes.items()}
        now = self.clock()
        # Missing held-asset marks cannot silently disappear from pool risk/P&L.
        self.ledger.state(marks)
        self._funding(marks, now)
        changes, errors = False, {}
        for coin in symbols:
            try:
                try:
                    if coin not in self.markets_config.symbols:
                        raise ValueError("Market removed from the configured strategy universe.")
                    prediction, sigma = self._forecast(coin, now)
                    error = None
                except (OSError, ValueError, KeyError, TypeError) as exc:
                    prediction, sigma, error = None, None, str(exc)
                changed = self._trade_coin(coin, quotes, marks, prediction, sigma, error)
                changes = changes or changed
                if error:
                    errors[coin] = error
            except Exception as exc:
                errors[coin] = f"{type(exc).__name__}: {exc}"
        self._execute(f"mark:{int(now // self.config.poll_seconds)}", marks)
        state = self.ledger.state(marks)
        self._state.update(status="running", updated_at_utc=_utc(self.clock()), last_error=None,
                           errors=errors, quote_errors=observation["errors"], funding_errors=self._funding_errors,
                           portfolio=state, simulated=True, funding_valuation="realized_rate/candle_close_proxy",
                           poll_seconds=self.config.poll_seconds, horizon_bars=self.horizon)
        _atomic_json(self.control / "status.json", self._state)
        if changes or now - self._last_export >= 900:
            self.ledger.export(self.directory)
            self._last_export = now
            self.report(state)
        return self._state

    def report(self, state):
        fills = self.ledger.history("fills")
        equity = pd.DataFrame(self.ledger.history("equity"))
        drawdown = 0.0
        if not equity.empty:
            # Journal contains each account plus a pooled row for every cycle.
            # Preserve each pooled observation; never sum multiple valuations.
            curve = equity.loc[equity.account.eq("pooled"), "equity"].reset_index(drop=True)
            baseline = state["pooled"]["initial_equity"]
            peak = curve.cummax().clip(lower=baseline)
            drawdown = float(((curve / peak - 1).min()))
        result = {"as_of_utc": _utc(self.clock()), "mode": "paper", "portfolio": state,
                  "fill_count": len(fills), "transfer_count": len(self.ledger.history("transfers")),
                  "turnover": sum(row["notional"] for row in fills), "max_drawdown_fraction": drawdown,
                  "fees": state["pooled"]["fees"], "funding": state["pooled"]["funding"],
                  "funding_is_estimated": True, "funding_errors": self._funding_errors,
                  "qualification_fill_counts": {label: sum(json.loads(row["details_json"]).get("qualified") is flag for row in fills)
                                                for label, flag in (("qualified", True), ("research", False), ("unavailable", None))},
                  "cash_benchmark_return": 0.0,
                  "limitations": ["Visible-book simulated fills, no queue or latency model", "Funding uses a completed-perp-close oracle proxy",
                                  "Risk exits are polled and can gap; no liquidation-engine simulation", "Opening positions are inherited; strategy P/L starts at snapshot equity"]}
        _atomic_json(self.directory / "performance.json", result)
        return result

    def run(self, once=False):
        lock = FileLock(str(self.control / ".paper.lock"), timeout=0)
        with lock:
            self.initialize()
            try:
                while not self._stopped.is_set() and not (self.control / "stop.request").exists():
                    try:
                        self.tick()
                    except Exception as exc:
                        self._state.update(status="degraded", updated_at_utc=_utc(self.clock()), last_error=f"{type(exc).__name__}: {exc}")
                        _atomic_json(self.control / "status.json", self._state)
                    if once:
                        break
                    self._stopped.wait(self.config.poll_seconds)
            finally:
                self._state["status"] = "stopped" if not self._state.get("last_error") else "failed"
                _atomic_json(self.control / "status.json", self._state)
                (self.control / "stop.request").unlink(missing_ok=True)
                self.ledger.export(self.directory)
                self.ledger.close()
        return self._state


def read_status(root):
    control = Path(root) / "_paper" / "_runtime"
    control.mkdir(parents=True, exist_ok=True)
    path = control / "status.json"
    state = json.loads(path.read_text()) if path.exists() else {"status": "not_started"}
    try:
        with FileLock(str(control / ".paper.lock"), timeout=0):
            state["running"] = False
    except Timeout:
        state["running"] = True
    return state


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=DEFAULT_PAPER_CONFIG_PATH)
    parser.add_argument("--once", action="store_true")
    controls = parser.add_mutually_exclusive_group()
    controls.add_argument("--status", action="store_true")
    controls.add_argument("--stop", action="store_true")
    args = parser.parse_args(argv)
    config = load_config(args.config)
    if args.status:
        result = read_status(config.data_root)
    elif args.stop:
        control = config.data_root / "_paper" / "_runtime"
        control.mkdir(parents=True, exist_ok=True)
        if read_status(config.data_root)["running"]:
            _atomic_json(control / "stop.request", {"requested_at_utc": _utc(time.time())})
            result = {"status": "stop_requested"}
        else:
            result = {"status": "already_stopped"}
    else:
        runtime = PaperRuntime(args.config)
        signal.signal(signal.SIGINT, lambda *_: runtime._stopped.set())
        signal.signal(signal.SIGTERM, lambda *_: runtime._stopped.set())
        result = runtime.run(once=args.once)
    print(json.dumps(result, indent=2))
    return 1 if result.get("status") == "failed" else 0


if __name__ == "__main__":
    raise SystemExit(main())
