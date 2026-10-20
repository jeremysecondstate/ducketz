"""Run the actuals regressions without a different Ducketz editable checkout."""
from __future__ import annotations

import argparse
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
    sys.path[:] = [str(candidate), *(entry for entry in sys.path
        if Path(entry or ".").resolve() not in original_roots | {candidate})]
    return original_roots


def _assert_candidate_imports(candidate: Path, original_roots: set[Path]) -> None:
    actuals = importlib.import_module("ml.gameplan_actuals_review")
    if not Path(actuals.__file__).resolve().is_relative_to(candidate):
        raise RuntimeError("Gameplan actuals imported from a different checkout")
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
                 str(candidate / "tests/test_gameplan_actuals_review.py")]
    if args.basetemp is not None:
        arguments += ["--basetemp", str(args.basetemp)]
    result = pytest.main(arguments)
    _assert_candidate_imports(candidate, original_roots)
    return result


if __name__ == "__main__":
    raise SystemExit(main())
