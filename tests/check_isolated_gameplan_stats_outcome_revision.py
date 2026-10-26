"""Run exact-candidate original-outcome revision and strict Stats checks offline."""
from __future__ import annotations

import argparse
import hashlib
import importlib
import json
import os
from pathlib import Path
import socket
import sys
import uuid


APPLICATION_PACKAGES = {"app", "datafetching", "ml", "tools", "tests", "fundamentals",
                        "options", "signals", "technicals"}
SUITES = (
    "tests/test_gameplan_stats_outcome_revision.py",
    "tests/test_gameplan_stats_handoff.py",
)


def _deny_network(*args, **kwargs):
    raise RuntimeError("Network connections are disabled in Stats outcome revision checks")


def _isolate(candidate: Path) -> set[Path]:
    original_roots, retained = set(), []
    for finder in sys.meta_path:
        name = getattr(finder, "__module__", "")
        if name.startswith("__editable___ducketz_") and name.endswith("_finder"):
            mapping = getattr(sys.modules.get(name), "MAPPING", {})
            original_roots.update(Path(value).resolve().parent for key, value in mapping.items()
                                  if key in APPLICATION_PACKAGES)
        else:
            retained.append(finder)
    sys.meta_path[:] = retained
    for entry in sys.path:
        path = Path(entry or ".").resolve()
        if (path / "pyproject.toml").is_file() and (path / "ml").is_dir():
            original_roots.add(path)
    original_roots.discard(candidate)
    retained_paths = []
    for entry in sys.path:
        path = Path(entry or ".").resolve()
        collision = (path.name == "cross_pc" and path.parent.name == "tools"
                     and (path / "test_runner.py").is_file() and (path / "requests.py").is_file())
        if path not in original_roots | {candidate} and not collision:
            retained_paths.append(entry)
    sys.path[:] = [str(candidate), *retained_paths]
    return original_roots


def _assert_imports(candidate: Path, originals: set[Path]) -> None:
    for name in ("ml.gameplan_stats_outcome_revision", "ml.gameplan_stats_handoff",
                 "app.ui.gameplan_stats_data"):
        module = importlib.import_module(name)
        if not Path(module.__file__).resolve().is_relative_to(candidate):
            raise RuntimeError("Stats module imported outside this candidate: " + name)
    for name, module in list(sys.modules.items()):
        filename = getattr(module, "__file__", None)
        if not filename:
            continue
        source = Path(filename).resolve()
        application = (name.split(".")[0] in APPLICATION_PACKAGES
                       or name.startswith("test_") or name == "conftest")
        if application and not source.is_relative_to(candidate):
            raise RuntimeError("Application or fixture import escaped this candidate: " + str(source))
        if (".venv" not in source.parts and not source.is_relative_to(candidate)
                and any(source.is_relative_to(root) for root in originals)):
            raise RuntimeError("Application source escaped this candidate: " + str(source))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--basetemp", type=Path)
    args = parser.parse_args()
    candidate = Path(__file__).resolve().parents[1]
    originals = _isolate(candidate)
    os.environ["PYTEST_DISABLE_PLUGIN_AUTOLOAD"] = "1"
    socket.socket.connect = socket.socket.connect_ex = socket.create_connection = _deny_network
    socket.socket.sendto = _deny_network

    def offline_boundary(event, arguments):
        if event in {"socket.connect", "socket.sendto", "socket.getaddrinfo", "socket.gethostbyname",
                     "socket.gethostbyaddr", "socket.bind", "subprocess.Popen", "os.system",
                     "os.spawn", "os.posix_spawn", "os.startfile", "os.startfile/2"}:
            raise RuntimeError("Stats outcome revision checks prohibit network/subprocess actions")

    sys.addaudithook(offline_boundary)
    _assert_imports(candidate, originals)
    scratch = (candidate / "scratch").resolve()
    basetemp = (args.basetemp or scratch / "gameplan-stats-outcome-revision-checks" / uuid.uuid4().hex).resolve()
    if basetemp == scratch or not basetemp.is_relative_to(scratch) or basetemp.exists():
        raise RuntimeError("Stats fixtures require a new candidate-owned scratch directory")
    basetemp.parent.mkdir(parents=True, exist_ok=True)
    import pytest

    result = pytest.main(["-q", "-p", "no:cacheprovider", *[str(candidate / name) for name in SUITES],
                          "--basetemp", str(basetemp)])
    _assert_imports(candidate, originals)
    closure = {}
    for module in list(sys.modules.values()):
        filename = getattr(module, "__file__", None)
        if filename:
            source = Path(filename).resolve()
            if source.is_relative_to(candidate) and source.suffix == ".py" and ".venv" not in source.parts:
                closure[source.relative_to(candidate).as_posix()] = hashlib.sha256(source.read_bytes()).hexdigest()
    for name in ("pyproject.toml", "tests/conftest.py",
                 "tests/check_isolated_gameplan_stats_outcome_revision.py", *SUITES):
        closure[name] = hashlib.sha256((candidate / name).read_bytes()).hexdigest()
    closure_path = basetemp.with_name(basetemp.name + "-import-closure.json")
    with closure_path.open("x", encoding="utf-8") as stream:
        json.dump(closure, stream, indent=2, sort_keys=True)
        stream.write("\n")
    print("GAMEPLAN_STATS_OUTCOME_REVISION_IMPORT_CLOSURE=" + json.dumps(closure, sort_keys=True))
    print("GAMEPLAN_STATS_OUTCOME_REVISION_CLOSURE_PATH=" + str(closure_path))
    return int(result)


if __name__ == "__main__":
    raise SystemExit(main())
