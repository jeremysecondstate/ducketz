"""Read-only projections of the auditable intraday forecast store.

This module deliberately has no training, acquisition, broker or write path.
Metrics use the latest saved outcome revision and never the live market price.
"""
from __future__ import annotations

import json
import math
import os
import sqlite3
from collections import Counter, defaultdict
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from statistics import mean, pstdev
from typing import Any, Mapping, Sequence
from zoneinfo import ZoneInfo

PACIFIC = ZoneInfo("America/Los_Angeles")
UTC = timezone.utc
OUTCOME_STATES = ("scored", "pending", "missing", "unscorable", "synthetic")
WINDOWS = ("Today", "Last 20", "Last 50", "Last 100", "7 days", "Cumulative")
FORECAST_MODES = ("live", "shadow", "historical_replay", "All")
DEFAULT_FORECAST_MODE = "live"


@dataclass(frozen=True)
class IntradaySnapshot:
    loaded_at: datetime
    forecasts: tuple[dict[str, Any], ...] = ()
    cycles: tuple[dict[str, Any], ...] = ()
    sources: tuple[dict[str, Any], ...] = ()
    warnings: tuple[str, ...] = ()


def timestamp(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        result = value
    else:
        try:
            result = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except (ValueError, TypeError):
            return None
    return result.astimezone(UTC) if result.tzinfo is not None else None


def probability(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        result = float(value)
    except (ValueError, TypeError):
        return None
    return result if math.isfinite(result) and 0 <= result <= 1 else None


def default_databases(datastore_root: Path | None = None) -> dict[str, Path]:
    """Only the local store is discovered; remote receipt paths are explicit."""
    configured = os.environ.get("DUCKETS_DATASTORE_DIR", "").strip() or os.environ.get("DUCKETS_OHLCV_PARQUET_DIR", "").strip()
    root = datastore_root or Path(configured or "C:/DATASTORE")
    result = {"Local": Path(root) / "intraday" / "shadow.sqlite3"}
    receipt = os.environ.get("DUCKETS_INTRADAY_PEER_RECEIPT", "").strip()
    if receipt:
        result["Peer receipt"] = Path(receipt)
    return result


def configured_actor() -> str | None:
    """Identity only; reading this never confers operating authority."""
    try:
        profile = Path(__file__).resolve().parents[2] / "scratch" / "cross-pc" / "local-profile.json"
        actor = json.loads(profile.read_text(encoding="utf-8"))["actor"]
        return actor if actor in ("Atlas", "Scout") else None
    except (OSError, ValueError, KeyError, TypeError):
        return None


def _payload(row: sqlite3.Row) -> dict[str, Any]:
    value = json.loads(row["payload"])
    if not isinstance(value, dict):
        raise ValueError("Saved payload must be a JSON object")
    return value


def outcome_status(forecast: Mapping[str, Any], now: datetime) -> str:
    outcome = forecast.get("outcome") or {}
    target = timestamp(forecast.get("target_close"))
    observed = timestamp(outcome.get("observed_at"))
    available = timestamp(outcome.get("available_at", outcome.get("observed_at")))
    status = str(outcome.get("status", ""))
    if target is None:
        return "unscorable"
    if target > now or (observed is not None and observed > now) or (available is not None and available > now):
        return "pending"
    if status in ("scored", "synthetic"):
        if observed is None or outcome.get("target_not_down") not in (True, False, 0, 1):
            return "unscorable"
        return status
    # A saved pending observation describes an earlier scoring cutoff. Once
    # the fixed target is past, absence of a mature saved outcome is missing.
    if status == "pending":
        return "missing"
    return status if status in OUTCOME_STATES else "missing"


def load_intraday_snapshot(database_paths: Mapping[str, Path] | None = None, *,
                           now: datetime | None = None, local_pc: str | None = None) -> IntradaySnapshot:
    now = timestamp(now or datetime.now(UTC))
    if now is None:
        raise ValueError("A timezone-aware now is required")
    local_pc = local_pc or configured_actor()
    forecasts, cycles, sources, warnings = [], [], [], []
    for label, raw_path in (database_paths if database_paths is not None else default_databases()).items():
        requested_path = Path(raw_path).expanduser()
        path = requested_path.resolve()
        source = {"label": label, "path": str(path), "status": "missing", "latest_issue": None}
        sources.append(source)
        if not path.is_file():
            continue
        try:
            if path.suffix.casefold() == ".json":
                # This validator reads sanitized immutable monitoring receipts,
                # and imports no provider, model, store or execution service.
                from ml.intraday.telemetry import load_receipt
                if local_pc not in ("Atlas", "Scout"):
                    raise ValueError("Local PC identity required for a peer receipt")
                expected_peer = "Scout" if local_pc == "Atlas" else "Atlas"
                received = load_receipt(requested_path, expected_peer=expected_peer, local_pc=local_pc, as_of=now.isoformat())
                receipt = received["receipt"]
                payload = receipt["packet"]["payload"]
                saved = []
                for raw in payload["forecasts"]:
                    value = dict(raw)
                    value["outcome"] = value.get("outcome") or {}
                    value["intents"] = []  # Private account intent quantities are excluded from this channel.
                    value["source_label"] = label
                    value["outcome_status"] = outcome_status(value, now)
                    saved.append(value)
                forecasts.extend(saved)
                for raw in payload["cycles"]:
                    value = dict(raw)
                    value.setdefault("id", value.get("cycle_id", "unknown"))
                    value["source_label"] = label
                    cycles.append(value)
                latest_issue = timestamp(received.get("latest_forecast_issue")) or max(
                    (timestamp(row.get("issued_at")) for row in saved if timestamp(row.get("issued_at"))), default=None)
                source.update(status="read", latest_issue=latest_issue, latest_recorded_at=received.get("latest_recorded_at"),
                              producer_pc=receipt["producer_pc"], produced_at=payload["produced_at"],
                              received_at=receipt["received_at"], producer_age_seconds=received["producer_age_seconds"],
                              receipt_age_seconds=received["receipt_age_seconds"], telemetry_status=received["status"],
                              latest_forecast_mode=received.get("latest_forecast_mode"))
                continue
            # mode=ro forbids schema creation or accidental commits. Do not use
            # immutable=1: a running local store may legitimately have WAL data.
            with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=1)) as connection:
                connection.row_factory = sqlite3.Row
                connection.execute("PRAGMA query_only=ON")
                connection.execute("BEGIN")
                tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
                if "forecasts" not in tables:
                    raise ValueError("Store has no forecasts table")
                outcomes: dict[str, dict[str, Any]] = {}
                if "outcomes" in tables:
                    for row in connection.execute("SELECT * FROM outcomes ORDER BY rowid"):
                        value = _payload(row)
                        value["observed_at"] = row["observed_at"]
                        value["revision_id"] = row["id"]
                        outcomes[str(row["forecast_id"])] = value
                intents: dict[str, list[dict[str, Any]]] = defaultdict(list)
                if "intents" in tables:
                    for row in connection.execute("SELECT * FROM intents ORDER BY rowid"):
                        value = _payload(row)
                        value["recorded_at"] = row["recorded_at"]
                        intents[str(row["forecast_id"])].append(value)
                persistence = {}
                if "events" in tables:
                    for row in connection.execute("SELECT * FROM events WHERE kind='forecast_persisted' ORDER BY rowid"):
                        value = _payload(row)
                        if value.get("forecast_id"):
                            persistence[str(value["forecast_id"])] = value
                saved = []
                for row in connection.execute("SELECT * FROM forecasts ORDER BY issued_at, id"):
                    value = _payload(row)
                    for key in ("pc", "symbol", "cycle_id", "reference_at", "issued_at"):
                        value[key] = row[key]
                    value["forecast_id"] = str(row["id"])
                    value["outcome"] = outcomes.get(value["forecast_id"], {})
                    value["intents"] = intents.get(value["forecast_id"], [])
                    value["persistence_evidence"] = persistence.get(value["forecast_id"], {})
                    value["source_label"] = label
                    value["outcome_status"] = outcome_status(value, now)
                    saved.append(value)
                if "cycles" in tables:
                    for row in connection.execute("SELECT * FROM cycles ORDER BY started_at, rowid"):
                        value = _payload(row)
                        value.update(id=row["id"], pc=row["pc"], started_at=row["started_at"], source_label=label)
                        cycles.append(value)
                connection.rollback()
            forecasts.extend(saved)
            latest = max((timestamp(row.get("issued_at")) for row in saved if timestamp(row.get("issued_at"))), default=None)
            source.update(status="read", latest_issue=latest)
        except (sqlite3.Error, OSError, ValueError, KeyError, IndexError) as exc:
            source["status"] = "unreadable"
            warnings.append(f"{label}: {type(exc).__name__}: {exc}")
    forecasts.sort(key=lambda row: (timestamp(row.get("issued_at")) or datetime.min.replace(tzinfo=UTC), row["forecast_id"]))
    return IntradaySnapshot(now, tuple(forecasts), tuple(cycles), tuple(sources), tuple(warnings))


def source_dataset_label(row: Mapping[str, Any]) -> str:
    return str(row.get("source_dataset") or "Unrecorded")


def filter_forecasts(snapshot: IntradaySnapshot, *, pc: str = "All", symbol: str = "All",
                     session: str = "All", mode: str = "shadow", reference_slot: str = "All",
                     source_dataset: str = "All") -> tuple[dict[str, Any], ...]:
    def accepts(row):
        issued = timestamp(row.get("issued_at"))
        reference = timestamp(row.get("reference_at"))
        return ((pc == "All" or row.get("pc") == pc)
                and (symbol == "All" or row.get("symbol") == symbol)
                and (mode == "All" or row.get("mode", "shadow") == mode)
                and (source_dataset == "All" or source_dataset_label(row) == source_dataset)
                and (reference_slot == "All" or (reference is not None and reference.astimezone(PACIFIC).strftime("%H:%M") == reference_slot))
                and (session == "All" or (issued is not None and issued.astimezone(PACIFIC).date().isoformat() == session)))
    return tuple(row for row in snapshot.forecasts if accepts(row))


def window_rows(rows: Sequence[Mapping[str, Any]], window: str, now: datetime) -> tuple[Mapping[str, Any], ...]:
    ordered = sorted(rows, key=lambda row: timestamp(row.get("issued_at")) or datetime.min.replace(tzinfo=UTC))
    if window.startswith("Last "):
        return tuple(ordered[-int(window.split()[1]):])
    if window == "Today":
        return tuple(row for row in ordered if timestamp(row.get("issued_at")) is not None
                     and timestamp(row["issued_at"]).astimezone(PACIFIC).date() == now.astimezone(PACIFIC).date())
    if window == "7 days":
        return tuple(row for row in ordered if timestamp(row.get("issued_at")) is not None
                     and now - timedelta(days=7) <= timestamp(row["issued_at"]) <= now)
    if window != "Cumulative":
        raise ValueError(f"Unknown metric window: {window}")
    return tuple(ordered)


def _scored_pairs(rows, model: str | None = None):
    result = []
    for row in rows:
        if row.get("outcome_status") not in ("scored", "synthetic"):
            continue
        value = row.get("probability_not_down")
        if model is not None:
            output = next((item for item in row.get("models", []) if item.get("name") == model and item.get("status") == "success"), None)
            value = output.get("probability_not_down") if output is not None else None
        p = probability(value)
        target = row.get("outcome", {}).get("target_not_down")
        if p is not None and target in (True, False, 0, 1):
            result.append((p, int(target), row))
    return result


def forecast_metrics(rows: Sequence[Mapping[str, Any]], *, model: str | None = None) -> dict[str, Any]:
    pairs = _scored_pairs(rows, model)
    counts = Counter(row.get("outcome_status", "unscorable") for row in rows)
    n = len(pairs)
    accuracy = mean(int(p >= .5) == y for p, y, _ in pairs) if n else None
    recalls = [mean(int(p >= .5) == y for p, y, _ in pairs if y == label) for label in (0, 1) if any(y == label for _, y, _ in pairs)]
    calibration = []
    for lower in range(0, 100, 10):
        samples = [(p, y) for p, y, _ in pairs if lower / 100 <= p < (lower + 10) / 100 or (lower == 90 and p == 1)]
        calibration.append({"range": f"{lower}–{lower + 10}%", "count": len(samples),
                            "mean_probability": mean(p for p, _ in samples) if samples else None,
                            "observed_not_down": mean(y for _, y in samples) if samples else None})
    disagreements = []
    for row in rows:
        values = [probability(item.get("probability_not_down")) for item in row.get("models", []) if item.get("status") == "success"]
        values = [p for p in values if p is not None]
        if len(values) > 1:
            disagreements.append(pstdev(values))
    observed_pairs = [item for item in pairs if item[2].get("outcome_status") == "scored"]
    synthetic_pairs = [item for item in pairs if item[2].get("outcome_status") == "synthetic"]
    def is_equality(row):
        try:
            reference = Decimal(str(row.get("reference_open")))
            target = Decimal(str(row.get("outcome", {}).get("target_close_price")))
            return reference.is_finite() and target.is_finite() and reference == target
        except InvalidOperation:
            return False
    equality_pairs = [item for item in pairs if is_equality(item[2])]
    return {"issued": len(rows), "denominator": n, "observed_denominator": len(observed_pairs),
            "synthetic_denominator": len(synthetic_pairs),
            "observed_accuracy": mean(int(p >= .5) == y for p, y, _ in observed_pairs) if observed_pairs else None,
            "synthetic_accuracy": mean(int(p >= .5) == y for p, y, _ in synthetic_pairs) if synthetic_pairs else None,
            "equality_denominator": len(equality_pairs),
            "equality_accuracy": mean(int(p >= .5) == y for p, y, _ in equality_pairs) if equality_pairs else None,
            "observed_equality_denominator": sum(item[2].get("outcome_status") == "scored" for item in equality_pairs),
            "synthetic_equality_denominator": sum(item[2].get("outcome_status") == "synthetic" for item in equality_pairs),
            "counts": {key: counts[key] for key in OUTCOME_STATES},
            "accuracy": accuracy, "balanced_accuracy": mean(recalls) if len(recalls) == 2 else None,
            "brier": mean((p-y)**2 for p, y, _ in pairs) if n else None,
            "log_loss": -mean(y*math.log(max(1e-15, p)) + (1-y)*math.log(max(1e-15, 1-p)) for p, y, _ in pairs) if n else None,
            "calibration": calibration, "mean_disagreement": mean(disagreements) if disagreements else None,
            "disagreement_denominator": len(disagreements)}


def model_metrics(rows: Sequence[Mapping[str, Any]]) -> dict[str, dict[str, Any]]:
    names = sorted({item["name"] for row in rows for item in row.get("models", []) if "name" in item})
    return {"Ensemble": forecast_metrics(rows), **{name: forecast_metrics(rows, model=name) for name in names}}


def error_correlations(rows: Sequence[Mapping[str, Any]]) -> tuple[dict[str, Any], ...]:
    names = sorted({item["name"] for row in rows for item in row.get("models", []) if "name" in item})
    residuals = {name: {row["forecast_id"]: p-y for p, y, row in _scored_pairs(rows, name)} for name in names}
    result = []
    for index, left in enumerate(names):
        for right in names[index + 1:]:
            shared = sorted(residuals[left].keys() & residuals[right].keys())
            x, y = [residuals[left][key] for key in shared], [residuals[right][key] for key in shared]
            coefficient = None
            if len(shared) >= 2:
                xm, ym = mean(x), mean(y)
                denom = math.sqrt(sum((v-xm)**2 for v in x) * sum((v-ym)**2 for v in y))
                if denom > 0:
                    coefficient = sum((a-xm)*(b-ym) for a, b in zip(x, y)) / denom
            result.append({"left": left, "right": right, "count": len(shared), "correlation": coefficient})
    return tuple(result)


def regime_metrics(rows: Sequence[Mapping[str, Any]]) -> tuple[dict[str, Any], ...]:
    """Explicit descriptive bins; realized-return bins are retrospective only."""
    groups = defaultdict(list)
    measured = defaultdict(list)
    for row in rows:
        if row.get("outcome_status") not in ("scored", "synthetic"):
            continue
        reference = row.get("reference_open")
        close = row.get("outcome", {}).get("target_close_price")
        try:
            reference, close = float(reference), float(close)
            if not math.isfinite(reference) or not math.isfinite(close) or reference <= 0 or close <= 0:
                continue
            movement = abs(close / reference - 1)
        except (TypeError, ValueError, ZeroDivisionError):
            continue
        bucket = "Flat ≤0.1%" if movement <= .001 else "Small 0.1–1%" if movement < .01 else "Large ≥1%"
        groups[("Realized |return|", bucket)].append(row)
        issued = timestamp(row.get("issued_at"))
        if issued:
            groups[("Issue hour Pacific", issued.astimezone(PACIFIC).strftime("%H:00"))].append(row)
        for key in ("volatility", "trend_strength", "choppiness"):
            regime = row.get("regime", {}).get(key)
            if isinstance(regime, str):
                groups[(key, regime)].append(row)
        for keys, title in ((("volatility20_observed", "volatility"), "Causal volatility20"),
                            (("trend_efficiency", "trend_strength"), "Causal trend efficiency"),
                            (("choppiness20", "choppiness"), "Causal choppiness20")):
            measurements = row.get("regime_measurements", {})
            value = next((measurements[key] for key in keys if key in measurements), None)
            if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value):
                measured[title].append((value, row))
    for title, samples in measured.items():
        values = sorted(value for value, _ in samples)
        def quantile(fraction):
            position = (len(values)-1) * fraction
            lower, upper = math.floor(position), math.ceil(position)
            return values[lower] + (values[upper]-values[lower]) * (position-lower)
        low, high = quantile(1/3), quantile(2/3)
        for value, row in samples:
            bucket = f"Low ≤{low:.5g}" if value <= low else f"Mid ≤{high:.5g}" if value <= high else f"High >{high:.5g}"
            groups[(title, bucket)].append(row)
    return tuple({"dimension": dimension, "bucket": bucket, **forecast_metrics(values)}
                 for (dimension, bucket), values in sorted(groups.items()))


def latest_matrix(rows: Sequence[Mapping[str, Any]]) -> tuple[Mapping[str, Any], ...]:
    latest = {}
    for row in sorted(rows, key=lambda row: timestamp(row.get("issued_at")) or datetime.min.replace(tzinfo=UTC)):
        latest[(row.get("pc"), row.get("symbol"), source_dataset_label(row), row.get("mode", "shadow"))] = row
    return tuple(latest[key] for key in sorted(latest))


def probability_geometry(value: Any, width: float) -> tuple[float | None, float]:
    """Canvas geometry retains the visible midpoint even for a failed model."""
    p = probability(value)
    return (p * width if p is not None else None, width / 2)
