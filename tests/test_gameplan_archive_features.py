from datetime import date
from types import SimpleNamespace

import exchange_calendars as xcals
import numpy as np
import pandas as pd
import pytest

from ml import gameplan_archive_features as archive
from ml.artifacts import file_checksum
from ml.gameplan_source_selection import SOURCE_SELECTION_COLUMNS, source_selection_contract


def _daily(*, count=70, start="2019-08-19", symbol="AAPL"):
    calendar = xcals.get_calendar("XNYS", start=start, end="2027-01-01")
    dates = calendar.sessions[:count]
    clocks = pd.to_datetime(dates, utc=True)
    prices = 100 + np.arange(len(clocks), dtype=float) / 10
    return pd.DataFrame({
        "symbol": symbol, "timestamp": clocks, "open": prices,
        "high": prices + 2, "low": prices - 1, "close": prices + 1,
        "volume": np.arange(len(clocks)) + 1000,
        "source_coverage_start": clocks.min(),
        "source_coverage_end": clocks.max() + pd.Timedelta(days=1),
        "source_kind": "verified-native-ohlcv-1d",
    })


def _hourly(daily):
    rows = []
    for day in daily.itertuples():
        for hour in range(13, 24):
            rows.append({"symbol": day.symbol, "timestamp": day.timestamp + pd.Timedelta(hours=hour),
                         "open": day.open, "high": day.high, "low": day.low, "close": day.close,
                         "volume": 20 + hour, "source_coverage_start": day.timestamp,
                         "source_coverage_end": day.timestamp + pd.Timedelta(days=1),
                         "source_kind": "verified-native-ohlcv-1h"})
    return pd.DataFrame(rows)


def _build(daily, hourly=None, **kwargs):
    return archive.build_archive_feature_sources(
        daily, archive._empty() if hourly is None else hourly, symbols=tuple(daily.symbol.unique()),
        available_at=kwargs.pop("available_at", pd.Timestamp("2026-09-24T04:00Z")), **kwargs)


def test_deep_daily_features_require_real_warmup_and_keep_daily_clocks():
    daily = _daily()
    before = daily.copy(deep=True)
    result = _build(daily)
    assert len(result.sources) == 50
    first = result.sources.iloc[0]
    assert first.source_session == daily.iloc[20].timestamp.date()
    assert first.arch__daily_return_20 == pytest.approx(daily.iloc[20].close / daily.iloc[0].close - 1)
    assert first.source_feature_timeframe == "1d"
    assert first.source_bar_end_timestamp - first.source_bar_timestamp == pd.Timedelta(days=1)
    assert first.information_available_at == first.source_bar_end_timestamp + pd.Timedelta(minutes=5)
    assert first.information_available_at < first.source_action_start
    assert set(SOURCE_SELECTION_COLUMNS).issubset(result.sources)
    assert source_selection_contract(result.sources) == archive.ARCHIVE_FEATURE_CONTRACT
    assert result.sources[list(archive.HOURLY_FEATURE_COLUMNS)].isna().all().all()
    assert result.report["synthetic_feature_bars"] == 0
    assert result.report["label_policy_changed"] is False
    pd.testing.assert_frame_equal(daily, before)


def test_future_prices_splits_and_quality_do_not_rewrite_earlier_features():
    daily = _daily()
    prefix = daily.iloc[:35]
    earlier = _build(prefix, _hourly(prefix))
    future = daily.copy()
    future.loc[35:, ["open", "high", "low", "close"]] *= 5
    warning_start = future.iloc[50].timestamp
    later = _build(future, _hourly(future), split_events={"AAPL": [{"ex_date": future.iloc[35].timestamp}]},
                   excluded_intervals=[{"symbol": "AAPL", "start": warning_start.isoformat(),
                                        "end": (warning_start + pd.Timedelta(days=1)).isoformat(),
                                        "reason": "PROVIDER_QUALITY_DEGRADED"}])
    filtered = later.sources.loc[later.sources.source_session.le(prefix.iloc[-1].timestamp.date())]
    pd.testing.assert_frame_equal(earlier.sources, filtered.reset_index(drop=True))


def test_missing_exchange_session_resets_the_whole_rolling_core():
    daily = _daily()
    missing = daily.iloc[30].timestamp.date()
    result = _build(daily.drop(index=30))
    days = set(result.sources.source_session)
    assert missing not in days
    assert daily.iloc[50].timestamp.date() not in days
    assert daily.iloc[51].timestamp.date() in days
    assert any("MISSING_EXCHANGE_SESSION" in row["reasons"] for row in result.report["quality_resets"])


def test_quality_interval_excludes_affected_rolling_windows_and_is_preserved_for_labels():
    daily = _daily()
    stamp = daily.iloc[30].timestamp + pd.Timedelta(hours=15, minutes=5)
    bad = {"symbol": "AAPL", "start": stamp.isoformat(),
           "end": (stamp + pd.Timedelta(minutes=1)).isoformat(), "reason": "UNDEFINED_OBSERVED_OHLC"}
    result = _build(daily, _hourly(daily), excluded_intervals=[bad])
    days = set(result.sources.source_session)
    assert all(daily.iloc[i].timestamp.date() not in days for i in range(30, 51))
    assert daily.iloc[51].timestamp.date() in days
    assert result.report["excluded_intervals"] == [bad]
    assert result.report["excluded_undefined_observations"] == 1


def test_known_small_split_resets_without_retroactive_price_adjustment():
    daily = _daily()
    before = _build(daily.iloc[:30]).sources
    daily.loc[30:, ["open", "high", "low", "close"]] /= 2
    exdate = daily.iloc[30].timestamp.date()
    result = _build(daily, split_events={"AAPL": [{"ex_date": exdate}]})
    pd.testing.assert_frame_equal(before, result.sources.loc[result.sources.source_session.lt(exdate)].reset_index(drop=True))
    assert daily.iloc[49].timestamp.date() not in set(result.sources.source_session)
    assert daily.iloc[50].timestamp.date() in set(result.sources.source_session)
    assert result.report["split_boundaries"] == [{"symbol": "AAPL", "at": archive._local(exdate, 0).tz_convert("UTC").isoformat()}]
    assert str(exdate) in result.report["by_symbol"]["AAPL"]["known_split_ex_dates"]


def test_unexplained_large_discontinuity_quarantines_warmup():
    daily = _daily()
    daily.loc[30:, ["open", "high", "low", "close"]] *= 4
    result = _build(daily)
    assert any("RAW_PRICE_DISCONTINUITY_RESET" in row["reasons"] for row in result.report["quality_resets"])
    assert daily.iloc[49].timestamp.date() not in set(result.sources.source_session)
    assert "not complete split detection" in result.report["split_policy"]
    assert result.report["target_discontinuity_boundaries"] == [{
        "symbol": "AAPL", "at": archive._local(daily.iloc[30].timestamp.date(), 0).tz_convert("UTC").isoformat(),
        "reason": "RAW_PRICE_DISCONTINUITY_RESET"}]


def test_asof_includes_only_completed_daily_interval_and_processing_delay():
    daily = _daily(count=25)
    available = daily.iloc[-1].timestamp + pd.Timedelta(days=1, minutes=5)
    before = _build(daily, available_at=available - pd.Timedelta(nanoseconds=1))
    exact = _build(daily, available_at=available)
    assert len(exact.sources) == len(before.sources) + 1
    assert exact.sources.iloc[-1].information_available_at == available
    assert before.sources.iloc[-1].source_session < daily.iloc[-1].timestamp.date()


@pytest.mark.parametrize("last_day,action,action_start", [
    ("2026-03-06", date(2026, 3, 9), "2026-03-09T11:00Z"),
    ("2026-10-30", date(2026, 11, 2), "2026-11-02T12:00Z"),
    ("2026-09-04", date(2026, 9, 8), "2026-09-08T11:00Z"),
])
def test_exchange_sessions_and_pacific_clocks_handle_dst_and_holiday(last_day, action, action_start):
    daily = _daily(count=230, start="2026-01-02")
    daily = daily.loc[daily.timestamp.le(pd.Timestamp(last_day, tz="UTC"))]
    result = _build(daily, available_at=pd.Timestamp("2027-01-01T00:00Z")).sources.iloc[-1]
    assert result.action_date == action
    assert result.source_action_start == pd.Timestamp(action_start)


def test_hourly_context_uses_only_observed_causal_hours_and_remains_optional():
    daily = _daily()
    hours = _hourly(daily)
    result = _build(daily, hours)
    row = result.sources.iloc[0]
    stamp = daily.iloc[20].timestamp + pd.Timedelta(hours=23)
    actual = hours.loc[hours.timestamp.le(stamp)]
    assert row.archive_hourly_observed_at == stamp
    assert row.arch__hourly_volume_ratio_20_observed == pytest.approx(actual.iloc[-1].volume / actual.iloc[-21:-1].volume.mean())
    stale = _build(daily, hours.loc[hours.timestamp.dt.hour.lt(19)])
    assert stale.sources[list(archive.HOURLY_FEATURE_COLUMNS)].isna().all().all()


def test_all_symbols_have_identical_core_feature_contract():
    daily = pd.concat([_daily(symbol="AAPL"), _daily(symbol="IONQ", start="2021-01-04")], ignore_index=True)
    result = _build(daily)
    assert result.report["selected_rows_by_symbol"] == {"AAPL": 50, "IONQ": 50}
    assert result.sources[list(archive.CORE_FEATURE_COLUMNS)].notna().all().all()


@pytest.mark.parametrize("mutation,error", [
    (lambda frame: frame.assign(timestamp=frame.timestamp.dt.tz_localize(None)), "timezone aware"),
    (lambda frame: frame.assign(high=frame.low - 1), "inconsistent"),
    (lambda frame: frame.assign(volume=-1), "inconsistent"),
    (lambda frame: frame.assign(close=np.inf), "finite observations"),
    (lambda frame: frame.assign(source_coverage_end=frame.timestamp), "complete declared"),
    (lambda frame: frame.assign(source_kind="canonical-EQUS.MINI"), "source kind"),
])
def test_invalid_observations_or_unproven_intervals_fail_closed(mutation, error):
    with pytest.raises(ValueError, match=error):
        _build(mutation(_daily()))


def test_conflicting_duplicates_rejected_but_identical_overlap_does_not_add_examples():
    daily = _daily()
    expected = _build(daily)
    duplicate = _build(pd.concat([daily, daily.iloc[:5]], ignore_index=True))
    pd.testing.assert_frame_equal(expected.sources, duplicate.sources)
    conflicting = daily.iloc[[0]].copy()
    conflicting["close"] += 0.5
    with pytest.raises(ValueError, match="Conflicting"):
        _build(pd.concat([daily, conflicting], ignore_index=True))


def test_minute_aggregation_requires_explicit_complete_coverage_not_min_max():
    day = pd.Timestamp("2026-09-22T00:00Z")
    minutes = pd.DataFrame({"symbol": ["AAPL"] * 2, "timestamp": [day, day + pd.Timedelta(hours=23, minutes=59)],
                            "open": [100, 101], "high": [101, 102], "low": [99, 100],
                            "close": [100.5, 101.5], "volume": [5, 7]})
    partial = {"AAPL": [(day, day + pd.Timedelta(minutes=1)),
                         (day + pd.Timedelta(hours=23, minutes=59), day + pd.Timedelta(days=1))]}
    assert archive._aggregate_minutes(minutes, frequency="1d", coverage=partial,
                                      as_of=day + pd.Timedelta(days=2)).empty
    complete = {"AAPL": [(day, day + pd.Timedelta(days=1))]}
    derived = archive._aggregate_minutes(minutes, frequency="1d", coverage=complete,
                                         as_of=day + pd.Timedelta(days=2)).iloc[0]
    assert derived.volume == 12
    assert derived.open == 100 and derived.close == 101.5
    assert derived.observed_constituent_count == 2
    assert derived.first_actual_observation == minutes.iloc[0].timestamp
    assert derived.last_actual_observation == minutes.iloc[-1].timestamp
    assert derived.source_kind == "verified-native-1m-aggregate"


def test_native_daily_observation_takes_precedence_without_blending():
    daily = _daily(count=2)
    derived = daily.copy().assign(close=daily.close + 0.1)
    selected = archive._prefer_native(daily, derived)
    pd.testing.assert_frame_equal(selected.reset_index(drop=True), daily)


def _partition(tmp_path, *, schema="ohlcv-1d", undefined=False):
    directory = tmp_path / "market-data" / "databento" / "us-equities" / "XNAS.ITCH" / "AAPL" / schema
    directory.mkdir(parents=True)
    raw = directory / "provider.dbn.zst"
    raw.write_bytes(b"native evidence")
    normal = directory / "normalized.parquet"
    frame = _daily(count=25)
    if undefined:
        frame.loc[2, ["open", "high", "low", "close"]] = np.nan
    frame[["symbol", "timestamp", "open", "high", "low", "close", "volume"]].rename(columns={"timestamp": "ts_event"}).to_parquet(normal)
    request = {"dataset": "XNAS.ITCH", "schema": schema, "stype_in": "raw_symbol", "symbol_scope": ["AAPL"],
               "start": str(frame.timestamp.min().date()), "end": str((frame.timestamp.max() + pd.Timedelta(days=1)).date())}
    manifest = {"request": request, "published_at": "2026-09-23T00:00:00Z", "provider_warnings": [],
                "raw": {"path": raw.name, "size_bytes": raw.stat().st_size, "checksum_sha256": file_checksum(raw)},
                "normalized": {"timestamp_column": "ts_event"}}
    return SimpleNamespace(directory=directory, normalized_path=normal, manifest_path=directory / "manifest.json",
                           request=request, manifest=manifest, receipt={"raw_checksum_sha256": file_checksum(raw)},
                           source_files=(normal, directory / "manifest.json", directory / "receipt.json"))


def test_loader_reverifies_native_raw_evidence_and_reports_undefined_observation(tmp_path, monkeypatch):
    part = _partition(tmp_path, undefined=True)
    monkeypatch.setattr(archive, "discover_archive_partitions", lambda *args, **kwargs: (part,))
    frame, files, coverage, excluded = archive._load_schema(tmp_path, ["AAPL"], "ohlcv-1d",
                                                         as_of=pd.Timestamp("2026-09-24T00:00Z"))
    assert len(frame) == 24
    assert excluded[0]["reason"] == "UNDEFINED_OBSERVED_OHLC"
    assert excluded[0]["schema"] == "ohlcv-1d"
    assert pd.Timestamp(excluded[0]["end"]) - pd.Timestamp(excluded[0]["start"]) == pd.Timedelta(days=1)
    assert part.directory / "provider.dbn.zst" in files
    (part.directory / "provider.dbn.zst").write_bytes(b"changed evidence")
    with pytest.raises(ValueError, match="raw evidence verification"):
        archive._load_schema(tmp_path, ["AAPL"], "ohlcv-1d", as_of=pd.Timestamp("2026-09-24T00:00Z"))


def test_loader_rejects_wrong_dataset_and_unclassified_warning(tmp_path, monkeypatch):
    part = _partition(tmp_path)
    monkeypatch.setattr(archive, "discover_archive_partitions", lambda *args, **kwargs: (part,))
    part.request["dataset"] = "EQUS.MINI"
    with pytest.raises(ValueError, match="identity mismatch"):
        archive._load_schema(tmp_path, ["AAPL"], "ohlcv-1d", as_of=pd.Timestamp("2026-09-24T00:00Z"))
    part.request["dataset"] = "XNAS.ITCH"
    part.manifest["provider_warnings"] = [{"message": "Unknown degradation without dated coverage"}]
    with pytest.raises(ValueError, match="Unclassified"):
        archive._load_schema(tmp_path, ["AAPL"], "ohlcv-1d", as_of=pd.Timestamp("2026-09-24T00:00Z"))


def test_provider_quality_days_keep_exact_evidence(tmp_path):
    part = _partition(tmp_path)
    part.manifest["provider_warnings"] = [{"message": "Reduced quality: 2021-07-07 (degraded), 2022-09-19 (degraded)."}]
    intervals = archive._quality_intervals(part)
    assert [row["start"] for row in intervals] == ["2021-07-07T00:00:00+00:00", "2022-09-19T00:00:00+00:00"]
    assert all(row["manifest_path"] == str(part.manifest_path) for row in intervals)


def test_split_provenance_binds_empty_native_reports_and_prefers_modern_folder(tmp_path):
    modern = tmp_path / "stocks/AAPL/corporate/stock_splits/fmp/normalized"
    legacy = tmp_path / "normalized/fmp/corporate"
    modern.mkdir(parents=True)
    legacy.mkdir(parents=True)
    current = modern / "empty.parquet"
    old = legacy / "AAPL_stock_splits_old.parquet"
    pd.DataFrame().to_parquet(current)
    pd.DataFrame().to_parquet(old)
    assert archive._split_source_files(tmp_path, "AAPL") == (current,)
