"""Observed-only EWM ancestry, causal warmup, clocks and immutable input checks."""
import json
import hashlib
from types import SimpleNamespace

import numpy as np
import pandas as pd
import pytest

from ml.gameplan_archive_features import ArchiveFeatureSources
from ml.gameplan_observed_technicals import (
    AVAILABILITY_BASIS, OBSERVED_TECHNICAL_CONTRACT, build_observed_technicals,
    rebuild_observed_technicals, validate_observed_technical_sources,
)
from technicals.calculations import market_regime, session_aware_breakout

FEATURES = ("mr__trend_atr", "mr__technical_score", "mr__technical_score_change_5",
            "bp__compression_score", "bp__bars_since_state_change", "bp__volume_participation_score",
            "bar__intrabar_range_atr", "bar__close_location", "life__timing_score")
HASH = "a" * 64


def bars(count=160):
    timestamps = pd.DatetimeIndex([stamp + pd.Timedelta(hours=hour)
        for stamp in pd.bdate_range("2026-05-04", periods=30, tz="UTC") for hour in range(8, 24)])[:count]
    n = np.arange(count, dtype=float)
    close = 20 + n / 20 + np.sin(n / 5)
    return pd.DataFrame({"symbol": "AAA", "timestamp": timestamps,
        "open": close - .1, "high": close + .4, "low": close - .5, "close": close,
        "volume": 100 + n % 13, "source_coverage_start": timestamps.min().floor("D"),
        "source_coverage_end": timestamps.max().ceil("D") + pd.Timedelta(days=1),
        "source_kind": "verified-native-ohlcv-1h", "source_raw_input_hashes_json": json.dumps([HASH])})


def archive(frame, *, end=None):
    end = pd.Timestamp(end) if end is not None else frame.timestamp.iloc[-1]
    day = end.tz_convert("America/New_York").date()
    decision = end + pd.Timedelta(hours=1, minutes=5)
    sources = pd.DataFrame([{ "symbol": "AAA", "action_date": str(day + pd.Timedelta(days=1)),
        "source_session": day, "decision_timestamp": decision, "source_feature_cutoff": decision,
        "source_regular_close": min(pd.Timestamp(day, tz="UTC") + pd.Timedelta(hours=20), end + pd.Timedelta(hours=1)),
        "quote__unrelated": 17.0, **{name: 98765.0 for name in FEATURES}}])
    return ArchiveFeatureSources(sources, FEATURES, (), {"contract_version": "archive-fixture"})


def build(frame, value=None, **kwargs):
    value = archive(frame) if value is None else value
    coverage = kwargs.pop("coverage", {"AAA": [(frame.source_coverage_start.min(), frame.source_coverage_end.max())]})
    return build_observed_technicals(value, frame, feature_columns=FEATURES,
        available_at=value.sources.decision_timestamp.max(), coverage=coverage, **kwargs)


def test_original_calculations_and_raw_sample_count_semantics_are_preserved():
    frame = bars()
    old = archive(frame)
    original = old.sources.copy(deep=True)
    actual = build(frame, old)
    mr = market_regime.calculate_market_regime(frame, symbol="AAA", provider="databento", timeframe="1h")
    bp = session_aware_breakout.calculate_breakout_pressure(frame, symbol="AAA", provider="databento", timeframe="1h")
    last = actual.sources.iloc[0]
    assert last["mr__trend_atr"] == pytest.approx(mr.iloc[-1].trend_atr)
    assert last["bp__bars_since_state_change"] == bp.iloc[-1].bars_since_state_change
    assert last["bp__volume_participation_score"] == pytest.approx(bp.iloc[-1].volume_participation_score)
    assert pd.isna(last["life__timing_score"])
    assert last["quote__unrelated"] == 17
    pd.testing.assert_frame_equal(old.sources, original)
    assert actual.report["observed_technicals"]["parameters"]["mr"] == market_regime.FULL_WINDOWS
    assert last.observed_technical_availability_basis == AVAILABILITY_BASIS
    assert json.loads(last.observed_technical_raw_input_hashes_json) == [HASH]


@pytest.mark.parametrize("marker", [None, "synthetic", "source_empty", "is_no_trade"])
def test_excluded_spike_cannot_change_any_ewm_descendant(marker):
    clean = bars().drop(index=80).reset_index(drop=True)
    dirty = bars()
    dirty.loc[80, ["open", "high", "low", "close"]] = 1e8
    if marker is None:
        dirty.loc[80, "volume"] = 0
    else:
        dirty[marker] = False
        dirty.loc[80, marker] = True
    expected = build(clean)
    actual = build(dirty)
    pd.testing.assert_frame_equal(actual.sources, expected.sources)
    assert actual.report["observed_technicals"]["rejected_nonpositive_or_invalid_hours"] == 1


def test_nonboolean_native_marker_is_rejected_before_recursive_calculation():
    frame = bars()
    frame["synthetic"] = "false"
    actual = build(frame)
    assert actual.sources[list(FEATURES)].isna().all(axis=None)
    assert actual.report["observed_technicals"]["rejected_nonpositive_or_invalid_hours"] == len(frame)


@pytest.mark.parametrize("column,value", [("volume", -1), ("volume", np.nan),
    ("high", np.inf), ("high", 0), ("low", 1e8), ("close", -3)])
def test_invalid_ohlcv_cannot_enter_recursive_history(column, value):
    frame = bars()
    clean = frame.drop(index=80).reset_index(drop=True)
    frame.loc[80, column] = value
    pd.testing.assert_frame_equal(build(frame).sources, build(clean).sources)


def test_append_future_observations_cannot_revise_a_past_vector_or_hashes():
    whole = bars(240)
    past = whole.iloc[:128].copy()
    value = archive(past)
    whole.loc[128:, "source_raw_input_hashes_json"] = json.dumps(["b" * 64])
    whole.loc[128:, ["open", "high", "low", "close"]] *= 1000
    expected = build(past, value)
    actual = build(whole, value)
    pd.testing.assert_frame_equal(actual.sources, expected.sources)
    assert json.loads(actual.sources.iloc[0].observed_technical_raw_input_hashes_json) == [HASH]


def test_full_history_future_length_does_not_enable_premature_warmup():
    whole = bars(128)
    past = whole.iloc[:59].copy()
    value = archive(past)
    before = build(past, value)
    after = build(whole, value)
    assert before.sources[list(FEATURES)].isna().all(axis=None)
    pd.testing.assert_frame_equal(before.sources, after.sources)


@pytest.mark.parametrize("reset_kind", ["split", "quality", "coverage"])
def test_reset_breaks_old_recursive_ancestry_and_restarts_sixty_observation_gate(reset_kind):
    frame = bars(240)
    split = frame.timestamp.iloc[160]
    kwargs = {}
    if reset_kind == "split":
        kwargs["split_boundaries"] = [{"symbol": "AAA", "at": split.isoformat()}]
    elif reset_kind == "quality":
        kwargs["excluded_intervals"] = [{"symbol": "AAA", "start": (split-pd.Timedelta(hours=1)).isoformat(), "end": split.isoformat()}]
    else:
        # A one-hour unverified acquisition gap occurs between Friday and Monday.
        left = frame.timestamp.iloc[159] + pd.Timedelta(hours=1)
        kwargs["coverage"] = {"AAA": [(frame.source_coverage_start.min(), left), (split, frame.source_coverage_end.max())]}
    expected = build(frame.iloc[160:].reset_index(drop=True))
    actual = build(frame, **kwargs)
    pd.testing.assert_frame_equal(actual.sources, expected.sources)
    assert actual.report["observed_technicals"]["resets"]
    early = archive(frame.iloc[:192])
    assert build(frame, early, **kwargs).sources[list(FEATURES)].isna().all(axis=None)


def test_no_stale_prior_session_or_future_hour_fallback():
    frame = bars(160)
    value = archive(frame)
    value.sources["source_session"] = pd.Timestamp("2026-05-18").date()
    assert build(frame, value).sources[list(FEATURES)].isna().all(axis=None)
    value = archive(frame)
    value.sources["source_regular_close"] = frame.timestamp.iloc[-1] + pd.Timedelta(hours=2)
    assert build(frame, value).sources[list(FEATURES)].isna().all(axis=None)


@pytest.mark.parametrize("change", ["hash", "future", "lifecycle", "warmup", "contract", "session", "infinite", "fractional"])
def test_saved_feature_validation_rejects_provenance_or_clock_tampering(change):
    result = build(bars()).sources
    if change == "hash": result.loc[0, "observed_technical_raw_input_hashes_json"] = "[]"
    elif change == "future": result.loc[0, "observed_technical_available_at"] += pd.Timedelta(nanoseconds=1)
    elif change == "lifecycle": result.loc[0, "life__timing_score"] = 50
    elif change == "warmup": result.loc[0, "observed_technical_history_rows"] = 59
    elif change == "contract": result.loc[0, "observed_technical_contract"] = "unknown"
    elif change == "infinite": result.loc[0, "mr__trend_atr"] = np.inf
    elif change == "fractional":
        result["observed_technical_history_rows"] = result.observed_technical_history_rows.astype(float)
        result.loc[0, "observed_technical_history_rows"] = 65.5
    else: result.loc[0, "source_session"] = pd.Timestamp("2026-05-18").date()
    with pytest.raises(ValueError):
        validate_observed_technical_sources(result, feature_columns=FEATURES)


def test_loader_preserves_actual_evidence_cutoff_and_durable_source_files(tmp_path, monkeypatch):
    frame = bars()
    value = archive(frame)
    cutoff = value.sources.decision_timestamp.max()
    received = cutoff + pd.Timedelta(days=10)
    calls = []
    coverage = {"AAA": [(frame.source_coverage_start.min(), frame.source_coverage_end.max())]}
    def load(root, symbols, schema, **kwargs):
        calls.append((symbols, schema, kwargs))
        return frame, (tmp_path / "native.parquet",), coverage, []
    monkeypatch.setattr("ml.gameplan_observed_technicals._load_schema", load)
    monkeypatch.setattr("ml.gameplan_observed_technicals._load_tail_hours",
        lambda *args, **kwargs: (frame.iloc[:0], (tmp_path / "tail.dbn",), {"AAA": []}, [], 3))
    result = rebuild_observed_technicals(tmp_path, value, symbols=["AAA"], feature_columns=FEATURES,
                                         available_at=cutoff, evidence_available_at=received)
    assert calls[0][2]["evidence_available_at"] == received
    assert result.report["observed_technicals"]["evidence_available_at"] == received.isoformat()
    assert result.report["observed_technicals"]["rejected_native_minutes_before_aggregation"] == 3
    assert result.source_files == (tmp_path / "native.parquet", tmp_path / "tail.dbn")
    assert result.sources.iloc[0].observed_technical_contract == OBSERVED_TECHNICAL_CONTRACT


def minute_partition(tmp_path, *, published="2026-05-05T00:00:00Z"):
    folder = tmp_path / "market-data/databento/us-equities/XNAS.ITCH/ohlcv-1m/AAA/windows/window"
    folder.mkdir(parents=True)
    raw = folder / "raw.dbn"
    raw.write_bytes(b"offline raw fixture")
    normalized = folder / "bars.parquet"
    frame = pd.DataFrame({"timestamp": pd.date_range("2026-05-04T20:00Z", periods=4, freq="min"),
        "open": [10., 1e8, 12., 1e8], "high": [11., 1e8, 13., 1e8],
        "low": [9., 1e8, 11., 1e8], "close": [10.5, 1e8, 12.5, 1e8],
        "volume": [100, 0, 80, 90], "source_empty": [False, False, False, True]})
    frame.to_parquet(normalized)
    digest = lambda path: hashlib.sha256(path.read_bytes()).hexdigest()
    part = SimpleNamespace(directory=folder, normalized_path=normalized, manifest_path=folder/"manifest.json",
        source_files=(normalized,),
        request={"dataset": "XNAS.ITCH", "schema": "ohlcv-1m", "stype_in": "raw_symbol",
                 "symbol_scope": ["AAA"], "start": "2026-05-04", "end": "2026-05-05"},
        manifest={"published_at": published, "normalized": {"timestamp_column": "timestamp"},
                  "raw": {"path": raw.name, "size_bytes": raw.stat().st_size, "checksum_sha256": digest(raw)}},
        receipt={"raw_checksum_sha256": digest(raw), "normalized_checksum_sha256": digest(normalized)})
    return part, raw


def test_minute_tail_filters_before_aggregation_and_keeps_native_hashes(tmp_path, monkeypatch):
    from ml.gameplan_observed_technicals import _load_tail_hours
    part, raw = minute_partition(tmp_path)
    monkeypatch.setattr("datafetching.databento_archive.discover_archive_partitions", lambda *a, **k: [part])
    hours, files, coverage, _, rejected = _load_tail_hours(tmp_path, ("AAA",),
        {"AAA": [(pd.Timestamp("2020-01-01T00:00Z"), pd.Timestamp("2026-05-04T20:00Z"))]},
        available_at="2026-05-05T00:05Z", evidence_available_at="2026-05-06T00:00Z")
    assert len(hours) == 1
    assert hours.iloc[0].open == 10 and hours.iloc[0].close == 12.5
    assert hours.iloc[0].high == 13 and hours.iloc[0].volume == 180
    assert hours.iloc[0].observed_constituent_count == 2
    assert rejected == 2 and raw in files
    assert json.loads(hours.iloc[0].source_raw_input_hashes_json) == [hashlib.sha256(raw.read_bytes()).hexdigest()]
    assert coverage["AAA"][0][0] == pd.Timestamp("2026-05-04T20:00Z")


@pytest.mark.parametrize("kind", ["raw_hash", "normalized_hash", "source_identity"])
def test_minute_tail_rejects_unverified_payloads(tmp_path, monkeypatch, kind):
    from ml.gameplan_observed_technicals import _load_tail_hours
    part, raw = minute_partition(tmp_path)
    if kind == "raw_hash": raw.write_bytes(b"changed raw bytes")
    elif kind == "normalized_hash": part.receipt["normalized_checksum_sha256"] = "0" * 64
    else: part.request["dataset"] = "OTHER"
    monkeypatch.setattr("datafetching.databento_archive.discover_archive_partitions", lambda *a, **k: [part])
    with pytest.raises((ValueError, RuntimeError)):
        _load_tail_hours(tmp_path, ("AAA",), {"AAA": []},
            available_at="2026-05-05T00:05Z", evidence_available_at="2026-05-06T00:00Z")


def test_minute_tail_does_not_read_archives_received_after_actual_evidence_cutoff(tmp_path, monkeypatch):
    from ml.gameplan_observed_technicals import _load_tail_hours
    part, _ = minute_partition(tmp_path, published="2026-05-07T00:00Z")
    monkeypatch.setattr("datafetching.databento_archive.discover_archive_partitions", lambda *a, **k: [part])
    monkeypatch.setattr(pd, "read_parquet", lambda *a, **k: pytest.fail("future receipt was read"))
    hours, files, _, _, _ = _load_tail_hours(tmp_path, ("AAA",), {"AAA": []},
        available_at="2026-05-05T00:05Z", evidence_available_at="2026-05-06T00:00Z")
    assert hours.empty and not files
