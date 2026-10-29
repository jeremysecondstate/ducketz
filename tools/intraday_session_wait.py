"""Wait for the Pacific 04:00 session before entering either 15-minute host.

This launcher imports only the standard library. Waiting owns no data/trader
session and never calls providers, changes controls, or affects an existing owner.
The Windows timezone database is available even under Python's isolated -I -S.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import os
from pathlib import Path
import runpy
import subprocess
import sys
import time


UTC = timezone.utc


def pacific_session_start(now):
    """Return today's 04:00 Pacific in UTC, including daylight-saving changes."""
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("The session clock must be timezone aware")
    now = now.astimezone(UTC)
    if os.name == "nt":
        # Do not import mutable site packages just to obtain timezone data.
        # The timestamp is generated here, never taken from command-line text.
        command = (
            "$ErrorActionPreference='Stop';"
            "$zone=[TimeZoneInfo]::FindSystemTimeZoneById('Pacific Standard Time');"
            f"$now=[DateTimeOffset]::Parse('{now.isoformat()}');"
            "$local=[TimeZoneInfo]::ConvertTime($now,$zone);"
            "$start=[TimeZoneInfo]::ConvertTimeToUtc($local.Date.AddHours(4),$zone);"
            "[Console]::WriteLine(([DateTimeOffset]$start).ToUnixTimeSeconds())"
        )
        powershell = Path(os.environ.get("SystemRoot", "C:/Windows")) / "System32/WindowsPowerShell/v1.0/powershell.exe"
        result = subprocess.run(
            [str(powershell), "-NoLogo", "-NoProfile", "-NonInteractive", "-Command", command],
            check=True, capture_output=True, text=True, timeout=15,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
        return datetime.fromtimestamp(int(result.stdout.strip()), UTC)
    from zoneinfo import ZoneInfo
    local = now.astimezone(ZoneInfo("America/Los_Angeles"))
    return local.replace(hour=4, minute=0, second=0, microsecond=0).astimezone(UTC)


def wait_until_start(label, *, now=lambda: datetime.now(UTC), sleep=time.sleep, emit=print):
    current = now()
    target = pacific_session_start(current)
    last_message = None
    while True:
        current = now()
        remaining = (target - current).total_seconds()
        if remaining <= 0:
            # A suspended PC can wake on another date before that day's start.
            target = pacific_session_start(current)
            remaining = (target - current).total_seconds()
            if remaining <= 0:
                emit(f"{label}: 4:00 AM Pacific session start reached. Starting now.")
                return
        if last_message is None or abs((current - last_message).total_seconds()) >= 60:
            minutes = max(1, int((remaining + 59) // 60))
            emit(f"{label}: Waiting until 4:00 AM Pacific session start "
                 f"({minutes} minutes remaining). Ctrl+C cancels this launch.")
            last_message = current
        sleep(min(1.0, remaining))


def dispatch_host(role, forwarded):
    """Enter the original host in this process, preserving its exit/stop behavior."""
    repository = Path(__file__).resolve().parents[1]
    if role == "data":
        host = repository / "tools/intraday_data_host.py"
        arguments = ["--repository", str(repository), "--datastore", "C:\\DATASTORE"]
    elif role == "trader":
        host = repository / "tools/trader_session_host.py"
        arguments = ["--trader", "intraday"]
    else:
        raise ValueError("Unknown 15-minute launcher role")
    original_argv = sys.argv
    sys.argv = [str(host), *arguments, *forwarded]
    try:
        runpy.run_path(str(host), run_name="__main__")
    except SystemExit as exc:
        if exc.code is None or isinstance(exc.code, int):
            return exc.code or 0
        raise
    finally:
        sys.argv = original_argv
    return 0


def main(argv=None, *, now=None, sleep=None, emit=None, dispatch=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("role", choices=("data", "trader"))
    arguments = list(sys.argv[1:] if argv is None else argv)
    # Only the first token belongs to this wrapper. Preserve every host option,
    # including --help, quoted paths, and literal punctuation, without parsing it.
    options = parser.parse_args(arguments[:1])
    label = "15-Minute Data" if options.role == "data" else "15-Minute Trader"
    output = emit if emit is not None else lambda line: print(line, flush=True)
    try:
        wait_until_start(label, now=now or (lambda: datetime.now(UTC)),
                         sleep=sleep or time.sleep, emit=output)
    except KeyboardInterrupt:
        output(f"{label}: Launch cancelled while waiting; no session was started.")
        return 130
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        output(f"{label}: Cannot determine the Pacific session start ({type(exc).__name__}); no session was started.")
        return 2
    return (dispatch or dispatch_host)(options.role, arguments[1:])


if __name__ == "__main__":
    raise SystemExit(main())
