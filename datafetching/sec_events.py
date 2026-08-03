from __future__ import annotations

from pathlib import Path
from typing import Mapping, Sequence

import pandas as pd

from datafetching.calculated_features import write_immutable_feature_partition
from datafetching.layout import safe_token

SEC_EVENT_CALCULATION = "sec-filing-events"
SEC_EVENT_CALCULATION_VERSION = "1.0.0"
SEC_EXTRACTOR_VERSION = "capital-structure-rules-v1"
SEC_EVENT_SCHEMA_VERSION = "sec-event-v1"

SEC_EVENT_COLUMNS = (
    "symbol",
    "form_group",
    "filing_accepted_at",
    "document_received_at",
    "extraction_completed_at",
    "denominator_available_at",
    "available_at",
    "event_type",
    "event_state",
    "accession_number",
    "document_url",
    "evidence_quality",
    "evidence_basis",
    "extractor_version",
    "calculation",
    "calculation_version",
    "schema_version",
    "quantity",
    "offering_price",
    "offering_size",
    "dilution_percentage",
    "normalized_amount_to_market_cap",
    "normalized_quantity_to_shares",
    "dilution_event",
    "offering_size_to_market_cap",
    "filing_event_impulse",
    "extraction_quality_pass",
)
SEC_EVENT_NATURAL_KEY = (
    "symbol",
    "filing_accepted_at",
    "event_type",
    "available_at",
)


def normalize_sec_event_rows(
    rows: Sequence[Mapping[str, object]],
    *,
    document_received_at: object,
    extraction_completed_at: object,
    market_cap: float | None = None,
    shares_outstanding: float | None = None,
    denominator_available_at: object | None = None,
) -> pd.DataFrame:
    """Normalize deterministic scanner rows without inventing causal denominators."""

    if not rows:
        return pd.DataFrame(columns=SEC_EVENT_COLUMNS)
    received = _utc_timestamp(document_received_at)
    completed = _utc_timestamp(extraction_completed_at)
    if completed < received:
        raise ValueError("SEC extraction cannot complete before document receipt")
    denominator_clock = (
        _utc_timestamp(denominator_available_at)
        if denominator_available_at is not None
        else None
    )
    if (
        market_cap is not None or shares_outstanding is not None
    ) and denominator_clock is None:
        raise ValueError(
            "SEC causal denominators require denominator_available_at"
        )
    if denominator_clock is not None and denominator_clock > completed:
        raise ValueError(
            "SEC extraction completion cannot precede denominator availability"
        )

    normalized: list[dict[str, object]] = []
    grouped: dict[tuple[str, pd.Timestamp, str], list[Mapping[str, object]]] = {}
    for raw in rows:
        symbol = str(raw.get("symbol") or "").strip().upper()
        accepted = conservative_publication_timestamp(
            raw.get("accepted_date") or raw.get("acceptedDate")
        )
        event_type = str(
            raw.get("instrument_event") or "No instrument or event identified"
        ).strip()
        if not symbol or accepted is None:
            raise ValueError("SEC events require symbol and filing acceptance")
        grouped.setdefault((symbol, accepted, event_type), []).append(raw)

    for (symbol, accepted, event_type), candidates in grouped.items():
        chosen = max(candidates, key=_quality_rank)
        available = max(
            accepted,
            received,
            completed,
            *((denominator_clock,) if denominator_clock is not None else ()),
        )
        quality_pass = _quality_rank(chosen) >= 2
        event_state = str(chosen.get("evidence_state") or "insufficient_evidence")
        supported = quality_pass and event_state not in {
            "unrelated",
            "insufficient_evidence",
        }
        dilution = supported and (
            "dilut" in event_type.lower()
            or event_type
            in {
                "Warrants",
                "Convertible securities",
                "Preferred stock",
                "At-the-market offering",
                "Securities offering",
            }
        )
        offering_size = _number(chosen.get("offering_size"))
        quantity = _number(chosen.get("quantity"))
        amount_ratio = _safe_ratio(offering_size, market_cap)
        quantity_ratio = _safe_ratio(quantity, shares_outstanding)
        normalized.append(
            {
                "symbol": symbol,
                "form_group": _form_group(chosen.get("form_type")),
                "filing_accepted_at": accepted,
                "document_received_at": received,
                "extraction_completed_at": completed,
                "denominator_available_at": denominator_clock,
                "available_at": available,
                "event_type": event_type,
                "event_state": event_state,
                "accession_number": str(
                    chosen.get("accession_number") or ""
                ).strip(),
                "document_url": str(chosen.get("document_url") or "").strip(),
                "evidence_quality": str(
                    chosen.get("evidence_quality") or ""
                ).strip(),
                "evidence_basis": str(
                    chosen.get("evidence_basis") or ""
                ).strip(),
                "extractor_version": SEC_EXTRACTOR_VERSION,
                "calculation": SEC_EVENT_CALCULATION,
                "calculation_version": SEC_EVENT_CALCULATION_VERSION,
                "schema_version": SEC_EVENT_SCHEMA_VERSION,
                "quantity": quantity,
                "offering_price": _number(chosen.get("offering_price")),
                "offering_size": offering_size,
                "dilution_percentage": _number(
                    chosen.get("dilution_percentage")
                ),
                "normalized_amount_to_market_cap": amount_ratio,
                "normalized_quantity_to_shares": quantity_ratio,
                "dilution_event": float(dilution),
                "offering_size_to_market_cap": amount_ratio,
                "filing_event_impulse": float(supported),
                "extraction_quality_pass": bool(quality_pass),
            }
        )
    return pd.DataFrame(normalized, columns=SEC_EVENT_COLUMNS)


def persist_sec_events(
    datastore_root: Path,
    frame: pd.DataFrame,
) -> tuple[Path, ...]:
    if frame.empty:
        return ()
    values = frame.copy()
    values["filing_accepted_at"] = pd.to_datetime(
        values["filing_accepted_at"], utc=True, errors="coerce"
    )
    paths: list[Path] = []
    for (symbol, year), partition in values.groupby(
        [
            values["symbol"].astype(str).str.upper(),
            values["filing_accepted_at"].dt.year,
        ],
        dropna=False,
    ):
        if pd.isna(year):
            raise ValueError("SEC event partition requires a valid acceptance year")
        path = (
            Path(datastore_root)
            / "stocks"
            / safe_token(str(symbol))
            / "corporate"
            / "sec-events"
            / "sec"
            / f"{int(year):04d}.parquet"
        )
        paths.append(
            write_immutable_feature_partition(
                path,
                partition,
                columns=SEC_EVENT_COLUMNS,
                natural_key=SEC_EVENT_NATURAL_KEY,
            )
        )
    return tuple(paths)


def conservative_publication_timestamp(value: object) -> pd.Timestamp | None:
    text = str(value or "").strip()
    if not text:
        return None
    parsed = pd.to_datetime(text, utc=True, errors="coerce")
    if pd.isna(parsed):
        return None
    timestamp = pd.Timestamp(parsed)
    if len(text) <= 10:
        return timestamp.normalize() + pd.Timedelta(days=1)
    return timestamp


def _utc_timestamp(value: object) -> pd.Timestamp:
    timestamp = pd.to_datetime(value, utc=True, errors="coerce")
    if pd.isna(timestamp):
        raise ValueError("Expected a valid UTC timestamp")
    return pd.Timestamp(timestamp)


def _quality_rank(row: Mapping[str, object]) -> int:
    quality = str(row.get("evidence_quality") or "").strip().lower()
    return {
        "confirmed terms": 3,
        "partial terms": 2,
        "keyword only": 1,
        "no relevant evidence": 0,
    }.get(quality, 0)


def _form_group(value: object) -> str:
    form = str(value or "").strip().upper()
    return form.split("/", 1)[0] if form else "UNKNOWN"


def _number(value: object) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if pd.notna(number) else None


def _safe_ratio(numerator: float | None, denominator: float | None) -> float | None:
    if numerator is None or denominator is None or denominator <= 0:
        return None
    return numerator / denominator
