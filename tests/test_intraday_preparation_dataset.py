"""Session-end label preparation is distinct from executable live inference."""
import numpy as np
import pandas as pd
import pytest

from ml.intraday import live_model_recipe as recipes
from ml.intraday.data import build_dataset, equity_sessions, PREPARATION_CONTRACT
from ml.intraday.native_interval import build_native_interval_dataset
from ml.intraday.prediction import ContractError, STEP, UnresolvedPolicy
from ml.intraday.runtime import ShadowRunner
from ml.intraday.store import ShadowStore
from tests.test_intraday_data import candles
from tests.test_intraday_live_model_recipe import selected, fit, candles as training_candles
from tests.test_intraday_market_recipe import frame as native_frame


@pytest.mark.parametrize("day,close", [
    ("2026-03-06", "2026-03-07T01:00Z"),
    ("2026-03-09", "2026-03-10T00:00Z"),
    ("2026-11-27", "2026-11-27T18:00Z"),
])
def test_final_three_closed_quarters_prepare_each_new_label_without_forecast(day, close):
    sessions = equity_sessions(day + "T12:00Z", day + "T23:00Z")
    position = next(i for i, session in enumerate(sessions) if session.session_id == day)
    session = sessions[position]
    opened, closed = pd.Timestamp(session.opened_at), pd.Timestamp(session.closed_at)
    assert closed == pd.Timestamp(close)
    frames = []
    for included in sessions[position-1:position+1]:
        start, end = pd.Timestamp(included.opened_at), pd.Timestamp(included.closed_at)
        part = candles(int((end-start)/STEP), start=start.isoformat())
        part["session_id"] = included.session_id
        part["session_open"], part["session_close"] = start.isoformat(), end.isoformat()
        frames.append(part)
    frame = pd.concat(frames, ignore_index=True)
    frame["available_at"] += pd.Timedelta(seconds=7)
    results = []
    for offset in (3, 2, 1):
        reference = closed-offset*STEP
        kwargs = dict(reference_at=reference, cutoff=reference+STEP+pd.Timedelta(seconds=8), synthetic_policy="exclude")
        dataset = build_dataset(frame, preparation_only=True, **kwargs)
        results.append(dataset)
        assert dataset.inference["target_open"] is dataset.inference["target_close"] is None
        assert dataset.inference["executable_forecast"] is False
        assert dataset.inference["snapshot"]["preparation_contract"] == PREPARATION_CONTRACT
        assert dataset.manifest["preparation_only"] is True
        assert dataset.target_close_at.max() == reference+STEP
        assert dataset.label_available_at.max() == reference+STEP+pd.Timedelta(seconds=7)
        assert all(dataset.target_close_at <= closed)
        if offset < 3:
            with pytest.raises(UnresolvedPolicy, match="crosses session closure"):
                build_dataset(frame, **kwargs)
        else:
            ordinary = build_dataset(frame, **kwargs)
            np.testing.assert_array_equal(dataset.X, ordinary.X)
            np.testing.assert_array_equal(dataset.y, ordinary.y)
            assert "preparation_only" not in ordinary.manifest
    assert [len(d.y) for d in results] == [len(results[0].y)+i for i in range(3)]
    assert results[-1].reference_at[-1] == closed-3*STEP


def test_native_adapter_preserves_explicit_preparation_marker():
    frame = native_frame()
    reference = pd.Timestamp(frame.opened_at.iloc[-1])
    dataset = build_native_interval_dataset(frame, reference_at=reference,
        cutoff=reference+STEP+pd.Timedelta(seconds=8), preparation_only=True)
    assert dataset.manifest["preparation_contract"] == PREPARATION_CONTRACT
    assert dataset.inference["snapshot"]["executable_forecast"] is False


def test_manual_runtime_rejects_prepared_dataset_without_explicit_nontrading_fit(tmp_path, monkeypatch):
    _, _, _, config, runtime = selected(tmp_path)
    frame = training_candles()
    reference = frame.opened_at.iloc[-1]
    kwargs = dict(reference_at=reference, cutoff=reference+STEP+pd.Timedelta(seconds=3), synthetic_policy="exclude")
    dataset = runtime["dataset_builder"].for_preparation(frame, **kwargs)
    with pytest.raises(ContractError, match="cannot train a runtime forecast"):
        fit(runtime, config, dataset)
    seen = []
    def fitted(X, y, inference_X, **arguments):
        seen.append(arguments["dataset_metadata"])
        return {"configured_count":2, "successful_count":2, "complete":True, "consensus_status":"full",
                "probability_not_down":.5,
                "outputs":[{"name":s.name, "status":"success"} for s in arguments["config"]]}
    monkeypatch.setattr(recipes, "fit_ensemble", fitted)
    result = fit(runtime, config, dataset, preparation_only=True)
    assert seen == [dataset.manifest]
    assert result["preparation_only"] is True and result["executable_forecast"] is False
    ordinary = runtime["dataset_builder"](frame, **kwargs)
    with pytest.raises(ContractError, match="explicit fit mode"):
        fit(runtime, config, ordinary, preparation_only=True)


def test_forecast_runner_rejects_preparation_snapshot_before_generic_fit_and_after_restart(tmp_path):
    _, _, _, config, runtime = selected(tmp_path)
    frame = training_candles()
    reference = frame.opened_at.iloc[-1]
    cutoff = reference+STEP+pd.Timedelta(seconds=3)
    prepared = runtime["dataset_builder"].for_preparation(frame, reference_at=reference,
        cutoff=cutoff, synthetic_policy="exclude")
    store = ShadowStore(tmp_path/"preparation-boundary.sqlite3")
    def no_fit(*args, **kwargs):
        pytest.fail("Preparation must be rejected before any generic model fit")
    runner = ShadowRunner(store=store, pc="Scout", symbols=("ABCL",), config=config,
        clock=lambda:cutoff.to_pydatetime(), trainer=no_fit, dataset_builder=lambda *a, **kw:prepared)
    first = runner.run_cycle({"ABCL":frame}, reference_at=reference, cutoff=cutoff)
    assert "preparation snapshots cannot publish" in first["failures"]["ABCL"]["message"]
    assert store.forecast(first["cycle_id"],"ABCL") is None
    assert store.inputs(first["cycle_id"],"ABCL") is not None
    # An exact saved snapshot also cannot bypass the boundary after restart.
    def no_rebuild(*args, **kwargs):
        pytest.fail("Restart should reuse the original immutable snapshot")
    restarted = ShadowRunner(store=store, pc="Scout", symbols=("ABCL",), config=config,
        clock=lambda:cutoff.to_pydatetime(), trainer=no_fit, dataset_builder=no_rebuild)
    second = restarted.run_cycle({"ABCL":frame}, reference_at=reference, cutoff=cutoff,
        retry_symbols=("ABCL",))
    assert second["cycle_id"] == first["cycle_id"]
    assert "preparation snapshots cannot publish" in second["failures"]["ABCL"]["message"]
    assert not store.forecasts(pc="Scout")
