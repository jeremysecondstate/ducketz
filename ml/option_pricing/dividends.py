from __future__ import annotations

import json
import math
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping, Sequence

import pandas as pd

from ml.artifacts import file_checksum, utc_timestamp


FMP_DIVIDEND_HISTORY_SCHEMA_VERSION = "fmp-dividend-history-v1"
DIVIDEND_RESOLUTION_POLICY_VERSION = "causal-expiration-cash-dividend-v1"
DIVIDEND_CONFIDENCE_LANES = (
    "DECLARED_FMP",
    "CAUSAL_RECURRING_ESTIMATE",
    "PUT_CALL_PARITY_FALLBACK",
    "ZERO_NO_KNOWN_DIVIDEND",
)


@dataclass(frozen=True)
class DividendResolution:
    known_dividend_pv: float
    equivalent_dividend_yield: float
    dividend_event_count: int
    next_ex_dividend_date: str | None
    dividend_source_available_at: pd.Timestamp | None
    dividend_confidence: str
    policy_version: str = DIVIDEND_RESOLUTION_POLICY_VERSION


def publish_fmp_dividend_history(
    datastore_root: Path,
    symbol: str,
    raw_response: object,
    *,
    received_at: object,
) -> Path:
    """Commit an already-fetched FMP dividend response; never calls FMP."""

    clean_symbol = _symbol(symbol)
    received = utc_timestamp(received_at)
    records = _response_records(raw_response)
    normalized = _normalize_records(
        records, symbol=clean_symbol, received_at=received
    )
    authority = (
        Path(datastore_root).resolve()
        / "stocks"
        / clean_symbol
        / "corporate-actions"
        / "dividends"
        / "fmp"
    )
    authority.mkdir(parents=True, exist_ok=True)
    name = received.strftime("%Y%m%dT%H%M%S.%fZ")
    destination = authority / name
    if destination.exists():
        observed = read_fmp_dividend_history(
            destination, datastore_root=datastore_root, symbol=clean_symbol
        )
        if observed.reset_index(drop=True).equals(normalized.reset_index(drop=True)):
            return destination
        raise RuntimeError("Divergent FMP dividend evidence has the same receipt identity")
    staging = Path(tempfile.mkdtemp(prefix=f".{name}.tmp-{os.getpid()}-", dir=authority))
    try:
        raw_path = staging / "raw-response.json"
        raw_path.write_text(
            json.dumps(raw_response, indent=2, sort_keys=True, default=str) + "\n",
            encoding="utf-8",
        )
        events_path = staging / "normalized-events.parquet"
        normalized.to_parquet(events_path, index=False)
        manifest = {
            "schema_version": FMP_DIVIDEND_HISTORY_SCHEMA_VERSION,
            "provider": "fmp",
            "endpoint": "stable/dividends",
            "symbol": clean_symbol,
            "received_at": received.isoformat(),
            "outputs": _inventory(staging, (raw_path.name, events_path.name)),
        }
        manifest_path = staging / "manifest.json"
        _write_json(manifest_path, manifest)
        _write_json(
            staging / "receipt.json",
            {
                "schema_version": FMP_DIVIDEND_HISTORY_SCHEMA_VERSION,
                "symbol": clean_symbol,
                "received_at": received.isoformat(),
                "run_path": destination.relative_to(Path(datastore_root).resolve()).as_posix(),
                "manifest_checksum_sha256": file_checksum(manifest_path),
                "automated_action_allowed": False,
            },
        )
        staging.replace(destination)
    except BaseException:
        raise
    read_fmp_dividend_history(
        destination, datastore_root=datastore_root, symbol=clean_symbol
    )
    return destination


def read_fmp_dividend_history(
    directory: Path,
    *,
    datastore_root: Path,
    symbol: str,
) -> pd.DataFrame:
    root = Path(datastore_root).resolve()
    clean_symbol = _symbol(symbol)
    run = Path(directory).resolve()
    authority = (
        root
        / "stocks"
        / clean_symbol
        / "corporate-actions"
        / "dividends"
        / "fmp"
    )
    if run.parent != authority:
        raise RuntimeError("FMP dividend run escapes its immutable authority")
    manifest_path = run / "manifest.json"
    manifest = _read_json(manifest_path)
    receipt = _read_json(run / "receipt.json")
    if (
        manifest.get("schema_version") != FMP_DIVIDEND_HISTORY_SCHEMA_VERSION
        or receipt.get("schema_version") != FMP_DIVIDEND_HISTORY_SCHEMA_VERSION
        or manifest.get("symbol") != clean_symbol
        or receipt.get("symbol") != clean_symbol
        or receipt.get("manifest_checksum_sha256") != file_checksum(manifest_path)
        or receipt.get("run_path") != run.relative_to(root).as_posix()
    ):
        raise RuntimeError("FMP dividend receipt does not match its immutable run")
    for name, metadata in manifest.get("outputs", {}).items():
        path = run / str(name)
        if (
            not path.is_file()
            or int(metadata.get("size", -1)) != path.stat().st_size
            or metadata.get("checksum_sha256") != file_checksum(path)
        ):
            raise RuntimeError(f"FMP dividend output failed verification: {path}")
    frame = pd.read_parquet(run / "normalized-events.parquet")
    for column in (
        "ex_dividend_date",
        "declaration_date",
        "record_date",
        "payment_date",
    ):
        frame[column] = pd.to_datetime(frame[column], errors="coerce")
    frame["source_available_at"] = pd.to_datetime(
        frame["source_available_at"], utc=True, errors="coerce"
    )
    return frame


def load_verified_fmp_dividend_history(
    datastore_root: Path, symbol: str
) -> pd.DataFrame:
    root = Path(datastore_root).resolve()
    clean_symbol = _symbol(symbol)
    authority = (
        root
        / "stocks"
        / clean_symbol
        / "corporate-actions"
        / "dividends"
        / "fmp"
    )
    frames = [
        read_fmp_dividend_history(
            receipt.parent, datastore_root=root, symbol=clean_symbol
        )
        for receipt in sorted(authority.glob("*/receipt.json"))
    ]
    return pd.concat(frames, ignore_index=True, sort=False) if frames else pd.DataFrame()


def resolve_dividend_for_expiration(
    symbol: str,
    as_of: object,
    expiration: object,
    spot: float,
    *,
    datastore_root: Path | None = None,
    events: pd.DataFrame | None = None,
    risk_free_rate: float = 0.0,
    recurring_estimates: pd.DataFrame | None = None,
    parity_fallback_yield: float | None = None,
) -> DividendResolution:
    decision = utc_timestamp(as_of)
    expiry = utc_timestamp(expiration)
    horizon = (expiry - decision).total_seconds() / (365.0 * 24.0 * 3600.0)
    if horizon <= 0.0:
        raise ValueError("Expiration must be later than as_of")
    if not math.isfinite(float(spot)) or float(spot) <= 0.0:
        raise ValueError("spot must be finite and positive")
    if not math.isfinite(float(risk_free_rate)):
        raise ValueError("risk_free_rate must be finite")
    history = (
        events.copy()
        if events is not None
        else load_verified_fmp_dividend_history(Path(datastore_root), symbol)
        if datastore_root is not None
        else pd.DataFrame()
    )
    selected = _causal_events(history, decision=decision, expiration=expiry)
    confidence = "DECLARED_FMP"
    if selected.empty and recurring_estimates is not None:
        selected = _causal_events(
            recurring_estimates, decision=decision, expiration=expiry
        )
        confidence = "CAUSAL_RECURRING_ESTIMATE"
    if not selected.empty:
        cash = pd.to_numeric(selected["cash_dividend"], errors="coerce")
        years = (
            pd.to_datetime(selected["ex_dividend_date"], utc=True, errors="coerce")
            - decision
        ).dt.total_seconds() / (365.0 * 24.0 * 3600.0)
        pv = float((cash * (-float(risk_free_rate) * years).map(math.exp)).sum())
        if pv < 0.0 or pv >= float(spot):
            raise ValueError("Known dividend PV is outside the valid spot range")
        equivalent = -math.log((float(spot) - pv) / float(spot)) / horizon
        available = pd.to_datetime(
            selected["source_available_at"], utc=True, errors="coerce"
        ).max()
        next_ex = pd.to_datetime(selected["ex_dividend_date"]).min().date().isoformat()
        return DividendResolution(
            known_dividend_pv=pv,
            equivalent_dividend_yield=equivalent,
            dividend_event_count=len(selected),
            next_ex_dividend_date=next_ex,
            dividend_source_available_at=pd.Timestamp(available),
            dividend_confidence=confidence,
        )
    if parity_fallback_yield is not None:
        value = float(parity_fallback_yield)
        if not math.isfinite(value):
            raise ValueError("parity_fallback_yield must be finite")
        return DividendResolution(
            known_dividend_pv=0.0,
            equivalent_dividend_yield=value,
            dividend_event_count=0,
            next_ex_dividend_date=None,
            dividend_source_available_at=None,
            dividend_confidence="PUT_CALL_PARITY_FALLBACK",
        )
    return DividendResolution(
        known_dividend_pv=0.0,
        equivalent_dividend_yield=0.0,
        dividend_event_count=0,
        next_ex_dividend_date=None,
        dividend_source_available_at=None,
        dividend_confidence="ZERO_NO_KNOWN_DIVIDEND",
    )


def _causal_events(
    frame: pd.DataFrame, *, decision: pd.Timestamp, expiration: pd.Timestamp
) -> pd.DataFrame:
    required = {
        "ex_dividend_date",
        "declaration_date",
        "cash_dividend",
        "source_available_at",
    }
    if frame.empty or not required.issubset(frame.columns):
        return pd.DataFrame(columns=sorted(required))
    output = frame.copy()
    output["ex_dividend_date"] = pd.to_datetime(
        output["ex_dividend_date"], errors="coerce"
    )
    output["declaration_date"] = pd.to_datetime(
        output["declaration_date"], errors="coerce"
    )
    output["source_available_at"] = pd.to_datetime(
        output["source_available_at"], utc=True, errors="coerce"
    )
    output["cash_dividend"] = pd.to_numeric(
        output["cash_dividend"], errors="coerce"
    )
    decision_date = decision.tz_convert("UTC").tz_localize(None)
    expiration_date = expiration.tz_convert("UTC").tz_localize(None)
    output = output.loc[
        output["source_available_at"].le(decision)
        # FMP supplies declaration dates without an intraday release clock.
        # A same-calendar-day declaration is therefore not presumed knowable
        # during that session.
        & output["declaration_date"].lt(decision_date.normalize())
        & output["ex_dividend_date"].gt(decision_date)
        & output["ex_dividend_date"].le(expiration_date)
        & output["cash_dividend"].gt(0.0)
    ].sort_values("source_available_at", kind="stable")
    # Repeated daily history receipts describe the same event. The earliest
    # verified receipt establishes causal availability and cannot multiply PV.
    return output.drop_duplicates(
        ["ex_dividend_date", "declaration_date", "cash_dividend"], keep="first"
    ).reset_index(drop=True)


def _normalize_records(
    records: Sequence[Mapping[str, object]], *, symbol: str, received_at: pd.Timestamp
) -> pd.DataFrame:
    rows: list[dict[str, object]] = []
    for record in records:
        ex_date = pd.to_datetime(record.get("date"), errors="coerce")
        declaration = pd.to_datetime(record.get("declarationDate"), errors="coerce")
        adjusted = pd.to_numeric(record.get("adjDividend"), errors="coerce")
        unadjusted = pd.to_numeric(record.get("dividend"), errors="coerce")
        amount = adjusted if pd.notna(adjusted) and float(adjusted) > 0.0 else unadjusted
        if pd.isna(ex_date) or pd.isna(declaration) or pd.isna(amount):
            continue
        rows.append(
            {
                "symbol": symbol,
                "ex_dividend_date": pd.Timestamp(ex_date).normalize(),
                "declaration_date": pd.Timestamp(declaration).normalize(),
                "record_date": pd.to_datetime(record.get("recordDate"), errors="coerce"),
                "payment_date": pd.to_datetime(record.get("paymentDate"), errors="coerce"),
                "cash_dividend": float(amount),
                "frequency": record.get("frequency"),
                "source_available_at": received_at,
                "provider": "fmp",
                "schema_version": FMP_DIVIDEND_HISTORY_SCHEMA_VERSION,
            }
        )
    columns = (
        "symbol",
        "ex_dividend_date",
        "declaration_date",
        "record_date",
        "payment_date",
        "cash_dividend",
        "frequency",
        "source_available_at",
        "provider",
        "schema_version",
    )
    return pd.DataFrame(rows, columns=columns)


def _response_records(raw_response: object) -> list[Mapping[str, object]]:
    value = raw_response
    if isinstance(value, Mapping):
        value = value.get("data", value.get("results", [value]))
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        raise TypeError("Provider response must be a JSON object or list")
    return [dict(item) for item in value if isinstance(item, Mapping)]


def _symbol(value: str) -> str:
    clean = str(value).strip().upper()
    if not clean or not clean.replace("-", "").isalnum():
        raise ValueError("Invalid dividend symbol")
    return clean


def _inventory(directory: Path, names: Sequence[str]) -> dict[str, dict[str, object]]:
    return {
        name: {
            "size": (directory / name).stat().st_size,
            "checksum_sha256": file_checksum(directory / name),
        }
        for name in names
    }


def _read_json(path: Path) -> Mapping[str, object]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"Authority metadata is unreadable: {path}") from exc
    if not isinstance(value, Mapping):
        raise RuntimeError(f"Authority metadata is malformed: {path}")
    return value


def _write_json(path: Path, payload: Mapping[str, object]) -> None:
    path.write_text(
        json.dumps(dict(payload), indent=2, sort_keys=True, default=str) + "\n",
        encoding="utf-8",
    )


__all__ = [
    "DIVIDEND_CONFIDENCE_LANES",
    "DIVIDEND_RESOLUTION_POLICY_VERSION",
    "DividendResolution",
    "FMP_DIVIDEND_HISTORY_SCHEMA_VERSION",
    "load_verified_fmp_dividend_history",
    "publish_fmp_dividend_history",
    "read_fmp_dividend_history",
    "resolve_dividend_for_expiration",
]
