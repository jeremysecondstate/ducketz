"""Current directional Gameplans; quantities and prices belong to live execution.

Scientific forecasts remain immutable. New publications derive the exact current
BUY/SELL/HOLD rule without estimated future prices, fills or quantity limits.
"""
from __future__ import annotations

import argparse
import json
import os
from dataclasses import asdict
from decimal import Decimal, ROUND_CEILING
from pathlib import Path
from typing import Mapping

import pandas as pd

from ml.artifacts import create_timestamp_directory, file_checksum, verify_manifest, write_manifest, utc_timestamp
from ml.stock_trader.contracts import PredictionSignal, StockTraderPolicy, finite, utc
from ml.stock_trader.fixed_horizon_budget import FIXED_HORIZON_WEIGHTS, fixed_budget_forecast_readiness
from ml.stock_direction_policy import BULLISH_PROBABILITY, BEARISH_PROBABILITY, STOCK_DIRECTION_POLICY_VERSION
from ml.gameplan_execution_policy import current_gameplan_policy_metadata, direction_only_rows, DIRECTION_ONLY_STATUS


VERSION = "cash-aware-gameplan-trade-planning-v4"
AUTHORITY = "REVIEW_ONLY_REVALIDATE_AT_ENTRY"
PLANNING_PRICE_LOOKBACK_SESSIONS = 504
PRODUCER_MODE = "ACCOUNT_PRODUCER_SOURCE"
RESEARCH_PRODUCER_MODE = "RESEARCH_PRODUCER_SOURCE"
SHARED_PROJECTION_UNAVAILABLE = "UNAVAILABLE_SHARED_ACCOUNT_PROJECTION"


def _planning_snapshot(snapshot: Mapping) -> tuple[dict, dict | None]:
    """Use fresh broker state for review planning when only a stale local buy
    reservation prevents the ownership ledger from being marked ready.

    This is deliberately planning-only.  The immutable account artifact keeps
    the original ownership evidence, while the live trader continues to
    reconcile its ledger and broker state before any order decision.
    """
    ownership = snapshot.get("ownership", {})
    reasons = set(ownership.get("reason_codes", ()))
    pending_buys = snapshot.get("pending_buy_shares", {})
    broker_has_no_pending_buys = not any(_money(value) for value in pending_buys.values())
    if (ownership.get("safe_for_planning") is True
            or reasons != {"PENDING_LEDGER_RESERVATIONS_REQUIRE_RECONCILIATION"}
            or snapshot.get("working_order_count") != 0
            or not broker_has_no_pending_buys):
        return dict(snapshot), None
    planning = dict(snapshot)
    planning_ownership = dict(ownership)
    planning_ownership["safe_for_planning"] = True
    planning_ownership["status"] = "OBSERVED_CONSISTENT_FOR_REVIEW"
    planning_ownership["reason_codes"] = []
    planning_ownership["planning_override"] = "CURRENT_BROKER_HAS_ZERO_WORKING_ORDERS_AND_ZERO_PENDING_BUYS"
    # Do not let a stale local BUY reservation suppress a new informational
    # plan.  This does not mutate the ledger or grant execution authority.
    planning_ownership["active_allocations"] = [
        {**allocation, "reserved_buy_shares": 0}
        for allocation in ownership.get("active_allocations", [])
    ]
    planning["ownership"] = planning_ownership
    return planning, {"type": "STALE_LOCAL_BUY_RESERVATION_IGNORED_FOR_REVIEW",
                      "basis": planning_ownership["planning_override"]}


def _money(value: object) -> Decimal:
    number = Decimal(str(value))
    if not number.is_finite() or number < 0:
        raise ValueError("Trade planning requires nonnegative finite monetary inputs")
    return number


def _signal(row: Mapping) -> PredictionSignal:
    return PredictionSignal(
        symbol=str(row["symbol"]), primary_horizon=str(row["model_group"]),
        prediction_id=str(row["id"]), decision_timestamp=str(row["decision_timestamp"]),
        target_window_start=str(row["target_window_start"]), target_window_end=str(row["target_window_end"]),
        actionable_until=str(row["target_window_end"]), prediction_created_at=str(row["frozen_at"]),
        calibrated_probability=float(row["calibrated_probability"]), assumed_round_trip_cost=0.,
        horizon_probabilities={}, model_name=str(row["model_family"]), model_version=str(row["model_artifact"]),
        source_fingerprint="verified-publication-review", target_definition_version=str(row["target_contract_version"]),
        target_price_source_contract=str(row["target_price_source_contract"]),
    )


def plan_trade_rows(forecasts: pd.DataFrame, snapshot: Mapping, bands: Mapping,
                    *, policy: StockTraderPolicy | None = None) -> pd.DataFrame:
    """Reserve provisional BUY capacity once, without assuming future sale proceeds.

The sizing rule mirrors the selected fixed-confidence budget. Entry-window and
live-control checks are deliberately not simulated; these are review proposals,
not executable TradeDecisions. A shared ledger of planned dollars prevents the
same current cash being promised to several horizons or symbols.
"""
    from ml.gameplan_cash_ledger import _planning_direction

    policy = policy or StockTraderPolicy()
    policy.validate()
    data = forecasts.copy()
    if data.empty or data.id.duplicated().any():
        raise ValueError("Trade planning requires unique frozen forecast rows")
    band_rows = {str(row.get("forecast_id", row.get("id"))): row for row in bands["rows"]}
    if len(bands["rows"]) != len(data) or len(band_rows) != len(data) or set(band_rows) != set(data.id.astype(str)):
        raise ValueError("Price bands differ from the frozen forecast identities")
    ownership = snapshot.get("ownership", {})
    active = {(str(row["symbol"]), str(row["horizon"])) for row in ownership.get("active_allocations", [])}
    blocked_symbols = set(ownership.get("blocked_symbols", []))
    evidence_ready = (snapshot.get("status") == "OBSERVED" and snapshot.get("cash_status") == "CASH_ONLY_BOUNDED"
                      and ownership.get("safe_for_planning") is True)
    try:
        equity = _money(snapshot["account_equity"])
        cash = _money(snapshot["available_cash"]) * _money(policy.maximum_cash_utilization_fraction)
        gross = equity * _money(policy.maximum_gross_equity_fraction) - _money(snapshot["gross_exposure"])
        pending = {}
        for symbol, quantity in snapshot.get("pending_buy_shares", {}).items():
            if _money(quantity):
                ask = _money(snapshot["quotes"][symbol]["ask"])
                if not ask:
                    raise ValueError("A working buy has no price")
                pending[symbol] = _money(quantity) * ask
        # Account-wide BUY/debit reservations can include symbols or assets
        # outside this forecast universe. They consume gross headroom too.
        gross -= max(sum(pending.values(), Decimal(0)), _money(snapshot["reserved_cash"]))
        available = max(Decimal(0), min(cash, gross))
        remaining = {symbol: max(Decimal(0), equity * _money(policy.maximum_symbol_equity_fraction)
                     - _money(snapshot["symbol_exposure"][symbol]) - pending.get(symbol, Decimal(0)))
                     for symbol in data.symbol.unique()}
        if equity <= 0:
            evidence_ready = False
    except (ArithmeticError, TypeError, KeyError, ValueError):
        evidence_ready = False
        equity = available = Decimal(0)
        remaining = {}
    # Each projection is the account's affordable horizon allocation for one
    # opportunity. It is not conditional on the entry signal being bullish and
    # is not summed as a batch of simultaneous orders. Scheduled entries below
    # retain a separate shared reservation ledger and confidence-weighted size.
    projection_available, projection_remaining = available, remaining.copy()
    ordered = data.sort_values(["target_window_start", "calibrated_probability", "symbol", "model_group"],
                               ascending=[True, False, True, True], kind="stable")
    planned_horizons = set()
    batch_counts: dict[str, int] = {}
    results = {}
    for row in ordered.to_dict("records"):
        symbol, horizon = str(row["symbol"]), str(row["model_group"])
        band = band_rows[str(row["id"])]
        quote = snapshot.get("quotes", {}).get(symbol, {})
        quote_freshness_anchor = (band.get("price_reference_effective_at")
                                  if band.get("price_reference_is_synthetic")
                                  else band.get("price_reference_observed_at"))
        result = {**row, **{key: value for key, value in band.items() if key not in row},
                  "trade_quantity": 0, "trade_action": "NO_TRADE", "trade_planning_authority": AUTHORITY,
                  "trade_planning_reason": "NON_ENTRY_CONTEXT", "trade_notional_reserved": 0.,
                  "trade_budget_before_cash_caps": 0., "trade_limit_price_reference": None,
                  "account_snapshot_at": snapshot.get("observed_at"),
                  "current_held_shares": snapshot.get("held_shares", {}).get(symbol),
                  "current_symbol_investment": snapshot.get("symbol_exposure", {}).get(symbol),
                  "cash_available_at_planning": snapshot.get("available_cash"),
                  "broker_price_reference": quote.get("price_reference"),
                  "broker_price_reference_time": quote.get("price_reference_time"),
                  "planning_direction": _planning_direction(row),
                  "projected_trade_quantity": None, "projected_trade_budget": None,
                  "projected_trade_notional": None, "projected_quantity_reason": "NON_ENTRY_CONTEXT"}
        if row["execution_eligible"]:
            result["projected_quantity_reason"] = "ACCOUNT_EVIDENCE_UNAVAILABLE"
            if evidence_ready:
                result["projected_quantity_reason"] = "PRICE_RANGE_UNAVAILABLE"
                if band.get("price_band_status") == "AVAILABLE":
                    high = _money(band["trade_price_high"]).quantize(
                        Decimal(1).scaleb(-policy.price_decimals), rounding=ROUND_CEILING)
                    if high > 0:
                        ceiling = equity * min(
                            _money(policy.maximum_symbol_equity_fraction) * FIXED_HORIZON_WEIGHTS[horizon] / 10,
                            _money(policy.maximum_single_order_equity_fraction))
                        budget = max(Decimal(0), min(ceiling, projection_available, projection_remaining[symbol]))
                        projected = int(budget / high)
                        if projected * high < _money(policy.minimum_order_notional):
                            projected = 0
                        result.update(projected_trade_quantity=projected, projected_trade_budget=float(budget),
                                      projected_trade_notional=float(projected * high),
                                      projected_quantity_reason="AFFORDABLE_HORIZON_ALLOCATION" if projected else "INSUFFICIENT_WHOLE_SHARE_CAPACITY")
            ready = fixed_budget_forecast_readiness(_signal(row), forecast_promoted=row["model_status"] == "PROMOTED", policy=policy)
            code = str(ready["reason"])
            if ready.get("entry_signal"):
                code = "PROVISIONAL_BUY"
                if finite(row.get("symbol_route_fitted_target_rows"), default=0) <= 0:
                    code = "NO_EXACT_ROUTE_FITTED_HISTORY"
                elif not evidence_ready or symbol in blocked_symbols:
                    code = "ACCOUNT_OR_OWNERSHIP_EVIDENCE_UNAVAILABLE"
                elif (symbol, horizon) in active:
                    code = "HORIZON_ALLOCATION_ALREADY_ACTIVE"
                elif (symbol, horizon) in planned_horizons:
                    code = "REQUIRES_PRIOR_EXIT_CONFIRMATION"
                elif not all(finite(quote.get(k)) is not None for k in ("bid", "ask", "price_reference")) or not 0 < quote["bid"] <= quote["ask"]:
                    code = "QUOTE_REFERENCE_UNAVAILABLE"
                elif (quote["ask"] - quote["bid"]) / ((quote["ask"] + quote["bid"]) / 2) > policy.maximum_extended_relative_spread:
                    code = "QUOTE_REFERENCE_SPREAD_TOO_WIDE"
                elif not quote.get("price_reference_time") or not quote_freshness_anchor:
                    code = "QUOTE_REFERENCE_TIME_UNAVAILABLE"
                elif not (utc(quote_freshness_anchor) - pd.Timedelta(minutes=5)
                          <= utc(quote["price_reference_time"]) <= utc(snapshot["observed_at"])):
                    code = "QUOTE_REFERENCE_STALE_OR_FUTURE"
                if code == "PROVISIONAL_BUY":
                    tick = Decimal(1).scaleb(-policy.price_decimals)
                    ask = _money(quote["ask"]).quantize(tick, rounding=ROUND_CEILING)
                    # Estimated planning ranges describe a scenario, never an
                    # acceptable execution-price boundary. This preview sizes
                    # and reserves against its observed ask; live execution
                    # obtains a new quote and actual available cash each time.
                    if (ask / _money(quote["ask"]) - 1) * 10000 > Decimal(str(policy.maximum_limit_offset_bps)):
                        code = "LIMIT_REFERENCE_ROUNDING_EXCEEDS_CAP"
                    else:
                        ceiling = equity * min(_money(policy.maximum_symbol_equity_fraction) * FIXED_HORIZON_WEIGHTS[horizon] / 10,
                                               _money(policy.maximum_single_order_equity_fraction))
                        confidence = min(Decimal("0.5"), max(Decimal(0), 2 * Decimal(str(row["calibrated_probability"])) - 1))
                        budget = ceiling * confidence
                        result["trade_budget_before_cash_caps"] = float(budget)
                        quantity = int(max(Decimal(0), min(budget, available, remaining[symbol])) / ask)
                        batch = utc(row["target_window_start"]).isoformat()
                        if batch_counts.get(batch, 0) >= min(6, policy.maximum_orders_per_wake):
                            code = "COMBINED_BATCH_ORDER_CAP"
                        elif quantity < 1 or quantity * ask < _money(policy.minimum_order_notional):
                            code = "INSUFFICIENT_WHOLE_SHARE_BUDGET"
                        else:
                            notional = quantity * ask
                            result.update(trade_quantity=quantity, trade_action="PROVISIONAL_BUY", trade_notional_reserved=float(notional),
                                          trade_limit_price_reference=float(ask))
                            available -= notional
                            remaining[symbol] -= notional
                            planned_horizons.add((symbol, horizon))
                            batch_counts[batch] = batch_counts.get(batch, 0) + 1
            result["trade_planning_reason"] = code
        result["scheduled_trade_quantity"] = result["trade_quantity"]
        results[str(row["id"])] = result
    return pd.DataFrame([results[str(identifier)] for identifier in data.id])


def _write_json(path: Path, value: Mapping) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, indent=2, sort_keys=True, default=str, allow_nan=False) + "\n", encoding="utf-8")
    temporary.replace(path)


def _plan_working_price_rows(forecasts: pd.DataFrame, snapshot: Mapping,
                             historical_bands: Mapping, price_path: Mapping,
                             *, policy: StockTraderPolicy) -> pd.DataFrame:
    """Keep the scheduled preview and recompute capacity at conditional prices.

    The scheduled preview uses its observed quote, independent of either
    historical or working ranges. The separate per-opportunity capacity and
    displayed working prices use the empirical-median path; neither
    calculation grants execution authority.
    """
    scheduled = plan_trade_rows(forecasts, snapshot, historical_bands, policy=policy)
    forecast_rows = {str(row["id"]): row for row in forecasts.to_dict("records")}
    working_rows = []
    for original in historical_bands["rows"]:
        row = dict(original)
        forecast = forecast_rows[str(row.get("forecast_id", row.get("id")))]
        row.update(historical_price_low=original.get("trade_price_low"),
                   historical_price_high=original.get("trade_price_high"),
                   historical_price_band_status=original.get("price_band_status"),
                   historical_price_band_reason=original.get("price_band_reason"),
                   price_band_reason=original.get("price_band_reason"),
                   trade_price_low=None, trade_price_mid=None, trade_price_high=None,
                   planning_price_point_key=None, planning_price_method=None)
        if forecast["execution_eligible"]:
            start = utc(forecast["target_window_start"])
            local = start.tz_convert("America/Los_Angeles")
            key = f"{forecast['symbol']}|{local.date().isoformat()}|{local:%H:%M}"
            point = price_path["points"].get(key)
            row["planning_price_point_key"] = key
            row["price_band_status"] = "UNAVAILABLE_PLANNING_PRICE_POINT"
            row["price_band_reason"] = "No exact conditional working price for the target entry"
            if point is not None:
                if (point.get("symbol") != forecast["symbol"]
                        or utc(point["timestamp"]) != start
                        or point.get("action_date") != local.date().isoformat()
                        or point.get("clock_local") != local.strftime("%H:%M")):
                    raise ValueError("Planning price point differs from the frozen target entry")
                row.update(price_band_status=point["status"], price_band_reason=point["reason"],
                           planning_price_method=point["method"])
                if point["status"] == "AVAILABLE":
                    low, mid, high = (_money(point[field]) for field in
                                      ("planned_price_low", "planned_price_mid", "planned_price_high"))
                    if not 0 < low <= mid <= high:
                        raise ValueError("Conditional working prices are invalid")
                    row.update(trade_price_low=float(low), trade_price_mid=float(mid), trade_price_high=float(high))
        working_rows.append(row)
    working_bands = {**historical_bands, "rows": working_rows}
    working = plan_trade_rows(forecasts, snapshot, working_bands, policy=policy)
    scheduled["scheduled_trade_price_low"] = scheduled["trade_price_low"]
    scheduled["scheduled_trade_price_high"] = scheduled["trade_price_high"]
    fields = ["trade_price_low", "trade_price_mid", "trade_price_high", "price_band_status", "price_band_reason",
              "historical_price_low", "historical_price_high", "historical_price_band_status", "historical_price_band_reason",
              "planning_price_point_key", "planning_price_method", "projected_trade_quantity", "projected_trade_budget",
              "projected_trade_notional", "projected_quantity_reason"]
    for field in fields:
        scheduled[field] = working[field]
    return scheduled


def _review_refresh_basis(root, prior_run, source, source_hash, action_date, now):
    """Pin an existing informational review; never acquire a new broker state."""
    prior = (root / prior_run).resolve()
    if prior.parent != (root / "ml/gameplan-trade-plan-runs").resolve():
        raise ValueError("Review refresh must use an existing trade-plan run")
    pointer_path = root / "ml/gameplan-trade-plan-latest/run.json"
    pointer_hash = file_checksum(pointer_path)
    pointer = json.loads(pointer_path.read_text(encoding="utf-8"))
    current = pointer.get("current", {})
    receipt_hash = file_checksum(prior / "receipt.json")
    receipt = json.loads((prior / "receipt.json").read_text(encoding="utf-8"))
    manifest = verify_manifest(prior)
    report = json.loads((prior / "report.json").read_text(encoding="utf-8"))
    config = manifest.get("configuration", {})
    if (pointer.get("schema_version") != VERSION or current.get("run_path") != prior.relative_to(root).as_posix()
            or current.get("receipt_sha256") != receipt_hash
            or receipt.get("manifest_sha256") != file_checksum(prior / "manifest.json")
            or receipt.get("run_path") != prior.relative_to(root).as_posix()
            or any(item.get("schema_version") != VERSION or item.get("action_date") != action_date
                   or item.get("source_gameplan_run") != source.relative_to(root).as_posix()
                   or item.get("source_receipt_sha256") != source_hash
                   or item.get("execution_authority") != AUTHORITY
                   or item.get("broker_orders_enabled") is not False or item.get("orders_placed") != 0
                   for item in (receipt, report, config))
            or any(item.get("status") != "COMPLETE" for item in (receipt, report))
            or current.get("source_receipt_sha256") != source_hash or current.get("action_date") != action_date):
        raise ValueError("Review refresh requires the complete current review of these exact frozen forecasts")
    snapshot_path = prior / "account-snapshot.json"
    snapshot = json.loads(snapshot_path.read_text(encoding="utf-8")) if snapshot_path.exists() else None
    if snapshot != report.get("snapshot") or utc(receipt["completed_at"]) > now:
        raise ValueError("Review refresh requires consistent original observation evidence")
    evidence = {"base_run": prior.relative_to(root).as_posix(), "base_receipt_sha256": receipt_hash,
                "refreshed_at": now.isoformat(), "account_snapshot_observed_at": snapshot.get("observed_at") if snapshot else None,
                "source_forecasts_unchanged": True, "purpose": "Derive current directions from the same immutable predictions"}
    inputs = [prior / name for name in ("receipt.json", "manifest.json", "report.json")]
    if snapshot is not None:
        inputs.append(snapshot_path)
    return snapshot, now, evidence, inputs, pointer_hash



def _publish_direction_plan(root, publication, forecasts, *, observed, deadline_at, clock,
                            snapshot=None, producer_mode="DIRECTION_ONLY_SOURCE", producer_id=None,
                            expected_source_receipt_sha256=None, publication_context=None,
                            selection_guard=None):
    """Publish current instructions without loading or estimating future prices."""
    source, config = publication.run_directory, publication.manifest["configuration"]
    day = publication.receipt["action_date"]
    source_hash = expected_source_receipt_sha256 or file_checksum(source / "receipt.json")
    if file_checksum(source / "receipt.json") != source_hash:
        raise ValueError("Frozen Gameplan receipt changed during directional preparation")
    pointer = root / "ml/gameplan-trade-plan-latest/run.json"
    before = pointer.read_bytes() if pointer.exists() else None
    run = create_timestamp_directory(root / "ml/gameplan-trade-plan-runs", timestamp=observed)
    metadata = {"schema_version": VERSION, "action_date": day,
        **current_gameplan_policy_metadata(day),
        "publication_mode": producer_mode, "producer_id": producer_id,
        "holding_policy": "accumulate_bullish_sell_on_bearish_no_scheduled_expiry_v1",
        "cross_horizon_fallback_policy": config.get("cross_horizon_fallback_policy"),
        "symbols": list(config["symbols"]), "source_gameplan_run": source.relative_to(root).as_posix(),
        "source_receipt_sha256": source_hash, "execution_authority": AUTHORITY,
        "orders_placed": 0, "broker_orders_enabled": False}
    context = publication_context or {}
    for key in ("deadline_at", "effective_deadline_at", "deadline_exception", "review_refresh",
                "preparation_mode", "late_action_date", "account_config_sha256"):
        if key in context:
            metadata[key] = context[key]
    from ml.gameplan_probability_target import probability_target_contract, probability_target_metadata
    metadata.update(probability_target_metadata(probability_target_contract(config)))
    rows, ledger = direction_only_rows(forecasts, action_date=day, snapshot=snapshot)
    ledger["cross_horizon_fallback_policy"] = config.get("cross_horizon_fallback_policy")
    rows.to_parquet(run / "trade-plan.parquet", index=False)
    report = {**metadata, "status": "COMPLETE", "observed_at": observed.isoformat(),
        "completed_at": utc(clock()).isoformat(), "deadline_at": metadata.get("deadline_at", deadline_at.isoformat()),
        "forecast_rows": len(rows), "rows_by_symbol": rows.groupby("symbol").size().to_dict(),
        "direction_projection_status": DIRECTION_ONLY_STATUS, "direction_based_projection": ledger,
        "snapshot": snapshot, "snapshot_status": "OBSERVED_CONTEXT_ONLY" if snapshot else "QUANTITIES_DEFERRED_TO_EXECUTION",
        "planning_note": "Directional instructions only. Live execution obtains current cash, eligible holdings, pending orders and bid/ask quotes."}
    for name, value in (("report.json", report), ("direction-ledger.json", ledger)):
        _write_json(run / name, value)
    outputs = ["trade-plan.parquet", "report.json", "direction-ledger.json", "Gameplan.md"]
    if snapshot is not None:
        _write_json(run / "account-snapshot.json", snapshot)
        outputs.append("account-snapshot.json")
    lines = [f"# Gameplan — {day}", "", report["planning_note"], "",
        "BUY at P(up) >= 50.01%; SELL at P(up) <= 49.99%; HOLD strictly between. Use unrounded probabilities.", "",
        "| Company | Horizon | P(up) | Direction | Action | Reason |", "|---|---|---:|---|---|---|"]
    for row in rows.to_dict("records"):
        lines.append(f"| {row['symbol']} | {row['model_group']} | {row['calibrated_probability']:.17g} | {row['direction']} | {row['direction_based_action']} | {row['direction_based_reason']} |")
    (run / "Gameplan.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    write_manifest(run, run_timestamp=observed,
        input_files=[source / "receipt.json", source / "manifest.json", source / "forecasts.parquet"],
        output_files=outputs, configuration=metadata, datastore_root=root)
    verify_manifest(run)
    receipt = {**metadata, "status": "COMPLETE", "run_path": run.relative_to(root).as_posix(),
        "completed_at": report["completed_at"], "forecast_rows": len(rows),
        "direction_projection_status": DIRECTION_ONLY_STATUS, "manifest_sha256": file_checksum(run / "manifest.json")}
    _write_json(run / "receipt.json", receipt)
    document = {"schema_version": VERSION, "current": {
        "run_path": receipt["run_path"], "action_date": day, "source_receipt_sha256": source_hash,
        "receipt_sha256": file_checksum(run / "receipt.json")}}
    committed = (json.dumps(document, indent=2, sort_keys=True, default=str, allow_nan=False) + "\n").replace("\n", os.linesep).encode("utf-8")
    def inputs_unchanged():
        return (utc(clock()) < deadline_at
            and file_checksum(source / "receipt.json") == source_hash
            and (selection_guard is None or selection_guard()))
    try:
        if not inputs_unchanged() or (pointer.read_bytes() if pointer.exists() else None) != before:
            raise ValueError("Direction-only publication source, binding, deadline or selected pointer changed")
        _write_json(pointer, document)
        if pointer.read_bytes() != committed or not inputs_unchanged():
            raise ValueError("Direction-only publication changed during pointer selection")
    except Exception:
        # Restore only our exact selection. A concurrent writer keeps its bytes.
        if pointer.exists() and pointer.read_bytes() == committed:
            if before is None:
                pointer.unlink()
            else:
                rollback = pointer.with_suffix(".rollback.tmp")
                rollback.write_bytes(before)
                if pointer.read_bytes() == committed:
                    rollback.replace(pointer)
                elif rollback.exists():
                    rollback.unlink()
        _write_json(run / "receipt.json", {**receipt, "status": "FAILED",
            "failure_code": "DIRECTIONAL_PUBLICATION_INPUT_CHANGED"})
        raise
    return run


def publish_trade_plan(datastore_root: Path, *, gameplan_run: Path, deadline: object | None = None,
                       snapshot_loader=None, price_loader=None, clock=utc_timestamp,
                       deadline_exception: Path | None = None, refresh_plan: Path | None = None,
                       account_producer_only: bool = False, expected_account_config: str | None = None,
                       late_action_date: str | None = None, research_producer_only: bool = False) -> Path:
    """Publish current directions from one verified scientific Gameplan."""
    from ml.nightly_gameplan import read_gameplan_run
    from ml.stock_trader.independent_signals import _validated_independent_forecasts, verified_promoted_model_groups, late_publication_time
    from ml.gameplan_trade_snapshot import capture_trade_planning_snapshot

    root = Path(datastore_root).resolve()
    if research_producer_only and (account_producer_only or expected_account_config is not None
                                 or snapshot_loader is not None or refresh_plan is not None):
        raise ValueError("Research-only preparation cannot capture an account or combine producer modes")
    binding = None
    if account_producer_only:
        from ml.account_gameplan.config import load_account_config
        binding = load_account_config(root)
        if (binding is None or binding.fingerprint != expected_account_config
                or deadline_exception is not None or refresh_plan is not None or snapshot_loader is not None):
            raise ValueError("Producer-only preparation requires its exact account binding and original deadline, without account snapshots")
    elif expected_account_config is not None:
        raise ValueError("Account producer binding requires explicit producer-only mode")
    publication = read_gameplan_run(root, Path(gameplan_run))
    source = publication.run_directory
    config = publication.manifest["configuration"]
    from ml.gameplan_probability_target import probability_target_contract, probability_target_metadata
    probability_metadata = probability_target_metadata(probability_target_contract(config))
    if config.get("preparation_scope") != "STOCK_ONLY" or config.get("target_contract_version") != "independent-stock-targets-v1":
        raise ValueError("Trade planning requires the explicit independent stock-only Gameplan")
    action_date = str(publication.receipt["action_date"])
    from ml.stock_trader.cross_horizon_fallback import validate_fallback_policy
    fallback_policy = validate_fallback_policy(config.get("cross_horizon_fallback_policy"), action_date)
    fallback_metadata = {"cross_horizon_fallback_policy": fallback_policy} if fallback_policy is not None else {}
    execution_policy_metadata = current_gameplan_policy_metadata(action_date)
    source_receipt_hash = file_checksum(source / "receipt.json")
    expected_deadline = pd.Timestamp(action_date).tz_localize("America/Los_Angeles") + pd.Timedelta(hours=4)
    deadline_at = utc(deadline) if deadline is not None else expected_deadline.tz_convert("UTC")
    observed = utc(clock())
    original_deadline = expected_deadline.tz_convert("UTC")
    if late_action_date is not None:
        close = (expected_deadline + pd.Timedelta(hours=13)).tz_convert("UTC")
        if (late_action_date != action_date
                or config.get("publication_mode") != "LATE_RECOVERY"
                or config.get("late_action_date") != action_date
                or deadline is None or refresh_plan is not None
                or observed < original_deadline
                or (deadline_exception is not None and not Path(deadline_exception).is_file())
                or (deadline_exception is None and observed >= deadline_at)
                or not original_deadline <= deadline_at <= close):
            raise ValueError("Late trade planning requires the matching recovery source and unexpired recovery deadline")
    elif deadline_at != original_deadline:
        # A saved planning-tail continuation retains its original recovery
        # cutoff even when its accepted forecast was published before 04:00.
        # preparation_deadline below verifies the exact source receipt, both
        # original deadlines and the explicit, fixed continuation window.
        from ml.preparation_deadline import RECOVERY_VERSION
        if (deadline_exception is None or not Path(deadline_exception).is_file()
                or json.loads(Path(deadline_exception).read_text(encoding="utf-8")).get("schema_version") != RECOVERY_VERSION):
            raise ValueError("Trade planning deadline differs from the pinned action session")
    from ml.preparation_deadline import preparation_deadline
    refresh_snapshot = refresh_asof = refresh_evidence = refresh_pointer_hash = None
    refresh_inputs = []
    if late_action_date is not None or (deadline_at != original_deadline and refresh_plan is None):
        deadline_at, exception_evidence = preparation_deadline(root, source, deadline_at, observed, deadline_exception)
    elif refresh_plan is None:
        deadline_at, exception_evidence = preparation_deadline(root, source, original_deadline, observed, deadline_exception)
    else:
        if deadline_exception is not None or snapshot_loader is not None:
            raise ValueError("Informational refresh reuses its saved snapshot and cannot extend preparation exceptions")
        session_start = pd.Timestamp(action_date).tz_localize("America/Los_Angeles").tz_convert("UTC")
        deadline_at = session_start + pd.Timedelta(hours=17)
        if not session_start <= observed < deadline_at:
            raise ValueError("Informational refresh must occur on the action date before 17:00 Pacific")
        refresh_snapshot, refresh_asof, refresh_evidence, refresh_inputs, refresh_pointer_hash = _review_refresh_basis(
            root, refresh_plan, source, source_receipt_hash, action_date, observed)
        exception_evidence = None
    if observed >= deadline_at:
        raise ValueError("Trade planning publication deadline has passed")
    symbols = tuple(config["symbols"])
    forecasts = _validated_independent_forecasts(pd.read_parquet(source / "forecasts.parquet"), action_date=action_date, symbols=symbols,
        late_publication_at=late_publication_time(config, publication.receipt))
    intents = pd.read_parquet(source / "option-strategy-intents.parquet")
    if (len(intents) != len(forecasts) or not intents.groupby("symbol").size().eq(24).all()
            or set(intents.id) != set(forecasts.id + ":OPTION")
            or not intents.plan_status.eq("NO_TRADE_STOCK_ONLY").all()
            or not intents.legs_json.isna().all() or not intents.candidate_key.isna().all()
            or not intents.strategy_source_run.isna().all()):
        raise ValueError("Trade planning requires matching non-executable stock-only option placeholders")
    if binding is not None and set(symbols) != set(binding.participants[binding.machine_id]):
        raise ValueError("Producer source universe differs from its account binding")
    # Frozen probabilities already belong to their scientific publication. New
    # trade intent uses the current rule without fitting or re-evaluating models.
    snapshot = refresh_snapshot
    if refresh_plan is None and not research_producer_only and binding is None:
        try:
            observed_snapshot = (snapshot_loader or capture_trade_planning_snapshot)(root, symbols=symbols)
            if observed_snapshot.get("status") == "OBSERVED":
                snapshot = observed_snapshot
        except (OSError, ValueError, RuntimeError):
            # An optional observed account context never blocks the directions.
            # Actual quantities still require the independent executor's broker.
            snapshot = None
    if refresh_pointer_hash is not None and file_checksum(root / "ml/gameplan-trade-plan-latest/run.json") != refresh_pointer_hash:
        raise ValueError("Selected informational review changed before direction refresh")
    return _publish_direction_plan(root, publication, forecasts, observed=observed,
        deadline_at=deadline_at, clock=clock, snapshot=snapshot,
        expected_source_receipt_sha256=source_receipt_hash,
        publication_context={"deadline_at": original_deadline.isoformat(),
            "effective_deadline_at": deadline_at.isoformat(), "deadline_exception": exception_evidence,
            "review_refresh": refresh_evidence,
            **({"late_action_date": late_action_date, "preparation_mode": "LATE_RECOVERY"} if late_action_date else {}),
            **({"account_config_sha256": binding.fingerprint} if binding is not None else {})},
        selection_guard=(lambda: load_account_config(root) == binding) if binding is not None else None,
        producer_mode=RESEARCH_PRODUCER_MODE if research_producer_only else PRODUCER_MODE if binding is not None else "DIRECTION_ONLY_SOURCE",
        producer_id="scout" if research_producer_only else binding.machine_id if binding is not None else None)


def main(argv: list[str] | None = None) -> int:
    from datafetching.parquet_store import DATASTORE_TARGETS, resolve_datastore_dir
    from datafetching.runtime_lock import exclusive_runtime_lock
    parser = argparse.ArgumentParser(description=__doc__)
    datastore = parser.add_mutually_exclusive_group()
    datastore.add_argument("--datastore", type=Path)
    datastore.add_argument("--datastore-target", choices=tuple(DATASTORE_TARGETS), default="pc")
    parser.add_argument("--gameplan-run", required=True, type=Path)
    parser.add_argument("--deadline")
    parser.add_argument("--late-action-date", help="Explicit date-pinned late recovery using its frozen deadline")
    parser.add_argument('--deadline-exception', type=Path)
    parser.add_argument('--refresh-plan', type=Path,
                        help="Derive current directions from the same immutable predictions and optional observed context")
    parser.add_argument("--account-producer-only", action="store_true")
    parser.add_argument("--research-producer-only", action="store_true")
    parser.add_argument("--expected-account-config")
    args = parser.parse_args(argv)
    root = resolve_datastore_dir(root_dir=args.datastore, target=None if args.datastore else args.datastore_target)
    with exclusive_runtime_lock(root / "state/gameplan-trade-planning.lock", process_name="gameplan trade planning"):
        extra = {'deadline_exception':args.deadline_exception} if args.deadline_exception is not None else {}
        if args.late_action_date is not None:
            extra['late_action_date'] = args.late_action_date
        if args.refresh_plan is not None:
            extra['refresh_plan'] = args.refresh_plan
        if args.account_producer_only or args.expected_account_config is not None:
            extra.update(account_producer_only=args.account_producer_only, expected_account_config=args.expected_account_config)
        if args.research_producer_only:
            extra["research_producer_only"] = True
        run = publish_trade_plan(root, gameplan_run=args.gameplan_run, deadline=args.deadline, **extra)
    print(json.dumps({"status": "COMPLETE", "run_path": str(run), "review_path": str(run / "Gameplan.md"), "orders_placed": 0}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
