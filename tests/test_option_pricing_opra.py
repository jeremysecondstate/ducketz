from __future__ import annotations

from pathlib import Path

import pandas as pd
import pytest

from ml.option_pricing.opra import (
    OPRA_PRICE_SCALE,
    OpraImportError,
    normalize_cbbo_records,
    normalize_definition_records,
    point_in_time_definition_asof,
    read_opra_import,
    resolve_market_schedule,
    run_import_phase,
    select_historical_source_target,
)


class _Metadata:
    def __init__(self, cost: float) -> None:
        self.cost = cost
        self.calls: list[dict[str, object]] = []

    def get_cost(self, **kwargs: object) -> float:
        self.calls.append(dict(kwargs))
        return self.cost


class _Timeseries:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    def get_range(self, **kwargs: object) -> None:
        self.calls.append(dict(kwargs))
        Path(str(kwargs["path"])).write_bytes(b"fixture dbn")


class _Client:
    def __init__(self, cost: float = 0.01) -> None:
        self.metadata = _Metadata(cost)
        self.timeseries = _Timeseries()


def _schedule() -> tuple[object, ...]:
    return resolve_market_schedule(
        symbols=("NVDA",),
        start_date="2026-07-06",
        end_date="2026-07-06",
        market_times=("10:00",),
    )


def test_opra_default_is_cost_only_and_never_calls_get_range(tmp_path: Path) -> None:
    client = _Client()
    result = run_import_phase(
        tmp_path,
        client=client,
        schedule=_schedule(),
        reporter=None,
    )
    assert result.status == "ESTIMATE_ONLY"
    assert result.phase == "definitions"
    assert client.metadata.calls
    assert client.timeseries.calls == []
    request = client.metadata.calls[0]
    assert request["dataset"] == "OPRA.PILLAR"
    assert request["schema"] == "definition"
    assert request["stype_in"] == "parent"
    assert request["symbols"] == ["NVDA.OPT"]


def test_opra_ceiling_rejects_before_any_paid_request(tmp_path: Path) -> None:
    client = _Client(cost=1.25)
    with pytest.raises(OpraImportError, match="exceeds ceiling"):
        run_import_phase(
            tmp_path,
            client=client,
            schedule=_schedule(),
            execute=True,
            max_cost_usd=1.0,
            reporter=None,
        )
    assert client.metadata.calls
    assert client.timeseries.calls == []


def test_mocked_paid_phase_is_immutable_verified_and_resumable(tmp_path: Path) -> None:
    client = _Client(cost=0.01)
    result = run_import_phase(
        tmp_path,
        client=client,
        schedule=_schedule(),
        execute=True,
        max_cost_usd=0.10,
        imported_at="2026-07-07T00:00:00Z",
        reporter=None,
    )
    assert result.status == "IMPORTED"
    assert result.downloaded_count == 1
    assert result.evidence_directory is not None
    read_opra_import(result.evidence_directory, datastore_root=tmp_path)
    assert Path(client.timeseries.calls[0]["path"]).name == "definitions-2026-07-06.dbn.zst"

    second = _Client(cost=0.01)
    resumed = run_import_phase(
        tmp_path,
        client=second,
        schedule=_schedule(),
        execute=True,
        max_cost_usd=0.10,
        imported_at="2026-07-08T00:00:00Z",
        reporter=None,
    )
    assert resumed.status == "ALREADY_COMMITTED"
    assert second.metadata.calls
    assert second.timeseries.calls == []


def test_opra_price_scale_definition_asof_expiration_and_interval_semantics() -> None:
    raw_definitions = pd.DataFrame(
        {
            "raw_symbol": ["NVDA   260821C00100000"] * 3,
            "underlying": ["NVDA"] * 3,
            "ts_event": [
                "2026-07-05T12:00:00Z",
                "2026-07-06T13:00:00Z",
                "2026-07-06T15:00:00Z",
            ],
            "expiration": [
                pd.Timestamp("2026-08-21T00:00:00Z").value,
            ]
            * 3,
            "instrument_class": ["C"] * 3,
            "strike_price": [100 * OPRA_PRICE_SCALE, 101 * OPRA_PRICE_SCALE, 102 * OPRA_PRICE_SCALE],
            "unit_of_measure_qty": [100] * 3,
        }
    )
    definitions = normalize_definition_records(raw_definitions)
    selected = point_in_time_definition_asof(definitions, "2026-07-06T14:00:00Z")
    assert len(selected) == 1
    assert selected.iloc[0]["strike"] == pytest.approx(101.0)
    assert selected.iloc[0]["expiration_date"] == pd.Timestamp("2026-08-21T00:00:00Z")
    assert selected.iloc[0]["call_put"] == "call"

    cbbo = normalize_cbbo_records(
        pd.DataFrame(
            {
                "raw_symbol": ["NVDA   260821C00100000"] * 3,
                "ts_recv": [
                    "2026-07-06T13:59:00Z",
                    "2026-07-06T14:01:00Z",
                    "2026-07-06T14:03:00Z",
                ],
                "bid_px_00": [1_920_000_000, 2_000_000_000, 2_100_000_000],
                "ask_px_00": [1_940_000_000, 2_040_000_000, 2_140_000_000],
                "publisher_id": [30, 30, 30],
            }
        )
    )
    assert cbbo.iloc[0]["bid"] == pytest.approx(1.92)
    assert cbbo.iloc[0]["mid"] == pytest.approx(1.93)
    assert cbbo.iloc[0]["interval_start"] == pd.Timestamp("2026-07-06T13:58:00Z")
    source, target = select_historical_source_target(
        cbbo,
        target_snapshot_for="2026-07-06T14:00:00Z",
    )
    assert source.iloc[0]["quote_timestamp"] == pd.Timestamp("2026-07-06T13:59:00Z")
    assert target.iloc[0]["quote_timestamp"] == pd.Timestamp("2026-07-06T14:01:00Z")


def test_schedule_uses_dst_and_excludes_post_close_early_close_times() -> None:
    winter = resolve_market_schedule(
        symbols=("NVDA",),
        start_date="2026-01-05",
        end_date="2026-01-05",
        market_times=("10:00",),
    )
    summer = resolve_market_schedule(
        symbols=("NVDA",),
        start_date="2026-07-06",
        end_date="2026-07-06",
        market_times=("10:00",),
    )
    assert winter[0].target_snapshot_for == "2026-01-05T15:00:00+00:00"
    assert summer[0].target_snapshot_for == "2026-07-06T14:00:00+00:00"
    early = resolve_market_schedule(
        symbols=("NVDA",),
        start_date="2026-11-27",
        end_date="2026-11-27",
        market_times=("12:30", "15:00"),
    )
    assert [point.market_time for point in early] == ["12:30"]
