"""Causal and provenance checks for explicit research recipes, not live policy."""
from dataclasses import FrozenInstanceError, asdict, replace
import json
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from ml.intraday.prediction import ContractError, STEP, TARGET_VERSION, digest, encoded
from ml.intraday.technical_sizing import (
    AvailableInput, HourlyReference, IndicatorSpec, ScoreRecipe, ScoreTerm,
    SymbolSizingPolicy, WindowPolicy, evaluate_size, indicator_snapshot,
)


def bars(count=32, *, symbol="AAPL", regime="up"):
    x = np.arange(count, dtype=float)
    close = np.full(count, 100.) if regime == "flat" else 100. * (1.01 if regime == "up" else .99) ** x
    opened = pd.date_range("2026-10-08T08:00:00Z", periods=count, freq="15min")
    previous = np.r_[close[0], close[:-1]]
    return pd.DataFrame({"symbol": symbol, "opened_at": opened,
        "available_at": opened + pd.Timedelta(minutes=15, seconds=2), "availability_kind": "actual",
        "open": previous, "high": np.maximum(previous, close) + 1, "low": np.minimum(previous, close) - 1,
        "close": close, "volume": 100 + x,
        "source_id": "databento:EQUS.MINI:ohlcv-1m", "source_dataset": "EQUS.MINI",
        "session_id": "2026-10-08", "session_open": "2026-10-08T08:00:00+00:00",
        "session_close": "2026-10-09T00:00:00+00:00", "synthetic": False, "no_trade": False,
        "synthetic_open": False, "synthetic_close": False, "observed_constituents": 15,
        "synthetic_constituents": 0, "evidence_json": '{"verified_source":"fixture-only"}'})


def specs():
    # These are deliberately small, explicit fixture periods, not defaults.
    return tuple(IndicatorSpec(name, kind, params, derivatives=True) for name, kind, params in (
        ("ema", "ema", {"span": 4, "adjust": False}), ("sma", "sma", {"window": 5}),
        ("rsi", "rsi", {"window": 3, "smoothing": "simple"}),
        ("bb", "bollinger", {"window": 5, "deviations": 2., "ddof": 0}),
        ("stoch", "stochastic", {"window": 3, "k_smoothing": 2, "d_smoothing": 2}),
        ("momentum", "momentum", {"lag": 2}), ("vwap", "vwap", {"window": 4}),
        ("atr", "atr", {"window": 3, "smoothing": "simple"}),
        ("volume", "relative_volume", {"window": 4, "baseline": "previous"})))


def recipe(*, score=.36, feature="probability_not_down", weight=0., version="fixture-score-v1"):
    return ScoreRecipe(version, (ScoreTerm(feature, 0., 1., -10., 10., weight),), score, 0., .99)


def policy(symbol="AAPL", *, buy=None, sell=None, indicators=None, window=None):
    return SymbolSizingPolicy(symbol, "fixture-symbol-policy-v1", indicators or specs(),
        window or WindowPolicy("observed_history"), buy or recipe(), sell or recipe(score=.16))


def fixture(frame=None, *, probability=.72, config=None, reference=None, cutoff=None):
    frame = bars() if frame is None else frame
    config = policy(str(frame.symbol.iloc[0])) if config is None else config
    reference = pd.Timestamp(frame.opened_at.iloc[-1]) if reference is None else pd.Timestamp(reference)
    cutoff = reference + pd.Timedelta(minutes=15, seconds=3) if cutoff is None else pd.Timestamp(cutoff)
    frozen_rows = json.loads(frame.to_json(orient="records", date_format="iso"))
    # Runtime persistence freezes JSON precision. Calculations must consume those
    # exact records, not subtly different raw floats/receipt microseconds.
    frame = pd.DataFrame(frozen_rows)
    frame["opened_at"] = pd.to_datetime(frame.opened_at, utc=True)
    frame["available_at"] = pd.to_datetime(frame.available_at, utc=True)
    snapshot = {"cutoff": cutoff.isoformat(), "inference": {"symbol": str(frame.symbol.iloc[0]),
        "reference_at": reference.isoformat()}, "candles": frozen_rows, "manifest": {"fixture": True}}
    frozen_hash = digest(snapshot)
    forecast = {"symbol": str(frame.symbol.iloc[0]), "reference_at": reference.isoformat(),
        "reference_open": str(frame.loc[frame.opened_at.eq(reference), "open"].iloc[0]),
        "data_through": (reference + STEP).isoformat(), "source_id": str(frame.source_id.iloc[-1]),
        "source_dataset": str(frame.source_dataset.iloc[-1]),
        "target_open": (reference + 2 * STEP).isoformat(), "target_close": (reference + 3 * STEP).isoformat(),
        "training_cutoff": cutoff.isoformat(), "issued_at": cutoff.isoformat(),
        "target_version": TARGET_VERSION, "probability_not_down": probability, "snapshot_hash": frozen_hash}
    forecast["forecast_id"] = digest(forecast)
    indicators = indicator_snapshot(frame, symbol=config.symbol, reference_at=reference, data_cutoff=cutoff,
        indicator_specs=config.indicators, window_policy=config.window,
        forecast_snapshot=snapshot, forecast_snapshot_hash=frozen_hash)
    side = "BUY" if probability >= .5 else "SELL"
    baseline = HourlyReference(config.symbol, side, 10, "fixture-hourly-scale-v1", cutoff.isoformat(),
                              {"method": "declared_fixture", "hourly_order_due": False})
    return forecast, indicators, config, baseline, snapshot, frame


def size_fixture(bundle, **overrides):
    forecast, indicators, config, baseline, _, _ = bundle
    arguments = {"symbol_policy": config, "hourly_reference": baseline,
                 "calculated_at": forecast["issued_at"]}
    arguments.update(overrides)
    return evaluate_size(forecast, indicators, **arguments)


def test_known_indicators_and_explicit_derivatives():
    _, indicators, _, _, _, frame = fixture()
    features = indicators.payload()["features"]
    np.testing.assert_allclose(features["ema.level"], frame.close.ewm(span=4, adjust=False, min_periods=4).mean().iloc[-1])
    np.testing.assert_allclose(features["sma.level"], frame.close.tail(5).mean())
    np.testing.assert_allclose(features["momentum.value"], 1.01 ** 2 - 1)
    assert features["rsi.value"] == 100
    recent = frame.tail(5).close
    np.testing.assert_allclose(features["bb.width"], 4 * recent.std(ddof=0) / recent.mean())
    raw_k = (frame.close - frame.low.rolling(3).min()) / (frame.high.rolling(3).max() - frame.low.rolling(3).min())
    np.testing.assert_allclose(features["stoch.k"], raw_k.rolling(2).mean().iloc[-1])
    np.testing.assert_allclose(features["stoch.d"], raw_k.rolling(2).mean().rolling(2).mean().iloc[-1])
    typical = (frame.high + frame.low + frame.close) / 3
    np.testing.assert_allclose(features["vwap.level"], (typical * frame.volume).tail(4).sum() / frame.volume.tail(4).sum())
    np.testing.assert_allclose(features["volume.value"], frame.volume.iloc[-1] / frame.volume.iloc[-5:-1].mean())
    np.testing.assert_allclose(features["sma.level.rate"], frame.close.rolling(5).mean().diff().iloc[-1])
    np.testing.assert_allclose(features["sma.level.acceleration"], frame.close.rolling(5).mean().diff().diff().iloc[-1])


@pytest.mark.parametrize("regime,expected", [("up", 100), ("down", 0), ("flat", 50)])
@pytest.mark.parametrize("smoothing", ["simple", "ewm_first"])
def test_rsi_definitions_have_known_limits(regime, expected, smoothing):
    config = policy(indicators=(IndicatorSpec("rsi", "rsi", {"window": 3, "smoothing": smoothing}),))
    assert fixture(bars(regime=regime), config=config)[1].payload()["features"]["rsi.value"] == expected


def test_future_and_late_available_rows_cannot_change_frozen_indicators_or_size():
    all_rows = bars(count=40)
    bundle = fixture(all_rows.iloc[:30].copy())
    forecast, original, config, baseline, snapshot, _ = bundle
    changed = pd.concat([pd.DataFrame(snapshot["candles"]), all_rows.iloc[30:]], ignore_index=True)
    changed.loc[30:, ["open", "high", "low", "close"]] *= 3
    changed.loc[30:, "volume"] = -999  # Even invalid future OHLCV is not an admitted input.
    late_revision = changed.iloc[[4]].copy()
    late_revision.loc[:, "close"] = late_revision.high
    late_revision.loc[:, "available_at"] = pd.Timestamp(forecast["issued_at"]) + pd.Timedelta(seconds=10)
    changed = pd.concat([changed, late_revision], ignore_index=True)
    after = indicator_snapshot(changed, symbol=config.symbol, reference_at=forecast["reference_at"],
        data_cutoff=forecast["training_cutoff"], indicator_specs=config.indicators, window_policy=config.window,
        forecast_snapshot=snapshot, forecast_snapshot_hash=forecast["snapshot_hash"])
    assert after == original
    assert size_fixture(bundle).payload() == evaluate_size(forecast, after, symbol_policy=config,
        hourly_reference=baseline, calculated_at=forecast["issued_at"]).payload()


def test_original_revision_and_snapshot_hash_are_enforced():
    forecast, _, config, _, snapshot, frame = fixture()
    changed = frame.copy(deep=True)
    changed.loc[4, "volume"] += 1
    kwargs = dict(symbol=config.symbol, reference_at=forecast["reference_at"], data_cutoff=forecast["training_cutoff"],
        indicator_specs=config.indicators, window_policy=config.window, forecast_snapshot=snapshot,
        forecast_snapshot_hash=forecast["snapshot_hash"])
    with pytest.raises(ContractError, match="differ from"):
        indicator_snapshot(changed, **kwargs)
    snapshot["candles"][4]["volume"] += 1
    with pytest.raises(ContractError, match="hash changed"):
        indicator_snapshot(frame, **kwargs)


def test_price_scale_changes_absolute_levels_but_not_dimensionless_score():
    config = policy(buy=recipe(score=.2, feature="ema.distance", weight=2.))
    original = fixture(config=config)
    doubled = bars()
    doubled[["open", "high", "low", "close"]] *= 2
    second = fixture(doubled, config=config)
    before, after = original[1].payload()["features"], second[1].payload()["features"]
    for feature in ("ema.distance", "sma.distance", "rsi.value", "bb.width", "bb.percent_b", "stoch.k", "atr.relative"):
        np.testing.assert_allclose(before[feature], after[feature])
    np.testing.assert_allclose(after["ema.level"], 2 * before["ema.level"])
    assert size_fixture(original).desired_quantity == size_fixture(second).desired_quantity


def test_each_symbol_and_side_has_independent_recipe_and_periods():
    first = fixture()
    ionq_config = policy("IONQ", indicators=(IndicatorSpec("ema", "ema", {"span": 8, "adjust": False}),),
                         buy=recipe(score=.76), sell=recipe(score=.06))
    second = fixture(bars(symbol="IONQ"), config=ionq_config)
    assert size_fixture(first).desired_quantity == 4
    assert size_fixture(second).desired_quantity == 8
    assert first[1].payload()["features"]["ema.distance"] != second[1].payload()["features"]["ema.distance"]
    assert size_fixture(fixture(bars(symbol="IONQ"), probability=.49, config=ionq_config)).desired_quantity == 1
    assert size_fixture(fixture(probability=.50)).payload()["side"] == "BUY"
    assert size_fixture(first, symbol_policy=ionq_config).desired_quantity is None


def test_nearest_shares_and_real_zero_do_not_depend_on_hourly_order_being_due():
    assert size_fixture(fixture()).desired_quantity == 4  # 3.6, not floor 3.
    zero = size_fixture(fixture(config=policy(buy=recipe(score=.04))))
    assert zero.status == "sized" and zero.desired_quantity == 0
    assert zero.adjusted_quantity is None
    assert zero.payload()["execution_disposition"] == "pending_capacity"
    assert zero.payload()["hourly_reference"]["evidence"]["hourly_order_due"] is False


def test_half_share_and_hourly_equality_require_explicit_decisions():
    bundle = fixture(config=policy(buy=recipe(score=.25)))
    assert size_fixture(bundle).desired_quantity is None
    assert size_fixture(bundle, half_share_ties="half_even").desired_quantity == 2
    assert size_fixture(bundle, half_share_ties="half_up").desired_quantity == 3
    bundle = fixture(config=policy(buy=recipe(score=.6)))
    baseline = replace(bundle[3], quantity=1)
    assert size_fixture(bundle, hourly_reference=baseline).desired_quantity is None
    assert size_fixture(bundle, hourly_reference=baseline, rounded_equality="allow").desired_quantity == 1


def test_decimal_config_arithmetic_does_not_silently_bypass_half_share_ties():
    bundle = fixture(probability=1., config=policy(buy=recipe(score=.7, weight=-.45)))
    result = size_fixture(bundle)
    assert result.desired_quantity is None
    assert float(result.payload()["score"]) == .25
    assert size_fixture(bundle, half_share_ties="half_even").desired_quantity == 2
    assert size_fixture(bundle, half_share_ties="half_up").desired_quantity == 3


def test_absent_recipe_input_and_reference_yield_null_not_zero():
    bundle = fixture()
    assert size_fixture(bundle, symbol_policy=None).desired_quantity is None
    assert size_fixture(bundle, hourly_reference=None).desired_quantity is None
    assert size_fixture(bundle, symbol_policy=replace(bundle[2], buy=None)).payload()["reason"] == "side_score_recipe_missing"
    bundle = fixture(config=policy(buy=recipe(feature="holdings", weight=.1)))
    result = size_fixture(bundle)
    assert result.status == "unavailable" and result.desired_quantity is None
    assert result.payload()["reason"] == "sizing_feature_unavailable: holdings"


def test_account_availability_evidence_and_contributions_are_recorded():
    bundle = fixture(config=policy(buy=ScoreRecipe("fixture-account-v1",
        (ScoreTerm("holdings", 10., 5., -1., 1., -.1), ScoreTerm("probability_not_down", .5, .2, -1., 1., .1)),
        .3, 0., .9)))
    at = bundle[0]["issued_at"]
    item = AvailableInput(20., at, "fixture-account-snapshot", {"snapshot_hash": "fixture-only"})
    result = size_fixture(bundle, account_inputs={"holdings": item}).payload()
    assert result["desired_quantity"] == 3
    terms = result["term_contributions"]
    assert terms[0]["normalized"] == 2 and terms[0]["clipped"] == 1
    assert terms[0]["contribution"] == -.1
    assert result["account_inputs"]["holdings"]["evidence"] == item.evidence
    future = replace(item, available_at=(pd.Timestamp(at) + pd.Timedelta(seconds=1)).isoformat())
    assert size_fixture(bundle, account_inputs={"holdings": future}).desired_quantity is None
    assert size_fixture(bundle, hourly_reference=replace(bundle[3], available_at=future.available_at)).desired_quantity is None
    with pytest.raises(ContractError, match="cannot replace"):
        size_fixture(bundle, account_inputs={"probability_not_down": item})


def test_confirmed_synthetic_provenance_survives_snapshot_and_immutable_copies():
    frame = bars()
    prior = frame.close.iloc[-2]
    frame.loc[len(frame)-1, ["open", "high", "low", "close"]] = prior
    frame.loc[len(frame)-1, "volume"] = 0
    frame.loc[len(frame)-1, ["synthetic", "no_trade", "synthetic_open", "synthetic_close"]] = True
    frame.loc[len(frame)-1, ["observed_constituents", "synthetic_constituents"]] = [0, 15]
    evidence = {"covered": True, "successful_empty_attempts": ["a", "b"],
                "prior_price": prior, "prior_opened_at": frame.opened_at.iloc[-2].isoformat()}
    frame.loc[len(frame)-1, "evidence_json"] = encoded(evidence)
    bundle = fixture(frame)
    snapshot = bundle[1].payload()
    last = snapshot["candles"][-1]
    assert last["synthetic"] is True and last["volume"] == 0 and last["synthetic_constituents"] == 15
    assert json.loads(last["evidence_json"]) == evidence
    assert snapshot["provenance"]["synthetic_rows"] == 1
    original_id = bundle[1].snapshot_id
    snapshot["candles"][-1]["close"] = 1
    assert bundle[1].snapshot_id == original_id
    assert bundle[1].payload()["candles"][-1]["close"] == bundle[5].close.iloc[-1]
    result = size_fixture(bundle)
    altered = result.payload()
    altered["desired_quantity"] = 900
    assert result.desired_quantity == 4
    with pytest.raises(FrozenInstanceError):
        result.payload_json = "{}"


def test_zero_volume_and_flat_band_remain_undefined_not_fabricated_score():
    frame = bars(regime="flat")
    frame[["open", "high", "low", "close"]] = 100.
    frame.volume = 0.
    for feature in ("vwap.distance", "bb.percent_b", "stoch.k", "volume.value"):
        bundle = fixture(frame, config=policy(buy=recipe(feature=feature, weight=.1)))
        assert bundle[1].payload()["features"][feature] is None
        assert size_fixture(bundle).status == "unavailable"
        assert size_fixture(bundle).desired_quantity is None
    assert fixture(frame)[1].payload()["features"]["bb.width"] == 0


def test_gaps_are_explicit_and_contiguous_session_requires_new_warmup():
    frame = bars().drop(index=29).reset_index(drop=True)
    observed = fixture(frame)
    assert observed[1].payload()["provenance"]["gaps_or_closures"] == 1
    assert len(observed[1].payload()["candles"]) == len(frame)
    contiguous = fixture(frame, config=policy(window=WindowPolicy("contiguous_session"),
                                             buy=recipe(feature="ema.distance", weight=.1)))
    assert len(contiguous[1].payload()["candles"]) == 2
    assert size_fixture(contiguous).desired_quantity is None
    assert contiguous[1].payload()["provenance"]["excluded_window_rows"] == len(frame) - 2


def test_config_json_roundtrip_and_versions_change_evidence_not_old_results():
    bundle = fixture()
    configured = SymbolSizingPolicy.from_payload(json.loads(encoded(asdict(bundle[2]))))
    assert configured == bundle[2]
    original = size_fixture(bundle)
    changed = size_fixture(bundle, symbol_policy=replace(configured, buy=recipe(version="fixture-score-v2")))
    assert changed.result_id != original.result_id
    assert changed.payload()["recipe_hash"] != original.payload()["recipe_hash"]
    changed_baseline = size_fixture(bundle, hourly_reference=replace(bundle[3], version="fixture-reference-v2"))
    assert changed_baseline.result_id != original.result_id
    assert original.payload()["recipe_version"] == "fixture-score-v1"


def test_late_sizing_retains_fixed_target_and_original_reference():
    bundle = fixture()
    later = (pd.Timestamp(bundle[0]["target_close"]) + pd.Timedelta(minutes=3)).isoformat()
    result = size_fixture(bundle, calculated_at=later).payload()
    assert result["target_close"] == bundle[0]["target_close"]
    assert result["reference_at"] == bundle[0]["reference_at"]
    assert result["calculated_at"] == later and result["orders_enabled"] is False
    assert result["calculated_after_target"] is True
    altered = dict(bundle[0], probability_not_down=.9)
    with pytest.raises(ContractError, match="immutable forecast"):
        evaluate_size(altered, bundle[1], symbol_policy=bundle[2], hourly_reference=bundle[3], calculated_at=later)


def test_forecast_reference_price_and_source_must_match_original_inputs():
    bundle = fixture()
    for field, replacement in (("reference_open", "99"), ("source_id", "another-source")):
        forecast = {**bundle[0], field: replacement}
        forecast.pop("forecast_id")
        forecast["forecast_id"] = digest(forecast)
        with pytest.raises(ContractError, match="reference price or source"):
            evaluate_size(forecast, bundle[1], calculated_at=forecast["issued_at"])


def test_raw_price_precision_is_not_substituted_for_frozen_runtime_bytes():
    bundle = fixture()
    with pytest.raises(ContractError, match="differ from"):
        indicator_snapshot(bars(), symbol="AAPL", reference_at=bundle[0]["reference_at"],
            data_cutoff=bundle[0]["training_cutoff"], indicator_specs=bundle[2].indicators,
            window_policy=bundle[2].window, forecast_snapshot=bundle[4], forecast_snapshot_hash=bundle[0]["snapshot_hash"])


def test_runtime_unrounded_reference_anchor_is_preserved_with_json_candle_precision():
    bundle = fixture()
    original = bundle[4]
    raw_reference = bars().open.iloc[-1]
    assert raw_reference != bundle[5].open.iloc[-1]
    original["inference"].update(reference_open=raw_reference, source=bundle[0]["source_id"],
                                 source_dataset=bundle[0]["source_dataset"])
    forecast = {**bundle[0], "reference_open": str(raw_reference), "snapshot_hash": digest(original)}
    forecast.pop("forecast_id")
    forecast["forecast_id"] = digest(forecast)
    snapshot = indicator_snapshot(original["candles"], symbol="AAPL", reference_at=forecast["reference_at"],
        data_cutoff=forecast["training_cutoff"], indicator_specs=bundle[2].indicators, window_policy=bundle[2].window,
        forecast_snapshot=original, forecast_snapshot_hash=forecast["snapshot_hash"])
    result = evaluate_size(forecast, snapshot, symbol_policy=bundle[2], hourly_reference=bundle[3],
                           calculated_at=forecast["issued_at"])
    assert result.status == "sized"
    assert snapshot.payload()["reference_open"] == str(raw_reference)
    assert snapshot.payload()["reference_price_basis"] == "frozen_inference"


@pytest.mark.parametrize("term", [
    ScoreTerm("probability_not_down", "0", 1., 0., 1., 1.),
    ScoreTerm("probability_not_down", 0., True, 0., 1., 1.),
    ScoreTerm("probability_not_down", 0., 1., 0., 1., float("nan")),
])
def test_json_recipe_coefficients_require_genuine_finite_numeric_types(term):
    with pytest.raises(ContractError):
        term.validate()
    with pytest.raises(ContractError):
        IndicatorSpec("bb", "bollinger", {"window": 5, "deviations": "2", "ddof": 0}).validate()


@pytest.mark.parametrize("spec", [
    IndicatorSpec("bad", "ema", {"span": 0, "adjust": False}),
    IndicatorSpec("bad", "bollinger", {"window": 3, "deviations": 2, "ddof": 2}),
    IndicatorSpec("bad", "unknown", {}),
])
def test_no_implicit_indicator_parameters_or_unsupported_fallback(spec):
    with pytest.raises(ContractError):
        spec.validate()
    with pytest.raises(ContractError):
        replace(recipe(), output_max=1.).validate()


def test_module_contains_no_account_provider_or_order_entry_points():
    # This is a pure research result; even valid BUY sizing has no submission path.
    result = size_fixture(fixture()).payload()
    assert result["adjusted_quantity"] is None and result["orders_enabled"] is False
    assert result["execution_disposition"] == "pending_capacity"
    import ast
    from ml.intraday import technical_sizing
    module = ast.parse(Path(technical_sizing.__file__).read_text(encoding="utf-8"))
    imports = [node.module or "" for node in ast.walk(module) if isinstance(node, ast.ImportFrom)]
    assert all(not any(part in item for part in ("broker", "schwab", "providers", "execution", "stock_trader")) for item in imports)
