from __future__ import annotations

import json
from dataclasses import replace
from datetime import date
from pathlib import Path

import pandas as pd
import pytest
import joblib
from sklearn.dummy import DummyClassifier

from ml.artifacts import write_manifest, file_checksum
from ml.calibration import IdentityCalibrator
from ml.independent_stock_targets import STOCK_TARGET_CONTRACT_VERSION, stock_target_windows
from ml.nightly_gameplan import EXECUTION_AUTHORITY, GAMEPLAN_VERSION, _publish_gameplan
from ml.stock_trader import independent_signals as signals_module
from ml.stock_trader.contracts import PredictionSignal, canonical_sha256
from ml.stock_trader.independent_signals import load_current_independent_gameplan_signals
from ml.stock_trader.model import (
    ENRICHMENT_FEATURE_NAMES,
    enrichment_signal_readiness,
    model_from_payload,
    require_enrichment_signal_support,
)
from ml.stock_trader.training import fit_enrichment_model_payload


SYMBOLS = ("AAPL", "AMZN", "GOOG", "MU", "NVDA", "SNDK", "COST")
ACTION_DATE = date(2026, 9, 8)
FROZEN_AT = pd.Timestamp("2026-09-08T06:00:00Z")


def _frame() -> pd.DataFrame:
    rows = []
    for symbol in SYMBOLS:
        for spec in stock_target_windows(ACTION_DATE):
            probability = 0.40 if spec["model_group"] == "4h" else 0.60
            rows.append({
                **spec,
                "id": f"{symbol}:{spec['route']}",
                "symbol": symbol,
                "decision_timestamp": FROZEN_AT,
                "information_available_at": FROZEN_AT,
                "frozen_at": FROZEN_AT,
                "action_date": ACTION_DATE.isoformat(),
                "target_contract_version": STOCK_TARGET_CONTRACT_VERSION,
                "execution_authority": EXECUTION_AUTHORITY,
                "broker_orders_enabled": False,
                "calibrated_probability": probability,
                "model_status": "PROMOTED",
                "model_family": "fixture",
                "model_artifact": f"models/{spec['model_group']}/model.joblib",
                "direction": "BEARISH" if probability < 0.5 else "BULLISH",
                "action_anchor_local": pd.Timestamp(spec["target_window_start"]).tz_convert("America/Los_Angeles").strftime("%H:%M") if spec["execution_eligible"] else None,
            })
    return pd.DataFrame(rows)


def _publish(root: Path, frame: pd.DataFrame, *, target_contract: str = STOCK_TARGET_CONTRACT_VERSION, reports=None) -> Path:
    run = root / "ml/nightly-gameplan-runs/fixture"
    run.mkdir(parents=True, exist_ok=True)
    frame.to_parquet(run / "forecasts.parquet", index=False)
    frame[["symbol", "route"]].assign(plan_status="NO_TRADE_STOCK_ONLY").to_parquet(run / "option-strategy-intents.parquet", index=False)
    payload = {
        "target_contract_version": target_contract,
        "action_date": ACTION_DATE.isoformat(),
        "symbols": list(SYMBOLS),
        "execution_authority": EXECUTION_AUTHORITY,
        "broker_orders_enabled": False,
        "orders_placed": 0,
    }
    (run / "gameplan.json").write_text(json.dumps(payload), encoding="utf-8")
    extra_outputs = []
    extra_config = {}
    if reports is not None:
        for horizon, report in reports.items():
            if "features" not in report:
                continue  # Deliberately bare reports remain invalid evidence.
            model_name = f"models/{horizon}/model.joblib"
            model_path = run / model_name
            model_path.parent.mkdir(parents=True, exist_ok=True)
            model = DummyClassifier().fit(pd.DataFrame({"x": [0., 1.]}), [0, 1])
            joblib.dump({"schema_version": GAMEPLAN_VERSION, "group": horizon,
                "feature_columns": ["x"], "categorical_columns": ["symbol", "route"],
                "selected_family": "fixture", "calibrator": IdentityCalibrator(), "estimator": model,
                "trained_at": FROZEN_AT.isoformat()}, model_path)
            report.setdefault("model_file", {"path": model_name, "size": model_path.stat().st_size,
                                           "checksum_sha256": file_checksum(model_path)})
            cohort_name = f"training-cohort-{horizon}.parquet"
            pd.DataFrame({"x": [0., 1.], "target": [0, 1]}).to_parquet(run / cohort_name, index=False)
            extra_outputs.extend((model_name, cohort_name))
        (run / "model-reports.json").write_text(json.dumps(reports), encoding="utf-8")
        extra_outputs.append("model-reports.json")
        extra_config["target_price_source_contract"] = "canonical-equity-minute-v1"
        extra_config.update(schema_version=GAMEPLAN_VERSION, target_price_dataset="EQUS.MINI")
    write_manifest(
        run,
        run_timestamp=FROZEN_AT,
        input_files=(),
        output_files=("gameplan.json", "forecasts.parquet", "option-strategy-intents.parquet", *extra_outputs),
        configuration={**payload, "preparation_scope": "STOCK_ONLY", **extra_config},
    )
    _publish_gameplan(root, run=run, action_date=ACTION_DATE, published_at=FROZEN_AT, source_loop_b="fixture", source_strategy=None)
    return run


def _promoted_reports():
    keys = ("assessment_has_at_least_10_decision_clusters", "brier_beats_training_base_rate",
            "calibration_retains_directional_information", "expected_calibration_error_at_most_0_15",
            "log_loss_beats_training_base_rate")
    return {h: {"promotion_gate": {"status": "PROMOTED", "checks": dict.fromkeys(keys, True)},
                "schema_version": GAMEPLAN_VERSION, "group": h, "target_contract_version": STOCK_TARGET_CONTRACT_VERSION,
                "target_price_source_contract": "canonical-equity-minute-v1", "target_price_dataset": "EQUS.MINI",
                "assessment": {"brier_score": .1, "log_loss": .3, "expected_calibration_error_10_bin": .1},
                "training_base_rate_assessment": {"brier_score": .25, "log_loss": .69},
                "partition_decision_clusters": {"assessment": 10},
                "calibration_diagnostics": {"information_available": True},
                "target_support_by_symbol": {symbol: {"fitted_rows": 20,
                    "fitted_rows_by_route": {spec["route"]: 20 for spec in stock_target_windows(ACTION_DATE)
                                             if spec["model_group"] == h}} for symbol in SYMBOLS},
                "features": {"admitted": ["x"]}, "selected_family": "fixture", "calibration_method": "none"}
            for h in ("1h", "4h", "1d", "1w")}


def test_fixed_policy_requires_real_manifest_bound_model_reports(published):
    with pytest.raises(ValueError, match="manifest-bound"):
        load_current_independent_gameplan_signals(published, as_of="2026-09-08T11:01:00Z", require_promoted_model_reports=True)
    reports = _promoted_reports()
    reports["1h"]["promotion_gate"]["checks"]["brier_beats_training_base_rate"] = False
    _publish(published, _frame(), reports=reports)
    with pytest.raises(ValueError, match="recorded assessment gates"):
        load_current_independent_gameplan_signals(published, as_of="2026-09-08T11:01:00Z", require_promoted_model_reports=True)


def test_fixed_policy_preflight_separates_qualified_bearish_forecasts_from_model_failure(published):
    from ml.stock_trader.independent_session import _independent_forecast_preflight
    frame = _frame()
    frame["calibrated_probability"] = .4
    frame["direction"] = "BEARISH"
    _publish(published, frame, reports=_promoted_reports())
    result = _independent_forecast_preflight(published, action_date=ACTION_DATE)
    assert result["status"] == "READY"
    assert result["all_execution_windows_qualified"] is True
    assert result["execution_window_count"] == len(result["qualified_forecast_windows"]) == 133
    assert result["bullish_entry_windows"] == []


def test_explicit_late_opening_retains_frozen_ids_and_ends_and_expires(published):
    _publish(published, _frame(), reports=_promoted_reports())
    standard, _ = load_current_independent_gameplan_signals(published, as_of='2026-09-08T11:01:00Z')
    assert load_current_independent_gameplan_signals(published, as_of='2026-09-08T11:30:00Z')[0] == {}
    late, _ = load_current_independent_gameplan_signals(published, as_of='2026-09-08T11:30:00Z',
                   require_promoted_model_reports=True, late_opening_date='2026-09-08')
    assert set(late) == set(standard)
    for key, signal in late.items():
        assert signal.prediction_id == standard[key].prediction_id
        assert signal.target_window_end == standard[key].target_window_end
        assert signal.actionable_until == '2026-09-08T12:00:00+00:00'
    for clock in ('2026-09-08T12:00:00Z', '2026-09-09T11:30:00Z'):
        with pytest.raises(ValueError, match='late-opening date'):
            load_current_independent_gameplan_signals(published, as_of=clock, late_opening_date='2026-09-08')


@pytest.mark.parametrize("fault", ["bare_flags", "wrong_schema", "wrong_group", "wrong_source", "failed_metrics", "wrong_artifact", "unseen_symbol", "unseen_route"])
def test_fixed_policy_rejects_qualification_without_actual_compatible_evidence(published, fault):
    reports = _promoted_reports()
    frame = _frame()
    if fault == "bare_flags":
        reports = {h: {"promotion_gate": r["promotion_gate"]} for h, r in reports.items()}
    elif fault == "wrong_schema":
        reports["1h"]["schema_version"] = "unknown"
    elif fault == "wrong_group":
        reports["1h"]["group"] = "4h"
    elif fault == "wrong_source":
        reports["1h"]["target_price_source_contract"] = "xnas-itch-archive-v1"
    elif fault == "failed_metrics":
        reports["1h"]["assessment"]["brier_score"] = .3
    elif fault == "unseen_symbol":
        reports["1h"]["target_support_by_symbol"]["COST"]["fitted_rows"] = 0
    elif fault == "unseen_route":
        reports["1h"]["target_support_by_symbol"]["COST"]["fitted_rows_by_route"]["1h@04:00"] = 0
    else:
        frame.loc[frame.model_group.eq("1h"), "model_artifact"] = "models/4h/model.joblib"
    _publish(published, frame, reports=reports)
    with pytest.raises(ValueError):
        load_current_independent_gameplan_signals(published, as_of="2026-09-08T11:01:00Z", require_promoted_model_reports=True)


def test_fixed_qualification_excludes_research_horizon(published):
    from ml.stock_trader.independent_signals import verified_promoted_model_groups
    from ml.nightly_gameplan import read_current_gameplan
    reports = _promoted_reports()
    reports["4h"]["promotion_gate"]["status"] = "RESEARCH_NOT_PROMOTED"
    _publish(published, _frame(), reports=reports)
    assert verified_promoted_model_groups(read_current_gameplan(published)) == frozenset({"1h", "1d", "1w"})


def test_reader_accepts_numerically_verified_v2_tolerance_without_rewriting_legacy(published):
    from ml.gameplan_promotion import build_promotion_gate
    from ml.stock_trader.independent_signals import verified_promoted_model_groups
    from ml.nightly_gameplan import read_current_gameplan
    reports = _promoted_reports()
    daily = reports["1d"]
    daily["assessment"].update(brier_score=.253, log_loss=.695)
    daily["calibration_diagnostics"].update(calibrated_probability_range=[.4, .6],
        assessment_probability_range=[.4, .6], calibration_positive_rate=.5, nondecreasing_constraint_active=False)
    daily["promotion_gate"] = build_promotion_gate(daily["assessment"], daily["training_base_rate_assessment"],
        daily["calibration_diagnostics"], 10)
    _publish(published, _frame(), reports=reports)
    assert verified_promoted_model_groups(read_current_gameplan(published)) == frozenset({"1h", "4h", "1d", "1w"})
    daily["promotion_gate"]["baseline_tolerances"]["brier_score"] = .05
    _publish(published, _frame(), reports=reports)
    with pytest.raises(ValueError, match="tolerances"):
        verified_promoted_model_groups(read_current_gameplan(published))


@pytest.fixture
def published(tmp_path, monkeypatch):
    monkeypatch.setattr(signals_module, "STOCK_TRADER_SYMBOLS", SYMBOLS)
    monkeypatch.setattr("ml.stock_trader.independent_session.STOCK_TRADER_SYMBOLS", SYMBOLS)
    _publish(tmp_path, _frame())
    return tmp_path


def test_opening_returns_four_independent_horizons_per_symbol_without_conflict_veto(published):
    signals, sources = load_current_independent_gameplan_signals(published, as_of="2026-09-08T11:01:00Z")
    assert len(signals) == 28
    assert len(sources) == 6
    assert set(signals) == {(symbol, horizon) for symbol in SYMBOLS for horizon in ("1h", "4h", "1d", "1w")}
    assert signals[("COST", "1h")].suggested_action == "BUY"
    assert signals[("COST", "4h")].suggested_action == "SELL"
    for (_, horizon), signal in signals.items():
        assert signal.horizon_probabilities == {horizon: signal.calibrated_probability}
        assert signal.target_definition_version == STOCK_TARGET_CONTRACT_VERSION
        assert "gap" not in signal.prediction_id
    assert signals[("COST", "1d")].target_window_end == "2026-09-09T00:00:00+00:00"
    assert signals[("COST", "1w")].target_window_end == "2026-09-15T00:00:00+00:00"


def test_last_hour_preserves_cross_session_four_hour_expiry(published):
    signals, _ = load_current_independent_gameplan_signals(published, as_of="2026-09-08T23:01:00Z")
    assert set(horizon for _, horizon in signals) == {"1h", "4h"}
    assert len(signals) == 14
    assert signals[("COST", "1h")].target_window_end == "2026-09-09T00:00:00+00:00"
    assert signals[("COST", "4h")].target_window_end == "2026-09-09T14:00:00+00:00"


def test_market_inputs_are_transported_from_verified_frozen_rows(published):
    from ml.stock_trader.market_features import INDEPENDENT_MARKET_FEATURE_NAMES, INDEPENDENT_MARKET_FEATURE_CONTRACT
    frame = _frame()
    values = {name: (index + 1) / 10 for index, name in enumerate(INDEPENDENT_MARKET_FEATURE_NAMES)}
    frame["enrichment_feature_values_json"] = json.dumps(values)
    frame["enrichment_feature_contract"] = INDEPENDENT_MARKET_FEATURE_CONTRACT
    _publish(published, frame)
    signals, _ = load_current_independent_gameplan_signals(published, as_of="2026-09-08T11:01:00Z")
    assert signals[("COST", "1h")].enrichment_feature_values == values
    assert signals[("COST", "1w")].enrichment_feature_values == values


@pytest.mark.parametrize("payload", ['{"future_return": 0.5}', '[]', 'null', '{invalid'])
def test_invalid_market_input_contract_cannot_authorize_signal(published, payload):
    frame = _frame()
    frame["enrichment_feature_values_json"] = payload
    _publish(published, frame)
    with pytest.raises(ValueError):
        load_current_independent_gameplan_signals(published, as_of="2026-09-08T11:01:00Z")


@pytest.mark.parametrize("version", [None, "frozen-causal-stock-market-inputs-v999"])
def test_market_values_with_missing_or_wrong_version_are_rejected(published, version):
    from ml.stock_trader.market_features import INDEPENDENT_MARKET_FEATURE_NAMES
    frame = _frame()
    frame["enrichment_feature_values_json"] = json.dumps({name: 1.0 for name in INDEPENDENT_MARKET_FEATURE_NAMES})
    if version is not None:
        frame["enrichment_feature_contract"] = version
    _publish(published, frame)
    with pytest.raises(ValueError, match="contract version"):
        load_current_independent_gameplan_signals(published, as_of="2026-09-08T11:01:00Z")


@pytest.mark.parametrize("as_of", ["2026-09-08T10:59:59Z", "2026-09-08T11:05:00Z", "2026-09-09T00:00:00Z"])
def test_no_early_expired_or_close_entries(published, as_of):
    assert load_current_independent_gameplan_signals(published, as_of=as_of)[0] == {}


def test_thirteen_hour_transition_uses_existing_ten_minute_grace(published):
    assert len(load_current_independent_gameplan_signals(published, as_of="2026-09-08T20:06:00Z")[0]) == 7
    assert load_current_independent_gameplan_signals(published, as_of="2026-09-08T20:10:00Z")[0] == {}


def test_only_own_promoted_and_directional_signal_is_selected(published):
    frame = _frame()
    frame.loc[frame.symbol.eq("COST") & frame.model_group.eq("4h"), "model_status"] = "RESEARCH_NOT_PROMOTED"
    frame.loc[frame.symbol.eq("COST") & frame.model_group.eq("1d"), ["direction", "calibrated_probability"]] = ["NO_EDGE", 0.5]
    _publish(published, frame)
    selected, _ = load_current_independent_gameplan_signals(published, as_of="2026-09-08T11:01:00Z")
    assert ("COST", "1h") in selected
    assert ("COST", "1w") in selected
    assert ("COST", "4h") not in selected
    assert ("COST", "1d") not in selected


@pytest.mark.parametrize("fault", ["wrong_end", "wrong_role", "wrong_action", "future_info", "bad_probability", "mismatched_direction", "duplicate", "legacy_row", "missing_route"])
def test_malformed_frozen_grid_fails_closed(published, fault):
    frame = _frame()
    index = frame.index[frame.symbol.eq("COST") & frame.route.eq("1h@04:00")][0]
    if fault == "wrong_end":
        frame.loc[index, "target_window_end"] += pd.Timedelta(hours=1)
    elif fault == "wrong_role":
        frame.loc[index, "target_role"] = "OUTLOOK"
    elif fault == "wrong_action":
        frame.loc[index, "action_anchor_local"] = "05:00"
    elif fault == "future_info":
        frame.loc[index, "information_available_at"] = pd.Timestamp("2026-09-08T12:00:00Z")
    elif fault == "bad_probability":
        frame.loc[index, "calibrated_probability"] = float("nan")
    elif fault == "mismatched_direction":
        frame.loc[index, "calibrated_probability"] = 0.49
    elif fault == "duplicate":
        frame.loc[index, "id"] = frame.iloc[0].id
    elif fault == "legacy_row":
        frame.loc[index, "target_contract_version"] = "overnight-path-targets-v2"
    else:
        frame = frame.drop(index=index)
    _publish(published, frame)
    with pytest.raises(ValueError):
        load_current_independent_gameplan_signals(published, as_of="2026-09-08T11:01:00Z")


def test_legacy_publication_and_checksum_tampering_fail_closed(published):
    _publish(published, _frame(), target_contract="overnight-path-targets-v2")
    with pytest.raises(ValueError, match="legacy windows"):
        load_current_independent_gameplan_signals(published, as_of="2026-09-08T11:01:00Z")
    run = _publish(published, _frame())
    (run / "gameplan.json").write_text("{}", encoding="utf-8")
    with pytest.raises(RuntimeError, match="size mismatch|checksum mismatch"):
        load_current_independent_gameplan_signals(published, as_of="2026-09-08T11:01:00Z")


def _training_pairs():
    return [{
        "decision_id": f"pair-{index}",
        "symbol": "COST",
        "model": {"feature_values": {name: 0.01 * (index + 1) for name in ENRICHMENT_FEATURE_NAMES}},
        "market_reality": {"status": "EVALUATED", "direction_aligned_net_return": 0.01 if index % 2 else -0.02, "direction_aligned_raw_return": 0.011 if index % 2 else -0.019},
    } for index in range(40)]


def test_existing_hourly_enrichment_rejects_every_independent_target_even_hourly(published):
    payload, report = fit_enrichment_model_payload(_training_pairs(), trained_at=FROZEN_AT)
    model = model_from_payload(payload)
    signals, _ = load_current_independent_gameplan_signals(published, as_of="2026-09-08T11:01:00Z")
    for horizon in ("1h", "4h", "1d", "1w"):
        signal = signals[("COST", horizon)]
        assert enrichment_signal_readiness(model, signal)["reason"] == "ENRICHMENT_INDEPENDENT_TARGET_CONTRACT_NOT_QUALIFIED"
        with pytest.raises(ValueError, match="no independent-target qualification"):
            require_enrichment_signal_support(model, signal)
    assert report["supported_horizons"] == ["1h"]
    legacy = replace(signals[("COST", "1h")], target_definition_version="overnight-path-targets-v2")
    require_enrichment_signal_support(model, legacy)
    with pytest.raises(ValueError, match="TARGET_DURATION_NOT_QUALIFIED"):
        require_enrichment_signal_support(model, replace(legacy, target_window_end="2026-09-08T15:00:00Z"))


def test_hourly_training_excludes_explicit_independent_and_longer_outcomes():
    pairs = _training_pairs()
    pairs[0]["prediction_horizon"] = "1w"
    pairs[1]["target_definition_version"] = STOCK_TARGET_CONTRACT_VERSION
    pairs[2].update(target_window_start="2026-09-08T11:00:00Z", target_window_end="2026-09-08T15:00:00Z")
    payload, report = fit_enrichment_model_payload(pairs, trained_at=FROZEN_AT, minimum_rows=37)
    assert report["row_count"] == 37
    assert report["excluded_row_count"] == 3
    payload["training"]["supported_horizons"] = ["1h", "1w"]
    payload.pop("model_fingerprint")
    payload["model_fingerprint"] = canonical_sha256(payload)
    with pytest.raises(ValueError, match="unsupported horizon qualification"):
        model_from_payload(payload)
