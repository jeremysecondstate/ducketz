from __future__ import annotations

import pandas as pd

from datafetching.bar_timing import annotate_bar_timing


def test_daily_bar_completes_at_official_session_close() -> None:
    row = pd.DataFrame(
        {"timestamp": [pd.Timestamp("2026-08-04T00:00:00Z")]}
    )

    before_close = annotate_bar_timing(
        row,
        timeframe="1d",
        as_of=pd.Timestamp("2026-08-04T19:59:59Z"),
    )
    at_close = annotate_bar_timing(
        row,
        timeframe="1d",
        as_of=pd.Timestamp("2026-08-04T20:00:00Z"),
    )

    assert before_close.loc[0, "bar_end_timestamp"] == pd.Timestamp(
        "2026-08-04T20:00:00Z"
    )
    assert not bool(before_close.loc[0, "bar_complete"])
    assert bool(at_close.loc[0, "bar_complete"])


def test_daily_bar_completion_observes_early_close() -> None:
    row = pd.DataFrame(
        {"timestamp": [pd.Timestamp("2026-11-27T00:00:00Z")]}
    )

    annotated = annotate_bar_timing(
        row,
        timeframe="1d",
        as_of=pd.Timestamp("2026-11-27T18:00:00Z"),
    )

    assert annotated.loc[0, "bar_end_timestamp"] == pd.Timestamp(
        "2026-11-27T18:00:00Z"
    )
    assert bool(annotated.loc[0, "bar_complete"])
