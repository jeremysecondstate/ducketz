"""Offline native-schema fixtures; no real chats, executables, or providers."""
from hashlib import sha256
import json
from pathlib import Path
import sqlite3
import subprocess
from types import SimpleNamespace
import uuid

import pytest

from tools import codex_chat_cleanup as cleanup

NOW = 2000000000
THREAD = "00000000-0000-4000-8000-000000000001"
AUTOMATION = "atlas-example-owner"


@pytest.fixture
def native(tmp_path):
    home = tmp_path / "codex-home"
    (home / "sqlite").mkdir(parents=True)
    paths = {key: home / relative for key, relative in cleanup.DB_PATHS.items()}
    schema = {
        "app": "CREATE TABLE automations(id TEXT,kind TEXT,rrule TEXT,execution_environment TEXT,status TEXT); CREATE TABLE automation_runs(thread_id TEXT,automation_id TEXT,status TEXT,created_at INTEGER,read_at INTEGER);",
        "state": "CREATE TABLE threads(id TEXT,thread_source TEXT,has_user_event INTEGER,is_pinned INTEGER,thread_section_id TEXT,archived INTEGER,archived_at INTEGER,updated_at_ms INTEGER); CREATE TABLE thread_spawn_edges(parent_thread_id TEXT,child_thread_id TEXT);",
        "history": "CREATE TABLE thread_turns(thread_id TEXT,turn_id TEXT,status TEXT,error_json TEXT,started_at INTEGER,completed_at INTEGER,first_user_item_id TEXT,final_agent_item_id TEXT); CREATE TABLE thread_items(thread_id TEXT,turn_id TEXT,item_id TEXT,item_json TEXT,item_type TEXT);",
        "queue": "CREATE TABLE queued_items(thread_id TEXT);",
    }
    for key, script in schema.items():
        with sqlite3.connect(paths[key]) as db:
            db.executescript(script)
    def sql(key, query, args=()):
        with sqlite3.connect(paths[key]) as db:
            db.execute(query, args)
    completed = NOW - 3600
    sql("app", "INSERT INTO automations VALUES(?,?,?,?,?)", (AUTOMATION, "cron", "FREQ=MINUTELY;INTERVAL=5", "local", "ACTIVE"))
    sql("app", "INSERT INTO automation_runs VALUES(?,?,?,?,NULL)", (THREAD, AUTOMATION, "PENDING_REVIEW", (completed - 60) * 1000))
    sql("state", "INSERT INTO threads VALUES(?,?,0,0,NULL,0,NULL,?)", (THREAD, "automation", completed * 1000))
    sql("history", "INSERT INTO thread_turns VALUES(?,?,?,NULL,?,?,?,?)", (THREAD, "turn-one", "completed", completed - 60, completed, "user-one", "final-one"))
    first = {"type": "userMessage", "id": "user-one", "content": [{"type": "text", "text": f"Automation: Example\nAutomation ID: {AUTOMATION}\n\nDo work."}]}
    final = {"type": "agentMessage", "id": "final-one", "phase": "final_answer", "text": "Bounded work completed unchanged."}
    sql("history", "INSERT INTO thread_items VALUES(?,?,?,?,?)", (THREAD, "turn-one", "user-one", json.dumps(first), "userMessage"))
    sql("history", "INSERT INTO thread_items VALUES(?,?,?,?,?)", (THREAD, "turn-one", "final-one", json.dumps(final), "agentMessage"))
    exe = tmp_path / "codex.exe"
    exe.write_bytes(b"offline-pinned-executable-fixture")
    config = {"codex_home": str(home), "codex_exe": str(exe), "codex_sha256": sha256(exe.read_bytes()).hexdigest(),
              "automation_ids": [AUTOMATION], "state_dir": str(tmp_path / "cleanup-state"), "minimum_age_seconds": 3600, "max_per_run": 10}
    return SimpleNamespace(config=config, sql=sql, paths=paths, home=home, final=final)


def never_run(*args, **kwargs):
    pytest.fail("Native command must not run for this case")


def archive_runner(native, calls):
    def run(command, **kwargs):
        calls.append((command, kwargs))
        native.sql("state", "UPDATE threads SET archived=1,archived_at=? WHERE id=?", (NOW, command[-1]))
        return SimpleNamespace(returncode=0, stdout="Archived", stderr="")
    return run


def test_hour_is_measured_from_actual_successful_completion(native):
    c = native.config
    assert cleanup.inspect_candidate(c, THREAD, NOW)["eligible"]
    assert cleanup.inspect_candidate(c, THREAD, NOW - 0.1)["reason"] == "YOUNGER_THAN_MINIMUM"
    native.sql("history", "UPDATE thread_turns SET completed_at=?", (NOW + 1,))
    assert cleanup.inspect_candidate(c, THREAD, NOW)["reason"] == "INVALID_COMPLETION_TIME"


@pytest.mark.parametrize("key,statement,args", [
    ("history", "UPDATE thread_turns SET status=?", ("inProgress",)),
    ("history", "UPDATE thread_turns SET status=?", ("failed",)),
    ("history", "UPDATE thread_turns SET status=?", ("interrupted",)),
    ("history", "UPDATE thread_turns SET error_json=?", ('{"message":"failed"}',)),
    ("history", "UPDATE thread_turns SET completed_at=NULL", ()),
    ("history", "UPDATE thread_turns SET final_agent_item_id=NULL", ()),
    ("state", "UPDATE threads SET has_user_event=1", ()),
    ("state", "UPDATE threads SET thread_source='manual'", ()),
    ("state", "UPDATE threads SET is_pinned=1", ()),
    ("state", "UPDATE threads SET thread_section_id='my-section'", ()),
    ("state", "UPDATE threads SET archived=1", ()),
    ("queue", "INSERT INTO queued_items VALUES(?)", (THREAD,)),
    ("app", "UPDATE automation_runs SET status='RUNNING'", ()),
    ("app", "UPDATE automation_runs SET status='FAILED'", ()),
    ("app", "UPDATE automations SET kind='heartbeat'", ()),
    ("app", "UPDATE automation_runs SET created_at=1", ()),
    ("state", "INSERT INTO thread_spawn_edges VALUES(?,?)", (THREAD, "child-one")),
])
def test_incomplete_manual_organized_and_out_of_scope_chats_are_excluded(native, key, statement, args):
    native.sql(key, statement, args)
    assert not cleanup.inspect_candidate(native.config, THREAD, NOW)["eligible"]
    assert cleanup.run_once(native.config, apply=True, now=NOW, runner=never_run)["archived"] == []


def test_subsequent_turn_or_same_turn_manual_steering_is_excluded(native):
    native.sql("history", "INSERT INTO thread_turns SELECT * FROM thread_turns")
    assert cleanup.inspect_candidate(native.config, THREAD, NOW)["reason"] == "NOT_SINGLE_SUCCESSFUL_TURN"
    native.sql("history", "DELETE FROM thread_turns WHERE rowid=(SELECT MAX(rowid) FROM thread_turns)")
    native.sql("history", "INSERT INTO thread_items VALUES(?,?,?,?,?)", (THREAD, "turn-one", "manual", '{}', "userMessage"))
    assert cleanup.inspect_candidate(native.config, THREAD, NOW)["reason"] == "MANUAL_OR_ADDITIONAL_USER_INPUT"


@pytest.mark.parametrize("status", ["PENDING_REVIEW", "ACCEPTED"])
@pytest.mark.parametrize("read_at", [None, NOW * 1000])
def test_reading_completed_scheduled_result_does_not_exempt_it(native, status, read_at):
    native.sql("app", "UPDATE automation_runs SET status=?,read_at=?", (status, read_at))
    result = cleanup.run_once(native.config, apply=True, now=NOW, runner=archive_runner(native, []))
    assert result["archived"] == [THREAD]


@pytest.mark.parametrize("rrule", ["FREQ=MINUTELY;INTERVAL=5", "FREQ=DAILY;BYHOUR=23", "FREQ=WEEKLY;BYDAY=MO", "FREQ=MONTHLY;BYMONTHDAY=1"])
@pytest.mark.parametrize("selection", ["allowlist", "local_cron"])
def test_scheduled_cron_cleanup_accepts_any_cadence(native, rrule, selection):
    if selection == "local_cron":
        native.config.pop("automation_ids")
        native.config["automation_selection"] = "local_cron"
    native.sql("app", "UPDATE automations SET rrule=?", (rrule,))
    assert cleanup.inspect_candidate(native.config, THREAD, NOW)["eligible"]
    result = cleanup.run_once(native.config, apply=True, now=NOW, runner=archive_runner(native, []))
    assert result["archived"] == [THREAD]


def test_local_cron_discovery_finds_new_ids_and_paused_definitions(native):
    native.config.pop("automation_ids")
    native.config["automation_selection"] = "local_cron"
    assert cleanup.run_once(native.config, now=NOW, runner=never_run)["eligible"][0]["thread_id"] == THREAD
    new_id = "new-nightly-schedule"
    new_thread = str(uuid.UUID(int=401))
    native.sql("app", "INSERT INTO automations VALUES(?,?,?,?,?)", (new_id, "cron", "FREQ=DAILY;BYHOUR=23", "local", "PAUSED"))
    add_completed_run(native, new_thread, NOW - 3600, automation_id=new_id)
    result = cleanup.run_once(native.config, apply=True, now=NOW, runner=archive_runner(native, []))
    assert set(result["archived"]) == {THREAD, new_thread}


@pytest.mark.parametrize("kind,environment", [("heartbeat", "local"), ("cron", "cloud"), ("cron", None)])
def test_local_cron_discovery_excludes_heartbeats_and_nonlocal_runs(native, kind, environment):
    native.config.pop("automation_ids")
    native.config["automation_selection"] = "local_cron"
    native.sql("app", "UPDATE automations SET kind=?,execution_environment=?", (kind, environment))
    assert not cleanup.inspect_candidate(native.config, THREAD, NOW)["eligible"]
    result = cleanup.run_once(native.config, apply=True, now=NOW, runner=never_run)
    assert result["considered"] == 0 and result["archived"] == []


def test_allowlist_remains_exact_when_new_schedules_arrive(native):
    new_id = "new-nightly-schedule"
    new_thread = str(uuid.UUID(int=402))
    native.sql("app", "INSERT INTO automations VALUES(?,?,?,?,?)", (new_id, "cron", "FREQ=DAILY", "local", "ACTIVE"))
    add_completed_run(native, new_thread, NOW - 3600, automation_id=new_id)
    assert cleanup.inspect_candidate(native.config, new_thread, NOW)["reason"] == "NOT_ALLOWED_AUTOMATION"
    assert cleanup.run_once(native.config, apply=True, now=NOW, runner=archive_runner(native, []))["archived"] == [THREAD]


def test_dynamic_discovery_requires_environment_schema_but_allowlist_remains_supported(native):
    native.sql("app", "ALTER TABLE automations DROP COLUMN execution_environment")
    assert cleanup.inspect_candidate(native.config, THREAD, NOW)["eligible"]
    native.config.pop("automation_ids")
    native.config["automation_selection"] = "local_cron"
    with pytest.raises(cleanup.CleanupError, match="execution_environment"):
        cleanup.run_once(native.config, apply=True, now=NOW, runner=never_run)


def add_completed_descendant(native, *, parent_id=THREAD, child_id=None, completed=NOW - 3600):
    """Native subagent history has agent turns without scheduled user items."""
    child_id = child_id or str(uuid.UUID(int=501))
    native.sql("state", "INSERT INTO thread_spawn_edges VALUES(?,?)", (parent_id, child_id))
    native.sql("state", "INSERT INTO threads VALUES(?,?,0,0,NULL,0,NULL,?)", (child_id, "subagent", completed * 1000))
    for number in (1, 2):
        turn_id = f"child-turn-{number}"
        final_id = f"child-final-{number}"
        turn_completed = completed - (2 - number) * 30
        native.sql("history", "INSERT INTO thread_turns VALUES(?,?,?,NULL,?,?,NULL,?)", (child_id, turn_id, "completed", turn_completed - 20, turn_completed, final_id))
        final = {"type": "agentMessage", "id": final_id, "phase": "final_answer", "text": f"Completed child work {number}."}
        native.sql("history", "INSERT INTO thread_items VALUES(?,?,?,?,?)", (child_id, turn_id, final_id, json.dumps(final), "agentMessage"))
    return child_id


def test_completed_descendants_with_multiple_agent_turns_are_eligible(native):
    child_id = add_completed_descendant(native)
    add_completed_descendant(native, parent_id=child_id, child_id=str(uuid.UUID(int=502)))
    candidate = cleanup.inspect_candidate(native.config, THREAD, NOW)
    assert candidate["eligible"] and candidate["completed_at"] == NOW - 3600 and candidate["descendant_count"] == 2
    calls = []
    result = cleanup.run_once(native.config, apply=True, now=NOW, runner=archive_runner(native, calls))
    assert result["archived"] == [THREAD]
    assert [command[-1] for command, _ in calls] == [THREAD]


def test_hour_is_measured_from_latest_descendant_completion(native):
    completed = NOW - 7200
    native.sql("history", "UPDATE thread_turns SET started_at=?,completed_at=?", (completed - 60, completed))
    native.sql("app", "UPDATE automation_runs SET created_at=?", ((completed - 60) * 1000,))
    add_completed_descendant(native)
    candidate = cleanup.inspect_candidate(native.config, THREAD, NOW)
    assert candidate["eligible"] and candidate["completed_at"] == NOW - 3600
    assert not cleanup.inspect_candidate(native.config, THREAD, NOW - 0.1)["eligible"]


@pytest.mark.parametrize("key,statement,args", [
    ("history", "UPDATE thread_turns SET status='inProgress' WHERE thread_id=?", (str(uuid.UUID(int=501)),)),
    ("history", "UPDATE thread_turns SET status='failed' WHERE thread_id=?", (str(uuid.UUID(int=501)),)),
    ("history", "UPDATE thread_turns SET error_json='{}' WHERE thread_id=?", (str(uuid.UUID(int=501)),)),
    ("history", "UPDATE thread_turns SET completed_at=? WHERE thread_id=?", (NOW - 3599, str(uuid.UUID(int=501)))),
    ("history", "UPDATE thread_turns SET final_agent_item_id=NULL WHERE thread_id=?", (str(uuid.UUID(int=501)),)),
    ("history", "INSERT INTO thread_items VALUES(?,?,?,?,?)", (str(uuid.UUID(int=501)), "child-turn-1", "manual", '{}', "userMessage")),
    ("history", "DELETE FROM thread_turns WHERE thread_id=?", (str(uuid.UUID(int=501)),)),
    ("state", "UPDATE threads SET has_user_event=1 WHERE id=?", (str(uuid.UUID(int=501)),)),
    ("state", "UPDATE threads SET thread_source='manual' WHERE id=?", (str(uuid.UUID(int=501)),)),
    ("state", "UPDATE threads SET is_pinned=1 WHERE id=?", (str(uuid.UUID(int=501)),)),
    ("state", "UPDATE threads SET thread_section_id='my-section' WHERE id=?", (str(uuid.UUID(int=501)),)),
    ("state", "UPDATE threads SET archived=1 WHERE id=?", (str(uuid.UUID(int=501)),)),
    ("queue", "INSERT INTO queued_items VALUES(?)", (str(uuid.UUID(int=501)),)),
])
def test_unsafe_descendant_prevents_parent_archival(native, key, statement, args):
    add_completed_descendant(native)
    native.sql(key, statement, args)
    assert not cleanup.inspect_candidate(native.config, THREAD, NOW)["eligible"]
    assert cleanup.run_once(native.config, apply=True, now=NOW, runner=never_run)["archived"] == []


def test_unfinished_descendant_goal_prevents_parent_archival(native):
    child_id = add_completed_descendant(native)
    with sqlite3.connect(native.home / "goals_1.sqlite") as db:
        db.execute("CREATE TABLE thread_goals(thread_id TEXT,status TEXT)")
        db.execute("INSERT INTO thread_goals VALUES(?,?)", (child_id, "active"))
    assert not cleanup.inspect_candidate(native.config, THREAD, NOW)["eligible"]
    assert cleanup.run_once(native.config, apply=True, now=NOW, runner=never_run)["archived"] == []


@pytest.mark.parametrize("patch", [{"phase": "commentary"}, {"id": "wrong-final"}, {"text": ""}])
def test_each_descendant_turn_requires_a_valid_final_response(native, patch):
    child_id = add_completed_descendant(native)
    final = {"type": "agentMessage", "id": "child-final-1", "phase": "final_answer", "text": "Completed child work.", **patch}
    native.sql("history", "UPDATE thread_items SET item_json=? WHERE thread_id=? AND item_id='child-final-1'", (json.dumps(final), child_id))
    assert not cleanup.inspect_candidate(native.config, THREAD, NOW)["eligible"]
    assert cleanup.run_once(native.config, apply=True, now=NOW, runner=never_run)["archived"] == []


@pytest.mark.parametrize("overflow", ["children", "turns"])
def test_descendant_inspection_is_bounded(native, monkeypatch, overflow):
    monkeypatch.setattr(cleanup, "FAMILY_LIMIT", 2)
    child_id = add_completed_descendant(native)
    if overflow == "children":
        add_completed_descendant(native, child_id=str(uuid.UUID(int=502)))
    assert cleanup.inspect_candidate(native.config, THREAD, NOW)["eligible"]
    if overflow == "children":
        add_completed_descendant(native, child_id=str(uuid.UUID(int=503)))
    else:
        native.sql("history", "INSERT INTO thread_turns VALUES(?,?,?,NULL,?,?,NULL,?)", (child_id, "child-turn-3", "completed", NOW - 3620, NOW - 3600, "child-final-3"))
    assert not cleanup.inspect_candidate(native.config, THREAD, NOW)["eligible"]
    assert cleanup.run_once(native.config, apply=True, now=NOW, runner=never_run)["archived"] == []


@pytest.mark.parametrize("edge", ["cycle", "duplicate", "multiple_parents"])
def test_ambiguous_descendant_graph_prevents_parent_archival(native, edge):
    child_id = add_completed_descendant(native)
    parent, child = {"cycle": (child_id, THREAD), "duplicate": (THREAD, child_id), "multiple_parents": (str(uuid.UUID(int=503)), child_id)}[edge]
    native.sql("state", "INSERT INTO thread_spawn_edges VALUES(?,?)", (parent, child))
    assert not cleanup.inspect_candidate(native.config, THREAD, NOW)["eligible"]
    assert cleanup.run_once(native.config, apply=True, now=NOW, runner=never_run)["archived"] == []


def test_descendant_snapshot_change_blocks_native_call(native, monkeypatch):
    child_id = add_completed_descendant(native)
    real = cleanup.inspect_candidate
    count = 0
    def changing(*args, **kwargs):
        nonlocal count
        count += 1
        if count == 2:
            final = {"type": "agentMessage", "id": "child-final-2", "phase": "final_answer", "text": "Updated child result."}
            native.sql("history", "UPDATE thread_items SET item_json=? WHERE thread_id=? AND item_id='child-final-2'", (json.dumps(final), child_id))
        return real(*args, **kwargs)
    monkeypatch.setattr(cleanup, "inspect_candidate", changing)
    result = cleanup.run_once(native.config, apply=True, now=NOW, runner=never_run)
    assert result["skipped"][-1]["reason"] == "CHANGED_BEFORE_ARCHIVE"


def test_native_final_identity_phase_and_text_are_required(native):
    for patch in ({"phase": "commentary"}, {"id": "wrong-final"}, {"text": ""}):
        final = {**native.final, **patch}
        native.sql("history", "UPDATE thread_items SET item_json=? WHERE item_id='final-one'", (json.dumps(final),))
        assert cleanup.inspect_candidate(native.config, THREAD, NOW)["reason"] == "MISSING_FINAL_RESPONSE"


def test_unknown_or_oversized_native_item_does_not_become_success(native):
    native.sql("history", "UPDATE thread_items SET item_json=? WHERE item_id='final-one'", ("x" * (cleanup.MAX_JSON + 1),))
    assert cleanup.inspect_candidate(native.config, THREAD, NOW)["reason"] == "MISSING_FINAL_RESPONSE"


def test_unfinished_goals_are_excluded(native):
    with sqlite3.connect(native.home / "goals_1.sqlite") as db:
        db.execute("CREATE TABLE thread_goals(thread_id TEXT,status TEXT)")
        db.execute("INSERT INTO thread_goals VALUES(?,?)", (THREAD, "paused"))
    assert cleanup.inspect_candidate(native.config, THREAD, NOW)["reason"] == "UNFINISHED_GOAL"


def test_default_dry_run_never_writes_state_or_native_databases(native):
    hashes = {key: sha256(path.read_bytes()).hexdigest() for key, path in native.paths.items()}
    result = cleanup.run_once(native.config, now=NOW, runner=never_run)
    assert result["status"] == "DRY_RUN" and len(result["eligible"]) == 1
    assert not Path(native.config["state_dir"]).exists()
    assert hashes == {key: sha256(path.read_bytes()).hexdigest() for key, path in native.paths.items()}


def test_exact_native_archive_command_and_durable_receipt(native):
    calls = []
    result = cleanup.run_once(native.config, apply=True, now=NOW, runner=archive_runner(native, calls))
    assert result["archived"] == [THREAD]
    command, kwargs = calls[0]
    assert command == [native.config["codex_exe"], "--no-daemon", "archive", THREAD]
    assert kwargs["shell"] is False and kwargs["timeout"] == 15
    assert kwargs["env"]["CODEX_HOME"] == native.config["codex_home"]
    root = Path(native.config["state_dir"])
    receipt = json.loads((root / "receipts" / f"{THREAD}.json").read_text())
    assert receipt["status"] == "archived" and receipt["completed_at"] == NOW - 3600
    events = [json.loads(x) for x in (root / "events.jsonl").read_text().splitlines()]
    assert [x["status"] for x in events] == ["attempting", "archived"]
    assert events[0]["attempt_id"] == events[1]["attempt_id"]


def test_immediate_snapshot_change_blocks_native_call(native, monkeypatch):
    real = cleanup.inspect_candidate
    count = 0
    def changing(*args, **kwargs):
        nonlocal count
        count += 1
        if count == 2:
            native.sql("queue", "INSERT INTO queued_items VALUES(?)", (THREAD,))
        return real(*args, **kwargs)
    monkeypatch.setattr(cleanup, "inspect_candidate", changing)
    result = cleanup.run_once(native.config, apply=True, now=NOW, runner=never_run)
    assert result["skipped"][-1]["reason"] == "CHANGED_BEFORE_ARCHIVE"


def test_user_restored_chat_is_never_archived_again(native):
    cleanup.run_once(native.config, apply=True, now=NOW, runner=archive_runner(native, []))
    native.sql("state", "UPDATE threads SET archived=0,archived_at=NULL")
    for later in (NOW + 60, NOW + 120):
        assert cleanup.run_once(native.config, apply=True, now=later, runner=never_run)["archived"] == []
    assert cleanup._receipt(Path(native.config["state_dir"]), THREAD)["status"] == "restored"


def test_native_writer_busy_may_retry_unchanged_chat_on_later_wake(native):
    busy = lambda *a, **kw: SimpleNamespace(returncode=1, stdout="", stderr="Foreign writer lock held")
    first = cleanup.run_once(native.config, apply=True, now=NOW, runner=busy)
    assert first["archived"] == []
    assert cleanup._receipt(Path(native.config["state_dir"]), THREAD)["status"] == "writer_busy"
    assert cleanup.run_once(native.config, apply=True, now=NOW + 60, runner=archive_runner(native, []))["archived"] == [THREAD]


@pytest.mark.parametrize("kind", ["zero_without_readback", "failed", "timeout"])
def test_ambiguous_or_failed_attempt_is_not_blindly_retried(native, kind):
    def run(*args, **kwargs):
        if kind == "timeout":
            raise subprocess.TimeoutExpired(args[0], 30)
        return SimpleNamespace(returncode=0 if kind == "zero_without_readback" else 2, stdout="", stderr="Unclassified failure")
    assert cleanup.run_once(native.config, apply=True, now=NOW, runner=run)["status"] == "FAILED"
    assert cleanup.run_once(native.config, apply=True, now=NOW + 60, runner=never_run)["status"] == "FAILED"


def test_interrupted_already_archived_attempt_is_preserved_without_retry_or_invented_confirmation(native):
    candidate = cleanup.inspect_candidate(native.config, THREAD, NOW)
    root = Path(native.config["state_dir"])
    root.mkdir()
    cleanup._record(root, {**candidate, "version": cleanup.VERSION, "attempt_id": uuid.uuid4().hex, "status": "attempting"})
    native.sql("state", "UPDATE threads SET archived=1,archived_at=?", (NOW,))
    result = cleanup.run_once(native.config, apply=True, now=NOW + 60, runner=never_run)
    # Archived native history is outside candidate scanning. The interrupted
    # local attempt stays unresolved until separate audit; it is never replayed.
    assert result["considered"] == 0
    assert cleanup._receipt(root, THREAD)["status"] == "attempting"
    native.sql("state", "UPDATE threads SET archived=0,archived_at=NULL")
    assert cleanup.run_once(native.config, apply=True, now=NOW + 120, runner=never_run)["status"] == "FAILED"


@pytest.mark.parametrize("target", ["native", "helper", "schema", "state", "receipt"])
def test_changed_binding_or_corrupt_evidence_fails_closed(native, target):
    root = Path(native.config["state_dir"])
    if target == "native":
        Path(native.config["codex_exe"]).write_bytes(b"changed native binary")
    elif target == "helper":
        native.config["helper_sha256"] = "0" * 64
    elif target == "schema":
        native.sql("queue", "DROP TABLE queued_items")
    elif target == "state":
        root.mkdir(); (root / "state.json").write_text('{"version":"unknown","cursor":null}')
    else:
        (root / "receipts").mkdir(parents=True)
        (root / "receipts" / f"{THREAD}.json").write_text('{}')
    with pytest.raises(cleanup.CleanupError):
        cleanup.run_once(native.config, apply=True, now=NOW, runner=never_run)
    assert json.loads((root / "last.json").read_text())["status"] == "FAILED"


def test_cursor_wraps_and_does_not_starve_candidate(native):
    root = Path(native.config["state_dir"])
    root.mkdir()
    cleanup._write(root / "state.json", {"version": cleanup.VERSION, "cursor": {"created_at": NOW * 1000, "thread_id": THREAD}})
    result = cleanup.run_once(native.config, apply=True, now=NOW, runner=archive_runner(native, []))
    assert result["archived"] == [THREAD]


def add_completed_run(native, thread_id, completed, automation_id=AUTOMATION):
    """Another natural cron run arriving while earlier runs wait to age."""
    native.sql("app", "INSERT INTO automation_runs VALUES(?,?,?,?,NULL)", (thread_id, automation_id, "PENDING_REVIEW", (completed - 60) * 1000))
    native.sql("state", "INSERT INTO threads VALUES(?,?,0,0,NULL,0,NULL,?)", (thread_id, "automation", completed * 1000))
    native.sql("history", "INSERT INTO thread_turns VALUES(?,?,?,NULL,?,?,?,?)", (thread_id, "turn-one", "completed", completed - 60, completed, "user-one", "final-one"))
    first = {"type": "userMessage", "id": "user-one", "content": [{"type": "text", "text": f"Automation: Example\nAutomation ID: {automation_id}\n\nDo work."}]}
    native.sql("history", "INSERT INTO thread_items VALUES(?,?,?,?,?)", (thread_id, "turn-one", "user-one", json.dumps(first), "userMessage"))
    native.sql("history", "INSERT INTO thread_items VALUES(?,?,?,?,?)", (thread_id, "turn-one", "final-one", json.dumps(native.final), "agentMessage"))


def test_continuous_five_minute_arrivals_do_not_strand_a_younger_chat(native):
    # The first run initially has no one-hour-old completion; a new row arrives
    # before every later wake, so an empty-query-only wrap would never revisit it.
    native.sql("history", "UPDATE thread_turns SET started_at=?,completed_at=?", (NOW - 60, NOW))
    native.sql("app", "UPDATE automation_runs SET created_at=?", ((NOW - 60) * 1000,))
    calls = []
    runner = archive_runner(native, calls)
    assert cleanup.run_once(native.config, apply=True, now=NOW, runner=runner)["archived"] == []
    for wake in range(1, 13):
        timestamp = NOW + wake * 300
        add_completed_run(native, str(uuid.UUID(int=100 + wake)), timestamp)
        result = cleanup.run_once(native.config, apply=True, now=timestamp, runner=runner)
        assert result["archived"] == ([THREAD] if wake == 12 else [])
    assert [command[-1] for command, _ in calls] == [THREAD]


def test_attempt_cap_keeps_remaining_candidates_before_starting_next_cycle(native):
    second = str(uuid.UUID(int=201))
    third = str(uuid.UUID(int=202))
    add_completed_run(native, second, NOW - 3599)
    add_completed_run(native, third, NOW - 3598)
    native.config["max_per_run"] = 1
    calls = []
    runner = archive_runner(native, calls)
    state = Path(native.config["state_dir"]) / "state.json"
    for expected in (THREAD, second, third):
        result = cleanup.run_once(native.config, apply=True, now=NOW + 10, runner=runner)
        assert result["archived"] == [expected]
        cursor = json.loads(state.read_text())["cursor"]
        assert cursor == (None if expected == third else {"created_at": (NOW - (3600 if expected == THREAD else 3599) - 60) * 1000, "thread_id": expected})
    assert [command[-1] for command, _ in calls] == [THREAD, second, third]
    assert cleanup.run_once(native.config, apply=True, now=NOW + 20, runner=never_run)["archived"] == []


def test_full_scan_batch_retains_cursor_until_bounded_tail_is_consumed(native, monkeypatch):
    monkeypatch.setattr(cleanup, "SCAN_LIMIT", 2)
    native.sql("history", "UPDATE thread_turns SET started_at=?,completed_at=?", (NOW - 60, NOW))
    native.sql("app", "UPDATE automation_runs SET created_at=?", ((NOW - 60) * 1000,))
    second = str(uuid.UUID(int=301))
    third = str(uuid.UUID(int=302))
    add_completed_run(native, second, NOW + 1)
    add_completed_run(native, third, NOW + 2)
    first = cleanup.run_once(native.config, apply=True, now=NOW + 5, runner=never_run)
    state = Path(native.config["state_dir"]) / "state.json"
    assert first["considered"] == 2
    assert json.loads(state.read_text())["cursor"]["thread_id"] == second
    tail = cleanup.run_once(native.config, apply=True, now=NOW + 10, runner=never_run)
    assert tail["considered"] == 1
    assert json.loads(state.read_text())["cursor"] is None


def test_archived_pending_review_prefix_cannot_consume_scan_limit(native):
    # Native CLI archive need not update the desktop automation inbox status.
    # More than a scan window of such old rows must not hide the current chat.
    ids = [str(uuid.UUID(int=1000 + i)) for i in range(cleanup.SCAN_LIMIT + 1)]
    with sqlite3.connect(native.paths["app"]) as db:
        db.executemany("INSERT INTO automation_runs VALUES(?,?,?,?,NULL)",
                       [(thread_id, AUTOMATION, "PENDING_REVIEW", (NOW - 10000 - i) * 1000) for i, thread_id in enumerate(ids)])
    with sqlite3.connect(native.paths["state"]) as db:
        db.executemany("INSERT INTO threads VALUES(?,?,0,0,NULL,1,?,?)",
                       [(thread_id, "automation", NOW - 5000, (NOW - 5000) * 1000) for thread_id in ids])
    calls = []
    result = cleanup.run_once(native.config, apply=True, now=NOW, runner=archive_runner(native, calls))
    assert result["considered"] == 1
    assert result["archived"] == [THREAD]
    assert [command[-1] for command, _ in calls] == [THREAD]


def test_private_config_rejects_short_delay_and_unknown_fields(native, tmp_path):
    path = tmp_path / "config.json"
    for patch in ({"minimum_age_seconds": 3599}, {"max_per_run": 11}, {"remote": "remote-host"}, {"codex_home": "relative"}, {"automation_ids": []}, {"state_dir": "//remote/share/state"}, {"codex_home": "\\\\remote\\share\\codex"}, {"state_dir": str(native.home / "maintenance")}):
        path.write_text(json.dumps({**native.config, **patch}))
        with pytest.raises(cleanup.CleanupError):
            cleanup.load_config(path)
    path.write_text(json.dumps(native.config))
    assert cleanup.load_config(path) == native.config


def test_private_config_accepts_explicit_local_cron_discovery(native, tmp_path):
    config = {key: value for key, value in native.config.items() if key != "automation_ids"}
    config["automation_selection"] = "local_cron"
    path = tmp_path / "config.json"
    path.write_text(json.dumps(config))
    assert cleanup.load_config(path) == config


@pytest.mark.parametrize("patch", [
    {"automation_selection": "local_cron"},
    {"automation_selection": "all"},
    {"automation_selection": None},
    {"automation_ids": [f"schedule-{i}" for i in range(65)]},
    {"automation_ids": [AUTOMATION, AUTOMATION]},
    {"automation_ids": ["UPPERCASE"]},
])
def test_private_config_rejects_ambiguous_or_invalid_selection(native, tmp_path, patch):
    path = tmp_path / "config.json"
    path.write_text(json.dumps({**native.config, **patch}))
    with pytest.raises(cleanup.CleanupError):
        cleanup.load_config(path)


def test_private_config_requires_selection_and_accepts_maximum_allowlist(native, tmp_path):
    config = {key: value for key, value in native.config.items() if key != "automation_ids"}
    path = tmp_path / "config.json"
    path.write_text(json.dumps(config))
    with pytest.raises(cleanup.CleanupError, match="automation_ids"):
        cleanup.load_config(path)
    config["automation_ids"] = [f"schedule-{i}" for i in range(64)]
    path.write_text(json.dumps(config))
    assert cleanup.load_config(path) == config


def test_only_one_local_maintenance_wake_can_hold_lock(native):
    root = Path(native.config["state_dir"])
    with cleanup._lock(root):
        with pytest.raises(cleanup.CleanupError, match="owns the local lock"):
            with cleanup._lock(root):
                pytest.fail("Concurrent cleanup must not acquire lock")


@pytest.mark.parametrize("prefix,version", [("state", 6), ("thread_history", 2), ("queue", 2), ("goals", 2)])
def test_newer_native_database_never_falls_back_to_stale_supported_version(native, prefix, version):
    (native.home / f"{prefix}_{version}.sqlite").write_bytes(b"newer native schema")
    with pytest.raises(cleanup.CleanupError, match="Newer native"):
        cleanup.run_once(native.config, apply=True, now=NOW, runner=never_run)


def test_native_subprocess_cannot_inherit_remote_or_provider_routing(native, monkeypatch):
    monkeypatch.setenv("CODEX_EXEC_SERVER_URL", "https://remote.invalid")
    monkeypatch.setenv("CODEX_APP_SERVER_URL", "https://remote.invalid")
    monkeypatch.setenv("OPENAI_BASE_URL", "https://provider.invalid")
    monkeypatch.setenv("OPENAI_API_KEY", "fixture-not-a-real-key")
    calls = []
    cleanup.run_once(native.config, apply=True, now=NOW, runner=archive_runner(native, calls))
    env = calls[0][1]["env"]
    assert not any(key in env for key in ("CODEX_EXEC_SERVER_URL", "CODEX_APP_SERVER_URL", "OPENAI_BASE_URL", "OPENAI_API_KEY"))
    assert env["CODEX_HOME"] == native.config["codex_home"]
