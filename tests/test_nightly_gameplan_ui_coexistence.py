"""Saved legacy account views coexist with the replacement local/joint workflow."""
import pytest

from app.ui.gameplan_data import GameplanError, load_gameplan, plan_sessions
from gameplan_fixture import write_plan
from ml.joint_capital_adoption import accept_joint_plan
from test_account_gameplan_ui import account, pin
from test_account_gameplan_config import write_config
import test_joint_capital_plan as joint_fixture


def joint(root, monkeypatch, day):
    now = day + "T10:00:00Z"
    monkeypatch.setattr(joint_fixture, "DAY", day)
    monkeypatch.setattr(joint_fixture, "NOW", now)
    plan = joint_fixture.compose()
    return accept_joint_plan(root, plan, expected_sha256=plan["plan_sha256"],
        expected_package_sha256={owner: item["package_sha256"] for owner, item in plan["input_bindings"].items()},
        expected_universes={owner: item["frozen_symbols"] for owner, item in plan["input_bindings"].items()},
        account_scope_sha256=joint_fixture.SCOPE, local_actor="atlas", executor_owner="atlas",
        action_date=day, accepted_at=now)


def test_new_local_session_is_visible_without_replacing_legacy_account(account):
    root, legacy = account
    pointer = root / "ml/account-gameplan-latest/run.json"
    original = pointer.read_bytes()
    local = write_plan(root, session="2026-10-06")
    assert load_gameplan(root).run_directory == local
    assert load_gameplan(root, "2026-10-06").run_directory == local
    assert load_gameplan(root, "2026-10-05").run_directory == legacy
    assert plan_sessions(root) == ("2026-10-06", "2026-10-05")
    assert pointer.read_bytes() == original


def test_new_joint_session_wins_its_local_plan_and_older_account(account, monkeypatch):
    root, legacy = account
    write_plan(root, session="2026-10-06")
    accepted = joint(root, monkeypatch, "2026-10-06")
    assert load_gameplan(root).run_directory == accepted
    assert load_gameplan(root, "2026-10-06").run_directory == accepted
    assert load_gameplan(root, "2026-10-05").run_directory == legacy
    assert plan_sessions(root) == ("2026-10-06", "2026-10-05")


def test_stale_joint_cannot_shadow_newer_local_or_account_session(account, monkeypatch):
    root, legacy = account
    old_joint = joint(root, monkeypatch, "2026-10-02")
    write_plan(root, session="2026-10-02")
    assert load_gameplan(root).run_directory == legacy
    local = write_plan(root, session="2026-10-06")
    assert load_gameplan(root).run_directory == local
    assert load_gameplan(root, "2026-10-02").run_directory == old_joint
    assert plan_sessions(root) == ("2026-10-06", "2026-10-05", "2026-10-02")


def test_verified_joint_replaces_missing_legacy_active_display_guard(tmp_path, monkeypatch):
    write_config(tmp_path, status="ACTIVE")
    accepted = joint(tmp_path, monkeypatch, "2026-10-06")
    assert load_gameplan(tmp_path).run_directory == accepted
    assert plan_sessions(tmp_path) == ("2026-10-06",)
    # A later local plan still cannot bypass an active account's missing joint
    # publication using a stale acceptance from the preceding session.
    write_plan(tmp_path, session="2026-10-07")
    with pytest.raises(GameplanError, match="shared account Gameplan"):
        load_gameplan(tmp_path)


def test_new_joint_does_not_hide_a_damaged_legacy_pointer(account, monkeypatch):
    root, legacy = account
    joint(root, monkeypatch, "2026-10-06")
    pin(root, legacy, digest="0" * 64)
    with pytest.raises(GameplanError):
        load_gameplan(root)
