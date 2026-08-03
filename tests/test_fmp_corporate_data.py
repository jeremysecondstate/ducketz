from __future__ import annotations

import pandas as pd
import pytest

from app.services.fmp_corporate_data import _corporate_row


@pytest.mark.parametrize(
    ("payload_row", "expected_publication"),
    (
        (
            {
                "acceptedDate": "2025-10-03 14:42:25",
                "filingDate": "2025-10-03",
            },
            pd.Timestamp("2025-10-03T14:42:25Z"),
        ),
        (
            {"filingDate": "2025-10-03"},
            pd.Timestamp("2025-10-04T00:00:00Z"),
        ),
    ),
)
def test_statement_publication_recognizes_compact_fmp_camel_case_columns(
    payload_row: dict[str, object],
    expected_publication: pd.Timestamp,
) -> None:
    fetched_at = "2026-07-29T23:34:47Z"

    row = _corporate_row(
        symbol="MU",
        request_key="income_statement_annual",
        endpoint="income-statement",
        fetched_at=fetched_at,
        row_index=0,
        payload_row=payload_row,
    )

    assert row["published_at"] == expected_publication
    assert row["available_at"] == pd.Timestamp(fetched_at)
    assert row["effective_date_estimated"] is False


def test_statement_without_publication_uses_receipt_and_marks_estimated() -> None:
    fetched_at = "2026-07-29T23:34:47Z"

    row = _corporate_row(
        symbol="MU",
        request_key="income_statement_annual",
        endpoint="income-statement",
        fetched_at=fetched_at,
        row_index=0,
        payload_row={"date": "2025-08-28", "period": "FY"},
    )

    assert row["published_at"] is None
    assert row["available_at"] == pd.Timestamp(fetched_at)
    assert row["effective_date_estimated"] is True
