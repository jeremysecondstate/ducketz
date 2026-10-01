"""Read-only byte-level shared-source inventory with explicit local overlays."""
from __future__ import annotations

from copy import deepcopy
from pathlib import Path
import re

from . import VERSION
from .core import digest, encoded, git, git_text, parse, relative, safe_path, validate_profile


_SOURCE_ROOTS = {
    "app", "ml", "tests", "configs", "config", "tools", "coordination", "signals",
    "technicals", "fundamentals", "options", "datafetching", "docs", "scripts",
    "benchmarks", ".github",
}
_PRIVATE_PARTS = {
    ".git", ".codex", ".venv", "scratch", "artifacts", "tmp", "temp", "data",
    "datastore", "state", "runtime-state", "logs", "receipts", "ledgers", "journals",
    "raw", "cache", "__pycache__", "credentials", "secrets", "_operations", "_paper", "_powder",
}
_PRIVATE_SUFFIXES = {
    ".db", ".sqlite", ".sqlite3", ".parquet", ".pkl", ".pickle", ".joblib", ".pem",
    ".key", ".pyc", ".pyo", ".feather", ".arrow", ".h5", ".hdf5", ".onnx", ".pt", ".pth",
}


def shared_source_path(name):
    """Known shared roots/manifests, with private runtime/data paths excluded."""
    relative(name)
    parts = name.lower().split("/")
    filename = parts[-1]
    if set(parts) & _PRIVATE_PARTS or any(
        part == "runs" or part.endswith(("-runs", "-latest")) for part in parts[:-1]
    ):
        return False
    if Path(filename).suffix in _PRIVATE_SUFFIXES or filename == ".env" or filename.startswith(".env.") and filename != ".env.example":
        return False
    if filename.endswith(".lock") and not (len(parts) == 1 and (
        filename in {"uv.lock", "poetry.lock", "pdm.lock", "yarn.lock", "bun.lock", "pipfile.lock", "cargo.lock"}
        or filename.startswith("requirements")
    )):
        return False
    if Path(filename).suffix in {".json", ".jsonl", ".yaml", ".yml", ".toml"} and re.match(
        r"(?:credentials?|secrets?|wallet|account[-_]state|balances?|holdings|ledger|receipts?|auth)(?:[._-]|$)", filename
    ):
        return False
    if len(parts) > 1:
        return parts[0] in _SOURCE_ROOTS
    return (Path(filename).suffix in {".md", ".rst", ".py", ".ps1", ".cmd", ".bat"}
            or filename in {".gitignore", ".gitattributes", ".env.example", "pyproject.toml",
                            "setup.cfg", "tox.ini", "pytest.ini", "package.json", "package-lock.json",
                            "pnpm-lock.yaml", "uv.lock", "poetry.lock", "pdm.lock", "yarn.lock", "bun.lock",
                            "environment.yml", "environment.yaml", "pipfile", "pipfile.lock", "dockerfile",
                            "makefile", "cargo.toml", "cargo.lock"}
            or filename.startswith("requirements") and filename.endswith((".txt", ".in", ".lock")))


def _git_names(root, *args):
    return {name for name in git(root, *args).stdout.decode("utf-8").split("\0") if name}


def project_config(value, local_pointers):
    """Remove only documented local JSON pointers, never a whole mixed file."""
    result = deepcopy(value)
    for pointer in local_pointers:
        if not pointer.startswith("/") or pointer == "/":
            raise ValueError("local exception must name a field")
        parts = [x.replace("~1", "/").replace("~0", "~") for x in pointer[1:].split("/")]
        parent = result
        for key in parts[:-1]:
            if not isinstance(parent, dict) or key not in parent:
                parent = None
                break
            parent = parent[key]
        if isinstance(parent, dict):
            parent.pop(parts[-1], None)
    return result


def inspect(profile, reference):
    validate_profile(profile)
    root = Path(profile["checkout"])
    reference = git_text(root, "rev-parse", "--verify", reference + "^{commit}")
    reference_paths = _git_names(root, "ls-tree", "-r", "-z", "--name-only", reference)
    tracked_paths = _git_names(root, "ls-files", "-z")
    local_head_paths = _git_names(root, "ls-tree", "-r", "-z", "--name-only", "HEAD")
    untracked_paths = _git_names(root, "ls-files", "-z", "--others", "--exclude-standard")
    paths = sorted(p for p in reference_paths | tracked_paths | local_head_paths | untracked_paths
                   if shared_source_path(p))
    actual, expected, deviations = {}, {}, []
    exceptions = profile.get("local_config_fields", {})
    for name in paths:
        path = safe_path(root, name)
        raw = path.read_bytes() if path.is_file() else None
        base = git(root, "show", reference + ":" + name).stdout if name in reference_paths else None
        actual[name] = digest(raw) if raw is not None else None
        expected[name] = digest(base) if base is not None else None
        if actual[name] != expected[name]:
            entry = {"path": name, "actual_sha256": actual[name], "reference_sha256": expected[name]}
            if base is None:
                entry["classification"] = "untracked_shared_source" if name in untracked_paths else "local_shared_addition"
            elif raw is None:
                entry["classification"] = "missing_shared_source"
            elif raw.replace(b"\r\n", b"\n") == base.replace(b"\r\n", b"\n"):
                entry["classification"] = "line_endings_only_actual_bytes_differ"
            elif raw is not None and name in exceptions:
                entry["shared_fields_match"] = project_config(parse(raw), exceptions[name]) == project_config(parse(base), exceptions[name])
                entry["classification"] = "mixed_configuration"
            else:
                entry["classification"] = "shared_source_deviation"
            deviations.append(entry)
    return {"contract_version": VERSION, "machine": profile["machine"], "reference_commit": reference,
            "checkout_head": git_text(root, "rev-parse", "HEAD"), "actual_shared_bytes": actual,
            "reference_shared_bytes": expected, "actual_digest": digest(encoded(actual)), "deviations": deviations,
            "symbol_profile_digest": digest(encoded(profile["symbols"])), "runtime_version": "unverified",
            "peer_source_version": "unverified", "local_state": "not_read"}


def compare(left, right, *, symbol_profile_evidence=None):
    names = set(left["actual_shared_bytes"]) | set(right["actual_shared_bytes"])
    different = sorted(p for p in names if left["actual_shared_bytes"].get(p) != right["actual_shared_bytes"].get(p))
    preservation = {}
    for inventory in (left, right):
        machine = inventory["machine"]
        evidence = (symbol_profile_evidence or {}).get(machine)
        preservation[machine] = "unverified"
        if evidence is not None:
            before, after = evidence.get("before_sha256"), evidence.get("after_sha256")
            if not all(isinstance(sha, str) and re.fullmatch(r"[0-9a-f]{64}", sha) for sha in (before, after)):
                raise ValueError("symbol profile preservation requires explicit pre/post SHA-256 evidence")
            preservation[machine] = before == after == inventory.get("symbol_profile_digest")
    preserved = False if False in preservation.values() else (
        True if all(value is True for value in preservation.values()) else "unverified")
    return {"same_reference": left["reference_commit"] == right["reference_commit"],
            "same_actual_bytes": not different, "different_paths": different,
            "runtime_parity": "unverified", "symbol_profiles_preserved": preserved,
            "symbol_profile_preservation_by_machine": preservation}
