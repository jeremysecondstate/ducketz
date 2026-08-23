from __future__ import annotations

import warnings
from datetime import datetime, timedelta, timezone

import pandas as pd
import pytest
from databento.common.error import BentoWarning

from app.services.databento_cme_context import (
    DatabentoCmeContextProvider,
    DatabentoCmeContextSpec,
)


class _EmptyStore:
    def to_df(self) -> pd.DataFrame:
        return pd.DataFrame()


class _WarningTimeseries:
    def get_range(self, **_: object) -> _EmptyStore:
        warnings.warn(
            "No data found for the request you submitted.",
            BentoWarning,
            stacklevel=2,
        )
        warnings.warn(
            "No data found for the request you submitted. "
            "The request time range falls entirely inside a weekend.",
            BentoWarning,
            stacklevel=2,
        )
        warnings.warn("degraded day", BentoWarning, stacklevel=2)
        return _EmptyStore()


class _WarningClient:
    timeseries = _WarningTimeseries()


def test_expected_empty_range_warning_is_suppressed_without_hiding_others(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = DatabentoCmeContextProvider(
        api_key="test-key",
        dataset="GLBX.MDP3",
        schemas=("mbp-10",),
        context_symbols=("NQ.v.0",),
        contract_symbols=(),
        chunk_days=None,
    )
    monkeypatch.setattr(provider, "_client", lambda: _WarningClient())
    start = datetime(2026, 8, 18, 21, 13, 36, tzinfo=timezone.utc)
    spec = DatabentoCmeContextSpec(
        group_key="context",
        output_symbol="CME_CONTEXT",
        symbols=("NQ.v.0",),
        dataset="GLBX.MDP3",
        schema="mbp-10",
        stype_in="continuous",
        start=start,
        end=start + timedelta(minutes=5),
        limit=250_000,
    )

    with pytest.warns(BentoWarning, match="degraded day") as observed:
        rows, raw_frame, effective = provider.fetch_cme_context_exact(spec)

    assert len(observed) == 1
    assert raw_frame.empty
    assert effective.availability_status == "NO CURRENT ROWS"
    assert rows[0]["cme_row_kind"] == "schema_status"
