from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

import pandas as pd

from app.services.sec_capital_structure import (
    SecCapitalStructureScanner,
    _filing_document_url,
    _is_relevant_filing,
    _raw_text_row,
    _scan_filing_text,
)
from datafetching import FetchResult
from datafetching.layout import safe_token
from datafetching.parquet_store import ParquetStore
from datafetching.sec_events import (
    SEC_EXTRACTOR_VERSION,
    conservative_publication_timestamp,
    normalize_sec_event_rows,
    persist_sec_events,
)


def fetch(symbol: str, store: ParquetStore) -> FetchResult:
    """Fetch SEC filing metadata and text into idempotent corporate Parquets."""
    scanner = SecCapitalStructureScanner()
    data_files = 0
    error_files = 0

    try:
        filings = scanner._fetch_fmp_filings(symbol)
    except Exception as exc:
        store.save_error(
            source="sec",
            category="corporate",
            symbol=symbol,
            request_key="filing_index",
            error_type=type(exc).__name__,
            error_message=str(exc),
            metadata={"discovery_source": "fmp_sec_filings_search_symbol"},
        )
        return FetchResult("sec", 0, 1)

    selected = [filing for filing in filings if _is_relevant_filing(filing)]
    metadata_rows = [_filing_metadata_row(symbol, filing, index) for index, filing in enumerate(selected)]
    if metadata_rows and store.save_corporate_rows(
        "sec",
        symbol,
        "filing_index",
        metadata_rows,
        metadata={"discovery_source": "fmp_sec_filings_search_symbol"},
    ) is not None:
        data_files += 1

    text_rows: list[dict[str, Any]] = []
    normalized_event_frames = []
    processed_acceptances = _processed_acceptances(
        store.root_dir,
        symbol=symbol,
    )
    for index, filing in enumerate(selected):
        accepted = conservative_publication_timestamp(
            filing.get("acceptedDate") or filing.get("accepted_date")
        )
        if accepted is not None and accepted in processed_acceptances:
            continue
        document_url = _filing_document_url(filing)
        if not document_url:
            store.save_error(
                source="sec",
                category="corporate",
                symbol=symbol,
                request_key="filing_text",
                error_type="MissingDocumentUrl",
                error_message="Filing metadata did not contain an SEC document URL.",
                metadata=_filing_error_metadata(filing),
            )
            error_files += 1
            continue

        try:
            text = scanner._fetch_text(document_url)
            document_received_at = datetime.now(timezone.utc)
            text_rows.append(_raw_text_row(symbol, filing, document_url, text, index))
            scanned = _scan_filing_text(
                symbol,
                filing,
                document_url,
                text,
                index,
            )
            extraction_completed_at = datetime.now(timezone.utc)
            normalized = normalize_sec_event_rows(
                scanned,
                document_received_at=document_received_at,
                extraction_completed_at=extraction_completed_at,
            )
            normalized_event_frames.append(normalized)
            if accepted is not None and not normalized.empty:
                processed_acceptances.add(accepted)
        except Exception as exc:
            store.save_error(
                source="sec",
                category="corporate",
                symbol=symbol,
                request_key="filing_text",
                error_type=type(exc).__name__,
                error_message=str(exc),
                metadata={
                    **_filing_error_metadata(filing),
                    "document_url": document_url,
                },
            )
            error_files += 1

    if text_rows and store.save_corporate_rows(
        "sec",
        symbol,
        "filing_text",
        text_rows,
        metadata={"discovery_source": "fmp_sec_filings_search_symbol"},
    ) is not None:
        data_files += 1

    if normalized_event_frames:
        events = pd.concat(
            normalized_event_frames,
            ignore_index=True,
            sort=False,
        )
        data_files += len(persist_sec_events(store.root_dir, events))

    return FetchResult("sec", data_files, error_files)


def _processed_acceptances(
    datastore_root: Path,
    *,
    symbol: str,
) -> set[pd.Timestamp]:
    """Return filings already scanned by the current frozen extractor.

    Re-polling a historical filing must not create a fresh model event merely
    because the local document receipt time changed. A new extractor version
    remains eligible for a separately versioned forward-going calculation.
    """

    root = (
        Path(datastore_root)
        / "stocks"
        / safe_token(symbol.strip().upper())
        / "corporate"
        / "sec-events"
        / "sec"
    )
    accepted: set[pd.Timestamp] = set()
    for path in sorted(root.glob("*.parquet")):
        try:
            frame = pd.read_parquet(
                path,
                columns=["filing_accepted_at", "extractor_version"],
            )
        except (OSError, ValueError):
            continue
        current = frame.loc[
            frame["extractor_version"].astype(str).eq(SEC_EXTRACTOR_VERSION)
        ]
        accepted.update(
            pd.Timestamp(value)
            for value in pd.to_datetime(
                current["filing_accepted_at"],
                utc=True,
                errors="coerce",
            ).dropna()
        )
    return accepted


def _filing_metadata_row(symbol: str, filing: Mapping[str, Any], row_index: int) -> dict[str, Any]:
    row: dict[str, Any] = {
        "symbol": symbol,
        "source": "sec",
        "request_key": "filing_index",
        "row_index": row_index,
        "fetched_at": datetime.now(timezone.utc).isoformat(),
    }
    for key, value in filing.items():
        column = str(key)
        if column in row:
            column = f"filing_{column}"
        row[column] = _parquet_value(value)
    return row


def _filing_error_metadata(filing: Mapping[str, Any]) -> dict[str, object]:
    return {
        "form_type": str(filing.get("type") or filing.get("form") or filing.get("formType") or ""),
        "filing_date": str(filing.get("filingDate") or filing.get("date") or ""),
    }


def _parquet_value(value: Any) -> Any:
    if isinstance(value, (dict, list, tuple)):
        import json

        return json.dumps(value, default=str, sort_keys=True)
    return value
