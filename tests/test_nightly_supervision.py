"""Offline continuity regressions; no production state, models or providers."""
import json
from pathlib import Path

import pandas as pd
import pytest

from ml import nightly_exchange_repair as repair
from ml import nightly_workflow as workflow
from tools import nightly_supervision as supervisor
from tests.test_nightly_exchange_repair import env, write, DAY, NOW, claimed, prepared, applied
from tests.test_nightly_stage_repair import env as preparation_env, prepare as prepare_stage, apply as apply_stage
from tests.test_nightly_exchange import exchange, source_inputs, synthesized, wake, DAY as EXCHANGE_DAY, NOW as EXCHANGE_NOW

OWNER = "scout-priority-source-reconciliation"


def inspect(e, **kwargs):
    return supervisor.inspect(e["native"], e["config"], owner=OWNER, action_date=DAY, now=NOW, **kwargs)


def attestation(result, **changes):
    value = {key: result[key] for key in ("actor", "action_date", "incident_id", "automation_id")}
    value.update(repair_id=(result["repository_owner"] or {}).get("repair_id"), thread_id="native-thread",
                 turn_id="native-turn", observed_at=NOW, provenance="current_chat")
    return {**value, **changes}


def test_completed_preparation_never_means_overall_completion(env):
    e = env
    write(e["status"], {"actor": "Scout", "action_date": DAY, "status": "PENDING", "reason": "JOINT_SCOUT"})
    before = {p: p.read_bytes() for p in (e["prep"], e["status"], e["budget"], e["selection"])}
    result = inspect(e)
    assert result["next_action"] == "ADVANCE_EXCHANGE"
    assert result["overall_complete"] is False
    assert result["mutation_authorized"] is False
    assert all(p.read_bytes() == raw for p, raw in before.items())
    assert not (e["work"] / "supervision").exists()


def test_claim_is_resume_work_never_live_owner_proof(env):
    claim = claimed(env)
    result = inspect(env)
    assert result["next_action"] == "CONTINUE_SAME_OWNER"
    assert result["owner"] == "Scout source reconciliation"
    assert result["phase"] == "REPAIR_CLAIMED"
    assert result["repository_owner"] == repair.registry.read(env["work"])
    assert result["phase_evidence"][str(claim)]
    assert result["owner_liveness_attestation"] is None


def test_interrupted_claim_before_evidence_is_retained_not_replaced(env):
    guard = {"owner": "stable original owner", "repair_id": "partial-claim-identity", "completion_record": None,
             "domain": "exchange", "action_date": DAY, "token": "a" * 64}
    repair.registry.acquire(env["work"], guard)
    result = inspect(env)
    assert result["phase"] == "CLAIM_INCOMPLETE"
    assert result["next_action"] == "CONTINUE_SAME_OWNER"
    assert repair.registry.read(env["work"]) == guard


def test_retained_older_claim_precedes_new_date_and_daily_gates(env):
    claimed(env)
    result = supervisor.inspect(env["native"], env["config"], owner=OWNER,
                                action_date="2026-10-12", now="2026-10-10T08:00:00Z")
    assert result["requested_action_date"] == "2026-10-12"
    assert result["action_date"] == DAY
    assert result["next_action"] == "CONTINUE_SAME_OWNER"


def test_real_prepared_applied_exchange_remains_owned_until_verified_resume(env):
    spec = prepared(env)
    first = inspect(env)
    assert first["phase"] == "REPAIR_PREPARED"
    applied(env, spec)
    second = inspect(env)
    assert second["phase"] == "REPAIR_APPLIED"
    assert second["next_action"] == "CONTINUE_SAME_OWNER"
    assert not second["overall_complete"]
    assert repair.registry.read(env["work"]) is not None


def test_forged_claim_evidence_cannot_count_as_progress(env):
    path = claimed(env)
    value = repair._read(path)
    write(path, {**value, "owner": "another owner"})
    result = inspect(env)
    assert result["next_action"] == "CONTINUE_SAME_OWNER"
    assert result["phase_rank"] == 0
    assert "does not match" in result["verification_error"]


def test_corrupt_completed_output_requires_repair_even_without_failure_label(env):
    env["output"].write_text("changed")
    result = inspect(env)
    assert result["next_action"] == "REPAIR_REQUIRED"
    assert not result["overall_complete"]


@pytest.mark.parametrize("value", ["2026-10-10", "2026-10-09/", "2026-02-30"])
def test_malformed_or_non_session_date_rejected(env, value):
    with pytest.raises(ValueError):
        supervisor.inspect(env["native"], env["config"], owner=OWNER, action_date=value, now=NOW)


def test_weekend_uses_intended_monday_without_falsifying_observation(env):
    result = supervisor.inspect(env["native"], env["config"], owner=OWNER, now="2026-10-10T08:00:00Z")
    assert result["action_date"] == "2026-10-12"
    assert result["observed_at"] == "2026-10-10T08:00:00+00:00"
    assert result["next_action"] == "DISPATCH_PREPARATION"


def test_terminal_status_label_alone_fails_real_completed_reader(env):
    write(env["status"], {"actor": "Scout", "action_date": DAY, "review_session": "2026-10-08", "status": "COMPLETE"})
    result = inspect(env)
    assert result["next_action"] == "REPAIR_REQUIRED"
    assert "Completed exchange status identity changed" in result["verification_error"]
    assert not result["overall_complete"]


@pytest.mark.parametrize("wrong", [{"action_date": "2026-10-12"}, {"ui_ready": False}, {"joint_ready": False},
                                    {"peer_verified": False}, {"review_session": DAY}, {"actor": "Atlas"}])
def test_wrong_date_or_incomplete_final_verification_cannot_complete(env, monkeypatch, wrong):
    write(env["status"], {"actor": "Scout", "action_date": DAY, "status": "COMPLETE"})
    verified = {"actor": "Scout", "action_date": DAY, "review_session": "2026-10-08", "status": "COMPLETE",
                "ui_ready": True, "joint_ready": True, "peer_verified": True, **wrong}
    monkeypatch.setattr(supervisor, "_completed", lambda *args: verified)
    result = inspect(env)
    assert result["next_action"] == "REPAIR_REQUIRED" and not result["overall_complete"]


def test_fresh_actual_completed_reader_is_called_with_exact_date(env, monkeypatch):
    from tools import nightly_exchange
    write(env["status"], {"actor": "Scout", "action_date": DAY, "status": "COMPLETE"})
    calls = []
    def completed(config, native, action, review, common):
        calls.append((config, native, action, review))
        return {**common, "status": "COMPLETE", "joint_ready": True, "ui_ready": True, "peer_verified": True}
    monkeypatch.setattr(nightly_exchange, "_completed_result", completed)
    monkeypatch.setattr(supervisor, "_current_display", lambda *args: {"fixture": "verified default display"})
    result = inspect(env)
    assert calls == [(env["config"], env["native"], DAY, "2026-10-08")]
    assert result["overall_complete"] is True
    assert result["final_verification"]["observed_at"] == pd.Timestamp(NOW).isoformat()


def test_current_native_turn_attestation_keeps_same_worker_actionable(env):
    claimed(env)
    result = inspect(env)
    proof = attestation(result)
    current = inspect(env, owner_liveness_attestation=proof, current_thread="native-thread")
    assert current["next_action"] == "CONTINUE_SAME_OWNER"
    supervisor.record(env["native"], current, wake_id="native-wake-identity")
    other = inspect(env, current_thread="later-thread")
    assert other["next_action"] == "WAIT_FOR_LIVE_OWNER"
    assert other["owner_liveness_attestation"]["provenance"] == "current_chat"
    assert other["owner_liveness_attestation"]["retained_from_ledger"] is True
    assert "native_observation" not in other["owner_liveness_attestation"]
    expired = supervisor.inspect(env["native"], env["config"], owner=OWNER, action_date=DAY,
                                 now="2026-10-09T21:05:01Z", current_thread="later-thread")
    assert expired["next_action"] == "INSPECT_RETAINED_OWNER"
    assert expired["repository_owner"] == result["repository_owner"]
    assert expired["last_known_owner_attestation"]["thread_id"] == "native-thread"
    assert expired["owner_liveness_attestation"] is None


def test_explicit_read_thread_attestation_waits_for_other_active_turn(env):
    claimed(env)
    result = inspect(env)
    proof = attestation(result, provenance="native_read_thread")
    proof["native_observation"] = {"tool": "read_thread", "status": "running",
                                   **{key: proof[key] for key in ("thread_id", "turn_id", "automation_id", "observed_at")}}
    observed = inspect(env, owner_liveness_attestation=proof, current_thread="different-thread")
    assert observed["next_action"] == "WAIT_FOR_LIVE_OWNER"
    assert observed["owner_liveness_attestation"]["evidence_kind"] == "owner_liveness_attestation"
    proof["native_observation"]["status"] = "completed"
    with pytest.raises(ValueError, match="active owner turn"):
        inspect(env, owner_liveness_attestation=proof)


@pytest.mark.parametrize("change", [{"actor": "Atlas"}, {"action_date": "2026-10-12"}, {"incident_id": "fake"},
    {"automation_id": "other-task"}, {"repair_id": "another-repair"}, {"thread_id": "unknown"}, {"turn_id": ""},
    {"observed_at": "NaT"}, {"observed_at": "2026-10-09T21:00:01Z"}, {"observed_at": "2026-10-09T20:54:59Z"},
    {"observed_at": "2026-10-09T21:00:00"}, {"provenance": "pid_guess"}])
def test_invalid_owner_liveness_never_authorizes_wait_or_takeover(env, change):
    claimed(env)
    result = inspect(env)
    with pytest.raises(ValueError):
        inspect(env, owner_liveness_attestation=attestation(result, **change), current_thread="native-thread")


def test_same_wake_does_not_accumulate_unchanged_counts_or_fake_progress(env):
    result = inspect(env)
    first = supervisor.record(env["native"], result, wake_id="same-wake-id")
    again = supervisor.record(env["native"], result, wake_id="same-wake-id")
    assert again["unchanged_wakes"] == first["unchanged_wakes"] == 1
    assert again["last_progress_at"] == first["last_progress_at"]
    result = inspect(env)
    result.update(observed_at="2026-10-09T21:01:00Z", failure={"fingerprint": "new error"})
    result["phase_evidence"][str(env["budget"])] = "new snapshot observation"
    third = supervisor.record(env["native"], result, wake_id="next-wake-id")
    assert third["unchanged_wakes"] == 2
    assert not third["progressed"]
    assert third["last_progress_at"] == first["last_progress_at"]


def test_applied_source_is_real_forward_progress_and_final_is_separate(env):
    spec = prepared(env)
    first = supervisor.record(env["native"], inspect(env), wake_id="first-wake")
    applied(env, spec)
    second = supervisor.record(env["native"], inspect(env), wake_id="second-wake")
    assert second["progressed"] and second["unchanged_wakes"] == 0
    assert not second["overall_complete"]
    assert first["phase"] == "REPAIR_PREPARED" and second["phase"] == "REPAIR_APPLIED"


def test_saved_future_observation_and_invalid_wake_are_rejected(env):
    result = inspect(env)
    supervisor.record(env["native"], result, wake_id="stable-wake")
    with pytest.raises(ValueError, match="backwards"):
        supervisor.record(env["native"], {**result, "observed_at": "2026-10-09T20:59:59Z"}, wake_id="stable-wake")
    with pytest.raises(ValueError, match="identity"):
        supervisor.record(env["native"], result, wake_id="../escape")


def test_workflow_running_label_without_verified_process_is_not_healthy(env):
    state = {**env["state"], "status": "RUNNING", "current_step": "model_review", "owner_pid": 123}
    write(env["prep"], state)
    result = inspect(env)
    assert result["next_action"] == "INSPECT_RUNNING_OWNER"
    assert result["worker_evidence"] is None


@pytest.mark.parametrize("birth,heartbeat,expected", [(100.0, NOW, "WAIT_FOR_HEALTHY_WORKER"),
    (200.0, NOW, "INSPECT_RUNNING_OWNER"), (100.0, "2026-10-09T20:57:59Z", "INSPECT_RUNNING_OWNER"),
    (100.0, "2026-10-09T21:00:01Z", "INSPECT_RUNNING_OWNER")])
def test_native_owner_pid_birth_and_fresh_heartbeat_all_required(env, monkeypatch, birth, heartbeat, expected):
    from ml import overnight_runtime
    from datafetching import runtime_lock
    state = {**env["state"], "status": "RUNNING", "current_step": "train_and_plan", "owner_pid": 123}
    state["steps"]["train_and_plan"] = {"status": "RUNNING", "native_run": str(env["data"] / "native-run")}
    write(env["prep"], state)
    monkeypatch.setattr(overnight_runtime, "overnight_status", lambda root: {
        "status": "RUNNING", "receipt_present": False, "run_path": str(env["data"] / "native-run"),
        "owner_pid": 123, "owner_created_at": 100.0, "heartbeat_at": heartbeat})
    monkeypatch.setattr(runtime_lock, "_process_created_at", lambda pid: birth)
    assert inspect(env)["next_action"] == expected


def test_source_failure_and_transient_backoff_have_different_next_actions(env):
    env["native"]["automatic_recovery"] = {"enabled": True, "authorization": "fixture local human", "max_attempts": 3}
    state = {**env["state"], "status": "FAILED", "failure": {"step": "prepare_stats", "kind": "SOURCE_DEFECT",
        "disposition": "OPEN", "fingerprint": "one-cause", "retry_after": "2026-10-09T21:05:00Z"}}
    write(env["prep"], state)
    assert inspect(env)["next_action"] == "REPAIR_REQUIRED"
    state["failure"]["kind"] = "TRANSIENT"
    write(env["prep"], state)
    assert inspect(env)["next_action"] == "WAIT_BACKOFF"


def test_partial_claim_without_global_registry_never_dispatches(env):
    write(env["session"] / "repair-claim.json", {"repair_id": "partial-claim"})
    result = inspect(env)
    assert result["next_action"] == "RECONCILE_PARTIAL_CLAIM"
    assert not result["overall_complete"]


def test_unknown_config_fails_closed_without_ledger_or_operating_mutation(tmp_path, capsys):
    path = write(tmp_path / "bad.json", {})
    assert supervisor.main(["--config", str(path), "--exchange-config", str(path), "--owner", OWNER, "--inspect"]) == 1
    result = json.loads(capsys.readouterr().out)
    assert result["next_action"] == "REPAIR_REQUIRED" and not result["overall_complete"]
    assert list(tmp_path.iterdir()) == [path]


def test_actual_preparation_claim_prepared_applied_lifecycle(preparation_env, tmp_path):
    from ml import nightly_stage_repair
    e = preparation_env
    exchange_config = {"actor": "Scout", "state_root": str(tmp_path / "exchange")}
    request = e["request"]
    claim = nightly_stage_repair.claim(e["config"], action_date=DAY, repair_id=request["repair_id"],
                                      owner=request["owner"], now=NOW)
    def observe():
        return supervisor.inspect(e["config"], exchange_config, owner=OWNER, action_date=DAY, now=NOW)
    first = observe()
    assert first["phase"] == "REPAIR_CLAIMED"
    assert first["repository_owner"] == claim["repository_claim"]
    spec = prepare_stage(e)
    second = observe()
    assert second["phase"] == "REPAIR_PREPARED"
    apply_stage(e, spec)
    assert (spec.parent / "applied.json").is_file()
    third = observe()
    assert third["phase"] == "REPAIR_APPLIED"
    assert third["next_action"] == "CONTINUE_SAME_OWNER" and not third["overall_complete"]
    assert third["deadline_at"] == e["state"]["deadline_at"]
    assert third["effective_deadline_at"] == e["state"]["effective_deadline_at"]


def test_corrupt_global_owner_fails_closed_without_replacing_it(env):
    path = write(env["work"] / "repair-owner.json", {"owner": "missing identity"})
    before = path.read_bytes()
    with pytest.raises(ValueError, match="Invalid repository repair owner"):
        inspect(env)
    assert path.read_bytes() == before
    assert not (env["work"] / "supervision").exists()


def test_failed_exchange_without_failure_record_requires_repair(env):
    result = inspect(env)
    assert result["next_action"] == "REPAIR_REQUIRED"
    assert "lacks a usable failure record" in result["reason"]


def test_resolved_exchange_claim_does_not_block_fresh_final_verification(env, monkeypatch):
    spec = prepared(env)
    applied(env, spec)
    guard = repair.registry.read(env["work"])
    repair.registry.release(env["work"], guard, verified=True)
    write(env["status"], {"actor": "Scout", "action_date": DAY, "status": "COMPLETE"})
    monkeypatch.setattr(supervisor, "_completed", lambda *args: {
        "actor": "Scout", "action_date": DAY, "review_session": "2026-10-08", "status": "COMPLETE",
        "joint_ready": True, "ui_ready": True, "peer_verified": True})
    monkeypatch.setattr(supervisor, "_current_display", lambda *args: {"fixture": "verified default display"})
    assert inspect(env)["next_action"] == "COMPLETE"


def retain_owner(e):
    claimed(e)
    initial = inspect(e)
    current = inspect(e, owner_liveness_attestation=attestation(initial), current_thread="native-thread")
    supervisor.record(e["native"], current, wake_id="original-native-turn")
    return current


def terminal_observation(result, **changes):
    value = {**attestation(result), "provenance": "native_read_thread", "observed_at": "2026-10-09T21:06:00Z",
             "readback_reference": "private-native-response.json"}
    value["native_observation"] = {"tool": "read_thread", "thread_status": "idle", "turn_status": "completed",
        **{key: value[key] for key in ("thread_id", "turn_id", "automation_id", "observed_at")}}
    return {**value, **changes}


def test_expired_owner_ids_survive_multiple_cron_wakes_until_native_status_is_read(env):
    original = retain_owner(env)
    for minute in (6, 12):
        result = supervisor.inspect(env["native"], env["config"], owner=OWNER, action_date=DAY,
            now=f"2026-10-09T21:{minute:02d}:00Z", current_thread=f"successor-{minute}")
        assert result["next_action"] == "INSPECT_RETAINED_OWNER"
        assert result["resume_action"] == "CONTINUE_SAME_OWNER"
        assert result["owner_liveness_attestation"] is None
        assert result["repository_owner"] == original["repository_owner"]
        saved = supervisor.record(env["native"], result, wake_id=f"successor-turn-{minute}")
        last = repair._read(Path(saved["ledger"]))["last_known_owner_attestation"]
        assert (last["thread_id"], last["turn_id"]) == ("native-thread", "native-turn")
        assert last["observed_at"] == NOW


def test_v1_ledger_migrates_without_losing_stale_owner_or_unchanged_wake_history(env):
    retain_owner(env)
    path = env["work"] / "supervision" / (DAY + ".json")
    legacy = repair._read(path)
    legacy["schema_version"] = "nightly-supervision-v1"
    legacy.pop("last_known_owner_attestation")
    legacy.pop("terminal_owner_observation")
    write(path, legacy)
    result = supervisor.inspect(env["native"], env["config"], owner=OWNER, action_date=DAY,
        now="2026-10-09T21:06:00Z", current_thread="later-cron-thread")
    assert result["next_action"] == "INSPECT_RETAINED_OWNER"
    saved = supervisor.record(env["native"], result, wake_id="later-cron-turn")
    assert saved["schema_version"] == "nightly-supervision-v2"
    assert saved["first_seen_at"] == legacy["first_seen_at"]
    assert saved["last_progress_at"] == legacy["last_progress_at"]
    assert saved["unchanged_wakes"] == legacy["unchanged_wakes"] + 1
    assert saved["last_known_owner_attestation"]["thread_id"] == "native-thread"


def test_overlapping_cron_inspection_cannot_overwrite_newly_recorded_owner_evidence(env):
    claimed(env)
    result = inspect(env)
    owner_result = inspect(env, owner_liveness_attestation=attestation(result), current_thread="native-thread")
    # A second chat inspects later, before the first chat has recorded its owner.
    stale = supervisor.inspect(env["native"], env["config"], owner=OWNER, action_date=DAY,
        now="2026-10-09T21:01:00Z", current_thread="overlapping-cron-thread")
    saved = supervisor.record(env["native"], owner_result, wake_id="original-native-turn")
    path = Path(saved["ledger"])
    original = path.read_bytes()
    with pytest.raises(ValueError, match="changed after inspection"):
        supervisor.record(env["native"], stale, wake_id="overlapping-cron-turn")
    assert path.read_bytes() == original
    fresh = supervisor.inspect(env["native"], env["config"], owner=OWNER, action_date=DAY,
        now="2026-10-09T21:01:00Z", current_thread="overlapping-cron-thread")
    assert fresh["next_action"] == "WAIT_FOR_LIVE_OWNER"
    recorded = supervisor.record(env["native"], fresh, wake_id="overlapping-cron-turn")
    assert recorded["last_known_owner_attestation"]["thread_id"] == "native-thread"


def test_native_terminal_turn_allows_same_owner_continuation_without_releasing_claim(env):
    original = retain_owner(env)
    observed = terminal_observation(original)
    result = supervisor.inspect(env["native"], env["config"], owner=OWNER, action_date=DAY,
        now=observed["observed_at"], current_thread="successor-thread", terminal_owner_observation=observed)
    assert result["next_action"] == "CONTINUE_SAME_OWNER"
    assert result["owner_liveness_attestation"] is None
    assert result["terminal_owner_observation"] == observed
    assert result["last_known_owner_attestation"]["thread_id"] == "native-thread"
    assert repair.registry.read(env["work"]) == original["repository_owner"]
    supervisor.record(env["native"], result, wake_id="successor-turn")
    again = supervisor.inspect(env["native"], env["config"], owner=OWNER, action_date=DAY,
        now="2026-10-09T21:07:00Z", current_thread="successor-thread")
    assert again["next_action"] == "CONTINUE_SAME_OWNER"
    stale = supervisor.inspect(env["native"], env["config"], owner=OWNER, action_date=DAY,
        now="2026-10-09T21:12:00Z", current_thread="later-thread")
    assert stale["next_action"] == "INSPECT_RETAINED_OWNER"


@pytest.mark.parametrize("change", [
    {"thread_id": "wrong-thread"}, {"turn_id": "wrong-turn"}, {"incident_id": "wrong-incident"},
    {"repair_id": "wrong-repair"}, {"provenance": "current_chat"}, {"observed_at": NOW},
    {"observed_at": "2026-10-09T21:06:01Z"}, {"readback_reference": ""},
    {"native_observation": {"tool": "read_thread", "thread_status": "idle"}},
])
def test_terminal_owner_proof_rejects_wrong_stale_future_or_incomplete_identity(env, change):
    original = retain_owner(env)
    with pytest.raises(ValueError):
        supervisor.inspect(env["native"], env["config"], owner=OWNER, action_date=DAY,
            now="2026-10-09T21:06:00Z", current_thread="successor-thread",
            terminal_owner_observation=terminal_observation(original, **change))


@pytest.mark.parametrize("thread_status,turn_status", [("running", "completed"), ("idle", "running"),
                                                      ("idle", "unknown"), ("unknown", "completed")])
def test_idle_chat_or_old_completed_turn_with_new_running_chat_never_allows_continuation(env, thread_status, turn_status):
    original = retain_owner(env)
    proof = terminal_observation(original)
    proof["native_observation"].update(thread_status=thread_status, turn_status=turn_status)
    with pytest.raises(ValueError, match="terminal retained turn"):
        supervisor.inspect(env["native"], env["config"], owner=OWNER, action_date=DAY,
            now=proof["observed_at"], current_thread="successor-thread", terminal_owner_observation=proof)


def test_new_exchange_failure_after_applied_is_repair_work_for_the_retained_owner(env):
    spec = prepared(env)
    applied(env, spec)
    owner = repair.registry.read(env["work"])
    failure = repair.record_failure(env["config"], DAY, ValueError("second deterministic defect"), now=NOW)
    write(env["status"], {**repair._read(env["status"]), "failure_epoch": 2})
    result = inspect(env)
    assert result["phase"] == "REPAIR_APPLIED" and result["next_action"] == "REPAIR_REQUIRED"
    assert result["failure"] == failure
    assert result["continuation_guard"] == repair.dispatch_guard(env["config"], DAY, now=NOW)
    assert result["repair_disposition"] == "SAME_OWNER_REPAIR_OR_RESTORATION_REQUIRED"
    assert result["repository_owner"] == owner and not result["overall_complete"]
    next_claim = claimed(env, "second-exchange-repair", supersede_applied=True)
    assert repair.registry.read(env["work"])["owner"] == owner["owner"]
    assert repair._read(next_claim)["supersedes_applied_repair"] == owner["repair_id"]


def test_applied_exchange_transient_retry_preserves_actual_backoff(env):
    applied(env, prepared(env))
    failure = repair.record_failure(env["config"], DAY, OSError("brief unavailable mount"), now=NOW)
    result = inspect(env)
    assert result["next_action"] == "WAIT_BACKOFF"
    assert result["failure"] == failure
    assert result["repair_disposition"] == "WAIT_RETAINED_RETRY"


def test_new_preparation_failure_after_applied_uses_supported_same_owner_chain(preparation_env, tmp_path):
    from ml import nightly_stage_repair
    from tests.test_nightly_stage_repair import new_failed_attempt
    e = preparation_env
    e["config"]["automatic_recovery"] = {"enabled": True, "authorization": "fixture local human", "max_attempts": 3}
    apply_stage(e, prepare_stage(e))
    old = nightly_stage_repair.registry.read(e["config"]["state_root"])
    state = new_failed_attempt(e)
    state["failure"]["kind"] = "SOURCE_DEFECT"
    workflow._write(e["state_path"], state)
    result = supervisor.inspect(e["config"], {"actor": "Scout", "state_root": str(tmp_path / "exchange")},
        owner=OWNER, action_date=DAY, now=NOW)
    assert result["next_action"] == "REPAIR_REQUIRED" and result["phase"] == "REPAIR_APPLIED"
    assert result["failure"] == state["failure"] and result["repository_owner"] == old
    assert result["repair_disposition"] == "SAME_OWNER_REPAIR_OR_RESTORATION_REQUIRED"
    continued = nightly_stage_repair.claim(e["config"], action_date=DAY, repair_id="second-stage-repair",
        owner=old["owner"], now=NOW)
    assert continued["repository_claim"]["owner"] == old["owner"]
    assert continued["repository_claim"]["repair_id"] == "second-stage-repair"


def completed_observation(exchange, monkeypatch, actor):
    native = exchange[1][actor]
    monkeypatch.setattr(supervisor.workflow, "status", lambda *args, **kwargs: {
        "status": "LOCAL_COMPLETE_PEER_SETUP_PENDING", "action_date": EXCHANGE_DAY,
        "source_session": "2026-09-08", "steps": {}})
    return supervisor.inspect(native, exchange[0][actor], owner=actor + "-priority-source-reconciliation",
        action_date=EXCHANGE_DAY, now=EXCHANGE_NOW)


@pytest.mark.parametrize("actor", ["atlas", "scout"])
def test_dated_exchange_completion_does_not_hide_broken_default_stats(exchange, monkeypatch, actor):
    synthesized(exchange)
    wake(exchange, "atlas")
    wake(exchange, "scout")
    native = exchange[1][actor]
    path = Path(native["datastore"]) / "ml/gameplan-actuals-review-latest/run.json"
    pointer = repair._read(path)
    pointer["current"]["run_path"] = "ml/gameplan-actuals-review-runs/missing-default-run"
    write(path, pointer)
    assert supervisor._completed(native, exchange[0][actor], EXCHANGE_DAY, "2026-09-08")["ui_ready"]
    result = completed_observation(exchange, monkeypatch, actor)
    assert result["next_action"] == "REPAIR_REQUIRED" and not result["overall_complete"]
    assert "GameplanStatsError" in result["verification_error"]


@pytest.mark.parametrize("no_history", [False, True])
def test_real_combined_completion_requires_default_display_but_accepts_no_history_stats(exchange, monkeypatch, tmp_path, no_history):
    if no_history:
        from ml.artifacts import file_checksum
        from tests.test_gameplan_stats_handoff import baseline_package
        for actor in ("atlas", "scout"):
            output = exchange[2][actor]["steps"]["local_handoff"]["output"]
            old = output["stats_package"]
            path, _ = baseline_package(tmp_path, actor.title(), exchange[0][actor]["owners"][actor],
                session="2026-09-08", reviewed_at=EXCHANGE_NOW)
            output["stats_package"] = str(path)
            output["files"].pop(old)
            output["files"][str(path)] = file_checksum(path)
    synthesized(exchange)
    wake(exchange, "atlas")
    wake(exchange, "scout")
    from app.ui.gameplan_stats_data import load_gameplan_stats
    for actor in ("atlas", "scout"):
        result = completed_observation(exchange, monkeypatch, actor)
        assert result["next_action"] == "COMPLETE" and result["overall_complete"]
        assert result["final_verification"]["default_display"]["action_date"] == EXCHANGE_DAY
        stats = load_gameplan_stats(Path(exchange[1][actor]["datastore"]))
        assert stats.metrics().total == (0 if no_history else 2)
        if no_history:
            assert stats.metrics().accuracy is None
            assert repair._read(stats.run_directory / "report.json")["missing_history_owners"] == ["Atlas", "Scout"]
