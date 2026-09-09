from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
from typing import Mapping, Sequence
from zoneinfo import ZoneInfo

import pandas as pd

from datafetching.parquet_store import DATASTORE_TARGETS, resolve_datastore_dir
from datafetching.runtime_lock import exclusive_runtime_lock
from ml.artifacts import create_timestamp_directory, file_checksum, utc_timestamp
from ml.nightly_gameplan import (
    ACTION_END_HOUR,
    ACTION_START_HOUR,
    EXECUTION_AUTHORITY,
    read_current_gameplan,
)


EXECUTOR_VERSION = "immutable-gameplan-paper-executor-v2"
EXECUTOR_RECEIPT_VERSION = "immutable-gameplan-paper-executor-receipt-v2"
SCHEDULE_TIMEZONE = ZoneInfo("America/Los_Angeles")


def run_gameplan_decision_once(
    datastore_root: Path,
    *,
    decision_at: object | None = None,
) -> Path:
    """Read a frozen plan and publish a no-order decision receipt.

    This is the deliberately lightweight daytime consumer.  It has no broker
    import, cannot retrain, and cannot replace a candidate.  Live execution can
    only be added later behind a separately authorized adapter.
    """

    root = Path(datastore_root).resolve()
    observed = utc_timestamp(decision_at)
    local = observed.tz_convert(SCHEDULE_TIMEZONE)
    if not ACTION_START_HOUR <= local.hour <= ACTION_END_HOUR:
        raise RuntimeError(
            "Gameplan decisions are limited to 04:00-17:00 America/Los_Angeles"
        )
    publication = read_current_gameplan(root)
    if publication.receipt.get("action_date") != local.date().isoformat():
        raise RuntimeError(
            "Current gameplan is not for this action date: "
            f"plan={publication.receipt.get('action_date')}; now={local.date()}"
        )
    forecasts = pd.read_parquet(publication.run_directory / "forecasts.parquet")
    intents = pd.read_parquet(
        publication.run_directory / "option-strategy-intents.parquet"
    )
    anchor = f"{local.hour:02d}:00"
    expected_routes = _routes_for_action_anchor(local.hour)
    if "action_anchor_local" in forecasts.columns:
        selected = forecasts.loc[
            forecasts["action_anchor_local"].astype("string").eq(anchor)
        ].copy()
        selected_routes = set(selected["route"].astype(str))
    else:
        selected_routes = expected_routes
        selected = forecasts.loc[forecasts["route"].isin(selected_routes)].copy()
    if selected_routes != expected_routes:
        raise RuntimeError(f"Frozen gameplan has no route for {anchor} PT")
    selected_intents = intents.loc[intents["route"].isin(selected_routes)].copy()
    decisions = []
    for forecast in selected.to_dict("records"):
        eligible = bool(
            forecast.get("model_status") == "PROMOTED"
            and forecast.get("direction") in {"BULLISH", "BEARISH"}
        )
        decisions.append(
            {
                "forecast_id": forecast["id"],
                "symbol": forecast["symbol"],
                "route": forecast["route"],
                "direction": forecast["direction"],
                "probability_up": forecast["calibrated_probability"],
                "decision": (
                    "PAPER_SIGNAL_RECORDED" if eligible else "SKIP_NO_PROMOTED_EDGE"
                ),
                "orders_submitted": 0,
            }
        )
    option_decisions = [
        {
            "intent_id": row["id"],
            "symbol": row["symbol"],
            "route": row["route"],
            "candidate_key": row.get("candidate_key"),
            "decision": row["plan_status"],
            "same_legs_revalidation_required": True,
            "orders_submitted": 0,
        }
        for row in selected_intents.to_dict("records")
    ]
    run = create_timestamp_directory(
        root / "ml" / "gameplan-decision-runs",
        timestamp=observed,
    )
    decision_path = run / "decision.json"
    _write_json_atomic(
        decision_path,
        {
            "schema_version": EXECUTOR_VERSION,
            "decision_at": observed.isoformat(),
            "decision_at_local": local.isoformat(),
            "action_date": local.date().isoformat(),
            "anchor_local": anchor,
            "forecast_routes_consumed": sorted(selected_routes),
            "cycle_status": (
                "SESSION_CLOSE_NO_NEW_FORWARD_WINDOW"
                if not selected_routes
                else "FROZEN_FORECASTS_CONSUMED"
            ),
            "source_gameplan_run": publication.run_directory.relative_to(root).as_posix(),
            "source_gameplan_receipt_checksum_sha256": publication.pointer["current"][
                "receipt_checksum_sha256"
            ],
            "execution_authority": EXECUTION_AUTHORITY,
            "broker_adapter_loaded": False,
            "orders_submitted": 0,
            "stock_direction_decisions": decisions,
            "option_strategy_decisions": option_decisions,
        },
    )
    _write_json_atomic(
        run / "receipt.json",
        {
            "schema_version": EXECUTOR_RECEIPT_VERSION,
            "decision_path": decision_path.name,
            "decision_size": decision_path.stat().st_size,
            "decision_checksum_sha256": file_checksum(decision_path),
            "source_gameplan_run": publication.run_directory.relative_to(root).as_posix(),
            "execution_authority": EXECUTION_AUTHORITY,
            "broker_adapter_loaded": False,
            "orders_submitted": 0,
        },
    )
    return run


def _routes_for_action_anchor(hour: int) -> set[str]:
    """Map an action clock to forecast endpoints that are still actionable.

    Intraday route labels are predicted checkpoints, not decision timestamps.
    At 05:00, for example, the completed 04:00-05:00 forecast is no longer a
    forward signal; the reader consumes the frozen 06:00 checkpoint instead.
    The 04:00 wake additionally records the two overnight-gap/open forecasts.
    """

    if not ACTION_START_HOUR <= int(hour) <= ACTION_END_HOUR:
        return set()
    routes: set[str] = set()
    if hour == ACTION_START_HOUR:
        routes.update(("1h@04:00", "4h@04:00"))
    if hour < ACTION_END_HOUR:
        routes.add(f"1h@{hour + 1:02d}:00")
    if hour in (4, 8, 12):
        routes.add(f"4h@{hour + 4:02d}:00")
    return routes


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
        description="Read the immutable gameplan and record one paper-only decision."
    )
    datastore = parser.add_mutually_exclusive_group()
    datastore.add_argument("--datastore", type=Path, default=None)
    datastore.add_argument(
        "--datastore-target",
        choices=tuple(DATASTORE_TARGETS),
        default="pc",
    )
    parser.add_argument("--at", default=None, help="Optional UTC test timestamp")
    parser.add_argument("--once", action="store_true", help="Compatibility flag")
    args = parser.parse_args(argv)
    root = resolve_datastore_dir(
        root_dir=args.datastore,
        target=None if args.datastore is not None else args.datastore_target,
    )
    lock = root / ".ducketz-gameplan-executor.lock"
    with exclusive_runtime_lock(lock, process_name="Duckets gameplan executor"):
        try:
            run = run_gameplan_decision_once(root, decision_at=args.at)
        except Exception as exc:
            print(f"Gameplan decision failed: {type(exc).__name__}: {exc}")
            return 1
    print(f"Gameplan paper decision recorded: {run}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "EXECUTOR_RECEIPT_VERSION",
    "EXECUTOR_VERSION",
    "_routes_for_action_anchor",
    "run_gameplan_decision_once",
]
