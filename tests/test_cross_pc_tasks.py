from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from tools.cross_pc import tasks


CATALOG_PATH = Path(__file__).resolve().parents[1] / "coordination" / "task-catalog.json"
BINDING = {"release_root": "C:/local/coordination/releases/abc123",
           "profile_path": "C:/local/coordination/profile.json", "machine": "pc-original"}


def purpose(key):
    return next(item for item in tasks.load_catalog(CATALOG_PATH)["tasks"] if item["key"] == key)


def native(kind="cron", status="ACTIVE"):
    value = {"version": 1, "id": "existing-task", "kind": kind, "name": "Keep this name",
             "prompt": "Original operating scope.\nAdaptive win +1, loss -2 min 1.  \n",
             "status": status, "rrule": "FREQ=HOURLY;INTERVAL=3", "created_at": 100,
             "updated_at": 200, "notification_policy": "failed_runs_only"}
    if kind == "cron":
        value.update(model="gpt-6-astra", reasoning_effort="ultra", execution_environment="local",
                     target={"type": "project", "project_id": "private-existing-project"},
                     cwds=["C:/original/app", "D:/private-data"])
    else:
        value["target_thread_id"] = "original-communication-chat"
    return value


def after_update(before, update):
    after = copy.deepcopy(before)
    after.update(prompt=update["prompt"], updated_at=201)
    return after


def test_cron_adoption_preserves_full_definition_and_adaptive_prompt():
    saved = native()
    unchanged = copy.deepcopy(saved)
    update = tasks.build_update(saved, purpose("hyperliquid_paper_improvement"), **BINDING)
    assert saved == unchanged
    assert update["prompt"].startswith(saved["prompt"] + "\n\n")
    assert update["rrule"] == saved["rrule"]
    assert update["status"] == "ACTIVE"
    assert update["model"] == "gpt-6-astra"
    assert update["reasoningEffort"] == "ultra"
    assert update["projectId"] == "private-existing-project"
    assert update["executionEnvironment"] == "local"
    assert update["notificationPolicy"] == "failed_runs_only"
    assert update["mode"] == "update"
    assert "temporary worktree" in update["prompt"]
    assert 'tools/cross_pc/cli.py" --profile' in update["prompt"]
    assert 'run --purpose hyperliquid_paper_improvement --outcome' in update["prompt"]
    assert 'queue --source' in update["prompt"]
    assert 'Do not enqueue both v1 and v2' in update["prompt"]
    result = tasks.verify_adoption(saved, after_update(saved, update), update)
    assert result["verified"] is True


def test_heartbeat_keeps_chat_continuity_exact_and_inherited_model():
    saved = native("heartbeat")
    saved["prompt"] += (tasks.CONTINUITY_BEGIN + "\\nOnly migrate the recorded transaction; "
                        "keep the existing target and readiness gate.\\n" + tasks.CONTINUITY_END)
    update = tasks.build_update(saved, purpose("cross_pc_inbox"), **BINDING)
    assert tasks.continuity_block(update["prompt"]) == tasks.continuity_block(saved["prompt"])
    assert update["targetThreadId"] == saved["target_thread_id"]
    assert update["destination"] == "thread"
    assert "model" not in update and "reasoningEffort" not in update
    assert "Immediately queue" not in update["prompt"]
    assert tasks.verify_adoption(saved, after_update(saved, update), update)["verified"]


def test_local_notification_policy_overrides_preserved_routing_without_changing_task_settings():
    saved = native()
    saved["prompt"] += "Historical routing: always read and update the Drive signal.\n"
    unchanged = copy.deepcopy(saved)
    update = tasks.build_update(saved, purpose("source_courier"), **BINDING)
    prompt = update["prompt"]
    assert prompt.startswith(saved["prompt"] + "\n\n")
    assert "coordination_notification_policy" in prompt
    assert "github_only uses GitHub and requires no Drive signal" in prompt
    assert "git_and_drive (the legacy default when absent)" in prompt
    assert "make no Drive API or drive-* calls" in prompt
    assert "supersedes older notification-routing instructions" in prompt
    assert "without claiming Drive delivery" in prompt
    assert "does not change the separate private financial exchange" in prompt
    assert "Preserve request and incoming-source work independently" in prompt
    assert saved == unchanged
    assert tasks.verify_adoption(saved, after_update(saved, update), update)["verified"]


def test_suffix_replacement_is_idempotent_and_preserves_trailing_whitespace():
    saved = native()
    task = purpose("overnight_gameplan")
    first = tasks.build_update(saved, task, **BINDING)
    second = tasks.build_update(after_update(saved, first), task, **BINDING)
    assert first == second
    new_binding = dict(BINDING, release_root="C:/local/coordination/releases/def456")
    third = tasks.build_update(after_update(saved, first), task, **new_binding)
    assert third["prompt"].startswith(saved["prompt"] + "\n\n")
    assert third["prompt"].count(tasks.BEGIN) == 1
    assert "abc123" not in third["prompt"]


@pytest.mark.parametrize("key", ["options_paper_tracking", "hyperliquid_operations_watch",
                                 "historical_gameplan_review"])
def test_paused_purposes_stay_paused(key):
    saved = native(status="PAUSED")
    update = tasks.build_update(saved, purpose(key), **BINDING)
    assert update["status"] == "PAUSED"
    assert "remains paused" in update["prompt"]
    with pytest.raises(ValueError, match="PAUSED"):
        tasks.build_update(native(), purpose(key), **BINDING)


@pytest.mark.parametrize("key", ["stock_daytime_supervision", "hyperliquid_paper_improvement",
                                 "hyperliquid_operations_watch", "overnight_gameplan"])
def test_scout_owner_restricted_counterparts_cannot_gain_operator_authority(key):
    binding = dict(BINDING, machine="pc-new")
    task = purpose(key)
    update = tasks.build_update(native(), task, **binding)
    assert "role observer" in update["prompt"]
    assert "must not modify application source" in update["prompt"]
    with pytest.raises(ValueError, match="role ceiling"):
        tasks.build_update(native(), task, role="operator", **binding)


def test_expired_one_time_definition_is_not_reactivated_or_recreated():
    saved = native("heartbeat")
    saved["rrule"] = "FREQ=DAILY;COUNT=1;BYHOUR=17;BYMINUTE=10;BYSECOND=0"
    update = tasks.build_update(saved, purpose("historical_fallback_installation"), **BINDING)
    assert update["rrule"] == saved["rrule"]
    assert update["status"] == saved["status"]
    assert update["mode"] == "update"
    assert "must not be recreated or replayed" in update["prompt"]


@pytest.mark.parametrize("field,value", [
    ("status", "PAUSED"), ("rrule", "FREQ=MINUTELY;INTERVAL=1"),
    ("model", "another-model"), ("reasoning_effort", "low"),
    ("notification_policy", None), ("cwds", ["C:/wrong-context"]),
    ("target", {"type": "project", "project_id": "wrong-project"}),
])
def test_post_update_verification_detects_scope_context_or_schedule_drift(field, value):
    before = native()
    update = tasks.build_update(before, purpose("overnight_gameplan"), **BINDING)
    after = after_update(before, update)
    after[field] = value
    with pytest.raises(ValueError, match="Native fields changed"):
        tasks.verify_adoption(before, after, update)


def test_prompt_verification_detects_truncation():
    before = native()
    update = tasks.build_update(before, purpose("overnight_gameplan"), **BINDING)
    after = after_update(before, update)
    after["prompt"] = after["prompt"][:100]
    with pytest.raises(ValueError, match="reviewed update"):
        tasks.verify_adoption(before, after, update)


@pytest.mark.parametrize("fragment", [tasks.BEGIN, tasks.END, tasks.END + tasks.BEGIN])
def test_malformed_suffix_is_preserved_for_review(fragment):
    saved = native()
    saved["prompt"] += fragment
    with pytest.raises(ValueError, match="[Ss]hared-contract"):
        tasks.build_update(saved, purpose("overnight_gameplan"), **BINDING)


def test_unknown_native_fields_fail_instead_of_silently_dropping_them():
    saved = native()
    saved["future_context_rules"] = "must preserve"
    with pytest.raises(ValueError, match="Unmapped native fields"):
        tasks.build_update(saved, purpose("overnight_gameplan"), **BINDING)


@pytest.mark.parametrize("release", ["relative/releases/version", "C:/local/.codex/worktrees/test",
                                   "C:/Temp/release", "C:/local/../release"])
def test_release_binding_rejects_ephemeral_or_unresolved_paths(release):
    with pytest.raises(ValueError):
        tasks.build_update(native(), purpose("overnight_gameplan"),
                           **dict(BINDING, release_root=release))


def test_portable_catalog_has_all_audited_purposes_and_no_native_identity():
    catalog = tasks.load_catalog(CATALOG_PATH)
    assert len(catalog["tasks"]) == 15
    serialized = json.dumps(catalog)
    assert "atlas-codexstore-inbox" not in serialized
    assert "c899e194" not in serialized
    assert "C:/" not in serialized
    assert "human-relayed" in catalog["evidence"]["pc-new"]
    for key, value in [("native_id", "private"), ("profile_path", "C:/private/profile")]:
        bad = copy.deepcopy(catalog)
        bad["tasks"][0][key] = value
        with pytest.raises(ValueError, match="Private native"):
            tasks.validate_catalog(bad)


def test_plan_reports_missing_tasks_without_creating_them_or_touching_storage(tmp_path):
    native_root = tmp_path / "native"
    task_dir = native_root / "existing-task"
    task_dir.mkdir(parents=True)
    saved = native("heartbeat")
    source = "\n".join(f"{key} = {json.dumps(value)}" for key, value in saved.items()) + "\n"
    path = task_dir / "automation.toml"
    path.write_text(source, encoding="utf-8")
    before_bytes = path.read_bytes()
    bindings = {"existing-task": "cross_pc_inbox", "missing-task": "source_courier"}
    plan = tasks.adopt_plan(native_root, CATALOG_PATH, bindings, **BINDING)
    assert len(plan["updates"]) == 1
    assert plan["updates"][0]["before"] == saved
    assert plan["missing"] == [{"id": "missing-task", "purpose": "source_courier",
                                "action": "no_creation"}]
    assert path.read_bytes() == before_bytes
    assert not (native_root / "missing-task").exists()


def test_catalog_rejects_duplicate_json_keys(tmp_path):
    path = tmp_path / "bad.json"
    path.write_text('{"schema_version":1,"schema_version":2}', encoding="utf-8")
    with pytest.raises(ValueError, match="Duplicate JSON key"):
        tasks.load_catalog(path)
