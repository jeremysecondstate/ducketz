"""Dated source provenance shared by current and historical publication recipes.

Selected from Scout PR192; keep frozen source evidence and refresh identity.
These tests do not assert planning prices or projected execution capacity.
"""
import json

import pandas as pd
import pytest

from ml import gameplan_trade_planning as planning
from ml.nightly_continuation import ENVIRONMENT_KEY, POLICY
from test_gameplan_trade_planning import publication_case

DAY = "2026-09-09"
OPENING = "2026-09-09T11:00:00+00:00"


@pytest.fixture(autouse=True)
def isolated_continuation_environment(monkeypatch):
    monkeypatch.delenv(ENVIRONMENT_KEY, raising=False)



def _source_bytes(case):
    return {path.name: path.read_bytes() for path in case.source.iterdir()}



def _late_source(case):
    from ml.nightly_gameplan import read_gameplan_run

    publication = read_gameplan_run(case.root, case.source)
    publication.manifest["configuration"].update(
        publication_mode="LATE_RECOVERY", late_action_date=DAY, action_date=DAY)
    receipt = {**publication.receipt, "published_at": "2026-09-09T15:30:00Z"}
    (case.source / "receipt.json").write_text(json.dumps(receipt))



@pytest.mark.parametrize("extra", [
    {"late_action_date": "2026-09-10"}, {"deadline": "2026-09-09T19:00Z"},
])
def test_dated_continuation_cannot_replace_original_date_or_target(publication_case, monkeypatch, extra):
    c = publication_case
    monkeypatch.setenv(ENVIRONMENT_KEY, DAY)

    def forbidden(*a, **kw):
        pytest.fail("Changed session identity must fail before account or price capture")

    with pytest.raises(ValueError, match="original action date and target"):
        planning.publish_trade_plan(
            c.root, gameplan_run=c.source, snapshot_loader=forbidden,
            price_loader=forbidden, clock=lambda: pd.Timestamp("2026-09-12T16:00Z"), **extra)



def test_dated_continuation_cannot_skip_requested_refresh_evidence(publication_case, monkeypatch):
    c = publication_case
    monkeypatch.setenv(ENVIRONMENT_KEY, DAY)
    before = _source_bytes(c)

    def forbidden(*a, **kw):
        pytest.fail("A requested refresh must never silently capture a new account")

    with pytest.raises(ValueError, match="[Rr]efresh"):
        planning.publish_trade_plan(
            c.root, gameplan_run=c.source, late_action_date=DAY,
            refresh_plan=c.source, snapshot_loader=forbidden, price_loader=forbidden,
            clock=lambda: pd.Timestamp("2026-09-12T16:00Z"))
    assert _source_bytes(c) == before
    assert not (c.root / "ml/gameplan-trade-plan-runs").exists()



@pytest.mark.parametrize("source_mode", ["LATE_RECOVERY", "NIGHTLY_CONTINUATION"])
@pytest.mark.parametrize("observed_at", ["2026-09-09T15:30Z", "2026-09-09T15:30:00.000001Z", "2026-09-12T16:00Z"])
def test_truthful_late_publication_time_reaches_forecast_validator(publication_case, monkeypatch, source_mode, observed_at):
    from ml.nightly_gameplan import read_gameplan_run
    from ml.stock_trader import independent_signals

    c = publication_case
    _late_source(c)
    if source_mode == "NIGHTLY_CONTINUATION":
        config = read_gameplan_run(c.root, c.source).manifest["configuration"]
        config.update(publication_mode=source_mode, continuation_policy=POLICY,
                      training_information_cutoff="2026-09-09T10:59:59.999999Z")
    monkeypatch.setenv(ENVIRONMENT_KEY, DAY)
    calls = []

    def validate(frame, **kwargs):
        calls.append((frame.copy(deep=True), kwargs))
        return frame

    monkeypatch.setattr(independent_signals, "_validated_independent_forecasts", validate)
    before = _source_bytes(c)
    run = planning.publish_trade_plan(
        c.root, gameplan_run=c.source, late_action_date=DAY,
        snapshot_loader=lambda *a, **kw: c.state, price_loader=c.prices,
        clock=lambda: pd.Timestamp(observed_at))
    assert len(calls) == 1
    assert calls[0][1] == {
        "action_date": DAY, "symbols": ("AAPL",),
        "late_publication_at": "2026-09-09T15:30:00Z",
    }
    pd.testing.assert_frame_equal(calls[0][0], pd.read_parquet(c.source / "forecasts.parquet"))
    assert _source_bytes(c) == before

    receipt = json.loads((run / "receipt.json").read_text())
    assert receipt["status"] == "COMPLETE"
    assert receipt["orders_placed"] == 0 and receipt["broker_orders_enabled"] is False
    original = pd.read_parquet(c.source / "forecasts.parquet")
    published = pd.read_parquet(run / "trade-plan.parquet")
    pd.testing.assert_series_equal(published.calibrated_probability, original.calibrated_probability)
    pd.testing.assert_series_equal(published.target_window_start, original.target_window_start)
    pd.testing.assert_series_equal(published.target_window_end, original.target_window_end)



@pytest.mark.parametrize("source_mode", ["LATE_RECOVERY", "NIGHTLY_CONTINUATION"])
def test_future_late_publication_cannot_be_used_before_it_exists(publication_case, monkeypatch, source_mode):
    from ml.nightly_gameplan import read_gameplan_run

    c = publication_case
    _late_source(c)
    publication = read_gameplan_run(c.root, c.source)
    if source_mode == "NIGHTLY_CONTINUATION":
        publication.manifest["configuration"].update(
            publication_mode=source_mode, continuation_policy=POLICY,
            training_information_cutoff="2026-09-09T10:59:59.999999Z")
    (c.source / "receipt.json").write_text(json.dumps({
        **publication.receipt, "published_at": "2026-09-09T17:00:00Z",
    }))
    monkeypatch.setenv(ENVIRONMENT_KEY, DAY)
    before = _source_bytes(c)

    def forbidden(*a, **kw):
        pytest.fail("Future publication must fail before account or price capture")

    with pytest.raises(ValueError, match="not yet available"):
        planning.publish_trade_plan(
            c.root, gameplan_run=c.source, late_action_date=DAY,
            snapshot_loader=forbidden, price_loader=forbidden,
            clock=lambda: pd.Timestamp("2026-09-09T16:00Z"))
    assert _source_bytes(c) == before
    assert not (c.root / "ml/gameplan-trade-plan-runs").exists()



def test_invalid_dated_source_evidence_fails_before_account_capture(publication_case, monkeypatch):
    from ml.nightly_gameplan import read_gameplan_run

    c = publication_case
    _late_source(c)
    read_gameplan_run(c.root, c.source).manifest["configuration"].update(
        publication_mode="NIGHTLY_CONTINUATION", continuation_policy=POLICY,
        training_information_cutoff=OPENING)
    monkeypatch.setenv(ENVIRONMENT_KEY, DAY)

    def forbidden(*a, **kw):
        pytest.fail("Invalid causal evidence must fail before account or price capture")

    with pytest.raises(ValueError, match="causal inputs"):
        planning.publish_trade_plan(
            c.root, gameplan_run=c.source, late_action_date=DAY,
            snapshot_loader=forbidden, price_loader=forbidden,
            clock=lambda: pd.Timestamp("2026-09-12T16:00Z"))



def test_cli_forwards_explicit_late_date_without_changing_deadline(tmp_path, monkeypatch):
    calls = []
    source = tmp_path / "source-gameplan"

    def publish(root, **kwargs):
        calls.append((root, kwargs))
        return tmp_path / "published-review"

    monkeypatch.setattr(planning, "publish_trade_plan", publish)
    assert planning.main([
        "--datastore", str(tmp_path), "--gameplan-run", str(source),
        "--deadline", OPENING, "--late-action-date", DAY,
    ]) == 0
    assert calls == [(tmp_path.resolve(), {
        "gameplan_run": source, "deadline": OPENING, "late_action_date": DAY,
    })]

