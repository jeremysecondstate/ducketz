"""Past-day scoring avoids repeated conversions without changing admissions."""
import threading

import pandas as pd
import pytest

from ml.intraday import minute_feed
from ml.intraday.data import History
from ml.intraday.native_interval import CONTRACT
from ml.intraday.prediction import ContractError, instant
from tests.test_intraday_native_feed import native_candle
from tests.test_intraday_store import candle


REFERENCE = instant("2026-10-08T17:30Z")
NOW = instant("2026-10-09T14:00Z")


def frame_of(value=None):
    frame = pd.DataFrame([value or native_candle("ABCL", REFERENCE)])
    for field in ("opened_at", "available_at"):
        frame[field] = pd.to_datetime(frame[field], utc=True)
    return frame


def outside_day_feed(frame, *, native=True):
    # No constructor, provider, journal, store or acquisition operations needed.
    feed = minute_feed.QuarterHourMinuteFeed.__new__(minute_feed.QuarterHourMinuteFeed)
    feed.symbols = ("ABCL",)
    feed.first = instant("2026-10-09T11:00Z")
    feed.end = instant("2026-10-10T00:00Z")
    feed._lock = threading.RLock()
    feed.source_id, feed.dataset = "databento:EQUS.MINI:ohlcv-1m", "EQUS.MINI"
    feed.clock = lambda: NOW
    feed.native_interval_symbols = ("ABCL",) if native else ()
    feed.native_interval_contract = CONTRACT if native else None
    feed.histories = {"ABCL": History(frame, {"source_dataset": feed.dataset})}
    return feed


@pytest.mark.parametrize("representation", ["utc", "naive", "other_zone", "object"])
def test_exact_observed_history_and_original_timestamp_conversion_paths(monkeypatch, representation):
    frame = frame_of()
    if representation == "naive":
        for field in ("opened_at", "available_at"):
            frame[field] = frame[field].dt.tz_localize(None)
    elif representation == "other_zone":
        for field in ("opened_at", "available_at"):
            frame[field] = frame[field].dt.tz_convert("America/New_York")
    elif representation == "object":
        for field in ("opened_at", "available_at"):
            frame[field] = frame[field].astype(str)
    feed = outside_day_feed(frame)
    converted, original = [], pd.to_datetime
    def conversion(value, *args, **kwargs):
        if isinstance(value, pd.Series) and value.name in {"opened_at", "available_at"} and len(value) == len(frame):
            converted.append(value.name)
        return original(value, *args, **kwargs)
    monkeypatch.setattr(minute_feed.pd, "to_datetime", conversion)
    if representation == "naive":
        # Selection retains its original UTC conversion, while genuine candle
        # qualification still rejects a source row without timezone evidence.
        with pytest.raises(ContractError, match="outside this feed's latched eligible day"):
            feed.history_at("ABCL", REFERENCE)
    else:
        assert feed.history_at("ABCL", REFERENCE) is feed.histories["ABCL"]
    assert converted == ([] if representation == "utc" else ["opened_at", "available_at"])


def test_repeated_absent_target_does_not_reconvert_or_project_empty_history(monkeypatch):
    frame = pd.concat([frame_of()] * 128, ignore_index=True)
    feed = outside_day_feed(frame)
    def no_conversion(*args, **kwargs):
        raise AssertionError("validated UTC history was converted again")
    def no_projection(*args, **kwargs):
        raise AssertionError("empty selection was converted to records")
    monkeypatch.setattr(minute_feed.pd, "to_datetime", no_conversion)
    monkeypatch.setattr(pd.DataFrame, "to_dict", no_projection)
    for _ in range(3):
        with pytest.raises(ContractError, match="outside this feed's latched eligible day"):
            feed.history_at("ABCL", instant("2026-10-08T18:00Z"))


@pytest.mark.parametrize("change", ["source_id", "source_dataset", "availability_kind", "available_at",
                                    "synthetic", "native_interval_contract"])
def test_original_source_availability_and_genuine_observation_guards_remain(change):
    frame = frame_of()
    if change == "available_at":
        frame[change] = NOW + pd.Timedelta(seconds=1)
    elif change == "synthetic":
        frame[change] = True
        frame["evidence_json"] = "{}"
    elif change == "availability_kind":
        frame[change] = "assumed_historical_close"
    else:
        frame[change] = "different"
    feed = outside_day_feed(frame)
    with pytest.raises(ContractError, match="outside this feed's latched eligible day"):
        feed.history_at("ABCL", REFERENCE)


def test_later_genuine_history_replacement_is_immediately_visible():
    real = frame_of()
    feed = outside_day_feed(real.iloc[:0].copy())
    with pytest.raises(ContractError, match="outside this feed's latched eligible day"):
        feed.history_at("ABCL", REFERENCE)
    feed.histories["ABCL"] = History(real, {"source_dataset": "EQUS.MINI"})
    assert feed.history_at("ABCL", REFERENCE) is feed.histories["ABCL"]


def test_original_legacy_interval_remains_eligible():
    original = candle(symbol="ABCL", source_id="databento:EQUS.MINI:ohlcv-1m", source_dataset="EQUS.MINI")
    feed = outside_day_feed(frame_of(original), native=False)
    assert feed.history_at("ABCL", REFERENCE) is feed.histories["ABCL"]


@pytest.mark.parametrize("field", ["opened_at", "available_at"])
def test_malformed_object_timestamp_keeps_original_conversion_error(field):
    frame = frame_of()
    frame[field] = "not-a-time"
    feed = outside_day_feed(frame)
    with pytest.raises(ValueError):
        feed.history_at("ABCL", REFERENCE)


def test_nonmatching_row_with_bad_availability_is_still_rejected():
    frame = frame_of()
    frame["available_at"] = "not-a-time"
    feed = outside_day_feed(frame)
    with pytest.raises(ValueError):
        feed.history_at("ABCL", instant("2026-10-08T18:00Z"))


def test_duplicate_timestamp_columns_keep_original_dataframe_conversion_error():
    frame = frame_of()
    frame = pd.concat([frame, frame[["opened_at"]]], axis=1)
    feed = outside_day_feed(frame)
    with pytest.raises(ValueError, match="duplicate keys"):
        feed.history_at("ABCL", REFERENCE)


def test_original_clock_is_read_once_and_never_changes_frozen_history():
    frame = frame_of()
    before = frame.copy(deep=True)
    feed = outside_day_feed(frame)
    reads = []
    def clock():
        reads.append(1)
        return NOW
    feed.clock = clock
    assert feed.history_at("ABCL", REFERENCE) is feed.histories["ABCL"]
    assert reads == [1]
    pd.testing.assert_frame_equal(frame, before)
