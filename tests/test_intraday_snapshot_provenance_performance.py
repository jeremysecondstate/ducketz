"""Final runtime candle snapshots preserve evidence without per-column attr copies."""
from copy import deepcopy
from types import SimpleNamespace

import pandas as pd
import pytest

from ml.intraday.runtime import ResearchConfig, ShadowRunner, _jsonable
from ml.intraday.store import ShadowStore
from tests.test_intraday_dataset_provenance_performance import CopyTrackedList
from tests.test_intraday_runtime import REF, dataset, history, trained


@pytest.mark.parametrize("count", [32, 64])
@pytest.mark.parametrize("wrapped", [False, True])
def test_final_candle_snapshot_bounds_attr_copies_and_preserves_exact_rows(tmp_path, count, wrapped):
    frame = pd.concat([history("AAPL")] * count, ignore_index=True)
    frame["opened_at"] = pd.date_range(REF - pd.Timedelta(minutes=15 * (count - 3)), periods=count, freq="15min")
    frame["available_at"] = frame.opened_at + pd.Timedelta(minutes=15)
    frame["native_evidence"] = [{"original": index, "synthetic": False} for index in range(count)]
    # An earlier candle received after the frozen cutoff must still be excluded.
    cutoff = REF + pd.Timedelta(minutes=16)
    frame.loc[0, "available_at"] = cutoff + pd.Timedelta(seconds=1)
    expected = _jsonable(frame.loc[frame.available_at.le(cutoff) & frame.opened_at.le(REF)].to_dict("records"))
    provenance = {"source_coverage": CopyTrackedList({"receipt": index} for index in range(64))}
    frame.attrs = {"intraday_history": provenance, "other_original": {"retain": [1, 2, 3]}}
    original = frame.copy(deep=True)
    original_attrs = deepcopy(frame.attrs)

    def build(source, **kwargs):
        result = dataset(source, **kwargs)
        result.manifest["source_history"] = provenance
        return result

    runner = ShadowRunner(store=ShadowStore(tmp_path / "shadow.sqlite3"), pc="Atlas", symbols=("AAPL",),
        config=ResearchConfig(models=("fixture",)), dataset_builder=build, trainer=trained,
        clock=lambda: cutoff.isoformat())
    supplied = SimpleNamespace(frame=frame, manifest=provenance) if wrapped else frame
    CopyTrackedList.copies = 0
    result = runner.run_cycle({"AAPL": supplied}, reference_at=REF, mode="live")
    copies = CopyTrackedList.copies

    assert result["failed_symbols"] == 0
    _, snapshot = runner.store.inputs(result["cycle_id"], "AAPL")
    assert snapshot["candles"] == expected
    assert snapshot["manifest"]["source_history"] == provenance
    assert snapshot["source_manifest"] == (provenance if wrapped else {})
    forecast = result["forecasts"]["AAPL"]
    assert forecast["probability_not_down"] == .6
    assert forecast["direction"] == "BULLISH"
    assert forecast["target_close"] == (REF + pd.Timedelta(minutes=45)).isoformat()
    assert copies <= 2
    pd.testing.assert_frame_equal(frame, original)
    assert frame.attrs == original_attrs
