"""One native row projection preserves exact typed groups for caller validation."""
import numpy as np
import pandas as pd
import pytest

from ml.intraday.source_aggregation import source_minute_groups

OPEN = pd.Timestamp("2026-10-26T08:00:00Z")


def minutes(count=45):
    opened = pd.date_range(OPEN, periods=count, freq="1min")
    return pd.DataFrame(dict(symbol="AAPL", opened_at=opened,
        available_at=opened + pd.Timedelta(minutes=1), availability_kind="actual",
        open=100.0, high=110.0, low=90.0, close=101.0, volume=1.0,
        source_id="native:one", source_dataset="EQUS.MINI", session_id="unassigned",
        synthetic=False, no_trade=False))


def test_whole_history_uses_one_typed_native_row_projection(monkeypatch):
    frame = minutes(1500)
    calls = []
    original = pd.DataFrame.itertuples
    def counted(self, *args, **kwargs):
        calls.append(len(self))
        return original(self, *args, **kwargs)
    monkeypatch.setattr(pd.DataFrame, "itertuples", counted)
    groups = list(source_minute_groups(frame))
    assert len(groups) == 100
    assert calls == [1500]
    assert [begin for _, _, begin, _ in groups] == list(range(0, 1500, 15))
    assert sum(len(rows) for _, rows, _, _ in groups) == len(frame)


@pytest.mark.parametrize("dtype,values", [
    ("float32", [1e8, 1, 1]), ("float64", [1e16, 1, 1]),
    ("int64", [2**62, 2**62, 1]), ("uint64", [2**63, 2**63, 1]),
    ("Float32", [1e8, 1, 1]), ("Float64", [1e16, 1, 1]),
    ("Int64", [2**62, 2**62, 1]), ("UInt64", [2**63, 2**63, 1]),
])
def test_bounds_retain_original_numeric_dtype_reduction(dtype, values):
    frame = minutes(3)
    frame["volume"] = pd.Series(values, dtype=dtype)
    original = frame.copy(deep=True)
    slot, rows, begin, end = next(source_minute_groups(frame))
    assert slot == OPEN and (begin, end) == (0, 3)
    assert float(frame.volume.iloc[begin:end].sum()).hex() == float(frame.volume.sum()).hex()
    assert [float(row.volume) for row in rows] == [float(v) for v in frame.volume]
    pd.testing.assert_frame_equal(frame, original)


def test_sparse_rows_match_original_groupby_at_noncontiguous_external_indices():
    frame = minutes().drop(index=[0, 1, 14, 15, 29, 44]).copy()
    frame.loc[10, "available_at"] = OPEN + pd.Timedelta(hours=2)
    frame.loc[10, "availability_kind"] = "assumed_historical_close"
    expected = list(frame.groupby(frame.opened_at.dt.floor("15min"), sort=True))
    groups = list(source_minute_groups(frame))
    fields = ["symbol", "opened_at", "available_at", "availability_kind",
              "open", "high", "low", "close", "volume", "source_id", "source_dataset"]
    assert len(groups) == len(expected) == 3
    for (slot, rows, begin, end), (old_slot, old_group) in zip(groups, expected):
        assert slot == old_slot
        assert [tuple(row) for row in rows] == list(old_group[fields].itertuples(index=False, name=None))
        pd.testing.assert_frame_equal(frame.iloc[begin:end], old_group)


def test_source_and_clock_scalar_types_are_not_normalized_by_projection():
    frame = minutes(30)
    frame["opened_at"] = frame.opened_at.dt.tz_convert("America/Los_Angeles")
    frame["available_at"] = frame.available_at.dt.tz_convert("America/Los_Angeles")
    frame["source_id"] = np.int64(42)
    groups = list(source_minute_groups(frame))
    assert groups[0][0] == OPEN
    assert groups[0][1][0].source_id == 42
    assert not isinstance(groups[0][1][0].source_id, str)
    assert str(groups[0][1][0].opened_at.tz) == "America/Los_Angeles"


def test_typed_empty_frame_has_no_groups_or_mutation():
    frame = minutes().iloc[:0]
    original = frame.copy(deep=True)
    assert list(source_minute_groups(frame)) == []
    pd.testing.assert_frame_equal(frame, original)
