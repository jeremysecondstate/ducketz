"""Persist the input candle's provenance with the actual immutable forecast."""
import pytest

from app.ui.intraday_data import reference_input_provenance
from tests.test_intraday_runtime import REF, history, runner, trained


@pytest.mark.parametrize("native", [15, 9, 0])
def test_reference_provenance_survives_publication_and_replay(tmp_path, native):
    calls = []
    def train(*args, **kwargs):
        calls.append(kwargs["run_id"])
        return trained(*args, **kwargs)
    subject = runner(tmp_path, trainer=train)
    frames = {}
    for symbol in subject.symbols:
        frame = history(symbol)
        frame["source_id"] = "databento:EQUS.MINI:ohlcv-1m"
        frame["source_dataset"] = "EQUS.MINI"
        # History concatenation can represent integral counts as floats.
        frame["observed_constituents"] = float(native)
        frame["synthetic_constituents"] = float(15 - native)
        frame["synthetic"] = native < 15
        frame["no_trade"] = native == 0
        frames[symbol] = frame
    first = subject.run_cycle(frames, reference_at=REF, mode="live")
    assert first["successful_symbols"] == 2
    for forecast in first["forecasts"].values():
        provenance = reference_input_provenance(forecast)
        assert provenance["status"] == "known", (provenance, forecast.get("reference_provenance"), forecast.get("source_id"))
        assert provenance["observed_constituents"] == native
        assert provenance["synthetic_constituents"] == 15 - native
        assert provenance["reference_at"] == REF.isoformat()
    # Retrying publication retains original input attribution, even if history changes.
    for frame in frames.values():
        frame["observed_constituents"] = 123
    second = subject.run_cycle(frames, reference_at=REF, mode="live")
    assert second["forecasts"] == first["forecasts"]
    assert len(calls) == 2
