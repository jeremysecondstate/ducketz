"""Integrity and exact-direction regressions for reused frozen sizing inputs."""
import copy

import pytest

from ml.intraday import starter_sizing as starter, technical_sizing as technical
from ml.intraday.prediction import ContractError
from test_intraday_starter_sizing import fixture, run


def indicator_args(forecast, snapshot):
    policy = starter.starter_policy(forecast["symbol"])
    return dict(symbol=forecast["symbol"], reference_at=forecast["reference_at"],
        data_cutoff=forecast["training_cutoff"], indicator_specs=policy.indicators,
        window_policy=policy.window, forecast_snapshot=snapshot,
        forecast_snapshot_hash=forecast["snapshot_hash"])


def test_sizing_verifies_full_snapshot_once_and_admits_identical_candles_once(monkeypatch):
    bundle = fixture(symbol="SDGR")
    snapshot = bundle[1]
    hashes, admissions = [], []
    starter_digest, indicator_digest = starter.digest, technical.digest
    validate = technical.validate_candles
    def sized_digest(value):
        if value is snapshot:
            hashes.append("starter")
        return starter_digest(value)
    def checked_digest(value):
        if value is snapshot:
            hashes.append("indicator")
        return indicator_digest(value)
    def admitted(frame):
        admissions.append(len(frame))
        return validate(frame)
    monkeypatch.setattr(starter, "digest", sized_digest)
    monkeypatch.setattr(technical, "digest", checked_digest)
    monkeypatch.setattr(technical, "validate_candles", admitted)
    result = run(bundle)
    assert result.payload()["status"] == "sized"
    assert hashes == ["indicator"]
    assert admissions == [len(snapshot["candles"])]


def test_distinct_equal_candles_keep_same_result_and_original_comparison(monkeypatch):
    forecast, snapshot, _, _ = fixture(symbol="SDGR")
    args = indicator_args(forecast, snapshot)
    original = technical.indicator_snapshot(snapshot["candles"], **args)
    validate = technical.validate_candles
    admissions = []
    def admitted(frame):
        admissions.append(len(frame))
        return validate(frame)
    monkeypatch.setattr(technical, "validate_candles", admitted)
    separate = technical.indicator_snapshot(list(snapshot["candles"]), **args)
    assert original == separate
    assert admissions == [len(snapshot["candles"])] * 2
    changed = copy.deepcopy(snapshot["candles"])
    changed[0]["volume"] += 1
    with pytest.raises(ContractError, match="Sizing candles differ from the original"):
        technical.indicator_snapshot(changed, **args)


@pytest.mark.parametrize("malformed", [None, [], {}, {"cutoff": "changed"}])
def test_malformed_snapshot_preserves_integrity_error(malformed):
    with pytest.raises(ContractError, match="Original forecast snapshot hash changed"):
        run(fixture(symbol="SDGR"), forecast_snapshot=malformed)


def test_snapshot_mutation_still_fails_before_sizing():
    bundle = fixture(symbol="SDGR")
    changed = dict(bundle[1], cutoff="2026-10-22T00:00:00+00:00")
    with pytest.raises(ContractError, match="Original forecast snapshot hash changed"):
        run(bundle, forecast_snapshot=changed)


@pytest.mark.parametrize("probability,side", [
    (.5001, "BUY"), (.4999, "SELL"), (.5, "HOLD"),
    (.499900001, "HOLD"), (.500099999, "HOLD")])
def test_independent_reference_uses_exact_current_direction(probability, side):
    forecast, _, equity, quote = fixture(symbol="SDGR", probability=probability)
    reference, reason = starter.independent_reference(forecast, account_equity=equity,
        quote=quote, calculated_at=forecast["issued_at"], maximum_quote_age_seconds=60)
    if side == "HOLD":
        assert reference is None and reason == "probability_between_action_cutoffs"
    else:
        assert reference.side == side and reason is None


def test_neutral_direction_is_preserved_when_account_inputs_are_unavailable():
    forecast, _, _, _ = fixture(symbol="SDGR", probability=.5)
    reference, reason = starter.independent_reference(forecast, account_equity=None,
        quote=None, calculated_at=forecast["issued_at"], maximum_quote_age_seconds=60)
    assert reference is None and reason == "probability_between_action_cutoffs"
