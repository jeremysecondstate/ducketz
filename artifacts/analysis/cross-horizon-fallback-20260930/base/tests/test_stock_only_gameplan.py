from __future__ import annotations

import json
from datetime import date
from pathlib import Path
from types import SimpleNamespace

import pandas as pd
import pytest

from ml import nightly_gameplan as nightly
from ml.artifacts import file_checksum

_REAL_CURRENT_SOURCE_SELECTOR = nightly._current_overnight_sources


def test_invalid_source_selection_cannot_advance_pointer_or_write_receipt(tmp_path, monkeypatch):
    run = tmp_path / "ml/nightly-gameplan-runs/invalid-selector"
    run.mkdir(parents=True)
    pointer = tmp_path / "ml/nightly-gameplan-latest/run.json"
    pointer.parent.mkdir(parents=True)
    pointer.write_bytes(b"previous publication pointer")
    monkeypatch.setattr(nightly, "verify_manifest", lambda path: {
        "configuration": {"source_selection_contract": "unknown-policy"}})
    with pytest.raises(RuntimeError, match="source selection configuration"):
        nightly._publish_gameplan(tmp_path, run=run, action_date=date(2026, 9, 14),
                                  published_at=pd.Timestamp("2026-09-13T05:00Z"),
                                  source_loop_b="fixture", source_strategy=None)
    assert pointer.read_bytes() == b"previous publication pointer"
    assert not (run / "receipt.json").exists()


SYMBOLS = ("AAPL", "AMZN", "GOOG", "MU", "NVDA", "SNDK", "COST")
ACTION_DATE = date(2026, 9, 8)
FROZEN_AT = pd.Timestamp("2026-09-08T05:00:00Z")


def _forecasts() -> pd.DataFrame:
    rows = []
    for symbol in SYMBOLS:
        for group, anchors in (("1h", range(4, 18)), ("4h", (4, 8, 12, 16)),
                               ("1d", range(1, 6)), ("1w", (5,))):
            for anchor in anchors:
                route = f"{group}@{anchor:02d}:00" if group in {"1h", "4h"} else f"{group}@D+{anchor}"
                start = nightly._local_timestamp(ACTION_DATE, anchor if group in {"1h", "4h"} else 4)
                rows.append({
                    "symbol": symbol, "route": route, "model_group": group,
                    "forecast_anchor_local": route.split("@", 1)[1],
                    "calibrated_probability": 0.7, "raw_probability": 0.71,
                    "model_status": "RESEARCH_NOT_PROMOTED" if group == "1w" else "PROMOTED",
                    "decision_timestamp": FROZEN_AT - pd.Timedelta(hours=1),
                    "information_available_at": FROZEN_AT - pd.Timedelta(hours=1),
                    "target_window_start": start,
                    "target_window_end": start + pd.Timedelta(hours=1 if group == "1h" else 4),
                })
    return pd.DataFrame(rows)


@pytest.fixture
def publication_fixture(tmp_path, monkeypatch):
    source = tmp_path / "ml" / "runs" / "loop-b-fixture"
    source.mkdir(parents=True)
    samples = pd.DataFrame({"symbol": SYMBOLS, "mr__test": range(len(SYMBOLS))})
    samples.to_parquet(source / "samples.parquet", index=False)
    samples[["symbol"]].to_parquet(source / "predictions.parquet", index=False)
    for name in ("manifest.json", "publication.json"):
        (source / name).write_text("{}", encoding="utf-8")
    manifest = {"configuration": {"symbols": list(SYMBOLS)}, "feature_columns": ["mr__test"]}
    monkeypatch.setattr(nightly, "read_current_publication",
                        lambda root: SimpleNamespace(run_directory=source, manifest=manifest))

    def no_strategy(*args, **kwargs):
        raise RuntimeError("Strategy publication unavailable in stock-only fixture")

    monkeypatch.setattr(nightly, "read_current_strategy_publication", no_strategy)
    sources = samples.assign(bar_end_timestamp=pd.Timestamp("2026-09-05T00:00:00Z"))
    monkeypatch.setattr(nightly, "_overnight_sources", lambda *a, **k: sources)
    monkeypatch.setattr(nightly, "_current_overnight_sources", lambda *a, **k: (sources, ACTION_DATE))
    for symbol in SYMBOLS:
        directory = tmp_path / "market-data/databento/opra/OPRA.PILLAR/state/symbol-history" / symbol
        directory.mkdir(parents=True)
        for schema in ("definition", "ohlcv-1h", "cbbo-1m"):
            (directory / f"{schema}.json").write_text(json.dumps({"completed_through": "2026-09-05"}))
    monkeypatch.setattr(nightly, "_load_equity_minute_bars", lambda *a, **k: ({}, ()))
    frame = _forecasts()
    groups = {group: frame.loc[frame.model_group.eq(group)].copy() for group in nightly.MODEL_GROUPS}
    monkeypatch.setattr(nightly, "_build_training_groups", lambda *a, **k: groups)
    monkeypatch.setattr(nightly, "_build_current_groups", lambda *a, **k: groups)
    evaluation_run = tmp_path / "ml/gameplan-evaluation-runs/evaluation-fixture"
    evaluation_run.mkdir(parents=True)
    evaluations = pd.DataFrame({"evaluation_status": ["PENDING_MATURITY"]})
    evaluations.to_parquet(evaluation_run / "evaluations.parquet", index=False)
    (evaluation_run / "receipt.json").write_text("{}", encoding="utf-8")
    monkeypatch.setattr("ml.gameplan_evaluation.evaluate_saved_gameplans",
                        lambda *a, **k: SimpleNamespace(run_directory=evaluation_run, evaluations=evaluations))
    clock = [FROZEN_AT]
    monkeypatch.setattr(nightly, "utc_timestamp", lambda value=None: pd.Timestamp(clock[0] if value is None else value))
    fits = []

    def fit(group_data, *, current, feature_columns, group, model_directory, trained_at):
        fits.append(group)
        from ml.independent_stock_targets import STOCK_CALENDAR_FEATURE_NAMES, STOCK_TARGET_CONTRACT_VERSION
        independent = "target_contract_version" in current and current.target_contract_version.eq(STOCK_TARGET_CONTRACT_VERSION).all()
        assert feature_columns == (("mr__test", *STOCK_CALENDAR_FEATURE_NAMES) if independent else ("mr__test",))
        model_directory.mkdir(parents=True)
        (model_directory / "model.joblib").write_bytes(b"synthetic model fixture")
        return {"forecasts": current.copy(), "report": {
            "promotion_gate": {"status": current.model_status.iloc[0]},
            "selected_family": "synthetic-test", "partitions": {"assessment_rows": 10},
        }}

    monkeypatch.setattr(nightly, "_fit_group_model", fit)
    return SimpleNamespace(root=tmp_path, expected=frame, clock=clock, fits=fits, fit=fit)


def test_stock_only_publishes_verified_seven_symbol_forecasts_without_strategy(publication_fixture):
    fixture = publication_fixture
    result = nightly.run_nightly_gameplan_once(fixture.root, stock_only=True, reporter=None)
    publication = nightly.read_current_gameplan(fixture.root)
    saved = nightly.read_gameplan_run(fixture.root, result.run_directory)
    assert publication.run_directory == saved.run_directory == result.run_directory
    assert fixture.fits == list(nightly.MODEL_GROUPS)
    forecasts = pd.read_parquet(result.run_directory / "forecasts.parquet")
    intents = pd.read_parquet(result.run_directory / "option-strategy-intents.parquet")
    assert result.forecast_rows == result.option_intent_rows == len(forecasts) == len(intents) == 168
    for frame in (forecasts, intents):
        assert frame.symbol.value_counts().to_dict() == {symbol: 24 for symbol in SYMBOLS}
        assert not frame[["symbol", "route"]].duplicated().any()
        assert not frame.broker_orders_enabled.any()
    pd.testing.assert_series_equal(
        forecasts.set_index(["symbol", "route"]).model_status.sort_index(),
        fixture.expected.set_index(["symbol", "route"]).model_status.sort_index(),
    )
    assert intents.plan_status.eq("NO_TRADE_STOCK_ONLY").all()
    for column in ("candidate_key", "legs_json", "decision_score", "score_basis", "strategy_source_run"):
        assert intents[column].isna().all(), column
    config = publication.manifest["configuration"]
    assert config["preparation_scope"] == publication.receipt["preparation_scope"] == "STOCK_ONLY"
    assert config["source_strategy_run"] is None
    assert publication.receipt["source_strategy_run"] is None
    assert config["opra_history"]["verified_cursor_count"] == 21
    assert publication.receipt["orders_placed"] == 0
    assert publication.receipt["manifest_checksum_sha256"] == file_checksum(result.run_directory / "manifest.json")
    assert "strategy-candidates" not in json.dumps(publication.manifest)
    assert not (fixture.root / "ml/strategy-runs").exists()


def test_default_publication_still_requires_strategy(publication_fixture):
    with pytest.raises(RuntimeError, match="Strategy publication unavailable"):
        nightly.run_nightly_gameplan_once(publication_fixture.root, reporter=None)
    assert publication_fixture.fits == []
    assert not (publication_fixture.root / "ml/nightly-gameplan-latest/run.json").exists()


def test_stock_only_ignores_supplied_option_candidates():
    forecasts = _forecasts().assign(id=lambda frame: frame.symbol + ":" + frame.route, direction="BULLISH")
    candidates = pd.DataFrame([{
        "symbol": "COST", "horizon": "1h", "candidate_key": "must-not-be-selected",
        "candidate_rank": 1, "score_basis": "OPRA_EXECUTION_CALIBRATED_MODEL",
        "decision_score": 0.99, "legs_json": '[{"symbol":"COST_OPTION"}]',
    }])
    intents = nightly._option_intents(forecasts, candidates=candidates, strategy_run=Path("ignored-strategy"),
                                      action_date=ACTION_DATE, stock_only=True)
    assert len(intents) == 168
    assert intents.plan_status.eq("NO_TRADE_STOCK_ONLY").all()
    for column in ("candidate_key", "legs_json", "decision_score", "score_basis", "strategy_source_run"):
        assert intents[column].isna().all(), column


@pytest.mark.parametrize("late_at", ["start", "completion"])
def test_stock_only_keeps_publication_deadline(publication_fixture, monkeypatch, late_at):
    fixture = publication_fixture
    boundary = nightly._local_timestamp(ACTION_DATE, nightly.ACTION_START_HOUR)
    pointer = fixture.root / "ml/nightly-gameplan-latest/run.json"
    pointer.parent.mkdir(parents=True)
    pointer.write_text("original immutable pointer", encoding="utf-8")
    before = pointer.read_bytes()
    if late_at == "start":
        fixture.clock[0] = boundary
    else:
        def finish_late(*args, **kwargs):
            result = fixture.fit(*args, **kwargs)
            if kwargs["group"] == nightly.MODEL_GROUPS[-1]:
                fixture.clock[0] = boundary
            return result
        monkeypatch.setattr(nightly, "_fit_group_model", finish_late)
    with pytest.raises(RuntimeError, match="04:00 PT"):
        nightly.run_nightly_gameplan_once(fixture.root, stock_only=True, reporter=None)
    assert pointer.read_bytes() == before
    assert not list((fixture.root / "ml/nightly-gameplan-runs").glob("*/receipt.json"))
    assert fixture.fits == ([] if late_at == "start" else list(nightly.MODEL_GROUPS))


@pytest.mark.parametrize("price_source", ["canonical-equity-minute-v1", "xnas-itch-archive-v1"])
def test_independent_target_publication_uses_new_labels_and_preserves_native_contract(publication_fixture, monkeypatch, price_source):
    from ml.independent_stock_targets import STOCK_TARGET_CONTRACT_VERSION, stock_target_windows

    fixture = publication_fixture
    sources = []
    points = set()
    for day in (date(2026, 8, 17), ACTION_DATE):
        windows = stock_target_windows(day)
        gap = next(row for row in windows if row["route"] == "1h@gap")
        for index, symbol in enumerate(SYMBOLS):
            information = gap["target_window_start"] + pd.Timedelta(minutes=5)
            sources.append({"symbol": symbol, "action_date": day, "mr__test": float(index),
                            "decision_timestamp": information, "information_available_at": information,
                            "bar_end_timestamp": gap["target_window_start"],
                            "bar_timestamp": gap["target_window_start"] - pd.Timedelta(hours=1),
                            "exchange_session": str(gap["target_window_start"].tz_convert("America/Los_Angeles").date()),
                            "exchange_calendar": "XNAS", "horizon": "1h", "timeframe": "1h",
                            "target_window_start": nightly._local_timestamp(day, 4),
                            "assumed_round_trip_cost": 0.0})
            for row in windows:
                is_gap = row["target_role"] == "OPENING_GAP_RESEARCH"
                points.add((symbol, row["target_window_start"] - pd.Timedelta(minutes=int(is_gap))))
                points.add((symbol, row["target_window_end"] - pd.Timedelta(minutes=int(not is_gap))))
    sources = pd.DataFrame(sources)
    sources.to_parquet(fixture.root / "ml/runs/loop-b-fixture/samples.parquet", index=False)
    monkeypatch.setattr(nightly, "_current_overnight_sources", _REAL_CURRENT_SOURCE_SELECTOR)
    origin = min(timestamp for _, timestamp in points)
    rows = []
    for symbol, timestamp in sorted(points):
        slope = 0.1 if symbol == "AAPL" else -0.1
        price = 10000 + slope * (timestamp - origin).total_seconds() / 3600
        rows.append({"symbol": symbol, "timestamp": timestamp, "open": price, "close": price + slope / 60})
    monkeypatch.setattr(nightly, "_load_equity_minute_bars", lambda *a, **k: (pd.DataFrame(rows), ()))
    def explicit_source(root, *, symbols, source_contract):
        assert tuple(symbols) == SYMBOLS
        assert source_contract == "xnas-itch-archive-v1"
        report = {"source_contract": source_contract, "dataset": "XNAS.ITCH"}
        bars = pd.DataFrame(rows)
        bars.attrs["stock_price_source"] = report
        return bars, (), report
    monkeypatch.setattr(nightly, "load_stock_target_prices", explicit_source)

    def fit_new_targets(training, **kwargs):
        assert set(training.target) == {0, 1}
        assert training.target_contract_version.eq(STOCK_TARGET_CONTRACT_VERSION).all()
        kwargs["current"] = kwargs["current"].assign(
            model_status="RESEARCH_NOT_PROMOTED" if kwargs["group"] == "1w" else "PROMOTED",
            calibrated_probability=0.7, raw_probability=0.71)
        result = fixture.fit(training, **kwargs)
        result["report"]["source_selection_contract"] = nightly.GAMEPLAN_SOURCE_SELECTION_VERSION
        return result

    monkeypatch.setattr(nightly, "_fit_group_model", fit_new_targets)
    result = nightly.run_nightly_gameplan_once(fixture.root, stock_only=True,
                                              independent_stock_horizons=True, stock_price_source=price_source, reporter=None)
    publication = nightly.read_current_gameplan(fixture.root)
    forecasts = pd.read_parquet(result.run_directory / "forecasts.parquet")
    intents = pd.read_parquet(result.run_directory / "option-strategy-intents.parquet")
    assert len(forecasts) == len(intents) == 168
    assert publication.manifest["configuration"]["target_contract_version"] == STOCK_TARGET_CONTRACT_VERSION
    assert publication.manifest["configuration"]["target_calendar_feature_contract"] == "independent-stock-known-calendar-inputs-v1"
    assert forecasts.target_contract_version.eq(STOCK_TARGET_CONTRACT_VERSION).all()
    assert forecasts.target_price_source_contract.eq(price_source).all()
    assert forecasts.source_selection_contract.eq(nightly.GAMEPLAN_SOURCE_SELECTION_VERSION).all()
    assert publication.manifest["configuration"]["source_selection_contract"] == nightly.GAMEPLAN_SOURCE_SELECTION_VERSION
    assert publication.manifest["configuration"]["target_price_source_contract"] == price_source
    for group in nightly.MODEL_GROUPS:
        name = f"training-cohort-{group}.parquet"
        assert name in publication.manifest["output_files"]
        cohort = pd.read_parquet(result.run_directory / name)
        assert cohort.target_price_source_contract.eq(price_source).all()
        assert cohort.source_selection_contract.eq(nightly.GAMEPLAN_SOURCE_SELECTION_VERSION).all()
        assert {"target", "observed_return", "decision_timestamp", "information_available_at",
                "target_window_start", "target_window_end", "observed_open_timestamp", "observed_close_timestamp"} <= set(cohort)
    assert forecasts.execution_eligible.sum() == 19 * 7
    assert forecasts.loc[forecasts.route.eq("1h@04:00"), "target_window_start"].eq(pd.Timestamp("2026-09-08T11:00Z")).all()
    assert forecasts.loc[forecasts.route.eq("4h@16:00"), "target_window_end"].eq(pd.Timestamp("2026-09-09T14:00Z")).all()
    assert forecasts.loc[forecasts.route.eq("1w@D+5"), "target_window_end"].eq(pd.Timestamp("2026-09-15T00:00Z")).all()
    assert forecasts.loc[forecasts.route.eq("1h@gap"), "action_anchor_local"].isna().all()
    assert intents.plan_status.eq("NO_TRADE_STOCK_ONLY").all()
    assert publication.receipt["orders_placed"] == 0
    # Even internally consistent file hashes cannot mix feature-selection policies.
    import copy
    invalid = copy.deepcopy(publication.manifest)
    invalid["configuration"]["source_selection_contract"] = "unknown-policy"
    with pytest.raises(RuntimeError, match="source selection configuration"):
        nightly._verify_source_selection_metadata(result.run_directory, invalid)
    report_path = result.run_directory / "model-reports.json"
    reports = json.loads(report_path.read_text())
    reports["1h"]["source_selection_contract"] = None
    report_path.write_text(json.dumps(reports))
    with pytest.raises(RuntimeError, match="model reports and source selection"):
        nightly._verify_source_selection_metadata(result.run_directory, publication.manifest)
