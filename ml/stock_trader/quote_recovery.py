"""Explicit, once-only recovery of an unsubmitted one-hour quote skip.

The operator names one native run and symbol. Forecast identity and expiry stay
unchanged; the normal hourly entry claim is never removed or replayed.
"""
from __future__ import annotations

import json
import os
import re
from dataclasses import replace
from datetime import timedelta
from pathlib import Path

from ml.stock_trader.contracts import canonical_sha256, utc
from ml.stock_trader.independent_signals import load_current_independent_gameplan_signals
from ml.stock_trader.publication import read_decision_run

_REASONS = {"CURRENT_QUOTE_TOO_OLD", "STOCK_SPREAD_TOO_WIDE", "USABLE_QUOTE_UNAVAILABLE", "REALTIME_QUOTE_UNAVAILABLE"}


def load_quote_recovery(root: Path, run_id: str, symbol: str, *, as_of):
    if not re.fullmatch(r"\d{8}T\d{6}\.\d{6}Z", run_id):
        raise ValueError("Quote recovery requires an exact native decision run ID")
    run = root / "ml/stock-trader-decision-runs" / run_id
    document, receipt = read_decision_run(root, run)
    now = utc(as_of)
    original_time = utc(receipt["decided_at"])
    if original_time.tz_convert("America/Los_Angeles").date() != now.tz_convert("America/Los_Angeles").date():
        raise ValueError("Quote recovery is limited to the same action date")
    candidates = [d for d in document["decisions"] if d["symbol"] == symbol
                  and d["decision_reason_code"] in _REASONS and d["quantity"] == 0
                  and d.get("order_payload") is None and d["prediction"]["primary_horizon"] == "1h"]
    if len(candidates) != 1:
        raise ValueError("Quote recovery requires one unsubmitted one-hour quote skip for that symbol")
    decision = candidates[0]
    if (root / "ml/stock-trader-execution-events" / decision["decision_id"]).exists():
        raise ValueError("The original decision already has execution evidence")
    signals, sources = load_current_independent_gameplan_signals(
        root, as_of=original_time, require_promoted_model_reports=True)
    key = (symbol, "1h")
    signal = signals.get(key)
    prediction = decision["prediction"]
    if (signal is None or signal.prediction_id != prediction["prediction_id"]
            or signal.source_fingerprint != prediction["source_fingerprint"]
            or utc(signal.target_window_end) != utc(prediction["target_window_end"])):
        raise ValueError("Quote recovery differs from the original frozen Gameplan signal")
    end = utc(signal.target_window_end)
    if not utc(signal.target_window_start) <= now < end - timedelta(seconds=5):
        raise ValueError("The original one-hour holding target has already ended")
    metadata = {"source_run": run_id, "source_decision_id": decision["decision_id"],
                "forecast_id": signal.prediction_id, "symbol": symbol, "horizon": "1h",
                "target_start": signal.target_window_start, "target_end": signal.target_window_end,
                "original_actionable_until": signal.actionable_until, "resume_deadline": end.isoformat()}
    return {key: replace(signal, actionable_until=end.isoformat())}, (*sources, run / "receipt.json", run / "decisions.json"), metadata


def _claim_path(root, forecast_id):
    return root / "state/independent-stock-trader/quote-recovery-slots" / (canonical_sha256(["quote-recovery-v1", forecast_id]) + ".json")


def claim_quote_recovery(root, metadata, *, as_of):
    path = _claim_path(root, metadata["forecast_id"])
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        return False
    with os.fdopen(fd, "w", encoding="utf-8") as stream:
        json.dump({**metadata, "claimed_at": utc(as_of).isoformat()}, stream)
        stream.flush()
        os.fsync(stream.fileno())
    return True


def recovered_entry_deadline(root, allocation, default):
    path = _claim_path(root, allocation.forecast_id)
    if not path.exists():
        return default
    evidence = json.loads(path.read_text(encoding="utf-8"))
    if (evidence["forecast_id"] != allocation.forecast_id or evidence["symbol"] != allocation.symbol
            or utc(evidence["target_start"]) != utc(allocation.target_start)
            or utc(evidence["target_end"]) != utc(allocation.target_end)):
        raise ValueError("Quote recovery does not match the tracked allocation")
    return max(default, min(utc(evidence["resume_deadline"]), utc(allocation.target_end)))
