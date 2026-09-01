from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Sequence

import pandas as pd

from datafetching.parquet_store import DATASTORE_TARGETS, resolve_datastore_dir
from datafetching.runtime_lock import exclusive_runtime_lock
from ml.calendars import (
    ExchangeSessionCalendar,
    US_EQUITY_ACTIONABLE_TARGET_POLICY,
)
from ml.stock_trader.audit import build_stock_trader_weekly_audit
from ml.stock_trader.contracts import utc
from ml.stock_trader.training import train_and_publish_enrichment_model


def adapt_after_latest_completed_session(
    datastore_root: Path,
    *,
    as_of: object | None = None,
    live_adaptation_weight: int = 2,
) -> dict[str, object]:
    root = Path(datastore_root).resolve()
    timestamp = utc(as_of)
    calendar = ExchangeSessionCalendar(
        "XNYS",
        start=timestamp - pd.Timedelta(days=14),
        end=timestamp + pd.Timedelta(days=2),
    )
    completed = []
    for candidate in calendar.sessions:
        _actionable_open, actionable_close = calendar.intraday_target_bounds(
            candidate,
            session_policy=US_EQUITY_ACTIONABLE_TARGET_POLICY,
        )
        if actionable_close <= timestamp:
            completed.append(candidate)
    if not completed:
        raise ValueError("No completed XNYS session is available for adaptation")
    session = completed[-1]
    local_date = timestamp.tz_convert("America/New_York").date()
    if pd.Timestamp(session).date() != local_date:
        return {
            "status": "NO_COMPLETED_XNYS_SESSION_TODAY",
            "as_of": timestamp.isoformat(),
        }
    opened = calendar.session_open(session)
    closed = calendar.session_close(session)
    actionable_open, actionable_close = calendar.intraday_target_bounds(
        session,
        session_policy=US_EQUITY_ACTIONABLE_TARGET_POLICY,
    )
    # Audit the complete broker-actionable PRE/REGULAR/POST stock day. The
    # official exchange open/close remain separately reported below.
    audit = build_stock_trader_weekly_audit(
        root,
        window_start=actionable_open,
        window_end=actionable_close + pd.Timedelta(nanoseconds=1),
        evaluated_at=timestamp,
    )
    model_run = train_and_publish_enrichment_model(
        root,
        trained_at=timestamp,
        minimum_rows=40,
        ridge_penalty=5.0,
        include_loop_b_bootstrap=True,
        live_adaptation_weight=live_adaptation_weight,
    )
    return {
        "status": "DAILY_ADAPTATION_PUBLISHED",
        "session": str(pd.Timestamp(session).date()),
        "session_open": opened.isoformat(),
        "session_close": closed.isoformat(),
        "equity_actionable_open": actionable_open.isoformat(),
        "equity_actionable_close": actionable_close.isoformat(),
        "audit_run_directory": str(audit.run_directory),
        "pair_count": audit.pair_count,
        "mature_pair_count": audit.mature_pair_count,
        "model_run_directory": str(model_run),
        "live_adaptation_weight": live_adaptation_weight,
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Audit the latest completed stock session and publish the next-session enrichment model."
    )
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--root-dir", type=Path)
    group.add_argument("--datastore-target", choices=sorted(DATASTORE_TARGETS))
    parser.add_argument("--as-of")
    parser.add_argument("--live-adaptation-weight", type=int, default=2)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        root = resolve_datastore_dir(
            root_dir=args.root_dir, target=args.datastore_target
        )
        with exclusive_runtime_lock(
            root / "locks" / "stock-trader-daily-adaptation.lock",
            process_name="stock-trader-daily-adaptation",
        ):
            result = adapt_after_latest_completed_session(
                root,
                as_of=args.as_of,
                live_adaptation_weight=args.live_adaptation_weight,
            )
    except Exception as exc:
        print(json.dumps({"status": "FAILED", "error": f"{type(exc).__name__}: {exc}"}))
        return 1
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = ["adapt_after_latest_completed_session", "main"]
