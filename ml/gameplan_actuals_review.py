"""Compare frozen Gameplans with observed prices after successor preparation."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Mapping

import exchange_calendars as xcals
import numpy as np
import pandas as pd

from ml.artifacts import create_timestamp_directory, file_checksum, utc_timestamp, verify_manifest, write_manifest
from ml.gameplan_price_bands import (
    SPARSE_PLANNING_PRICE_PATH_CONTRACT, _aware_timestamp, _observation, _source_identity,
)
from ml.independent_stock_targets import STOCK_TARGET_BOUNDARY_TOLERANCE


VERSION = "gameplan-actuals-review-v1"
TIMEZONE = "America/Los_Angeles"
RUNS = "ml/gameplan-actuals-review-runs"
IDENTITY_COLUMNS = ("id", "symbol", "route", "model_group", "model_status", "target_role",
                    "target_window_start", "target_window_end", "calibrated_probability",
                    "direction", "target_price_source_contract")


def _json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _write_json(path: Path, value: Mapping) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(value, indent=2, sort_keys=True, default=str, allow_nan=False) + "\n", encoding="utf-8")
    temporary.replace(path)


def _clock(day: str, hour: int) -> pd.Timestamp:
    return pd.Timestamp(day).tz_localize(TIMEZONE).replace(hour=hour).tz_convert("UTC")


def previous_action_date(successor_date: str) -> str:
    calendar = xcals.get_calendar("XNYS", start=pd.Timestamp(successor_date) - pd.Timedelta(days=14),
                                  end=pd.Timestamp(successor_date) + pd.Timedelta(days=7))
    return pd.Timestamp(calendar.previous_session(pd.Timestamp(successor_date))).date().isoformat()


def _number(value) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if np.isfinite(number) else None


def _observed_prices(prices: pd.DataFrame, forecasts: pd.DataFrame, observed_at: object) -> dict:
    _source_identity(prices, forecasts)
    now = _aware_timestamp(observed_at, "observed_at")
    bars = prices.loc[:, ["symbol", "timestamp", "open", "close"]].copy()
    bars["timestamp"] = pd.to_datetime(bars.timestamp, utc=True, errors="raise")
    bars["symbol"] = bars.symbol.astype(str).str.upper()
    # The close of a minute is not available until that minute has completed.
    bars = bars.loc[bars.timestamp.add(pd.Timedelta(minutes=1)).le(now)]
    bars = bars.drop_duplicates(["symbol", "timestamp", "open", "close"])
    if bars.duplicated(["symbol", "timestamp"]).any():
        raise ValueError("Actual price evidence contains conflicting minute observations")
    for field in ("open", "close"):
        bars[field] = pd.to_numeric(bars[field], errors="coerce")
    return {symbol: frame.sort_values("timestamp", kind="stable")
            for symbol, frame in bars.groupby("symbol", sort=False)}


def _source_window_coverage(inventory: Mapping, symbol: str, start: pd.Timestamp, end: pd.Timestamp) -> str:
    """Describe requested archive coverage, never infer that missing bars mean no trades."""
    partitions = inventory.get("partitions")
    verified = inventory.get("native_archive_partitions_verified")
    if (not isinstance(partitions, list) or not isinstance(verified, int) or isinstance(verified, bool)
            or verified <= 0 or verified != len(partitions) or inventory.get("schema") != "ohlcv-1m"):
        return "UNKNOWN"

    def bound(value):
        result = pd.Timestamp(value)
        if pd.isna(result):
            raise ValueError("Missing source interval bound")
        if result.tzinfo is None:
            if not isinstance(value, str) or value != result.date().isoformat():
                raise ValueError("Source interval wall time has no timezone")
            result = result.tz_localize("UTC")
        return result.tz_convert("UTC")

    intervals = []
    for partition in partitions:
        if (not isinstance(partition, Mapping) or not isinstance(partition.get("symbol"), str)
                or not partition["symbol"] or not isinstance(partition.get("manifest_path"), str)
                or not partition["manifest_path"]):
            return "UNKNOWN"
        try:
            if partition.get("start") is not None and partition.get("end") is not None:
                left, right = (bound(partition[field]) for field in ("start", "end"))
            elif partition.get("delivery_mode") == "live-intraday-replay" and partition.get("session"):
                left, right = _clock(partition["session"], 4), _clock(partition["session"], 17)
            else:
                return "UNKNOWN"
        except (TypeError, ValueError, OverflowError):
            return "UNKNOWN"
        if pd.isna(left) or pd.isna(right) or right <= left:
            return "UNKNOWN"
        if partition["symbol"] == symbol:
            intervals.append((left, right))
    covered_to = start
    for left, right in sorted(intervals):
        if left > covered_to:
            break
        if right > covered_to:
            covered_to = right
        if covered_to >= end:
            return "VERIFIED_COMPLETE"
    return "INCOMPLETE"


def _boundary_evidence(bars: pd.DataFrame, boundary: pd.Timestamp, *, close: bool,
                       now: pd.Timestamp, inventory: Mapping, symbol: str) -> dict:
    """Explain the existing selector without changing its five-minute eligibility."""
    minute = pd.Timedelta(minutes=1)
    # Open at boundary+5m needs that entire minute; close at boundary-5m
    # belongs to the minute beginning boundary-6m. Archive ends are exclusive.
    start = boundary - STOCK_TARGET_BOUNDARY_TOLERANCE - minute if close else boundary
    end = boundary if close else boundary + STOCK_TARGET_BOUNDARY_TOLERANCE + minute
    evidence = {"status": "PENDING_MATURITY" if boundary > now else "NO_OBSERVATION",
                "candidate_price": None, "candidate_observed_at": None, "gap_seconds": None,
                "source_coverage": _source_window_coverage(inventory, symbol, start, end),
                "required_source_start": start.isoformat(), "required_source_end": end.isoformat()}
    if boundary > now or bars.empty:
        return evidence
    timestamps = pd.DatetimeIndex(bars.timestamp)
    position = (timestamps.searchsorted(boundary - minute, side="right") - 1
                if close else timestamps.searchsorted(boundary, side="left"))
    if position < 0 or position >= len(bars):
        return evidence
    candidate = bars.iloc[position]
    observed = candidate.timestamp + (minute if close else pd.Timedelta(0))
    gap = boundary - observed if close else observed - boundary
    price = _number(candidate["close" if close else "open"])
    evidence.update(candidate_price=price, candidate_observed_at=observed.isoformat(), gap_seconds=gap.total_seconds(),
                    status="INVALID_OBSERVATION" if price is None or price <= 0 else
                    "OUTSIDE_TOLERANCE" if gap > STOCK_TARGET_BOUNDARY_TOLERANCE else "OBSERVED")
    return evidence


def _evidence_columns(prefix: str, evidence: Mapping) -> dict:
    return {f"{prefix}_{key}": value for key, value in evidence.items()}


def _match_trade_rows(forecasts: pd.DataFrame, trades: pd.DataFrame) -> None:
    if trades.id.duplicated().any() or set(trades.id) != set(forecasts.id):
        raise ValueError("Saved trade plan differs from the frozen forecast identities")
    columns = [column for column in IDENTITY_COLUMNS if column in forecasts]
    if not set(columns).issubset(trades):
        raise ValueError("Saved trade plan is missing frozen forecast fields")
    try:
        pd.testing.assert_frame_equal(forecasts[columns].sort_values("id").reset_index(drop=True),
                                      trades[columns].sort_values("id").reset_index(drop=True),
                                      check_dtype=False, check_exact=True)
    except AssertionError as exc:
        raise ValueError("Saved trade plan changed a frozen forecast") from exc


def compare_forecasts(forecasts: pd.DataFrame, prices: pd.DataFrame, *, observed_at: object,
                      trade_rows: pd.DataFrame | None = None) -> pd.DataFrame:
    """Retain every frozen row, admitting only genuine, mature endpoint prices."""
    now = _aware_timestamp(observed_at, "observed_at")
    if forecasts.empty or forecasts.id.duplicated().any():
        raise ValueError("Actuals review requires unique frozen forecasts")
    by_symbol = _observed_prices(prices, forecasts, now)
    inventory = prices.attrs.get("stock_price_source", {})
    if trade_rows is not None:
        _match_trade_rows(forecasts, trade_rows)
    planned = {} if trade_rows is None else {row["id"]: row for row in trade_rows.to_dict("records")}
    rows = []
    for forecast in forecasts.to_dict("records"):
        row = {**planned.get(forecast["id"], {}), **forecast}
        start = _aware_timestamp(row["target_window_start"], "target_window_start")
        end = _aware_timestamp(row["target_window_end"], "target_window_end")
        if end <= start:
            raise ValueError("Frozen forecast has an invalid target window")
        bars = by_symbol.get(str(row["symbol"]).upper(), prices.iloc[:0])
        gap = row["target_role"] == "OPENING_GAP_RESEARCH"
        entry = _observation(bars, start, close=gap) if start <= now else None
        finish = _observation(bars, end, close=not gap) if end <= now else None
        for prefix, boundary, is_close in (("actual_start", start, gap), ("actual_end", end, not gap)):
            row.update(_evidence_columns(prefix, _boundary_evidence(
                bars, boundary, close=is_close, now=now, inventory=inventory, symbol=str(row["symbol"]).upper())))
        mature = end <= now
        scored = bool(mature and entry and finish and pd.Timestamp(entry[1]) < pd.Timestamp(finish[1]))
        change = finish[0] / entry[0] - 1 if scored else None
        direction = str(row.get("direction", "NO_EDGE")).upper()
        correct = (change > 0 if direction == "BULLISH" else change < 0) if scored and direction in {"BULLISH", "BEARISH"} else None
        probability = _number(row["calibrated_probability"])
        if probability is None or not 0 <= probability <= 1:
            raise ValueError("Frozen forecast has an invalid probability")
        cost = _number(row.get("assumed_round_trip_cost"))
        cost = 0.001 if cost is None else cost
        model_target = int(change > cost) if scored else None
        low, mid, high = (_number(row.get(f"trade_price_{field}")) for field in ("low", "mid", "high"))
        comparable = bool(entry and mid is not None and mid > 0)
        row.update(actual_start_price=entry[0] if entry else None,
                   actual_start_observed_at=entry[1] if entry else None,
                   actual_end_price=finish[0] if finish else None,
                   actual_end_observed_at=finish[1] if finish else None,
                   actual_return=change, direction_correct=correct,
                   direction_result=("Correct" if correct else "Incorrect") if correct is not None else "No directional call" if scored else "Pending",
                   actuals_status="EVALUATED" if scored else "MATURE_AWAITING_DATA" if mature else "PENDING_MATURITY",
                   entry_price_error=entry[0] - mid if comparable else None,
                   entry_price_error_fraction=entry[0] / mid - 1 if comparable else None,
                   entry_price_in_range=bool(low <= entry[0] <= high) if entry and low is not None and high is not None else None,
                   model_observed_target=model_target,
                   model_brier_score=(probability - model_target) ** 2 if scored else None)
        rows.append(row)
    return pd.DataFrame(rows)


def compare_price_points(forecasts: pd.DataFrame, prices: pd.DataFrame, *, action_date: str,
                         observed_at: object, planning_path: Mapping | None = None) -> pd.DataFrame:
    """Compare the saved 04:00-17:00 working estimates with the same market clocks."""
    now = _aware_timestamp(observed_at, "observed_at")
    contract, dataset = _source_identity(prices, forecasts)
    by_symbol = _observed_prices(prices, forecasts, now)
    inventory = prices.attrs.get("stock_price_source", {})
    if planning_path is not None and (planning_path.get("price_source_contract") != contract
                                      or planning_path.get("price_dataset") != dataset
                                      or _aware_timestamp(planning_path["observed_at"], "planning observation") >= _clock(action_date, 4)):
        raise ValueError("Saved price path must match the forecast source and predate the action session")
    points = (planning_path or {}).get("points", {})
    closing_kind = ("planning_close" if (planning_path or {}).get("contract_version")
                    == SPARSE_PLANNING_PRICE_PATH_CONTRACT else "observed_close")
    rows = []
    for symbol in sorted(forecasts.symbol.unique()):
        bars = by_symbol.get(symbol, prices.iloc[:0])
        for hour in range(4, 18):
            clock = f"{hour:02d}:00"
            timestamp = _clock(action_date, hour)
            point = points.get(f"{symbol}|{action_date}|{clock}")
            if point is not None and (point.get("symbol") != symbol or point.get("action_date") != action_date
                                      or point.get("clock_local") != clock
                                      or _aware_timestamp(point["timestamp"], "planning clock") != timestamp
                                      or point.get("endpoint_kind") != (closing_kind if hour == 17 else "observed_open")):
                raise ValueError("Saved planning price point differs from its declared market clock")
            estimate = point is not None and point.get("status") == "AVAILABLE"
            low, mid, high = ((_number(point.get(f"planned_price_{field}")) for field in ("low", "mid", "high"))
                              if estimate else (None, None, None))
            if estimate and (None in (low, mid, high) or not 0 < low <= mid <= high):
                raise ValueError("Saved planning prices are invalid")
            actual = _observation(bars, timestamp, close=hour == 17) if timestamp <= now else None
            status = "PENDING_MATURITY" if timestamp > now else "MATURE_AWAITING_DATA" if actual is None else "COMPARED" if estimate else "NO_SAVED_ESTIMATE"
            rows.append({"symbol": symbol, "action_date": action_date, "clock_local": clock,
                         "timestamp": timestamp, "planned_price_low": low, "planned_price_mid": mid, "planned_price_high": high,
                         "actual_price": actual[0] if actual else None, "actual_observed_at": actual[1] if actual else None,
                         "price_error": actual[0] - mid if actual and estimate else None,
                         "price_error_fraction": actual[0] / mid - 1 if actual and estimate else None,
                         "in_planned_range": bool(low <= actual[0] <= high) if actual and estimate else None,
                         "comparison_status": status,
                         **_evidence_columns("actual", _boundary_evidence(bars, timestamp, close=hour == 17,
                            now=now, inventory=inventory, symbol=str(symbol).upper()))})
    return pd.DataFrame(rows)


def _latest_saved_gameplan(root: Path, action_date: str):
    from ml.nightly_gameplan import read_gameplan_run
    candidates = []
    for path in (root / "ml/nightly-gameplan-runs").glob("*/receipt.json"):
        receipt = _json(path)
        if receipt.get("action_date") != action_date:
            continue
        publication = read_gameplan_run(root, path.parent)
        if publication.manifest["configuration"].get("target_contract_version") != "independent-stock-targets-v1":
            continue
        published = _aware_timestamp(publication.receipt["published_at"], "publication time")
        if published < _clock(action_date, 4):
            candidates.append((published, path.parent.name, publication))
    return max(candidates, key=lambda item: item[:2])[2] if candidates else None


def _saved_trade_plan(root: Path, publication, *, preparation_cutoff=None):
    source = publication.run_directory.relative_to(root).as_posix()
    action_date = publication.receipt["action_date"]
    candidates = []
    for path in (root / "ml/gameplan-trade-plan-runs").glob("*/receipt.json"):
        receipt = _json(path)
        if receipt.get("status") != "COMPLETE" or receipt.get("source_gameplan_run") != source:
            continue
        completed = _aware_timestamp(receipt["completed_at"], "trade plan completion")
        if completed < (_clock(action_date, 4) if preparation_cutoff is None else preparation_cutoff):
            candidates.append((completed, path.parent.name, path.parent, receipt))
    if not candidates:
        return None
    _, _, run, receipt = max(candidates, key=lambda item: item[:2])
    manifest = verify_manifest(run)
    if (run.resolve().parent != root / "ml/gameplan-trade-plan-runs"
            or receipt.get("run_path") != run.relative_to(root).as_posix()
            or receipt.get("manifest_sha256") != file_checksum(run / "manifest.json")
            or receipt.get("action_date") != action_date
            or receipt.get("source_receipt_sha256") != file_checksum(publication.run_directory / "receipt.json")
            or receipt.get("orders_placed") != 0 or receipt.get("broker_orders_enabled") is not False
            or manifest["configuration"].get("source_gameplan_run") != source
            or manifest["configuration"].get("source_receipt_sha256") != receipt["source_receipt_sha256"]
            or "trade-plan.parquet" not in manifest["output_files"]):
        raise ValueError("Saved trade plan receipt or source identity is invalid")
    trades = pd.read_parquet(run / "trade-plan.parquet")
    forecasts = pd.read_parquet(publication.run_directory / "forecasts.parquet")
    _match_trade_rows(forecasts, trades)
    price_path = _json(run / "planning-price-path.json") if "planning-price-path.json" in manifest["output_files"] else None
    return run, trades, price_path


def _money(value) -> str:
    number = _number(value)
    return "—" if number is None else f"${number:,.2f}"


def _percent(value, *, signed=False) -> str:
    number = _number(value)
    return "—" if number is None else format(number * 100, "+.2f" if signed else ".2f") + "%"


def _table(headers, rows) -> list[str]:
    def text(value):
        return str(value).replace("|", "\\|").replace("\n", " ")
    return ["| " + " | ".join(headers) + " |", "| " + " | ".join("---" for _ in headers) + " |",
            *("| " + " | ".join(map(text, row)) + " |" for row in rows), ""]


def _missing_boundary_text(row: Mapping, prefix: str, *, detailed: bool = False) -> str | None:
    status = row.get(f"{prefix}_status")
    if status not in {"NO_OBSERVATION", "INVALID_OBSERVATION", "OUTSIDE_TOLERANCE"}:
        return None
    descriptions = {"NO_OBSERVATION": "No observed price on the required side of the clock",
                    "INVALID_OBSERVATION": "Nearest observed price is invalid",
                    "OUTSIDE_TOLERANCE": "No observed price within five minutes"}
    text = descriptions[status]
    if not detailed:
        return text
    candidate = row.get(f"{prefix}_candidate_observed_at")
    seconds = _number(row.get(f"{prefix}_gap_seconds"))
    if candidate is not None and pd.notna(candidate) and seconds is not None:
        timestamp = pd.Timestamp(candidate).tz_convert(TIMEZONE).strftime("%b %d %H:%M")
        text += f" (nearest {timestamp}; {seconds / 60:g} minutes away)"
    coverage = row.get(f"{prefix}_source_coverage")
    text += {"VERIFIED_COMPLETE": "; source request covers the full tolerance window",
             "INCOMPLETE": "; source request coverage is incomplete",
             "UNKNOWN": "; source request coverage is unknown"}.get(coverage, "")
    return text


def render_actuals_review(forecasts: pd.DataFrame, prices: pd.DataFrame, report: Mapping) -> str:
    lines = [f"# Gameplan results · {report['action_date']}", "",
             ("**Preview — final results await the completed-session data fetch.** All times are Pacific."
              if report.get("preview") else f"Tomorrow's Gameplan for **{report['successor_action_date']}** is prepared. All times are Pacific."), "",
             "The saved price estimates below are compared with actual market prices at the same clock. "
             "The ranges were planning estimates; the observations are market prices, not broker fills or trading P/L.", ""]
    if report.get("source_gameplan_run"):
        lines += [f"Original forecast: [{report['action_date']} Gameplan]({report['source_gameplan_path']}/forecasts.parquet).", ""]
    if report.get("source_trade_plan_path"):
        lines += [f"[Original Gameplan with prices and quantities]({report['source_trade_plan_path']}/Gameplan.md).", ""]
    if forecasts.empty:
        return "\n".join(lines + ["No saved independent-stock Gameplan was available for this completed session.", ""])
    totals = report["forecasts"]
    missing_label = ("missing eligible price observations" if "actual_start_status" in forecasts else "waiting for price data")
    lines += [f"**{totals['evaluated']} evaluated · {totals['pending_maturity']} still pending · "
              f"{totals['mature_awaiting_data']} {missing_label}.**", "",
              "Direction results compare the saved Bullish/Bearish call with the actual price move. Neutral forecasts "
              "have no directional score. Future and missing outcomes are excluded from accuracy.", ""]
    summary_rows = []
    for symbol, frame in forecasts.groupby("symbol", sort=True):
        calls = frame.loc[frame.direction_correct.notna() & frame.model_status.eq("PROMOTED")]
        clocks = prices.loc[prices.symbol.eq(symbol) & prices.comparison_status.eq("COMPARED")]
        summary_rows.append((symbol, f"{int(calls.direction_correct.sum())} / {len(calls)}" if len(calls) else "—",
                             _percent(calls.direction_correct.astype(float).mean() if len(calls) else None),
                             _percent(clocks.price_error_fraction.abs().mean() if len(clocks) else None),
                             f"{int(clocks.in_planned_range.sum())} / {len(clocks)}" if len(clocks) else "—"))
    lines += _table(["Stock", "Correct / scored approved calls", "Direction accuracy", "Mean absolute price error", "Prices within range"], summary_rows)
    for symbol, frame in forecasts.groupby("symbol", sort=True):
        lines += [f"## {symbol}", "", "### Planned prices vs actual prices", ""]
        price_rows = []
        missing_rows = []
        for row in prices.loc[prices.symbol.eq(symbol)].to_dict("records"):
            difference = _number(row["price_error"])
            observed = row["actual_observed_at"]
            observed = pd.Timestamp(observed).tz_convert(TIMEZONE).strftime("%H:%M") if pd.notna(observed) else "—"
            status = {"COMPARED": "Inside" if row["in_planned_range"] else "Outside", "NO_SAVED_ESTIMATE": "No saved estimate",
                      "MATURE_AWAITING_DATA": "Waiting for data", "PENDING_MATURITY": "Pending"}[row["comparison_status"]]
            if row["comparison_status"] == "MATURE_AWAITING_DATA":
                status = _missing_boundary_text(row, "actual") or status
                detail = _missing_boundary_text(row, "actual", detailed=True)
                if detail:
                    missing_rows.append((f"Price {row['clock_local']}", detail))
            price_rows.append((row["clock_local"], f"{_money(row['planned_price_low'])}–{_money(row['planned_price_high'])}" if _number(row["planned_price_mid"]) else "—",
                               _money(row["planned_price_mid"]), _money(row["actual_price"]), observed,
                               f"{'+' if difference >= 0 else '−'}{_money(abs(difference))}" if difference is not None else "—",
                               _percent(row["price_error_fraction"], signed=True), status))
        lines += _table(["Time", "Saved price range", "Saved midpoint", "Actual price", "Observed at", "Actual − midpoint", "Difference %", "Range result"], price_rows)
        lines += ["### Forecast outcomes", ""]
        outcome_rows = []
        for row in frame.to_dict("records"):
            start, end = (pd.Timestamp(row[field]).tz_convert(TIMEZONE).strftime("%b %d %H:%M")
                          for field in ("target_window_start", "target_window_end"))
            status = {"PENDING_MATURITY": "Pending target end", "MATURE_AWAITING_DATA": "Waiting for data"}.get(row["actuals_status"], row["direction_result"])
            if row["actuals_status"] == "MATURE_AWAITING_DATA":
                reasons = [(label, _missing_boundary_text(row, prefix))
                           for label, prefix in (("Start", "actual_start"), ("End", "actual_end"))]
                status = "; ".join(f"{label}: {reason}" for label, reason in reasons if reason) or status
                for label, prefix in (("Start", "actual_start"), ("End", "actual_end")):
                    detail = _missing_boundary_text(row, prefix, detailed=True)
                    if detail:
                        missing_rows.append((f"{row['route']} {label.lower()}", detail))
            outcome_rows.append((row["route"], f"{start} → {end}", row["direction"], _percent(row["calibrated_probability"]),
                                 "Approved" if row["model_status"] == "PROMOTED" else "Research",
                                 _money(row["actual_start_price"]), _money(row["actual_end_price"]), _percent(row["actual_return"], signed=True), status))
        lines += _table(["Forecast", "Window", "Saved direction", "P(up)", "Model", "Actual start", "Actual end", "Price move", "Result"], outcome_rows)
        if missing_rows:
            lines += ["<details>", "<summary>Missing price observation details</summary>", ""]
            lines += _table(["Boundary", "Observed evidence and source coverage"], missing_rows)
            lines += ["Complete source request coverage does not prove that no trades occurred. "
                      "Missing observations remain unscored; nearby observations are shown only to explain the gap.", "", "</details>", ""]
    lines += ["Actual prices use the Gameplan's own stock dataset and the existing five-minute boundary tolerance. "
              "The 17:00 price is a completed minute's closing price; earlier hourly clocks use opening prices. "
              "No missing prices are filled. Longer forecasts continue in the cumulative saved-Gameplan evaluation.", "",
              "The machine-readable results also retain the model's cost-adjusted target and Brier score, "
              "separately from the raw-price direction results above.", ""]
    return "\n".join(lines)


def publish_actuals_review(root: Path, *, gameplan_run: Path, deadline: object | None = None,
                           clock=utc_timestamp, price_loader=None,
                           deadline_exception: Path | None = None) -> Path:
    from ml.nightly_gameplan import read_gameplan_run
    from ml.stock_target_prices import load_stock_target_prices
    root = Path(root).resolve()
    successor = read_gameplan_run(root, Path(gameplan_run))
    if successor.manifest["configuration"].get("target_contract_version") != "independent-stock-targets-v1":
        raise ValueError("Actuals review requires an independent-stock successor Gameplan")
    successor_date = str(successor.receipt["action_date"])
    expected_deadline = _clock(successor_date, 4)
    deadline_at = expected_deadline if deadline is None else _aware_timestamp(deadline, "deadline")
    now = _aware_timestamp(clock(), "review time")
    if deadline_at != expected_deadline:
        raise ValueError("Actuals review must retain the successor's original 04:00 deadline")
    from ml.preparation_deadline import preparation_deadline
    original_deadline = deadline_at
    deadline_at, exception_evidence = preparation_deadline(root, successor.run_directory, deadline_at, now, deadline_exception)
    if now >= deadline_at:
        raise ValueError("Actuals review must retain the successor's original 04:00 deadline")
    # A one-time late successor may finish its preparation now. Historical
    # comparisons below still select only estimates saved before their opening.
    successor_trade_plan = _saved_trade_plan(root, successor, preparation_cutoff=now if exception_evidence else None)
    if successor_trade_plan is None:
        raise ValueError("Successor Gameplan trade planning must finish before actuals review")
    if (_aware_timestamp(successor.receipt["published_at"], "successor publication") > now
            or _aware_timestamp(_json(successor_trade_plan[0] / "receipt.json")["completed_at"], "successor trade planning") > now):
        raise ValueError("Successor preparation is not completed at the review time")
    action_date = previous_action_date(successor_date)
    cutoff = _clock(action_date, 17)
    if now < cutoff:
        raise ValueError("The reviewed action session has not closed")
    original = _latest_saved_gameplan(root, action_date)
    inputs = [successor.run_directory / "receipt.json", successor_trade_plan[0] / "receipt.json"]
    results, price_results = pd.DataFrame(), pd.DataFrame()
    report = {"schema_version": VERSION, "status": "COMPLETE", "action_date": action_date,
              "successor_action_date": successor_date, "successor_gameplan_run": successor.run_directory.relative_to(root).as_posix(),
              "reviewed_at": now.isoformat(), "outcomes_through": cutoff.isoformat(), "deadline_at": original_deadline.isoformat(),
              "effective_deadline_at":deadline_at.isoformat(), "deadline_exception":exception_evidence,
              "source_selection": "Last verified publication and matching trade plan saved before the reviewed 04:00 opening",
              "orders_placed": 0, "broker_orders_enabled": False}
    if original is not None:
        forecasts = pd.read_parquet(original.run_directory / "forecasts.parquet")
        trades = _saved_trade_plan(root, original)
        contract = original.manifest["configuration"]["target_price_source_contract"]
        prices, price_files, inventory = (price_loader or load_stock_target_prices)(
            root, symbols=tuple(sorted(forecasts.symbol.unique())), source_contract=contract)
        results = compare_forecasts(forecasts, prices, observed_at=cutoff, trade_rows=trades[1] if trades else None)
        price_results = compare_price_points(forecasts, prices, observed_at=cutoff, action_date=action_date,
                                            planning_path=trades[2] if trades else None)
        inputs += [original.run_directory / "receipt.json", original.run_directory / "manifest.json",
                   original.run_directory / "forecasts.parquet", *price_files]
        if trades:
            inputs += [trades[0] / "receipt.json", trades[0] / "manifest.json", trades[0] / "trade-plan.parquet"]
            if trades[2] is not None:
                inputs.append(trades[0] / "planning-price-path.json")
        report.update(source_gameplan_run=original.run_directory.relative_to(root).as_posix(),
                      source_gameplan_path=original.run_directory.as_posix(),
                      source_trade_plan_path=trades[0].as_posix() if trades else None,
                      target_price_source_contract=contract, price_inventory=inventory,
                      forecasts={"total": len(results), "evaluated": int(results.actuals_status.eq("EVALUATED").sum()),
                                 "pending_maturity": int(results.actuals_status.eq("PENDING_MATURITY").sum()),
                                 "mature_awaiting_data": int(results.actuals_status.eq("MATURE_AWAITING_DATA").sum())},
                      prices={str(key): int(value) for key, value in price_results.comparison_status.value_counts().items()})
    else:
        report["coverage_status"] = "NO_SAVED_INDEPENDENT_GAMEPLAN"
    if _aware_timestamp(clock(), "completion time") >= deadline_at:
        raise ValueError("Actuals review publication deadline passed")
    run = create_timestamp_directory(root / RUNS, timestamp=now)
    results.to_parquet(run / "forecast-results.parquet", index=False)
    price_results.to_parquet(run / "price-results.parquet", index=False)
    _write_json(run / "report.json", report)
    (run / "Gameplan-results.md").write_text(render_actuals_review(results, price_results, report), encoding="utf-8")
    write_manifest(run, run_timestamp=now, input_files=inputs,
                   output_files=["forecast-results.parquet", "price-results.parquet", "report.json", "Gameplan-results.md"],
                   configuration={key: report[key] for key in ("schema_version", "action_date", "successor_gameplan_run", "outcomes_through")},
                   datastore_root=root)
    verify_manifest(run)
    if _aware_timestamp(clock(), "publication time") >= deadline_at:
        raise ValueError("Actuals review publication deadline passed")
    _write_json(run / "receipt.json", {"schema_version": VERSION, "status": "COMPLETE", "action_date": action_date,
                "run_path": run.relative_to(root).as_posix(), "manifest_sha256": file_checksum(run / "manifest.json"),
                "orders_placed": 0, "broker_orders_enabled": False})
    pointer = {"schema_version": VERSION, "current": {"run_path": run.relative_to(root).as_posix(), "action_date": action_date,
               "receipt_sha256": file_checksum(run / "receipt.json")}}
    _write_json(root / "ml/gameplan-actuals-review-latest/run.json", pointer)
    dated = root / f"ml/gameplan-actuals-review-by-date/{action_date}"
    _write_json(dated / "run.json", pointer)
    # The durable run is immutable; this dated reader is the link in the next
    # Gameplan. Replace atomically so an open document never sees a partial copy.
    temporary = dated / "Gameplan-results.tmp"
    temporary.write_bytes((run / "Gameplan-results.md").read_bytes())
    temporary.replace(dated / "Gameplan-results.md")
    return run


def main(argv: list[str] | None = None) -> int:
    from datafetching.parquet_store import DATASTORE_TARGETS, resolve_datastore_dir
    from datafetching.runtime_lock import exclusive_runtime_lock
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--datastore", type=Path)
    group.add_argument("--datastore-target", choices=tuple(DATASTORE_TARGETS), default="pc")
    parser.add_argument("--gameplan-run", required=True, type=Path, help="The completed successor Gameplan")
    parser.add_argument("--deadline")
    parser.add_argument('--deadline-exception', type=Path)
    args = parser.parse_args(argv)
    root = resolve_datastore_dir(root_dir=args.datastore, target=None if args.datastore else args.datastore_target)
    with exclusive_runtime_lock(root / "state/gameplan-actuals-review.lock", process_name="Gameplan actuals review"):
        extra = {'deadline_exception':args.deadline_exception} if args.deadline_exception is not None else {}
        run = publish_actuals_review(root, gameplan_run=args.gameplan_run, deadline=args.deadline, **extra)
    print(json.dumps({"status": "COMPLETE", "run_path": str(run), "review_path": str(run / "Gameplan-results.md"), "orders_placed": 0}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
