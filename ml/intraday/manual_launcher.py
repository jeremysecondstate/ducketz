"""Human-started Scout launcher that waits for one selected operating session.

The existing engine and frozen model assessment bindings remain unchanged.
Without --live, or for Atlas, delegate directly to the existing entrypoint.
"""
import argparse
from datetime import timedelta
import json
import os
from pathlib import Path
import sys
import time
import uuid

from datafetching.runtime_lock import exclusive_runtime_lock
from .prediction import ContractError, PACIFIC, instant
from .runtime import local_symbols
from .store import utcnow


def selected_wake_at(started):
    """Select the first unfinished exchange session once per manual invocation."""
    from .data import equity_sessions
    started = instant(started)
    local = started.astimezone(PACIFIC)
    for session in equity_sessions(started, started + timedelta(days=14)):
        opened = instant(session.opened_at)
        day = opened.astimezone(PACIFIC)
        if day.date() >= local.date() and started < instant(session.closed_at):
            wake = day.replace(hour=4, minute=0, second=0, microsecond=0)
            return max(instant(wake), opened)
    raise ContractError("No upcoming exchange session available for manual startup")


def _delegate(argv):
    from .live_session import main
    return main(argv)


class _SelectedSessionUnavailable(RuntimeError):
    def __init__(self, reason):
        super().__init__(reason)
        self.reason = reason


def _run_scout_engine(args, *, wake, stop, clock):
    from . import live_session
    first_read = [True]

    def startup_clock():
        observed = instant(clock())
        if first_read[0]:
            if observed < wake or observed >= stop:
                raise _SelectedSessionUnavailable(
                    "selected_session_elapsed" if observed >= stop else "clock_before_selected_session")
            first_read[0] = False
        # Preserve actual timestamps. Once startup is latched, the engine's
        # normal deadline and shutdown logic receive every subsequent real read.
        return observed

    result = live_session.run_manual(repository=args.repository, datastore=args.datastore,
        profile=args.profile or args.repository / "scratch/cross-pc/local-profile.json",
        watchlist=args.watchlist or args.repository / "datafetching/watchlist.local.txt",
        scout_admission=args.scout_admission, clock=startup_clock, model_recipe=args.model_recipe,
        config=live_session.ResearchConfig(publication_offset_seconds=args.publication_offset_seconds,
            symbol_workers=args.symbol_workers, lookback_days=args.lookback_days, use_supervised_models=True))
    print(json.dumps(result, sort_keys=True))
    return 1 if result["status"] == "FAILED" else 0


def run_scout(argv, *, datastore, clock=utcnow, sleep=time.sleep, delegate=None, engine_args=None):
    root = Path(datastore).resolve()
    started = instant(clock())
    wake = selected_wake_at(started)
    stop = instant(wake.astimezone(PACIFIC).replace(hour=17, minute=0, second=0, microsecond=0))
    invocation = uuid.uuid4().hex
    status_path = root / "intraday/manual-launcher.json"
    log_path = root / "intraday/launchers" / (invocation + ".jsonl")
    last_state = [None]

    def status(state, **extra):
        value = {"schema_version": "scout-manual-launcher-v1", "invocation_id": invocation,
            "pc": "Scout", "pid": os.getpid(), "started_at": started.isoformat(),
            "heartbeat_at": instant(clock()).isoformat(), "status": state,
            "wake_at": wake.isoformat(), "automatic_stop_at": stop.isoformat(),
            "manual_start": True, "automatic_daily_restart": False,
            "engine_status_path": str(root / "intraday/manual-session.json"), **extra}
        status_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = status_path.with_name(status_path.name + "." + invocation + ".tmp")
        with temporary.open("w", encoding="utf-8") as stream:
            json.dump(value, stream, sort_keys=True)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, status_path)
        if last_state[0] != state:
            log_path.parent.mkdir(parents=True, exist_ok=True)
            with log_path.open("a", encoding="utf-8") as stream:
                stream.write(json.dumps(value, sort_keys=True) + "\n")
            last_state[0] = state

    # The launcher lock spans delegation. The engine lock excludes direct
    # engine starts while waiting; release it only for the engine to acquire.
    with exclusive_runtime_lock(root / "locks/intraday-manual-launcher.lock", process_name="intraday-manual-launcher"):
        try:
            with exclusive_runtime_lock(root / "locks/intraday-manual-session.lock", process_name="intraday-manual-session"):
                if instant(clock()) < wake:
                    print(f"Scout 15m waiting until {wake.astimezone(PACIFIC).isoformat()} America/Los_Angeles. "
                          "Ctrl-C cancels this manual start; the trader has not started.", flush=True)
                while instant(clock()) < wake:
                    status("WAITING")
                    sleep(min(60, max(0, (wake - instant(clock())).total_seconds())))
                if instant(clock()) >= stop:
                    status("STOPPED", stop_reason="selected_session_elapsed")
                    return 0
                status("DELEGATING")
            # No new calendar selection or repeat loop belongs here. The engine
            # loads the current recipe and performs its normal preflight now.
            if instant(clock()) >= stop:
                status("STOPPED", stop_reason="selected_session_elapsed")
                return 0
            result = (delegate(argv) if delegate is not None else
                _run_scout_engine(engine_args or _arguments(argv), wake=wake, stop=stop, clock=clock))
            status("FINISHED", engine_exit_code=result)
            return result
        except _SelectedSessionUnavailable as exc:
            status("STOPPED", stop_reason=exc.reason)
            return 0
        except KeyboardInterrupt:
            status("STOPPED", stop_reason="manual_stop")
            return 0
        except Exception as exc:
            status("FAILED", error_type=type(exc).__name__)
            raise


def _arguments(argv):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repository", type=Path, default=Path(__file__).resolve().parents[2])
    parser.add_argument("--datastore", type=Path, default=Path("C:/DATASTORE"))
    parser.add_argument("--profile", type=Path)
    parser.add_argument("--watchlist", type=Path)
    parser.add_argument("--live", action="store_true")
    parser.add_argument("--scout-admission", choices=("broker-local", "atlas-service"), default="broker-local")
    parser.add_argument("--publication-offset-seconds", type=float, default=30)
    parser.add_argument("--symbol-workers", type=int, default=1)
    parser.add_argument("--lookback-days", type=int)
    parser.add_argument("--model-recipe", type=Path,
        help="Explicit local manual model recipe; never replace the frozen study default")
    return parser.parse_args(argv)


def main(argv=None, *, clock=utcnow, sleep=time.sleep, delegate=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    args = _arguments(argv)
    if not args.live:
        return (delegate or _delegate)(argv)
    profile = args.profile or args.repository / "scratch/cross-pc/local-profile.json"
    watchlist = args.watchlist or args.repository / "datafetching/watchlist.local.txt"
    pc, _ = local_symbols(profile, watchlist, args.datastore)
    if pc != "Scout":
        return (delegate or _delegate)(argv)
    return run_scout(argv, datastore=args.datastore, clock=clock, sleep=sleep, delegate=delegate, engine_args=args)


if __name__ == "__main__":
    raise SystemExit(main())
