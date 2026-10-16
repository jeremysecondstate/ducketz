"""Dated planning preserves source evidence without replaying expired entries."""
from copy import deepcopy
import json

import pandas as pd
import pytest

from ml import gameplan_trade_planning as planning
from ml.artifacts import verify_manifest
from ml.gameplan_cash_ledger import project_direction_trades
from ml.nightly_continuation import ENVIRONMENT_KEY, POLICY
from test_gameplan_trade_planning import publication_case, forecast, snapshot, bands
from test_gameplan_cash_ledger import (
    forecast as ledger_forecast, snapshot as ledger_snapshot,
    path as ledger_path, POLICY as LEDGER_POLICY,
)


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


def test_recovered_trade_plan_keeps_deadlines_and_forecasts(publication_case):
    c = publication_case
    _late_source(c)
    before = _source_bytes(c)
    run = planning.publish_trade_plan(
        c.root, gameplan_run=c.source, deadline="2026-09-09T19:00Z",
        late_action_date=DAY, snapshot_loader=lambda *a, **kw: c.state,
        price_loader=c.prices, clock=lambda: pd.Timestamp("2026-09-09T16:00Z"))
    verify_manifest(run)
    report = json.loads((run / "report.json").read_text())
    assert report["deadline_at"] == OPENING
    assert report["effective_deadline_at"] == "2026-09-09T19:00:00+00:00"
    assert report["observed_at"] == "2026-09-09T16:00:00+00:00"
    assert report["publication_mode"] == "LATE_RECOVERY"
    assert report["orders_placed"] == 0 and report["broker_orders_enabled"] is False
    assert _source_bytes(c) == before


def test_repaired_late_tail_keeps_separate_source_bound_continuation(publication_case):
    from ml.preparation_deadline import RECOVERY_VERSION
    from test_preparation_deadline import exception_record

    c = publication_case
    _late_source(c)
    path, record = exception_record(c.root, c.source, session=DAY)
    record.update(
        schema_version=RECOVERY_VERSION,
        original_session_deadline_at=record["original_deadline_at"],
        original_deadline_at="2026-09-09T19:00Z",
        approved_at="2026-09-09T19:01Z", expires_at="2026-09-09T21:00Z")
    path.write_text(json.dumps(record))
    before, exception_bytes = _source_bytes(c), path.read_bytes()
    run = planning.publish_trade_plan(
        c.root, gameplan_run=c.source, deadline="2026-09-09T19:00Z",
        late_action_date=DAY, deadline_exception=path,
        snapshot_loader=lambda *a, **kw: c.state, price_loader=c.prices,
        clock=lambda: pd.Timestamp("2026-09-09T19:20Z"))
    verify_manifest(run)
    report = json.loads((run / "report.json").read_text())
    assert report["deadline_at"] == OPENING
    assert report["effective_deadline_at"] == "2026-09-09T21:00:00+00:00"
    assert report["deadline_exception"]["authorization"]["original_deadline_at"] == "2026-09-09T19:00Z"
    assert _source_bytes(c) == before and path.read_bytes() == exception_bytes


@pytest.mark.parametrize("damage", [
    "normal_source", "wrong_date", "missing_deadline", "expired", "before_open",
    "after_close", "refresh", "exception", "missing_flag",
])
def test_recovery_deadline_cannot_bypass_source_or_time_guards(publication_case, damage):
    c = publication_case
    if damage != "normal_source":
        _late_source(c)
    extra = dict(late_action_date=DAY, deadline="2026-09-09T19:00Z")
    now = "2026-09-09T16:00Z"
    if damage == "wrong_date":
        extra["late_action_date"] = "2026-09-10"
    if damage == "missing_deadline":
        extra["deadline"] = None
    if damage == "expired":
        now = "2026-09-09T19:00Z"
    if damage == "before_open":
        now = "2026-09-09T10:59Z"
    if damage == "after_close":
        extra["deadline"] = "2026-09-10T00:01Z"
    if damage == "refresh":
        extra["refresh_plan"] = c.source
    if damage == "exception":
        extra["deadline_exception"] = c.source / "missing-exception.json"
    if damage == "missing_flag":
        del extra["late_action_date"]

    def forbidden(*a, **kw):
        pytest.fail("Invalid recovery must fail before account or price capture")

    before = _source_bytes(c)
    with pytest.raises(ValueError, match="[Dd]eadline|recovery"):
        planning.publish_trade_plan(
            c.root, gameplan_run=c.source, snapshot_loader=forbidden,
            price_loader=forbidden, clock=lambda: pd.Timestamp(now), **extra)
    assert _source_bytes(c) == before
    assert not (c.root / "ml/gameplan-trade-plan-runs").exists()


def test_recovery_expiring_during_publication_keeps_old_pointer(publication_case):
    c = publication_case
    _late_source(c)
    pointer = c.root / "ml/gameplan-trade-plan-latest/run.json"
    pointer.parent.mkdir(parents=True)
    pointer.write_text("previous-plan")
    ticks = iter([pd.Timestamp("2026-09-09T18:59Z"), pd.Timestamp("2026-09-09T19:00Z")])

    def clock():
        return next(ticks, pd.Timestamp("2026-09-09T19:00Z"))

    with pytest.raises(RuntimeError, match="Trade planning failed"):
        planning.publish_trade_plan(
            c.root, gameplan_run=c.source, deadline="2026-09-09T19:00Z",
            late_action_date=DAY, snapshot_loader=lambda *a, **kw: c.state,
            price_loader=c.prices, clock=clock)
    assert pointer.read_text() == "previous-plan"


@pytest.mark.parametrize("late_date", [None, DAY])
def test_matching_dated_night_finishes_after_close_without_rewriting_source(
        publication_case, monkeypatch, late_date):
    c = publication_case
    monkeypatch.setenv(ENVIRONMENT_KEY, DAY)
    now = pd.Timestamp("2026-09-12T16:00Z")
    state = {**c.state, "observed_at": now.isoformat()}
    original_state = deepcopy(state)
    before = _source_bytes(c)
    run = planning.publish_trade_plan(
        c.root, gameplan_run=c.source, deadline=OPENING,
        late_action_date=late_date, snapshot_loader=lambda *a, **kw: state,
        price_loader=c.prices, clock=lambda: now)
    verify_manifest(run)
    report = json.loads((run / "report.json").read_text())
    receipt = json.loads((run / "receipt.json").read_text())
    ledger = json.loads((run / "direction-ledger.json").read_text())
    rows = pd.read_parquet(run / "trade-plan.parquet")
    assert report["action_date"] == DAY
    assert report["deadline_at"] == report["effective_deadline_at"] == OPENING
    assert report["observed_at"] == now.isoformat()
    assert report["deadline_exception"] is None
    assert receipt["status"] == "COMPLETE"
    assert receipt["orders_placed"] == 0 and receipt["broker_orders_enabled"] is False
    eligible = rows.loc[rows.execution_eligible]
    assert eligible.trade_quantity.eq(0).all()
    assert eligible.projected_trade_quantity.eq(0).all()
    assert eligible.direction_based_trade_quantity.eq(0).all()
    assert not ledger["events"]
    original = pd.read_parquet(c.source / "forecasts.parquet")
    for field in ("target_window_start", "target_window_end", "frozen_at"):
        pd.testing.assert_series_equal(rows[field], original[field])
    assert state == original_state and _source_bytes(c) == before


@pytest.mark.parametrize("selected", ["2026-09-08", "2026-09-10"])
def test_another_nights_context_does_not_bypass_expiry(publication_case, monkeypatch, selected):
    c = publication_case
    monkeypatch.setenv(ENVIRONMENT_KEY, selected)

    def forbidden(*a, **kw):
        pytest.fail("Another night's continuation cannot read this account")

    with pytest.raises(ValueError, match="deadline has passed"):
        planning.publish_trade_plan(
            c.root, gameplan_run=c.source, snapshot_loader=forbidden,
            price_loader=forbidden, clock=lambda: pd.Timestamp("2026-09-12T16:00Z"))


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
def test_truthful_late_publication_time_reaches_forecast_validator(publication_case, monkeypatch, source_mode):
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
    planning.publish_trade_plan(
        c.root, gameplan_run=c.source, late_action_date=DAY,
        snapshot_loader=lambda *a, **kw: c.state, price_loader=c.prices,
        clock=lambda: pd.Timestamp("2026-09-12T16:00Z"))
    assert len(calls) == 1
    assert calls[0][1] == {
        "action_date": DAY, "symbols": ("AAPL",),
        "late_publication_at": "2026-09-09T15:30:00Z",
    }
    pd.testing.assert_frame_equal(calls[0][0], pd.read_parquet(c.source / "forecasts.parquet"))
    assert _source_bytes(c) == before


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


def test_expired_entry_does_not_reserve_cash_or_claim_current_horizon():
    state = snapshot(cash=75)
    state["observed_at"] = "2026-09-09T12:00:00Z"
    original_state = deepcopy(state)
    frame = pd.DataFrame([forecast(hour=4), forecast(hour=5)])
    original = frame.copy(deep=True)
    result = planning.plan_trade_rows(frame, state, bands(frame.to_dict("records")))
    current = planning.plan_trade_rows(frame.iloc[[1]], state, bands(frame.iloc[[1]].to_dict("records")))
    for field in ("trade_quantity", "projected_trade_quantity", "projected_trade_budget",
                  "projected_trade_notional", "scheduled_trade_quantity", "trade_notional_reserved"):
        assert result.iloc[0][field] == 0
    assert result.iloc[0].trade_planning_reason == "EXPIRED_BEFORE_PREPARATION"
    assert result.iloc[1].trade_quantity == current.iloc[0].trade_quantity > 0
    assert result.iloc[1].projected_trade_quantity == current.iloc[0].projected_trade_quantity
    assert result.iloc[1].trade_planning_reason == "PROVISIONAL_BUY"
    assert state == original_state
    pd.testing.assert_frame_equal(frame, original)


@pytest.mark.parametrize("probability,held,action", [(0.7, 0, "BUY"), (0.3, 2, "SELL")])
def test_direction_ledger_skips_past_buys_and_sells_but_keeps_current_slot(probability, held, action):
    state = ledger_snapshot(held=held)
    state["observed_at"] = "2026-09-09T12:00:00Z"
    original_state = deepcopy(state)
    frame = pd.DataFrame([
        ledger_forecast(hour=4, probability=probability),
        ledger_forecast(hour=5, probability=probability),
    ])
    original = frame.copy(deep=True)
    rows, ledger = project_direction_trades(
        frame, state, ledger_path(), policy=LEDGER_POLICY, signal_driven=True)
    assert rows.iloc[0].direction_based_trade_quantity == 0
    assert rows.iloc[0].direction_based_reason == "EXPIRED_BEFORE_PREPARATION"
    assert rows.iloc[1].direction_based_action == action
    assert len(ledger["events"]) == 1
    assert ledger["events"][0]["action"] == action
    assert pd.Timestamp(ledger["events"][0]["timestamp"]) == pd.Timestamp(state["observed_at"])
    assert state == original_state
    pd.testing.assert_frame_equal(frame, original)


def test_all_expired_direction_slots_leave_cash_and_shares_unchanged():
    state = ledger_snapshot(held=2)
    state["observed_at"] = "2026-09-10T04:00:00Z"
    original_state = deepcopy(state)
    frame = pd.DataFrame([
        ledger_forecast(hour=4, probability=.7), ledger_forecast(hour=10, probability=.3),
    ])
    rows, ledger = project_direction_trades(
        frame, state, ledger_path(), policy=LEDGER_POLICY, signal_driven=True)
    assert not ledger["events"]
    assert rows.direction_based_trade_quantity.eq(0).all()
    assert rows.direction_based_reason.eq("EXPIRED_BEFORE_PREPARATION").all()
    assert all(hour["cash_base"] == state["available_cash"] for hour in ledger["hourly"])
    assert all(hour["held_shares"] == state["held_shares"] for hour in ledger["hourly"])
    assert state == original_state


def test_expired_ledger_rows_still_require_valid_target_clocks():
    state = ledger_snapshot()
    state["observed_at"] = "2026-09-10T04:00:00Z"
    row = ledger_forecast()
    row["target_window_end"] = row["target_window_start"]
    with pytest.raises(ValueError, match="clocks"):
        project_direction_trades(
            pd.DataFrame([row]), state, ledger_path(), policy=LEDGER_POLICY, signal_driven=True)
