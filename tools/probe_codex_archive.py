"""Verify native Codex archive writer guards using disposable Windows fixtures.

This probe never opens a real Codex home. It invokes only ``--version`` and
``--no-daemon archive <synthetic UUID>``. Proofs bind to the tested binary hash;
they do not authorize cleanup or prove a different binary is safe.
"""

from __future__ import annotations

import argparse
import ctypes
from ctypes import wintypes
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
from typing import Any
import uuid


TIMEOUT_SECONDS = 30
PROBE_NAME = "codex-native-archive-writer-guard"


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def require(condition: bool, message: str) -> None:
    if not condition:
        raise RuntimeError(message)


def fixture(root: Path, name: str) -> Path:
    home = root / name
    home.mkdir(exist_ok=False)
    (home / "config.toml").write_text(
        "[analytics]\nenabled = false\n", encoding="utf-8"
    )
    for relative in ("tmp", "AppData/Local", "AppData/Roaming"):
        (home / relative).mkdir(parents=True, exist_ok=True)
    return home


def isolated_environment(home: Path) -> dict[str, str]:
    # An allowlist avoids inheriting provider keys, app routing, and auth settings.
    allowed = {
        "SYSTEMROOT", "WINDIR", "PATH", "PATHEXT", "COMSPEC", "SYSTEMDRIVE",
        "PROCESSOR_ARCHITECTURE", "PROCESSOR_IDENTIFIER", "NUMBER_OF_PROCESSORS", "OS",
    }
    environment = {key: value for key, value in os.environ.items() if key.upper() in allowed}
    environment.update({
        "CODEX_HOME": str(home),
        "HOME": str(home),
        "USERPROFILE": str(home),
        "HOMEDRIVE": home.drive,
        "HOMEPATH": str(home)[len(home.drive):],
        "APPDATA": str(home / "AppData/Roaming"),
        "LOCALAPPDATA": str(home / "AppData/Local"),
        "TEMP": str(home / "tmp"),
        "TMP": str(home / "tmp"),
        "HTTP_PROXY": "http://127.0.0.1:9",
        "HTTPS_PROXY": "http://127.0.0.1:9",
        "ALL_PROXY": "http://127.0.0.1:9",
        "NO_PROXY": "",
    })
    return environment


def invoke(executable: Path, home: Path, arguments: list[str]) -> dict[str, Any]:
    command = [str(executable), *arguments]
    result = subprocess.run(
        command,
        cwd=home,
        env=isolated_environment(home),
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=TIMEOUT_SECONDS,
        creationflags=subprocess.CREATE_NO_WINDOW,
        check=False,
    )
    require(len(result.stdout) + len(result.stderr) <= 65536, "Unexpected excessive native output")
    return {
        "command": command,
        "returncode": result.returncode,
        "stdout": result.stdout,
        "stderr": result.stderr,
    }


def rollout(home: Path, thread_id: str, source: Any = "cli", paginated: bool = False) -> Path:
    timestamp = "2026-01-01T10:00:00.000Z"
    path = home / "sessions/2026/01/01" / f"rollout-2026-01-01T10-00-00-{thread_id}.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    metadata = {
        "id": thread_id,
        "timestamp": timestamp,
        "cwd": str(home),
        "originator": "synthetic-archive-safety-probe",
        "cli_version": "synthetic-fixture",
        "source": source,
        "model_provider": "openai",
    }
    if paginated:
        metadata["history_mode"] = "paginated"
    items = [
        {"timestamp": timestamp, "type": "session_meta", "payload": metadata},
        {"timestamp": timestamp, "type": "response_item", "payload": {
            "type": "message", "role": "user",
            "content": [{"type": "input_text", "text": "Synthetic offline fixture"}],
        }},
        {"timestamp": timestamp, "type": "response_item", "payload": {
            "type": "message", "role": "assistant", "phase": "final",
            "content": [{"type": "output_text", "text": "Synthetic completed response"}],
        }},
    ]
    path.write_text("".join(json.dumps(item) + "\n" for item in items), encoding="utf-8")
    return path


def state_database(home: Path) -> Path:
    paths = list(home.glob("state_*.sqlite"))
    require(len(paths) == 1, "Expected exactly one synthetic state database")
    return paths[0]


def read_archived(home: Path) -> dict[str, int]:
    with sqlite3.connect(state_database(home).as_uri() + "?mode=ro", uri=True) as database:
        return dict(database.execute("SELECT id, archived FROM threads"))


def tree_fixture(root: Path, template_home: Path, name: str, paginated: bool) -> tuple[Path, list[str], list[Path]]:
    home = fixture(root, name)
    thread_ids = [str(uuid.uuid4()) for _ in range(3)]
    sources = ["cli", *[
        {"subagent": {"thread_spawn": {"parent_thread_id": thread_ids[depth - 1], "depth": depth}}}
        for depth in (1, 2)
    ]]
    paths = [rollout(home, thread_id, source, paginated) for thread_id, source in zip(thread_ids, sources)]
    # The template was generated by this probe's first synthetic native invocation.
    template_path = state_database(template_home)
    with sqlite3.connect(template_path.as_uri() + "?mode=ro", uri=True) as template:
        with sqlite3.connect(home / template_path.name) as database:
            template.backup(database)
            database.execute("DELETE FROM threads")
            database.execute("DELETE FROM thread_spawn_edges")
            for thread_id, path, source in zip(thread_ids, paths, sources):
                database.execute(
                    """INSERT INTO threads(
                        id, rollout_path, created_at, updated_at, source,
                        model_provider, cwd, title, sandbox_policy, approval_mode,
                        has_user_event, history_mode)
                    VALUES(?, ?, 1767261600, 1767261600, ?, 'openai', ?,
                        'Synthetic tree fixture', 'read-only', 'never', 1, ?)""",
                    (thread_id, str(path), source if isinstance(source, str) else json.dumps(source),
                     str(home), "paginated" if paginated else "legacy"),
                )
            database.executemany(
                "INSERT INTO thread_spawn_edges VALUES(?, ?, 'completed')",
                [(thread_ids[0], thread_ids[1]), (thread_ids[1], thread_ids[2])],
            )
    return home, thread_ids, paths


class ExclusiveWriterLock:
    """Hold the same full-range Windows file lock used by native thread writers."""

    def __init__(self, home: Path, thread_id: str):
        import msvcrt

        class Overlapped(ctypes.Structure):
            _fields_ = [
                ("Internal", ctypes.c_size_t), ("InternalHigh", ctypes.c_size_t),
                ("Offset", wintypes.DWORD), ("OffsetHigh", wintypes.DWORD),
                ("hEvent", wintypes.HANDLE),
            ]

        path = home / "thread-writer-locks" / f"{thread_id}.lock"
        path.parent.mkdir(parents=True, exist_ok=True)
        self.stream = path.open("w+b")
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        lock = kernel32.LockFileEx
        lock.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.DWORD,
                         wintypes.DWORD, wintypes.DWORD, ctypes.POINTER(Overlapped)]
        lock.restype = wintypes.BOOL
        self.overlapped = Overlapped()
        handle = msvcrt.get_osfhandle(self.stream.fileno())
        if not lock(handle, 3, 0, 0xffffffff, 0xffffffff, ctypes.byref(self.overlapped)):
            error = ctypes.get_last_error()
            self.stream.close()
            raise ctypes.WinError(error)

    def __enter__(self) -> "ExclusiveWriterLock":
        return self

    def __exit__(self, *_: Any) -> None:
        self.stream.close()


def archive_case(
    proof: dict[str, Any], executable: Path, home: Path, name: str,
    thread_ids: list[str], paths: list[Path], expect_archived: bool,
) -> None:
    before = {path.name: sha256(path) for path in paths}
    result = {
        "name": name, "verified": False, "thread_ids": thread_ids,
        "synthetic_home": str(home), "before_sha256": before,
    }
    proof["tests"].append(result)
    result.update(invoke(executable, home, ["--no-daemon", "archive", thread_ids[0]]))
    flags = read_archived(home)
    result["archived_flags"] = flags
    require(flags == {thread_id: int(expect_archived) for thread_id in thread_ids},
            f"{name}: unexpected archive metadata")
    if expect_archived:
        require(result["returncode"] == 0, f"{name}: native archive failed")
        require(all(not path.exists() for path in paths), f"{name}: source rollouts remain")
        destinations = [home / "archived_sessions" / path.name for path in paths]
        after = {path.name: sha256(path) for path in destinations}
    else:
        require(result["returncode"] != 0, f"{name}: archive accepted an active writer")
        require(all(path.is_file() for path in paths), f"{name}: source rollouts moved")
        require(not any((home / "archived_sessions" / path.name).exists() for path in paths),
                f"{name}: rejected archive left archived rollouts")
        after = {path.name: sha256(path) for path in paths}
    result["after_sha256"] = after
    require(before == after, f"{name}: rollout bytes changed")
    result["verified"] = True


def run_probe(executable: Path, root: Path, proof: dict[str, Any]) -> None:
    initial_hash = sha256(executable)
    proof["codex_sha256"] = initial_hash
    idle_home = fixture(root, "idle-legacy")
    version = invoke(executable, idle_home, ["--version"])
    require(version["returncode"] == 0 and version["stdout"].strip(), "Native version lookup failed")
    proof["version"] = version["stdout"].strip()
    thread_id = str(uuid.uuid4())
    paths = [rollout(idle_home, thread_id)]
    archive_case(proof, executable, idle_home, "idle-legacy-archives", [thread_id], paths, True)

    locked_home = fixture(root, "locked-legacy")
    thread_id = str(uuid.uuid4())
    paths = [rollout(locked_home, thread_id)]
    with ExclusiveWriterLock(locked_home, thread_id):
        archive_case(proof, executable, locked_home, "root-writer-refuses", [thread_id], paths, False)
    archive_case(proof, executable, locked_home, "released-root-archives", [thread_id], paths, True)

    for paginated in (False, True):
        mode = "paginated" if paginated else "legacy"
        home, thread_ids, paths = tree_fixture(root, idle_home, f"transitive-{mode}", paginated)
        with ExclusiveWriterLock(home, thread_ids[2]):
            archive_case(proof, executable, home, f"grandchild-writer-refuses-{mode}", thread_ids, paths, False)
        archive_case(proof, executable, home, f"released-tree-archives-{mode}", thread_ids, paths, True)
    require(len(proof["tests"]) == 7 and all(test["verified"] for test in proof["tests"]),
            "Missing required native archive cases")
    require(sha256(executable) == initial_hash, "Executable changed during probe")
    proof["verified"] = True


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--codex-exe", type=Path, required=True, help="Absolute installed codex.exe path")
    parser.add_argument("--output-root", type=Path, required=True, help="Absolute local parent for a new fixture directory")
    arguments = parser.parse_args()
    if sys.platform != "win32":
        parser.error("This native writer-lock probe requires Windows")
    if not arguments.codex_exe.is_absolute() or not arguments.output_root.is_absolute():
        parser.error("Both paths must be absolute")
    executable = arguments.codex_exe.resolve(strict=True)
    if not executable.is_file():
        parser.error("--codex-exe must identify a file")
    root = arguments.output_root.resolve() / f"archive-probe-{uuid.uuid4().hex}"
    root.mkdir(parents=True, exist_ok=False)
    proof: dict[str, Any] = {
        "schema_version": 1, "probe": PROBE_NAME, "verified": False,
        "started_at_utc": utc_now(), "platform": sys.platform,
        "codex_exe": str(executable), "fixture_root": str(root), "tests": [],
        "limitations": [
            "Synthetic offline fixtures only; no production chats or credentials used.",
            "Paginated case exercises archive metadata, not model conversation generation.",
            "Applies only to the recorded binary hash and --no-daemon archive UUID route.",
            "Does not establish cleanup eligibility or authorize scheduler installation.",
        ],
    }
    try:
        run_probe(executable, root, proof)
    except Exception as error:
        proof["verified"] = False
        proof["failure"] = f"{type(error).__name__}: {error}"
    proof["finished_at_utc"] = utc_now()
    proof_path = root / "proof.json"
    proof_path.write_text(json.dumps(proof, indent=2) + "\n", encoding="utf-8")
    if proof["verified"]:
        print(f"PASS: 7 native archive guard cases; proof: {proof_path}")
        return 0
    print(f"FAIL: archive guard not verified; proof: {proof_path}", file=sys.stderr)
    print(proof.get("failure", "Unknown probe outcome"), file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
