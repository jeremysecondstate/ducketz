"""Read locally pinned Scout aggregate reports without inventing detail rows.

Reports are private, explicitly imported saved evidence. This reader has no
transport, database, production, model, broker or trading dependencies.
"""
from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation, localcontext
import hashlib
import json
import math
import os
from pathlib import Path
import re
import stat

_SHA = re.compile(r"[a-f0-9]{64}\Z")
_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{7,127}\Z")
_SYMBOL = re.compile(r"[A-Z][A-Z0-9.-]{0,15}\Z")
_TARGET = ("Third 15-minute candle close compared with the first reference candle open; "
           "unchanged counts as not-down.")
_RELATIVE = Path("intraday/scout/statistics-reports/v1")
_MAX_PAYLOAD = 2 * 1024 * 1024
_COUNTS = ("forecasts", "scored", "missing_outcomes", "synthetic_outcomes",
           "other_unscored_outcomes", "unscored_outcomes", "late_forecasts",
           "extreme_probability_count", "wrong_extreme_probability_count",
           "submitted_quantity", "filled_quantity", "submission_unknown", "correct")
_RATIOS = ("accuracy", "brier", "scored_coverage", "observed_not_down_fraction",
           "majority_baseline_accuracy")
_CONFUSION = {"true_not_down", "false_not_down", "true_down", "false_down"}
_TOP = {"schema", "producer", "session_date", "timezone", "as_of_utc",
        "target_definition", "scope", "aggregate", "symbols", "limitations"}
_AGGREGATE = set(_COUNTS) | set(_RATIOS) | {
    "realized_pnl", "execution_status_counts", "execution_dispositions",
    "confusion_matrix", "journal_request_states", "unique_fill_events",
    "buy_quantity", "sell_quantity", "buy_notional", "sell_notional"}
_SYMBOL_FIELDS = set(_COUNTS) | set(_RATIOS) | {
    "session_id", "pc", "symbol", "realized_pnl", "trade_result_basis",
    "limitations", "status", "execution_status_counts", "confusion_matrix",
    "filled_orders", "fill_details"}
_DETAILS = {"buy_quantity", "sell_quantity", "buy_notional", "sell_notional", "fill_events"}
_EXTRA_LIMITATIONS = (
    "Aggregate report only: individual forecast, request, order and fill details are not provided.",
    "Metrics are the original Scout report aggregates; row-level timing and outcomes cannot be reconstructed here.",
    "Trading P&L is unknown: matched cost basis, fees and saved accounting are not provided; gross notional is activity, not profit.",
)


@dataclass(frozen=True)
class ScoutStatsReport:
    session: str | None = None
    status: str = "missing"
    message: str = "No saved Scout statistical report is available."
    completion_id: str | None = None
    generated_at: str | None = None
    report_sha256: str | None = None
    aggregate: dict = field(default_factory=dict)
    symbols: tuple[dict, ...] = ()
    target_definition: str | None = None
    limitations: tuple[str, ...] = ()


def _require(condition, message):
    if not condition:
        raise ValueError(message)


def _day(value):
    _require(isinstance(value, str) and date.fromisoformat(value).isoformat() == value,
             "Invalid report session date.")
    return value


def _stamp(value):
    _require(isinstance(value, str) and len(value) <= 64, "Invalid report timestamp.")
    result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    _require(result.tzinfo is not None, "Report timestamp has no timezone.")
    return result.astimezone(timezone.utc)


def _text(value, maximum=4096):
    _require(isinstance(value, str) and 0 < len(value) <= maximum and
             not any(ord(c) < 32 and c not in "\n\r\t" for c in value), "Invalid report text.")


def _limitations(value):
    _require(isinstance(value, list) and len(value) <= 256, "Invalid report limitations.")
    for text in value:
        _text(text)


def _pairs(pairs):
    result = {}
    for key, value in pairs:
        _require(key not in result, "Duplicate report JSON field.")
        result[key] = value
    return result


def _json(raw):
    return json.loads(raw.decode("utf-8"), object_pairs_hook=_pairs,
                      parse_constant=lambda _: (_ for _ in ()).throw(ValueError("Nonfinite report JSON.")))


def _safe(path):
    for item in (*reversed(path.parents), path):
        try:
            info = item.lstat()
        except FileNotFoundError:
            continue
        _require(not stat.S_ISLNK(info.st_mode) and not getattr(info, "st_file_attributes", 0) & 1024,
                 "Linked report paths are not supported.")
    return path


def _read(path, maximum):
    _safe(path)
    _require(stat.S_ISREG(path.stat().st_mode), "Report input is not a regular file.")
    with path.open("rb") as handle:
        raw = handle.read(maximum + 1)
    _require(len(raw) <= maximum, "Report input exceeds its size bound.")
    return raw


def _root(value):
    configured = os.environ.get("DUCKETS_DATASTORE_DIR", "").strip() or os.environ.get("DUCKETS_OHLCV_PARQUET_DIR", "").strip()
    return Path(value if value is not None else configured or "C:/DATASTORE").expanduser().absolute()


def _records(root):
    directory = _safe(root / _RELATIVE)
    if not directory.exists():
        return ()
    _require(directory.is_dir(), "Report archive is not a directory.")
    entries = tuple(directory.iterdir())
    _require(len(entries) <= 1024, "Too many saved report archives.")
    records = []
    for path in entries:
        _safe(path)
        if not path.is_dir() or not (path / "receipt.json").exists():
            continue  # An unregistered staging directory is not an imported report.
        receipt, session, error = None, None, None
        try:
            receipt = _json(_read(path / "receipt.json", 65536))
            _require(isinstance(receipt, dict), "Invalid report receipt.")
            session = _day(receipt.get("session_date"))
            _require(_ID.fullmatch(path.name) and receipt.get("completion_id") == path.name,
                     "Report completion identity differs.")
            _require(receipt.get("schema") == "scout-15m-statistics-report-receipt-v1"
                     and receipt.get("producer") == "Scout", "Unsupported report receipt identity.")
            for key in ("json_sha256", "markdown_sha256"):
                _require(isinstance(receipt.get(key), str) and _SHA.fullmatch(receipt[key]), "Invalid report receipt hash.")
            verified = _stamp(receipt.get("verified_at"))
            _require(_stamp(receipt.get("as_of_utc")) <= verified, "Report cutoff is newer than receipt verification.")
            if "created_at" in receipt:
                _require(_stamp(receipt["created_at"]) <= verified, "Receipt verification predates creation.")
        except (OSError, ValueError, TypeError, UnicodeError, RecursionError, OverflowError) as exc:
            error = str(exc) if isinstance(exc, ValueError) else "Saved report receipt could not be read."
        records.append((path, receipt, session, error))
    return tuple(records)


def _count(value):
    _require(type(value) is int and 0 <= value <= 10**15, "Invalid report count.")


def _mapping(value):
    _require(isinstance(value, dict) and len(value) <= 128, "Invalid report count mapping.")
    for key, count in value.items():
        _text(key, 128)
        _count(count)


def _decimal(value):
    _require(isinstance(value, str) and len(value) <= 64 and
             re.fullmatch(r"[0-9]+(?:\.[0-9]+)?", value), "Invalid saved gross notional.")
    try:
        result = Decimal(value)
    except InvalidOperation as exc:
        raise ValueError("Invalid saved gross notional.") from exc
    _require(result.is_finite() and result >= 0, "Invalid saved gross notional.")
    return result


def _equal(value, expected):
    if expected is None:
        _require(value is None, "A score without observations must remain unknown.")
    else:
        _require(type(value) in {float, int} and math.isfinite(value) and
                 math.isclose(value, expected, rel_tol=1e-10, abs_tol=1e-12), "Report metrics do not reconcile.")


def _metrics(value):
    for name in _COUNTS:
        _count(value[name])
    forecasts, scored = value["forecasts"], value["scored"]
    _require(scored + value["unscored_outcomes"] == forecasts and
             value["missing_outcomes"] + value["synthetic_outcomes"] + value["other_unscored_outcomes"] == value["unscored_outcomes"],
             "Report outcome coverage does not reconcile.")
    _require(value["correct"] <= scored and value["late_forecasts"] <= forecasts and
             value["wrong_extreme_probability_count"] <= value["extreme_probability_count"] <= forecasts and
             value["filled_quantity"] <= value["submitted_quantity"], "Report counts are inconsistent.")
    _require(value["realized_pnl"] is None, "Aggregate report has no matched P&L accounting.")
    for name in _RATIOS:
        item = value[name]
        _require(item is None or type(item) in {float, int} and math.isfinite(item) and 0 <= item <= 1,
                 "Invalid report ratio.")
    _equal(value["accuracy"], value["correct"] / scored if scored else None)
    _equal(value["scored_coverage"], scored / forecasts if forecasts else None)
    _require((value["brier"] is None) == (scored == 0), "Report Brier population is unavailable.")
    confusion = value["confusion_matrix"]
    _mapping(confusion)
    _require(set(confusion) <= _CONFUSION and sum(confusion.values()) == scored and
             confusion.get("true_not_down", 0) + confusion.get("true_down", 0) == value["correct"],
             "Report confusion matrix does not reconcile.")
    positive = confusion.get("true_not_down", 0) + confusion.get("false_down", 0)
    _equal(value["observed_not_down_fraction"], positive / scored if scored else None)
    _equal(value["majority_baseline_accuracy"], max(positive, scored-positive) / scored if scored else None)
    _mapping(value["execution_status_counts"])
    _require(sum(value["execution_status_counts"].values()) == forecasts,
             "Report execution coverage does not reconcile.")
    _require(value["execution_status_counts"].get("UNKNOWN", 0) == value["submission_unknown"],
             "Report unknown submissions do not reconcile.")


def _validate(payload, receipt):
    _require(isinstance(payload, dict) and set(payload) == _TOP, "Unexpected report fields.")
    _require(payload["schema"] == "scout-15m-session-stats-v1" and payload["producer"] == "Scout"
             and payload["timezone"] == "America/Los_Angeles"
             and _day(payload["session_date"]) == receipt["session_date"], "Report producer or session differs.")
    _require(payload["as_of_utc"] == receipt["as_of_utc"], "Report cutoff differs from its pinned receipt.")
    _require(payload["target_definition"] == _TARGET, "Unsupported original report target.")
    _text(payload["scope"], 8192)
    _limitations(payload["limitations"])
    aggregate, symbols = payload["aggregate"], payload["symbols"]
    _require(isinstance(aggregate, dict) and set(aggregate) == _AGGREGATE, "Unexpected aggregate fields.")
    _require(isinstance(symbols, list) and len(symbols) <= 1000, "Invalid report company inventory.")
    _metrics(aggregate)
    _mapping(aggregate["execution_dispositions"])
    _require(sum(aggregate["execution_dispositions"].values()) == aggregate["forecasts"], "Report dispositions do not reconcile.")
    _mapping(aggregate["journal_request_states"])
    _require(aggregate["journal_request_states"].get("UNKNOWN", 0) == aggregate["submission_unknown"] and
             sum(aggregate["journal_request_states"].values()) <= aggregate["forecasts"], "Report journal coverage does not reconcile.")
    names = set()
    for row in symbols:
        _require(isinstance(row, dict) and set(row) == _SYMBOL_FIELDS, "Unexpected company report fields.")
        name = row["symbol"]
        _require(isinstance(name, str) and _SYMBOL.fullmatch(name) and name not in names, "Duplicate or invalid report company.")
        names.add(name)
        _require(row["pc"] == "Scout" and row["session_id"] == payload["session_date"]
                 and row["status"] == "reviewed" and row["trade_result_basis"] == "saved_execution_projections_only",
                 "Company report ownership or session differs.")
        _limitations(row["limitations"])
        _metrics(row)
        _count(row["filled_orders"])
        _require(row["filled_orders"] == row["execution_status_counts"].get("FILLED", 0), "Filled-order counts differ.")
        detail = row["fill_details"]
        _require(isinstance(detail, dict) and set(detail) == _DETAILS, "Unexpected aggregate fill fields.")
        for field_name in ("buy_quantity", "sell_quantity", "fill_events"):
            _count(detail[field_name])
        _require(detail["buy_quantity"] + detail["sell_quantity"] == row["filled_quantity"], "Company fill quantities differ.")
        for side in ("buy", "sell"):
            _require((detail[side+"_quantity"] == 0) == (_decimal(detail[side+"_notional"]) == 0),
                     "Company gross notional has no matching filled quantity.")
    for name in _COUNTS:
        _require(sum(row[name] for row in symbols) == aggregate[name], "Company totals do not reconcile.")
    for name in _CONFUSION:
        _require(sum(row["confusion_matrix"].get(name, 0) for row in symbols) == aggregate["confusion_matrix"].get(name, 0),
                 "Company confusion totals differ.")
    statuses = Counter()
    for row in symbols:
        statuses.update(row["execution_status_counts"])
    _require(statuses == Counter(aggregate["execution_status_counts"]), "Company execution totals differ.")
    _equal(aggregate["brier"], sum(row["brier"] * row["scored"] for row in symbols if row["scored"]) / aggregate["scored"] if aggregate["scored"] else None)
    for name in ("buy_quantity", "sell_quantity", "unique_fill_events"):
        _count(aggregate[name])
        detail_name = "fill_events" if name == "unique_fill_events" else name
        _require(sum(row["fill_details"][detail_name] for row in symbols) == aggregate[name], "Company fill totals differ.")
    with localcontext() as context:
        context.prec = 128  # Bounded 64-character amounts across <=1,000 companies.
        for name in ("buy_notional", "sell_notional"):
            _require(sum((_decimal(row["fill_details"][name]) for row in symbols), Decimal(0)) == _decimal(aggregate[name]),
                     "Company gross notional totals differ.")


def _rank(record):
    path, receipt, _, _ = record
    # Selection uses only the local receipt's pinned original cutoff. A damaged
    # payload cannot lower its date and quietly expose an older healthy score.
    return _stamp(receipt["as_of_utc"]), path.name


def scout_report_sessions(datastore_root=None):
    """Discover receipted report dates, including damaged reports for inspection."""
    return tuple(sorted({session for _, _, session, _ in _records(_root(datastore_root)) if session}, reverse=True))


def load_scout_report(datastore_root=None, session=None):
    """Read the newest selected aggregate; a damaged revision never falls back."""
    if session is not None:
        _day(session)
    chosen = None
    try:
        records = _records(_root(datastore_root))
        if any(error and day is None for _, _, day, error in records):
            raise ValueError("A saved report receipt has no trustworthy session identity.")
        if session is None:
            dates = {day for _, _, day, _ in records if day}
            session = max(dates) if dates else None
        candidates = [record for record in records if record[2] == session]
        if not candidates:
            return ScoutStatsReport(session=session)
        damaged = next((record for record in candidates if record[3]), None)
        if damaged is not None:
            chosen = damaged
            raise ValueError(damaged[3])
        chosen = max(candidates, key=_rank)
        path, receipt, _, error = chosen
        if error:
            raise ValueError(error)
        raw = _read(path / "report.json", _MAX_PAYLOAD)
        markdown = _read(path / "report.md", _MAX_PAYLOAD)
        _require(hashlib.sha256(raw).hexdigest() == receipt["json_sha256"] and
                 hashlib.sha256(markdown).hexdigest() == receipt["markdown_sha256"], "Saved report bytes differ from their receipt.")
        payload = _json(raw)
        _validate(payload, receipt)
        return ScoutStatsReport(session=session, status="available", message="Verified saved Scout aggregate report · Individual forecast and order details are not provided.",
            completion_id=receipt["completion_id"], generated_at=payload["as_of_utc"], report_sha256=receipt["json_sha256"],
            aggregate=payload["aggregate"], symbols=tuple(payload["symbols"]), target_definition=payload["target_definition"],
            limitations=tuple(dict.fromkeys((*payload["limitations"], *_EXTRA_LIMITATIONS))))
    except (OSError, ValueError, TypeError, KeyError, AttributeError, UnicodeError, RecursionError, OverflowError):
        identity = chosen[0].name if chosen else None
        return ScoutStatsReport(session=session, status="unreadable", completion_id=identity,
            message="Saved Scout aggregate report could not be verified. Its metrics remain unavailable; no older report is substituted.",
            limitations=_EXTRA_LIMITATIONS)
