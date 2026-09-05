from __future__ import annotations

import argparse
import json
import os
import re
import signal
import subprocess
import sys
import time
import uuid
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Callable, Mapping, Sequence
from zoneinfo import ZoneInfo

import exchange_calendars as xcals
import pandas as pd

from datafetching.parquet_store import DATASTORE_TARGETS, resolve_datastore_dir
from datafetching.runtime_lock import exclusive_runtime_lock
from ml.artifacts import create_timestamp_directory, file_checksum, utc_timestamp


OVERNIGHT_RUNTIME_VERSION = "supervised-overnight-gameplan-runtime-v2"
OVERNIGHT_NOOP_VERSION = "single-owner-overnight-gameplan-noop-v1"
SCHEDULE_TIMEZONE = ZoneInfo("America/Los_Angeles")
ACTION_DAY_CLOSE_HOUR = 17
STAGE_ORDER = (
    "loop_a_close_fetch",
    "loop_b_directional_generation",
    "gameplan_evaluation",
    "strategy_profit_training",
    "strategy_generation",
    "gameplan_publication",
)


@dataclass(frozen=True)
class StageResult:
    stage: str
    command: tuple[str, ...]
    started_at: str
    finished_at: str
    elapsed_seconds: float
    exit_code: int | None
    status: str
    log_path: str
    error: str | None = None


def scheduled_session_eligibility(
    observed_at: object | None = None,
) -> dict[str, object]:
    """Return whether a scheduled wake follows an eligible XNYS action day.

    The recurrence is weekday-based, so this calendar check is what prevents a
    US market holiday from retraining on unchanged data or replacing the next
    session's existing frozen plan.  The system's action day ends at 17:00 PT,
    later than the regular XNYS close used by the exchange calendar.
    """

    observed = utc_timestamp(observed_at)
    local = observed.tz_convert(SCHEDULE_TIMEZONE)
    label = pd.Timestamp(local.date())
    calendar = xcals.get_calendar(
        "XNYS",
        start=label - pd.Timedelta(days=10),
        end=label + pd.Timedelta(days=14),
    )
    is_session = bool(calendar.is_session(label))
    next_session = calendar.date_to_session(label, direction="next")
    base = {
        "observed_at": observed.isoformat(),
        "observed_at_local": local.isoformat(),
        "local_date": local.date().isoformat(),
        "calendar": "XNYS",
        "is_exchange_session": is_session,
        "next_exchange_session": pd.Timestamp(next_session).date().isoformat(),
    }
    if not is_session:
        return {
            **base,
            "eligible": False,
            "status": "NOOP_NON_SESSION_DATE",
            "reason": "The scheduled local date is not an XNYS session.",
        }
    if local.hour < ACTION_DAY_CLOSE_HOUR:
        return {
            **base,
            "eligible": False,
            "status": "FAIL_ACTION_DAY_NOT_CLOSED",
            "reason": "The 04:00-17:00 PT action day has not finished.",
        }
    return {
        **base,
        "eligible": True,
        "status": "ELIGIBLE_COMPLETED_ACTION_SESSION",
        "reason": "The scheduled wake follows a completed XNYS action date.",
    }


def record_scheduled_noop(
    datastore_root: Path,
    *,
    eligibility: Mapping[str, object],
) -> Path:
    """Publish a receipt for an expected non-session scheduled wake."""

    if eligibility.get("status") != "NOOP_NON_SESSION_DATE":
        raise ValueError("Only a non-session date may be recorded as a no-op")
    root = Path(datastore_root).resolve()
    observed = utc_timestamp(eligibility.get("observed_at"))
    run = create_timestamp_directory(
        root / "ml" / "overnight-runs",
        timestamp=observed,
    )
    report_path = run / "stage-report.json"
    report = {
        "schema_version": OVERNIGHT_NOOP_VERSION,
        "run_timestamp": observed.isoformat(),
        "datastore_root": str(root),
        "status": "NOOP_NON_SESSION_DATE",
        "eligibility": dict(eligibility),
        "stages": [],
        "prior_gameplan_pointer_preserved": True,
        "broker_orders_enabled": False,
        "orders_placed": 0,
    }
    _write_json_atomic(report_path, report)
    _write_json_atomic(
        run / "receipt.json",
        {
            "schema_version": OVERNIGHT_NOOP_VERSION,
            "run_path": run.relative_to(root).as_posix(),
            "status": "NOOP_NON_SESSION_DATE",
            "completed_at": utc_timestamp().isoformat(),
            "stage_report_size": report_path.stat().st_size,
            "stage_report_checksum_sha256": file_checksum(report_path),
            "prior_gameplan_pointer_preserved": True,
            "broker_orders_enabled": False,
            "orders_placed": 0,
        },
    )
    return run


def run_overnight_pipeline(
    datastore_root: Path,
    *,
    datastore_argument: tuple[str, str],
    repository_root: Path,
    start_at: str = STAGE_ORDER[0],
    stop_after: str = STAGE_ORDER[-1],
    reporter=print,
    resume_run: Path | None = None,
    deadline: object | None = None,
    poll_seconds: float = 30.0,
) -> Path:
    """Run the one-owner post-close chain and fail before downstream stages."""

    if start_at not in STAGE_ORDER or stop_after not in STAGE_ORDER:
        raise ValueError("Unknown overnight stage boundary")
    start_index = STAGE_ORDER.index(start_at)
    stop_index = STAGE_ORDER.index(stop_after)
    if start_index > stop_index:
        raise ValueError("start_at must not come after stop_after")
    root = Path(datastore_root).resolve()
    repository = Path(repository_root).resolve()
    created = utc_timestamp()
    resume = _resume_configuration(root, resume_run) if resume_run else None
    if resume:
        start_at, stop_after = resume["failed_stage"], resume["stage_order"][-1]
        start_index, stop_index = STAGE_ORDER.index(start_at), STAGE_ORDER.index(stop_after)
    deadline_at = utc_timestamp(resume["deadline_at"] if resume else deadline) if (resume or deadline is not None) else next_action_deadline(created)
    run = create_timestamp_directory(
        root / "ml" / "overnight-runs",
        timestamp=created,
    )
    python = str(Path(sys.executable).resolve())
    commands: dict[str, tuple[str, ...]] = {
        "loop_a_close_fetch": (
            python,
            "-u",
            "-m",
            "datafetching.orchestrate",
            *datastore_argument,
            "--watchlist",
            str(repository / "datafetching" / "watchlist.txt"),
            "--providers",
            "databento",
            "fmp",
            "fred",
            "schwab",
            "sec",
            "--cme-mode",
            "inline",
            "--options-mode",
            "inline",
            "--opra-history-mode",
            "daily",
            "--opra-history-utc-hour",
            "0",
            "--opra-history-max-estimated-download-bytes",
            "20000000000",
            "--opra-history-max-estimated-cost-usd",
            "1",
            "--opra-history-max-catchup-days",
            "30",
            "--bar-readiness-recovery-timeout-seconds",
            "420",
            "--bar-readiness-recovery-poll-seconds",
            "10",
            "--once",
        ),
        "loop_b_directional_generation": (
            python,
            "-u",
            "-m",
            "ml.prediction_runtime",
            *datastore_argument,
            "--watchlist",
            str(repository / "datafetching" / "watchlist.txt"),
            "--provider",
            "databento",
            "--horizons",
            "1h",
            "4h",
            "1d",
            "1w",
            "--feature-profile",
            "loop-a-all-bsgp-active-v3",
            "--model-family",
            "logistic",
            "--calibration",
            "platt",
            "--round-trip-cost",
            "0.001",
            "--require-all-routes",
            "--once",
        ),
        "strategy_profit_training": (
            python,
            "-u",
            "-m",
            "ml.strategy_profit_training_runtime",
            *datastore_argument,
            "--once",
        ),
        "gameplan_evaluation": (
            python, "-u", "-m", "ml.gameplan_evaluation", *datastore_argument,
        ),
        "strategy_generation": (
            python,
            "-u",
            "-m",
            "ml.strategy_runtime",
            *datastore_argument,
            "--pricing-mode",
            "active",
            "--once",
        ),
        "gameplan_publication": (
            python,
            "-u",
            "-m",
            "ml.nightly_gameplan",
            *datastore_argument,
            "--once",
        ),
    }
    selected = STAGE_ORDER[start_index : stop_index + 1]
    report: dict[str, object] = {
        "schema_version": OVERNIGHT_RUNTIME_VERSION,
        "run_timestamp": created.isoformat(), "owner_pid": os.getpid(),
        "owner_created_at": _process_created_at(os.getpid()),
        "repository_root": str(repository), "datastore_root": str(root),
        "deadline_at": deadline_at.isoformat(), "stage_order": list(selected),
        "resumed_from": str(Path(resume_run).resolve()) if resume_run else None,
        "completed_stages_from_previous_attempt": resume["completed_stages"] if resume else [],
        "broker_orders_enabled": False, "orders_placed": 0,
        "status": "RUNNING", "stages": [], "current_stage": None,
    }
    report_path = run / "stage-report.json"
    _write_json_atomic(report_path, report)
    _write_json_atomic(root / "ml/overnight-latest/run.json", {"run_path": run.relative_to(root).as_posix()})
    if reporter is not None:
        reporter(f"OVERNIGHT RUN {run}; deadline={deadline_at.isoformat()}")
    results: list[StageResult] = []
    stage = selected[0]
    error: BaseException | None = None
    try:
        for stage in selected:
            command = commands[stage]
            started = utc_timestamp()
            monotonic_started = time.monotonic()
            log_path = run / f"{stage}.log"
            report["current_stage"] = stage
            report["current_log_path"] = str(log_path)
            report["heartbeat_at"] = started.isoformat()
            report["stage_started_at"] = started.isoformat()
            report["stage_health"] = "STARTING"
            report["child_pid"] = None
            _write_json_atomic(report_path, report)
            if reporter is not None:
                reporter(f"OVERNIGHT START {stage} at {started.isoformat()}; log={log_path}")
            exit_code: int | None = None
            stage_error: BaseException | None = None
            status = "COMPLETE"

            def progress(payload: Mapping[str, object]) -> None:
                report.update(payload)
                with (run / "health.jsonl").open("a", encoding="utf-8") as history:
                    history.write(json.dumps({"stage": stage, **payload}, default=str) + "\n")
                _write_json_atomic(report_path, report)
                if reporter is not None:
                    reporter("OVERNIGHT HEALTH " + json.dumps(dict(payload), default=str))

            try:
                exit_code = _run_stage(command, repository=repository, log_path=log_path,
                    deadline=deadline_at, stop_request=run / "stop-request.json",
                    progress=progress, poll_seconds=poll_seconds)
                if exit_code:
                    raise RuntimeError(f"{stage} exited with code {exit_code}; inspect {log_path}")
            except BaseException as exc:
                stage_error = exc
                status = "TIMED_OUT" if isinstance(exc, TimeoutError) else "CANCELLED" if isinstance(exc, (InterruptedError, KeyboardInterrupt)) else "FAILED"
            result = StageResult(stage, command, started.isoformat(), utc_timestamp().isoformat(),
                time.monotonic() - monotonic_started, exit_code, status, log_path.name,
                f"{type(stage_error).__name__}: {stage_error}" if stage_error else None)
            results.append(result)
            report["stages"] = [asdict(value) for value in results]
            report["status"] = status if stage_error else "RUNNING"
            if reporter is not None:
                reporter(f"OVERNIGHT END {stage}: status={status}; elapsed={result.elapsed_seconds:.1f}s")
            if stage_error:
                raise stage_error
        report["status"] = "COMPLETE"
        report["current_stage"] = None
    except BaseException as exc:
        error = exc
        if report["status"] == "RUNNING":
            report["status"] = "FAILED"
        report["failed_stage"] = stage
        report["error"] = f"{type(exc).__name__}: {exc}"
    finally:
        report["completed_at"] = utc_timestamp().isoformat()
        report["heartbeat_at"] = report["completed_at"]
        report["total_elapsed_seconds"] = (utc_timestamp(report["completed_at"]) - created).total_seconds()
        _write_json_atomic(report_path, report)
        logs = {path.name: {"size": path.stat().st_size, "checksum_sha256": file_checksum(path)} for path in run.glob("*.log")}
        _write_json_atomic(run / "receipt.json", {
            "schema_version": OVERNIGHT_RUNTIME_VERSION,
            "run_path": run.relative_to(root).as_posix(), "status": report["status"],
            "completed_at": report["completed_at"], "failed_stage": report.get("failed_stage"),
            "stage_report_size": report_path.stat().st_size,
            "stage_report_checksum_sha256": file_checksum(report_path), "logs": logs,
            "broker_orders_enabled": False, "orders_placed": 0,
        })
    if error:
        raise error
    return run


def next_action_deadline(observed_at: object) -> pd.Timestamp:
    local = utc_timestamp(observed_at).tz_convert(SCHEDULE_TIMEZONE)
    label = pd.Timestamp(local.date())
    calendar = xcals.get_calendar("XNYS", start=label - pd.Timedelta(days=10), end=label + pd.Timedelta(days=30))
    session = calendar.date_to_session(label, direction="next")
    if calendar.is_session(label) and local.hour >= 4:
        session = calendar.next_session(session)
    return pd.Timestamp(pd.Timestamp(session).date()).tz_localize(SCHEDULE_TIMEZONE).replace(hour=4).tz_convert("UTC")


def _run_stage(command: Sequence[str], *, repository: Path, log_path: Path,
               deadline: pd.Timestamp, stop_request: Path,
               progress: Callable[[Mapping[str, object]], None], poll_seconds: float) -> int:
    """Stream child output to disk and expose health while long fits continue."""
    if poll_seconds <= 0:
        raise ValueError("poll_seconds must be positive")
    if utc_timestamp() >= deadline:
        raise TimeoutError("Next session's 04:00 PT publication deadline has passed")
    if stop_request.is_file():
        raise InterruptedError("A supervisor stop request was recorded before stage launch")
    environment = dict(os.environ, PYTHONUNBUFFERED="1", DUCKETZ_TRAINING_PROGRESS="1")
    flags = {"creationflags": subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP} if os.name == "nt" else {"start_new_session": True}
    with log_path.open("wb") as output:
        process = subprocess.Popen(command, cwd=repository, stdout=output, stderr=subprocess.STDOUT, env=environment, **flags)
        offset = 0
        issue_count = 0
        issue_tail: list[str] = []
        partial = b""
        try:
            while True:
                now = utc_timestamp()
                with log_path.open("rb") as reader:
                    reader.seek(offset)
                    new = reader.read()
                    offset = reader.tell()
                lines = (partial + new).split(b"\n")
                partial = lines.pop()
                exited = process.poll() is not None
                if exited and partial:
                    lines.append(partial)
                    partial = b""
                text_lines = [line.decode("utf-8", errors="replace").strip() for line in lines]
                issues = [line for line in text_lines if re.search(r"Traceback \(most recent call last\)|\b(ERROR|FAILED|FATAL|FIT_FAILED|FIT_WARNING)\b|(?:ValueError|RuntimeError|MemoryError|Exception):|loss[^\n]*\b(?:nan|inf)\b", line)]
                if issues:
                    issue_count += len(issues)
                    issue_tail = (issue_tail + issues)[-8:]
                payload = {
                    "heartbeat_at": now.isoformat(), "child_pid": process.pid,
                    "child_created_at": _process_created_at(process.pid),
                    "log_bytes": offset, "log_modified_at": pd.Timestamp(log_path.stat().st_mtime, unit="s", tz="UTC").isoformat(),
                    "seconds_to_deadline": max(0, (deadline - now).total_seconds()),
                    "stage_health": "ATTENTION_REQUIRED" if issue_count else "RUNNING",
                    "reported_issue_count": issue_count, "recent_issues": issue_tail,
                    "recent_output": text_lines[-5:],
                }
                payload.update(_process_metrics(process.pid))
                progress(payload)
                if exited:
                    return int(process.returncode)
                if stop_request.is_file():
                    raise InterruptedError(f"Supervisor requested stop: {stop_request.read_text(encoding='utf-8')[:1000]}")
                remaining = (deadline - now).total_seconds()
                if remaining <= 0:
                    raise TimeoutError("Next session's 04:00 PT publication deadline reached")
                try:
                    process.wait(timeout=min(poll_seconds, remaining))
                except subprocess.TimeoutExpired:
                    pass
        except BaseException:
            _terminate_owned_process(process)
            raise


def _process_metrics(pid: int) -> dict[str, object]:
    try:
        import psutil
        process = psutil.Process(pid)
        processes = [process, *process.children(recursive=True)]
        metrics = {"cpu_seconds": 0.0, "memory_bytes": 0, "io_bytes": 0, "process_count": 0}
        for child in processes:
            try:
                cpu = child.cpu_times()
                io = child.io_counters()
                metrics["cpu_seconds"] += cpu.user + cpu.system
                metrics["memory_bytes"] += child.memory_info().rss
                metrics["io_bytes"] += io.read_bytes + io.write_bytes
                metrics["process_count"] += 1
            except psutil.Error:
                continue
        return metrics
    except (ImportError, OSError):
        return {"process_metrics": "unavailable"}
    except Exception as exc:
        return {"process_metrics": type(exc).__name__}


def _process_created_at(pid: int) -> float | None:
    import psutil
    try:
        return psutil.Process(pid).create_time()
    except psutil.NoSuchProcess:
        return None


def recover_interrupted_run(root: Path, run: Path, reason: str) -> Path:
    """Receipt an interrupted owner; never stop a healthy owner or reused PID."""
    import psutil
    run = _validated_run(root, run)
    report_path = run / "stage-report.json"
    report = json.loads(report_path.read_text(encoding="utf-8"))
    if not reason.strip() or report.get("status") != "RUNNING" or (run / "receipt.json").exists():
        raise RuntimeError("Recovery needs an interrupted running attempt and a reason")
    owner_created = _process_created_at(int(report["owner_pid"]))
    if owner_created is not None and owner_created == report.get("owner_created_at"):
        raise RuntimeError("Overnight owner is still alive; request a controlled stage stop instead")
    child_pid = report.get("child_pid")
    if child_pid and _process_created_at(int(child_pid)) is not None:
        child = psutil.Process(int(child_pid))
        if child.create_time() != report.get("child_created_at"):
            raise RuntimeError("Child PID was reused; refusing to stop an unrelated process")
        children = child.children(recursive=True)
        for process in [*reversed(children), child]:
            try:
                process.terminate()
            except psutil.NoSuchProcess:
                pass
        _, alive = psutil.wait_procs([*children, child], timeout=10)
        for process in alive:
            process.kill()
        _, alive = psutil.wait_procs(alive, timeout=10)
        if alive:
            raise RuntimeError("Owned child processes did not stop; recovery is incomplete")
    now = utc_timestamp().isoformat()
    completed = {row["stage"] for row in report["stages"] if row["status"] == "COMPLETE"}
    unfinished = [stage for stage in report["stage_order"] if stage not in completed]
    failed_stage = unfinished[0] if unfinished else None
    if failed_stage is not None and failed_stage not in STAGE_ORDER:
        raise RuntimeError("Interrupted stage cannot be identified")
    status = "CANCELLED" if unfinished else "COMPLETE"
    report.update(status=status, failed_stage=failed_stage, completed_at=now,
                  heartbeat_at=now, error="Supervisor process exited: " + reason)
    _write_json_atomic(report_path, report)
    _write_json_atomic(run / "receipt.json", {
        "schema_version": OVERNIGHT_RUNTIME_VERSION, "run_path": run.relative_to(root).as_posix(),
        "status": status, "completed_at": now, "failed_stage": failed_stage,
        "stage_report_size": report_path.stat().st_size,
        "stage_report_checksum_sha256": file_checksum(report_path),
        "logs": {path.name: {"size": path.stat().st_size, "checksum_sha256": file_checksum(path)} for path in run.glob("*.log")},
        "broker_orders_enabled": False, "orders_placed": 0,
    })
    return run


def _terminate_owned_process(process: subprocess.Popen) -> None:
    if process.poll() is not None:
        return
    if os.name == "nt":
        subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"], capture_output=True, check=False, timeout=30)
    else:
        os.killpg(process.pid, signal.SIGTERM)
    try:
        process.wait(timeout=10)
    except subprocess.TimeoutExpired:
        if os.name != "nt":
            os.killpg(process.pid, signal.SIGKILL)
        else:
            process.kill()
        process.wait(timeout=10)


def _validated_run(root: Path, run: Path) -> Path:
    run = Path(run).resolve()
    if run.parent != (root / "ml/overnight-runs").resolve():
        raise ValueError("Overnight run escapes its expected directory")
    return run


def _resume_configuration(root: Path, run: Path) -> dict[str, object]:
    run = _validated_run(root, run)
    receipt = json.loads((run / "receipt.json").read_text(encoding="utf-8"))
    report = json.loads((run / "stage-report.json").read_text(encoding="utf-8"))
    if (receipt.get("schema_version") != OVERNIGHT_RUNTIME_VERSION
        or receipt.get("run_path") != run.relative_to(root).as_posix()
        or receipt.get("status") not in {"FAILED", "CANCELLED"}
        or receipt.get("status") != report.get("status")
        or receipt.get("stage_report_checksum_sha256") != file_checksum(run / "stage-report.json")
        or receipt.get("orders_placed") != 0 or receipt.get("broker_orders_enabled") is not False):
        raise RuntimeError("Only a verified failed or stopped overnight attempt can resume")
    if utc_timestamp() >= utc_timestamp(report["deadline_at"]):
        raise RuntimeError("The failed attempt's publication deadline has passed")
    for name, record in receipt.get("logs", {}).items():
        path = (run / name).resolve()
        if path.parent != run or file_checksum(path) != record["checksum_sha256"]:
            raise RuntimeError("Failed attempt's log evidence changed")
    completed = list(report.get("completed_stages_from_previous_attempt", []))
    completed.extend(row["stage"] for row in report["stages"] if row["status"] == "COMPLETE")
    if report.get("failed_stage") not in STAGE_ORDER:
        raise RuntimeError("Failed overnight stage is missing")
    return {**report, "completed_stages": completed}


def overnight_status(root: Path) -> dict[str, object]:
    pointer = root / "ml/overnight-latest/run.json"
    if not pointer.is_file():
        return {"status": "NO_RUN", "supervision": _read_supervision(root)}
    run = _validated_run(root, root / json.loads(pointer.read_text(encoding="utf-8"))["run_path"])
    report = json.loads((run / "stage-report.json").read_text(encoding="utf-8"))
    status = {**report, "run_path": str(run), "receipt_present": (run / "receipt.json").is_file(),
              "supervision": _read_supervision(root)}
    if report["status"] == "RUNNING":
        age = (utc_timestamp() - utc_timestamp(report.get("heartbeat_at", report["run_timestamp"]))).total_seconds()
        status["heartbeat_age_seconds"] = age
        if age > 120:
            status["stage_health"] = "SUPERVISOR_HEARTBEAT_STALE"
    return status


def _read_supervision(root: Path) -> dict[str, object]:
    path = root / "ml/overnight-supervision.json"
    if not path.is_file():
        return {"active": False}
    payload = json.loads(path.read_text(encoding="utf-8"))
    return {**payload, "active": utc_timestamp(payload["expires_at"]) > utc_timestamp()}


def claim_supervision(root: Path, token: str, *, release: bool = False,
                      observed_at: object | None = None) -> dict[str, object]:
    """A three-minute renewable lease coordinates Scheduled repair operators."""
    token = str(uuid.UUID(token))
    now = utc_timestamp(observed_at)
    path = root / "ml/overnight-supervision.json"
    with exclusive_runtime_lock(root / ".ducketz-overnight-supervision.lock", process_name="Overnight supervision claim"):
        previous = json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}
        live = previous and utc_timestamp(previous["expires_at"]) > now
        if previous.get("owner_token") != token and (live or release):
            return {"status": "BUSY" if live else "NOT_OWNER", **previous}
        payload = {"owner_token": token, "updated_at": now.isoformat(),
                   "expires_at": (now if release else now + pd.Timedelta(minutes=3)).isoformat()}
        _write_json_atomic(path, payload)
        return {"status": "RELEASED" if release else "ACQUIRED", **payload}


def request_stage_stop(root: Path, run: Path, reason: str) -> Path:
    run = _validated_run(root, run)
    report = json.loads((run / "stage-report.json").read_text(encoding="utf-8"))
    if report.get("status") != "RUNNING" or (run / "receipt.json").exists():
        raise RuntimeError("Only a running overnight attempt can be stopped")
    if not reason.strip():
        raise ValueError("A stop reason is required")
    request = run / "stop-request.json"
    _write_json_atomic(request, {"requested_at": utc_timestamp().isoformat(), "reason": reason})
    return request


def _write_json_atomic(path: Path, payload: Mapping[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + f".tmp-{os.getpid()}")
    try:
        temporary.write_text(
            json.dumps(dict(payload), indent=2, sort_keys=True, default=str) + "\n",
            encoding="utf-8",
        )
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Run the single post-close owner: fetch, Loop B, Strategy training, "
            "Strategy selection, and immutable gameplan publication."
        )
    )
    datastore = parser.add_mutually_exclusive_group()
    datastore.add_argument("--datastore", type=Path, default=None)
    datastore.add_argument(
        "--datastore-target",
        choices=tuple(DATASTORE_TARGETS),
        default="pc",
    )
    parser.add_argument("--start-at", choices=STAGE_ORDER, default=STAGE_ORDER[0])
    parser.add_argument("--stop-after", choices=STAGE_ORDER, default=STAGE_ORDER[-1])
    parser.add_argument("--resume-run", type=Path, help="Resume the failed stage after a verified repair")
    parser.add_argument("--status", action="store_true", help="Read the latest overnight progress without starting work")
    parser.add_argument("--request-stop-run", type=Path, help="Ask the owner to stop its current stage")
    parser.add_argument("--recover-run", type=Path, help="Recover an attempt whose supervisor process exited")
    parser.add_argument("--claim-supervision", metavar="UUID", help="Acquire/renew this Scheduled operator's three-minute supervision lease")
    parser.add_argument("--release-supervision", metavar="UUID", help="Release this Scheduled operator's supervision lease")
    parser.add_argument("--reason", default="", help="Evidence-based reason for requesting a stage stop")
    parser.add_argument("--once", action="store_true", help="Compatibility flag")
    parser.add_argument(
        "--scheduled",
        action="store_true",
        help=(
            "Require a completed 17:00 PT XNYS action date; record an audited "
            "no-op on exchange holidays"
        ),
    )
    args = parser.parse_args(argv)
    root = resolve_datastore_dir(
        root_dir=args.datastore,
        target=None if args.datastore is not None else args.datastore_target,
    )
    datastore_argument = (
        ("--datastore", str(args.datastore))
        if args.datastore is not None
        else ("--datastore-target", str(args.datastore_target))
    )
    if args.status:
        print(json.dumps(overnight_status(root), indent=2, default=str))
        return 0
    if args.claim_supervision or args.release_supervision:
        result = claim_supervision(root, args.claim_supervision or args.release_supervision,
                                   release=bool(args.release_supervision))
        print(json.dumps(result))
        return 0
    if args.request_stop_run:
        print(request_stage_stop(root, args.request_stop_run, args.reason))
        return 0
    repository = Path(__file__).resolve().parents[1]
    lock = root / ".ducketz-overnight-runtime.lock"
    with exclusive_runtime_lock(lock, process_name="Duckets overnight runtime"):
        try:
            if args.recover_run:
                print(recover_interrupted_run(root, args.recover_run, args.reason))
                return 0
            if args.scheduled and not args.resume_run:
                eligibility = scheduled_session_eligibility()
                if not bool(eligibility["eligible"]):
                    if eligibility["status"] == "NOOP_NON_SESSION_DATE":
                        run = record_scheduled_noop(
                            root,
                            eligibility=eligibility,
                        )
                        print(
                            "Overnight runtime no-op: "
                            f"{eligibility['reason']} receipt={run}"
                        )
                        return 0
                    raise RuntimeError(str(eligibility["reason"]))
            run = run_overnight_pipeline(
                root,
                datastore_argument=datastore_argument,
                repository_root=repository,
                start_at=args.start_at,
                stop_after=args.stop_after,
                resume_run=args.resume_run,
            )
        except Exception as exc:
            print(f"Overnight runtime failed: {type(exc).__name__}: {exc}")
            return 1
    print(f"Overnight runtime complete: {run}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "ACTION_DAY_CLOSE_HOUR",
    "OVERNIGHT_NOOP_VERSION",
    "OVERNIGHT_RUNTIME_VERSION",
    "SCHEDULE_TIMEZONE",
    "STAGE_ORDER",
    "StageResult",
    "record_scheduled_noop",
    "run_overnight_pipeline",
    "scheduled_session_eligibility",
    "claim_supervision",
    "next_action_deadline",
    "overnight_status",
    "recover_interrupted_run",
    "request_stage_stop",
]
