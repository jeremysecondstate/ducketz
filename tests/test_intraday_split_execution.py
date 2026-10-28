"""Compact execution parity, immutable evidence and fake-broker boundaries."""
from copy import deepcopy

import pytest

from tests.intraday_split_package_support import require_split_integration
require_split_integration(__file__, files=(
    "ml/intraday/account_execution.py", "ml/intraday/starter_sizing.py", "ml/intraday/split_execution.py",
    "tests/test_intraday_starter_sizing.py", "tests/test_intraday_account_execution.py"),
    interfaces=(("ml/intraday/runtime.py", "ShadowRunner", "prepare_cycle", ()),
                ("ml/intraday/runtime.py", "ShadowRunner", "__init__",
                 ("prepared_source", "forecast_preparer", "datastore"))))

from ml.intraday.prediction import ContractError, digest, encoded
from ml.intraday.split_execution import execution_indicators, prepare_forecast, read_evidence
from ml.intraday.starter_sizing import size_forecast
from tests.test_intraday_starter_sizing import fixture
from tests.test_intraday_account_execution import env, advance, native


def compact_fixture(tmp_path, **options):
    forecast, snapshot, equity, quote = fixture(**options)
    payload = {key: value for key, value in forecast.items() if key != "forecast_id"}
    payload.update(cycle_id="fixture-cycle", models=[{
        "name": "original", "family": "fixture", "status": "success", "version": "original-version",
        "probability_not_down": forecast["probability_not_down"], "metadata": {"original_diagnostics": list(range(5000))}}])
    fitted = {"outputs": payload["models"], "probability_not_down": forecast["probability_not_down"]}
    small = prepare_forecast(payload, snapshot, fitted, evidence_root=tmp_path)
    small["forecast_id"] = digest(small)
    return forecast, small, snapshot, equity, quote, fitted


@pytest.mark.parametrize("probability", [.72, .2, .5, .5001, .4999])
def test_compact_features_keep_same_direction_score_quantity_and_deadlines(tmp_path, probability):
    original, small, snapshot, equity, quote, fitted = compact_fixture(tmp_path, probability=probability)
    arguments = dict(account_equity=equity, quote=quote, calculated_at=original["issued_at"], maximum_quote_age_seconds=60)
    full = size_forecast(original, forecast_snapshot=snapshot, **arguments).payload()
    compact = size_forecast(small, forecast_snapshot=None, **arguments).payload()
    for field in ("side", "status", "reason", "score", "desired_quantity", "target_open", "target_close",
                  "term_contributions", "direction_quantity_policy", "reference_context"):
        assert compact.get(field) == full.get(field)
    assert compact["indicator_snapshot"]["features"] == full["indicator_snapshot"]["features"]
    assert compact["indicator_snapshot"]["candles_hash"] == full["indicator_snapshot"]["candles_hash"]
    assert compact["indicator_snapshot"]["candles"] == []
    assert small["probability_not_down"] == fitted["probability_not_down"]
    assert small["models"][0]["version"] == "original-version"
    record = small["split_execution"]
    assert read_evidence(tmp_path, record["full_models"])["fitted"] == fitted
    assert read_evidence(tmp_path, record["full_indicator"])["candles"] == full["indicator_snapshot"]["candles"]
    assert len(encoded(record["indicator"])) < len(encoded(full["indicator_snapshot"])) / 2


@pytest.mark.parametrize("change", ["forecast", "feature", "config", "snapshot", "cutoff", "source"])
def test_changed_compact_forecast_or_indicators_rejected(tmp_path, change):
    _, small, _, _, _, _ = compact_fixture(tmp_path)
    value = deepcopy(small)
    indicator = value["split_execution"]["indicator"]
    if change == "forecast":
        value["probability_not_down"] = .99
    else:
        if change == "feature": indicator["features"]["rsi14.value"] = -100
        elif change == "config": indicator["indicator_config"]["window"]["basis"] = "contiguous_session"
        elif change == "snapshot": indicator["forecast_snapshot_hash"] = "0" * 64
        elif change == "cutoff": indicator["data_cutoff"] = "2026-10-09T00:00:00Z"
        elif change == "source": indicator["source_id"] = "different-source"
        # Isolate the inner checks while preserving a valid forecast envelope.
        if change != "feature":
            value["split_execution"]["indicator_hash"] = digest(indicator)
        value.pop("forecast_id")
        value["forecast_id"] = digest(value)
    with pytest.raises(ContractError):
        execution_indicators(value)


def test_full_evidence_remains_retrievable_and_changed_bytes_never_reuse(tmp_path):
    _, small, _, _, _, _ = compact_fixture(tmp_path)
    reference = small["split_execution"]["full_models"]
    path = tmp_path / reference["file"]
    original = path.read_bytes()
    path.write_bytes(original.replace(b"original_diagnostics", b"corrupt__diagnostics"))
    with pytest.raises(ContractError, match="evidence changed"):
        read_evidence(tmp_path, reference)
    assert path.read_bytes() != original  # Historical corruption is not silently rewritten.


@pytest.mark.parametrize("unknown", [False, True])
def test_small_forecast_executes_with_no_training_snapshot_reads_and_no_duplicate_send(env, monkeypatch, unknown):
    save = env.store.save_forecast

    def compact_save(payload, **kwargs):
        snapshot = env.store.inputs(payload["cycle_id"], payload["symbol"])[1]
        small = prepare_forecast(payload, snapshot, {"outputs": payload["models"]},
                                 evidence_root=env.root / "private-model-evidence")
        return save(small, **kwargs)

    monkeypatch.setattr(env.store, "save_forecast", compact_save)
    forecast = env.forecast()
    monkeypatch.setattr(env.store, "inputs", lambda *a: pytest.fail("trading process loaded full training snapshot"))
    env.broker.unknown_submission = unknown
    result = env.executor.step(forecast)
    assert result["desired_quantity"] == 4
    assert result["adjusted_quantity"] == 2
    assert result["disposition"] == ("submission_recovery_required" if unknown else "submitted")
    assert len(env.broker.submissions) == 1
    assert env.broker.submissions[0]["orderLegCollection"][0]["quantity"] == 2
    assert env.broker.submissions[0]["price"] == "50.00"
    reservation = native(env).snapshot().reservations[0]
    assert reservation.status == ("UNKNOWN" if unknown else "SUBMITTED")
    advance(env)
    assert env.executor.step(forecast)["disposition"] == "recover_existing_intention"
    assert len(env.broker.submissions) == 1
    assert native(env).snapshot().reservations[0].reservation_id == reservation.reservation_id
