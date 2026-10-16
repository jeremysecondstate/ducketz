"""Archive completed local scheduled chats and their finished subagents after an hour.

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
TREE_LIMIT = 64
MAX_JSON = 262144
REQUIRED = {
    "app": {"automations": {"id", "kind", "rrule", "target_type", "project_id", "execution_environment"},
            "automation_runs": {"thread_id", "automation_id", "status", "created_at", "read_at"}},
    "state": {"threads": {"id", "source", "thread_source", "has_user_event", "is_pinned", "thread_section_id", "archived", "archived_at", "updated_at_ms"},
              "thread_spawn_edges": {"parent_thread_id", "child_thread_id"}},
    "history": {"thread_turns": {"thread_id", "turn_id", "root_turn_id", "status", "error_json", "started_at", "completed_at", "first_user_item_id", "final_agent_item_id"},
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
    required = {"codex_home", "codex_exe", "codex_sha256", "state_dir", "minimum_age_seconds"}
    if not required <= value.keys() or set(value) - required - {"max_per_run", "helper_sha256", "automation_ids", "automation_project_id", "automation_selection", "archive_policy", "fallback_idle_seconds"}:
        raise CleanupError("Configuration fields are missing or unknown")
    for field in ("codex_home", "codex_exe", "state_dir"):
        if not isinstance(value[field], str) or value[field].startswith(("\\\\", "//")) or not Path(value[field]).is_absolute() or str(Path(value[field]).resolve()).startswith(("\\\\", "//")):
            raise CleanupError(f"{field} must be an absolute local path")
    for field in ("codex_sha256", "helper_sha256"):
        if field in value and not re.fullmatch(r"[0-9a-f]{64}", str(value[field])):
            raise CleanupError(f"{field} must be a lowercase SHA256")
    ids = value.setdefault("automation_ids", [])
    if not isinstance(ids, list) or len(ids) > 32 or any(
            not isinstance(x, str) or not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,127}", x) for x in ids) or len(set(ids)) != len(ids):
        raise CleanupError("automation_ids must be a small exact native ID allowlist")
    project = value.get("automation_project_id")
    if project is not None and not _uuid(project):
        raise CleanupError("automation_project_id must be an exact native project UUID")
    selection = value.get("automation_selection")
    if selection is not None and (selection != "local_cron" or ids or project):
        raise CleanupError("Choose all local cron tasks or an exact ID/project scope")
    if not ids and not project and selection != "local_cron":
        raise CleanupError("Select exact automation IDs or a local automation project")
    if value.get("archive_policy", "single_successful_turn") not in {"single_successful_turn", "completed_or_idle"}:
        raise CleanupError("Unsupported archive policy")
    if "fallback_idle_seconds" in value and (type(value["fallback_idle_seconds"]) is not int or value["fallback_idle_seconds"] < 7200):
        raise CleanupError("fallback_idle_seconds must be at least 7200")
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


def _automation_scope(config: dict):
    """Native definitions, never display titles, establish the local scope."""
    clauses, args = [], []
    if config.get("automation_selection") == "local_cron":
        return "a.kind='cron' AND a.execution_environment='local'", []
    ids = config.get("automation_ids", [])
    if ids:
        clauses.append("a.id IN (" + ",".join("?" for _ in ids) + ")")
        args.extend(ids)
    if config.get("automation_project_id"):
        clauses.append("(a.target_type='project' AND a.project_id=?)")
        args.append(config["automation_project_id"])
    if not clauses:
        raise CleanupError("No local automation scope configured")
    return "a.kind='cron' AND a.execution_environment='local' AND (" + " OR ".join(clauses) + ")", args


def _definition(db, config: dict, automation_id: str):
    scope, args = _automation_scope(config)
    return _one(db, "SELECT a.id,a.kind,a.rrule,a.target_type,a.project_id,a.execution_environment FROM automations AS a WHERE a.id=? AND " + scope,
                [automation_id, *args])


def _finished_descendants(db, config: dict, root_id: str, root_turn_id: str, now: float):
    """Bounded transitive completion proof; an edge's 'open' is not turn state."""
    pending, seen, snapshots = [root_id], {root_id}, []
    while pending:
        parent = pending.pop()
        edges = db["state"].execute("SELECT parent_thread_id,child_thread_id FROM thread_spawn_edges WHERE parent_thread_id=? LIMIT ?",
                                    (parent, TREE_LIMIT + 1)).fetchall()
        for edge in edges:
            child = edge["child_thread_id"]
            if not _uuid(child) or child in seen or len(seen) >= TREE_LIMIT:
                return "UNSUPPORTED_DESCENDANT_TREE", []
            seen.add(child)
            pending.append(child)
            incoming = db["state"].execute("SELECT parent_thread_id FROM thread_spawn_edges WHERE child_thread_id=? LIMIT 2", (child,)).fetchall()
            if len(incoming) != 1 or incoming[0]["parent_thread_id"] != parent:
                return "UNSUPPORTED_DESCENDANT_TREE", []
            thread = _one(db["state"], "SELECT id,source,thread_source,has_user_event,is_pinned,thread_section_id,archived,archived_at,updated_at_ms FROM threads WHERE id=?", (child,))
            if not thread or thread["thread_source"] != "subagent" or thread["has_user_event"] != 0:
                return "DESCENDANT_MANUAL_OR_UNKNOWN", []
            try:
                source_parent = json.loads(thread["source"])["subagent"]["thread_spawn"]["parent_thread_id"]
            except (TypeError, ValueError, KeyError):
                return "DESCENDANT_MANUAL_OR_UNKNOWN", []
            if source_parent != parent or db["history"].execute("SELECT 1 FROM thread_items WHERE thread_id=? AND item_type='userMessage' LIMIT 1", (child,)).fetchone():
                return "DESCENDANT_MANUAL_OR_UNKNOWN", []
            if thread["is_pinned"] or thread["thread_section_id"] is not None or thread["archived"] or thread["archived_at"] is not None:
                return "DESCENDANT_ORGANIZED_OR_ARCHIVED", []
            if db["queue"].execute("SELECT 1 FROM queued_items WHERE thread_id=? LIMIT 1", (child,)).fetchone():
                return "DESCENDANT_QUEUED_INPUT", []
            if "goals" in db and db["goals"].execute("SELECT 1 FROM thread_goals WHERE thread_id=? AND status != 'complete' LIMIT 1", (child,)).fetchone():
                return "DESCENDANT_UNFINISHED_GOAL", []
            turns = [dict(row) for row in db["history"].execute("SELECT turn_id,root_turn_id,status,error_json,started_at,completed_at,final_agent_item_id FROM thread_turns WHERE thread_id=? ORDER BY started_at,turn_id LIMIT ?", (child, TREE_LIMIT + 1))]
            if not turns or len(turns) > TREE_LIMIT:
                return "DESCENDANT_NOT_SUCCESSFUL", []
            finals = []
            for turn in turns:
                if turn["status"] != "completed" or turn["error_json"] is not None or turn["root_turn_id"] != root_turn_id:
                    return "DESCENDANT_NOT_SUCCESSFUL", []
                start, completed = turn["started_at"], turn["completed_at"]
                if type(start) is not int or type(completed) is not int or not 0 < start <= completed <= now:
                    return "DESCENDANT_INVALID_COMPLETION_TIME", []
                if now - completed < config["minimum_age_seconds"]:
                    return "DESCENDANT_YOUNGER_THAN_MINIMUM", []
                final = _item(db["history"], child, turn["turn_id"], turn["final_agent_item_id"])
                if not final or final.get("id") != turn["final_agent_item_id"] or final.get("type") != "agentMessage" or final.get("phase") not in {"final", "final_answer"} or not isinstance(final.get("text"), str) or not final["text"].strip():
                    return "DESCENDANT_MISSING_FINAL_RESPONSE", []
                finals.append(sha256(json.dumps(final, sort_keys=True).encode()).hexdigest())
            snapshots.append({"edge": dict(edge), "thread": thread, "turns": turns, "final_sha256": finals})
    return None, sorted(snapshots, key=lambda value: value["thread"]["id"])


def _inspect_finished_chat(config: dict, thread_id: str, now: float) -> dict:
    """Age completed scheduled chats, including automatic continuation turns.

    The two-hour fallback applies only to terminal native turns. An old creation
    time never makes an active turn or queued input eligible for archival.
    """
    def reject(reason):
        return {"thread_id": thread_id, "eligible": False, "reason": reason}
    if not _uuid(thread_id):
        return reject("INVALID_NATIVE_ID")
    with _databases(config) as db:
        run = _one(db["app"], "SELECT thread_id,automation_id,status,created_at,read_at FROM automation_runs WHERE thread_id=?", (thread_id,))
        if not run or not (definition := _definition(db["app"], config, run["automation_id"])):
            return reject("NOT_ALLOWED_AUTOMATION")
        if type(run["created_at"]) is not int or not 0 < run["created_at"] <= now * 1000:
            return reject("INVALID_CREATION_TIME")
        pending, seen, snapshots = [(thread_id, None)], set(), []
        activity = [run["created_at"] / 1000]
        completions, successful = [], True
        while pending:
            identity, parent = pending.pop()
            if not _uuid(identity) or identity in seen or len(seen) >= TREE_LIMIT:
                return reject("UNSUPPORTED_DESCENDANT_TREE")
            seen.add(identity)
            thread = _one(db["state"], "SELECT id,thread_source,has_user_event,is_pinned,thread_section_id,archived,archived_at,updated_at_ms FROM threads WHERE id=?", (identity,))
            if not thread or thread["thread_source"] != ("automation" if parent is None else "subagent"):
                return reject("NOT_SCHEDULED_FAMILY")
            if parent is not None:
                parents = db["state"].execute("SELECT parent_thread_id FROM thread_spawn_edges WHERE child_thread_id=? LIMIT 2", (identity,)).fetchall()
                if len(parents) != 1 or parents[0][0] != parent or thread["has_user_event"]:
                    return reject("UNSUPPORTED_DESCENDANT_TREE")
            if thread["archived"] or thread["archived_at"] is not None:
                return reject("ALREADY_ARCHIVED")
            if thread["is_pinned"] or thread["thread_section_id"] is not None:
                return reject("PINNED_OR_ORGANIZED")
            updated = thread["updated_at_ms"]
            if type(updated) is not int or not 0 < updated <= now * 1000:
                return reject("INVALID_ACTIVITY_TIME")
            activity.append(updated / 1000)
            if db["queue"].execute("SELECT 1 FROM queued_items WHERE thread_id=? LIMIT 1", (identity,)).fetchone():
                return reject("QUEUED_INPUT")
            if "goals" in db and db["goals"].execute("SELECT 1 FROM thread_goals WHERE thread_id=? AND status != 'complete' LIMIT 1", (identity,)).fetchone():
                return reject("UNFINISHED_GOAL")
            turns = [dict(row) for row in db["history"].execute("SELECT turn_id,status,error_json,started_at,completed_at,first_user_item_id,final_agent_item_id FROM thread_turns WHERE thread_id=? ORDER BY started_at,turn_id LIMIT ?", (identity, TREE_LIMIT + 1))]
            if not turns or len(turns) > TREE_LIMIT or len({t["turn_id"] for t in turns}) != len(turns):
                return reject("UNSUPPORTED_TURNS")
            finals = []
            for turn in turns:
                if turn["status"] not in {"completed", "failed", "interrupted"}:
                    return reject("TURN_STILL_ACTIVE")
                start, completed = turn["started_at"], turn["completed_at"]
                if type(start) is not int or not 0 < start <= now:
                    return reject("INVALID_START_TIME")
                activity.append(start)
                if completed is not None:
                    if type(completed) is not int or not start <= completed <= now:
                        return reject("INVALID_COMPLETION_TIME")
                    activity.append(completed)
                    completions.append(completed)
                final = _item(db["history"], identity, turn["turn_id"], turn["final_agent_item_id"])
                valid_final = (final and final.get("id") == turn["final_agent_item_id"]
                               and final.get("type") == "agentMessage" and final.get("phase") in {"final", "final_answer"}
                               and isinstance(final.get("text"), str) and bool(final["text"].strip()))
                successful = successful and turn["status"] == "completed" and turn["error_json"] is None and completed is not None and bool(valid_final)
                finals.append(sha256(json.dumps(final, sort_keys=True).encode()).hexdigest())
            snapshots.append({"parent": parent, "thread": thread, "turns": turns, "final_sha256": finals})
            children = db["state"].execute("SELECT child_thread_id FROM thread_spawn_edges WHERE parent_thread_id=? ORDER BY child_thread_id LIMIT ?", (identity, TREE_LIMIT + 1)).fetchall()
            pending.extend((child[0], identity) for child in children)
        age_base = max(completions) if successful else max(activity)
        grace = config["minimum_age_seconds"] if successful else config.get("fallback_idle_seconds", 7200)
        if now - age_base < grace:
            return reject("YOUNGER_THAN_MINIMUM" if successful else "YOUNGER_THAN_FALLBACK")
        descendants = sorted(seen - {thread_id})
        snapshot = {"run": run, "definition": definition, "family": snapshots}
        return {"thread_id": thread_id, "automation_id": run["automation_id"], "eligible": True,
                "completed_at": max(completions) if completions else None,
                "eligible_at": age_base + grace, "age_basis": "completion" if successful else "terminal_idle",
                "descendant_count": len(descendants), "descendant_ids": descendants,
                "snapshot_sha256": sha256(json.dumps(snapshot, sort_keys=True).encode()).hexdigest()}


def inspect_candidate(config: dict, thread_id: str, now: float) -> dict:
    """A fresh bounded native snapshot; no writes and no model invocation."""
    if config.get("archive_policy") == "completed_or_idle":
        return _inspect_finished_chat(config, thread_id, now)
    def reject(reason):
        return {"thread_id": thread_id, "eligible": False, "reason": reason}
    if not _uuid(thread_id):
        return reject("INVALID_NATIVE_ID")
    with _databases(config) as db:
        run = _one(db["app"], "SELECT thread_id,automation_id,status,created_at,read_at FROM automation_runs WHERE thread_id=?", (thread_id,))
        if not run:
            return reject("NOT_ALLOWED_AUTOMATION")
        definition = _definition(db["app"], config, run["automation_id"])
        if not definition:
            return reject("NOT_ALLOWED_AUTOMATION")
        # Opening a chat only marks it read. Manual input is excluded below by
        # native thread metadata, turn count and the original scheduled item.
        if run["status"] != "PENDING_REVIEW":
            return reject("RUN_NOT_PENDING_REVIEW")
        thread = _one(db["state"], "SELECT id,thread_source,has_user_event,is_pinned,thread_section_id,archived,archived_at,updated_at_ms FROM threads WHERE id=?", (thread_id,))
        if not thread or thread["thread_source"] != "automation" or thread["has_user_event"] != 0:
            return reject("NOT_UNTOUCHED_AUTOMATION")
        if thread["archived"] or thread["archived_at"] is not None:
            return reject("ALREADY_ARCHIVED")
        if thread["is_pinned"] or thread["thread_section_id"] is not None:
            return reject("PINNED_OR_ORGANIZED")
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
        tree_error, descendants = _finished_descendants(db, config, thread_id, turn["turn_id"], now)
        if tree_error:
            return reject(tree_error)
        snapshot = {"run": run, "definition": definition, "thread": thread, "turn": turn, "descendants": descendants,
                    "initial_item_sha256": sha256(json.dumps(first, sort_keys=True).encode()).hexdigest(),
                    "final_item_sha256": sha256(json.dumps(final, sort_keys=True).encode()).hexdigest()}
        return {"thread_id": thread_id, "automation_id": run["automation_id"], "eligible": True,
                "completed_at": completed, "eligible_at": completed + config["minimum_age_seconds"],
                "turn_id": turn["turn_id"], "final_item_id": turn["final_agent_item_id"],
                "descendant_count": len(descendants), "descendant_ids": [value["thread"]["id"] for value in descendants],
                "snapshot_sha256": sha256(json.dumps(snapshot, sort_keys=True).encode()).hexdigest()}


def _archived(config: dict, thread_id: str, descendant_ids=()) -> bool | None:
    with _databases(config) as db:
        ids = [thread_id, *descendant_ids]
        rows = db["state"].execute("SELECT id,archived FROM threads WHERE id IN (" + ",".join("?" for _ in ids) + ")", ids).fetchall()
        return None if len(rows) != len(ids) else all(row["archived"] == 1 for row in rows)


def _all_unarchived(config: dict, thread_ids) -> bool:
    ids = sorted(set(thread_ids))
    with _databases(config) as db:
        rows = db["state"].execute("SELECT id,archived,archived_at FROM threads WHERE id IN (" + ",".join("?" for _ in ids) + ")", ids).fetchall()
        return len(rows) == len(ids) and all(row["archived"] == 0 and row["archived_at"] is None for row in rows)


def _foreign_writer_busy(config: dict, thread_ids) -> bool:
    """Advisory Windows lease check without creating or changing lock files.

    Native archive still takes its own atomic tree lease after this check.
    Completed idle subagents can retain a writer lease in the desktop app.
    """
    if os.name != "nt":
        return False
    import ctypes
    from ctypes import wintypes

    class Overlapped(ctypes.Structure):
        _fields_ = [("Internal", ctypes.c_size_t), ("InternalHigh", ctypes.c_size_t),
                    ("Offset", wintypes.DWORD), ("OffsetHigh", wintypes.DWORD),
                    ("hEvent", wintypes.HANDLE)]

    native = ctypes.WinDLL("kernel32", use_last_error=True)
    create = native.CreateFileW
    create.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p,
                       wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
    create.restype = wintypes.HANDLE
    lock = native.LockFileEx
    lock.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.DWORD,
                     wintypes.DWORD, wintypes.DWORD, ctypes.POINTER(Overlapped)]
    lock.restype = wintypes.BOOL
    unlock = native.UnlockFileEx
    unlock.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.DWORD,
                       wintypes.DWORD, ctypes.POINTER(Overlapped)]
    unlock.restype = wintypes.BOOL
    close = native.CloseHandle
    close.argtypes = [wintypes.HANDLE]
    close.restype = wintypes.BOOL
    for thread_id in thread_ids:
        if not _uuid(thread_id):
            raise CleanupError("Invalid native writer identity")
        path = Path(config["codex_home"]) / "thread-writer-locks" / f"{thread_id}.lock"
        # GENERIC_READ; share read/write/delete; OPEN_EXISTING. Never create,
        # truncate or read bytes from a production writer-lock file.
        handle = create(str(path), 0x80000000, 7, None, 3, 0x80, None)
        if handle == ctypes.c_void_p(-1).value:
            error = ctypes.get_last_error()
            if error in {2, 3}:
                continue
            if error == 32:
                return True
            raise CleanupError(f"Native writer lock inspection failed ({error})")
        try:
            overlapped = Overlapped()
            if not lock(handle, 3, 0, 0xffffffff, 0xffffffff, ctypes.byref(overlapped)):
                error = ctypes.get_last_error()
                if error == 33:
                    return True
                raise CleanupError(f"Native writer lease inspection failed ({error})")
            if not unlock(handle, 0, 0xffffffff, 0xffffffff, ctypes.byref(overlapped)):
                raise CleanupError("Native writer lease release failed")
        finally:
            close(handle)
    return False


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
        # CLI archival changes native thread state independently of the app's
        # automation inbox row. Exclude that retained archive history before the
        # scan limit, using another explicitly read-only SQLite attachment.
        state_uri = (Path(config["codex_home"]) / DB_PATHS["state"]).resolve().as_uri() + "?mode=ro"
        db["app"].execute("ATTACH DATABASE ? AS cleanup_state", (state_uri,))
        scope, args = _automation_scope(config)
        completed_filter = "" if config.get("archive_policy") == "completed_or_idle" else " AND r.status='PENDING_REVIEW' AND t.has_user_event=0"
        query = f"""SELECT r.thread_id,r.created_at FROM automation_runs AS r
            JOIN cleanup_state.threads AS t ON t.id=r.thread_id
            JOIN automations AS a ON a.id=r.automation_id
            WHERE {scope}{completed_filter}
              AND t.archived=0 AND t.thread_source='automation'
              AND t.is_pinned=0 AND t.thread_section_id IS NULL"""
        if cursor:
            query += " AND (r.created_at>? OR (r.created_at=? AND r.thread_id>?))"
            args.extend((cursor["created_at"], cursor["created_at"], cursor["thread_id"]))
        query += " ORDER BY r.created_at,r.thread_id LIMIT ?"
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
        retry_failed = bool(prior and config.get("archive_policy") == "completed_or_idle"
                            and prior["status"] == "failed" and type(prior.get("native_exit_code")) is int
                            and prior["native_exit_code"] != 0 and prior.get("retry_safe") is True)
        if prior and prior["status"] != "writer_busy" and not retry_failed:
            archived = _archived(config, thread_id, prior.get("descendant_ids", []))
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
        if retry_failed and not _all_unarchived(config, [thread_id, *candidate["descendant_ids"], *prior.get("descendant_ids", [])]):
            result["skipped"].append({"thread_id": thread_id, "reason": "PRIOR_PARTIAL_OR_UNKNOWN_ARCHIVE"})
            result["status"] = "FAILED"
            continue
        if prior and not retry_failed and config.get("archive_policy") != "completed_or_idle" and prior.get("snapshot_sha256") != candidate["snapshot_sha256"]:
            result["skipped"].append({"thread_id": thread_id, "reason": "CHANGED_AFTER_BUSY_ATTEMPT"})
            continue
        result["eligible"].append(candidate)
        if apply and _foreign_writer_busy(config, [thread_id, *candidate["descendant_ids"]]):
            result["skipped"].append({"thread_id": thread_id, "reason": "NATIVE_WRITER_BUSY"})
            continue
        attempts += 1
        if not apply:
            continue
        # Foreign-writer leases are checked atomically by native CLI, including
        # transitive descendants. Every descendant was checked for successful
        # completion and is included in the fresh snapshot fingerprint.
        fresh = inspect_candidate(config, thread_id, now)
        if fresh != candidate:
            result["skipped"].append({"thread_id": thread_id, "reason": "CHANGED_BEFORE_ARCHIVE"})
            continue
        _verify_binaries(config)
        attempt = {**candidate, "version": VERSION, "attempt_id": uuid.uuid4().hex,
                   "attempted_at": now, "status": "attempting", "native_sha256": config["codex_sha256"]}
        if prior:
            # Historical events are append-only, including legacy failures whose
            # snapshot format predates this policy. Never erase that evidence.
            attempt["previous_attempt_id"] = prior["attempt_id"]
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
            archived = _archived(config, thread_id, candidate["descendant_ids"])
            fully_unarchived = False if archived else _all_unarchived(config, [thread_id, *candidate["descendant_ids"]])
            message = (outcome.stderr or outcome.stdout or "")[-2000:]
            if archived:
                receipt = {**attempt, "status": "archived", "native_exit_code": outcome.returncode,
                           "archived_verified_at": time.time()}
                result["archived"].append(thread_id)
            elif fully_unarchived and outcome.returncode != 0 and re.search(r"writer.*(?:lock|lease)|(?:lock|lease).*writer", message, re.IGNORECASE):
                receipt = {**attempt, "status": "writer_busy", "native_exit_code": outcome.returncode}
            else:
                receipt = {**attempt, "status": "failed" if outcome.returncode != 0 and fully_unarchived else "unknown",
                           "native_exit_code": outcome.returncode, "error": message,
                           "retry_safe": fully_unarchived, "unarchived_verified_at": time.time() if fully_unarchived else None}
                result["status"] = "FAILED"
        except (OSError, subprocess.TimeoutExpired, sqlite3.Error, CleanupError) as error:
            receipt = {**attempt, "status": "unknown", "error": f"{type(error).__name__}: {error}"}
            result["status"] = "FAILED"
        _record(root, receipt)
    else:
        # A short, fully consumed query reaches the end of this bounded snapshot.
        # Begin another cycle next wake so previously young chats are revisited
        # even when new scheduled runs keep arriving beyond the old cursor.
        # A break at the attempt cap must retain the last processed cursor.
        if len(rows) < SCAN_LIMIT:
            cursor = None
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
