from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Mapping, Sequence

import pandas as pd

from ml.artifacts import utc_timestamp
from ml.option_pricing.target_outcome import (
    TargetOutcomeError,
    TargetOutcomePublication,
    read_target_outcome,
)


PRICING_BARRIER_VERSION = "pricing-options-barrier-v1"


@dataclass(frozen=True)
class PricingBarrierObservation:
    target_snapshot_for: pd.Timestamp
    status: str
    observed_at: pd.Timestamp
    deadline_at: pd.Timestamp
    terminal_status: str | None = None
    pricing_run_path: str | None = None
    pricing_receipt_checksum_sha256: str | None = None
    pricing_published_at: pd.Timestamp | None = None
    detail: str = ""

    @property
    def verified(self) -> bool:
        return self.status == "VERIFIED"

    def as_receipt_metadata(self, *, request_started_at: object) -> dict[str, object]:
        request = utc_timestamp(request_started_at)
        observed_before_request = self.observed_at <= request
        authority_before_request = bool(
            self.verified
            and self.pricing_published_at is not None
            and self.pricing_published_at <= request
            and observed_before_request
        )
        prediction_outcome = self.terminal_status in {
            "PREDICTIONS_PUBLISHED",
            "MIXED_TERMINAL",
        }
        prospective_credit_allowed = bool(
            authority_before_request and prediction_outcome
        )
        return {
            "schema_version": PRICING_BARRIER_VERSION,
            "target_snapshot_for": self.target_snapshot_for.isoformat(),
            "status": self.status,
            "observed_at": self.observed_at.isoformat(),
            "deadline_at": self.deadline_at.isoformat(),
            "terminal_status": self.terminal_status,
            "pricing_run_path": self.pricing_run_path,
            "pricing_receipt_checksum_sha256": self.pricing_receipt_checksum_sha256,
            "pricing_published_at": (
                self.pricing_published_at.isoformat()
                if self.pricing_published_at is not None
                else None
            ),
            "observed_before_request": observed_before_request,
            "authority_before_request": authority_before_request,
            "prospective_credit_allowed": prospective_credit_allowed,
            "detail": self.detail,
        }


def wait_for_pricing_barrier(
    datastore_root: Path,
    *,
    target_snapshot_for: object,
    required_symbols: Sequence[str],
    timeout_seconds: float,
    clock: Callable[[], object] | None = None,
    sleeper: Callable[[float], None] = time.sleep,
    poll_seconds: float = 0.25,
) -> PricingBarrierObservation:
    """Wait a bounded interval for the verified Pricing authority for one target."""

    if timeout_seconds < 0:
        raise ValueError("Pricing barrier timeout cannot be negative")
    if poll_seconds <= 0:
        raise ValueError("Pricing barrier poll interval must be positive")
    now = clock or utc_timestamp
    started_at = utc_timestamp(now())
    deadline_at = started_at + pd.Timedelta(seconds=float(timeout_seconds))
    monotonic_deadline = time.monotonic() + float(timeout_seconds)
    last_error = ""
    while True:
        try:
            publication = read_target_outcome(
                datastore_root,
                target_snapshot_for=target_snapshot_for,
            )
            _validate_scope(publication, required_symbols=required_symbols)
            observed_at = max(utc_timestamp(now()), publication.published_at)
            return PricingBarrierObservation(
                target_snapshot_for=publication.target_snapshot_for,
                status="VERIFIED",
                observed_at=observed_at,
                deadline_at=deadline_at,
                terminal_status=publication.terminal_status,
                pricing_run_path=str(publication.receipt.get("run_path", "")),
                pricing_receipt_checksum_sha256=publication.receipt_checksum_sha256,
                pricing_published_at=publication.published_at,
            )
        except TargetOutcomeError as exc:
            last_error = f"{type(exc).__name__}: {exc}"
        except Exception as exc:
            last_error = f"{type(exc).__name__}: {exc}"
        remaining = monotonic_deadline - time.monotonic()
        if remaining <= 0:
            observed_at = utc_timestamp(now())
            return PricingBarrierObservation(
                target_snapshot_for=utc_timestamp(target_snapshot_for),
                status="MISSING" if timeout_seconds == 0 else "TIMED_OUT",
                observed_at=observed_at,
                deadline_at=deadline_at,
                detail=last_error,
            )
        sleeper(min(float(poll_seconds), remaining))


def verify_pricing_barrier_metadata(
    value: object,
    *,
    target_snapshot_for: object,
    request_started_at: object,
) -> Mapping[str, object] | None:
    """Fail closed when an Options receipt's barrier proof is malformed."""

    if value is None:
        return None
    if not isinstance(value, Mapping):
        raise ValueError("Options Pricing barrier metadata must be an object")
    target = utc_timestamp(target_snapshot_for)
    request = utc_timestamp(request_started_at)
    observed = utc_timestamp(value.get("observed_at"))
    deadline = utc_timestamp(value.get("deadline_at"))
    status = str(value.get("status", ""))
    if (
        value.get("schema_version") != PRICING_BARRIER_VERSION
        or utc_timestamp(value.get("target_snapshot_for")) != target
        or observed > request
        or status not in {"VERIFIED", "MISSING", "TIMED_OUT", "FAILED", "INVALID"}
    ):
        raise ValueError("Options Pricing barrier metadata is invalid")
    verified = status == "VERIFIED"
    published = (
        utc_timestamp(value.get("pricing_published_at"))
        if value.get("pricing_published_at") is not None
        else None
    )
    proof_fields = (
        value.get("pricing_run_path"),
        value.get("pricing_receipt_checksum_sha256"),
        value.get("terminal_status"),
    )
    authority_before_request = bool(
        verified and published is not None and published <= request and observed <= request
    )
    prediction_outcome = value.get("terminal_status") in {
        "PREDICTIONS_PUBLISHED",
        "MIXED_TERMINAL",
    }
    prospective_credit_allowed = bool(
        authority_before_request and prediction_outcome
    )
    if (
        verified != all(isinstance(item, str) and item for item in proof_fields)
        or bool(value.get("observed_before_request")) != (observed <= request)
        or bool(value.get("authority_before_request")) != authority_before_request
        or bool(value.get("prospective_credit_allowed"))
        != prospective_credit_allowed
    ):
        raise ValueError("Options Pricing barrier proof is incoherent")
    return dict(value)


def _validate_scope(
    publication: TargetOutcomePublication,
    *,
    required_symbols: Sequence[str],
) -> None:
    required = tuple(
        dict.fromkeys(
            str(value).strip().upper() for value in required_symbols if str(value).strip()
        )
    )
    if any(symbol not in publication.symbol_outcomes for symbol in required):
        raise TargetOutcomeError(
            "Pricing target authority does not cover the Options symbol scope"
        )


__all__ = [
    "PRICING_BARRIER_VERSION",
    "PricingBarrierObservation",
    "verify_pricing_barrier_metadata",
    "wait_for_pricing_barrier",
]
