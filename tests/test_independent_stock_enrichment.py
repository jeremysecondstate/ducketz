"""Exact independent enrichment: synthetic prices only, never providers/orders."""
from __future__ import annotations

from copy import deepcopy
from dataclasses import replace
import json

import numpy as np
import pandas as pd
import pytest

from ml.independent_stock_targets import GROUPS, STOCK_TARGET_CONTRACT_VERSION, _calendar, stock_target_windows
from ml.stock_trader import independent_training as training
from ml.stock_trader.contracts import STOCK_TRADER_SYMBOLS, PredictionSignal, canonical_sha256
from ml.stock_trader.model import (
    INDEPENDENT_ENRICHMENT_SCHEMA_VERSION, IndependentEnrichmentModel,
    enrichment_signal_readiness, independent_target_feature_values, load_current_enrichment_model,
    model_from_payload,
)
from ml.stock_trader.market_features import INDEPENDENT_MARKET_FEATURE_NAMES


TRAINED = pd.Timestamp("2026-09-08T08:00:00Z")


def _market_values(day, symbol):
    weekday = pd.Timestamp(day).dayofweek
    symbol_index = STOCK_TRADER_SYMBOLS.index(symbol)
    return {name: float(.05 * index + (1 + index * .1) * np.sin(2 * np.pi * weekday / 7) + .01 * symbol_index)
            for index, name in enumerate(INDEPENDENT_MARKET_FEATURE_NAMES)}


@pytest.fixture(scope="module")
def cohorts():
    calendar = _calendar(pd.Timestamp("2026-01-05").date(), pd.Timestamp("2026-09-08").date())
    sessions = calendar.sessions_in_range("2026-01-05", "2026-07-31")[:120]
    rows = {group: [] for group in GROUPS}
    for session in sessions:
        for spec in stock_target_windows(session.date(), calendar=calendar):
            if not spec["execution_eligible"] or spec["route"] not in {"1h@04:00", "4h@04:00", "4h@16:00", "1d@D+1", "1w@D+5"}:
                continue
            start, end = spec["target_window_start"], spec["target_window_end"]
            for index, symbol in enumerate(STOCK_TRADER_SYMBOLS):
                raw = (.002 if index % 2 else -.002) + .018 * np.sin(2 * np.pi * session.dayofweek / 7)
                rows[spec["model_group"]].append({**spec, **_market_values(session, symbol), "symbol": symbol, "action_date": session.date(),
                    "decision_timestamp": pd.Timestamp(session.date(), tz="America/Los_Angeles") + pd.Timedelta(hours=2),
                    "information_available_at": pd.Timestamp(session.date(), tz="America/Los_Angeles") + pd.Timedelta(hours=1),
                    "observed_open_timestamp": start, "observed_close_timestamp": end,
                    "target_open": 100., "target_close": 100. * (1 + raw), "observed_return": raw,
                    "assumed_round_trip_cost": .001, "target_boundary_aligned": True,
                    "target_price_source_contract": "xnas-itch-archive-v1", "target_price_dataset": "XNAS.ITCH"})
    return {group: pd.DataFrame(values) for group, values in rows.items()}


@pytest.fixture(scope="module")
def fitted(cohorts):
    return training.fit_independent_enrichment_model_payload(cohorts, trained_at=TRAINED, source_fingerprint="f" * 64)


def _signal(group="1h", *, symbol="AMZN", hour=4, day="2026-09-08"):
    spec = next(item for item in stock_target_windows(pd.Timestamp(day).date())
                if item["model_group"] == group and item["execution_eligible"]
                and item["target_window_start"].tz_convert("America/Los_Angeles").hour == hour)
    return PredictionSignal(symbol, group, "fixture", (spec["target_window_start"] - pd.Timedelta(hours=2)).isoformat(),
        spec["target_window_start"].isoformat(), spec["target_window_end"].isoformat(),
        (spec["target_window_start"] + pd.Timedelta(minutes=5)).isoformat(),
        (spec["target_window_start"] - pd.Timedelta(hours=1)).isoformat(), .6, .001, {group: .6},
        "directional", "v1", "s" * 64, target_definition_version=STOCK_TARGET_CONTRACT_VERSION,
        target_price_source_contract="xnas-itch-archive-v1", enrichment_feature_values=_market_values(day, symbol))


def _resign(payload):
    payload["model_fingerprint"] = canonical_sha256({key: value for key, value in payload.items() if key != "model_fingerprint"})
    return payload


def test_four_real_separate_horizon_fits_and_heldout_scope_evidence(fitted):
    payload, report = fitted
    assert payload["schema_version"] == INDEPENDENT_ENRICHMENT_SCHEMA_VERSION
    assert all(payload["horizons"][group]["fitted"] for group in GROUPS)
    assert report["status"] == "MODELS_FIT"
    model = model_from_payload(payload)
    assert isinstance(model, IndependentEnrichmentModel)
    assert set(model.horizon_models) == set(GROUPS)
    assert model.scope_readiness
    assert set(model.supported_horizons) >= {"1h", "4h", "1d"}
    for group, record in payload["horizons"].items():
        assert record["assessment_rows"]
        assert record["magnitude_scale_training_only"] > 0
        for left, right in zip(training._PARTITIONS, training._PARTITIONS[1:]):
            assert pd.Timestamp(record["partition_evidence"][left]["last_target_end"]) < pd.Timestamp(record["partition_evidence"][right]["first_decision"])
    assert "fixed_conservative_policy" in payload["execution_head_basis"]


@pytest.mark.parametrize("group,hour", [("1h", 4), ("4h", 4), ("4h", 16), ("1d", 4), ("1w", 4)])
def test_prediction_holding_is_exact_expiry_and_not_hourly_proxy(fitted, group, hour):
    model = model_from_payload(fitted[0])
    signal = _signal(group, hour=hour)
    features = independent_target_feature_values(symbol=signal.symbol, horizon=group,
        start=signal.target_window_start, end=signal.target_window_end, cost=.001)
    features.update(signal.enrichment_feature_values)
    result = model.predict(features)
    assert result.expected_holding_minutes == (pd.Timestamp(signal.target_window_end) - pd.Timestamp(signal.target_window_start)).total_seconds() / 60
    if group != "1h":
        assert result.expected_holding_minutes > 60


def test_support_uses_symbol_route_duration_and_exact_contract(fitted):
    model = model_from_payload(fitted[0])
    known = enrichment_signal_readiness(model, _signal())
    assert known["scope"].endswith("1h@04:00|60m")
    assert known["evidence"]["fit_decision_clusters"] >= 20
    assert enrichment_signal_readiness(model, _signal(hour=5))["status"] == "NOT_READY"
    assert enrichment_signal_readiness(model, replace(_signal("1w"), target_window_end="2026-09-08T12:00:00+00:00"))["reason"] == "ENRICHMENT_EXACT_TARGET_WINDOW_INVALID"
    assert enrichment_signal_readiness(model, replace(_signal(), target_definition_version="legacy"))["status"] == "NOT_READY"
    assert enrichment_signal_readiness(model, _signal(symbol="AAPL"))["status"] == "READY"
    assert enrichment_signal_readiness(model, replace(_signal(symbol="AAPL"), target_price_source_contract="canonical-equity-minute-v1"))["reason"] == "ENRICHMENT_TARGET_PRICE_SOURCE_MISMATCH"
    assert enrichment_signal_readiness(model, replace(_signal(symbol="AAPL"), target_price_source_contract=""))["status"] == "NOT_READY"
    assert enrichment_signal_readiness(model, replace(_signal(symbol="AAPL"), enrichment_feature_values={}))["reason"] == "ENRICHMENT_MARKET_FEATURES_MISSING_OR_NONFINITE"
    invalid = dict(_signal().enrichment_feature_values)
    invalid[INDEPENDENT_MARKET_FEATURE_NAMES[0]] = float("nan")
    assert enrichment_signal_readiness(model, replace(_signal(), enrichment_feature_values=invalid))["status"] == "NOT_READY"


def test_metadata_only_legacy_claims_cannot_enable_independent_hourly():
    from types import SimpleNamespace
    impostor = SimpleNamespace(model_fingerprint="fake", supported_horizons=GROUPS,
                                qualified_target_contracts=(STOCK_TARGET_CONTRACT_VERSION,))
    assert enrichment_signal_readiness(impostor, _signal())["reason"] == "ENRICHMENT_INDEPENDENT_TARGET_CONTRACT_NOT_QUALIFIED"


@pytest.mark.parametrize("mutation,match", [
    ("future_feature", "future features"), ("future_outcome", "immature outcomes"),
    ("false_return", "observed target prices"), ("false_window", "exact clock contract"),
    ("duplicate", "repeats a natural target"), ("mixed_source", "mix or omit"),
    ("relabelled", "relabelled"), ("boundary", "boundary aligned"),
])
def test_training_rejects_causal_and_target_contract_corruption(cohorts, mutation, match):
    data = cohorts["1h"].copy()
    if mutation == "future_feature":
        data.loc[0, "information_available_at"] = data.loc[0, "target_window_end"]
    elif mutation == "future_outcome":
        return_value = data.target_window_end.min() - pd.Timedelta(seconds=1)
        with pytest.raises(ValueError, match=match):
            training._admit_targets(data, group="1h", trained_at=return_value)
        return
    elif mutation == "false_return":
        data.loc[0, "observed_return"] = .9
    elif mutation == "false_window":
        data.loc[0, "target_window_end"] += pd.Timedelta(hours=1)
    elif mutation == "duplicate":
        data = pd.concat([data, data.iloc[[0]]], ignore_index=True)
    elif mutation == "mixed_source":
        data.loc[0, "target_price_dataset"] = "EQUS.MINI"
    elif mutation == "relabelled":
        data.loc[0, "target_contract_version"] = "legacy-hourly"
    elif mutation == "boundary":
        data.loc[0, "observed_open_timestamp"] += pd.Timedelta(minutes=6)
    with pytest.raises(ValueError, match=match):
        training._admit_targets(data, group="1h", trained_at=TRAINED)


def test_missing_history_is_research_without_fake_fitted_heads(cohorts):
    small = {group: frame.iloc[:0].copy() for group, frame in cohorts.items()}
    payload, report = training.fit_independent_enrichment_model_payload(small, trained_at=TRAINED, source_fingerprint="a" * 64)
    model = model_from_payload(payload)
    assert report["status"] == "INSUFFICIENT_EVIDENCE"
    assert model.supported_horizons == model.qualified_target_contracts == ()
    assert model.horizon_models == {}


def test_assessment_cannot_change_fitted_heads_or_training_only_sizing_scale(cohorts, fitted):
    changed = {group: frame.copy() for group, frame in cohorts.items()}
    for group, frame in changed.items():
        cutoff = pd.Timestamp(fitted[0]["horizons"][group]["partition_evidence"]["assessment"]["first_decision"])
        mask = pd.to_datetime(frame.decision_timestamp, utc=True).ge(cutoff)
        frame.loc[mask, "observed_return"] *= .5
        frame.loc[mask, "target_close"] = frame.loc[mask, "target_open"] * (1 + frame.loc[mask, "observed_return"])
        frame.loc[mask, list(INDEPENDENT_MARKET_FEATURE_NAMES)] += 1000.
    payload, _ = training.fit_independent_enrichment_model_payload(changed, trained_at=TRAINED, source_fingerprint="b" * 64)
    for group in GROUPS:
        for key in ("heads", "feature_means", "feature_scales", "magnitude_scale_training_only", "probability_calibration", "head_training_objectives", "calibration_selection", "head_development_selection", "head_ridge_penalties"):
            assert payload["horizons"][group][key] == fitted[0]["horizons"][group][key]


def test_qualification_is_recomputed_not_accepted_from_metadata(fitted):
    payload = deepcopy(fitted[0])
    payload["supported_horizons"] = ["1h", "4h", "1d", "1w"]
    payload["qualified_target_contracts"] = [STOCK_TARGET_CONTRACT_VERSION]
    for record in payload["horizons"].values():
        record["fit_scope_decision_clusters"] = {scope: 1 for scope in record["fit_scope_decision_clusters"]}
        record["qualification_status"] = "READY"
    model = model_from_payload(_resign(payload))
    assert model.supported_horizons == model.qualified_target_contracts == ()
    assert all(value["status"] == "RESEARCH" for value in model.scope_readiness.values())


def test_loader_rejects_overlapping_fit_holdout_evidence(fitted):
    payload = deepcopy(fitted[0])
    record = payload["horizons"]["1w"]
    record["partition_evidence"]["train"]["last_target_end"] = record["partition_evidence"]["assessment"]["last_target_end"]
    with pytest.raises(ValueError, match="label leakage"):
        model_from_payload(_resign(payload))


def test_loader_rejects_fabricated_assessment_features(fitted):
    payload = deepcopy(fitted[0])
    payload["horizons"]["1w"]["assessment_rows"][0]["log_target_duration_minutes"] = np.log(60.)
    with pytest.raises(ValueError, match="not causal target features"):
        model_from_payload(_resign(payload))


def test_native_publication_staging_loading_and_source_checksum(tmp_path, monkeypatch, cohorts, fitted):
    from ml.artifacts import file_checksum, write_manifest
    from ml.nightly_gameplan import GAMEPLAN_RECEIPT_VERSION, EXECUTION_AUTHORITY
    run = tmp_path / "ml" / "nightly-gameplan-runs" / "fixture"
    run.mkdir(parents=True)
    paths = tuple(run / name for name in ["manifest.json", "receipt.json", *[f"training-cohort-{group}.parquet" for group in GROUPS]])
    for group, path in zip(GROUPS, paths[2:]):
        cohorts[group].to_parquet(path)
    write_manifest(run, run_timestamp=TRAINED, input_files=(), output_files=tuple(path.name for path in paths[2:]),
        configuration={"target_contract_version": STOCK_TARGET_CONTRACT_VERSION, "action_date": "2026-09-08"})
    receipt = {"schema_version": GAMEPLAN_RECEIPT_VERSION, "run_path": run.relative_to(tmp_path).as_posix(),
        "manifest_checksum_sha256": file_checksum(paths[0]), "execution_authority": EXECUTION_AUTHORITY,
        "broker_orders_enabled": False, "orders_placed": 0, "run_timestamp": TRAINED.isoformat(),
        "published_at": TRAINED.isoformat(), "action_date": "2026-09-08"}
    paths[1].write_text(json.dumps(receipt), encoding="utf-8")
    inventory = {path.relative_to(tmp_path).as_posix(): file_checksum(path) for path in paths}
    metadata = {"source_gameplan_run": run.relative_to(tmp_path).as_posix(), "source_files": inventory,
                "source_fingerprint": canonical_sha256(inventory)}
    monkeypatch.setattr(training, "load_current_training_cohorts", lambda root, **kwargs: (cohorts, paths, metadata))
    def fit(*args, **kwargs):
        payload, report = deepcopy(fitted)
        payload["source_fingerprint"] = kwargs["source_fingerprint"]
        return _resign(payload), report
    monkeypatch.setattr(training, "fit_independent_enrichment_model_payload", fit)
    staged = training.train_and_publish_independent_enrichment_model(tmp_path, trained_at=TRAINED, publish_current=False)
    assert (staged / "model.json").is_file()
    assert not (tmp_path / "ml" / "stock-trader-model-latest" / "run.json").exists()
    published = training.train_and_publish_independent_enrichment_model(tmp_path, trained_at=TRAINED, publish_current=True)
    model = load_current_enrichment_model(tmp_path)
    assert isinstance(model, IndependentEnrichmentModel)
    assert len(model.horizon_models) == 4
    native_payload = json.loads((published / "model.json").read_text(encoding="utf-8"))
    native_manifest = json.loads((published / "manifest.json").read_text(encoding="utf-8"))
    altered = deepcopy(native_payload)
    altered["horizons"]["1h"]["market_feature_admission"]["excluded_missing_market_rows"] += 1
    with pytest.raises(ValueError, match="market admission differs"):
        training.verify_independent_model_sources(tmp_path, _resign(altered), native_manifest)
    altered = deepcopy(native_payload)
    changed_scope = next(iter(altered["horizons"]["1h"]["fit_scope_decision_clusters"]))
    altered["horizons"]["1h"]["fit_scope_decision_clusters"][changed_scope] += 1
    with pytest.raises(ValueError, match="scope counts differ"):
        training.verify_independent_model_sources(tmp_path, _resign(altered), native_manifest)
    altered = deepcopy(native_payload)
    altered["horizons"]["1h"]["assessment_rows"][0]["observed_return"] += .1
    with pytest.raises(ValueError, match="assessment evidence differs"):
        training.verify_independent_model_sources(tmp_path, _resign(altered), native_manifest)
    altered = deepcopy(native_payload)
    altered["horizons"]["1h"]["assessment_rows"][0][INDEPENDENT_MARKET_FEATURE_NAMES[0]] += 1.
    with pytest.raises(ValueError, match="assessment evidence differs"):
        training.verify_independent_model_sources(tmp_path, _resign(altered), native_manifest)
    paths[-1].write_text("changed cohort", encoding="utf-8")
    with pytest.raises(ValueError, match="source cohort checksum"):
        load_current_enrichment_model(tmp_path)


def test_cli_stage_only_uses_native_lock_and_never_broker(monkeypatch, tmp_path, capsys):
    from contextlib import contextmanager
    calls = []
    @contextmanager
    def lock(path, *, process_name):
        calls.append((path, process_name))
        yield
    monkeypatch.setattr(training, "exclusive_runtime_lock", lock)
    monkeypatch.setattr(training, "train_and_publish_independent_enrichment_model",
        lambda root, **kwargs: calls.append((root, kwargs)) or tmp_path / "model-run")
    assert training.main(["--datastore", str(tmp_path), "--stage-only", "--gameplan-run", str(tmp_path / "pinned")]) == 0
    assert calls[0][0] == tmp_path / "locks" / "stock-trader-training.lock"
    assert calls[1][1]["publish_current"] is False
    assert calls[1][1]["gameplan_run"] == tmp_path / "pinned"
    assert json.loads(capsys.readouterr().out)["orders_placed"] == 0


def test_binomial_head_recovers_the_observed_probability_not_logit_label_average():
    labels = np.r_[np.ones(20), np.zeros(80)]
    head, report = training._fit_binomial_head(np.zeros((100, 3)), labels, penalty=5.)
    probability = float(training.expit(head["intercept"]))
    old_transformed_average = float(training.expit(np.mean(training._logit(.05 + .9 * labels))))
    assert probability == pytest.approx(.2, abs=1e-8)
    assert abs(old_transformed_average - .2) > .04
    assert head["coefficients"] == [0., 0., 0.]
    assert report["objective"] == "penalized_binomial_negative_log_likelihood"


def test_direct_downside_objective_recovers_arithmetic_mean_with_many_zero_losses():
    losses = np.r_[np.zeros(90), np.full(10, .1)]
    head, report = training._fit_arithmetic_downside_head(np.zeros((100, 3)), losses, penalty=5.)
    predicted = float(np.logaddexp(0, head["intercept"]))
    old_transformed_average = float(np.logaddexp(0, np.mean(training._inverse_softplus(np.maximum(losses, 1e-8)))))
    assert predicted == pytest.approx(.01, abs=1e-9)
    assert old_transformed_average < .000001
    assert report["training_downside_mse"] == pytest.approx(np.mean((losses - losses.mean()) ** 2))
    assert report["objective"] == "penalized_direct_arithmetic_downside_squared_error"


def test_direct_downside_head_learns_different_conditional_means_without_negative_predictions():
    z = np.r_[np.full(200, -1.), np.full(200, 1.)][:, None]
    losses = np.r_[np.tile([0., .002], 100), np.tile([0., .02], 100)]
    head, report = training._fit_arithmetic_downside_head(z, losses, penalty=1.)
    predicted = np.logaddexp(0, head["intercept"] + z[:, 0] * head["coefficients"][0])
    assert (predicted >= 0).all()
    assert predicted[:200].mean() == pytest.approx(.001, abs=.0005)
    assert predicted[200:].mean() == pytest.approx(.01, abs=.0005)
    assert report["training_downside_mse"] < np.mean((losses - losses.mean()) ** 2)


@pytest.mark.parametrize("bad", ["missing", "nonfinite"])
def test_new_market_cohort_contract_never_fills_missing_inputs_with_zero(cohorts, bad):
    data = cohorts["1h"].copy()
    name = INDEPENDENT_MARKET_FEATURE_NAMES[0]
    if bad == "missing":
        data = data.drop(columns=[name])
    else:
        data.loc[0, name] = np.inf
    with pytest.raises(ValueError, match="market features"):
        training._admit_targets(data, group="1h", trained_at=TRAINED)


def test_new_feature_contract_and_runtime_transport(fitted):
    from ml.stock_trader.contracts import PortfolioState, QuoteState
    from ml.stock_trader.model import build_feature_values
    signal = _signal()
    portfolio = PortfolioState(signal.target_window_start, 10000., 10000., 0., 0., {}, {}, {}, {}, 0, {}, "fixture")
    quote = QuoteState(signal.symbol, 100., 101., 100.5, 100.5, 1000., signal.target_window_start)
    features = build_feature_values(signal, portfolio, quote, as_of=signal.target_window_start)
    assert {name: features[name] for name in INDEPENDENT_MARKET_FEATURE_NAMES} == signal.enrichment_feature_values
    model = model_from_payload(fitted[0])
    assert tuple(model.horizon_models["1h"].feature_names) == tuple(training.FEATURES)
    assert model.predict(features).feature_values[INDEPENDENT_MARKET_FEATURE_NAMES[0]] == signal.enrichment_feature_values[INDEPENDENT_MARKET_FEATURE_NAMES[0]]
    features.pop(INDEPENDENT_MARKET_FEATURE_NAMES[0])
    with pytest.raises(ValueError, match="features are missing"):
        model.predict(features)


def test_legacy_calendar_feature_shape_remains_readable(fitted):
    from ml.stock_trader.model import INDEPENDENT_ENRICHMENT_FEATURE_NAMES
    legacy = deepcopy(fitted[0])
    legacy.pop("feature_contract_version")
    legacy.pop("market_feature_contract")
    legacy.pop("scope_qualification_policy")
    legacy.pop("market_feature_admission_policy")
    count = len(INDEPENDENT_ENRICHMENT_FEATURE_NAMES)
    for record in legacy["horizons"].values():
        record.pop("feature_contract_version")
        record.pop("market_feature_contract")
        record.pop("scope_qualification_policy")
        record.pop("market_feature_admission")
        record["feature_names"] = record["feature_names"][:count]
        record["feature_means"] = record["feature_means"][:count]
        record["feature_scales"] = record["feature_scales"][:count]
        for head in record["heads"].values():
            head["coefficients"] = head["coefficients"][:count]
        for row in record["assessment_rows"]:
            for name in INDEPENDENT_MARKET_FEATURE_NAMES:
                row.pop(name)
    model = model_from_payload(_resign(legacy))
    signal = replace(_signal(), enrichment_feature_values={})
    readiness = enrichment_signal_readiness(model, signal)
    assert readiness["required_market_feature_names"] == []
    assert readiness["reason"] != "ENRICHMENT_MARKET_FEATURES_MISSING_OR_NONFINITE"
    values = independent_target_feature_values(symbol=signal.symbol, horizon=signal.primary_horizon,
        start=signal.target_window_start, end=signal.target_window_end, cost=.001)
    assert model.predict(values).expected_holding_minutes == 60.


def test_all_absent_archive_sizing_inputs_are_reported_as_insufficient_evidence(cohorts):
    from ml.gameplan_archive_features import ARCHIVE_FEATURE_CONTRACT
    missing = {group: frame.copy() for group, frame in cohorts.items()}
    for frame in missing.values():
        frame["source_selection_contract"] = ARCHIVE_FEATURE_CONTRACT
        frame[list(INDEPENDENT_MARKET_FEATURE_NAMES)] = np.nan
    payload, report = training.fit_independent_enrichment_model_payload(
        missing, trained_at=TRAINED, source_fingerprint="d" * 64)
    assert report["status"] == "INSUFFICIENT_EVIDENCE"
    for group, frame in missing.items():
        record = payload["horizons"][group]
        assert record["fitted"] is False
        evidence = record["market_feature_admission"]
        assert evidence["input_execution_rows"] == evidence["excluded_missing_market_rows"] == len(frame)
        assert evidence["admitted_rows"] == 0
        assert report["horizons"][group]["market_feature_admission"] == evidence
        assert report["horizons"][group]["excluded_context_rows"] == 0


def _calibration_frame():
    decisions = pd.date_range("2026-08-01T00:00:00Z", periods=8, freq="D")
    return pd.DataFrame({"decision_timestamp": decisions, "target_window_start": decisions + pd.Timedelta(hours=1),
                         "target_window_end": decisions + pd.Timedelta(hours=2), "target": np.tile([0, 1], 4)})


def test_development_calibration_keeps_identity_when_platt_would_erase_later_signal():
    frame = _calibration_frame()
    probability = np.array([.9, .1, .9, .1, .1, .9, .1, .9])
    calibration, report = training._select_development_calibration(frame, probability)
    assert calibration == (1., 0.)
    assert report["selected_family"] == "identity"
    assert report["candidate_metrics"]["identity"]["log_loss"] < report["candidate_metrics"]["platt"]["log_loss"]
    assert report["assessment_used_for_selection"] is False


def test_development_calibration_purges_overlapping_weekly_labels():
    frame = _calibration_frame()
    frame["target_window_end"] += pd.Timedelta(days=7)
    calibration, report = training._select_development_calibration(frame, np.tile([.4, .6], 4))
    assert calibration == (1., 0.)
    assert report["purged_rows"] == 4
    assert report["selection_status"] == "IDENTITY_INSUFFICIENT_PURGED_DEVELOPMENT_SUPPORT"


def test_development_calibration_identity_wins_exact_ties(monkeypatch):
    calls = []
    monkeypatch.setattr(training, "_fit_logit_platt", lambda probability, labels: calls.append(len(labels)) or (1., 0.))
    calibration, report = training._select_development_calibration(_calibration_frame(), np.tile([.4, .6], 4))
    assert calibration == (1., 0.)
    assert report["selected_family"] == "identity"
    assert calls == [4]


def test_head_regularization_uses_its_own_fixed_development_objective(fitted):
    for record in fitted[0]["horizons"].values():
        report = record["head_development_selection"]
        assert report["candidate_penalties"] == [1., 5., 20.]
        assert report["assessment_used_for_selection"] is False
        for head, metric in report["head_objectives"].items():
            expected = min(report["candidate_penalties"], key=lambda penalty: report["candidate_metrics"][str(penalty)][metric])
            assert record["head_ridge_penalties"][head] == expected


def test_pinned_cohort_loading_does_not_follow_a_newer_current_pointer(monkeypatch, tmp_path):
    from types import SimpleNamespace
    from ml import nightly_gameplan
    pinned = tmp_path / "ml" / "nightly-gameplan-runs" / "pinned"
    pinned.mkdir(parents=True)
    for name in ("manifest.json", "receipt.json"):
        (pinned / name).write_text("{}", encoding="utf-8")
    names = [f"training-cohort-{group}.parquet" for group in GROUPS]
    for name in names:
        pd.DataFrame({"marker": ["pinned"]}).to_parquet(pinned / name)
    publication = SimpleNamespace(run_directory=pinned,
        manifest={"configuration": {"target_contract_version": STOCK_TARGET_CONTRACT_VERSION},
                  "output_files": dict.fromkeys(names, {})})
    calls = []
    monkeypatch.setattr(nightly_gameplan, "read_gameplan_run", lambda root, run: calls.append(run) or publication)
    monkeypatch.setattr(nightly_gameplan, "read_current_gameplan", lambda root: pytest.fail("A pinned source must not follow latest"))
    groups, _, metadata = training.load_current_training_cohorts(tmp_path, gameplan_run=pinned)
    assert calls == [pinned]
    assert all(frame.marker.tolist() == ["pinned"] for frame in groups.values())
    assert metadata["source_gameplan_run"].endswith("/pinned")
