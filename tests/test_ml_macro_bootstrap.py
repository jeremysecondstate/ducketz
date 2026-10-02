from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pandas as pd
import pytest

import ml.macro_bootstrap as bootstrap
import ml.rolling_materialization as rolling
from datafetching.fred_alfred_readiness import (
    FredAlfredReadinessError, derive_fred_alfred_backfill_plan,
    read_verified_macro_evidence, verify_and_publish_fred_alfred_readiness,
)
from datafetching.fred_vintage_import import FRED_ALFRED_SUPPORTED_SERIES, FredAlfredClient, import_fred_alfred_vintages
from datafetching.loop_a_cycle import begin_loop_a_cycle, finish_loop_a_cycle
from ml.artifacts import file_checksum
from ml.feature_registry import DEFAULT_FEATURE_REGISTRY
from test_ml_runtime_pipeline import _write_synthetic_loop_a_outputs
from test_fred_alfred_causal_pipeline import _CompleteFredSession, _TEST_KEY


@pytest.fixture
def sealed(tmp_path):
    fixture = _write_synthetic_loop_a_outputs(tmp_path)
    from datafetching.bar_schema import write_normalized_bar_parquet
    cutoff_day = pd.Timestamp("2024-03-21T00:00:00Z")
    write_normalized_bar_parquet(fixture["bars"].loc[fixture["bars"].timestamp.le(cutoff_day)], fixture["bars_path"])
    for family in ("mr", "bp"):
        calculation = DEFAULT_FEATURE_REGISTRY.calculation(family)
        path = tmp_path / "stocks/GOOG/technicals" / calculation.calculation_name / "databento/1d.parquet"
        frame = pd.read_parquet(path)
        frame = frame.loc[frame.timestamp.le(cutoff_day)].copy()
        for feature in (*DEFAULT_FEATURE_REGISTRY.feature_set("loop-a-all-bsgp-active-v3-1d").for_family(family), *DEFAULT_FEATURE_REGISTRY.feature_set("loop-a-all-bsgp-active-v3-1w").for_family(family)):
            if feature.source_column not in frame:
                frame[feature.source_column] = 1.0
        frame.to_parquet(path, index=False)
    cycle = begin_loop_a_cycle(tmp_path, symbols=("GOOG",), providers=("databento",), now="2024-03-21T20:06:00Z")
    finish_loop_a_cycle(tmp_path, cycle, failure_count=0, now="2024-03-21T20:07:00Z")
    return bootstrap.create_bootstrap_decisions(tmp_path, symbols=("GOOG",))


def test_bootstrap_matches_production_clock_path_without_model_publication(tmp_path, sealed, monkeypatch):
    # Only external enrichment is replaced; real canonical bars, technical source
    # validation, calendars, universe membership and rolling targets are shared.
    monkeypatch.setattr(rolling, "_attach_loop_a_features", lambda root, decisions, **kwargs: (decisions, ()))
    result = rolling.materialize_rolling_samples(
        tmp_path, symbols=("GOOG",), specifications=bootstrap._specifications(),
        materialized_at="2024-03-21T20:07:00Z", reporter=None,
    )
    actual = result.samples.loc[:, list(bootstrap.COLUMNS)].sort_values(list(bootstrap.COLUMNS)).reset_index(drop=True)
    pd.testing.assert_frame_equal(actual, sealed.decisions, check_dtype=False)
    # DST changes the UTC close; the ML clock follows the exchange, not midnight.
    daily = sealed.decisions.loc[sealed.decisions.horizon.eq("1d"), "decision_timestamp"]
    assert pd.Timestamp("2024-03-08T21:05:00Z") in set(daily)
    assert pd.Timestamp("2024-03-11T20:05:00Z") in set(daily)
    assert not any(daily.dt.date == pd.Timestamp("2024-01-15").date())
    assert daily.max() <= pd.Timestamp(sealed.receipt["input_cutoff"])
    assert not (tmp_path / "ml/latest").exists()
    assert not (tmp_path / "ml/runs").exists()
    assert not (tmp_path / "ml/nightly-gameplan-latest").exists()
    assert tuple(sealed.decisions.columns) == bootstrap.COLUMNS


def test_explicit_bootstrap_breaks_backfill_dependency(tmp_path, sealed, monkeypatch):
    import datafetching.fred_alfred_readiness as module
    monkeypatch.setattr(module, "resolve_current_output", lambda *_: (_ for _ in ()).throw(AssertionError("must not read a model")))
    plan = derive_fred_alfred_backfill_plan(tmp_path, as_of="2024-03-22T12:00:00Z", bootstrap_decisions=sealed.receipt_path)
    assert plan.decision_source == sealed.decisions_path
    assert plan.earliest_eligible_decision == sealed.decisions.decision_timestamp.min()
    assert plan.observation_start.year <= 2022
    with pytest.raises(AssertionError, match="must not read"):
        derive_fred_alfred_backfill_plan(tmp_path)


@pytest.mark.parametrize("field,value", [
    ("schema_version", "unknown"), ("purpose", "MODEL"),
    ("provider", "schwab"), ("feature_profile", "technical-all-v2"),
    ("symbols", ["OTHER"]), ("decision_file", "../escape.parquet"),
    ("row_count", 1), ("input_cutoff", "2020-01-01T00:00:00Z"),
])
def test_receipt_scope_mutations_fail(tmp_path, sealed, field, value):
    record = dict(sealed.receipt)
    record[field] = value
    sealed.receipt_path.write_text(json.dumps(record))
    with pytest.raises(FredAlfredReadinessError):
        bootstrap.read_bootstrap_decisions(tmp_path, sealed.receipt_path)


def test_changed_input_and_decision_bytes_fail(tmp_path, sealed):
    path = sealed.source_files[0]
    original = path.read_bytes()
    path.write_bytes(original + b"changed")
    with pytest.raises(FredAlfredReadinessError, match="checksum"):
        bootstrap.read_bootstrap_decisions(tmp_path, sealed.receipt_path)
    path.write_bytes(original)
    sealed.decisions_path.write_bytes(sealed.decisions_path.read_bytes() + b"changed")
    with pytest.raises(FredAlfredReadinessError, match="decision bytes"):
        bootstrap.read_bootstrap_decisions(tmp_path, sealed.receipt_path)


def test_duplicate_and_missing_route_fail_even_with_resealed_bytes(tmp_path, sealed):
    duplicate = pd.concat([sealed.decisions, sealed.decisions.iloc[[0]]], ignore_index=True)
    with pytest.raises(FredAlfredReadinessError, match="duplicated"):
        bootstrap._validate_decisions(duplicate, symbols=["GOOG"], cutoff=pd.Timestamp(sealed.receipt["input_cutoff"]))
    partial = sealed.decisions.loc[sealed.decisions.horizon.ne("1w-d5")]
    with pytest.raises(FredAlfredReadinessError, match="routes"):
        bootstrap._validate_decisions(partial, symbols=["GOOG"], cutoff=pd.Timestamp(sealed.receipt["input_cutoff"]))


def test_path_escape_is_rejected(tmp_path, sealed):
    for name in ("../outside", "/absolute", "C:/outside", "a\\b", "a:stream"):
        with pytest.raises(FredAlfredReadinessError):
            bootstrap._safe_path(tmp_path.resolve(), name)


def test_actual_scope_is_checked_before_consumption(tmp_path, sealed, monkeypatch):
    report = tmp_path / "readiness.json"
    report.write_text(json.dumps({"bootstrap_decisions": {
        "receipt_path": sealed.receipt_path.relative_to(tmp_path).as_posix(),
        "receipt_checksum_sha256": file_checksum(sealed.receipt_path),
    }}))
    evidence = SimpleNamespace(readiness=SimpleNamespace(report_path=report), release_context=None, vintages=None,
        bootstrap_authority={**json.loads(report.read_text())["bootstrap_decisions"], "readiness_report_sha256": file_checksum(report)})
    calls = []
    def coverage(actual, **kwargs):
        calls.append(kwargs["minimum_coverage"])
        return {"status": "PASS"}
    monkeypatch.setattr(bootstrap, "_coverage_report", coverage)
    args = dict(evidence=evidence, samples=sealed.decisions, specifications=bootstrap._specifications(), provider="databento")
    bootstrap.validate_bootstrap_consumption(tmp_path, **args)
    assert calls == [0.95]
    changed = sealed.decisions.copy()
    changed.loc[0, "decision_timestamp"] += pd.Timedelta(seconds=1)
    with pytest.raises(FredAlfredReadinessError, match="scope differs"):
        bootstrap.validate_bootstrap_consumption(tmp_path, **{**args, "samples": changed})
    specs = dict(args["specifications"])
    specs["1d"] = replace(specs["1d"], processing_delay=pd.Timedelta(minutes=6))
    with pytest.raises(FredAlfredReadinessError, match="contract differs"):
        bootstrap.validate_bootstrap_consumption(tmp_path, **{**args, "specifications": specs})
    monkeypatch.setattr(bootstrap, "_coverage_report", lambda *a, **k: {"status": "FAIL"})
    with pytest.raises(FredAlfredReadinessError, match="coverage or causal"):
        bootstrap.validate_bootstrap_consumption(tmp_path, **args)


def test_real_import_and_readiness_remain_causal_and_sealed(tmp_path, sealed, monkeypatch):
    imported = import_fred_alfred_vintages(
        tmp_path, client=FredAlfredClient(_TEST_KEY, session=_CompleteFredSession(), sleeper=lambda _: None),
        series_ids=FRED_ALFRED_SUPPORTED_SERIES,
        realtime_start="2023-01-01", realtime_end="2024-05-31",
        observation_start="2022-01-01", observation_end="2024-05-31", acquired_at="2024-06-01T12:00:00Z",
    )
    with pytest.raises(ValueError, match="cannot lower"):
        verify_and_publish_fred_alfred_readiness(tmp_path, import_result=imported, bootstrap_decisions=sealed.receipt_path, minimum_coverage=0.5)
    readiness = verify_and_publish_fred_alfred_readiness(tmp_path, import_result=imported,
        bootstrap_decisions=sealed.receipt_path, verified_at="2024-06-01T12:01:00Z")
    assert readiness.coverage["status"] == "PASS"
    assert json.loads(readiness.report_path.read_text())["schema_version"] == "fred-alfred-bootstrap-readiness-v1"
    evidence = read_verified_macro_evidence(tmp_path)
    bootstrap.validate_bootstrap_consumption(tmp_path, evidence=evidence, samples=sealed.decisions,
        specifications=bootstrap._specifications(), provider="databento")
    remove_first = False
    original_attach = rolling._attach_loop_a_features
    original_values = rolling._family_values
    # Other families are synthetic fixture inputs; keep the production macro
    # reader, cache, join and final scope check intact.
    monkeypatch.setattr(rolling, "_family_values", lambda fs, family:
        original_values(fs, family) if family == "macro" else {})
    def attach_macro(root, decisions, **kwargs):
        decisions = decisions.iloc[1:].copy() if remove_first else decisions.copy()
        feature_set = DEFAULT_FEATURE_REGISTRY.feature_set(kwargs["feature_set_name"])
        extras = {feature.name: 0.0 for feature in feature_set.features
                  if feature.source_family != "macro" and feature.name not in decisions}
        decisions = pd.concat([decisions, pd.DataFrame(extras, index=decisions.index)], axis=1)
        return original_attach(root, decisions, **kwargs)
    monkeypatch.setattr(rolling, "_attach_loop_a_features", attach_macro)
    assert readiness.verified_at > pd.Timestamp(sealed.receipt["input_cutoff"])
    materialized = rolling.materialize_rolling_samples(tmp_path, symbols=("GOOG",),
        specifications=bootstrap._specifications(), materialized_at="2024-06-01T12:02:00Z",
        input_available_at=sealed.receipt["input_cutoff"], reporter=None)
    assert len(materialized.samples) == len(sealed.decisions)
    remove_first = True
    with pytest.raises(FredAlfredReadinessError, match="scope differs"):
        rolling.materialize_rolling_samples(tmp_path, symbols=("GOOG",),
            specifications=bootstrap._specifications(), materialized_at="2024-06-01T12:02:00Z",
            input_available_at=sealed.receipt["input_cutoff"], reporter=None)
    assert not (tmp_path / "ml/latest").exists()
    assert not (tmp_path / "ml/nightly-gameplan-latest").exists()
    original_report = readiness.report_path.read_bytes()
    changed_report = json.loads(original_report)
    changed_report.pop("bootstrap_decisions")
    readiness.report_path.write_text(json.dumps(changed_report))
    with pytest.raises(FredAlfredReadinessError, match="readiness report changed"):
        bootstrap.validate_bootstrap_consumption(tmp_path, evidence=evidence, samples=sealed.decisions,
            specifications=bootstrap._specifications(), provider="databento")
    readiness.report_path.write_bytes(original_report)
    sealed.receipt_path.write_bytes(sealed.receipt_path.read_bytes() + b" ")
    with pytest.raises(FredAlfredReadinessError):
        read_verified_macro_evidence(tmp_path)


def test_cli_rejects_implicit_or_incremental_bootstrap_selection(tmp_path):
    from ml.option_pricing_fred import _request_plan
    args = SimpleNamespace(bootstrap_decisions=tmp_path / "receipt.json", series=FRED_ALFRED_SUPPORTED_SERIES,
        backfill=False, incremental=True, realtime_start=None, realtime_end=None,
        observation_start=None, observation_end=None)
    with pytest.raises(ValueError, match="requires --backfill"):
        _request_plan(tmp_path, args=args)
