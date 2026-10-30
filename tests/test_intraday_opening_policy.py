"""Opening preparation keeps the original target and cannot execute before 04:00."""
from datetime import date, datetime, timedelta
from types import SimpleNamespace

import numpy as np
import pandas as pd
import pytest

from ml.intraday.execution_policy import submission_gate
from ml.intraday.live_session import eligible_reference
from ml.intraday.prediction import (
    Candle, ContractError, PACIFIC, STEP, TARGET_VERSION, UnresolvedPolicy,
    digest, instant, issue_prediction,
)


def candle(reference, *, delay=1):
    ref = instant(reference)
    return Candle("AAPL", ref.isoformat(), (ref + STEP + timedelta(seconds=delay)).isoformat(),
                  "100", "101", "99", "100", "1000", "offline-observation")


def predict(reference, issued, *, day=None, rows=None, infer=lambda _: "0.5001"):
    return issue_prediction(rows if rows is not None else [candle(reference)], symbol="AAPL",
        issued_at=issued, eligible_session=day or instant(issued).astimezone(PACIFIC).date(),
        model_version="offline-fixture", model_target_version=TARGET_VERSION, infer=infer)


@pytest.mark.parametrize("day,offset", [("2026-10-30", "-07:00"), ("2026-11-02", "-08:00")])
def test_opening_prediction_keeps_reference_and_target_across_dst(day, offset):
    reference = f"{day}T03:30:00{offset}"
    issued = f"{day}T03:45:30{offset}"
    result = predict(reference, issued).validate()
    assert instant(result.reference_at) == instant(reference)
    assert instant(result.target_open) == instant(f"{day}T04:00:00{offset}")
    assert instant(result.target_close) == instant(f"{day}T04:15:00{offset}")
    assert result.probability_not_down == "0.5001" and result.reference_open == "100"
    assert result.target_version == TARGET_VERSION
    assert instant(result.information_available_at) <= instant(result.issued_at)


def test_opening_prediction_needs_actual_completed_reference_and_causal_inputs():
    reference = "2026-10-30T03:30:00-07:00"
    issued = "2026-10-30T03:45:30-07:00"
    calls = []
    with pytest.raises(ContractError, match="Fresh completed"):
        predict(reference, issued, rows=[candle(reference, delay=31)], infer=lambda _: calls.append(True))
    assert calls == []
    with pytest.raises(ContractError, match="Issue date"):
        predict(reference, issued, day=date(2026, 10, 29))
    with pytest.raises(UnresolvedPolicy, match="before 04:00"):
        predict("2026-10-30T03:15:00-07:00", "2026-10-30T03:30:30-07:00")
    with pytest.raises(UnresolvedPolicy, match="Final 16:45"):
        predict("2026-10-30T16:30:00-07:00", "2026-10-30T16:45:30-07:00")


@pytest.mark.parametrize("day", ["2026-10-31", "2026-12-25", "2026-11-27"])
def test_closed_and_regular_only_short_sessions_do_not_invent_opening(day):
    reference = datetime.fromisoformat(day).replace(hour=3, minute=30, tzinfo=PACIFIC)
    issued = reference + STEP + timedelta(seconds=30)
    assert not eligible_reference(reference)
    with pytest.raises(ContractError, match="eligible equity session"):
        predict(reference, issued)


@pytest.mark.parametrize("reference,expected", [
    ("03:15", False), ("03:30", True), ("03:45", True), ("04:00", True),
    ("16:15", True), ("16:30", False),
])
def test_live_reference_admission_uses_target_lower_bound_and_existing_final_cutoff(reference, expected):
    assert eligible_reference(f"2026-10-30T{reference}:00-07:00") is expected


@pytest.mark.parametrize("day,offset", [("2026-10-30", "-07:00"), ("2026-11-02", "-08:00")])
def test_submission_window_is_half_open_and_dst_aware(day, offset):
    session = date.fromisoformat(day)
    assert submission_gate(as_of=f"{day}T03:59:59.999999{offset}", eligible_session=session).state == "before_session_open"
    assert submission_gate(as_of=f"{day}T04:00:00{offset}", eligible_session=session).state == "before_submission_cutoff"
    assert submission_gate(as_of=f"{day}T16:44:59.999999{offset}", eligible_session=session).state == "before_submission_cutoff"
    assert submission_gate(as_of=f"{day}T16:45:00{offset}", eligible_session=session).state == "submission_cutoff"


def test_executor_rejects_preopening_before_account_quotes_sizing_or_order_work():
    from ml.intraday.account_execution import IntradayAccountExecutor

    payload = dict(pc="Atlas", symbol="AAPL", mode="live", availability_basis="observed",
        reference_at="2026-10-30T03:30:00-07:00", issued_at="2026-10-30T03:45:30-07:00",
        target_open="2026-10-30T04:00:00-07:00", target_close="2026-10-30T04:15:00-07:00",
        target_version=TARGET_VERSION, ensemble_status="full", successful_count=1, configured_count=1)
    forecast = {**payload, "forecast_id": digest(payload)}
    executor = object.__new__(IntradayAccountExecutor)
    executor.actor, executor.symbols = "Atlas", ("AAPL",)
    executor.session_started_at = instant("2026-10-30T03:30:00-07:00")
    executor.clock = lambda: "2026-10-30T03:59:59.999999-07:00"
    executor._record = lambda value, disposition: (value, disposition)
    executor._account = lambda **kwargs: pytest.fail("Preopening cannot reach account or broker work")
    assert executor.step(forecast) == (forecast, "before_session_open")


def test_runtime_prepares_opening_with_existing_model_and_target_without_execution(tmp_path):
    from ml.intraday.runtime import ResearchConfig, ShadowRunner
    from ml.intraday.store import ShadowStore

    reference = instant("2026-10-30T03:30:00-07:00")
    issued = reference + STEP + timedelta(seconds=30)
    rows = pd.DataFrame([dict(symbol="AAPL", opened_at=reference,
        available_at=reference+STEP+timedelta(seconds=1), availability_kind="actual",
        interval_seconds=900, open=100, high=101, low=99, close=100, volume=100, synthetic=False)])

    def dataset(frame, *, cutoff, reference_at, **kwargs):
        assert reference_at == reference and instant(cutoff) == issued
        return SimpleNamespace(X=np.array([[0], [1], [0], [1]]), y=np.array([0, 1, 0, 1]),
            inference_X=np.array([[1]]), feature_names=("x",),
            label_available_at=pd.date_range(reference-timedelta(hours=2), periods=4, freq="15min"),
            reference_at=pd.date_range(reference-timedelta(hours=3), periods=4, freq="15min"),
            target_close_at=pd.date_range(reference-timedelta(hours=2, minutes=15), periods=4, freq="15min"),
            inference=dict(symbol="AAPL", reference_at=reference.isoformat(), reference_open=100,
                information_available_at=(reference+STEP+timedelta(seconds=1)).isoformat(), availability_kind="actual"),
            manifest={"training_rows": 4})

    calls = []

    def trainer(*args, config, **kwargs):
        calls.append(config)
        return dict(outputs=[dict(name="fixture", status="success", probability_not_down=.5001,
                    version="offline-fixture", training_seconds=0, inference_seconds=0)],
            configured_count=1, successful_count=1, complete=True, probability_not_down=.5001,
            training_seconds=0, inference_seconds=0)

    store = ShadowStore(tmp_path / "shadow.sqlite3")
    runner = ShadowRunner(store=store, pc="Atlas", symbols=("AAPL",),
        config=ResearchConfig(models=("fixture",), symbol_workers=1), dataset_builder=dataset,
        trainer=trainer, clock=lambda: issued.isoformat())
    result = runner.run_cycle({"AAPL": rows}, reference_at=reference, mode="live", allow_execution=False)
    assert result["failed_symbols"] == 0, result.get("failures")
    forecast, = store.forecasts()
    assert len(calls) == 1 and forecast["probability_not_down"] == .5001
    assert instant(forecast["target_open"]) == reference+2*STEP
    assert instant(forecast["target_close"]) == reference+3*STEP
    assert instant(forecast["issued_at"]) < instant(forecast["target_open"])

