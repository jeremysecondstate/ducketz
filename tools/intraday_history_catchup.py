"""Continue canonical 15-minute history without fitting models or starting a trader."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import uuid


def catch_up(*, datastore, profile, watchlist, config, log_dir=None):
    from datafetching.runtime_lock import exclusive_runtime_lock
    from ml.intraday.prediction import ContractError
    from ml.intraday.runtime import local_symbols
    from ml.intraday.supervisor import _atomic_json, _failure, refresh_ohlcv

    datastore = Path(datastore)
    actor, symbols = local_symbols(profile, watchlist, datastore)
    settings = json.loads(Path(config).read_text(encoding="utf-8-sig"))
    dataset = settings.get("source_dataset", "EQUS.MINI")
    if not isinstance(dataset, str) or not dataset:
        raise ContractError("The existing supervisor source_dataset must be a nonempty string")
    started = datetime.now(timezone.utc)
    run_id = started.strftime("%Y%m%dT%H%M%SZ") + "-" + uuid.uuid4().hex[:12]
    report_path = Path(log_dir or datastore / "intraday/history-catchup") / (run_id + ".json")
    report = {"schema": "intraday-history-catchup-v1", "run_id": run_id,
              "actor": actor, "source_dataset": dataset,
              "started_at": started.isoformat(), "symbols": {}, "status": "running",
              "model_fitting": False, "trader_activation": False}
    # Share the supervisor's owner lock; refresh_ohlcv also uses the canonical
    # DATASTORE writer lock and reuses retained daytime minute responses first.
    with exclusive_runtime_lock(datastore / "locks/intraday-model-supervision.lock",
                                process_name="local 15m history catch-up"):
        _atomic_json(report_path, report)
        try:
            evidence = refresh_ohlcv(datastore, symbols, source_dataset=dataset)
            report["symbols"] = evidence
            report["status"] = ("complete" if set(evidence) == set(symbols)
                                and all(evidence[s].get("status") == "refreshed" for s in symbols)
                                else "incomplete")
        except Exception as exc:
            report.update(status="error", error=_failure(exc))
        report["completed_at"] = datetime.now(timezone.utc).isoformat()
        _atomic_json(report_path, report)
    return report, report_path


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("datastore", "profile", "watchlist", "config"):
        parser.add_argument("--" + name, type=Path, required=True)
    parser.add_argument("--log-dir", type=Path)
    args = parser.parse_args(argv)
    report, path = catch_up(**vars(args))
    print(json.dumps({"run_id": report["run_id"], "status": report["status"],
                      "report": str(path)}, sort_keys=True), flush=True)
    return 0 if report["status"] == "complete" else 1


if __name__ == "__main__":
    raise SystemExit(main())
