"""Offline checks for the portable patch and real, mocked registration helper."""
import importlib.util
from pathlib import Path
import tempfile


ROOT = Path(__file__).resolve().parents[1]


def load(name):
    spec = importlib.util.spec_from_file_location(name, ROOT / "tests" / (name + ".py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def main():
    package = load("test_gameplan_stats_1730_patch")
    package.main()
    registration = load("test_register_nightly_watchdog")
    scratch = ROOT / "scratch/stats-1730-publication-check"
    scratch.mkdir(parents=True, exist_ok=True)
    for enabled in (True, False):
        for installed in (True, False):
            fixture = Path(tempfile.mkdtemp(prefix="registration-", dir=scratch)).resolve()
            if not fixture.is_relative_to(scratch.resolve()):
                raise ValueError("Registration fixture escaped the task-owned scratch directory")
            registration.test_existing_registration_preserves_everything_except_stats_trigger(
                fixture, enabled, installed)
    print("Four mocked Windows registration and repeat-install checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
