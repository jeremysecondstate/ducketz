"""Current Gameplans need predictions, never estimates of future prices."""
from copy import deepcopy
import json
from types import SimpleNamespace

import pandas as pd
import pytest

from ml.gameplan_execution_policy import (
    direction_only_rows, current_gameplan_policy_metadata, is_current_gameplan_recipe,
)
from ml.gameplan_trade_planning import publish_trade_plan, _publish_direction_plan
from ml.joint_capital_handoff import export_owner_package
from ml.joint_capital_plan import build_owner_package, compose_joint_plan, load_owner_package
from test_gameplan_trade_planning import publication_case
from test_joint_capital_handoff import saved, rebind, FROZEN, CREATED, DAY


@pytest.mark.parametrize("horizon", ["1h", "4h", "1d", "1w"])
@pytest.mark.parametrize("probability,action", [(.48, "SELL"), (.4999, "SELL"),
    (.49995, "HOLD"), (.5, "HOLD"), (.50005, "HOLD"), (.5001, "BUY"), (.7, "BUY")])
def test_every_horizon_uses_exact_current_calls_without_promotion_cash_or_price_gates(horizon, probability, action):
    frame = pd.DataFrame([dict(id="fixture", symbol="TEST", model_group=horizon,
        execution_eligible=True, calibrated_probability=probability, direction="NO_EDGE",
        model_status="RESEARCH_NOT_PROMOTED", symbol_route_fitted_target_rows=40)])
    before = frame.copy(deep=True)
    rows, ledger = direction_only_rows(frame, action_date="2026-10-20")
    row = rows.iloc[0]
    assert row.direction_based_action == action
    assert pd.isna(row.direction_based_trade_quantity) if action != "HOLD" else row.direction_based_trade_quantity == 0
    assert row.source_saved_direction == "NO_EDGE"
    assert is_current_gameplan_recipe(row)
    assert ledger["events"] == [] and ledger["hourly"] == []
    pd.testing.assert_frame_equal(frame, before)


def test_producer_does_not_load_price_history_or_capture_scout_account(publication_case, monkeypatch):
    from ml import gameplan_price_bands
    case = publication_case
    def forbidden(*args, **kwargs):
        pytest.fail("Directional preparation must not estimate prices or capture a Scout account")
    monkeypatch.setattr(gameplan_price_bands, "build_entry_price_bands", forbidden)
    monkeypatch.setattr(gameplan_price_bands, "build_planning_price_path", forbidden)
    run = publish_trade_plan(case.root, gameplan_run=case.source, research_producer_only=True,
        price_loader=forbidden, clock=case.clock)
    assert not (run / "planning-price-path.json").exists()
    assert not (run / "price-bands.json").exists()
    assert not (run / "account-snapshot.json").exists()
    assert is_current_gameplan_recipe(json.loads((run / "receipt.json").read_text()))


def test_current_publisher_rejects_receipt_swap_during_observed_context_capture(publication_case):
    case = publication_case
    prior = publish_trade_plan(case.root, gameplan_run=case.source,
        snapshot_loader=lambda *args, **kwargs: case.state, clock=case.clock)
    pointer = case.root / "ml/gameplan-trade-plan-latest/run.json"
    before = pointer.read_bytes()
    runs = set(prior.parent.iterdir())
    def changed(*args, **kwargs):
        with (case.source / "receipt.json").open("ab") as stream:
            stream.write(b" ")
        return case.state
    with pytest.raises(ValueError, match="Frozen Gameplan receipt changed"):
        publish_trade_plan(case.root, gameplan_run=case.source,
            snapshot_loader=changed, clock=case.clock)
    assert pointer.read_bytes() == before
    assert set(prior.parent.iterdir()) == runs


@pytest.mark.parametrize("change", ["source_before", "source_after", "deadline", "binding", "foreign_before", "foreign_after"])
def test_current_pointer_selection_revalidates_inputs_and_preserves_other_writers(publication_case, monkeypatch, change):
    from ml import gameplan_trade_planning as planning
    from ml.artifacts import file_checksum
    from ml.nightly_gameplan import read_gameplan_run
    case = publication_case
    prior = publish_trade_plan(case.root, gameplan_run=case.source,
        snapshot_loader=lambda *a, **kw: case.state, clock=case.clock)
    pointer = case.root / "ml/gameplan-trade-plan-latest/run.json"
    before, foreign = pointer.read_bytes(), b'{"independent_writer": true}'
    publication = read_gameplan_run(case.root, case.source)
    source_hash = file_checksum(case.source / "receipt.json")
    now, bound = [case.clock()], [True]
    original = planning._write_json
    def write_then_change(path, value):
        original(path, value)
        trigger = (path.name == "receipt.json" and value.get("status") == "COMPLETE"
                   if change.endswith("before") else path == pointer)
        if trigger:
            if change.startswith("source"):
                with (case.source / "receipt.json").open("ab") as stream:
                    stream.write(b" ")
            elif change == "deadline":
                now[0] = pd.Timestamp("2026-09-09T11:00Z")
            elif change == "binding":
                bound[0] = False
            else:
                pointer.write_bytes(foreign)
    monkeypatch.setattr(planning, "_write_json", write_then_change)
    with pytest.raises(ValueError, match="changed"):
        _publish_direction_plan(case.root, publication, pd.read_parquet(case.source / "forecasts.parquet"),
            observed=now[0], deadline_at=pd.Timestamp("2026-09-09T11:00Z"), clock=lambda: now[0],
            expected_source_receipt_sha256=source_hash, selection_guard=lambda: bound[0])
    assert pointer.read_bytes() == (foreign if change.startswith("foreign") else before)
    assert any(json.loads(path.read_text())["status"] == "FAILED" for path in prior.parent.glob("*/receipt.json"))


def test_current_refresh_without_account_observation_reuses_source_and_records_provenance(publication_case, monkeypatch):
    from ml import gameplan_trade_snapshot
    from ml.artifacts import file_checksum
    case = publication_case
    first = publish_trade_plan(case.root, gameplan_run=case.source,
        snapshot_loader=lambda *a, **kw: {"status": "UNAVAILABLE"}, clock=case.clock)
    original_hash = file_checksum(first / "receipt.json")
    def forbidden(*args, **kwargs):
        pytest.fail("A current informational refresh must reuse its original optional context")
    monkeypatch.setattr(gameplan_trade_snapshot, "capture_trade_planning_snapshot", forbidden)
    second = publish_trade_plan(case.root, gameplan_run=case.source, refresh_plan=first,
        clock=lambda: pd.Timestamp("2026-09-09T14:00Z"))
    report = json.loads((second / "report.json").read_text())
    assert report["deadline_at"] == "2026-09-09T11:00:00+00:00"
    assert report["effective_deadline_at"] == "2026-09-10T00:00:00+00:00"
    assert report["review_refresh"]["base_receipt_sha256"] == original_hash
    assert report["snapshot"] is None and not (second / "account-snapshot.json").exists()
    assert file_checksum(first / "receipt.json") == original_hash


def test_current_late_preparation_retains_original_and_effective_deadline_evidence(publication_case):
    from test_preparation_deadline import exception_record
    case = publication_case
    record, _ = exception_record(case.root, case.source, session="2026-09-09")
    run = publish_trade_plan(case.root, gameplan_run=case.source, deadline_exception=record,
        snapshot_loader=lambda *a, **kw: case.state, clock=lambda: pd.Timestamp("2026-09-09T11:20Z"))
    report = json.loads((run / "report.json").read_text())
    assert report["deadline_at"] == "2026-09-09T11:00:00+00:00"
    assert report["effective_deadline_at"] == "2026-09-09T12:00:00+00:00"
    assert report["deadline_exception"]["authorization"]["orders_authorized"] is False


def test_stats_accepts_verified_current_derivation_without_relabelling_scientific_history(publication_case):
    from ml.nightly_gameplan import read_gameplan_run
    from ml.gameplan_actuals_review import _saved_trade_plan, compare_forecasts, compare_price_points
    from test_gameplan_actuals_review import prices
    case = publication_case
    science = pd.read_parquet(case.source / "forecasts.parquet")
    science = science.assign(calibrated_probability=.48, direction="NO_EDGE", model_status="RESEARCH_NOT_PROMOTED")
    science.to_parquet(case.source / "forecasts.parquet", index=False)
    before = (case.source / "forecasts.parquet").read_bytes()
    run = publish_trade_plan(case.root, gameplan_run=case.source,
        snapshot_loader=lambda *a, **kw: {"status": "UNAVAILABLE"}, clock=case.clock)
    selected, trades, path = _saved_trade_plan(case.root, read_gameplan_run(case.root, case.source))
    assert selected == run and path is None and trades.direction.eq("BEARISH").all()
    outcomes = compare_forecasts(science, prices(), observed_at="2026-09-10T00:00Z", trade_rows=trades)
    assert outcomes.direction.eq("NO_EDGE").all() and outcomes.execution_direction.eq("BEARISH").all()
    assert outcomes.direction_policy_version.isna().all()
    from ml.stock_direction_policy import STOCK_DIRECTION_POLICY_VERSION
    assert outcomes.execution_direction_policy_version.eq(STOCK_DIRECTION_POLICY_VERSION).all()
    assert outcomes.direction_correct.isna().all()
    comparisons = compare_price_points(science, prices(), observed_at="2026-09-10T00:00Z", action_date="2026-09-09", planning_path=path)
    assert comparisons.planned_price_mid.isna().all()
    assert (case.source / "forecasts.parquet").read_bytes() == before


@pytest.mark.parametrize("field,value", [("source_saved_direction", "BULLISH"),
    ("calibrated_probability", .4), ("direction_down_threshold", .48),
    ("direction_based_action", "BUY"), ("direction", "BULLISH")])
def test_stats_rejects_tampered_current_derivation_or_immutable_probability(field, value):
    from ml.gameplan_actuals_review import _match_trade_rows
    from test_gameplan_actuals_review import forecasts
    science = forecasts().assign(calibrated_probability=.48, direction="NO_EDGE", model_status="RESEARCH_NOT_PROMOTED")
    trades, _ = direction_only_rows(science, action_date="2026-09-09")
    trades.loc[trades.index[0], field] = value
    with pytest.raises(ValueError, match="changed a frozen forecast"):
        _match_trade_rows(science, trades)


def test_price_free_producer_handoff_composition_and_ui_keep_real_probabilities(saved, monkeypatch):
    from app.ui.gameplan_data import load_gameplan, _forecast, _direction_actions
    from ml import joint_capital_handoff
    case = saved
    # Existing immutable scientific outputs may contain an old abstaining label.
    case.frame["calibrated_probability"] = .48
    case.frame["direction"] = "NO_EDGE"
    case.frame["model_status"] = "RESEARCH_NOT_PROMOTED"
    case.frame.to_parquet(case.game / "forecasts.parquet", index=False)
    rebind(case)
    before = {p: p.read_bytes() for p in (case.game / "receipt.json", case.game / "manifest.json", case.game / "forecasts.parquet")}
    publication = SimpleNamespace(run_directory=case.game, manifest=case.gm, receipt=case.gr)
    native = _publish_direction_plan(case.root, publication, case.frame,
        observed=pd.Timestamp(FROZEN), deadline_at=pd.Timestamp(f"{DAY}T11:00Z"), clock=lambda: pd.Timestamp(CREATED))
    read = joint_capital_handoff._read
    def checked(path, *args, **kwargs):
        assert path.name not in {"planning-price-path.json", "price-bands.json", "model.joblib", "account-snapshot.json"}
        return read(path, *args, **kwargs)
    monkeypatch.setattr(joint_capital_handoff, "_read", checked)
    export = export_owner_package(case.root, gameplan_run=case.game, trade_plan_run=native,
        owner_id="scout", output_root=case.out, created_at=CREATED)
    payload = json.loads(export.read_text())
    scout = load_owner_package(case.out, export, expected_sha256=payload["package_sha256"])
    assert "price_path" not in scout and "price_path_sha256" not in scout["source_hashes"]
    atlas_rows = [{**row, "id": "atlas:" + row["id"], "symbol": "AAPL"} for row in scout["forecasts"]]
    atlas = build_owner_package(owner_id="atlas", run_id="atlas-fixture", source_revision=None,
        action_date=DAY, frozen_symbols=["AAPL"], created_at=CREATED,
        source_hashes=scout["source_hashes"], forecasts=atlas_rows, direction_only=True)
    packages = [scout, atlas]
    plan = compose_joint_plan(packages, action_date=DAY,
        expected_universes={p["owner_id"]: p["frozen_symbols"] for p in packages},
        expected_package_sha256={p["owner_id"]: p["package_sha256"] for p in packages},
        account_scope_sha256="a" * 64, as_of=CREATED, executor_owner="atlas")
    assert is_current_gameplan_recipe(plan) and "price_path" not in plan
    assert plan["ledger"]["status"] == "DIRECTION_ONLY" and plan["ledger"]["events"] == []
    calls = tuple(_forecast(row) for row in plan["forecasts"])
    assert _direction_actions(calls)
    assert all(row.action == "SELL" and row.quantity is None for row in calls if row.eligible)
    assert all(row.probability == .48 for row in calls)
    from ml.artifacts import file_checksum
    ui = load_gameplan(case.root, session=DAY, run_directory=native,
                       expected_receipt_sha256=file_checksum(native / "receipt.json"))
    assert ui.projection_status == "DIRECTION_ONLY" and ui.projection_available
    assert ui.actions and all(row.quantity is None for row in ui.actions)
    from app.ui import gameplan_data
    monkeypatch.setattr(gameplan_data, "_account_pointer", lambda *a, **kw: pytest.fail("Legacy account selection cannot shadow current local directions"))
    assert load_gameplan(case.root).run_directory == native
    assert {p: p.read_bytes() for p in before} == before


def test_observed_quotes_and_holdings_are_context_without_estimated_quantities():
    frame = pd.DataFrame([dict(id="observed", symbol="TEST", model_group="1h", execution_eligible=True,
        calibrated_probability=.48, direction="BEARISH", model_status="PROMOTED")])
    state = {"observed_at": "2026-10-20T10:00Z", "held_shares": {"TEST": 12},
        "pending_sell_shares": {"TEST": 2}, "quotes": {"TEST": {"bid": 9., "ask": 11., "mark": 10.}}}
    rows, _ = direction_only_rows(frame, action_date="2026-10-20", snapshot=state)
    assert rows.iloc[0].observed_available_account_shares == 10
    assert rows.iloc[0].observed_bid == 9 and rows.iloc[0].observed_ask == 11
    assert rows.iloc[0].direction_based_action == "SELL" and pd.isna(rows.iloc[0].direction_based_trade_quantity)


def test_saved_estimated_plan_is_history_and_new_revision_uses_only_its_predictions():
    from ml.joint_capital_plan import _compose_legacy_joint_plan, direction_plan_from_saved_plan, _validate_plan_digest
    from test_joint_capital_plan import package, snapshot, NOW, SCOPE
    packages = [package(), package("atlas", "AAPL", .7)]
    legacy = _compose_legacy_joint_plan(packages, snapshot(), action_date=DAY,
        expected_universes={p["owner_id"]: p["frozen_symbols"] for p in packages},
        expected_package_sha256={p["owner_id"]: p["package_sha256"] for p in packages},
        account_scope_sha256=SCOPE, as_of=NOW, executor_owner="atlas")
    original = deepcopy(legacy)
    current = direction_plan_from_saved_plan(legacy, as_of=CREATED)
    _validate_plan_digest(current, current["plan_sha256"])
    assert legacy == original and current["source_plan_sha256"] == legacy["plan_sha256"]
    assert is_current_gameplan_recipe(current) and current["ledger"]["events"] == []
    assert "price_path" not in current and "source_price_paths" not in current
    assert [r["calibrated_probability"] for r in current["forecasts"]] == [r["calibrated_probability"] for r in legacy["forecasts"]]
    assert all(not any(k.startswith(("trade_", "projected_", "planning_")) for k in row)
               for row in current["forecasts"])


def test_saved_no_history_label_remains_truthful_without_suppressing_current_probability():
    frame = pd.DataFrame([dict(id="no-history", symbol="TEST", model_group="1h", execution_eligible=True,
        calibrated_probability=.48, direction="NO_EDGE", model_status="RESEARCH_NO_TARGET_HISTORY",
        target_contract_version="independent-stock-targets-v1", symbol_fitted_target_rows=0,
        symbol_route_fitted_target_rows=0)])
    rows, _ = direction_only_rows(frame, action_date="2026-10-20")
    assert rows.iloc[0].source_saved_direction == "NO_EDGE"
    assert rows.iloc[0].calibrated_probability == .48
    assert rows.iloc[0].direction == "BEARISH" and rows.iloc[0].direction_based_action == "SELL"
    assert frame.iloc[0].direction == "NO_EDGE"


def test_current_review_refresh_never_requires_a_planning_price_file(publication_case):
    case = publication_case
    def forbidden(*args, **kwargs):
        pytest.fail("Refresh must not estimate prices")
    first = publish_trade_plan(case.root, gameplan_run=case.source,
        snapshot_loader=lambda *a, **kw: case.state, price_loader=forbidden, clock=case.clock)
    second = publish_trade_plan(case.root, gameplan_run=case.source, refresh_plan=first,
        price_loader=forbidden, clock=lambda: pd.Timestamp("2026-09-09T14:00Z"))
    assert first != second and not list(second.glob("*price*.json"))
    assert is_current_gameplan_recipe(json.loads((second / "receipt.json").read_text()))


def test_retired_default_selection_requires_explicit_history_date(tmp_path):
    from gameplan_fixture import write_plan
    from app.ui.gameplan_data import load_gameplan, GameplanError
    write_plan(tmp_path, session="2026-10-19")
    with pytest.raises(GameplanError, match="retired estimated-price recipe"):
        load_gameplan(tmp_path)
    assert load_gameplan(tmp_path, "2026-10-19").session == "2026-10-19"
