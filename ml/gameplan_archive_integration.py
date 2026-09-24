"""Join causal archive features to existing optional features without blending prices."""
from __future__ import annotations

from dataclasses import replace

import pandas as pd


def validate_archive_feature_clocks(frame: pd.DataFrame) -> None:
    """Verify persisted daily feature availability against each action opening."""
    names = ("source_bar_timestamp", "source_bar_end_timestamp", "information_available_at",
             "decision_timestamp", "source_effective_cutoff", "source_feature_cutoff", "source_action_start")
    if frame.empty or not set((*names, "action_date")).issubset(frame):
        raise RuntimeError("Archive feature rows lack causal clocks")
    clocks = {}
    for name in names:
        values = frame[name]
        if not isinstance(values.dtype, pd.DatetimeTZDtype):
            if any(pd.isna(v) or pd.Timestamp(v).tzinfo is None for v in values):
                raise RuntimeError("Archive feature clocks must be timezone aware")
        clocks[name] = pd.to_datetime(values, utc=True, errors="raise")
        if clocks[name].isna().any():
            raise RuntimeError("Archive feature clocks must be present")
    start, end, information, decision, effective, cutoff, action = (clocks[n] for n in names)
    expected_action = pd.Series([pd.Timestamp(day).tz_localize("America/Los_Angeles") + pd.Timedelta(hours=4)
                                 for day in frame.action_date], index=frame.index)
    if (not end.sub(start).eq(pd.Timedelta(days=1)).all()
            or not information.ge(end + pd.Timedelta(minutes=5)).all()
            or not decision.ge(information).all() or not decision.le(effective).all()
            or not effective.le(cutoff).all() or not cutoff.lt(action).all()
            or not action.eq(pd.to_datetime(expected_action, utc=True)).all()):
        raise RuntimeError("Archive feature availability differs from its causal daily contract")


def combine_archive_sources(archive, operational: pd.DataFrame, *, feature_columns):
    """Keep archive clocks authoritative and attach only already available inputs.

    The older rows may have missing optional operational features. The normal
    model admission checks decide which columns have adequate fitting evidence.
    No source's OHLC prices are appended into another dataset's price series.
    """
    sources = archive.sources.copy()
    keys = ["symbol", "action_date"]
    optional = list(dict.fromkeys(feature_columns))
    if set(optional).intersection(archive.feature_columns):
        raise ValueError("Archive and operational feature namespaces overlap")
    left = operational.loc[:, [*keys, "information_available_at", *optional]].copy()
    for frame in (sources, left):
        frame["action_date"] = pd.to_datetime(frame.action_date).dt.date
        if frame.duplicated(keys).any():
            raise ValueError("Historical source identity is not unique")
    left = left.rename(columns={"information_available_at": "operational_information_available_at"})
    sources = sources.merge(left, on=keys, how="left", validate="one_to_one")
    available = pd.to_datetime(sources.operational_information_available_at, utc=True, errors="coerce")
    decision = pd.to_datetime(sources.decision_timestamp, utc=True, errors="raise")
    future = available.notna() & available.gt(decision)
    unknown = available.isna() & sources[optional].notna().any(axis=1)
    sources.loc[future | available.isna(), optional] = float("nan")
    report = {**archive.report,
        "optional_operational_features": optional,
        "operational_rows_attached": int((available.notna() & ~future).sum()),
        "operational_rows_excluded_as_future": int(future.sum()),
        "operational_rows_excluded_unknown_availability": int(unknown.sum()),
        "optional_feature_policy": "missing historical columns remain missing; native training admission unchanged"}
    sources.attrs["source_selection"] = report
    return replace(archive, sources=sources,
                   feature_columns=tuple(dict.fromkeys((*optional, *archive.feature_columns))), report=report)


def exclude_quality_intervals(groups, intervals, *, split_boundaries=()):
    """Exclude outcome windows touching a source-degraded period, retaining evidence."""
    result = {}
    for group, frame in groups.items():
        reject = pd.Series(False, index=frame.index)
        for item in intervals:
            start, end = pd.Timestamp(item["start"]), pd.Timestamp(item["end"])
            if start.tzinfo is None or end.tzinfo is None or start >= end:
                raise ValueError("Archive quality interval is invalid")
            reject |= (frame.symbol.eq(item["symbol"])
                       & pd.to_datetime(frame.target_window_start, utc=True).lt(end)
                       & pd.to_datetime(frame.target_window_end, utc=True).ge(start))
        for item in split_boundaries:
            boundary = pd.Timestamp(item["at"])
            if boundary.tzinfo is None:
                raise ValueError("Archive split boundary must be timezone aware")
            reject |= (frame.symbol.eq(item["symbol"])
                       & pd.to_datetime(frame.target_window_start, utc=True).lt(boundary)
                       & pd.to_datetime(frame.target_window_end, utc=True).ge(boundary))
        clean = frame.loc[~reject].copy()
        clean.attrs = {**frame.attrs, "archive_quality_excluded_rows": int(reject.sum())}
        result[group] = clean
    return result
