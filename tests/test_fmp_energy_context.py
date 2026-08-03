from __future__ import annotations

import math
from pathlib import Path

import pandas as pd
import pytest

from app.services.fmp_macro_context import FmpMacroContextSpec
from datafetching import fmp_fetch
from datafetching.fmp_energy_context import (
    ENERGY_CONTEXT_COLUMNS,
    calculate_fmp_energy_context,
    fmp_energy_context_path,
    materialize_fmp_energy_context,
    normalize_fmp_quote_timestamps,
)
from datafetching.parquet_store import ParquetStore


def test_fmp_energy_normalizes_unix_seconds_and_breaks_identity_chain(
    tmp_path: Path,
) -> None:
    epochs = [
        int(pd.Timestamp(value).timestamp())
        for value in (
            "2026-07-29T16:00:00Z",
            "2026-07-29T17:00:00Z",
            "2026-07-29T18:00:00Z",
            "2026-07-29T19:00:00Z",
        )
    ]
    raw_rows = [
        _quote_row(
            provider_symbol="CLUSD",
            timestamp=epochs[0],
            fetched_at="2026-07-29T16:01:00Z",
            price=80.0,
        ),
        _quote_row(
            provider_symbol="CLUSD",
            timestamp=str(epochs[1]),
            fetched_at="2026-07-29T17:01:00Z",
            price=88.0,
        ),
        _quote_row(
            provider_symbol="USO",
            timestamp=epochs[2],
            fetched_at="2026-07-29T18:01:00Z",
            price=70.0,
            proxy=True,
        ),
        _quote_row(
            provider_symbol="USO",
            timestamp=epochs[3],
            fetched_at="2026-07-29T19:01:00Z",
            price=77.0,
            proxy=True,
        ),
    ]

    normalized = normalize_fmp_quote_timestamps(raw_rows)
    assert normalized[0]["timestamp"] == "2026-07-29T16:00:00+00:00"
    assert normalized[0]["available_at"] == pd.Timestamp(
        "2026-07-29T16:01:00Z"
    )
    calculated = calculate_fmp_energy_context(pd.DataFrame(normalized))

    assert calculated.columns.tolist() == list(ENERGY_CONTEXT_COLUMNS)
    assert calculated["observed_at"].tolist() == [
        pd.Timestamp("2026-07-29T16:00:00Z"),
        pd.Timestamp("2026-07-29T17:00:00Z"),
        pd.Timestamp("2026-07-29T18:00:00Z"),
        pd.Timestamp("2026-07-29T19:00:00Z"),
    ]
    assert calculated["available_at"].equals(calculated["fetched_at"])
    assert math.isnan(calculated["wti_or_proxy_return"].iloc[0])
    assert calculated["wti_or_proxy_return"].iloc[1] == pytest.approx(
        math.log1p(8.0 / 80.0)
    )
    assert math.isnan(calculated["wti_or_proxy_return"].iloc[2])
    assert calculated["wti_or_proxy_return"].iloc[3] == pytest.approx(
        math.log1p(7.0 / 70.0)
    )
    assert calculated["instrument_changed"].tolist() == [
        False,
        False,
        True,
        False,
    ]
    assert calculated["chain_complete"].tolist() == [
        False,
        True,
        False,
        True,
    ]
    assert calculated["instrument_chain"].tolist() == [
        "CLUSD:CLUSD:direct_commodity",
        "CLUSD:CLUSD:direct_commodity",
        "CLUSD:USO:exchange_traded_proxy",
        "CLUSD:USO:exchange_traded_proxy",
    ]

    source_path = (
        tmp_path
        / "pools"
        / "macro"
        / "CLUSD"
        / "quote"
        / "fmp"
        / "normalized"
        / "CLUSD_quote.parquet"
    )
    source_path.parent.mkdir(parents=True, exist_ok=True)
    pd.DataFrame(normalized).to_parquet(source_path, index=False)
    output_path = materialize_fmp_energy_context(tmp_path)

    assert output_path == fmp_energy_context_path(tmp_path)
    assert output_path == (
        tmp_path
        / "pools"
        / "macro"
        / "features"
        / "energy-context"
        / "fmp"
        / "quote.parquet"
    )
    stored = pd.read_parquet(output_path)
    assert stored.columns[0] == "id"
    assert stored.columns.tolist().count("id") == 1
    assert len(stored) == 4
    assert stored["id"].str.contains("1970").sum() == 0
    assert materialize_fmp_energy_context(tmp_path) is None


def test_fmp_energy_repairs_legacy_nanosecond_misparse() -> None:
    epoch = int(pd.Timestamp("2026-07-29T16:00:00Z").timestamp())
    legacy_timestamp = pd.to_datetime(epoch, unit="ns", utc=True)
    source = pd.DataFrame(
        [
            _quote_row(
                provider_symbol="CLUSD",
                timestamp=legacy_timestamp,
                fetched_at="2026-07-29T16:01:00Z",
                price=80.0,
            )
        ]
    )

    calculated = calculate_fmp_energy_context(source)

    assert calculated["observed_at"].item() == pd.Timestamp(
        "2026-07-29T16:00:00Z"
    )
    assert calculated["available_at"].item() == pd.Timestamp(
        "2026-07-29T16:01:00Z"
    )


def test_fmp_source_appends_to_legacy_arrow_timestamp(
    tmp_path: Path,
) -> None:
    epoch = int(pd.Timestamp("2026-07-29T16:00:00Z").timestamp())
    legacy_timestamp = pd.to_datetime(epoch, unit="ns", utc=True)
    source_path = (
        tmp_path
        / "pools"
        / "macro"
        / "CLUSD"
        / "quote"
        / "fmp"
        / "normalized"
        / "CLUSD_quote.parquet"
    )
    source_path.parent.mkdir(parents=True, exist_ok=True)
    pd.DataFrame(
        [
            _quote_row(
                provider_symbol="CLUSD",
                timestamp=legacy_timestamp,
                fetched_at="2026-07-29T16:01:00Z",
                price=80.0,
            )
        ]
    ).to_parquet(source_path, index=False)

    written_path = ParquetStore(tmp_path).save_macro_rows(
        "fmp",
        "CLUSD",
        "quote",
        normalize_fmp_quote_timestamps(
            [
                _quote_row(
                    provider_symbol="CLUSD",
                    timestamp=epoch,
                    fetched_at="2026-07-29T16:02:00Z",
                    price=80.0,
                )
            ]
        ),
        pool="macro",
        mode="append_if_changed",
    )

    assert written_path == source_path
    stored = pd.read_parquet(source_path)
    assert len(stored) == 2
    assert isinstance(stored["timestamp"].dtype, pd.DatetimeTZDtype)
    assert stored["timestamp"].dt.year.eq(2026).all()
    assert stored["timestamp"].eq(
        pd.Timestamp("2026-07-29T16:00:00Z")
    ).all()
    assert stored["id"].str.contains("1970").sum() == 0


@pytest.mark.parametrize(
    "provider_timestamp",
    [
        "2026-07-29T16:00:00Z",
        pd.Timestamp("2026-07-29T16:00:00Z"),
        1_785_340_800,
        "1785340800",
        1_785_340_800_000,
        1_785_340_800_000_000,
        1_785_340_800_000_000_000,
    ],
)
def test_fmp_source_preserves_supported_provider_timestamp_units(
    tmp_path: Path,
    provider_timestamp: object,
) -> None:
    path = ParquetStore(tmp_path).save_macro_rows(
        "fmp",
        "CLUSD",
        "quote",
        [
            _quote_row(
                provider_symbol="CLUSD",
                timestamp=provider_timestamp,
                fetched_at="2026-07-29T16:01:00Z",
                price=80.0,
            )
        ],
        pool="macro",
    )

    assert path is not None
    stored = pd.read_parquet(path)
    assert stored["timestamp"].item() == pd.Timestamp(
        "2026-07-29T16:00:00Z"
    )
    assert "1970" not in stored["id"].item()


def test_fmp_source_keeps_new_receipt_for_repeated_provider_timestamp(
    tmp_path: Path,
) -> None:
    epoch = int(pd.Timestamp("2026-07-29T16:00:00Z").timestamp())
    rows = [
        _quote_row(
            provider_symbol="CLUSD",
            timestamp=epoch,
            fetched_at=f"2026-07-29T16:0{minute}:00Z",
            price=80.0,
        )
        for minute in (1, 2)
    ]
    store = ParquetStore(tmp_path)
    path = None
    for row in rows:
        path = store.save_macro_rows(
            "fmp",
            "CLUSD",
            "quote",
            normalize_fmp_quote_timestamps([row]),
            pool="macro",
            mode="append_if_changed",
        )

    assert path is not None
    stored = pd.read_parquet(path)
    assert len(stored) == 2
    assert stored["timestamp"].nunique() == 1
    assert pd.to_datetime(
        stored["available_at"],
        utc=True,
    ).tolist() == [
        pd.Timestamp("2026-07-29T16:01:00Z"),
        pd.Timestamp("2026-07-29T16:02:00Z"),
    ]
    assert stored["id"].nunique() == 2


def test_fmp_energy_rejects_ambiguous_proxy_identity() -> None:
    source = pd.DataFrame(
        [
            _quote_row(
                provider_symbol="USO",
                timestamp=int(
                    pd.Timestamp("2026-07-29T16:00:00Z").timestamp()
                ),
                fetched_at="2026-07-29T16:01:00Z",
                price=80.0,
                proxy=False,
            )
        ]
    )

    with pytest.raises(ValueError, match="unrecognized direct/proxy identity"):
        calculate_fmp_energy_context(source)


def test_fmp_fetch_normalizes_then_persists_before_materialization(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    epoch = int(pd.Timestamp("2026-07-29T16:00:00Z").timestamp())
    spec = FmpMacroContextSpec(
        key="quote",
        endpoint="batch-commodity-quotes",
        params={},
        output_symbol="CLUSD",
        kind="commodity_proxy_quote",
        provider_symbol="CLUSD",
    )

    class _CorporateProvider:
        base_url = "https://example.test"

        def corporate_specs(self, _: str) -> tuple[()]:
            return ()

        def _get_json(
            self,
            _: str,
            __: dict[str, object],
        ) -> list[object]:
            return []

    class _MacroProvider:
        base_url = "https://example.test"

        def commodity_proxy_specs(self) -> tuple[FmpMacroContextSpec, ...]:
            return (spec,)

        def fetch_commodity_proxy_quotes(
            self,
            _: tuple[FmpMacroContextSpec, ...],
        ) -> list[
            tuple[
                FmpMacroContextSpec,
                list[dict[str, object]],
                list[dict[str, object]],
                None,
            ]
        ]:
            row = _quote_row(
                provider_symbol="CLUSD",
                timestamp=epoch,
                fetched_at="2026-07-29T16:01:00Z",
                price=80.0,
            )
            return [(spec, [row], [dict(row)], None)]

    class _Store:
        root_dir = tmp_path
        source_persisted = False
        persisted_timestamp: object = None
        persistence_mode: object = None

        def save_corporate_rows(self, *_: object, **__: object) -> None:
            return None

        def save_macro_rows(
            self,
            _: str,
            __: str,
            ___: str,
            rows: list[dict[str, object]],
            **kwargs: object,
        ) -> Path:
            self.persisted_timestamp = rows[0]["timestamp"]
            self.persistence_mode = kwargs.get("mode")
            self.source_persisted = True
            return tmp_path / "source.parquet"

        def save_raw_payload(self, *_: object, **__: object) -> None:
            return None

        def save_error(self, *_: object, **__: object) -> None:
            pytest.fail("post-persistence materialization should not fail")

    store = _Store()

    def _materialize(root: Path) -> Path:
        assert root == tmp_path
        assert store.source_persisted
        assert store.persisted_timestamp == "2026-07-29T16:00:00+00:00"
        assert store.persistence_mode == "append_if_changed"
        return tmp_path / "calculated.parquet"

    monkeypatch.setattr(
        fmp_fetch,
        "FmpCorporateDataProvider",
        _CorporateProvider,
    )
    monkeypatch.setattr(
        fmp_fetch,
        "FmpMacroContextProvider",
        _MacroProvider,
    )
    monkeypatch.setattr(
        fmp_fetch,
        "materialize_fmp_energy_context",
        _materialize,
    )

    result = fmp_fetch.fetch("NVDA", store)

    assert result.data_files == 2
    assert result.error_files == 0


def _quote_row(
    *,
    provider_symbol: str,
    timestamp: object,
    fetched_at: str,
    price: float,
    proxy: bool = False,
) -> dict[str, object]:
    return {
        "symbol": "CLUSD",
        "provider_symbol": provider_symbol,
        "proxy_fallback_for": "CLUSD" if proxy else "",
        "is_proxy_fallback": proxy,
        "timestamp": timestamp,
        "fetched_at": fetched_at,
        "price": price,
    }
