"""Verify the isolated candidate without importing production state or brokers."""
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
import subprocess
import sys

WORKTREE = Path(r"C:\Users\7980X\.codex\worktrees\cross-horizon-bearish-fallback\ducketz")
OUT = Path(__file__).resolve().parent
PYTHON = Path(r"C:\dev\ducketz\.venv\Scripts\python.exe")
TESTS = [
    "tests/test_cross_horizon_fallback_policy.py",
    "tests/test_cross_horizon_fallback_ledger.py",
    "tests/test_cross_horizon_fallback_runtime.py",
    "tests/test_cross_horizon_fallback_review.py",
    "tests/test_gameplan_fallback_planning.py",
    "tests/test_stock_horizon_ledger.py",
    "tests/test_stock_horizon_broker.py",
    "tests/test_stock_horizon_schwab_context.py",
    "tests/test_independent_stock_runtime.py",
    "tests/test_independent_stock_session.py",
    "tests/test_gameplan_direction_runtime.py",
    "tests/test_gameplan_direction_engine.py",
    "tests/test_gameplan_direction_snapshot.py",
    "tests/test_gameplan_quote_recovery.py",
    "tests/test_gameplan_quote_clock_recovery.py",
    "tests/test_gameplan_cash_ledger.py",
    "tests/test_gameplan_trade_planning.py",
    "tests/test_gameplan_trade_snapshot.py",
    "tests/test_gameplan_trade_review.py",
    "tests/test_gameplan_data.py",
    "tests/test_gameplan_ui.py",
    "tests/test_gameplan_deployment_execution.py",
    "tests/test_gameplan_deployment_handoff.py",
    "tests/test_gameplan_stock_trader.py",
    "tests/test_nightly_gameplan.py",
    "tests/test_gameplan_probability_target.py",
    "tests/test_stock_only_gameplan.py",
]


def now():
    return datetime.now(timezone.utc).isoformat()


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def files():
    tracked = subprocess.check_output(["git", "ls-files", "-z"], cwd=WORKTREE).decode().split("\0")
    extra = subprocess.check_output(["git", "ls-files", "--others", "--exclude-standard", "-z"], cwd=WORKTREE).decode().split("\0")
    return sorted({p for p in tracked + extra if p and Path(p).suffix in {".py", ".json", ".toml", ".txt", ".md"}
                   and not p.startswith(("artifacts/", "scratch/"))})


def main():
    missing = [p for p in TESTS if not (WORKTREE / p).is_file()]
    if missing:
        raise RuntimeError(f"Candidate tests are incomplete: {missing}")
    paths = files()
    before = {p: sha(WORKTREE / p) for p in paths}
    command = [str(PYTHON), "-m", "pytest", *TESTS, "-q"]
    started = now()
    with (OUT / "combined-tests.log").open("w", encoding="utf-8") as log:
        result = subprocess.run(command, cwd=WORKTREE, stdout=log, stderr=subprocess.STDOUT)
    after = {p: sha(WORKTREE / p) for p in paths}
    drift = [p for p in paths if before[p] != after[p]]
    check = subprocess.run(["git", "diff", "HEAD", "--check"], cwd=WORKTREE, capture_output=True, text=True)
    report = {"status": "PASS" if result.returncode == 0 and not drift and check.returncode == 0 else "FAIL",
        "started_at_utc": started, "completed_at_utc": now(), "command": command,
        "cwd": str(WORKTREE), "exit_code": result.returncode, "source_drift": drift,
        "diff_check_exit_code": check.returncode, "diff_check_output": check.stdout + check.stderr,
        "source_fingerprints": after, "log_path": str(OUT / "combined-tests.log"),
        "log_sha256": sha(OUT / "combined-tests.log"), "production_actions": False,
        "limitations": ["Mocked broker tests only; no order or broker request was made.",
                        "Verified candidate is isolated; deployment and next-session publication are separate."]}
    (OUT / "combined-verification.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({k: v for k, v in report.items() if k != "source_fingerprints"}, indent=2))
    print((OUT / "combined-tests.log").read_text(encoding="utf-8")[-14000:])
    return 0 if report["status"] == "PASS" else 1


if __name__ == "__main__":
    sys.exit(main())
