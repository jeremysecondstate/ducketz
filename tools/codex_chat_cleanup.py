"""Archive untouched completed five-minute cron leaf chats after a full hour.

Standalone standard-library maintenance, suitable for ``python -I -B``. Native
databases are read-only. Only the pinned ``codex --no-daemon archive UUID`` CLI
mutates native state; its foreign-writer guard refuses active conversations.
The immediate snapshot recheck is not an atomic compare-and-swap for idle edits.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
from datetime import datetime, timezone
from hashlib import sha256
import json
import os
from pathlib import Path
import re
import sqlite3
import subprocess
import sys
import time
import uuid

VERSION = "codex-chat-cleanup-v1"
SCAN_LIMIT = 200
MAX_JSON = 262144
REQUIRED = {
    "app": {"automations": {"id", "kind", "rrule"},
            "automation_runs": {"thread_id", "automation_id", "status", "created_at", "read_at"}},
    "state": {"threads": {"id", "thread_source", "has_user_event", "is_pinned", "thread_section_id", "archived", "archived_at", "updated_at_ms"},
              "thread_spawn_edges": {"parent_thread_id", "child_thread_id"}},
    "history": {"thread_turns": {"thread_id", "turn_id", "status", "error_json", "started_at", "completed_at", "first_user_item_id", "final_agent_item_id"},
                "thread_items": {"thread_id", "turn_id", "item_id", "item_json", "item_type"}},
    "queue": {"queued_items": {"thread_id"}},
}
DB_PATHS = {"app": "sqlite/codex-dev.db", "state": "state_5.sqlite",
            "history": "thread_history_1.sqlite", "queue": "queue_1.sqlite"}


class CleanupError(ValueError):
    pass


def _json(path: Path, limit: int = MAX_JSON):
    if path.stat().st_size > limit:
        raise CleanupError("JSON exceeds bounded size")
    return json.loads(path.read_text(encoding="utf-8-sig"))


def _hash(path: Path) -> str:
    digest = sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _uuid(value: str) -> bool:
    try:
        return str(uuid.UUID(value)) == value
    except (ValueError, TypeError, AttributeError):
        return False


def load_config(path: Path) -> dict:
    value = _json(Path(path))
    if not isinstance(value, dict):
        raise CleanupError("Configuration must be an object")
    required = {"codex_home", "codex_exe", "codex_sha256", "automation_ids", "state_dir", "minimum_age_seconds"}
    if not required <= value.keys() or set(value) - required - {"max_per_run", "helper_sha256"}:
        raise CleanupError("Configuration fields are missing or unknown")
    for field in ("codex_home", "codex_exe", "state_dir"):
        if not isinstance(value[field], str) or value[field].startswith(("\\\\", "//")) or not Path(value[field]).is_absolute() or str(Path(value[field]).resolve()).startswith(("\\\\", "//")):
            raise CleanupError(f"{field} must be an absolute local path")
    for field in ("codex_sha256", "helper_sha256"):
        if field in value and not re.fullmatch(r"[0-9a-f]{64}", str(value[field])):
            raise CleanupError(f"{field} must be a lowercase SHA256")
    ids = value["automation_ids"]
    if not isinstance(ids, list) or not 1 <= len(ids) <= 8 or len(set(ids)) != len(ids) or any(
            not isinstance(x, str) or not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,127}", x) for x in ids):
        raise CleanupError("automation_ids must be a small exact native ID allowlist")
    if type(value["minimum_age_seconds"]) is not int or value["minimum_age_seconds"] < 3600:
        raise CleanupError("minimum_age_seconds must be at least 3600")
    value.setdefault("max_per_run", 10)
    if type(value["max_per_run"]) is not int or not 1 <= value["max_per_run"] <= 10:
        raise CleanupError("max_per_run must be between one and ten")
    if Path(value["state_dir"]).resolve().is_relative_to(Path(value["codex_home"]).resolve()):
        raise CleanupError("Maintenance state must be separate from Codex home")
    return value


def _verify_binaries(config: dict) -> None:
    if _hash(Path(config["codex_exe"])) != config["codex_sha256"]:
        raise CleanupError("Native executable changed; review and repin before cleanup")
    if config.get("helper_sha256") and _hash(Path(__file__)) != config["helper_sha256"]:
        raise CleanupError("Maintenance helper changed; review and repin before cleanup")


@contextmanager
def _databases(config: dict):
    opened = {}
    try:
        home = Path(config["codex_home"])
        for prefix, supported in (("state", 5), ("thread_history", 1), ("queue", 1), ("goals", 1)):
            for candidate in home.glob(f"{prefix}_*.sqlite"):
                match = re.fullmatch(re.escape(prefix) + r"_(\d+)\.sqlite", candidate.name)
                if match and int(match[1]) > supported:
                    raise CleanupError(f"Newer native {prefix} database requires review")
        for name, relative in DB_PATHS.items():
            path = Path(config["codex_home"]) / relative
            if not path.is_file():
                raise CleanupError(f"Required native {name} database unavailable")
            connection = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=1)
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA query_only=ON")
            opened[name] = connection
            for table, required in REQUIRED[name].items():
                columns = {row[1] for row in connection.execute(f"PRAGMA table_info({table})")}
                if not required <= columns:
                    raise CleanupError(f"Unsupported native schema: {name}.{table}")
        goals = Path(config["codex_home"]) / "goals_1.sqlite"
        if goals.exists():
            opened["goals"] = sqlite3.connect(goals.resolve().as_uri() + "?mode=ro", uri=True, timeout=1)
            cols = {x[1] for x in opened["goals"].execute("PRAGMA table_info(thread_goals)")}
            if not {"thread_id", "status"} <= cols:
                raise CleanupError("Unsupported native goals schema")
        yield opened
    finally:
        for connection in opened.values():
            connection.close()


def _one(connection, sql, parameters):
    rows = connection.execute(sql, parameters).fetchmany(2)
    if len(rows) != 1:
        return None
    return dict(rows[0])


def _item(connection, thread_id: str, turn_id: str, item_id: str):
    row = _one(connection, "SELECT item_json,length(item_json) AS size FROM thread_items WHERE thread_id=? AND turn_id=? AND item_id=? AND length(item_json)<=?", (thread_id, turn_id, item_id, MAX_JSON))
    if not row:
        return None
    value = json.loads(row["item_json"])
    return value if isinstance(value, dict) else None


def inspect_candidate(config: dict, thread_id: str, now: float) -> dict:
    """A fresh bounded native snapshot; no writes and no model invocation."""
    def reject(reason):
        return {"thread_id": thread_id, "eligible": False, "reason": reason}
    if not _uuid(thread_id):
        return reject("INVALID_NATIVE_ID")
    with _databases(config) as db:
        run = _one(db["app"], "SELECT thread_id,automation_id,status,created_at,read_at FROM automation_runs WHERE thread_id=?", (thread_id,))
        if not run or run["automation_id"] not in config["automation_ids"]:
            return reject("NOT_ALLOWED_AUTOMATION")
        definition = _one(db["app"], "SELECT id,kind,rrule FROM automations WHERE id=?", (run["automation_id"],))
        if not definition or definition["kind"] != "cron" or set(definition["rrule"].split(";")) != {"FREQ=MINUTELY", "INTERVAL=5"}:
            return reject("NOT_FIVE_MINUTE_CRON")
        if run["status"] != "PENDING_REVIEW" or run["read_at"] is not None:
            return reject("RUN_NOT_UNTOUCHED")
        thread = _one(db["state"], "SELECT id,thread_source,has_user_event,is_pinned,thread_section_id,archived,archived_at,updated_at_ms FROM threads WHERE id=?", (thread_id,))
        if not thread or thread["thread_source"] != "automation" or thread["has_user_event"] != 0:
            return reject("NOT_UNTOUCHED_AUTOMATION")
        if thread["archived"] or thread["archived_at"] is not None:
            return reject("ALREADY_ARCHIVED")
        if thread["is_pinned"] or thread["thread_section_id"] is not None:
            return reject("PINNED_OR_ORGANIZED")
        if db["state"].execute("SELECT 1 FROM thread_spawn_edges WHERE parent_thread_id=? LIMIT 1", (thread_id,)).fetchone():
            return reject("HAS_DESCENDANTS")
        if db["queue"].execute("SELECT 1 FROM queued_items WHERE thread_id=? LIMIT 1", (thread_id,)).fetchone():
            return reject("QUEUED_INPUT")
        if "goals" in db and db["goals"].execute("SELECT 1 FROM thread_goals WHERE thread_id=? AND status != 'complete' LIMIT 1", (thread_id,)).fetchone():
            return reject("UNFINISHED_GOAL")
        turn = _one(db["history"], "SELECT turn_id,status,error_json,started_at,completed_at,first_user_item_id,final_agent_item_id FROM thread_turns WHERE thread_id=?", (thread_id,))
        if not turn or turn["status"] != "completed" or turn["error_json"] is not None:
            return reject("NOT_SINGLE_SUCCESSFUL_TURN")
        completed = turn["completed_at"]
        started = turn["started_at"]
        if type(completed) is not int or type(started) is not int or not 0 < started <= completed <= now:
            return reject("INVALID_COMPLETION_TIME")
        if now - completed < config["minimum_age_seconds"]:
            return reject("YOUNGER_THAN_MINIMUM")
        # Scheduling mapping is milliseconds; native turn times are whole seconds.
        if type(run["created_at"]) is not int or not run["created_at"] - 1000 <= started * 1000 <= run["created_at"] + 60000:
            return reject("START_NOT_SCHEDULED_RUN")
        if not turn["first_user_item_id"] or not turn["final_agent_item_id"]:
            return reject("MISSING_FINAL_OR_INITIAL_ITEM")
        users = db["history"].execute("SELECT item_id FROM thread_items WHERE thread_id=? AND item_type='userMessage' LIMIT 2", (thread_id,)).fetchall()
        if len(users) != 1:
            return reject("MANUAL_OR_ADDITIONAL_USER_INPUT")
        first = _item(db["history"], thread_id, turn["turn_id"], turn["first_user_item_id"])
        final = _item(db["history"], thread_id, turn["turn_id"], turn["final_agent_item_id"])
        text = "\n".join(x.get("text", "") for x in first.get("content", []) if isinstance(x, dict)) if first else ""
        if not first or first.get("type") != "userMessage" or not text.startswith("Automation: ") or f"\nAutomation ID: {run['automation_id']}\n" not in text:
            return reject("INITIAL_ITEM_NOT_SCHEDULED")
        if not final or final.get("id") != turn["final_agent_item_id"] or final.get("type") != "agentMessage" or final.get("phase") not in {"final", "final_answer"} or not isinstance(final.get("text"), str) or not final["text"].strip():
            return reject("MISSING_FINAL_RESPONSE")
        snapshot = {"run": run, "definition": definition, "thread": thread, "turn": turn,
                    "initial_item_sha256": sha256(json.dumps(first, sort_keys=True).encode()).hexdigest(),
                    "final_item_sha256": sha256(json.dumps(final, sort_keys=True).encode()).hexdigest()}
        return {"thread_id": thread_id, "automation_id": run["automation_id"], "eligible": True,
                "completed_at": completed, "eligible_at": completed + config["minimum_age_seconds"],
                "turn_id": turn["turn_id"], "final_item_id": turn["final_agent_item_id"],
                "snapshot_sha256": sha256(json.dumps(snapshot, sort_keys=True).encode()).hexdigest()}


def _archived(config: dict, thread_id: str) -> bool | None:
    with _databases(config) as db:
        row = db["state"].execute("SELECT archived FROM threads WHERE id=?", (thread_id,)).fetchone()
        return None if row is None else row[0] == 1


def _write(path: Path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    with temporary.open("x", encoding="utf-8") as stream:
        json.dump(value, stream, sort_keys=True, indent=2)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)


@contextmanager
def _lock(root: Path):
    root.mkdir(parents=True, exist_ok=True)
    with (root / "wake.lock").open("a+b") as stream:
        stream.seek(0)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as error:
            raise CleanupError("Another cleanup invocation owns the local lock") from error
        try:
            yield
        finally:
            stream.seek(0)
            if os.name == "nt":
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


def _receipt(root: Path, thread_id: str):
    path = root / "receipts" / f"{thread_id}.json"
    if not path.exists():
        return None
    value = _json(path)
    if not isinstance(value, dict) or value.get("version") != VERSION or value.get("thread_id") != thread_id or value.get("status") not in {"attempting", "archived", "restored", "writer_busy", "failed", "unknown"} or not re.fullmatch(r"[0-9a-f]{32}", str(value.get("attempt_id"))) or not re.fullmatch(r"[0-9a-f]{64}", str(value.get("snapshot_sha256"))):
        raise CleanupError("Malformed cleanup receipt")
    return value


def _record(root: Path, value: dict) -> None:
    _write(root / "receipts" / f"{value['thread_id']}.json", value)
    with (root / "events.jsonl").open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(value, sort_keys=True) + "\n")
        stream.flush()
        os.fsync(stream.fileno())


def _candidates(config, cursor):
    if cursor is not None and (not isinstance(cursor, dict) or set(cursor) != {"created_at", "thread_id"} or type(cursor["created_at"]) is not int or not _uuid(cursor["thread_id"])):
        raise CleanupError("Malformed cleanup cursor")
    with _databases(config) as db:
        placeholders = ",".join("?" for _ in config["automation_ids"])
        query = f"SELECT thread_id,created_at FROM automation_runs WHERE automation_id IN ({placeholders}) AND status='PENDING_REVIEW'"
        args = list(config["automation_ids"])
        if cursor:
            query += " AND (created_at>? OR (created_at=? AND thread_id>?))"
            args.extend((cursor["created_at"], cursor["created_at"], cursor["thread_id"]))
        query += " ORDER BY created_at,thread_id LIMIT ?"
        args.append(SCAN_LIMIT)
        return [dict(row) for row in db["app"].execute(query, args)]


def _run(config: dict, apply: bool, now: float, runner) -> dict:
    _verify_binaries(config)
    root = Path(config["state_dir"])
    state_path = root / "state.json"
    state = _json(state_path) if state_path.exists() else {"version": VERSION, "cursor": None}
    if not isinstance(state, dict) or set(state) != {"version", "cursor"} or state["version"] != VERSION:
        raise CleanupError("Malformed cleanup state; preserve it for review")
    rows = _candidates(config, state["cursor"])
    if not rows and state["cursor"]:
        rows = _candidates(config, None)
    result = {"version": VERSION, "status": "OK" if apply else "DRY_RUN", "observed_at": datetime.fromtimestamp(now, timezone.utc).isoformat(), "apply": apply, "considered": 0, "eligible": [], "archived": [], "skipped": []}
    cursor = state["cursor"]
    attempts = 0
    for row in rows:
        if attempts >= config["max_per_run"]:
            break
        thread_id = row["thread_id"]
        if not _uuid(thread_id):
            raise CleanupError("Invalid native candidate identity")
        cursor = row
        result["considered"] += 1
        prior = _receipt(root, thread_id)
        if prior and prior["status"] != "writer_busy":
            archived = _archived(config, thread_id)
            if apply and archived and prior["status"] in {"attempting", "unknown", "failed"}:
                _record(root, {**prior, "status": "archived", "reconciled_at": now})
            elif apply and archived is False and prior["status"] == "archived":
                _record(root, {**prior, "status": "restored", "restored_observed_at": now})
            result["skipped"].append({"thread_id": thread_id, "reason": "PRIOR_ATTEMPT_OR_RESTORED"})
            if not archived and prior["status"] in {"attempting", "unknown", "failed"}:
                result["status"] = "FAILED"
            continue
        candidate = inspect_candidate(config, thread_id, now)
        if not candidate["eligible"]:
            result["skipped"].append(candidate)
            continue
        if prior and prior.get("snapshot_sha256") != candidate["snapshot_sha256"]:
            result["skipped"].append({"thread_id": thread_id, "reason": "CHANGED_AFTER_BUSY_ATTEMPT"})
            continue
        result["eligible"].append(candidate)
        attempts += 1
        if not apply:
            continue
        # Foreign-writer leases are checked atomically by native CLI, including
        # transitive descendants. This version rejects all parent chats anyway.
        fresh = inspect_candidate(config, thread_id, now)
        if fresh != candidate:
            result["skipped"].append({"thread_id": thread_id, "reason": "CHANGED_BEFORE_ARCHIVE"})
            continue
        _verify_binaries(config)
        attempt = {**candidate, "version": VERSION, "attempt_id": uuid.uuid4().hex,
                   "attempted_at": now, "status": "attempting", "native_sha256": config["codex_sha256"]}
        _record(root, attempt)
        command = [config["codex_exe"], "--no-daemon", "archive", thread_id]
        # Maintenance has no network/model job. Inherit only OS/user directories;
        # CODEX_EXEC_SERVER_URL and other remote/provider routing overrides must
        # never redirect this explicitly local native archive command.
        allowed_environment = {"SYSTEMROOT", "WINDIR", "COMSPEC", "TEMP", "TMP",
                               "USERPROFILE", "APPDATA", "LOCALAPPDATA", "HOMEDRIVE",
                               "HOMEPATH", "USERNAME", "USERDOMAIN", "PROGRAMDATA", "HOME"}
        environment = {key: value for key, value in os.environ.items() if key.upper() in allowed_environment}
        environment["CODEX_HOME"] = config["codex_home"]
        try:
            outcome = runner(command, env=environment, capture_output=True, text=True,
                             timeout=15, check=False, shell=False,
                             creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            archived = _archived(config, thread_id)
            message = (outcome.stderr or outcome.stdout or "")[-2000:]
            if archived:
                receipt = {**attempt, "status": "archived", "native_exit_code": outcome.returncode,
                           "archived_verified_at": time.time()}
                result["archived"].append(thread_id)
            elif outcome.returncode != 0 and re.search(r"writer.*(?:lock|lease)|(?:lock|lease).*writer", message, re.IGNORECASE):
                receipt = {**attempt, "status": "writer_busy", "native_exit_code": outcome.returncode}
            else:
                receipt = {**attempt, "status": "unknown" if outcome.returncode == 0 else "failed",
                           "native_exit_code": outcome.returncode, "error": message}
                result["status"] = "FAILED"
        except (OSError, subprocess.TimeoutExpired, sqlite3.Error, CleanupError) as error:
            receipt = {**attempt, "status": "unknown", "error": f"{type(error).__name__}: {error}"}
            result["status"] = "FAILED"
        _record(root, receipt)
    if apply:
        _write(state_path, {"version": VERSION, "cursor": cursor})
        _write(root / "last.json", result)
    return result


def run_once(config: dict, *, apply: bool = False, now: float | None = None, runner=None) -> dict:
    now = time.time() if now is None else now
    runner = subprocess.run if runner is None else runner
    if not apply:
        return _run(config, False, now, runner)
    root = Path(config["state_dir"])
    with _lock(root):
        try:
            return _run(config, True, now, runner)
        except Exception as error:
            _write(root / "last.json", {"version": VERSION, "status": "FAILED", "observed_at": datetime.fromtimestamp(now, timezone.utc).isoformat(), "error": f"{type(error).__name__}: {error}"})
            raise


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--apply", action="store_true", help="Archive eligible chats; default only inspects")
    args = parser.parse_args(argv)
    try:
        result = run_once(load_config(args.config), apply=args.apply)
        print(json.dumps(result, indent=2))
        return int(result["status"] == "FAILED")
    except Exception as error:
        print(json.dumps({"version": VERSION, "status": "FAILED", "error": f"{type(error).__name__}: {error}"}))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
