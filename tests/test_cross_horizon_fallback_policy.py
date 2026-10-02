import json
from copy import deepcopy

import pandas as pd
import pytest

from ml.artifacts import file_checksum, write_manifest
from ml.stock_trader.cross_horizon_fallback import (
    POLICY_FIELD, donor_horizons, policy_for_action_date,
    read_gameplan_fallback_policy, slot_index, slot_quota, validate_fallback_policy,
)


DAY = "2026-10-01"


def slots(day=DAY):
    for hour in range(4, 17):
        for horizon in ("1h", "4h", "1d"):
            if horizon == "1h" or (horizon == "4h" and hour in (4, 8, 12, 16)) or (horizon == "1d" and hour == 4):
                yield horizon, pd.Timestamp(f"{day} {hour:02d}:00", tz="America/Los_Angeles")


def publication(tmp_path, policy=True):
    metadata = {"action_date": DAY}
    if policy:
        metadata[POLICY_FIELD] = policy_for_action_date(DAY)
    (tmp_path / "gameplan.json").write_text(json.dumps(metadata))
    write_manifest(tmp_path, run_timestamp="2026-10-01T08:00Z", input_files=(),
                   output_files=("gameplan.json",), configuration=metadata)
    (tmp_path / "receipt.json").write_text(json.dumps({**metadata,
        "manifest_checksum_sha256": file_checksum(tmp_path / "manifest.json")}))
    return tmp_path


def test_prospective_policy_and_independent_values(tmp_path):
    assert policy_for_action_date("2026-09-30") is None
    assert read_gameplan_fallback_policy(tmp_path / "does-not-exist", "2026-09-30") is None
    policy = policy_for_action_date(DAY)
    policy["daily_cap_numerator"] = 99
    assert policy_for_action_date(DAY)["daily_cap_numerator"] == 1
    with pytest.raises(ValueError):
        validate_fallback_policy(policy, DAY)
    with pytest.raises(ValueError):
        validate_fallback_policy(policy_for_action_date(DAY), "2026-09-30")


def test_exact_hierarchy_and_no_reverse_edges():
    assert donor_horizons("1h") == ("4h", "1d", "1w")
    assert donor_horizons("4h") == ("1d", "1w")
    assert donor_horizons("1d") == ("1w",)
    assert donor_horizons("1w") == ()
    with pytest.raises(ValueError):
        donor_horizons("2h")


def test_all_weighted_quotas_conserve_budget_for_small_and_large_holdings():
    for budget in range(1001):
        quotas = [slot_quota(budget, h, start, DAY) for h, start in slots()]
        assert len(quotas) == 18
        assert sum(quotas) == budget
        assert min(quotas) >= 0
    quotas = [(h, slot_quota(150, h, start, DAY)) for h, start in slots()]
    assert set(q for h, q in quotas if h == "1h") <= {6, 7}
    assert set(q for h, q in quotas if h == "4h") <= {12, 13}
    assert [q for h, q in quotas if h == "1d"] == [19]


@pytest.mark.parametrize("day", [DAY, "2026-11-03"])
def test_grid_is_pacific_dst_aware(day):
    assert [slot_index(h, start.tz_convert("UTC"), day) for h, start in slots(day)] == list(range(18))
    assert sum(slot_quota(155, h, start, day) for h, start in slots(day)) == 155


@pytest.mark.parametrize("horizon,start", [
    ("4h", "2026-10-01T12:00Z"), ("1h", "2026-10-01T11:01Z"),
    ("1h", "2026-10-02T11:00Z"), ("1h", "2026-10-01 04:00"),
    ("1d", "2026-10-01T15:00Z"),
])
def test_non_entry_and_mismatched_clocks_cannot_create_quota(horizon, start):
    with pytest.raises(ValueError):
        slot_quota(150, horizon, start, DAY)


def test_source_bound_policy_and_legacy_absence(tmp_path):
    run = publication(tmp_path)
    assert read_gameplan_fallback_policy(run, DAY) == policy_for_action_date(DAY)
    publication(tmp_path, policy=False)
    assert read_gameplan_fallback_policy(run, DAY) is None


@pytest.mark.parametrize("target", ["gameplan.json", "receipt.json", "manifest.json"])
def test_source_policy_tampering_rejected(tmp_path, target):
    run = publication(tmp_path)
    path = run / target
    data = json.loads(path.read_text())
    (data["configuration"] if target == "manifest.json" else data)[POLICY_FIELD]["daily_cap_numerator"] = 2
    path.write_text(json.dumps(data))
    with pytest.raises((RuntimeError, ValueError)):
        read_gameplan_fallback_policy(run, DAY)


def test_receipt_policy_removal_and_wrong_date_rejected(tmp_path):
    run = publication(tmp_path)
    path = run / "receipt.json"
    data = json.loads(path.read_text())
    del data[POLICY_FIELD]
    path.write_text(json.dumps(data))
    with pytest.raises(ValueError):
        read_gameplan_fallback_policy(run, DAY)
    with pytest.raises(ValueError):
        read_gameplan_fallback_policy(run, "2026-10-02")


def test_native_receipt_and_both_readers_preserve_prospective_policy(tmp_path):
    from datetime import date
    from ml import nightly_gameplan

    run = tmp_path / "ml/nightly-gameplan-runs/policy-binding-fixture"
    run.mkdir(parents=True)
    publication(run)
    nightly_gameplan._publish_gameplan(tmp_path, run=run, action_date=date.fromisoformat(DAY),
        published_at=pd.Timestamp("2026-10-01T08:01Z"), source_loop_b="fixture", source_strategy=None)
    current = nightly_gameplan.read_current_gameplan(tmp_path)
    saved = nightly_gameplan.read_gameplan_run(tmp_path, run)
    assert current.receipt[POLICY_FIELD] == saved.receipt[POLICY_FIELD] == policy_for_action_date(DAY)
    assert read_gameplan_fallback_policy(run, DAY) == current.receipt[POLICY_FIELD]
