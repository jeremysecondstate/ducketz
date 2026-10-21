"""Run the Gameplan worker regressions without a different Ducketz editable checkout."""
from __future__ import annotations

import argparse
import hashlib
import json
import importlib
from pathlib import Path
import sys


def _isolate_ducketz_imports(candidate: Path) -> set[Path]:
    original_roots = set()
    retained_finders = []
    for finder in sys.meta_path:
        name = getattr(finder, "__module__", "")
        if name.startswith("__editable___ducketz_") and name.endswith("_finder"):
            module = sys.modules.get(name)
            mapping = getattr(module, "MAPPING", {})
            for package in ("app", "datafetching", "ml"):
                if package in mapping:
                    original_roots.add(Path(mapping[package]).resolve().parent)
        else:
            retained_finders.append(finder)
    sys.meta_path[:] = retained_finders
    original_roots.discard(candidate)
    retained_paths = []
    for entry in sys.path:
        path = Path(entry or ".").resolve()
        # The pinned verifier's script directory has its own qualified
        # requests.py helper. It must not shadow the third-party package
        # before pytest has loaded the identical conftest.py safeguard.
        collision = (path.name == "cross_pc" and path.parent.name == "tools"
                     and (path / "test_runner.py").is_file() and (path / "requests.py").is_file())
        if path not in original_roots | {candidate} and not collision:
            retained_paths.append(entry)
    sys.path[:] = [str(candidate), *retained_paths]
    return original_roots


def _assert_candidate_imports(candidate: Path, original_roots: set[Path]) -> None:
    worker = importlib.import_module("ml.stock_trader.independent_session")
    if not Path(worker.__file__).resolve().is_relative_to(candidate):
        raise RuntimeError("Gameplan worker imported from a different checkout")
    for module in list(sys.modules.values()):
        filename = getattr(module, "__file__", None)
        if filename and any(Path(filename).resolve().is_relative_to(root)
                            for root in original_roots):
            # Dependencies in the original checkout's virtual environment are
            # still the configured third-party runtime, not application source.
            if ".venv" not in Path(filename).resolve().parts:
                raise RuntimeError("Ducketz application import escaped the candidate")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--basetemp", type=Path)
    args = parser.parse_args()
    candidate = Path(__file__).resolve().parents[1]
    original_roots = _isolate_ducketz_imports(candidate)
    _assert_candidate_imports(candidate, original_roots)
    import pytest

    arguments = ["-q", "-p", "no:cacheprovider",
                 str(candidate / "tests/test_independent_session_error_diagnostics.py"),
                 str(candidate / "tests/test_independent_stock_session.py")]
    if args.basetemp is not None:
        arguments += ["--basetemp", str(args.basetemp)]
    result = pytest.main(arguments)
    _assert_candidate_imports(candidate, original_roots)
    closure = {}
    for module in list(sys.modules.values()):
        filename = getattr(module, "__file__", None)
        if filename:
            source = Path(filename).resolve()
            if source.is_relative_to(candidate) and source.suffix == ".py" and ".venv" not in source.parts:
                closure[source.relative_to(candidate).as_posix()] = hashlib.sha256(source.read_bytes()).hexdigest()
    for name in ("pyproject.toml", "tests/conftest.py", "tests/check_isolated_gameplan_session.py"):
        source = candidate / name
        closure[name] = hashlib.sha256(source.read_bytes()).hexdigest()
    print("GAMEPLAN_CANDIDATE_IMPORT_CLOSURE=" + json.dumps(closure, sort_keys=True))
    return result


if __name__ == "__main__":
    raise SystemExit(main())
