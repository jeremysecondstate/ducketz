from __future__ import annotations

import json
from pathlib import Path
from threading import Barrier
from types import SimpleNamespace

import pandas as pd
import pytest

from ml.option_pricing.opra import (
    DEFAULT_EMULATED_PREDICTION_LATENCY_SECONDS,
    DEFAULT_SYMBOLS,
    OPRA_METADATA_TIMEOUT_SECONDS,
    OPRA_PRICE_SCALE,
    OPRA_TIMESERIES_TIMEOUT_SECONDS,
    OpraRequest,
    OpraImportError,
    cbbo_request_coverage_report,
    configure_historical_client_timeouts,
    definition_requests,
    estimate_requests,
    normalize_cbbo_records,
    normalize_definition_records,
    opra_storage_capacity_report,
    point_in_time_definition_asof,
    read_opra_import,
    research_benchmark_schedule_report,
    required_eligibility_clusters_per_symbol,
    resolve_market_schedule,
    run_import_phase,
    schedule_contract_report,
    select_historical_source_target,
)
from ml.option_pricing.eligibility import publish_eligibility_policy
from ml.option_pricing.research_benchmark import run_spy_exact_gp_benchmark


class _Metadata:
    def __init__(self, cost: float) -> None:
        self.cost = cost
        self.calls: list[dict[str, object]] = []

    def get_cost(self, **kwargs: object) -> float:
        self.calls.append(dict(kwargs))
        return self.cost

    def get_billable_size(self, **_kwargs: object) -> int:
        return 1024


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


def _approved_authorization(
    tmp_path: Path,
    *,
    client: object,
    schedule: tuple[object, ...],
    policy: object,
    max_cost_usd: float,
    imported_at: str,
) -> Path:
    path = tmp_path / f"opra-authorization-{len(list(tmp_path.glob('opra-authorization-*')))}.json"
    run_import_phase(
        tmp_path,
        client=client,
        schedule=schedule,
        max_cost_usd=max_cost_usd,
        reporter=None,
        eligibility_policy_artifact=policy,
        eligibility_scope=False,
        authorization_template_path=path,
    )
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload.update(
        {
            "status": "APPROVED",
            "approval_id": "fixture-operator-approval",
            "approved_by": "pytest-operator",
            "approved_at": (
                pd.Timestamp(imported_at) - pd.Timedelta(minutes=1)
            ).isoformat(),
            "external_cost_authorized": True,
            "datastore_write_authorized": True,
        }
    )
    path.write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return path


def test_opra_default_is_cost_only_and_never_calls_get_range(tmp_path: Path) -> None:
    client = _Client()
    result = run_import_phase(
        tmp_path,
        client=client,
        schedule=_schedule(),
        reporter=None,
        eligibility_scope=False,
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


def test_storage_report_includes_expanded_materialization_size(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "ml.option_pricing.opra.shutil.disk_usage",
        lambda _path: SimpleNamespace(
            total=20_000_000_000,
            used=0,
            free=20_000_000_000,
        ),
    )

    report = opra_storage_capacity_report(
        tmp_path,
        estimated_billable_size_bytes=1_024,
    )

    assert report["estimated_expanded_bytes"] == 2_048
    assert report["required_free_bytes"] == (
        report["estimated_expanded_bytes"] + report["immutable_reserve_bytes"]
    )
    assert report["status"] == "PASS"


def test_production_schedule_uses_all_six_symbols_and_derived_cluster_count() -> None:
    short = resolve_market_schedule(
        symbols=DEFAULT_SYMBOLS,
        start_date="2026-02-06",
        end_date="2026-08-06",
    )
    complete = resolve_market_schedule(
        symbols=DEFAULT_SYMBOLS,
        start_date="2026-02-05",
        end_date="2026-08-06",
    )
    assert schedule_contract_report(short)["status"] == "NOT_PROVEN"
    complete_report = schedule_contract_report(complete)
    assert complete_report["status"] == "PASS"
    required = required_eligibility_clusters_per_symbol()
    assert complete_report["required_symbols"] == list(DEFAULT_SYMBOLS)
    assert complete_report["clusters_per_symbol"] == {
        symbol: required for symbol in DEFAULT_SYMBOLS
    }
    assert complete_report["required_clusters_per_symbol"] == required


def test_spy_schedule_is_separate_and_research_only() -> None:
    schedule = resolve_market_schedule(
        symbols=("SPY",),
        start_date="2026-02-05",
        end_date="2026-08-06",
    )
    report = research_benchmark_schedule_report(schedule)
    assert report["status"] == "PASS"
    assert report["scope"] == "RESEARCH_BENCHMARK_ONLY"
    assert report["production_eligible"] is False
    assert report["required_symbols"] == ["SPY"]
    assert definition_requests(schedule)[0].symbols == ("SPY.OPT",)
    assert schedule_contract_report(schedule)["status"] == "NOT_PROVEN"


def test_spy_exact_gp_benchmark_is_bounded_and_research_only() -> None:
    rows: list[dict[str, object]] = []
    for session_index in range(4):
        target = pd.Timestamp("2026-07-06T14:00:00Z") + pd.Timedelta(
            days=session_index
        )
        for call_put in ("CALL", "PUT"):
            for contract_index in range(6):
                underlying = 600.0 + session_index
                black_scholes = 4.0 + 0.1 * contract_index
                residual = 0.0002 * (contract_index - 2) + 0.00005 * session_index
                rows.append(
                    {
                        "symbol": "SPY",
                        "call_put": call_put,
                        "contract_symbol": (
                            f"SPY-{session_index}-{call_put}-{contract_index}"
                        ),
                        "target_snapshot_for": target,
                        "observed_mid": black_scholes + residual * underlying,
                        "black_scholes_price": black_scholes,
                        "normalized_residual": residual,
                        "underlying_price": underlying,
                        "strike": 590.0 + 4.0 * contract_index,
                        "risk_free_rate": 0.04,
                        "lagged_implied_volatility": 0.20 + 0.002 * contract_index,
                        "target_years_to_expiration": 30.0 / 365.0,
                        "dividend_yield": 0.012,
                        "sample_status": "AVAILABLE",
                        "volume": 10 + contract_index,
                    }
                )
    result = run_spy_exact_gp_benchmark(
        pd.DataFrame(rows),
        maximum_rows=100,
        maximum_runtime_seconds=30.0,
    )
    assert result.report["research_only"] is True
    assert result.report["production_eligible"] is False
    assert result.report["paper_may_june_2019_experiment_claimed"] is False
    assert set(result.report["routes"]) == {"CALL", "PUT"}
    assert {value["status"] for value in result.report["routes"].values()} == {
        "COMPLETE"
    }
    assert not result.predictions.empty
    assert result.predictions["symbol"].eq("SPY").all()
    assert result.predictions["exact_residual_gp_standard_deviation"].ge(0.0).all()


def test_cbbo_plan_requires_call_and_put_for_every_scheduled_point() -> None:
    point = _schedule()[0]
    target = pd.Timestamp(point.target_snapshot_for)
    base = {
        "dataset": "OPRA.PILLAR",
        "schema": "cbbo-1m",
        "stype_in": "raw_symbol",
        "start": (target - pd.Timedelta(minutes=5)).isoformat(),
        "end": (
            target
            + pd.Timedelta(seconds=DEFAULT_EMULATED_PREDICTION_LATENCY_SECONDS)
            + pd.Timedelta(minutes=5)
        ).isoformat(),
        "purpose": f"SOURCE_BACKWARD_TARGET_FORWARD:NVDA:{target.isoformat()}",
        "output_name": "cbbo-NVDA-test.dbn.zst",
    }
    call_only = OpraRequest(
        **base,
        symbols=("NVDA  260821C00100000",),
    )
    assert cbbo_request_coverage_report((point,), (call_only,))["status"] == "NOT_PROVEN"
    both = OpraRequest(
        **base,
        symbols=("NVDA  260821C00100000", "NVDA  260821P00100000"),
    )
    report = cbbo_request_coverage_report((point,), (both,))
    assert report["status"] == "PASS"
    assert report["route_point_counts"] == {
        "NVDA/call": 1,
        "NVDA/put": 1,
    }


def test_opra_ceiling_rejects_before_any_paid_request(tmp_path: Path) -> None:
    client = _Client(cost=1.25)
    policy = publish_eligibility_policy(
        tmp_path, published_at="2026-07-01T00:00:00Z"
    )
    with pytest.raises(OpraImportError, match="exceeds ceiling"):
        run_import_phase(
            tmp_path,
            client=client,
            schedule=_schedule(),
            execute=True,
            max_cost_usd=1.0,
            reporter=None,
            eligibility_policy_artifact=policy,
            eligibility_scope=False,
        )
    assert client.metadata.calls
    assert client.timeseries.calls == []

    template = tmp_path / "underfunded-authorization.json"
    with pytest.raises(OpraImportError, match="no authorization template was written"):
        run_import_phase(
            tmp_path,
            client=client,
            schedule=_schedule(),
            max_cost_usd=1.0,
            reporter=None,
            eligibility_scope=False,
            authorization_template_path=template,
        )
    assert not template.exists()


def test_mocked_paid_phase_is_immutable_verified_and_resumable(tmp_path: Path) -> None:
    client = _Client(cost=0.01)
    policy = publish_eligibility_policy(
        tmp_path, published_at="2026-07-01T00:00:00Z"
    )
    authorization = _approved_authorization(
        tmp_path,
        client=client,
        schedule=_schedule(),
        policy=policy,
        max_cost_usd=0.10,
        imported_at="2026-07-07T00:00:00Z",
    )
    result = run_import_phase(
        tmp_path,
        client=client,
        schedule=_schedule(),
        execute=True,
        max_cost_usd=0.10,
        imported_at="2026-07-07T00:00:00Z",
        reporter=None,
        eligibility_policy_artifact=policy,
        eligibility_scope=False,
        authorization_record=authorization,
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
        eligibility_policy_artifact=policy,
        eligibility_scope=False,
        authorization_record=authorization,
    )
    assert resumed.status == "ALREADY_COMMITTED"
    assert second.metadata.calls
    assert second.timeseries.calls == []


def test_paid_phase_requires_exact_authorization_and_current_disk_capacity(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _Client(cost=0.01)
    policy = publish_eligibility_policy(
        tmp_path, published_at="2026-07-01T00:00:00Z"
    )
    with pytest.raises(OpraImportError, match="authorization-record"):
        run_import_phase(
            tmp_path,
            client=client,
            schedule=_schedule(),
            execute=True,
            max_cost_usd=0.10,
            imported_at="2026-07-07T00:00:00Z",
            reporter=None,
            eligibility_policy_artifact=policy,
            eligibility_scope=False,
        )
    assert client.timeseries.calls == []

    authorization = _approved_authorization(
        tmp_path,
        client=client,
        schedule=_schedule(),
        policy=policy,
        max_cost_usd=0.10,
        imported_at="2026-07-07T00:00:00Z",
    )
    tampered = json.loads(authorization.read_text(encoding="utf-8"))
    tampered["plan"]["request_count"] = 999
    authorization.write_text(
        json.dumps(tampered, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    with pytest.raises(OpraImportError, match="does not approve this exact plan"):
        run_import_phase(
            tmp_path,
            client=client,
            schedule=_schedule(),
            execute=True,
            max_cost_usd=0.10,
            imported_at="2026-07-07T00:00:00Z",
            reporter=None,
            eligibility_policy_artifact=policy,
            eligibility_scope=False,
            authorization_record=authorization,
        )
    assert client.timeseries.calls == []

    valid = _approved_authorization(
        tmp_path,
        client=client,
        schedule=_schedule(),
        policy=policy,
        max_cost_usd=0.10,
        imported_at="2026-07-07T00:00:00Z",
    )
    monkeypatch.setattr(
        "ml.option_pricing.opra.shutil.disk_usage",
        lambda _path: SimpleNamespace(total=10, used=9, free=1),
    )
    with pytest.raises(OpraImportError, match="storage-capacity"):
        run_import_phase(
            tmp_path,
            client=client,
            schedule=_schedule(),
            execute=True,
            max_cost_usd=0.10,
            imported_at="2026-07-07T00:00:00Z",
            reporter=None,
            eligibility_policy_artifact=policy,
            eligibility_scope=False,
            authorization_record=valid,
        )
    assert client.timeseries.calls == []


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


def test_offline_outcome_must_follow_emulated_prediction_availability() -> None:
    cbbo = normalize_cbbo_records(
        pd.DataFrame(
            {
                "raw_symbol": ["NVDA   260821C00100000"] * 3,
                "ts_recv": [
                    "2026-07-06T13:59:00Z",
                    "2026-07-06T14:00:30Z",
                    "2026-07-06T14:01:01Z",
                ],
                "bid_px_00": [1_900_000_000, 2_000_000_000, 2_100_000_000],
                "ask_px_00": [2_000_000_000, 2_100_000_000, 2_200_000_000],
            }
        )
    )
    _source, outcome = select_historical_source_target(
        cbbo,
        target_snapshot_for="2026-07-06T14:00:00Z",
        prediction_available_at="2026-07-06T14:01:00Z",
    )
    assert outcome.iloc[0]["quote_timestamp"] == pd.Timestamp(
        "2026-07-06T14:01:01Z"
    )
    assert not outcome["quote_timestamp"].eq(
        pd.Timestamp("2026-07-06T14:00:30Z")
    ).any()


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


def test_metadata_retries_are_bounded_and_timeouts_are_explicit() -> None:
    class FlakyMetadata:
        TIMEOUT = 0

        def __init__(self) -> None:
            self.calls = 0

        def get_cost(self, **_kwargs: object) -> float:
            self.calls += 1
            if self.calls < 3:
                raise TimeoutError("fixture timeout")
            return 0.25

    class Endpoint:
        TIMEOUT = 0

    client = type(
        "Client",
        (),
        {"metadata": FlakyMetadata(), "timeseries": Endpoint()},
    )()
    sleeps: list[float] = []
    estimates = estimate_requests(
        client,
        definition_requests(_schedule()),
        reporter=None,
        sleeper=sleeps.append,
    )
    assert estimates[0].estimated_cost_usd == pytest.approx(0.25)
    assert client.metadata.calls == 3
    assert sleeps == [1.0, 2.0]

    configure_historical_client_timeouts(client)
    assert client.metadata.TIMEOUT == OPRA_METADATA_TIMEOUT_SECONDS
    assert client.timeseries.TIMEOUT == OPRA_TIMESERIES_TIMEOUT_SECONDS


def test_metadata_estimates_use_bounded_concurrency_and_keep_request_order() -> None:
    requests = definition_requests(
        resolve_market_schedule(
            symbols=("NVDA",),
            start_date="2026-07-06",
            end_date="2026-07-07",
            market_times=("10:00",),
        )
    )
    rendezvous = Barrier(2)

    class ConcurrentMetadata:
        def get_cost(self, **_kwargs: object) -> float:
            rendezvous.wait(timeout=2.0)
            return 0.01

        def get_billable_size(self, **_kwargs: object) -> int:
            return 1024

    client = type("Client", (), {"metadata": ConcurrentMetadata()})()
    estimates = estimate_requests(
        client,
        requests,
        reporter=None,
        maximum_workers=2,
    )
    assert [item.request.output_name for item in estimates] == [
        request.output_name for request in requests
    ]


def test_interrupted_paid_phase_resumes_verified_requests_without_redownload(
    tmp_path: Path,
) -> None:
    schedule = resolve_market_schedule(
        symbols=("NVDA",),
        start_date="2026-07-06",
        end_date="2026-07-07",
        market_times=("10:00",),
    )

    class FailingTimeseries(_Timeseries):
        def get_range(self, **kwargs: object) -> None:
            self.calls.append(dict(kwargs))
            if len(self.calls) == 2:
                raise TimeoutError("fixture paid timeout")
            Path(str(kwargs["path"])).write_bytes(b"first verified request")

    first = _Client(cost=0.01)
    policy = publish_eligibility_policy(
        tmp_path, published_at="2026-07-01T00:00:00Z"
    )
    first.timeseries = FailingTimeseries()
    authorization = _approved_authorization(
        tmp_path,
        client=first,
        schedule=schedule,
        policy=policy,
        max_cost_usd=0.10,
        imported_at="2026-07-08T00:00:00Z",
    )
    with pytest.raises(OpraImportError, match="after 1 bounded attempt"):
        run_import_phase(
            tmp_path,
            client=first,
            schedule=schedule,
            execute=True,
            max_cost_usd=0.10,
            imported_at="2026-07-08T00:00:00Z",
            reporter=None,
            eligibility_policy_artifact=policy,
            eligibility_scope=False,
            authorization_record=authorization,
        )
    assert len(first.timeseries.calls) == 2

    second = _Client(cost=0.01)
    resumed = run_import_phase(
        tmp_path,
        client=second,
        schedule=schedule,
        execute=True,
        max_cost_usd=0.10,
        imported_at="2026-07-09T00:00:00Z",
        reporter=None,
        eligibility_policy_artifact=policy,
        eligibility_scope=False,
        authorization_record=authorization,
    )
    assert resumed.status == "IMPORTED"
    assert resumed.downloaded_count == 1
    assert len(second.timeseries.calls) == 1
    assert Path(str(second.timeseries.calls[0]["path"])).name == (
        "definitions-2026-07-07.dbn.zst"
    )
    assert resumed.evidence_directory is not None
    read_opra_import(resumed.evidence_directory, datastore_root=tmp_path)
