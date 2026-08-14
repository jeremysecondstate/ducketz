from __future__ import annotations

import json
import math
import numbers
import os
import re
import shutil
import time as time_module
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass
from datetime import date, time
from pathlib import Path
from typing import TYPE_CHECKING, Callable, Mapping, Sequence
from zoneinfo import ZoneInfo

import exchange_calendars as xcals
import pandas as pd

from ml.artifacts import file_checksum, semantic_metadata_fingerprint, utc_timestamp
from ml.option_pricing.policies import ContractSelectionPolicy, PricingPartitionConfig
from ml.universe import (
    PRODUCTION_OPTION_SYMBOLS,
    RESEARCH_OPTION_BENCHMARK_SYMBOLS,
)

if TYPE_CHECKING:
    from ml.option_pricing.eligibility import EligibilityPolicyArtifact


OPRA_DATASET = "OPRA.PILLAR"
OPRA_DEFINITION_SCHEMA = "definition"
OPRA_CBBO_SCHEMA = "cbbo-1m"
OPRA_IMPORT_VERSION = "opra-pillar-causal-import-v3"
OPRA_LEGACY_IMPORT_VERSIONS = {
    "opra-pillar-causal-import-v1",
    "opra-pillar-causal-import-v2",
}
OPRA_RECEIPT_NAME = "receipt.json"
OPRA_REQUEST_RECEIPT_VERSION = "opra-pillar-request-receipt-v1"
OPRA_ATTEMPT_VERSION = "opra-pillar-resumable-attempt-v1"
OPRA_AUTHORIZATION_VERSION = "opra-paid-execution-authorization-v1"
OPRA_AUTHORIZATION_ACTION = "databento.timeseries.get_range"
OPRA_PRICE_SCALE = 1_000_000_000
DEFAULT_MARKET_TIMES = ("10:00", "11:30", "13:30", "15:00")
DEFAULT_SYMBOLS = PRODUCTION_OPTION_SYMBOLS
RESEARCH_BENCHMARK_SYMBOLS = RESEARCH_OPTION_BENCHMARK_SYMBOLS


def required_eligibility_clusters_per_symbol(
    config: PricingPartitionConfig | None = None,
) -> int:
    """Derive the partition requirement rather than embedding the value 504."""

    effective = config or PricingPartitionConfig()
    return sum(
        int(getattr(effective, name))
        for name in (
            "minimum_train_clusters",
            "calibration_clusters",
            "assessment_clusters",
            "lockbox_clusters",
        )
    )


REQUIRED_ELIGIBILITY_CLUSTERS_PER_SYMBOL = required_eligibility_clusters_per_symbol()
MINIMUM_ELIGIBILITY_CALENDAR_MONTHS = 6
OPRA_METADATA_TIMEOUT_SECONDS = 30
OPRA_TIMESERIES_TIMEOUT_SECONDS = 180
OPRA_METADATA_MAX_ATTEMPTS = 3
OPRA_METADATA_MAX_WORKERS = 8
OPRA_PAID_DOWNLOAD_MAX_ATTEMPTS = 1
OPRA_STORAGE_EXPANSION_FACTOR = 2.0
OPRA_STORAGE_RESERVE_BYTES = 5 * 1024**3
DEFAULT_EMULATED_PREDICTION_LATENCY_SECONDS = 60
DEFAULT_OUTCOME_FORWARD_MINUTES = 5
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
    estimated_billable_size_bytes: int | None


@dataclass(frozen=True)
class OpraImportResult:
    status: str
    phase: str
    estimated_cost_usd: float
    evidence_directory: Path | None
    request_count: int
    downloaded_count: int
    estimated_billable_size_bytes: int | None


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


def schedule_contract_report(
    schedule: Sequence[OpraSchedulePoint],
) -> dict[str, object]:
    """Prove the fixed universe, cluster count, uniqueness, and calendar span."""

    return _schedule_contract_report_for_symbols(
        schedule,
        required_symbols=DEFAULT_SYMBOLS,
        scope="PRODUCTION_ELIGIBILITY",
    )


def research_benchmark_schedule_report(
    schedule: Sequence[OpraSchedulePoint],
) -> dict[str, object]:
    """Prove the separate, research-only SPY historical benchmark scope."""

    return _schedule_contract_report_for_symbols(
        schedule,
        required_symbols=RESEARCH_BENCHMARK_SYMBOLS,
        scope="RESEARCH_BENCHMARK_ONLY",
    )


def _schedule_contract_report_for_symbols(
    schedule: Sequence[OpraSchedulePoint],
    *,
    required_symbols: Sequence[str],
    scope: str,
) -> dict[str, object]:
    required = tuple(required_symbols)

    expected_symbols = set(required)
    observed_symbols = {point.symbol for point in schedule}
    counts = {
        symbol: len(
            {
                point.target_snapshot_for
                for point in schedule
                if point.symbol == symbol
            }
        )
        for symbol in required
    }
    natural_keys = [
        (point.symbol, point.target_snapshot_for) for point in schedule
    ]
    duplicate_count = len(natural_keys) - len(set(natural_keys))
    targets = sorted(
        {
            utc_timestamp(point.target_snapshot_for)
            for point in schedule
            if point.symbol in expected_symbols
        }
    )
    first = targets[0] if targets else None
    last = targets[-1] if targets else None
    span_pass = bool(
        first is not None
        and last is not None
        and first <= last - pd.DateOffset(months=MINIMUM_ELIGIBILITY_CALENDAR_MONTHS)
    )
    cluster_pass = all(
        count >= REQUIRED_ELIGIBILITY_CLUSTERS_PER_SYMBOL
        for count in counts.values()
    )
    passed = bool(
        observed_symbols == expected_symbols
        and duplicate_count == 0
        and cluster_pass
        and span_pass
    )
    return {
        "status": "PASS" if passed else "NOT_PROVEN",
        "scope": scope,
        "production_eligible": scope == "PRODUCTION_ELIGIBILITY",
        "required_symbols": list(required),
        "observed_symbols": sorted(observed_symbols),
        "clusters_per_symbol": counts,
        "required_clusters_per_symbol": REQUIRED_ELIGIBILITY_CLUSTERS_PER_SYMBOL,
        "minimum_calendar_months": MINIMUM_ELIGIBILITY_CALENDAR_MONTHS,
        "first_target": first.isoformat() if first is not None else None,
        "last_target": last.isoformat() if last is not None else None,
        "calendar_span_pass": span_pass,
        "duplicate_natural_target_count": duplicate_count,
    }


def cbbo_request_coverage_report(
    schedule: Sequence[OpraSchedulePoint],
    requests: Sequence[OpraRequest],
) -> dict[str, object]:
    """Verify every scheduled symbol/target has exact bounded CALL and PUT requests."""

    expected = {
        (point.symbol, utc_timestamp(point.target_snapshot_for).isoformat())
        for point in schedule
    }
    observed: dict[tuple[str, str], set[str]] = {}
    paired_contracts: dict[tuple[str, str], dict[str, set[str]]] = {}
    invalid_requests: list[str] = []
    output_names: list[str] = []
    for request in requests:
        output_names.append(request.output_name)
        prefix = "SOURCE_BACKWARD_TARGET_FORWARD:"
        rendered = request.purpose
        if not rendered.startswith(prefix):
            invalid_requests.append(f"{request.output_name}: invalid purpose")
            continue
        try:
            symbol, raw_target = rendered[len(prefix) :].split(":", 1)
            target = utc_timestamp(raw_target)
        except (TypeError, ValueError):
            invalid_requests.append(f"{request.output_name}: invalid target")
            continue
        key = (symbol, target.isoformat())
        start = utc_timestamp(request.start)
        end = utc_timestamp(request.end)
        if (
            request.dataset != OPRA_DATASET
            or request.schema != OPRA_CBBO_SCHEMA
            or request.stype_in != "raw_symbol"
            or start != target - pd.Timedelta(minutes=5)
            or end
            != target
            + pd.Timedelta(seconds=DEFAULT_EMULATED_PREDICTION_LATENCY_SECONDS)
            + pd.Timedelta(minutes=DEFAULT_OUTCOME_FORWARD_MINUTES)
            or not request.symbols
        ):
            invalid_requests.append(f"{request.output_name}: request boundary/contract")
            continue
        observed.setdefault(key, set()).update(_request_call_puts(asdict(request)))
        pairs = paired_contracts.setdefault(key, {})
        for raw_symbol in request.symbols:
            pair = _option_pair_key(raw_symbol)
            if pair is not None:
                pair_key, call_put = pair
                pairs.setdefault(pair_key, set()).add(call_put)
    missing_points = sorted(expected.difference(observed))
    extra_points = sorted(set(observed).difference(expected))
    missing_routes = {
        f"{symbol}@{target}": sorted({"call", "put"}.difference(observed.get((symbol, target), set())))
        for symbol, target in sorted(expected)
        if observed.get((symbol, target), set()) != {"call", "put"}
    }
    missing_parity_pairs = [
        f"{symbol}@{target}"
        for symbol, target in sorted(expected)
        if not any(
            routes == {"call", "put"}
            for routes in paired_contracts.get((symbol, target), {}).values()
        )
    ]
    duplicate_outputs = sorted(
        name for name in set(output_names) if output_names.count(name) > 1
    )
    passed = not (
        missing_points
        or extra_points
        or missing_routes
        or missing_parity_pairs
        or duplicate_outputs
        or invalid_requests
    )
    route_symbols = tuple(sorted({point.symbol for point in schedule}))
    route_counts = {
        f"{symbol}/{call_put}": sum(
            call_put in observed.get((point.symbol, utc_timestamp(point.target_snapshot_for).isoformat()), set())
            for point in schedule
            if point.symbol == symbol
        )
        for symbol in route_symbols
        for call_put in ("call", "put")
    }
    return {
        "status": "PASS" if passed else "NOT_PROVEN",
        "scheduled_point_count": len(expected),
        "covered_point_count": len(set(observed).intersection(expected)),
        "request_count": len(requests),
        "route_point_counts": route_counts,
        "missing_points": [f"{symbol}@{target}" for symbol, target in missing_points],
        "extra_points": [f"{symbol}@{target}" for symbol, target in extra_points],
        "missing_routes": missing_routes,
        "missing_put_call_parity_pairs": missing_parity_pairs,
        "duplicate_output_names": duplicate_outputs,
        "invalid_requests": invalid_requests,
    }


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
    emulated_prediction_latency_seconds: int = (
        DEFAULT_EMULATED_PREDICTION_LATENCY_SECONDS
    ),
    outcome_forward_minutes: int = DEFAULT_OUTCOME_FORWARD_MINUTES,
) -> tuple[OpraRequest, ...]:
    """Build raw-symbol ten-minute requests after point-in-time filtering."""

    policy = contract_policy or ContractSelectionPolicy()
    if emulated_prediction_latency_seconds < 0:
        raise ValueError("Emulated prediction latency cannot be negative")
    if outcome_forward_minutes < 1:
        raise ValueError("Outcome forward window must be positive")
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
        # Extend beyond the emulated publication boundary so a quote after the
        # market target but before prediction availability can never become the
        # label merely because the paid window ended too early.
        start = target - pd.Timedelta(minutes=5)
        end = (
            target
            + pd.Timedelta(seconds=emulated_prediction_latency_seconds)
            + pd.Timedelta(minutes=outcome_forward_minutes)
        )
        token = target.strftime("%Y%m%dT%H%M%SZ")
        symbol_chunks = tuple(
            raw_symbols[index : index + 2_000]
            for index in range(0, len(raw_symbols), 2_000)
        )
        for part, chunk in enumerate(symbol_chunks, start=1):
            suffix = f"-part{part:03d}" if len(symbol_chunks) > 1 else ""
            requests.append(
                OpraRequest(
                    dataset=OPRA_DATASET,
                    schema=OPRA_CBBO_SCHEMA,
                    symbols=chunk,
                    stype_in="raw_symbol",
                    start=start.isoformat(),
                    end=end.isoformat(),
                    purpose=f"SOURCE_BACKWARD_TARGET_FORWARD:{point.symbol}:{target.isoformat()}",
                    output_name=f"cbbo-{point.symbol}-{token}{suffix}.dbn.zst",
                )
            )
    return tuple(requests)


def estimate_requests(
    client: object,
    requests: Sequence[OpraRequest],
    *,
    reporter: Callable[[str], None] | None = print,
    ceiling: float | None = None,
    maximum_attempts: int = OPRA_METADATA_MAX_ATTEMPTS,
    maximum_workers: int = OPRA_METADATA_MAX_WORKERS,
    sleeper: Callable[[float], None] = time_module.sleep,
) -> tuple[EstimatedOpraRequest, ...]:
    """Estimate every request with deterministic output and bounded concurrency."""

    if maximum_workers < 1:
        raise ValueError("maximum_workers must be positive")
    metadata = getattr(client, "metadata", None)
    get_cost = getattr(metadata, "get_cost", None)
    if not callable(get_cost):
        raise TypeError("Databento client has no metadata.get_cost")
    get_billable_size = getattr(metadata, "get_billable_size", None)

    def estimate_one(request: OpraRequest) -> EstimatedOpraRequest:
        cost = float(
            _bounded_provider_call(
                get_cost,
                kwargs=request.kwargs(),
                operation="metadata.get_cost",
                maximum_attempts=maximum_attempts,
                sleeper=sleeper,
            )
        )
        if not math.isfinite(cost) or cost < 0:
            raise OpraImportError("Databento returned an invalid cost estimate")
        billable_size = None
        if callable(get_billable_size):
            billable_size = int(
                _bounded_provider_call(
                    get_billable_size,
                    kwargs=request.kwargs(),
                    operation="metadata.get_billable_size",
                    maximum_attempts=maximum_attempts,
                    sleeper=sleeper,
                )
            )
            if billable_size < 0:
                raise OpraImportError("Databento returned an invalid billable size")
        return EstimatedOpraRequest(request, cost, billable_size)

    request_tuple = tuple(requests)
    if not request_tuple:
        return ()
    worker_count = min(maximum_workers, len(request_tuple))
    with ThreadPoolExecutor(
        max_workers=worker_count,
        thread_name_prefix="opra-metadata",
    ) as executor:
        estimated = tuple(executor.map(estimate_one, request_tuple))
    for item in estimated:
        request = item.request
        cost = item.estimated_cost_usd
        billable_size = item.estimated_billable_size_bytes
        if reporter is not None:
            reporter(
                "OPRA ESTIMATE "
                f"dataset={request.dataset} schema={request.schema} "
                f"symbols={','.join(request.symbols)} start={request.start} "
                f"end={request.end} cost_usd={cost:.6f} "
                f"billable_size_bytes={billable_size if billable_size is not None else 'UNKNOWN'} "
                f"ceiling_usd={_render_ceiling(ceiling)}"
            )
    return estimated


def opra_storage_capacity_report(
    datastore_root: Path,
    *,
    estimated_billable_size_bytes: int | None,
) -> dict[str, object]:
    """Prove room for the compressed download, materialization, and reserve."""

    root = Path(datastore_root).resolve()
    anchor = root
    while not anchor.exists() and anchor.parent != anchor:
        anchor = anchor.parent
    usage = shutil.disk_usage(anchor)
    estimated = (
        int(estimated_billable_size_bytes)
        if estimated_billable_size_bytes is not None
        else None
    )
    estimated_expanded = (
        math.ceil(estimated * OPRA_STORAGE_EXPANSION_FACTOR)
        if estimated is not None and estimated >= 0
        else None
    )
    required = (
        estimated_expanded + OPRA_STORAGE_RESERVE_BYTES
        if estimated_expanded is not None
        else None
    )
    status = (
        "PASS"
        if required is not None and usage.free >= required
        else "FAIL"
        if required is not None
        else "NOT_PROVEN"
    )
    return {
        "status": status,
        "anchor": str(anchor),
        "estimated_billable_size_bytes": estimated,
        "estimated_expanded_bytes": estimated_expanded,
        "materialization_expansion_factor": OPRA_STORAGE_EXPANSION_FACTOR,
        "immutable_reserve_bytes": OPRA_STORAGE_RESERVE_BYTES,
        "required_free_bytes": required,
        "available_free_bytes": usage.free,
        "shortfall_bytes": max(0, required - usage.free) if required is not None else None,
    }


def _opra_execution_plan(
    *,
    phase: str,
    estimates: Sequence[EstimatedOpraRequest],
    schedule: Sequence[OpraSchedulePoint],
    eligibility_policy_hash: str,
    maximum_approved_cost_usd: float,
    storage_capacity: Mapping[str, object],
) -> dict[str, object]:
    payload = {
        "action": OPRA_AUTHORIZATION_ACTION,
        "dataset": OPRA_DATASET,
        "phase": phase,
        "eligibility_policy_hash": eligibility_policy_hash,
        "schedule_fingerprint_sha256": semantic_metadata_fingerprint(
            {"schedule": [asdict(point) for point in schedule]}
        ),
        "request_count": len(estimates),
        "requests": [
            {
                **_request_semantics(item.request),
                "estimated_cost_usd": item.estimated_cost_usd,
                "estimated_billable_size_bytes": item.estimated_billable_size_bytes,
            }
            for item in estimates
        ],
        "estimated_cost_usd": float(
            sum(item.estimated_cost_usd for item in estimates)
        ),
        "estimated_billable_size_bytes": storage_capacity.get(
            "estimated_billable_size_bytes"
        ),
        "required_free_bytes": storage_capacity.get("required_free_bytes"),
        "maximum_approved_cost_usd": maximum_approved_cost_usd,
        "paid_download_maximum_attempts": OPRA_PAID_DOWNLOAD_MAX_ATTEMPTS,
    }
    return {
        **payload,
        "plan_fingerprint_sha256": semantic_metadata_fingerprint(payload),
    }


def _write_opra_authorization_template(
    path: Path,
    *,
    plan: Mapping[str, object],
) -> None:
    destination = Path(path).resolve()
    if destination.exists():
        raise OpraImportError(
            f"Refusing to overwrite an OPRA authorization record: {destination}"
        )
    _write_json_atomic(
        destination,
        {
            "schema_version": OPRA_AUTHORIZATION_VERSION,
            "status": "PENDING_OPERATOR_APPROVAL",
            "approval_id": None,
            "approved_by": None,
            "approved_at": None,
            "external_cost_authorized": False,
            "datastore_write_authorized": False,
            "automated_action_allowed": False,
            "plan": dict(plan),
            "operator_instruction": (
                "After independently reviewing every request and the aggregate ceiling, "
                "set status=APPROVED, record approval_id/approved_by/approved_at, and set "
                "both authorization booleans true. Any plan edit invalidates the record."
            ),
        },
    )


def _read_opra_execution_authorization(
    path: Path,
    *,
    expected_plan: Mapping[str, object],
    execution_timestamp: pd.Timestamp,
) -> dict[str, object]:
    source = Path(path).resolve()
    try:
        payload = json.loads(source.read_text(encoding="utf-8"))
    except (OSError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise OpraImportError("OPRA paid-execution authorization is unreadable") from exc
    approved_at = pd.to_datetime(
        payload.get("approved_at") if isinstance(payload, Mapping) else None,
        utc=True,
        errors="coerce",
    )
    if (
        not isinstance(payload, Mapping)
        or payload.get("schema_version") != OPRA_AUTHORIZATION_VERSION
        or payload.get("status") != "APPROVED"
        or not str(payload.get("approval_id", "")).strip()
        or not str(payload.get("approved_by", "")).strip()
        or pd.isna(approved_at)
        or pd.Timestamp(approved_at) > execution_timestamp
        or payload.get("external_cost_authorized") is not True
        or payload.get("datastore_write_authorized") is not True
        or payload.get("automated_action_allowed") is not False
        or payload.get("plan") != dict(expected_plan)
    ):
        raise OpraImportError(
            "OPRA paid-execution authorization does not approve this exact plan"
        )
    return dict(payload)


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
    eligibility_policy_artifact: EligibilityPolicyArtifact | None = None,
    eligibility_scope: bool = True,
    research_benchmark_scope: bool = False,
    authorization_record: Path | None = None,
    authorization_template_path: Path | None = None,
) -> OpraImportResult:
    """Estimate or execute exactly one resumable OPRA phase.

    A definition phase is selected until verified normalized definitions are
    supplied. The next invocation can then estimate/execute eligible raw-symbol
    CBBO requests. This avoids paying for definitions before the CBBO ceiling is
    known and keeps every paid phase bounded by an explicit operator ceiling.
    """

    if eligibility_scope and research_benchmark_scope:
        raise ValueError(
            "An OPRA import cannot be both production eligibility and research benchmark"
        )
    if execute and max_cost_usd is None:
        raise OpraImportError("--execute requires an explicit --max-cost-usd")
    if execute and authorization_template_path is not None:
        raise OpraImportError(
            "An authorization template can only be written during estimate-only mode"
        )
    if authorization_template_path is not None and max_cost_usd is None:
        raise OpraImportError(
            "Writing an authorization template requires an explicit --max-cost-usd"
        )
    if max_cost_usd is not None and (not math.isfinite(max_cost_usd) or max_cost_usd < 0):
        raise ValueError("max_cost_usd must be finite and non-negative")
    root = Path(datastore_root).resolve()
    policy_reference: dict[str, object] | None = None
    if eligibility_policy_artifact is not None:
        from ml.option_pricing.eligibility import read_eligibility_policy

        verified_policy = read_eligibility_policy(
            eligibility_policy_artifact.directory,
            datastore_root=root,
        )
        if verified_policy.policy_hash != eligibility_policy_artifact.policy_hash:
            raise OpraImportError("Eligibility policy identity changed before OPRA execution")
        policy_reference = {
            "policy_hash": verified_policy.policy_hash,
            "path": verified_policy.directory.relative_to(root).as_posix(),
            "receipt_checksum_sha256": file_checksum(
                verified_policy.directory / "receipt.json"
            ),
            "published_before_paid_target_download": True,
        }
    elif execute:
        raise OpraImportError(
            "Paid OPRA execution requires a prepublished eligibility policy artifact"
        )
    if policy_reference is not None:
        expected_policy_hash = str(policy_reference["policy_hash"])
    else:
        from ml.option_pricing.eligibility import eligibility_policy_payload

        expected_policy_hash = semantic_metadata_fingerprint(
            eligibility_policy_payload()
        )

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
    schedule_contract = (
        research_benchmark_schedule_report(schedule)
        if research_benchmark_scope
        else schedule_contract_report(schedule)
    )
    cbbo_coverage = (
        cbbo_request_coverage_report(schedule, requests)
        if phase == "cbbo"
        else None
    )
    verified_scope = eligibility_scope or research_benchmark_scope
    if verified_scope and schedule_contract.get("status") != "PASS":
        raise OpraImportError(
            "OPRA schedule does not satisfy eligibility scope: "
            + json.dumps(schedule_contract, sort_keys=True)
        )
    if (
        verified_scope
        and isinstance(cbbo_coverage, Mapping)
        and cbbo_coverage.get("status") != "PASS"
    ):
        raise OpraImportError(
            "OPRA CBBO plan does not cover every required route: "
            + json.dumps(cbbo_coverage, sort_keys=True)
        )
    estimates = estimate_requests(
        client,
        requests,
        reporter=reporter,
        ceiling=max_cost_usd,
    )
    total = float(sum(item.estimated_cost_usd for item in estimates))
    sizes = [
        item.estimated_billable_size_bytes
        for item in estimates
        if item.estimated_billable_size_bytes is not None
    ]
    aggregate_size = sum(sizes) if len(sizes) == len(estimates) else None
    storage_capacity = opra_storage_capacity_report(
        root,
        estimated_billable_size_bytes=aggregate_size,
    )
    if reporter is not None:
        reporter(
            f"OPRA TOTAL phase={phase} requests={len(estimates)} "
            f"estimated_cost_usd={total:.6f} ceiling_usd={_render_ceiling(max_cost_usd)}"
            f" billable_size_bytes={aggregate_size if aggregate_size is not None else 'UNKNOWN'}"
        )
        reporter(
            "OPRA STORAGE "
            f"status={storage_capacity['status']} "
            f"required_free_bytes={storage_capacity['required_free_bytes']} "
            f"available_free_bytes={storage_capacity['available_free_bytes']}"
        )
    if not estimates:
        return OpraImportResult(
            status="NO_ELIGIBLE_REQUESTS",
            phase=phase,
            estimated_cost_usd=0.0,
            evidence_directory=None,
            request_count=0,
            downloaded_count=0,
            estimated_billable_size_bytes=0,
        )
    execution_plan = (
        _opra_execution_plan(
            phase=phase,
            estimates=estimates,
            schedule=schedule,
            eligibility_policy_hash=expected_policy_hash,
            maximum_approved_cost_usd=max_cost_usd,
            storage_capacity=storage_capacity,
        )
        if max_cost_usd is not None
        else None
    )
    if authorization_template_path is not None:
        assert max_cost_usd is not None
        if total > max_cost_usd + 1e-12:
            raise OpraImportError(
                f"Estimated OPRA cost ${total:.6f} exceeds ceiling "
                f"${max_cost_usd:.6f}; no authorization template was written"
            )
        if storage_capacity.get("status") != "PASS":
            raise OpraImportError(
                "Cannot authorize an OPRA plan without a complete passing storage estimate"
            )
        assert execution_plan is not None
        _write_opra_authorization_template(
            authorization_template_path,
            plan=execution_plan,
        )
        if reporter is not None:
            reporter(
                "OPRA AUTHORIZATION TEMPLATE "
                f"path={Path(authorization_template_path).resolve()} "
                f"plan_fingerprint_sha256={execution_plan['plan_fingerprint_sha256']}"
            )
    if not execute:
        return OpraImportResult(
            status="ESTIMATE_ONLY",
            phase=phase,
            estimated_cost_usd=total,
            evidence_directory=None,
            request_count=len(estimates),
            downloaded_count=0,
            estimated_billable_size_bytes=aggregate_size,
        )
    assert max_cost_usd is not None
    if total > max_cost_usd + 1e-12:
        raise OpraImportError(
            f"Estimated OPRA cost ${total:.6f} exceeds ceiling ${max_cost_usd:.6f}"
        )

    evidence_root = root / "ml" / "option-pricing-evidence" / "opra"
    completed = _matching_completed_import(
        evidence_root,
        phase,
        estimates,
        schedule,
        eligibility_scope=eligibility_scope,
        research_benchmark_scope=research_benchmark_scope,
        schedule_contract=schedule_contract,
        cbbo_coverage=cbbo_coverage,
        eligibility_policy=policy_reference,
    )
    if completed is not None:
        return OpraImportResult(
            status="ALREADY_COMMITTED",
            phase=phase,
            estimated_cost_usd=total,
            evidence_directory=completed,
            request_count=len(estimates),
            downloaded_count=0,
            estimated_billable_size_bytes=aggregate_size,
        )
    if storage_capacity.get("status") != "PASS":
        raise OpraImportError(
            "OPRA paid execution failed the storage-capacity preflight: "
            + json.dumps(storage_capacity, sort_keys=True)
        )
    if authorization_record is None:
        raise OpraImportError(
            "--execute requires an exact approved --authorization-record"
        )

    timestamp = utc_timestamp(imported_at)
    assert execution_plan is not None
    assert authorization_record is not None
    authorization = _read_opra_execution_authorization(
        authorization_record,
        expected_plan=execution_plan,
        execution_timestamp=timestamp,
    )
    run_name = timestamp.strftime("%Y%m%dT%H%M%S.%fZ")
    destination = evidence_root / run_name
    suffix = 2
    while destination.exists():
        destination = evidence_root / f"{run_name}-{suffix}"
        suffix += 1
    evidence_root.mkdir(parents=True, exist_ok=True)
    semantics = {
        "phase": phase,
        "schedule": [asdict(point) for point in schedule],
        "requests": [_request_semantics(item.request) for item in estimates],
        "eligibility_scope_verified": bool(eligibility_scope),
        "research_benchmark_scope_verified": bool(research_benchmark_scope),
        "schedule_contract": schedule_contract,
        "cbbo_request_coverage": cbbo_coverage,
        "eligibility_policy": policy_reference,
    }
    attempt_id = semantic_metadata_fingerprint(semantics)
    staging = evidence_root / f".{phase}-{attempt_id}.incomplete"
    attempt_path = staging / "attempt.json"
    staging.mkdir(exist_ok=True)
    authorization_copy = staging / "authorization.json"
    if authorization_copy.is_file():
        try:
            existing_authorization = json.loads(
                authorization_copy.read_text(encoding="utf-8")
            )
        except (OSError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise OpraImportError(
                "Resumable OPRA authorization copy is unreadable"
            ) from exc
        if existing_authorization != authorization:
            raise OpraImportError(
                "Resumable OPRA attempt has a different paid authorization"
            )
    else:
        _write_json_atomic(authorization_copy, authorization)
    authorization_reference = {
        "schema_version": OPRA_AUTHORIZATION_VERSION,
        "path": authorization_copy.name,
        "size": authorization_copy.stat().st_size,
        "checksum_sha256": file_checksum(authorization_copy),
        "approval_id": authorization["approval_id"],
        "approved_by": authorization["approved_by"],
        "approved_at": authorization["approved_at"],
        "plan_fingerprint_sha256": execution_plan["plan_fingerprint_sha256"],
        "external_cost_authorized": True,
        "datastore_write_authorized": True,
        "automated_action_allowed": False,
    }
    attempt = {
        "schema_version": OPRA_ATTEMPT_VERSION,
        "attempt_id": attempt_id,
        "phase": phase,
        "semantics": semantics,
        "maximum_approved_cost_usd": max_cost_usd,
        "paid_download_maximum_attempts": OPRA_PAID_DOWNLOAD_MAX_ATTEMPTS,
        "created_at": timestamp.isoformat(),
        "eligibility_policy": policy_reference,
        "paid_execution_authorization": authorization_reference,
    }
    if attempt_path.is_file():
        try:
            existing_attempt = json.loads(attempt_path.read_text(encoding="utf-8"))
        except (OSError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise OpraImportError("Resumable OPRA attempt receipt is unreadable") from exc
        if (
            not isinstance(existing_attempt, Mapping)
            or existing_attempt.get("schema_version") != OPRA_ATTEMPT_VERSION
            or existing_attempt.get("attempt_id") != attempt_id
            or existing_attempt.get("semantics") != semantics
            or existing_attempt.get("eligibility_policy") != policy_reference
            or existing_attempt.get("paid_execution_authorization")
            != authorization_reference
            or _finite_cost(existing_attempt.get("maximum_approved_cost_usd"))
            != max_cost_usd
        ):
            raise OpraImportError(
                "Existing resumable OPRA attempt does not match this exact approved phase"
            )
    else:
        _write_json_atomic(attempt_path, attempt)
    downloaded_count = 0
    try:
        timeseries = getattr(client, "timeseries", None)
        get_range = getattr(timeseries, "get_range", None)
        if not callable(get_range):
            raise TypeError("Databento client has no timeseries.get_range")
        for item in estimates:
            output = staging / item.request.output_name
            request_receipt = staging / "request-receipts" / (
                item.request.output_name + ".json"
            )
            if _verified_resumable_output(
                output,
                request_receipt,
                request=item.request,
                attempt_id=attempt_id,
            ):
                continue
            if request_receipt.exists():
                raise OpraImportError(
                    f"Resumable request receipt failed verification: {request_receipt}"
                )
            if output.exists():
                quarantine = output.with_name(
                    output.name + f".unverified-{os.getpid()}"
                )
                output.replace(quarantine)
            # Databento streams DBN directly to path; no unbounded response is
            # converted to a DataFrame here.
            _bounded_provider_call(
                get_range,
                kwargs={**item.request.kwargs(), "path": output},
                operation="timeseries.get_range",
                maximum_attempts=OPRA_PAID_DOWNLOAD_MAX_ATTEMPTS,
                sleeper=time_module.sleep,
            )
            if not output.is_file():
                raise OpraImportError(f"Databento did not create bounded output: {output}")
            _write_json_atomic(
                request_receipt,
                {
                    "schema_version": OPRA_REQUEST_RECEIPT_VERSION,
                    "attempt_id": attempt_id,
                    "request": _request_semantics(item.request),
                    "estimated_cost_usd": item.estimated_cost_usd,
                    "estimated_billable_size_bytes": item.estimated_billable_size_bytes,
                    "output_name": output.name,
                    "output_size": output.stat().st_size,
                    "output_checksum_sha256": file_checksum(output),
                    "downloaded_at": utc_timestamp().isoformat(),
                    "paid_download_attempts": 1,
                },
            )
            downloaded_count += 1
        manifest = _import_manifest(
            phase,
            estimates,
            schedule,
            staging,
            timestamp,
            maximum_approved_cost_usd=max_cost_usd,
            attempt_id=attempt_id,
            eligibility_policy=policy_reference,
            eligibility_scope=eligibility_scope,
            research_benchmark_scope=research_benchmark_scope,
            schedule_contract=schedule_contract,
            cbbo_coverage=cbbo_coverage,
            paid_execution_authorization=authorization_reference,
            execution_plan=execution_plan,
            storage_capacity=storage_capacity,
        )
        manifest_path = staging / "manifest.json"
        _write_json(manifest_path, manifest)
        receipt = {
            "schema_version": OPRA_IMPORT_VERSION,
            "phase": phase,
            "imported_at": timestamp.isoformat(),
            "run_path": destination.relative_to(root).as_posix(),
            "manifest_checksum_sha256": file_checksum(manifest_path),
            "estimated_cost_usd": total,
            "estimated_billable_size_bytes": aggregate_size,
            "maximum_approved_cost_usd": max_cost_usd,
            "paid_cost_ceiling_respected": total <= max_cost_usd,
            "paid_download_maximum_attempts": OPRA_PAID_DOWNLOAD_MAX_ATTEMPTS,
            "eligibility_policy": policy_reference,
            "paid_execution_authorization": authorization_reference,
            "evidence_kind": _opra_evidence_kind(
                eligibility_scope=eligibility_scope,
                research_benchmark_scope=research_benchmark_scope,
            ),
            "eligibility_scope_verified": bool(eligibility_scope),
            "research_benchmark_scope_verified": bool(research_benchmark_scope),
        }
        _write_json(staging / OPRA_RECEIPT_NAME, receipt)
        staging.replace(destination)
    except BaseException:
        # Keep individually checksummed request receipts so an authorized retry
        # can resume the exact phase without redownloading verified outputs.
        raise
    read_opra_import(destination, datastore_root=root)
    return OpraImportResult(
        status="IMPORTED",
        phase=phase,
        estimated_cost_usd=total,
        evidence_directory=destination,
        request_count=len(estimates),
        downloaded_count=downloaded_count,
        estimated_billable_size_bytes=aggregate_size,
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
    artifact_version = manifest.get("schema_version")
    if (
        artifact_version not in ({OPRA_IMPORT_VERSION} | OPRA_LEGACY_IMPORT_VERSIONS)
        or receipt.get("schema_version") != artifact_version
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
    if artifact_version == OPRA_IMPORT_VERSION:
        ceiling = _finite_cost(manifest.get("maximum_approved_cost_usd"))
        estimated = _finite_cost(manifest.get("estimated_cost_usd"))
        evidence_kind = manifest.get("evidence_kind")
        eligibility_scope = manifest.get("eligibility_scope_verified")
        research_benchmark_scope = manifest.get(
            "research_benchmark_scope_verified"
        )
        schedule_contract = manifest.get("schedule_contract")
        cbbo_coverage = manifest.get("cbbo_request_coverage")
        if (
            ceiling is None
            or estimated is None
            or estimated > ceiling + 1e-12
            or manifest.get("paid_cost_ceiling_respected") is not True
            or manifest.get("paid_download_maximum_attempts") != 1
            or evidence_kind
            not in {
                "PRODUCTION_ELIGIBILITY_RECEIPT_PROVEN",
                "RESEARCH_BENCHMARK_RECEIPT_PROVEN",
                "FIXTURE_TEST_ONLY",
            }
            or not isinstance(eligibility_scope, bool)
            or not isinstance(research_benchmark_scope, bool)
            or eligibility_scope
            != (evidence_kind == "PRODUCTION_ELIGIBILITY_RECEIPT_PROVEN")
            or research_benchmark_scope
            != (evidence_kind == "RESEARCH_BENCHMARK_RECEIPT_PROVEN")
            or receipt.get("evidence_kind") != evidence_kind
            or receipt.get("eligibility_scope_verified") is not eligibility_scope
            or receipt.get("research_benchmark_scope_verified")
            is not research_benchmark_scope
            or not isinstance(schedule_contract, Mapping)
            or (
                (eligibility_scope or research_benchmark_scope)
                and schedule_contract.get("status") != "PASS"
            )
            or (
                (eligibility_scope or research_benchmark_scope)
                and manifest.get("phase") == "cbbo"
                and (
                    not isinstance(cbbo_coverage, Mapping)
                    or cbbo_coverage.get("status") != "PASS"
                )
            )
        ):
            raise OpraImportError("OPRA paid ceiling metadata failed verification")
        authorization_reference = manifest.get("paid_execution_authorization")
        authorization_reference = (
            authorization_reference
            if isinstance(authorization_reference, Mapping)
            else {}
        )
        authorization_path = run / str(authorization_reference.get("path", ""))
        if (
            authorization_path.parent != run
            or authorization_path.name != "authorization.json"
            or not authorization_path.is_file()
            or int(authorization_reference.get("size", -1))
            != authorization_path.stat().st_size
            or authorization_reference.get("checksum_sha256")
            != file_checksum(authorization_path)
            or receipt.get("paid_execution_authorization")
            != authorization_reference
        ):
            raise OpraImportError("OPRA paid authorization receipt changed")
        try:
            authorization = json.loads(
                authorization_path.read_text(encoding="utf-8")
            )
        except (OSError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise OpraImportError("OPRA paid authorization copy is unreadable") from exc
        execution_plan = manifest.get("execution_plan")
        storage_capacity = manifest.get("storage_capacity_preflight")
        if not isinstance(execution_plan, Mapping) or not isinstance(
            storage_capacity, Mapping
        ):
            raise OpraImportError("OPRA execution preflight metadata is malformed")
        plan_payload = {
            key: value
            for key, value in execution_plan.items()
            if key != "plan_fingerprint_sha256"
        }
        expected_plan_requests = [
            {
                key: raw.get(key)
                for key in (
                    "dataset",
                    "schema",
                    "symbols",
                    "stype_in",
                    "start",
                    "end",
                    "purpose",
                    "output_name",
                    "estimated_cost_usd",
                    "estimated_billable_size_bytes",
                )
            }
            for raw in manifest.get("requests", ())
            if isinstance(raw, Mapping)
        ]
        required_free = storage_capacity.get("required_free_bytes")
        available_free = storage_capacity.get("available_free_bytes")
        plan_fingerprint = execution_plan.get("plan_fingerprint_sha256")
        if (
            execution_plan.get("action") != OPRA_AUTHORIZATION_ACTION
            or execution_plan.get("dataset") != OPRA_DATASET
            or execution_plan.get("phase") != manifest.get("phase")
            or execution_plan.get("eligibility_policy_hash")
            != dict(manifest.get("eligibility_policy", {})).get("policy_hash")
            or execution_plan.get("schedule_fingerprint_sha256")
            != semantic_metadata_fingerprint(
                {"schedule": manifest.get("schedule")}
            )
            or execution_plan.get("request_count") != len(expected_plan_requests)
            or execution_plan.get("requests") != expected_plan_requests
            or _finite_cost(execution_plan.get("estimated_cost_usd")) != estimated
            or execution_plan.get("estimated_billable_size_bytes")
            != manifest.get("estimated_billable_size_bytes")
            or execution_plan.get("required_free_bytes") != required_free
            or _finite_cost(execution_plan.get("maximum_approved_cost_usd"))
            != ceiling
            or execution_plan.get("paid_download_maximum_attempts") != 1
            or plan_fingerprint != semantic_metadata_fingerprint(plan_payload)
            or authorization_reference.get("plan_fingerprint_sha256")
            != plan_fingerprint
            or storage_capacity.get("status") != "PASS"
            or not isinstance(required_free, int)
            or not isinstance(available_free, int)
            or available_free < required_free
            or not isinstance(authorization, Mapping)
            or authorization.get("schema_version") != OPRA_AUTHORIZATION_VERSION
            or authorization.get("status") != "APPROVED"
            or authorization.get("external_cost_authorized") is not True
            or authorization.get("datastore_write_authorized") is not True
            or authorization.get("automated_action_allowed") is not False
            or authorization.get("plan") != dict(execution_plan)
            or authorization_reference.get("approval_id")
            != authorization.get("approval_id")
            or authorization_reference.get("approved_by")
            != authorization.get("approved_by")
            or authorization_reference.get("approved_at")
            != authorization.get("approved_at")
        ):
            raise OpraImportError("OPRA paid authorization failed verification")
        attempt_metadata = manifest.get("attempt_receipt")
        attempt_metadata = (
            attempt_metadata if isinstance(attempt_metadata, Mapping) else {}
        )
        attempt_path = run / str(attempt_metadata.get("path", ""))
        if (
            attempt_path.parent != run
            or not attempt_path.is_file()
            or int(attempt_metadata.get("size", -1)) != attempt_path.stat().st_size
            or attempt_metadata.get("checksum_sha256") != file_checksum(attempt_path)
        ):
            raise OpraImportError("OPRA resumable-attempt receipt changed")
        try:
            attempt = json.loads(attempt_path.read_text(encoding="utf-8"))
        except (OSError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise OpraImportError("OPRA resumable-attempt receipt is unreadable") from exc
        policy_reference = manifest.get("eligibility_policy")
        policy_reference = (
            policy_reference if isinstance(policy_reference, Mapping) else {}
        )
        semantics = {
            "phase": manifest.get("phase"),
            "schedule": manifest.get("schedule"),
            "requests": [
                {
                    key: raw.get(key)
                    for key in (
                        "dataset",
                        "schema",
                        "symbols",
                        "stype_in",
                        "start",
                        "end",
                        "purpose",
                        "output_name",
                    )
                }
                for raw in manifest.get("requests", ())
                if isinstance(raw, Mapping)
            ],
            "eligibility_scope_verified": eligibility_scope,
            "research_benchmark_scope_verified": research_benchmark_scope,
            "schedule_contract": schedule_contract,
            "cbbo_request_coverage": cbbo_coverage,
            "eligibility_policy": manifest.get("eligibility_policy"),
        }
        expected_attempt_id = semantic_metadata_fingerprint(semantics)
        if (
            not isinstance(attempt, Mapping)
            or attempt.get("schema_version") != OPRA_ATTEMPT_VERSION
            or attempt.get("attempt_id") != expected_attempt_id
            or manifest.get("attempt_id") != expected_attempt_id
            or attempt.get("semantics") != semantics
            or attempt.get("eligibility_policy") != policy_reference
            or attempt.get("paid_execution_authorization")
            != authorization_reference
            or receipt.get("eligibility_policy") != policy_reference
        ):
            raise OpraImportError("OPRA resumable-attempt metadata failed verification")
        from ml.option_pricing.eligibility import read_eligibility_policy

        policy = read_eligibility_policy(
            root / str(policy_reference.get("path", "")),
            datastore_root=root,
        )
        policy_published = pd.Timestamp(policy.receipt["published_at"])
        attempt_created = pd.to_datetime(
            attempt.get("created_at"), utc=True, errors="coerce"
        )
        imported = pd.to_datetime(
            receipt.get("imported_at"), utc=True, errors="coerce"
        )
        authorized = pd.to_datetime(
            authorization.get("approved_at"), utc=True, errors="coerce"
        )
        if (
            policy.policy_hash != policy_reference.get("policy_hash")
            or policy_reference.get("receipt_checksum_sha256")
            != file_checksum(policy.directory / "receipt.json")
            or policy_reference.get("published_before_paid_target_download") is not True
            or pd.isna(attempt_created)
            or pd.isna(imported)
            or pd.isna(authorized)
            or pd.Timestamp(authorized) > pd.Timestamp(attempt_created)
            or policy_published > pd.Timestamp(attempt_created)
            or pd.Timestamp(attempt_created) > pd.Timestamp(imported)
        ):
            raise OpraImportError("OPRA eligibility-policy chronology failed verification")
        request_receipts = manifest.get("request_receipts")
        if not isinstance(request_receipts, Mapping) or set(request_receipts) != set(outputs):
            raise OpraImportError("OPRA per-request receipt inventory is invalid")
        attempt_id = str(manifest.get("attempt_id", ""))
        for output_name, raw_receipt_path in request_receipts.items():
            receipt_relative = Path(str(raw_receipt_path))
            if receipt_relative.is_absolute() or ".." in receipt_relative.parts:
                raise OpraImportError("OPRA request receipt path escapes its run")
            if not _verified_resumable_output(
                run / str(output_name),
                run / receipt_relative,
                request=_request_for_output(manifest, str(output_name)),
                attempt_id=attempt_id,
            ):
                raise OpraImportError(
                    f"OPRA per-request receipt failed verification: {output_name}"
                )
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
                "exercise_style",
                "settlement_type",
                "settlement_reference",
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
    exercise_column = _optional_column(
        records, "exercise_style", "exerciseStyle", "exercise"
    )
    settlement_column = _optional_column(
        records, "settlement_type", "settlementType", "settlement"
    )
    settlement_reference_column = _optional_column(
        records, "settlement_reference", "settlementReference"
    )
    output["exercise_style"] = (
        records[exercise_column].astype("string").str.strip().str.upper()
        if exercise_column is not None
        else "AMBIGUOUS"
    )
    output["settlement_type"] = (
        records[settlement_column].astype("string").str.strip().str.upper()
        if settlement_column is not None
        else "AMBIGUOUS"
    )
    output["settlement_reference"] = (
        records[settlement_reference_column].astype("string").str.strip()
        if settlement_reference_column is not None
        else ""
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
    prediction_available_at: object | None = None,
    source_staleness: pd.Timedelta = pd.Timedelta(minutes=5),
    target_forward_window: pd.Timedelta = pd.Timedelta(minutes=5),
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Select the last source before cutoff and first outcome after availability."""

    target = utc_timestamp(target_snapshot_for)
    outcome_boundary = (
        utc_timestamp(prediction_available_at)
        if prediction_available_at is not None
        else target
    )
    if outcome_boundary < target:
        raise ValueError("Prediction availability cannot precede the market target")
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
        timestamps.gt(outcome_boundary)
        & timestamps.le(outcome_boundary + target_forward_window)
    ].copy()
    observed["quote_timestamp"] = pd.to_datetime(observed["quote_timestamp"], utc=True)
    observed = (
        observed.sort_values("quote_timestamp", kind="stable")
        .drop_duplicates("contract_symbol", keep="first")
        .reset_index(drop=True)
    )
    return source, observed


def _opra_evidence_kind(
    *,
    eligibility_scope: bool,
    research_benchmark_scope: bool,
) -> str:
    if eligibility_scope and research_benchmark_scope:
        raise ValueError("OPRA evidence scopes are mutually exclusive")
    if eligibility_scope:
        return "PRODUCTION_ELIGIBILITY_RECEIPT_PROVEN"
    if research_benchmark_scope:
        return "RESEARCH_BENCHMARK_RECEIPT_PROVEN"
    return "FIXTURE_TEST_ONLY"


def _import_manifest(
    phase: str,
    estimates: Sequence[EstimatedOpraRequest],
    schedule: Sequence[OpraSchedulePoint],
    directory: Path,
    timestamp: pd.Timestamp,
    *,
    maximum_approved_cost_usd: float,
    attempt_id: str,
    eligibility_policy: Mapping[str, object] | None,
    eligibility_scope: bool,
    research_benchmark_scope: bool,
    schedule_contract: Mapping[str, object],
    cbbo_coverage: Mapping[str, object] | None,
    paid_execution_authorization: Mapping[str, object],
    execution_plan: Mapping[str, object],
    storage_capacity: Mapping[str, object],
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
        "evidence_kind": _opra_evidence_kind(
            eligibility_scope=eligibility_scope,
            research_benchmark_scope=research_benchmark_scope,
        ),
        "eligibility_scope_verified": bool(eligibility_scope),
        "research_benchmark_scope_verified": bool(research_benchmark_scope),
        "schedule_contract": dict(schedule_contract),
        "cbbo_request_coverage": (
            dict(cbbo_coverage) if isinstance(cbbo_coverage, Mapping) else None
        ),
        "estimated_cost_usd": float(sum(item.estimated_cost_usd for item in estimates)),
        "estimated_billable_size_bytes": (
            sum(
                int(item.estimated_billable_size_bytes)
                for item in estimates
                if item.estimated_billable_size_bytes is not None
            )
            if all(item.estimated_billable_size_bytes is not None for item in estimates)
            else None
        ),
        "maximum_approved_cost_usd": maximum_approved_cost_usd,
        "paid_cost_ceiling_respected": (
            float(sum(item.estimated_cost_usd for item in estimates))
            <= maximum_approved_cost_usd
        ),
        "paid_download_maximum_attempts": OPRA_PAID_DOWNLOAD_MAX_ATTEMPTS,
        "attempt_id": attempt_id,
        "eligibility_policy": dict(eligibility_policy or {}),
        "paid_execution_authorization": dict(paid_execution_authorization),
        "execution_plan": dict(execution_plan),
        "storage_capacity_preflight": dict(storage_capacity),
        "attempt_receipt": {
            "path": "attempt.json",
            "size": (directory / "attempt.json").stat().st_size,
            "checksum_sha256": file_checksum(directory / "attempt.json"),
        },
        "schedule": [asdict(point) for point in schedule],
        "requests": [
            {
                **asdict(item.request),
                "estimated_cost_usd": item.estimated_cost_usd,
                "estimated_billable_size_bytes": item.estimated_billable_size_bytes,
            }
            for item in estimates
        ],
        "outputs": outputs,
        "request_receipts": {
            item.request.output_name: (
                Path("request-receipts") / (item.request.output_name + ".json")
            ).as_posix()
            for item in estimates
        },
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
    *,
    eligibility_scope: bool,
    research_benchmark_scope: bool,
    schedule_contract: Mapping[str, object],
    cbbo_coverage: Mapping[str, object] | None,
    eligibility_policy: Mapping[str, object] | None,
) -> Path | None:
    if not evidence_root.is_dir():
        return None
    semantic = {
        "phase": phase,
        "schedule": [asdict(point) for point in schedule],
        "requests": [_request_semantics(item.request) for item in estimates],
        "eligibility_scope_verified": bool(eligibility_scope),
        "research_benchmark_scope_verified": bool(research_benchmark_scope),
        "schedule_contract": dict(schedule_contract),
        "cbbo_request_coverage": (
            dict(cbbo_coverage) if isinstance(cbbo_coverage, Mapping) else None
        ),
        "eligibility_policy": eligibility_policy,
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
            "eligibility_scope_verified": manifest.get(
                "eligibility_scope_verified"
            ),
            "research_benchmark_scope_verified": manifest.get(
                "research_benchmark_scope_verified"
            ),
            "schedule_contract": manifest.get("schedule_contract"),
            "cbbo_request_coverage": manifest.get("cbbo_request_coverage"),
            "eligibility_policy": manifest.get("eligibility_policy"),
        }
        if observed == semantic:
            return receipt_path.parent
    return None


def _request_semantics(request: OpraRequest) -> dict[str, object]:
    payload = asdict(request)
    payload["symbols"] = list(request.symbols)
    return payload


def _request_for_output(
    manifest: Mapping[str, object], output_name: str
) -> OpraRequest:
    for raw in manifest.get("requests", ()):
        if isinstance(raw, Mapping) and raw.get("output_name") == output_name:
            try:
                return OpraRequest(
                    dataset=str(raw["dataset"]),
                    schema=str(raw["schema"]),
                    symbols=tuple(str(value) for value in raw["symbols"]),
                    stype_in=str(raw["stype_in"]),
                    start=str(raw["start"]),
                    end=str(raw["end"]),
                    purpose=str(raw["purpose"]),
                    output_name=str(raw["output_name"]),
                )
            except (KeyError, TypeError, ValueError) as exc:
                raise OpraImportError("OPRA request metadata is malformed") from exc
    raise OpraImportError(f"OPRA output has no exact request metadata: {output_name}")


def _verified_resumable_output(
    output: Path,
    receipt_path: Path,
    *,
    request: OpraRequest,
    attempt_id: str,
) -> bool:
    if not output.is_file() or not receipt_path.is_file():
        return False
    try:
        receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    except (OSError, TypeError, ValueError, json.JSONDecodeError):
        return False
    return bool(
        isinstance(receipt, Mapping)
        and receipt.get("schema_version") == OPRA_REQUEST_RECEIPT_VERSION
        and receipt.get("attempt_id") == attempt_id
        and receipt.get("request") == _request_semantics(request)
        and receipt.get("output_name") == output.name
        and int(receipt.get("output_size", -1)) == output.stat().st_size
        and receipt.get("output_checksum_sha256") == file_checksum(output)
        and receipt.get("paid_download_attempts") == 1
    )


def _finite_cost(value: object) -> float | None:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if math.isfinite(parsed) and parsed >= 0.0 else None


def configure_historical_client_timeouts(client: object) -> None:
    """Bound Databento HTTP connect/read waits without exposing its API key."""

    for name, timeout_seconds in (
        ("metadata", OPRA_METADATA_TIMEOUT_SECONDS),
        ("timeseries", OPRA_TIMESERIES_TIMEOUT_SECONDS),
    ):
        endpoint = getattr(client, name, None)
        if endpoint is None or not hasattr(endpoint, "TIMEOUT"):
            raise TypeError(f"Databento client {name} endpoint has no timeout control")
        setattr(endpoint, "TIMEOUT", timeout_seconds)


def _bounded_provider_call(
    function: Callable[..., object],
    *,
    kwargs: Mapping[str, object],
    operation: str,
    maximum_attempts: int,
    sleeper: Callable[[float], None],
) -> object:
    if maximum_attempts < 1:
        raise ValueError("maximum_attempts must be positive")
    last_error: Exception | None = None
    for attempt in range(1, maximum_attempts + 1):
        try:
            return function(**kwargs)
        except Exception as exc:
            last_error = exc
            if attempt == maximum_attempts:
                break
            sleeper(min(2.0 ** (attempt - 1), 4.0))
    assert last_error is not None
    raise OpraImportError(
        f"{operation} failed after {maximum_attempts} bounded attempt(s): "
        f"{type(last_error).__name__}: {last_error}"
    ) from last_error


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


def _request_call_puts(request: Mapping[str, object]) -> set[str]:
    values: set[str] = set()
    for raw in request.get("symbols", ()):
        match = re.match(
            r"^[A-Z.]{1,6}\s*\d{6}([CP])", str(raw).strip().upper()
        )
        if match is not None:
            values.add("call" if match.group(1) == "C" else "put")
    return values


def _option_pair_key(raw_symbol: object) -> tuple[str, str] | None:
    match = re.match(
        r"^([A-Z.]{1,6})\s*(\d{6})([CP])(\d{8})$",
        str(raw_symbol).strip().upper(),
    )
    if match is None:
        return None
    root, expiration, side, strike = match.groups()
    return f"{root}:{expiration}:{strike}", "call" if side == "C" else "put"


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


def _write_json_atomic(path: Path, payload: Mapping[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + f".tmp-{os.getpid()}")
    _write_json(temporary, payload)
    temporary.replace(path)


__all__ = [
    "DEFAULT_MARKET_TIMES",
    "DEFAULT_SYMBOLS",
    "RESEARCH_BENCHMARK_SYMBOLS",
    "OPRA_CBBO_SCHEMA",
    "OPRA_AUTHORIZATION_ACTION",
    "OPRA_AUTHORIZATION_VERSION",
    "OPRA_DATASET",
    "OPRA_DEFINITION_SCHEMA",
    "OPRA_IMPORT_VERSION",
    "OPRA_METADATA_MAX_ATTEMPTS",
    "OPRA_METADATA_MAX_WORKERS",
    "OPRA_METADATA_TIMEOUT_SECONDS",
    "OPRA_PAID_DOWNLOAD_MAX_ATTEMPTS",
    "OPRA_STORAGE_EXPANSION_FACTOR",
    "OPRA_STORAGE_RESERVE_BYTES",
    "OPRA_TIMESERIES_TIMEOUT_SECONDS",
    "EstimatedOpraRequest",
    "OpraImportError",
    "OpraImportResult",
    "OpraRequest",
    "OpraSchedulePoint",
    "cbbo_requests",
    "cbbo_request_coverage_report",
    "configure_historical_client_timeouts",
    "definition_requests",
    "estimate_requests",
    "normalize_cbbo_records",
    "normalize_definition_records",
    "normalize_fixed_price",
    "opra_storage_capacity_report",
    "point_in_time_definition_asof",
    "read_opra_import",
    "research_benchmark_schedule_report",
    "resolve_market_schedule",
    "run_import_phase",
    "schedule_contract_report",
    "select_historical_source_target",
]
