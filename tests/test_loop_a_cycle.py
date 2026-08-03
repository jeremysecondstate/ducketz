from __future__ import annotations

import subprocess
import sys
import threading
from pathlib import Path

import pytest

from datafetching.loop_a_cycle import (
    LOOP_A_CYCLE_LOCK_FILENAME,
    LoopACycleError,
    begin_loop_a_cycle,
    datastore_cycle_lock,
    finish_loop_a_cycle,
    read_loop_a_cycle,
    require_complete_loop_a_cycle,
)


def test_loop_a_cycle_transitions_are_atomic_and_readable(tmp_path: Path) -> None:
    writing = begin_loop_a_cycle(
        tmp_path,
        symbols=("goog",),
        providers=("DataBento", "FMP"),
        now="2026-07-30T15:00:00Z",
    )
    assert read_loop_a_cycle(tmp_path) == writing
    assert writing.status == "WRITING"
    with pytest.raises(LoopACycleError, match="WRITING"):
        require_complete_loop_a_cycle(tmp_path)

    complete = finish_loop_a_cycle(
        tmp_path,
        writing,
        failure_count=0,
        now="2026-07-30T15:01:00Z",
    )
    assert require_complete_loop_a_cycle(tmp_path) == complete
    assert complete.status == "COMPLETE"
    assert complete.symbols == ("GOOG",)
    assert complete.providers == ("databento", "fmp")


def test_failed_cycle_cannot_be_consumed_by_loop_b(tmp_path: Path) -> None:
    writing = begin_loop_a_cycle(
        tmp_path,
        symbols=("GOOG",),
        providers=("databento",),
        now="2026-07-30T15:00:00Z",
    )
    failed = finish_loop_a_cycle(
        tmp_path,
        writing,
        failure_count=3,
        now="2026-07-30T15:01:00Z",
    )
    assert failed.status == "FAILED"
    with pytest.raises(LoopACycleError, match="FAILED"):
        require_complete_loop_a_cycle(tmp_path)


def test_cycle_lock_is_released_when_the_owner_process_exits(
    tmp_path: Path,
) -> None:
    script = (
        "import os, sys; "
        "from pathlib import Path; "
        "from datafetching.loop_a_cycle import datastore_cycle_lock; "
        "\nwith datastore_cycle_lock(Path(sys.argv[1])):\n"
        " print('locked', flush=True); os._exit(0)"
    )
    process = subprocess.run(
        [sys.executable, "-c", script, str(tmp_path)],
        cwd=Path(__file__).resolve().parents[1],
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    assert process.returncode == 0
    assert process.stdout.strip() == "locked"

    with datastore_cycle_lock(tmp_path):
        assert (tmp_path / LOOP_A_CYCLE_LOCK_FILENAME).is_file()


def test_cycle_lock_excludes_the_other_loop_until_release(
    tmp_path: Path,
) -> None:
    script = (
        "import sys; "
        "from pathlib import Path; "
        "from datafetching.loop_a_cycle import datastore_cycle_lock; "
        "\nwith datastore_cycle_lock(Path(sys.argv[1])):\n"
        " print('locked', flush=True); sys.stdin.readline()"
    )
    process = subprocess.Popen(
        [sys.executable, "-c", script, str(tmp_path)],
        cwd=Path(__file__).resolve().parents[1],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        bufsize=1,
    )
    acquired = threading.Event()

    def contend() -> None:
        with datastore_cycle_lock(tmp_path, poll_seconds=0.01):
            acquired.set()

    contender: threading.Thread | None = None
    try:
        assert process.stdout is not None
        assert process.stdout.readline().strip() == "locked"
        contender = threading.Thread(target=contend, daemon=True)
        contender.start()
        assert not acquired.wait(0.2)

        assert process.stdin is not None
        process.stdin.write("\n")
        process.stdin.flush()
        assert process.wait(timeout=10) == 0
        assert acquired.wait(5)
        contender.join(timeout=5)
        assert not contender.is_alive()
    finally:
        if process.poll() is None:
            if process.stdin is not None:
                process.stdin.write("\n")
                process.stdin.flush()
            try:
                process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=2)
        if contender is not None:
            contender.join(timeout=5)
