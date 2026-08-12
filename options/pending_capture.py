from __future__ import annotations

import json
import hashlib
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

import pandas as pd

from datafetching.bar_readiness import BarReadinessError, read_bar_readiness
from datafetching.decision_time import (
    DecisionClock,
    completed_bar_clock_for_target,
    expected_quarter_hour_target,
    is_eligible_option_target,
)
from datafetching.layout import safe_token
from datafetching.pricing_barrier import verify_pricing_barrier_metadata
from ml.artifacts import file_checksum
from ml.option_pricing.policies import ContractSelectionPolicy
from options import OptionSnapshotOutput
from options.publication import committed_option_snapshots, read_option_snapshot


PENDING_OPTION_REQUEST_VERSION = "option-pending-request-v1"
PENDING_OPTION_CAPTURE_VERSION = "option-pending-capture-v1"
PENDING_OPTION_TERMINAL_VERSION = "option-pending-terminal-v1"
PENDING_OPTION_RECONCILIATION_VERSION = "option-pending-reconciliation-v1"
PENDING_OPTION_CAUSAL_WINDOW_SECONDS = (
    ContractSelectionPolicy().maximum_source_staleness_seconds
)


class PendingOptionCaptureError(RuntimeError):
    """A quarantined Options request/capture failed strict verification."""


@dataclass(frozen=True)
class PendingOptionRequest:
    symbol: str
    target_snapshot_for: pd.Timestamp
    request_started_at: pd.Timestamp
    required_symbols: tuple[str, ...]
    bar_readiness_mode: str
    regime_available_not_after: pd.Timestamp
    pricing_barrier: Mapping[str, object]
    directory: Path
    request_path: Path


@dataclass(frozen=True)
class PendingOptionCapture:
    request: PendingOptionRequest
    response_received_at: pd.Timestamp
    fetched_at: pd.Timestamp
    provider_quote_timestamps: tuple[pd.Timestamp, ...]
    capture_path: Path
    payload_checksum_sha256: str

    def payload(self) -> Mapping[str, Any]:
        envelope = _read_mapping(
            self.capture_path,
            label="Pending Options capture",
        )
        value = envelope.get("raw_payload")
        if not isinstance(value, Mapping):
            raise PendingOptionCaptureError(
                f"Pending Options payload is not an object: {self.capture_path}"
            )
        encoded = _json_bytes(value)
        try:
            payload_size = int(envelope.get("raw_payload_size", -1))
        except (TypeError, ValueError) as exc:
            raise PendingOptionCaptureError(
                f"Pending Options raw payload size is invalid: {self.capture_path}"
            ) from exc
        if (
            payload_size != len(encoded)
            or envelope.get("raw_payload_checksum_sha256")
            != _bytes_checksum(encoded)
        ):
            raise PendingOptionCaptureError(
                f"Pending Options raw payload checksum is invalid: {self.capture_path}"
            )
        return dict(value)


@dataclass(frozen=True)
class PendingReconciliationSummary:
    pending: int = 0
    reconciled: int = 0
    expired: int = 0
    failed: int = 0
    newly_reconciled: int = 0


def pending_option_capture_root(datastore_root: Path) -> Path:
    return Path(datastore_root) / "options" / "pending-captures" / "schwab"


def pending_option_capture_directory(
    datastore_root: Path,
    *,
    symbol: str,
    target_snapshot_for: object,
) -> Path:
    target = _utc(target_snapshot_for, "target_snapshot_for")
    return (
        pending_option_capture_root(datastore_root)
        / str(target.value)
        / safe_token(str(symbol).strip().upper())
    )


def begin_pending_option_request(
    datastore_root: Path,
    *,
    symbol: str,
    target_snapshot_for: object,
    request_started_at: object,
    required_symbols: Sequence[str],
    bar_readiness_mode: str,
    regime_available_not_after: object,
    pricing_barrier: Mapping[str, object],
) -> tuple[PendingOptionRequest, bool]:
    """Durably claim one target/symbol before its sole provider request."""

    root = Path(datastore_root).resolve()
    clean_symbol = str(symbol).strip().upper()
    target = _utc(target_snapshot_for, "target_snapshot_for")
    request_at = _utc(request_started_at, "request_started_at")
    scope = _symbols(required_symbols)
    readiness_mode = str(bar_readiness_mode).strip().lower()
    regime_cutoff = _utc(
        regime_available_not_after,
        "regime_available_not_after",
    )
    if not clean_symbol or clean_symbol not in scope:
        raise ValueError("Pending Options request symbol must be in its exact scope")
    if target != expected_quarter_hour_target(target) or not is_eligible_option_target(
        target
    ):
        raise ValueError("Pending Options request target is not calendar-actionable")
    if request_at < target:
        raise ValueError("Pending Options request cannot precede its completed target")
    if request_at > target + pd.Timedelta(
        seconds=PENDING_OPTION_CAUSAL_WINDOW_SECONDS
    ):
        raise ValueError("Pending Options request is outside its causal window")
    if readiness_mode not in {"required", "exact"}:
        raise ValueError("Pending Options readiness mode must be required or exact")
    if regime_cutoff > request_at:
        raise ValueError("Pending Options regime cutoff follows its request")
    verified_barrier = verify_pricing_barrier_metadata(
        pricing_barrier,
        target_snapshot_for=target,
        request_started_at=request_at,
    )
    if verified_barrier is None:
        raise ValueError("Pending Options request requires explicit Pricing barrier status")

    destination = pending_option_capture_directory(
        root,
        symbol=clean_symbol,
        target_snapshot_for=target,
    )
    if destination.exists():
        return read_pending_option_request(destination), False
    destination.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(
        tempfile.mkdtemp(
            prefix=f".{destination.name}.request-{os.getpid()}-",
            dir=destination.parent,
        )
    )
    payload = {
        "schema_version": PENDING_OPTION_REQUEST_VERSION,
        "status": "REQUEST_STARTED",
        "symbol": clean_symbol,
        "target_snapshot_for": target.isoformat(),
        "request_started_at": request_at.isoformat(),
        "required_symbols": list(scope),
        "bar_readiness_mode": readiness_mode,
        "regime_available_not_after": regime_cutoff.isoformat(),
        "pricing_barrier": dict(verified_barrier),
        "automated_action_allowed": False,
    }
    try:
        _write_json(staging / "request.json", payload)
        try:
            staging.replace(destination)
        except FileExistsError:
            _remove_private_directory(staging)
            return read_pending_option_request(destination), False
    except BaseException:
        _remove_private_directory(staging)
        raise
    return read_pending_option_request(destination), True


def read_pending_option_request(directory: Path) -> PendingOptionRequest:
    run = Path(directory).resolve()
    path = run / "request.json"
    value = _read_mapping(path, label="Pending Options request")
    symbol = str(value.get("symbol", "")).strip().upper()
    target = _utc(value.get("target_snapshot_for"), "target_snapshot_for")
    requested = _utc(value.get("request_started_at"), "request_started_at")
    scope = _symbols(value.get("required_symbols"))
    mode = str(value.get("bar_readiness_mode", "")).strip().lower()
    regime_cutoff = _utc(
        value.get("regime_available_not_after"),
        "regime_available_not_after",
    )
    expected = pending_option_capture_directory(
        _datastore_root_from_pending_directory(run),
        symbol=symbol,
        target_snapshot_for=target,
    ).resolve()
    try:
        barrier = verify_pricing_barrier_metadata(
            value.get("pricing_barrier"),
            target_snapshot_for=target,
            request_started_at=requested,
        )
    except Exception as exc:
        raise PendingOptionCaptureError(
            f"Pending Options request Pricing barrier is invalid: {path}"
        ) from exc
    if (
        value.get("schema_version") != PENDING_OPTION_REQUEST_VERSION
        or value.get("status") != "REQUEST_STARTED"
        or value.get("automated_action_allowed") is not False
        or not symbol
        or symbol not in scope
        or mode not in {"required", "exact"}
        or barrier is None
        or requested < target
        or requested
        > target + pd.Timedelta(seconds=PENDING_OPTION_CAUSAL_WINDOW_SECONDS)
        or regime_cutoff > requested
        or target != expected_quarter_hour_target(target)
        or not is_eligible_option_target(target)
        or run != expected
    ):
        raise PendingOptionCaptureError(
            f"Pending Options request identity or contract is invalid: {path}"
        )
    return PendingOptionRequest(
        symbol=symbol,
        target_snapshot_for=target,
        request_started_at=requested,
        required_symbols=scope,
        bar_readiness_mode=mode,
        regime_available_not_after=regime_cutoff,
        pricing_barrier=dict(barrier),
        directory=run,
        request_path=path,
    )


def complete_pending_option_capture(
    request: PendingOptionRequest,
    *,
    payload: Mapping[str, Any],
    response_received_at: object,
) -> PendingOptionCapture:
    """Seal a provider response under the non-production pending authority."""

    response_at = _utc(response_received_at, "response_received_at")
    if response_at < request.request_started_at:
        raise ValueError("Pending Options response cannot precede its request")
    _validate_payload_symbol(payload, expected_symbol=request.symbol)
    capture_path = request.directory / "capture.json"
    encoded = _json_bytes(payload)
    checksum = _bytes_checksum(encoded)
    if capture_path.is_file():
        existing = read_pending_option_capture(request.directory)
        if (
            existing.payload_checksum_sha256 != checksum
            or existing.response_received_at != response_at
        ):
            raise PendingOptionCaptureError(
                "Pending Options response conflicts with the immutable capture"
            )
        return existing
    capture = {
        "schema_version": PENDING_OPTION_CAPTURE_VERSION,
        "status": "PENDING_READINESS",
        "symbol": request.symbol,
        "target_snapshot_for": request.target_snapshot_for.isoformat(),
        "request_started_at": request.request_started_at.isoformat(),
        "response_received_at": response_at.isoformat(),
        "fetched_at": response_at.isoformat(),
        "provider_quote_timestamps": [
            value.isoformat() for value in _provider_quote_timestamps(payload)
        ],
        "raw_payload": dict(payload),
        "raw_payload_size": len(encoded),
        "raw_payload_checksum_sha256": checksum,
        "request_checksum_sha256": file_checksum(request.request_path),
        "pricing_barrier": dict(request.pricing_barrier),
        "automated_action_allowed": False,
    }
    _write_json_atomic(capture_path, capture)
    return read_pending_option_capture(request.directory)


def read_pending_option_capture(directory: Path) -> PendingOptionCapture:
    request = read_pending_option_request(directory)
    path = request.directory / "capture.json"
    value = _read_mapping(path, label="Pending Options capture")
    response = _utc(value.get("response_received_at"), "response_received_at")
    fetched = _utc(value.get("fetched_at"), "fetched_at")
    quote_values = value.get("provider_quote_timestamps")
    if isinstance(quote_values, (str, bytes)) or not isinstance(
        quote_values, Sequence
    ):
        raise PendingOptionCaptureError(
            f"Pending Options quote timestamp inventory is invalid: {path}"
        )
    quote_timestamps = tuple(
        _utc(item, "provider_quote_timestamp") for item in quote_values
    )
    if (
        value.get("schema_version") != PENDING_OPTION_CAPTURE_VERSION
        or value.get("status") != "PENDING_READINESS"
        or value.get("automated_action_allowed") is not False
        or str(value.get("symbol", "")).strip().upper() != request.symbol
        or _utc(value.get("target_snapshot_for"), "capture target")
        != request.target_snapshot_for
        or _utc(value.get("request_started_at"), "capture request")
        != request.request_started_at
        or response < request.request_started_at
        or fetched != response
        or value.get("request_checksum_sha256") != file_checksum(request.request_path)
        or value.get("pricing_barrier") != dict(request.pricing_barrier)
    ):
        raise PendingOptionCaptureError(
            f"Pending Options capture failed checksum or identity verification: {path}"
        )
    capture = PendingOptionCapture(
        request=request,
        response_received_at=response,
        fetched_at=fetched,
        provider_quote_timestamps=quote_timestamps,
        capture_path=path,
        payload_checksum_sha256=str(value.get("raw_payload_checksum_sha256", "")),
    )
    sealed_payload = capture.payload()
    _validate_payload_symbol(sealed_payload, expected_symbol=request.symbol)
    if quote_timestamps != _provider_quote_timestamps(sealed_payload):
        raise PendingOptionCaptureError(
            f"Pending Options quote timestamp inventory disagrees with payload: {path}"
        )
    return capture


def record_pending_request_failure(
    request: PendingOptionRequest,
    *,
    failed_at: object,
    exc: Exception,
) -> None:
    _publish_terminal(
        request,
        status="REQUEST_FAILED",
        terminal_at=_utc(failed_at, "failed_at"),
        detail=f"{type(exc).__name__}: {exc}",
    )


def reconcile_pending_option_captures(
    datastore_root: Path,
    *,
    reconciled_at: object,
    persist: Callable[..., OptionSnapshotOutput],
    acquire_writer_lock: bool = True,
) -> PendingReconciliationSummary:
    """Verify and promote causally eligible captures, including during closure."""

    root = Path(datastore_root).resolve()
    observed = _utc(reconciled_at, "reconciled_at")
    counters = {
        "pending": 0,
        "reconciled": 0,
        "expired": 0,
        "failed": 0,
        "newly_reconciled": 0,
    }
    for request_path in sorted(
        pending_option_capture_root(root).glob("*/*/request.json")
    ):
        request = read_pending_option_request(request_path.parent)
        reconciliation_path = request.directory / "reconciled.json"
        terminal_path = request.directory / "terminal.json"
        if reconciliation_path.is_file():
            _verify_reconciliation(root, request, reconciliation_path)
            counters["reconciled"] += 1
            continue
        if terminal_path.is_file():
            status = _verify_terminal(request, terminal_path)
            counters["expired" if status.startswith("EXPIRED_") else "failed"] += 1
            continue

        deadline = request.target_snapshot_for + pd.Timedelta(
            seconds=PENDING_OPTION_CAUSAL_WINDOW_SECONDS
        )
        capture_path = request.directory / "capture.json"
        if not capture_path.is_file():
            if observed > deadline:
                _publish_terminal(
                    request,
                    status="EXPIRED_INCOMPLETE_REQUEST",
                    terminal_at=observed,
                    detail="The durable request claim has no sealed provider response.",
                )
                counters["expired"] += 1
            else:
                counters["pending"] += 1
            continue
        capture = read_pending_option_capture(request.directory)

        committed = tuple(
            snapshot
            for snapshot in committed_option_snapshots(
                root,
                symbol=request.symbol,
            )
            if snapshot.snapshot_for == request.target_snapshot_for
        )
        if len(committed) > 1:
            raise PendingOptionCaptureError(
                "More than one committed Options snapshot owns a pending target/symbol"
            )
        if committed:
            _verify_committed_capture(root, capture, committed[0])
            _publish_reconciliation(
                root,
                capture,
                snapshot_directory=committed[0].directory,
                receipt_path=committed[0].receipt_path,
                reconciled_at=max(observed, committed[0].available_at),
                readiness=None,
            )
            counters["reconciled"] += 1
            continue
        if capture.response_received_at > deadline:
            _publish_terminal(
                request,
                status="EXPIRED_RESPONSE",
                terminal_at=observed,
                detail="The Schwab response arrived outside the 1,200-second causal window.",
            )
            counters["expired"] += 1
            continue
        if observed > deadline:
            _publish_terminal(
                request,
                status="EXPIRED_RECONCILIATION",
                terminal_at=observed,
                detail="Reconciliation occurred outside the 1,200-second causal window.",
            )
            counters["expired"] += 1
            continue

        clock, readiness_metadata, ready_at = _reconciliation_clock(
            root,
            request=request,
            observed_at=observed,
        )
        if clock is None:
            counters["pending"] += 1
            continue
        assert ready_at is not None
        if ready_at > deadline:
            _publish_terminal(
                request,
                status="EXPIRED_READINESS",
                terminal_at=observed,
                detail="Exact Loop A readiness arrived outside the causal window.",
            )
            counters["expired"] += 1
            continue
        canonical_available = max(
            capture.response_received_at,
            ready_at,
            observed,
        )
        if canonical_available > deadline:
            _publish_terminal(
                request,
                status="EXPIRED_CANONICAL_AVAILABILITY",
                terminal_at=observed,
                detail="Canonical availability would renew an expired target.",
            )
            counters["expired"] += 1
            continue
        output = persist(
            root,
            symbol=request.symbol,
            payload=capture.payload(),
            clock=clock,
            fetched_at=canonical_available,
            quote_cutoff_at=request.request_started_at,
            regime_available_not_after=request.regime_available_not_after,
            pricing_barrier=request.pricing_barrier,
            receipt_published_at=canonical_available,
            capture_provenance={
                "pending_capture_path": capture.request.directory.relative_to(
                    root
                ).as_posix(),
                "pending_capture_checksum_sha256": file_checksum(capture.capture_path),
                "response_received_at": capture.response_received_at.isoformat(),
                "readiness_ready_at": ready_at.isoformat(),
                "reconciled_at": observed.isoformat(),
            },
            acquire_writer_lock=acquire_writer_lock,
        )
        if output.receipt_path is None or not output.receipt_path.is_file():
            raise PendingOptionCaptureError(
                "Pending Options reconciliation did not produce a committed receipt"
            )
        snapshot = read_option_snapshot(output.receipt_path.parent)
        if (
            snapshot.symbol != request.symbol
            or snapshot.snapshot_for != request.target_snapshot_for
            or snapshot.available_at != canonical_available
        ):
            raise PendingOptionCaptureError(
                "Reconciled Options snapshot disagrees with its pending capture"
            )
        _verify_committed_capture(root, capture, snapshot)
        _publish_reconciliation(
            root,
            capture,
            snapshot_directory=snapshot.directory,
            receipt_path=snapshot.receipt_path,
            reconciled_at=canonical_available,
            readiness=readiness_metadata,
        )
        counters["reconciled"] += 1
        counters["newly_reconciled"] += 1
    return PendingReconciliationSummary(**counters)


def pending_option_capture_counts(datastore_root: Path) -> PendingReconciliationSummary:
    """Read and verify pending inventory without reconciling or writing it."""

    root = Path(datastore_root).resolve()
    counters = {"pending": 0, "reconciled": 0, "expired": 0, "failed": 0}
    for request_path in sorted(
        pending_option_capture_root(root).glob("*/*/request.json")
    ):
        request = read_pending_option_request(request_path.parent)
        if (request.directory / "reconciled.json").is_file():
            _verify_reconciliation(root, request, request.directory / "reconciled.json")
            counters["reconciled"] += 1
        elif (request.directory / "terminal.json").is_file():
            status = _verify_terminal(request, request.directory / "terminal.json")
            counters["expired" if status.startswith("EXPIRED_") else "failed"] += 1
        else:
            if (request.directory / "capture.json").is_file():
                read_pending_option_capture(request.directory)
            counters["pending"] += 1
    return PendingReconciliationSummary(**counters)


def _reconciliation_clock(
    root: Path,
    *,
    request: PendingOptionRequest,
    observed_at: pd.Timestamp,
) -> tuple[DecisionClock | None, Mapping[str, object] | None, pd.Timestamp | None]:
    if request.bar_readiness_mode == "exact":
        clocks: dict[str, DecisionClock] = {}
        try:
            for symbol in request.required_symbols:
                clocks[symbol] = completed_bar_clock_for_target(
                    root,
                    symbol=symbol,
                    target_snapshot_for=request.target_snapshot_for,
                    as_of=observed_at,
                )
        except FileNotFoundError:
            return None, None, None
        return clocks[request.symbol], {
            "mode": "exact-compatibility",
            "ready_at": observed_at.isoformat(),
        }, observed_at

    try:
        readiness = read_bar_readiness(
            root,
            target_snapshot_for=request.target_snapshot_for,
            required_symbols=request.required_symbols,
        )
    except BarReadinessError as exc:
        directory = (
            root
            / "loop-a"
            / "bar-readiness"
            / str(request.target_snapshot_for.value)
        )
        if (directory / "readiness.json").exists() or (directory / "receipt.json").exists():
            raise PendingOptionCaptureError(
                "Pending Options reconciliation found corrupt Loop A readiness"
            ) from exc
        return None, None, None
    if readiness.ready_at > observed_at:
        raise PendingOptionCaptureError(
            "Loop A readiness carries a future availability clock"
        )
    return readiness.decision_clock(request.symbol), {
        "mode": "required",
        "run_path": readiness.directory.relative_to(root).as_posix(),
        "receipt_checksum_sha256": readiness.receipt_checksum_sha256,
        "ready_at": readiness.ready_at.isoformat(),
        "loop_a_generation": readiness.loop_a_generation,
    }, readiness.ready_at


def _publish_reconciliation(
    root: Path,
    capture: PendingOptionCapture,
    *,
    snapshot_directory: Path,
    receipt_path: Path,
    reconciled_at: pd.Timestamp,
    readiness: Mapping[str, object] | None,
) -> None:
    path = capture.request.directory / "reconciled.json"
    payload = {
        "schema_version": PENDING_OPTION_RECONCILIATION_VERSION,
        "status": "RECONCILED",
        "symbol": capture.request.symbol,
        "target_snapshot_for": capture.request.target_snapshot_for.isoformat(),
        "reconciled_at": reconciled_at.isoformat(),
        "capture_checksum_sha256": file_checksum(capture.capture_path),
        "snapshot_run_path": snapshot_directory.relative_to(root).as_posix(),
        "snapshot_receipt_checksum_sha256": file_checksum(receipt_path),
        "readiness": dict(readiness) if readiness is not None else None,
        "automated_action_allowed": False,
    }
    _write_once(path, payload)
    _verify_reconciliation(root, capture.request, path)


def _verify_committed_capture(
    root: Path,
    capture: PendingOptionCapture,
    snapshot: object,
) -> None:
    """Bind crash recovery to the exact quarantined payload, not just its key."""

    raw_path = getattr(snapshot, "raw_path", None)
    receipt = getattr(snapshot, "receipt", None)
    if not isinstance(raw_path, Path) or not isinstance(receipt, Mapping):
        raise PendingOptionCaptureError("Committed Options snapshot proof is malformed")
    raw = pd.read_parquet(raw_path)
    required = {
        "symbol",
        "snapshot_for",
        "available_at",
        "response_received_at",
        "payload_json",
        "capture_provenance_json",
    }
    if len(raw) != 1 or not required.issubset(raw.columns):
        raise PendingOptionCaptureError(
            "Committed Options snapshot lacks pending-capture provenance"
        )
    row = raw.iloc[0]
    try:
        provenance = json.loads(str(row["capture_provenance_json"]))
        committed_payload = json.loads(str(row["payload_json"]))
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise PendingOptionCaptureError(
            "Committed Options pending-capture provenance is malformed"
        ) from exc
    expected_path = capture.request.directory.relative_to(root).as_posix()
    if (
        not isinstance(provenance, Mapping)
        or provenance.get("pending_capture_path") != expected_path
        or provenance.get("pending_capture_checksum_sha256")
        != file_checksum(capture.capture_path)
        or _utc(provenance.get("response_received_at"), "committed response")
        != capture.response_received_at
        or _utc(row["response_received_at"], "raw response")
        != capture.response_received_at
        or str(row["symbol"]).strip().upper() != capture.request.symbol
        or _utc(row["snapshot_for"], "raw target")
        != capture.request.target_snapshot_for
        or _utc(row["available_at"], "raw availability")
        < capture.response_received_at
        or committed_payload != dict(capture.payload())
        or receipt.get("request_started_at")
        != capture.request.request_started_at.isoformat()
        or receipt.get("pricing_barrier") != dict(capture.request.pricing_barrier)
    ):
        raise PendingOptionCaptureError(
            "Committed Options snapshot is not the exact pending capture"
        )


def _verify_reconciliation(
    root: Path,
    request: PendingOptionRequest,
    path: Path,
) -> Mapping[str, object]:
    value = _read_mapping(path, label="Pending Options reconciliation")
    capture_path = request.directory / "capture.json"
    relative = Path(str(value.get("snapshot_run_path", "")))
    snapshot_path = (root / relative).resolve()
    if relative.is_absolute() or root not in snapshot_path.parents:
        raise PendingOptionCaptureError("Reconciled Options snapshot path escapes datastore")
    snapshot = read_option_snapshot(snapshot_path)
    if (
        value.get("schema_version") != PENDING_OPTION_RECONCILIATION_VERSION
        or value.get("status") != "RECONCILED"
        or value.get("automated_action_allowed") is not False
        or str(value.get("symbol", "")).strip().upper() != request.symbol
        or _utc(value.get("target_snapshot_for"), "reconciled target")
        != request.target_snapshot_for
        or value.get("capture_checksum_sha256") != file_checksum(capture_path)
        or value.get("snapshot_receipt_checksum_sha256")
        != file_checksum(snapshot.receipt_path)
        or snapshot.symbol != request.symbol
        or snapshot.snapshot_for != request.target_snapshot_for
    ):
        raise PendingOptionCaptureError(
            f"Pending Options reconciliation proof is invalid: {path}"
        )
    return value


def _publish_terminal(
    request: PendingOptionRequest,
    *,
    status: str,
    terminal_at: pd.Timestamp,
    detail: str,
) -> None:
    payload = {
        "schema_version": PENDING_OPTION_TERMINAL_VERSION,
        "status": status,
        "symbol": request.symbol,
        "target_snapshot_for": request.target_snapshot_for.isoformat(),
        "terminal_at": terminal_at.isoformat(),
        "detail": str(detail),
        "request_checksum_sha256": file_checksum(request.request_path),
        "capture_checksum_sha256": (
            file_checksum(request.directory / "capture.json")
            if (request.directory / "capture.json").is_file()
            else None
        ),
        "automated_action_allowed": False,
    }
    path = request.directory / "terminal.json"
    _write_once(path, payload)
    _verify_terminal(request, path)


def _verify_terminal(request: PendingOptionRequest, path: Path) -> str:
    value = _read_mapping(path, label="Pending Options terminal record")
    status = str(value.get("status", ""))
    capture_path = request.directory / "capture.json"
    expected_capture_checksum = (
        file_checksum(capture_path) if capture_path.is_file() else None
    )
    if (
        value.get("schema_version") != PENDING_OPTION_TERMINAL_VERSION
        or value.get("automated_action_allowed") is not False
        or (not status.startswith("EXPIRED_") and status != "REQUEST_FAILED")
        or str(value.get("symbol", "")).strip().upper() != request.symbol
        or _utc(value.get("target_snapshot_for"), "terminal target")
        != request.target_snapshot_for
        or value.get("request_checksum_sha256") != file_checksum(request.request_path)
        or value.get("capture_checksum_sha256") != expected_capture_checksum
    ):
        raise PendingOptionCaptureError(
            f"Pending Options terminal record is invalid: {path}"
        )
    _utc(value.get("terminal_at"), "terminal_at")
    return status


def _provider_quote_timestamps(payload: Mapping[str, Any]) -> tuple[pd.Timestamp, ...]:
    values: set[pd.Timestamp] = set()

    def visit(value: object) -> None:
        if isinstance(value, Mapping):
            for key, item in value.items():
                if str(key) in {
                    "quoteTimeInLong",
                    "quoteTime",
                    "tradeTimeInLong",
                    "tradeTime",
                }:
                    parsed = _provider_timestamp(item)
                    if parsed is not None:
                        values.add(parsed)
                else:
                    visit(item)
        elif isinstance(value, list):
            for item in value:
                visit(item)

    visit(payload)
    return tuple(sorted(values))


def _provider_timestamp(value: object) -> pd.Timestamp | None:
    if value is None:
        return None
    try:
        numeric = float(value)
    except (TypeError, ValueError):
        parsed = pd.to_datetime(value, utc=True, errors="coerce")
    else:
        unit = "ms" if abs(numeric) >= 10_000_000_000 else "s"
        parsed = pd.to_datetime(numeric, unit=unit, utc=True, errors="coerce")
    return None if pd.isna(parsed) else pd.Timestamp(parsed)


def _validate_payload_symbol(
    payload: Mapping[str, Any],
    *,
    expected_symbol: str,
) -> None:
    if not isinstance(payload, Mapping):
        raise TypeError("Pending Schwab option-chain payload must be an object")
    observed: set[str] = set()
    top = payload.get("symbol")
    if isinstance(top, str) and top.strip():
        observed.add(top.strip().upper())
    underlying = payload.get("underlying")
    if isinstance(underlying, Mapping):
        nested = underlying.get("symbol")
        if isinstance(nested, str) and nested.strip():
            observed.add(nested.strip().upper())
    if observed and observed != {expected_symbol}:
        raise PendingOptionCaptureError(
            "Pending Schwab payload identity disagrees with its requested symbol"
        )


def _datastore_root_from_pending_directory(directory: Path) -> Path:
    # <root>/options/pending-captures/schwab/<target>/<symbol>
    try:
        root = directory.parents[4]
    except IndexError as exc:
        raise PendingOptionCaptureError(
            f"Pending Options directory is outside its authority: {directory}"
        ) from exc
    expected_parent = root / "options" / "pending-captures" / "schwab"
    if directory.parent.parent.resolve() != expected_parent.resolve():
        raise PendingOptionCaptureError(
            f"Pending Options directory is outside its authority: {directory}"
        )
    return root


def _symbols(values: object) -> tuple[str, ...]:
    if isinstance(values, (str, bytes)) or not isinstance(values, Sequence):
        raise PendingOptionCaptureError("Pending Options scope must be an array")
    output = tuple(
        dict.fromkeys(
            str(value).strip().upper() for value in values if str(value).strip()
        )
    )
    if not output:
        raise PendingOptionCaptureError("Pending Options scope cannot be empty")
    return output


def _read_mapping(path: Path, *, label: str) -> Mapping[str, object]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise PendingOptionCaptureError(f"{label} is unreadable: {path}") from exc
    if not isinstance(value, Mapping):
        raise PendingOptionCaptureError(f"{label} is malformed: {path}")
    return value


def _write_once(path: Path, payload: Mapping[str, object]) -> None:
    if path.is_file():
        existing = _read_mapping(path, label="Immutable pending Options record")
        if dict(existing) != dict(payload):
            raise PendingOptionCaptureError(
                f"Immutable pending Options record conflicts with existing content: {path}"
            )
        return
    _write_json_atomic(path, payload)


def _write_json(path: Path, payload: Mapping[str, object]) -> None:
    path.write_bytes(_json_bytes(payload))


def _write_json_atomic(path: Path, payload: Mapping[str, object]) -> None:
    _write_bytes_atomic(path, _json_bytes(payload))


def _write_bytes_atomic(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + f".tmp-{os.getpid()}")
    try:
        temporary.write_bytes(payload)
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def _json_bytes(payload: Mapping[str, object]) -> bytes:
    return (
        json.dumps(
            dict(payload),
            indent=2,
            sort_keys=True,
            default=str,
            allow_nan=False,
        )
        + "\n"
    ).encode("utf-8")


def _bytes_checksum(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _remove_private_directory(path: Path) -> None:
    if not path.is_dir() or ".request-" not in path.name:
        return
    for child in path.iterdir():
        if child.is_file():
            child.unlink(missing_ok=True)
    path.rmdir()


def _utc(value: object, label: str) -> pd.Timestamp:
    timestamp = pd.to_datetime(value, utc=True, errors="coerce")
    if pd.isna(timestamp):
        raise PendingOptionCaptureError(f"Invalid pending Options {label}")
    return pd.Timestamp(timestamp)


__all__ = [
    "PENDING_OPTION_CAPTURE_VERSION",
    "PENDING_OPTION_CAUSAL_WINDOW_SECONDS",
    "PENDING_OPTION_REQUEST_VERSION",
    "PendingOptionCapture",
    "PendingOptionCaptureError",
    "PendingOptionRequest",
    "PendingReconciliationSummary",
    "begin_pending_option_request",
    "complete_pending_option_capture",
    "pending_option_capture_counts",
    "pending_option_capture_directory",
    "pending_option_capture_root",
    "read_pending_option_capture",
    "read_pending_option_request",
    "reconcile_pending_option_captures",
    "record_pending_request_failure",
]
