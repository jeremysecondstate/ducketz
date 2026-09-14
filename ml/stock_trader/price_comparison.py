"""Observational planned-price versus broker-midpoint records; never a gate."""
from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pandas as pd

from ml.stock_trader.contracts import finite


def quote_comparison(decision: dict, planned: dict | None = None) -> dict:
    quote = decision.get("quote") or {}
    bid, ask = finite(quote.get("bid")), finite(quote.get("ask"))
    midpoint = (bid + ask) / 2 if bid is not None and ask is not None and 0 < bid <= ask else None
    planned = planned or {}
    price = finite(planned.get("planned_price"))
    return {**planned, "planned_price": price, "live_midpoint": midpoint,
            "live_bid": bid, "live_ask": ask,
            "quote_received_at": quote.get("received_at"), "quote_updated_at": quote.get("observed_at"),
            "quote_realtime": quote.get("realtime"), "order_limit": decision.get("limit_price"),
            "order_quantity": decision.get("quantity"), "action": decision.get("action"),
            "midpoint_minus_planned": midpoint - price if midpoint is not None and price is not None else None,
            "comparison_affects_execution": False}


def attach_price_comparisons(root: Path, decisions):
    planned, status = {}, "AVAILABLE"
    try:
        pointer = json.loads((root / "ml/gameplan-trade-plan-latest/run.json").read_text(encoding="utf-8"))
        run = (root / pointer["current"]["run_path"]).resolve()
        if run.parent != (root / "ml/gameplan-trade-plan-runs").resolve():
            raise ValueError("Planning comparison path is outside its saved directory")
        frame = pd.read_parquet(run / "trade-plan.parquet")
        planned = {(str(row["id"]), False): {"planned_price": finite(row.get("trade_price_mid")),
            "planned_price_low": finite(row.get("trade_price_low")), "planned_price_high": finite(row.get("trade_price_high")),
            "planned_quantity": finite(row.get("direction_based_trade_quantity")),
            "planned_cash": finite(row.get("cash_available_at_planning")), "planned_run": run.name}
            for row in frame.to_dict("records")}
        exit_path = run / "direction-ledger.json"
        if exit_path.is_file():
            for event in json.loads(exit_path.read_text(encoding="utf-8")).get("events", []):
                if event.get("reason") == "HORIZON_EXIT":
                    planned[(str(event["forecast_id"]), True)] = {
                        "planned_price": finite(event.get("price_base")),
                        "planned_price_low": finite(event.get("price_low")), "planned_price_high": finite(event.get("price_high")),
                        "planned_quantity": finite(event.get("quantity")), "planned_run": run.name}
    except Exception as exc:
        # An unavailable planning comparison cannot prevent broker execution.
        status = "UNAVAILABLE: " + type(exc).__name__
    return tuple(replace(decision, enrichment={**decision.enrichment,
        "price_comparison": {**quote_comparison(decision.to_dict(), planned.get((
            decision.prediction.get("parent_forecast_id", decision.prediction.get("prediction_id")),
            decision.prediction.get("position_purpose") == "EXIT"))),
                             "planning_comparison_status": status}}) for decision in decisions)
