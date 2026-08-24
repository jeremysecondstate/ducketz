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


class _UnresolvedSymbolTimeseries:
    def get_range(self, **_: object) -> _EmptyStore:
        warnings.warn(
            "The streaming request had one or more symbols which did not "
            "resolve: CLU6.",
            BentoWarning,
            stacklevel=2,
        )
        return _EmptyStore()


class _UnresolvedSymbolClient:
    timeseries = _UnresolvedSymbolTimeseries()


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


def test_weekend_unresolved_symbol_warning_is_suppressed_only_off_hours(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = DatabentoCmeContextProvider(
        api_key="test-key",
        dataset="GLBX.MDP3",
        schemas=("mbp-10",),
        context_symbols=(),
        contract_symbols=("CLU6",),
        chunk_days=None,
    )
    monkeypatch.setattr(provider, "_client", lambda: _UnresolvedSymbolClient())
    weekend = DatabentoCmeContextSpec(
        group_key="contracts",
        output_symbol="CME_CONTRACTS",
        symbols=("CLU6",),
        dataset="GLBX.MDP3",
        schema="mbp-10",
        stype_in="raw_symbol",
        start=datetime(2026, 8, 23, 16, 59, 55, tzinfo=timezone.utc),
        end=datetime(2026, 8, 23, 17, 0, tzinfo=timezone.utc),
        limit=250_000,
    )

    with warnings.catch_warnings(record=True) as observed:
        warnings.simplefilter("always")
        rows, raw_frame, effective = provider.fetch_cme_context_exact(weekend)
    assert not observed
    assert raw_frame.empty
    assert effective.availability_status == "NO CURRENT ROWS"
    assert rows[0]["cme_row_kind"] == "schema_status"

    weekday = DatabentoCmeContextSpec(
        **{
            **weekend.__dict__,
            "start": datetime(2026, 8, 18, 21, 59, 55, tzinfo=timezone.utc),
            "end": datetime(2026, 8, 18, 22, 0, tzinfo=timezone.utc),
        }
    )
    with pytest.warns(BentoWarning, match="did not resolve: CLU6"):
        provider.fetch_cme_context_exact(weekday)
