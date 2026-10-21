"""Run snapshot provenance regressions without network or editable-checkout imports."""
from __future__ import annotations

import importlib
from pathlib import Path
import sys
import socket

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
    for name in ("ml.intraday.account_execution", "ml.intraday.local_execution", "ml.account_gameplan.authority", "ml.stock_trader.horizon_broker"):
        worker = importlib.import_module(name)
        if not Path(worker.__file__).resolve().is_relative_to(candidate):
            raise RuntimeError("Intraday execution imported from a different checkout")
    for name, module in list(sys.modules.items()):
        filename = getattr(module, "__file__", None)
        if (filename and name.split(".")[0] in {"ml", "app", "datafetching", "tests"}
                and not Path(filename).resolve().is_relative_to(candidate)):
            raise RuntimeError("Ducketz application/fixture import escaped the candidate")
        if (filename and not Path(filename).resolve().is_relative_to(candidate)
                and any(Path(filename).resolve().is_relative_to(root) for root in original_roots)):
            # Dependencies in the original checkout's virtual environment are
            # still the configured third-party runtime, not application source.
            if ".venv" not in Path(filename).resolve().parts:
                raise RuntimeError("Ducketz application import escaped the candidate")


def main():
    candidate = Path(__file__).resolve().parents[1]
    original_roots = _isolate_ducketz_imports(candidate)
    def deny_network(*args, **kwargs):
        raise RuntimeError("Snapshot provenance verification forbids network calls")
    socket.create_connection = deny_network
    socket.socket.connect = deny_network
    socket.socket.connect_ex = deny_network
    runtime = importlib.import_module("ml.intraday.runtime")
    if not Path(runtime.__file__).resolve().is_relative_to(candidate):
        raise RuntimeError("Intraday runtime imported outside the candidate")
    import pytest
    result = pytest.main(["-q", "-p", "no:cacheprovider", *[
        str(candidate / name) for name in (
            "tests/test_intraday_snapshot_provenance_performance.py",
            "tests/test_intraday_dataset_provenance_performance.py",
            "tests/test_intraday_preparation_latency.py",
            "tests/test_intraday_runtime.py",
            "tests/test_intraday_capture_freshness.py")]])
    _assert_candidate_imports(candidate, original_roots)
    return result


if __name__ == "__main__":
    raise SystemExit(main())
