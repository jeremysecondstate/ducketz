from __future__ import annotations

import json
import math
import numbers
import os
import re
from dataclasses import asdict, dataclass
from datetime import date, time
from pathlib import Path
from typing import Callable, Mapping, Sequence
from zoneinfo import ZoneInfo

import exchange_calendars as xcals
import pandas as pd

from ml.artifacts import file_checksum, utc_timestamp
from ml.option_pricing.policies import ContractSelectionPolicy


OPRA_DATASET = "OPRA.PILLAR"
OPRA_DEFINITION_SCHEMA = "definition"
OPRA_CBBO_SCHEMA = "cbbo-1m"
OPRA_IMPORT_VERSION = "opra-pillar-causal-import-v1"
OPRA_RECEIPT_NAME = "receipt.json"
OPRA_PRICE_SCALE = 1_000_000_000
DEFAULT_MARKET_TIMES = ("10:00", "11:30", "13:30", "15:00")
DEFAULT_SYMBOLS = ("NVDA", "GOOG", "MU")
_NEW_YORK = ZoneInfo("America/New_York")


class OpraImportError(RuntimeError):
    """An OPRA request or immutable evidence set failed closed."""


@dataclass(frozen=True)
class OpraSchedulePoint:
    symbol: str
    session_date: str
    market_time: str
    target_snapshot_for: str


@dataclass(frozen=True)
class OpraRequest:
    dataset: str
    schema: str
    symbols: tuple[str, ...]
    stype_in: str
    start: str
    end: str
    purpose: str
    output_name: str

    def kwargs(self) -> dict[str, object]:
        return {
            "dataset": self.dataset,
            "schema": self.schema,
            "symbols": list(self.symbols),
            "stype_in": self.stype_in,
            "start": self.start,
            "end": self.end,
        }


@dataclass(frozen=True)
class EstimatedOpraRequest:
    request: OpraRequest
    estimated_cost_usd: float


@dataclass(frozen=True)
class OpraImportResult:
    status: str
    phase: str
    estimated_cost_usd: float
    evidence_directory: Path | None
    request_count: int
    downloaded_count: int


def resolve_market_schedule(
    *,
    symbols: Sequence[str],
    start_date: object,
    end_date: object,
    market_times: Sequence[str] = DEFAULT_MARKET_TIMES,
) -> tuple[OpraSchedulePoint, ...]:
    """Resolve XNYS sessions/times without hard-coding a UTC offset."""

    clean_symbols = tuple(
        dict.fromkeys(str(value).strip().upper() for value in symbols if str(value).strip())
    )
    if not clean_symbols:
        raise ValueError("At least one OPRA underlying symbol is required")
    parsed_times = tuple(_parse_market_time(value) for value in market_times)
    if not parsed_times:
        raise ValueError("At least one market time is required")
    start = pd.Timestamp(start_date).date()
    end = pd.Timestamp(end_date).date()
    if end < start:
        raise ValueError("OPRA end date precedes start date")

    calendar = xcals.get_calendar("XNYS")
    sessions = calendar.sessions_in_range(start, end)
    points: list[OpraSchedulePoint] = []
    for session in sessions:
        session_date = pd.Timestamp(session).date()
        open_utc = pd.Timestamp(calendar.session_open(session)).tz_convert("UTC")
        close_utc = pd.Timestamp(calendar.session_close(session)).tz_convert("UTC")
        for parsed, rendered in parsed_times:
            local = pd.Timestamp.combine(session_date, parsed).tz_localize(_NEW_YORK)
            target = local.tz_convert("UTC")
            # This naturally removes post-close points on early-close sessions.
            if target < open_utc or target > close_utc:
                continue
            for symbol in clean_symbols:
                points.append(
                    OpraSchedulePoint(
                        symbol=symbol,
                        session_date=session_date.isoformat(),
                        market_time=rendered,
                        target_snapshot_for=target.isoformat(),
                    )
                )
    return tuple(points)


def definition_requests(
    schedule: Sequence[OpraSchedulePoint],
) -> tuple[OpraRequest, ...]:
    """Build whole-UTC-day definition estimates as Databento recommends."""

    by_day: dict[date, set[str]] = {}
    for point in schedule:
        target = utc_timestamp(point.target_snapshot_for)
        day = target.date()
        by_day.setdefault(day, set()).add(f"{point.symbol}.OPT")
    requests: list[OpraRequest] = []
    for day, symbols in sorted(by_day.items()):
        start = pd.Timestamp(day, tz="UTC")
        end = start + pd.Timedelta(days=1)
        requests.append(
            OpraRequest(
                dataset=OPRA_DATASET,
                schema=OPRA_DEFINITION_SCHEMA,
                symbols=tuple(sorted(symbols)),
                stype_in="parent",
                start=start.isoformat(),
                end=end.isoformat(),
                purpose="POINT_IN_TIME_DEFINITIONS",
                output_name=f"definitions-{day.isoformat()}.dbn.zst",
            )
        )
    return tuple(requests)


def cbbo_requests(
    schedule: Sequence[OpraSchedulePoint],
    definitions: pd.DataFrame,
    *,
    contract_policy: ContractSelectionPolicy | None = None,
    reference_underlyings: Mapping[tuple[str, str], float] | None = None,
) -> tuple[OpraRequest, ...]:
    """Build raw-symbol ten-minute requests after point-in-time filtering."""

    policy = contract_policy or ContractSelectionPolicy()
    reference_underlyings = reference_underlyings or {}
    required = {
        "symbol",
        "contract_symbol",
        "definition_effective_at",
        "expiration_date",
        "call_put",
        "strike",
        "multiplier",
        "standard_contract",
    }
    missing = sorted(required.difference(definitions.columns))
    if missing:
        raise ValueError("Normalized OPRA definitions are missing: " + ", ".join(missing))

    requests: list[OpraRequest] = []
    for point in schedule:
        target = utc_timestamp(point.target_snapshot_for)
        asof = definitions.loc[
            definitions["symbol"].astype("string").str.upper().eq(point.symbol)
        ].copy()
        asof = point_in_time_definition_asof(asof, target)
        if asof.empty:
            continue
        expiration = pd.to_datetime(asof["expiration_date"], utc=True, errors="coerce")
        dte = (expiration.dt.normalize() - target.normalize()).dt.days
        mask = (
            asof["standard_contract"].fillna(False).astype(bool)
            & pd.to_numeric(asof["multiplier"], errors="coerce").eq(100)
            & asof["call_put"].isin(("call", "put"))
            & dte.between(policy.minimum_days_to_expiration, policy.maximum_days_to_expiration)
        )
        underlying = reference_underlyings.get((point.symbol, point.target_snapshot_for))
        if underlying is None or not math.isfinite(float(underlying)) or float(underlying) <= 0:
            # Fetching the whole chain would violate the narrow paid-request
            # contract. A completed point-in-time underlying is required first.
            continue
        strikes = pd.to_numeric(asof["strike"], errors="coerce")
        mask &= (strikes / float(underlying)).map(math.log).abs().le(
            policy.maximum_absolute_log_moneyness
        )
        raw_symbols = tuple(
            sorted(
                set(
                    asof.loc[mask, "contract_symbol"]
                    .astype("string")
                    .dropna()
                    .str.strip()
                )
            )
        )
        if not raw_symbols:
            continue
        # One bounded request includes a five-minute backward source allowance
        # and a five-minute forward target observation allowance. It is exactly
        # ten minutes, which keeps metadata estimates in the documented unit.
        start = target - pd.Timedelta(minutes=5)
        end = target + pd.Timedelta(minutes=5)
        token = target.strftime("%Y%m%dT%H%M%SZ")
        requests.append(
            OpraRequest(
                dataset=OPRA_DATASET,
                schema=OPRA_CBBO_SCHEMA,
                symbols=raw_symbols,
                stype_in="raw_symbol",
                start=start.isoformat(),
                end=end.isoformat(),
                purpose=f"SOURCE_BACKWARD_TARGET_FORWARD:{point.symbol}:{target.isoformat()}",
                output_name=f"cbbo-{point.symbol}-{token}.dbn.zst",
            )
        )
    return tuple(requests)


def estimate_requests(
    client: object,
    requests: Sequence[OpraRequest],
    *,
    reporter: Callable[[str], None] | None = print,
    ceiling: float | None = None,
) -> tuple[EstimatedOpraRequest, ...]:
    """Call metadata.get_cost before every proposed request and render it."""

    estimated: list[EstimatedOpraRequest] = []
    metadata = getattr(client, "metadata", None)
    get_cost = getattr(metadata, "get_cost", None)
    if not callable(get_cost):
        raise TypeError("Databento client has no metadata.get_cost")
    for request in requests:
        cost = float(get_cost(**request.kwargs()))
        if not math.isfinite(cost) or cost < 0:
            raise OpraImportError("Databento returned an invalid cost estimate")
        item = EstimatedOpraRequest(request, cost)
        estimated.append(item)
        if reporter is not None:
            reporter(
                "OPRA ESTIMATE "
                f"dataset={request.dataset} schema={request.schema} "
                f"symbols={','.join(request.symbols)} start={request.start} "
                f"end={request.end} cost_usd={cost:.6f} "
                f"ceiling_usd={_render_ceiling(ceiling)}"
            )
    return tuple(estimated)


def run_import_phase(
    datastore_root: Path,
    *,
    client: object,
    schedule: Sequence[OpraSchedulePoint],
    execute: bool = False,
    max_cost_usd: float | None = None,
    normalized_definitions: pd.DataFrame | None = None,
    reference_underlyings: Mapping[tuple[str, str], float] | None = None,
    reporter: Callable[[str], None] | None = print,
    imported_at: object | None = None,
) -> OpraImportResult:
    """Estimate or execute exactly one resumable OPRA phase.

    A definition phase is selected until verified normalized definitions are
    supplied. The next invocation can then estimate/execute eligible raw-symbol
    CBBO requests. This avoids paying for definitions before the CBBO ceiling is
    known and keeps every paid phase bounded by an explicit operator ceiling.
    """

    if execute and max_cost_usd is None:
        raise OpraImportError("--execute requires an explicit --max-cost-usd")
    if max_cost_usd is not None and (not math.isfinite(max_cost_usd) or max_cost_usd < 0):
        raise ValueError("max_cost_usd must be finite and non-negative")

    if normalized_definitions is None:
        phase = "definitions"
        requests = definition_requests(schedule)
    else:
        phase = "cbbo"
        requests = cbbo_requests(
            schedule,
            normalized_definitions,
            reference_underlyings=reference_underlyings,
        )
    estimates = estimate_requests(
        client,
        requests,
        reporter=reporter,
        ceiling=max_cost_usd,
    )
    total = float(sum(item.estimated_cost_usd for item in estimates))
    if reporter is not None:
        reporter(
            f"OPRA TOTAL phase={phase} requests={len(estimates)} "
            f"estimated_cost_usd={total:.6f} ceiling_usd={_render_ceiling(max_cost_usd)}"
        )
    if not estimates:
        return OpraImportResult(
            status="NO_ELIGIBLE_REQUESTS",
            phase=phase,
            estimated_cost_usd=0.0,
            evidence_directory=None,
            request_count=0,
            downloaded_count=0,
        )
    if not execute:
        return OpraImportResult(
            status="ESTIMATE_ONLY",
            phase=phase,
            estimated_cost_usd=total,
            evidence_directory=None,
            request_count=len(estimates),
            downloaded_count=0,
        )
    assert max_cost_usd is not None
    if total > max_cost_usd + 1e-12:
        raise OpraImportError(
            f"Estimated OPRA cost ${total:.6f} exceeds ceiling ${max_cost_usd:.6f}"
        )

    root = Path(datastore_root).resolve()
    evidence_root = root / "ml" / "option-pricing-evidence" / "opra"
    completed = _matching_completed_import(evidence_root, phase, estimates, schedule)
    if completed is not None:
        return OpraImportResult(
            status="ALREADY_COMMITTED",
            phase=phase,
            estimated_cost_usd=total,
            evidence_directory=completed,
            request_count=len(estimates),
            downloaded_count=0,
        )

    timestamp = utc_timestamp(imported_at)
    run_name = timestamp.strftime("%Y%m%dT%H%M%S.%fZ")
    destination = evidence_root / run_name
    suffix = 2
    while destination.exists():
        destination = evidence_root / f"{run_name}-{suffix}"
        suffix += 1
    evidence_root.mkdir(parents=True, exist_ok=True)
    staging = evidence_root / f".{destination.name}.tmp-{os.getpid()}"
    staging.mkdir()
    try:
        timeseries = getattr(client, "timeseries", None)
        get_range = getattr(timeseries, "get_range", None)
        if not callable(get_range):
            raise TypeError("Databento client has no timeseries.get_range")
        for item in estimates:
            output = staging / item.request.output_name
            # Databento streams DBN directly to path; no unbounded response is
            # converted to a DataFrame here.
            get_range(**item.request.kwargs(), path=output)
            if not output.is_file():
                raise OpraImportError(f"Databento did not create bounded output: {output}")
        manifest = _import_manifest(phase, estimates, schedule, staging, timestamp)
        manifest_path = staging / "manifest.json"
        _write_json(manifest_path, manifest)
        receipt = {
            "schema_version": OPRA_IMPORT_VERSION,
            "phase": phase,
            "imported_at": timestamp.isoformat(),
            "run_path": destination.relative_to(root).as_posix(),
            "manifest_checksum_sha256": file_checksum(manifest_path),
            "estimated_cost_usd": total,
        }
        _write_json(staging / OPRA_RECEIPT_NAME, receipt)
        staging.replace(destination)
    except BaseException:
        _remove_staging(staging)
        raise
    read_opra_import(destination, datastore_root=root)
    return OpraImportResult(
        status="IMPORTED",
        phase=phase,
        estimated_cost_usd=total,
        evidence_directory=destination,
        request_count=len(estimates),
        downloaded_count=len(estimates),
    )


def read_opra_import(directory: Path, *, datastore_root: Path) -> Mapping[str, object]:
    run = Path(directory).resolve()
    root = Path(datastore_root).resolve()
    allowed = (root / "ml" / "option-pricing-evidence" / "opra").resolve()
    if run.parent != allowed:
        raise OpraImportError("OPRA evidence path escapes the immutable import root")
    try:
        manifest = json.loads((run / "manifest.json").read_text(encoding="utf-8"))
        receipt = json.loads((run / OPRA_RECEIPT_NAME).read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
        raise OpraImportError(f"OPRA evidence metadata is unreadable: {run}") from exc
    if not isinstance(manifest, Mapping) or not isinstance(receipt, Mapping):
        raise OpraImportError("OPRA evidence metadata is malformed")
    if (
        manifest.get("schema_version") != OPRA_IMPORT_VERSION
        or receipt.get("schema_version") != OPRA_IMPORT_VERSION
        or receipt.get("run_path") != run.relative_to(root).as_posix()
        or receipt.get("manifest_checksum_sha256") != file_checksum(run / "manifest.json")
    ):
        raise OpraImportError("OPRA evidence receipt does not match its manifest")
    outputs = manifest.get("outputs")
    if not isinstance(outputs, Mapping):
        raise OpraImportError("OPRA evidence output inventory is invalid")
    for name, raw_metadata in outputs.items():
        relative = Path(str(name))
        if relative.is_absolute() or len(relative.parts) != 1:
            raise OpraImportError("OPRA evidence output path escapes its run")
        metadata = raw_metadata if isinstance(raw_metadata, Mapping) else {}
        path = run / relative
        if (
            not path.is_file()
            or int(metadata.get("size", -1)) != path.stat().st_size
            or metadata.get("checksum_sha256") != file_checksum(path)
        ):
            raise OpraImportError(f"OPRA evidence output checksum mismatch: {path}")
    return {"manifest": manifest, "receipt": receipt}


def normalize_fixed_price(value: object) -> float:
    """Normalize a raw DBN fixed-point price without guessing float units."""

    if value is None or value is pd.NA:
        return math.nan
    if isinstance(value, bool):
        return math.nan
    if isinstance(value, numbers.Integral):
        return float(value) / OPRA_PRICE_SCALE
    result = float(value)
    return result if math.isfinite(result) else math.nan


def normalize_definition_records(records: pd.DataFrame) -> pd.DataFrame:
    """Preserve semantic definition fields for point-in-time as-of matching."""

    if records.empty:
        return pd.DataFrame(
            columns=(
                "symbol",
                "contract_symbol",
                "definition_effective_at",
                "expiration_date",
                "call_put",
                "strike",
                "multiplier",
                "standard_contract",
            )
        )
    raw_symbol_column = _first_column(records, "raw_symbol", "symbol")
    effective_column = _first_column(records, "ts_event", "ts_recv")
    expiration_column = _first_column(records, "expiration", "expiration_date")
    class_column = _first_column(records, "instrument_class", "class")
    strike_column = _first_column(records, "strike_price", "strike")
    multiplier_column = _optional_column(records, "unit_of_measure_qty", "contract_multiplier", "multiplier")
    output = pd.DataFrame(index=records.index)
    output["contract_symbol"] = records[raw_symbol_column].astype("string").str.strip()
    if "underlying" in records:
        output["symbol"] = records["underlying"].astype("string").str.strip().str.upper()
    else:
        output["symbol"] = output["contract_symbol"].map(_underlying_from_occ)
    output["definition_effective_at"] = pd.to_datetime(
        records[effective_column], utc=True, errors="coerce"
    )
    expiration = records[expiration_column]
    if pd.api.types.is_numeric_dtype(expiration):
        expiration = pd.to_datetime(expiration, unit="ns", utc=True, errors="coerce")
    else:
        expiration = pd.to_datetime(expiration, utc=True, errors="coerce")
    output["expiration_date"] = expiration.dt.normalize()
    output["call_put"] = records[class_column].map(_normalize_call_put).astype("string")
    output["strike"] = records[strike_column].map(normalize_fixed_price)
    if multiplier_column is None:
        output["multiplier"] = 100.0
    else:
        output["multiplier"] = pd.to_numeric(records[multiplier_column], errors="coerce")
    occ_root = output["contract_symbol"].map(_underlying_from_occ).astype("string")
    output["standard_contract"] = (
        output["multiplier"].eq(100)
        & output["call_put"].isin(("call", "put"))
        & occ_root.eq(output["symbol"])
        & occ_root.str.fullmatch(r"[A-Z.]{1,6}", na=False)
    )
    return output.reset_index(drop=True)


def point_in_time_definition_asof(
    definitions: pd.DataFrame,
    asof: object,
) -> pd.DataFrame:
    if definitions.empty:
        return definitions.copy()
    cutoff = utc_timestamp(asof)
    effective = pd.to_datetime(
        definitions["definition_effective_at"], utc=True, errors="coerce"
    )
    eligible = definitions.loc[effective.le(cutoff)].copy()
    if eligible.empty:
        return eligible
    eligible["definition_effective_at"] = pd.to_datetime(
        eligible["definition_effective_at"], utc=True, errors="coerce"
    )
    return (
        eligible.sort_values("definition_effective_at", kind="stable")
        .drop_duplicates("contract_symbol", keep="last")
        .reset_index(drop=True)
    )


def normalize_cbbo_records(records: pd.DataFrame) -> pd.DataFrame:
    """Normalize CBBO interval-end timestamps and explicit fixed-point prices."""

    if records.empty:
        return pd.DataFrame(
            columns=(
                "contract_symbol",
                "interval_start",
                "quote_timestamp",
                "bid",
                "ask",
                "mid",
                "publisher_id",
            )
        )
    symbol_column = _first_column(records, "raw_symbol", "symbol")
    bid_column = _first_column(records, "bid_px_00", "bid_px", "bid")
    ask_column = _first_column(records, "ask_px_00", "ask_px", "ask")
    output = pd.DataFrame(index=records.index)
    output["contract_symbol"] = records[symbol_column].astype("string").str.strip()
    output["quote_timestamp"] = pd.to_datetime(
        records["ts_recv"], utc=True, errors="coerce"
    )
    output["interval_start"] = output["quote_timestamp"] - pd.Timedelta(minutes=1)
    output["bid"] = records[bid_column].map(normalize_fixed_price)
    output["ask"] = records[ask_column].map(normalize_fixed_price)
    output["mid"] = (output["bid"] + output["ask"]) / 2.0
    output["publisher_id"] = (
        pd.to_numeric(records["publisher_id"], errors="coerce")
        if "publisher_id" in records
        else pd.NA
    )
    valid = (
        output["quote_timestamp"].notna()
        & output["bid"].ge(0)
        & output["ask"].gt(0)
        & output["ask"].ge(output["bid"])
    )
    return output.loc[valid].reset_index(drop=True)


def select_historical_source_target(
    cbbo: pd.DataFrame,
    *,
    target_snapshot_for: object,
    source_staleness: pd.Timedelta = pd.Timedelta(minutes=5),
    target_forward_window: pd.Timedelta = pd.Timedelta(minutes=5),
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Select backward-only source rows and forward-only target rows."""

    target = utc_timestamp(target_snapshot_for)
    timestamps = pd.to_datetime(cbbo["quote_timestamp"], utc=True, errors="coerce")
    source = cbbo.loc[
        timestamps.lt(target) & timestamps.ge(target - source_staleness)
    ].copy()
    source["quote_timestamp"] = pd.to_datetime(source["quote_timestamp"], utc=True)
    source = (
        source.sort_values("quote_timestamp", kind="stable")
        .drop_duplicates("contract_symbol", keep="last")
        .reset_index(drop=True)
    )
    observed = cbbo.loc[
        timestamps.gt(target) & timestamps.le(target + target_forward_window)
    ].copy()
    observed["quote_timestamp"] = pd.to_datetime(observed["quote_timestamp"], utc=True)
    observed = (
        observed.sort_values("quote_timestamp", kind="stable")
        .drop_duplicates("contract_symbol", keep="first")
        .reset_index(drop=True)
    )
    return source, observed


def _import_manifest(
    phase: str,
    estimates: Sequence[EstimatedOpraRequest],
    schedule: Sequence[OpraSchedulePoint],
    directory: Path,
    timestamp: pd.Timestamp,
) -> dict[str, object]:
    outputs = {
        item.request.output_name: {
            "size": (directory / item.request.output_name).stat().st_size,
            "checksum_sha256": file_checksum(directory / item.request.output_name),
        }
        for item in estimates
    }
    return {
        "schema_version": OPRA_IMPORT_VERSION,
        "provider": "databento",
        "dataset": OPRA_DATASET,
        "phase": phase,
        "imported_at": timestamp.isoformat(),
        "prediction_mode": "OFFLINE",
        "estimated_cost_usd": float(sum(item.estimated_cost_usd for item in estimates)),
        "schedule": [asdict(point) for point in schedule],
        "requests": [
            {**asdict(item.request), "estimated_cost_usd": item.estimated_cost_usd}
            for item in estimates
        ],
        "outputs": outputs,
        "limitations": [
            "Historical rows are OFFLINE and cannot count as prospective evidence.",
            "CBBO ts_recv is interpreted as the one-minute interval end.",
            "Missing CBBO intervals remain missing; no look-forward fill is performed.",
        ],
    }


def _matching_completed_import(
    evidence_root: Path,
    phase: str,
    estimates: Sequence[EstimatedOpraRequest],
    schedule: Sequence[OpraSchedulePoint],
) -> Path | None:
    if not evidence_root.is_dir():
        return None
    semantic = {
        "phase": phase,
        "schedule": [asdict(point) for point in schedule],
        "requests": [_request_semantics(item.request) for item in estimates],
    }
    for receipt_path in sorted(evidence_root.glob(f"*/{OPRA_RECEIPT_NAME}")):
        try:
            payload = read_opra_import(receipt_path.parent, datastore_root=evidence_root.parents[2])
            manifest = payload["manifest"]
        except OpraImportError:
            continue
        observed_requests: list[dict[str, object]] = []
        for raw in manifest.get("requests", []):
            if not isinstance(raw, Mapping):
                continue
            observed_requests.append(
                {
                    "dataset": raw.get("dataset"),
                    "schema": raw.get("schema"),
                    "symbols": list(raw.get("symbols", [])),
                    "stype_in": raw.get("stype_in"),
                    "start": raw.get("start"),
                    "end": raw.get("end"),
                    "purpose": raw.get("purpose"),
                    "output_name": raw.get("output_name"),
                }
            )
        observed = {
            "phase": manifest.get("phase"),
            "schedule": manifest.get("schedule"),
            "requests": observed_requests,
        }
        if observed == semantic:
            return receipt_path.parent
    return None


def _request_semantics(request: OpraRequest) -> dict[str, object]:
    payload = asdict(request)
    payload["symbols"] = list(request.symbols)
    return payload


def _parse_market_time(value: str) -> tuple[time, str]:
    match = re.fullmatch(r"(\d{1,2}):(\d{2})", str(value).strip())
    if not match:
        raise ValueError(f"Invalid America/New_York market time: {value}")
    hour, minute = int(match.group(1)), int(match.group(2))
    if hour > 23 or minute > 59:
        raise ValueError(f"Invalid America/New_York market time: {value}")
    return time(hour, minute), f"{hour:02d}:{minute:02d}"


def _render_ceiling(value: float | None) -> str:
    return "NOT_SET" if value is None else f"{value:.6f}"


def _first_column(frame: pd.DataFrame, *names: str) -> str:
    found = _optional_column(frame, *names)
    if found is None:
        raise ValueError("OPRA records are missing one of: " + ", ".join(names))
    return found


def _optional_column(frame: pd.DataFrame, *names: str) -> str | None:
    return next((name for name in names if name in frame.columns), None)


def _underlying_from_occ(raw_symbol: object) -> str:
    match = re.match(r"^([A-Z.]{1,6})\s*\d{6}[CP]", str(raw_symbol).strip().upper())
    return match.group(1) if match else ""


def _normalize_call_put(value: object) -> str | None:
    token = str(value).strip().upper()
    if token in {"C", "CALL", "1"}:
        return "call"
    if token in {"P", "PUT", "2"}:
        return "put"
    return None


def _write_json(path: Path, payload: Mapping[str, object]) -> None:
    path.write_text(
        json.dumps(dict(payload), indent=2, sort_keys=True, default=str) + "\n",
        encoding="utf-8",
    )


def _remove_staging(path: Path) -> None:
    if not path.is_dir() or ".tmp-" not in path.name:
        return
    for child in path.iterdir():
        if child.is_file():
            child.unlink(missing_ok=True)
    path.rmdir()


__all__ = [
    "DEFAULT_MARKET_TIMES",
    "DEFAULT_SYMBOLS",
    "OPRA_CBBO_SCHEMA",
    "OPRA_DATASET",
    "OPRA_DEFINITION_SCHEMA",
    "OPRA_IMPORT_VERSION",
    "EstimatedOpraRequest",
    "OpraImportError",
    "OpraImportResult",
    "OpraRequest",
    "OpraSchedulePoint",
    "cbbo_requests",
    "definition_requests",
    "estimate_requests",
    "normalize_cbbo_records",
    "normalize_definition_records",
    "normalize_fixed_price",
    "point_in_time_definition_asof",
    "read_opra_import",
    "resolve_market_schedule",
    "run_import_phase",
    "select_historical_source_target",
]
