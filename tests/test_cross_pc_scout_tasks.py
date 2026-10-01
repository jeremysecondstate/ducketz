"""Synthetic Scout heartbeat adoption, independent of native scheduler storage."""
from copy import deepcopy
from pathlib import Path

import pytest

from tools.cross_pc import tasks


CATALOG = Path(__file__).resolve().parents[1] / "coordination" / "task-catalog.json"
BINDING = {
    "release_root": "C:/fixture/coordination/releases/reviewed-release",
    "profile_path": "C:/fixture/coordination/local-profile.json",
    "machine": "pc-new",
}
OPERATING_CHAT = "00000000-0000-4000-8000-000000000001"
DEDICATED_CHAT = "00000000-0000-4000-8000-000000000002"


@pytest.fixture
def scout_definitions():
    """The five local task shapes, with fictional IDs, paths and prompt content."""
    purposes = (
        ("cross_pc_inbox", "observer", "ACTIVE", 5, OPERATING_CHAT,
         "Use the reviewed Git inbox migration. Preserve legacy receipts.\n"
         "After the inbox check, read the dedicated completion chat once; retain its separate receipts."),
        ("source_courier", "publisher", "ACTIVE", 5, OPERATING_CHAT,
         "Publish one reviewed tested source snapshot and one independent pending notice.\n"
         "Preserve already-completed source stages and all legacy bundles."),
        ("communication_continuity", "continuity", "ACTIVE", 15, OPERATING_CHAT,
         "Use Scout's independent activity-based continuity helper.\n"
         "After three compactions prepare a handoff and capture the four existing task templates.\n"
         "Transfer only inbox, courier, bootstrap completion and this continuity task.\n"
         "Archive only after readiness and exact field verification; preserve an in-progress transaction.  \n"),
        ("bootstrap_completion", "disabled", "PAUSED", 10, OPERATING_CHAT,
         "Retain the handled terminal bootstrap outcomes and separate local receipts.\n"
         "Observe only the named runs if separately resumed; never start a worker."),
        ("data_fetch_completion", "disabled", "PAUSED", 10, DEDICATED_CHAT,
         "Keep the dedicated completion chat and all superseded terminal outcomes.\n"
         "If separately resumed, report one brief progress line every run, including unchanged progress.\n"
         "Current and prior run counters stay separate; pause only this task after handled terminal results."),
    )
    return [
        {
            "purpose": purpose, "role": role,
            "saved": {
                "version": 1, "id": f"fixture-{purpose}", "kind": "heartbeat",
                "name": f"Synthetic Scout {purpose}", "prompt": prompt,
                "status": status, "rrule": f"FREQ=MINUTELY;INTERVAL={minutes}",
                "target_thread_id": target, "created_at": 100, "updated_at": 200,
            },
        }
        for purpose, role, status, minutes, target, prompt in purposes
    ]


def catalog_task(purpose):
    return next(item for item in tasks.load_catalog(CATALOG)["tasks"] if item["key"] == purpose)


def adopted(before, update):
    result = deepcopy(before)
    result.update(prompt=update["prompt"], updated_at=201)
    return result


def test_five_scout_heartbeats_preserve_local_definitions_and_inherited_settings(scout_definitions):
    for item in scout_definitions:
        before = deepcopy(item["saved"])
        update = tasks.build_update(item["saved"], catalog_task(item["purpose"]), **BINDING)
        assert item["saved"] == before
        assert update["prompt"].startswith(before["prompt"] + "\n\n")
        assert f"local machine pc-new; role {item['role']}." in update["prompt"]
        assert update["prompt"].count(tasks.BEGIN) == 1
        assert update["rrule"] == before["rrule"]
        assert update["status"] == before["status"]
        assert update["targetThreadId"] == before["target_thread_id"]
        assert update["destination"] == "thread"
        assert {"model", "reasoningEffort", "notificationPolicy", "projectId", "executionEnvironment"}.isdisjoint(update)
        after = adopted(before, update)
        assert tasks.preserved_definition(after) == tasks.preserved_definition(before)
        assert tasks.verify_adoption(before, after, update)["verified"]


def test_scout_standalone_continuity_and_periodic_completion_reporting_remain_exact(scout_definitions):
    by_purpose = {item["purpose"]: item for item in scout_definitions}
    for purpose in ("communication_continuity", "bootstrap_completion", "data_fetch_completion"):
        before = by_purpose[purpose]["saved"]
        update = tasks.build_update(before, catalog_task(purpose), **BINDING)
        # Scout has no embedded Atlas block. Compare its whole base prompt instead.
        assert tasks.continuity_block(before["prompt"]) is None
        assert update["prompt"][:len(before["prompt"])] == before["prompt"]
        assert update["prompt"][len(before["prompt"]):].startswith("\n\n" + tasks.BEGIN)
    continuity = by_purpose["communication_continuity"]["saved"]
    dedicated = by_purpose["data_fetch_completion"]["saved"]
    assert continuity["target_thread_id"] == OPERATING_CHAT
    assert dedicated["target_thread_id"] == DEDICATED_CHAT
    assert "dedicated completion" not in continuity["prompt"]
    completion_update = tasks.build_update(dedicated, catalog_task("data_fetch_completion"), **BINDING)
    assert "one brief progress line every run, including unchanged progress" in completion_update["prompt"]
    assert "Preserve requested periodic reports" in completion_update["prompt"]


@pytest.mark.parametrize("purpose", ["bootstrap_completion", "data_fetch_completion"])
def test_scout_disabled_completion_requires_preserved_pause(scout_definitions, purpose):
    before = next(item["saved"] for item in scout_definitions if item["purpose"] == purpose)
    update = tasks.build_update(before, catalog_task(purpose), **BINDING)
    assert update["status"] == "PAUSED"
    assert "This purpose remains paused" in update["prompt"]
    assert "Immediately queue" not in update["prompt"]
    active = deepcopy(before)
    active["status"] = "ACTIVE"
    with pytest.raises(ValueError, match="saved PAUSED"):
        tasks.build_update(active, catalog_task(purpose), **BINDING)


def test_scout_task_roles_cannot_gain_operator_authority(scout_definitions):
    for item in scout_definitions:
        with pytest.raises(ValueError, match="role ceiling"):
            tasks.build_update(item["saved"], catalog_task(item["purpose"]), role="operator", **BINDING)
        update = tasks.build_update(item["saved"], catalog_task(item["purpose"]), **BINDING)
        assert ("Immediately queue" in update["prompt"]) == (item["role"] == "publisher")


def test_scout_readback_rejects_dedicated_target_merge_and_invented_model(scout_definitions):
    before = next(item["saved"] for item in scout_definitions if item["purpose"] == "data_fetch_completion")
    update = tasks.build_update(before, catalog_task("data_fetch_completion"), **BINDING)
    wrong_target = adopted(before, update)
    wrong_target["target_thread_id"] = OPERATING_CHAT
    with pytest.raises(ValueError, match="target_thread_id"):
        tasks.verify_adoption(before, wrong_target, update)
    invented_model = adopted(before, update)
    invented_model["model"] = "fixture-model-not-inherited"
    with pytest.raises(ValueError, match="model"):
        tasks.verify_adoption(before, invented_model, update)
