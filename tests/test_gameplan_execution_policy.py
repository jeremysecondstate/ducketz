"""Offline durable policy metadata; all publication fixtures are synthetic."""
from copy import deepcopy
import json

import pandas as pd
import pytest

from ml import nightly_gameplan
from ml.gameplan_execution_policy import current_gameplan_policy_metadata, saved_gameplan_policy_metadata
from ml.gameplan_trade_planning import publish_trade_plan
from ml.joint_capital_handoff import export_owner_package
from test_stock_only_gameplan import publication_fixture
from test_gameplan_trade_planning import publication_case
from test_joint_capital_handoff import saved, rebind, CREATED


def read(path):
    return json.loads(path.read_text(encoding="utf-8"))


@pytest.mark.parametrize("day,autonomous", [
    ("2026-10-18", ["15m"]), ("2026-10-19", ["15m", "1h", "4h"]),
])
def test_policy_records_exact_inclusive_cutoffs_and_dated_ownership(day, autonomous):
    metadata = current_gameplan_policy_metadata(day)
    assert metadata["direction_policy_version"] == "stock-direction-5001-4999-v3"
    assert metadata["direction_up_threshold"] == .5001
    assert metadata["direction_down_threshold"] == .4999
    policy = metadata["stock_execution_policy"]
    assert (policy["buy_comparison"], policy["sell_comparison"]) == (">=", "<=")
    assert policy["probability_precision"] == "unrounded"
    assert policy["hold_comparison"] == "strictly_between_thresholds"
    assert policy["autonomous_horizons"] == autonomous
    assert policy["unchanged_horizons"] == ["1d", "1w"]
    assert policy["reserved_sell_shares"] == "unavailable"
    assert policy["price_basis"] == "live_bid_ask_midpoint"


def test_saved_metadata_copy_does_not_assign_current_policy_to_history():
    assert saved_gameplan_policy_metadata({"symbols": ["AAPL"]}) == {}
    legacy = {"direction_policy_version": "stock-direction-50-v2", "direction_up_threshold": .5,
              "direction_down_threshold": .5}
    assert saved_gameplan_policy_metadata(legacy) == legacy
    original = current_gameplan_policy_metadata("2026-10-19")
    copied = saved_gameplan_policy_metadata(original)
    copied["stock_execution_policy"]["autonomous_horizons"].clear()
    assert original["stock_execution_policy"]["autonomous_horizons"] == ["15m", "1h", "4h"]


def test_new_nightly_manifest_plan_receipt_and_rows_preserve_exact_policy(publication_fixture):
    case = publication_fixture
    result = nightly_gameplan.run_nightly_gameplan_once(case.root, stock_only=True, reporter=None)
    run = result.run_directory
    expected = current_gameplan_policy_metadata(result.action_date)
    for payload in (read(run / "gameplan.json"), read(run / "manifest.json")["configuration"], read(run / "receipt.json")):
        assert saved_gameplan_policy_metadata(payload) == expected
    rows = pd.read_parquet(run / "forecasts.parquet")
    for name in ("direction_policy_version", "direction_up_threshold", "direction_down_threshold"):
        assert rows[name].eq(expected[name]).all()
    assert read(run / "receipt.json")["orders_placed"] == 0


def test_new_trade_plan_copies_policy_to_every_audit_artifact_without_source_mutation(publication_case):
    case = publication_case
    before = {p: p.read_bytes() for p in case.source.iterdir()}
    run = publish_trade_plan(case.root, gameplan_run=case.source,
        snapshot_loader=lambda *a, **kw: case.state, price_loader=case.prices, clock=case.clock)
    expected = current_gameplan_policy_metadata("2026-09-09")
    for payload in (read(run / "report.json"), read(run / "manifest.json")["configuration"],
                    read(run / "receipt.json"), read(run / "direction-ledger.json")):
        assert saved_gameplan_policy_metadata(payload) == expected
    assert {p: p.read_bytes() for p in before} == before


def test_owner_export_preserves_optional_forward_policy_fields(saved):
    case = saved
    metadata = current_gameplan_policy_metadata("2026-09-09")
    frame = case.frame.copy()
    for name in ("direction_policy_version", "direction_up_threshold", "direction_down_threshold"):
        frame[name] = metadata[name]
    frame.to_parquet(case.game / "forecasts.parquet", index=False)
    rebind(case)
    original = {p: p.read_bytes() for p in (case.game / "receipt.json", case.game / "forecasts.parquet")}
    path = export_owner_package(case.root, gameplan_run=case.game, trade_plan_run=case.trade,
        owner_id="scout", output_root=case.out, created_at=CREATED)
    rows = read(path)["forecasts"]
    for name in ("direction_policy_version", "direction_up_threshold", "direction_down_threshold"):
        assert all(row[name] == metadata[name] for row in rows)
    assert {p: p.read_bytes() for p in original} == original
