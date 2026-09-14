"""Account-aware nightly review artifacts; no order or trading-control authority.

Frozen forecasts remain immutable. This separate source-bound publication adds
cash-only provisional quantities and empirical entry-price planning ranges.
The existing live worker still revalidates ownership, cash and its ask LIMIT.
"""
from __future__ import annotations

import argparse
import json
from dataclasses import asdict
from decimal import Decimal, ROUND_CEILING
from pathlib import Path
from typing import Mapping

import pandas as pd

from ml.artifacts import create_timestamp_directory, file_checksum, verify_manifest, write_manifest, utc_timestamp
from ml.stock_trader.contracts import PredictionSignal, StockTraderPolicy, finite, utc
from ml.stock_trader.fixed_horizon_budget import FIXED_HORIZON_WEIGHTS, fixed_budget_forecast_readiness
from ml.stock_direction_policy import BULLISH_PROBABILITY, BEARISH_PROBABILITY, STOCK_DIRECTION_POLICY_VERSION, stock_direction


VERSION = "cash-aware-gameplan-trade-planning-v4"
AUTHORITY = "REVIEW_ONLY_REVALIDATE_AT_ENTRY"


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
                  "planning_direction": stock_direction(row["calibrated_probability"]),
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


def publish_trade_plan(datastore_root: Path, *, gameplan_run: Path, deadline: object | None = None,
                       snapshot_loader=None, price_loader=None, clock=utc_timestamp,
                       deadline_exception: Path | None = None) -> Path:
    """Publish a separate immutable account/price review for one verified Gameplan."""
    from ml.nightly_gameplan import read_gameplan_run
    from ml.stock_trader.independent_signals import _validated_independent_forecasts, verified_promoted_model_groups
    from ml.stock_target_prices import load_stock_target_prices
    from ml.gameplan_price_bands import build_entry_price_bands, build_planning_price_path
    from ml.gameplan_cash_ledger import project_direction_trades, UnavailablePlanningPricePath
    from ml.gameplan_trade_snapshot import capture_trade_planning_snapshot
    from ml.gameplan_trade_review import render_trade_review
    from ml.gameplan_actuals_review import previous_action_date

    root = Path(datastore_root).resolve()
    publication = read_gameplan_run(root, Path(gameplan_run))
    source = publication.run_directory
    config = publication.manifest["configuration"]
    if config.get("preparation_scope") != "STOCK_ONLY" or config.get("target_contract_version") != "independent-stock-targets-v1":
        raise ValueError("Trade planning requires the explicit independent stock-only Gameplan")
    action_date = str(publication.receipt["action_date"])
    source_receipt_hash = file_checksum(source / "receipt.json")
    expected_deadline = pd.Timestamp(action_date).tz_localize("America/Los_Angeles") + pd.Timedelta(hours=4)
    deadline_at = utc(deadline) if deadline is not None else expected_deadline.tz_convert("UTC")
    if deadline_at != expected_deadline.tz_convert("UTC"):
        raise ValueError("Trade planning deadline differs from the pinned action session")
    observed = utc(clock())
    from ml.preparation_deadline import preparation_deadline
    original_deadline = deadline_at
    deadline_at, exception_evidence = preparation_deadline(root, source, original_deadline, observed, deadline_exception)
    if observed >= deadline_at:
        raise ValueError("Trade planning publication deadline has passed")
    symbols = tuple(config["symbols"])
    forecasts = _validated_independent_forecasts(pd.read_parquet(source / "forecasts.parquet"), action_date=action_date, symbols=symbols)
    intents = pd.read_parquet(source / "option-strategy-intents.parquet")
    if (len(intents) != len(forecasts) or not intents.groupby("symbol").size().eq(24).all()
            or set(intents.id) != set(forecasts.id + ":OPTION")
            or not intents.plan_status.eq("NO_TRADE_STOCK_ONLY").all()
            or not intents.legs_json.isna().all() or not intents.candidate_key.isna().all()
            or not intents.strategy_source_run.isna().all()):
        raise ValueError("Trade planning requires matching non-executable stock-only option placeholders")
    promoted = verified_promoted_model_groups(publication)
    if any(row.model_group not in promoted for row in forecasts.loc[forecasts.model_status.eq("PROMOTED")].itertuples()):
        raise ValueError("A forecast claims promotion without verified model authority")
    run = create_timestamp_directory(root / "ml/gameplan-trade-plan-runs", timestamp=observed)
    report = {"schema_version": VERSION, "observed_at": observed.isoformat(), "action_date": action_date,
              "source_gameplan_run": source.relative_to(root).as_posix(), "source_receipt_sha256": source_receipt_hash,
              "deadline_at": original_deadline.isoformat(), "effective_deadline_at":deadline_at.isoformat(),
              "deadline_exception":exception_evidence, "execution_authority": AUTHORITY,
              "orders_placed": 0, "broker_orders_enabled": False, "status": "RUNNING"}
    prior_action_date = previous_action_date(action_date)
    report["previous_session_results_date"] = prior_action_date
    report["previous_session_results_path"] = (root / "ml/gameplan-actuals-review-by-date" /
                                                prior_action_date / "Gameplan-results.md").as_posix()
    _write_json(run / "report.json", report)
    phase = "ACCOUNT_SNAPSHOT"
    try:
        snapshot = (snapshot_loader or capture_trade_planning_snapshot)(root, symbols=symbols)
        _write_json(run / "account-snapshot.json", snapshot)
        if (snapshot.get("status") != "OBSERVED" or snapshot.get("cash_status") != "CASH_ONLY_BOUNDED"
                or snapshot.get("available_cash") is None):
            raise ValueError("ACCOUNT_SNAPSHOT_UNAVAILABLE")
        if snapshot.get("ownership", {}).get("safe_for_planning") is not True:
            raise ValueError("OWNERSHIP_SNAPSHOT_UNAVAILABLE")
        phase = "VERIFIED_PRICE_HISTORY"
        prices, price_files, price_report = (price_loader or load_stock_target_prices)(
            root, symbols=symbols, source_contract=config["target_price_source_contract"])
        phase = "PRICE_BANDS_AND_BUDGETS"
        band_asof = utc(clock())
        bands = build_entry_price_bands(prices, forecasts, observed_at=band_asof,
                                        allow_reference_forward_fill=True)
        price_path = build_planning_price_path(prices, forecasts, observed_at=band_asof, entry_bands=bands,
                                                allow_reference_forward_fill=True)
        completion = bands["reference_completion"]
        _write_json(run / "planning-reference-completion.json", completion)
        synthetic = pd.DataFrame(completion["synthetic_bars"])
        if synthetic.empty:
            synthetic = pd.DataFrame(columns=["symbol", "timestamp", "open", "high", "low", "close",
                                              "volume", "is_synthetic", "reason", "original_observed_at"])
        synthetic.to_parquet(run / "synthetic-reference-bars.parquet", index=False)
        policy = StockTraderPolicy()
        rows = _plan_working_price_rows(forecasts, snapshot, bands, price_path, policy=policy)
        phase = "DIRECTION_BASED_CASH_AND_SHARE_PROJECTION"
        try:
            rows, direction_projection = project_direction_trades(rows, snapshot, price_path, policy=policy)
        except UnavailablePlanningPricePath as unavailable:
            # Complete the informational report without manufacturing prices,
            # fills, ending cash or ending holdings. Other validation errors
            # continue to fail the publication.
            direction_projection = {
                "status":"UNAVAILABLE_PRICE_REFERENCES", "unavailable_points":unavailable.points,
                "events":[], "hourly":[], "ending_positions":{}, "summary":{},
                "orders_placed":0, "broker_orders_enabled":False,
                "reason":"Required observed price references or historical pairs are missing; no shared cash projection was calculated.",
            }
        rows.to_parquet(run / "trade-plan.parquet", index=False)
        _write_json(run / "price-bands.json", bands)
        _write_json(run / "planning-price-path.json", price_path)
        _write_json(run / "direction-ledger.json", direction_projection)
        model_reports = json.loads((source / "model-reports.json").read_text(encoding="utf-8"))
        report.update(status="COMPLETE", completed_at=utc(clock()).isoformat(), forecast_rows=len(rows),
                      rows_by_symbol=rows.groupby("symbol").size().to_dict(), option_intent_rows=len(intents),
                      provisional_buy_rows=int(rows.trade_quantity.gt(0).sum()),
                      planned_notional=float(rows.trade_notional_reserved.sum()),
                      quantity_sum=int(rows.trade_quantity.sum()),
                      projected_quantity_rows=int(rows.projected_trade_quantity.gt(0).sum()),
                      projected_quantity_semantics="Per-opportunity affordable horizon allocation; alternatives are not added as simultaneous orders",
                      direction_policy_version=STOCK_DIRECTION_POLICY_VERSION,
                      direction_up_threshold=BULLISH_PROBABILITY, direction_down_threshold=BEARISH_PROBABILITY,
                      price_band_status_counts=rows.price_band_status.value_counts().to_dict(),
                      trade_reason_counts=rows.trade_planning_reason.value_counts().to_dict(),
                      snapshot=snapshot, sizing_policy=asdict(policy),
                      direction_based_projection=direction_projection,
                      direction_projection_status=direction_projection.get("status", "AVAILABLE"),
                      opra_history=config.get("opra_history", {}),
                      price_band_policy={k: v for k, v in bands.items() if k not in {"rows", "statistics", "reference_completion"}},
                      planning_price_path={k: v for k, v in price_path.items() if k not in {"points", "reference_completion"}},
                      reference_completion={k: v for k, v in completion.items() if k != "synthetic_bars"},
                      target_price_source_contract=config["target_price_source_contract"],
                      source_price_inventory=price_report,
                      limitations=["Review projections only; the existing live worker revalidates all controls and capital.",
                                   "Price and cash ranges are estimates only. Live orders use the current tradable quote, actual available cash and holdings even when those values are outside the estimates.",
                                   "A bounded synthetic reference may carry the last actual close through at most 15 trailing minutes in a verified source window. Zero volume is an assumed no-trade interval, not a newly observed exchange candle.",
                                   "The direction-based cash/share projection depends on its recorded sale and expiry fills; projected proceeds are not actual spendable broker cash.",
                                   "The separate scheduled-entry preview uses current cash only and requires prior exit confirmation for later entries in a planned horizon."])
        # Optional research assessments add context, never alter the pinned
        # forecast or supply authority for a proposed quantity.
        for pointer, folder, report_name, field in (
            ("ml/gameplan-evaluation-latest/run.json", "ml/gameplan-evaluation-runs", "summary.json", "evaluation_summary"),
            ("ml/stock-trader-model-latest/run.json", "ml/stock-trader-model-runs", "training-report.json", "enrichment_summary"),
        ):
            path = root / pointer
            if path.is_file():
                try:
                    payload = json.loads(path.read_text(encoding="utf-8"))
                    ref = payload.get("current", payload)
                    related = (root / str(ref.get("run_path", ref.get("path", "")))).resolve()
                    if related.parent == (root / folder).resolve():
                        verify_manifest(related)
                        detail = json.loads((related / report_name).read_text(encoding="utf-8"))
                        if field != "enrichment_summary" or detail.get("source_gameplan_run") == source.relative_to(root).as_posix():
                            report[field] = detail
                except (OSError, ValueError, KeyError, RuntimeError):
                    report[field + "_status"] = "UNAVAILABLE_OPTIONAL_CONTEXT"
        phase = "REVIEW_AND_PUBLICATION"
        _write_json(run / "report.json", report)
        (run / "Gameplan.md").write_text(render_trade_review(rows, report, model_reports, source_gameplan=source.as_posix()), encoding="utf-8")
        if utc(clock()) >= deadline_at:
            raise ValueError("TRADE_PLANNING_DEADLINE_PASSED")
        if file_checksum(source / "receipt.json") != source_receipt_hash:
            raise ValueError("PINNED_GAMEPLAN_RECEIPT_CHANGED")
        outputs = ["trade-plan.parquet", "account-snapshot.json", "price-bands.json", "planning-price-path.json",
                   "planning-reference-completion.json", "synthetic-reference-bars.parquet",
                   "direction-ledger.json", "report.json", "Gameplan.md"]
        write_manifest(run, run_timestamp=observed,
                       input_files=[source / "receipt.json", source / "manifest.json", source / "forecasts.parquet", *price_files],
                       output_files=outputs, configuration={"schema_version": VERSION, "action_date": action_date,
                       "source_gameplan_run": report["source_gameplan_run"], "source_receipt_sha256": source_receipt_hash,
                       "reference_completion_contract": completion["contract_version"],
                       "allow_reference_forward_fill": True,
                       "execution_authority": AUTHORITY, "broker_orders_enabled": False, "orders_placed": 0}, datastore_root=root)
        verify_manifest(run)
        if utc(clock()) >= deadline_at:
            raise ValueError("TRADE_PLANNING_DEADLINE_PASSED")
        terminal = {"schema_version": VERSION, "status": "COMPLETE", "run_path": run.relative_to(root).as_posix(),
                    "action_date": action_date, "source_gameplan_run": report["source_gameplan_run"],
                    "source_receipt_sha256": source_receipt_hash, "manifest_sha256": file_checksum(run / "manifest.json"),
                    "forecast_rows": len(rows), "orders_placed": 0, "broker_orders_enabled": False,
                    "execution_authority": AUTHORITY, "completed_at": utc(clock()).isoformat()}
        _write_json(run / "receipt.json", terminal)
        if utc(clock()) >= deadline_at:
            raise ValueError("TRADE_PLANNING_DEADLINE_PASSED")
        _write_json(root / "ml/gameplan-trade-plan-latest/run.json", {"schema_version": VERSION, "current": {
            "run_path": terminal["run_path"], "action_date": action_date,
            "source_receipt_sha256": source_receipt_hash, "receipt_sha256": file_checksum(run / "receipt.json")}})
        return run
    except Exception as exc:
        # Do not serialize broker exception text, credentials or identifiers.
        safe_codes = {"ACCOUNT_SNAPSHOT_UNAVAILABLE", "OWNERSHIP_SNAPSHOT_UNAVAILABLE",
                      "TRADE_PLANNING_DEADLINE_PASSED", "PINNED_GAMEPLAN_RECEIPT_CHANGED"}
        code = str(exc) if str(exc) in safe_codes else "TRADE_PLANNING_VALIDATION_FAILED"
        report.update(status="FAILED", failure_type=type(exc).__name__, failure_phase=phase,
                      failure_code=code, completed_at=utc(clock()).isoformat())
        _write_json(run / "report.json", report)
        _write_json(run / "receipt.json", {"schema_version": VERSION, "status": "FAILED", "failure_type": type(exc).__name__,
                    "failure_code": code, "failure_phase": phase,
                    "report_sha256": file_checksum(run / "report.json"), "orders_placed": 0, "broker_orders_enabled": False})
        raise RuntimeError(f"Trade planning failed ({type(exc).__name__}); review {run / 'report.json'}") from None


def main(argv: list[str] | None = None) -> int:
    from datafetching.parquet_store import DATASTORE_TARGETS, resolve_datastore_dir
    from datafetching.runtime_lock import exclusive_runtime_lock
    parser = argparse.ArgumentParser(description=__doc__)
    datastore = parser.add_mutually_exclusive_group()
    datastore.add_argument("--datastore", type=Path)
    datastore.add_argument("--datastore-target", choices=tuple(DATASTORE_TARGETS), default="pc")
    parser.add_argument("--gameplan-run", required=True, type=Path)
    parser.add_argument("--deadline")
    parser.add_argument('--deadline-exception', type=Path)
    args = parser.parse_args(argv)
    root = resolve_datastore_dir(root_dir=args.datastore, target=None if args.datastore else args.datastore_target)
    with exclusive_runtime_lock(root / "state/gameplan-trade-planning.lock", process_name="gameplan trade planning"):
        extra = {'deadline_exception':args.deadline_exception} if args.deadline_exception is not None else {}
        run = publish_trade_plan(root, gameplan_run=args.gameplan_run, deadline=args.deadline, **extra)
    print(json.dumps({"status": "COMPLETE", "run_path": str(run), "review_path": str(run / "Gameplan.md"), "orders_placed": 0}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
