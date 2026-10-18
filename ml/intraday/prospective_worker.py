"""Durable non-trading prospective worker; scheduler installation is external."""
from __future__ import annotations

import argparse
from contextlib import contextmanager
import json
import os
from pathlib import Path
import time

from .prediction import ContractError, encoded
from .prospective import Collector, utcnow


@contextmanager
def process_lock(path):
    """Native process-owned lock: a crash releases ownership without stale deletion."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = path.open("a+b")
    handle.seek(0, os.SEEK_END)
    if handle.tell() == 0:
        handle.write(b"0")
        handle.flush()
    handle.seek(0)
    try:
        if os.name == "nt":
            import msvcrt
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as exc:
        handle.close()
        raise ContractError("Prospective collector already owns this store") from exc
    try:
        yield
    finally:
        handle.close()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--once", action="store_true")
    mode.add_argument("--watch", action="store_true")
    mode.add_argument("--verify-only", action="store_true")
    args = parser.parse_args(argv)
    config = json.loads(args.config.read_text(encoding="utf-8"))
    root = Path(config["state_root"])
    with process_lock(root / "collector.lock"):
        collector = Collector(config)
        collector.verify_sources()
        if args.verify_only:
            result = {"status": "verified_offline", "trading": False, "provider_calls": 0,
                "frozen_artifacts": sum(len(v) for v in config["model_artifacts"].values()),
                "scheduled_slots": len(collector.schedule), "qualification": "not_assessed"}
            collector.store.event("offline_installation_verification", utcnow, **result)
            print(encoded(result), flush=True)
            return 0
        from dotenv import load_dotenv
        load_dotenv(Path(__file__).resolve().parents[2] / ".env", override=False)
        collector.store.event("worker_started", utcnow, pid=os.getpid(), trading=False,
                              mode="watch" if args.watch else "once")
        interval = max(10.0, min(60.0, float(config.get("poll_seconds", 30))))
        while True:
            try:
                result = collector.tick()
                print(encoded(result), flush=True)
            except Exception as exc:
                collector.store.event("worker_error", utcnow, exception_type=type(exc).__name__,
                                      message=str(exc)[:500])
                print(encoded({"status": "error", "type": type(exc).__name__, "at": utcnow().isoformat()}), flush=True)
                if args.once:
                    return 1
            if args.once:
                return 0
            time.sleep(interval)


if __name__ == "__main__":
    raise SystemExit(main())
