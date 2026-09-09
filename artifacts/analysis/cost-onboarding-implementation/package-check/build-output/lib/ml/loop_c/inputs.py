from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass, fields
from pathlib import Path
from typing import Mapping, Sequence

import pandas as pd

from ml.artifacts import file_checksum, utc_timestamp
from ml.loop_c.policy import (
    LoopCPredictiveThresholds,
    LoopCRiskLimits,
    LoopCSequenceModelBinding,
)


LOOP_C_RISK_APPROVAL_SCHEMA_VERSION = "loop-c-risk-approval-v2"
LOOP_C_PORTFOLIO_SNAPSHOT_SCHEMA_VERSION = "loop-c-portfolio-snapshot-v2"
LOOP_C_BROKER_SNAPSHOT_SCHEMA_VERSION = "loop-c-broker-snapshot-v2"
LOOP_C_HALT_CONTROL_SCHEMA_VERSION = "loop-c-halt-control-v2"
LOOP_C_APPROVAL_SCOPE = "LOOP_C_OBSERVE_ONLY"
PORTFOLIO_SOURCE_POLICY_VERSION = "loop-c-schwab-read-only-snapshot-v1"
BROKER_SOURCE_POLICY_VERSION = "loop-c-schwab-read-only-snapshot-v1"
_SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
_TOKEN_PATTERN = re.compile(r"^[A-Za-z0-9_.:-]+$")


@dataclass(frozen=True)
class LoopCInputBundle:
    risk_limits: LoopCRiskLimits
    model_binding: LoopCSequenceModelBinding
    portfolio: Mapping[str, object]
    broker: Mapping[str, object]
    halt_requested: bool
    source_files: tuple[Path, ...]
    public_summary: Mapping[str, object]


def load_loop_c_inputs(
    datastore_root: Path,
    *,
    risk_limits_path: Path,
    portfolio_snapshot_path: Path,
    broker_snapshot_path: Path,
    halt_control_path: Path,
    as_of: object,
) -> LoopCInputBundle:
    """Load all four explicit inputs or fail closed before Loop C can run."""

    root = Path(datastore_root).resolve()
    now = utc_timestamp(as_of)
    risk_payload = _read_object(risk_limits_path, "risk approval")
    risk_limits, model_binding, approval = _risk_limits(risk_payload, as_of=now)
    maximum_age = float(risk_limits.maximum_snapshot_age_seconds)

    portfolio_payload = _read_object(portfolio_snapshot_path, "portfolio snapshot")
    portfolio, portfolio_source, portfolio_observed = _portfolio_snapshot(
        root,
        portfolio_payload,
        as_of=now,
        maximum_age_seconds=maximum_age,
    )
    broker_payload = _read_object(broker_snapshot_path, "broker snapshot")
    broker, broker_source, broker_observed = _broker_snapshot(
        root,
        broker_payload,
        as_of=now,
        maximum_age_seconds=maximum_age,
    )
    halt_payload = _read_object(halt_control_path, "halt control")
    halt_requested, halt_issued, halt_expires, halt_control_id = _halt_control(
        halt_payload,
        as_of=now,
    )
    source_files = tuple(
        dict.fromkeys(
            (
                Path(risk_limits_path).resolve(),
                Path(portfolio_snapshot_path).resolve(),
                portfolio_source,
                Path(broker_snapshot_path).resolve(),
                broker_source,
                Path(halt_control_path).resolve(),
            )
        )
    )
    summary = {
        "risk_approval_schema_version": LOOP_C_RISK_APPROVAL_SCHEMA_VERSION,
        "risk_approval_id": approval["approval_id"],
        "risk_approval_expires_at": approval["expires_at"],
        "risk_policy_version": risk_limits.policy_version,
        "model_binding_schema_version": model_binding.schema_version,
        "sequence_model_name": model_binding.model_name,
        "sequence_configuration_fingerprint": (
            model_binding.configuration_fingerprint
        ),
        "sequence_distribution_schema_version": (
            model_binding.distribution_schema_version
        ),
        "portfolio_snapshot_schema_version": (
            LOOP_C_PORTFOLIO_SNAPSHOT_SCHEMA_VERSION
        ),
        "portfolio_observed_at": portfolio_observed.isoformat(),
        "account_equity": portfolio["account_equity"],
        "gross_exposure": portfolio["gross_exposure"],
        "available_cash": portfolio["available_cash"],
        "trade_history_status": portfolio["trade_history_status"],
        "portfolio_source_receipt_sha256": file_checksum(portfolio_source),
        "broker_snapshot_schema_version": LOOP_C_BROKER_SNAPSHOT_SCHEMA_VERSION,
        "broker_observed_at": broker_observed.isoformat(),
        "working_orders": broker["working_orders"],
        "reserved_cash": broker["reserved_cash"],
        "broker_source_receipt_sha256": file_checksum(broker_source),
        "halt_control_schema_version": LOOP_C_HALT_CONTROL_SCHEMA_VERSION,
        "halt_control_id": halt_control_id,
        "halt_issued_at": halt_issued.isoformat(),
        "halt_expires_at": halt_expires.isoformat(),
        "validated_as_of": now.isoformat(),
    }
    return LoopCInputBundle(
        risk_limits=risk_limits,
        model_binding=model_binding,
        portfolio=portfolio,
        broker=broker,
        halt_requested=halt_requested,
        source_files=source_files,
        public_summary=summary,
    )


def _risk_limits(
    payload: Mapping[str, object],
    *,
    as_of: pd.Timestamp,
) -> tuple[LoopCRiskLimits, LoopCSequenceModelBinding, Mapping[str, object]]:
    _require_exact_keys(
        payload,
        {"schema_version", "approval", "model_binding", "limits"},
        "risk approval",
    )
    if payload["schema_version"] != LOOP_C_RISK_APPROVAL_SCHEMA_VERSION:
        raise ValueError("Unsupported Loop C risk approval schema")
    approval = payload["approval"]
    if not isinstance(approval, Mapping):
        raise ValueError("Loop C risk approval metadata must be an object")
    _require_exact_keys(
        approval,
        {
            "status",
            "approval_id",
            "approved_by",
            "approved_at",
            "expires_at",
            "scope",
            "rationale",
        },
        "risk approval metadata",
    )
    if approval["status"] != "APPROVED":
        raise ValueError("Loop C risk inputs do not have explicit APPROVED status")
    if approval["scope"] != LOOP_C_APPROVAL_SCOPE:
        raise ValueError("Loop C risk approval scope is not observe-only")
    _required_token(approval["approval_id"], "risk approval_id")
    _required_text(approval["approved_by"], "risk approved_by")
    _required_text(approval["rationale"], "risk approval rationale")
    approved_at = _required_timestamp(approval["approved_at"], "risk approved_at")
    expires_at = _required_timestamp(approval["expires_at"], "risk expires_at")
    if approved_at > as_of:
        raise ValueError("Loop C risk approval is future-dated")
    if expires_at <= as_of or expires_at <= approved_at:
        raise ValueError("Loop C risk approval is expired or has invalid chronology")

    model_binding = _model_binding(payload["model_binding"])
    limits = payload["limits"]
    if not isinstance(limits, Mapping):
        raise ValueError("Loop C risk limits must be an object")
    expected = {field.name for field in fields(LoopCRiskLimits)}
    _require_exact_keys(limits, expected, "risk limits")
    integer_limits = {
        "maximum_open_positions",
        "maximum_working_orders",
        "maximum_candidate_quantity",
    }
    normalized_limits: dict[str, object] = {}
    for name, value in limits.items():
        if name == "policy_version":
            normalized_limits[name] = _required_text(value, "risk policy_version")
        elif name == "predictive_thresholds_by_horizon":
            normalized_limits[name] = _predictive_thresholds(value)
        elif name in integer_limits:
            normalized_limits[name] = _positive_integer(value, f"risk {name}")
        else:
            normalized_limits[name] = _finite_number(value, f"risk {name}")
    return LoopCRiskLimits(**normalized_limits), model_binding, dict(approval)


def _model_binding(value: object) -> LoopCSequenceModelBinding:
    if not isinstance(value, Mapping):
        raise ValueError("Loop C model_binding must be an object")
    expected = {field.name for field in fields(LoopCSequenceModelBinding)}
    _require_exact_keys(value, expected, "Loop C model binding")
    horizons = value["horizons"]
    if not isinstance(horizons, Sequence) or isinstance(horizons, (str, bytes)):
        raise ValueError("Loop C model-binding horizons must be an array")
    return LoopCSequenceModelBinding(
        schema_version=_required_text(
            value["schema_version"], "model binding schema_version"
        ),
        model_name=_required_text(value["model_name"], "model binding model_name"),
        sequence_policy_version=_required_text(
            value["sequence_policy_version"], "model binding sequence_policy_version"
        ),
        configuration_fingerprint=str(value["configuration_fingerprint"]).lower(),
        distribution_schema_version=_required_text(
            value["distribution_schema_version"],
            "model binding distribution_schema_version",
        ),
        required_authority=_required_text(
            value["required_authority"], "model binding required_authority"
        ).upper(),
        consumer=_required_text(value["consumer"], "model binding consumer").upper(),
        horizons=tuple(str(item) for item in horizons),
    )


def _predictive_thresholds(value: object) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise ValueError("predictive_thresholds_by_horizon must be an object")
    expected_fields = {field.name for field in fields(LoopCPredictiveThresholds)}
    normalized: dict[str, object] = {}
    for raw_horizon, raw_thresholds in value.items():
        horizon = str(raw_horizon).strip()
        if not isinstance(raw_thresholds, Mapping):
            raise ValueError(f"predictive thresholds for {horizon} must be an object")
        _require_exact_keys(
            raw_thresholds,
            expected_fields,
            f"predictive thresholds for {horizon}",
        )
        normalized[horizon] = {
            name: _finite_number(raw_thresholds[name], f"{horizon} {name}")
            for name in expected_fields
        }
    return normalized


def _portfolio_snapshot(
    root: Path,
    payload: Mapping[str, object],
    *,
    as_of: pd.Timestamp,
    maximum_age_seconds: float,
) -> tuple[Mapping[str, object], Path, pd.Timestamp]:
    _require_exact_keys(
        payload,
        {
            "schema_version",
            "authority",
            "observed_at",
            "reconciled",
            "account_equity",
            "daily_pnl",
            "gross_exposure",
            "symbol_exposure",
            "open_positions",
            "available_cash",
            "available_cash_source",
            "trade_history_status",
            "source",
        },
        "portfolio snapshot",
    )
    if payload["schema_version"] != LOOP_C_PORTFOLIO_SNAPSHOT_SCHEMA_VERSION:
        raise ValueError("Unsupported Loop C portfolio snapshot schema")
    if payload["authority"] != "OBSERVED_READ_ONLY":
        raise ValueError("Loop C portfolio snapshot is not observed read-only state")
    if payload["reconciled"] is not True:
        raise ValueError("Loop C portfolio snapshot is not reconciled")
    observed = _fresh_timestamp(
        payload["observed_at"],
        "portfolio observed_at",
        as_of=as_of,
        maximum_age_seconds=maximum_age_seconds,
    )
    daily_pnl = _finite_number(payload["daily_pnl"], "portfolio daily_pnl")
    account_equity = _positive_number(
        payload["account_equity"], "portfolio account_equity"
    )
    gross_exposure = _nonnegative_number(
        payload["gross_exposure"], "portfolio gross_exposure"
    )
    available_cash = _nonnegative_number(
        payload["available_cash"], "portfolio available_cash"
    )
    open_positions = _nonnegative_integer(
        payload["open_positions"], "portfolio open_positions"
    )
    available_cash_source = _required_token(
        payload["available_cash_source"], "portfolio available_cash_source"
    )
    trade_history_status = _required_token(
        payload["trade_history_status"], "portfolio trade_history_status"
    )
    raw_symbol_exposure = payload["symbol_exposure"]
    if not isinstance(raw_symbol_exposure, Mapping):
        raise ValueError("portfolio symbol_exposure must be an object")
    symbol_exposure: dict[str, float] = {}
    for raw_symbol, raw_value in raw_symbol_exposure.items():
        symbol = str(raw_symbol).strip().upper()
        if not symbol or symbol in symbol_exposure:
            raise ValueError("portfolio symbol_exposure keys must be unique symbols")
        symbol_exposure[symbol] = _nonnegative_number(
            raw_value,
            f"portfolio symbol_exposure[{symbol}]",
        )
    source = _source_receipt(
        root,
        payload["source"],
        expected_policy=PORTFOLIO_SOURCE_POLICY_VERSION,
        label="portfolio source",
    )
    return (
        {
            "reconciled": True,
            "observed_at": observed,
            "account_equity": account_equity,
            "daily_pnl": daily_pnl,
            "gross_exposure": gross_exposure,
            "symbol_exposure": symbol_exposure,
            "open_positions": open_positions,
            "available_cash": available_cash,
            "available_cash_source": available_cash_source,
            "trade_history_status": trade_history_status,
        },
        source,
        observed,
    )


def _broker_snapshot(
    root: Path,
    payload: Mapping[str, object],
    *,
    as_of: pd.Timestamp,
    maximum_age_seconds: float,
) -> tuple[Mapping[str, object], Path, pd.Timestamp]:
    _require_exact_keys(
        payload,
        {
            "schema_version",
            "authority",
            "observed_at",
            "reconciled",
            "working_orders",
            "reserved_cash",
            "unknown_submission_status",
            "source",
        },
        "broker snapshot",
    )
    if payload["schema_version"] != LOOP_C_BROKER_SNAPSHOT_SCHEMA_VERSION:
        raise ValueError("Unsupported Loop C broker snapshot schema")
    if payload["authority"] != "OBSERVED_READ_ONLY":
        raise ValueError("Loop C broker snapshot is not observed read-only state")
    if payload["reconciled"] is not True:
        raise ValueError("Loop C broker snapshot is not reconciled")
    if not isinstance(payload["unknown_submission_status"], bool):
        raise ValueError("broker unknown_submission_status must be boolean")
    observed = _fresh_timestamp(
        payload["observed_at"],
        "broker observed_at",
        as_of=as_of,
        maximum_age_seconds=maximum_age_seconds,
    )
    source = _source_receipt(
        root,
        payload["source"],
        expected_policy=BROKER_SOURCE_POLICY_VERSION,
        label="broker source",
    )
    return (
        {
            "reconciled": True,
            "observed_at": observed,
            "working_orders": _nonnegative_integer(
                payload["working_orders"], "broker working_orders"
            ),
            "reserved_cash": _nonnegative_number(
                payload["reserved_cash"], "broker reserved_cash"
            ),
            "unknown_submission_status": payload["unknown_submission_status"],
        },
        source,
        observed,
    )


def _halt_control(
    payload: Mapping[str, object],
    *,
    as_of: pd.Timestamp,
) -> tuple[bool, pd.Timestamp, pd.Timestamp, str]:
    _require_exact_keys(
        payload,
        {
            "schema_version",
            "control_id",
            "issued_at",
            "expires_at",
            "halt_requested",
            "set_by",
        },
        "halt control",
    )
    if payload["schema_version"] != LOOP_C_HALT_CONTROL_SCHEMA_VERSION:
        raise ValueError("Unsupported Loop C halt-control schema")
    if not isinstance(payload["halt_requested"], bool):
        raise ValueError("halt_requested must be boolean")
    control_id = _required_token(payload["control_id"], "halt control_id")
    _required_text(payload["set_by"], "halt set_by")
    issued = _required_timestamp(payload["issued_at"], "halt issued_at")
    expires = _required_timestamp(payload["expires_at"], "halt expires_at")
    if issued > as_of:
        raise ValueError("Loop C halt control is future-dated")
    if expires <= as_of or expires <= issued:
        raise ValueError("Loop C halt control is expired or has invalid chronology")
    return bool(payload["halt_requested"]), issued, expires, control_id


def _source_receipt(
    root: Path,
    value: object,
    *,
    expected_policy: str,
    label: str,
) -> Path:
    if not isinstance(value, Mapping):
        raise ValueError(f"{label} must be an object")
    _require_exact_keys(
        value,
        {"policy_version", "receipt_path", "receipt_sha256"},
        label,
    )
    if value["policy_version"] != expected_policy:
        raise ValueError(f"{label} policy version changed")
    relative = Path(_required_text(value["receipt_path"], f"{label} receipt_path"))
    receipt = relative.resolve() if relative.is_absolute() else (root / relative).resolve()
    if not receipt.is_relative_to(root):
        raise ValueError(f"{label} receipt escapes the datastore")
    checksum = str(value["receipt_sha256"]).lower()
    if not _SHA256_PATTERN.fullmatch(checksum):
        raise ValueError(f"{label} receipt_sha256 is invalid")
    if not receipt.is_file() or file_checksum(receipt) != checksum:
        raise ValueError(f"{label} receipt is missing or changed")
    return receipt


def _read_object(path: Path, label: str) -> Mapping[str, object]:
    source = Path(path)
    try:
        payload = json.loads(source.read_text(encoding="utf-8"))
    except (OSError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise ValueError(f"{label} is unreadable: {source}") from exc
    if not isinstance(payload, Mapping):
        raise ValueError(f"{label} must be a JSON object: {source}")
    return payload


def _require_exact_keys(
    value: Mapping[str, object],
    expected: set[str],
    label: str,
) -> None:
    if set(value) != expected:
        missing = sorted(expected.difference(value))
        extra = sorted(set(value).difference(expected))
        raise ValueError(f"{label} fields differ; missing={missing}, extra={extra}")


def _fresh_timestamp(
    value: object,
    label: str,
    *,
    as_of: pd.Timestamp,
    maximum_age_seconds: float,
) -> pd.Timestamp:
    observed = _required_timestamp(value, label)
    age = (as_of - observed).total_seconds()
    if age < 0.0:
        raise ValueError(f"{label} is future-dated")
    if age > maximum_age_seconds:
        raise ValueError(f"{label} exceeds the approved snapshot age")
    return observed


def _required_timestamp(value: object, label: str) -> pd.Timestamp:
    if value is None or not str(value).strip():
        raise ValueError(f"{label} must be an ISO-8601 timestamp")
    parsed = pd.to_datetime(value, utc=True, errors="coerce")
    if pd.isna(parsed):
        raise ValueError(f"{label} must be an ISO-8601 timestamp")
    return utc_timestamp(parsed)


def _finite_number(value: object, label: str) -> float:
    if isinstance(value, bool):
        raise ValueError(f"{label} must be numeric")
    try:
        parsed = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{label} must be numeric") from exc
    if not math.isfinite(parsed):
        raise ValueError(f"{label} must be finite")
    return parsed


def _nonnegative_number(value: object, label: str) -> float:
    parsed = _finite_number(value, label)
    if parsed < 0.0:
        raise ValueError(f"{label} cannot be negative")
    return parsed


def _positive_number(value: object, label: str) -> float:
    parsed = _finite_number(value, label)
    if parsed <= 0.0:
        raise ValueError(f"{label} must be positive")
    return parsed


def _nonnegative_integer(value: object, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{label} must be an integer")
    if value < 0:
        raise ValueError(f"{label} must be a non-negative integer")
    return value


def _positive_integer(value: object, label: str) -> int:
    parsed = _nonnegative_integer(value, label)
    if parsed < 1:
        raise ValueError(f"{label} must be positive")
    return parsed


def _required_text(value: object, label: str) -> str:
    text = str(value).strip() if value is not None else ""
    if not text:
        raise ValueError(f"{label} must not be empty")
    if len(text) > 512:
        raise ValueError(f"{label} is too long")
    return text


def _required_token(value: object, label: str) -> str:
    text = _required_text(value, label)
    if not _TOKEN_PATTERN.fullmatch(text):
        raise ValueError(f"{label} contains unsupported characters")
    return text


__all__ = [
    "BROKER_SOURCE_POLICY_VERSION",
    "LOOP_C_APPROVAL_SCOPE",
    "LOOP_C_BROKER_SNAPSHOT_SCHEMA_VERSION",
    "LOOP_C_HALT_CONTROL_SCHEMA_VERSION",
    "LOOP_C_PORTFOLIO_SNAPSHOT_SCHEMA_VERSION",
    "LOOP_C_RISK_APPROVAL_SCHEMA_VERSION",
    "LoopCInputBundle",
    "PORTFOLIO_SOURCE_POLICY_VERSION",
    "load_loop_c_inputs",
]
