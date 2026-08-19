from __future__ import annotations

import argparse
import json
import math
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Mapping, Sequence

import pandas as pd

from datafetching.bar_readiness import read_bar_readiness
from datafetching.parquet_store import DATASTORE_TARGETS, resolve_datastore_dir
from ml.option_pricing.target_outcome import read_target_outcome
from ml.strategy_publication import read_current_strategy_publication
from ml.strategy_selection.contracts import (
    BLACK_SCHOLES_CALIBRATED_MODEL_SCORE_BASIS,
    BSGP_CALIBRATED_MODEL_SCORE_BASIS,
    SCENARIO_COVERAGE_SCORE_BASIS,
)


DEFAULT_WATCHLIST = Path(__file__).resolve().parents[1] / "datafetching" / "watchlist.txt"


class StrategyPricingCanaryError(RuntimeError):
    """The bounded read-only Strategy/Pricing canary did not pass."""


def run_canary(
    datastore_root: Path,
    *,
    target_snapshot_for: object,
    symbols: Sequence[str],
    timeout_seconds: float = 600.0,
    poll_seconds: float = 5.0,
    clock: Callable[[], object] | None = None,
    sleeper: Callable[[float], None] = time.sleep,
    monotonic_clock: Callable[[], float] = time.monotonic,
) -> Mapping[str, object]:
    """Wait for and verify one exact target without writing or placing orders."""

    if timeout_seconds < 0:
        raise ValueError("Canary timeout cannot be negative")
    if poll_seconds <= 0:
        raise ValueError("Canary poll interval must be positive")
    root = Path(datastore_root).resolve()
    target = _utc(target_snapshot_for, label="target")
    clean_symbols = tuple(
        dict.fromkeys(
            str(symbol).strip().upper() for symbol in symbols if str(symbol).strip()
        )
    )
    if not clean_symbols:
        raise ValueError("Canary requires at least one symbol")
    now = clock or (lambda: datetime.now(timezone.utc))
    started_at = _utc(now(), label="clock")
    deadline_at = started_at + pd.Timedelta(seconds=float(timeout_seconds))
    monotonic_deadline = monotonic_clock() + float(timeout_seconds)
    last_error = "No verification attempt completed"

    while True:
        observed_at = _utc(now(), label="clock")
        try:
            return _inspect_target(
                root,
                target=target,
                symbols=clean_symbols,
                observed_at=observed_at,
                deadline_at=deadline_at,
            )
        except Exception as exc:
            last_error = f"{type(exc).__name__}: {exc}"
        remaining = monotonic_deadline - monotonic_clock()
        if remaining <= 0 or observed_at >= deadline_at:
            raise StrategyPricingCanaryError(
                "Canary deadline expired before every exact-target check passed; "
                f"target={target.isoformat()}; last_error={last_error}"
            )
        sleeper(min(float(poll_seconds), remaining))


def _inspect_target(
    root: Path,
    *,
    target: pd.Timestamp,
    symbols: tuple[str, ...],
    observed_at: pd.Timestamp,
    deadline_at: pd.Timestamp,
) -> Mapping[str, object]:
    readiness = read_bar_readiness(
        root,
        target_snapshot_for=target,
        required_symbols=symbols,
    )
    if readiness.target_snapshot_for != target or readiness.ready_at > observed_at:
        raise StrategyPricingCanaryError("Loop A readiness is future or mismatched")

    pricing = read_target_outcome(root, target_snapshot_for=target)
    if pricing.target_snapshot_for != target or pricing.published_at > observed_at:
        raise StrategyPricingCanaryError("Pricing authority is future or mismatched")
    if pricing.published_at > deadline_at:
        raise StrategyPricingCanaryError("Pricing authority arrived after canary deadline")
    if pricing.terminal_status not in {"PREDICTIONS_PUBLISHED", "MIXED_TERMINAL"}:
        raise StrategyPricingCanaryError(
            f"Pricing target is not a prediction authority: {pricing.terminal_status}"
        )
    predictions = pricing.predictions(include_proof=False)
    shadow = pricing.shadow_predictions()
    if predictions.empty or shadow.empty:
        raise StrategyPricingCanaryError(
            "Pricing target lacks baseline or Black-Scholes/BSGP sidecar rows"
        )
    prediction_targets = pd.to_datetime(
        predictions["target_snapshot_for"], utc=True, errors="coerce"
    )
    shadow_targets = pd.to_datetime(
        shadow["target_snapshot_for"], utc=True, errors="coerce"
    )
    if not prediction_targets.eq(target).all() or not shadow_targets.eq(target).all():
        raise StrategyPricingCanaryError("Pricing rows do not match the exact target")
    if predictions.get(
        "automated_action_allowed", pd.Series(True, index=predictions.index)
    ).fillna(True).astype(bool).any() or shadow.get(
        "automated_action_allowed", pd.Series(True, index=shadow.index)
    ).fillna(True).astype(bool).any():
        raise StrategyPricingCanaryError("Pricing artifact enables automated action")
    priced_symbols = set(shadow["symbol"].astype("string").str.upper())
    missing_priced_symbols = sorted(set(symbols).difference(priced_symbols))
    if missing_priced_symbols:
        raise StrategyPricingCanaryError(
            "Pricing sidecar does not cover symbol(s): "
            + ", ".join(missing_priced_symbols)
        )
    shadow_status = shadow["bsgp_shadow_status"].astype("string")
    pricing_source_counts = {
        "BSGP": int(shadow_status.eq("BSGP_SHADOW_READY").sum()),
        "BLACK_SCHOLES": int((~shadow_status.eq("BSGP_SHADOW_READY")).sum()),
    }

    strategy = read_current_strategy_publication(root)
    if _utc(strategy.receipt.get("published_at"), label="Strategy published_at") > observed_at:
        raise StrategyPricingCanaryError("Strategy publication is future-dated")
    candidate_path = strategy.run_directory / "strategy-candidates.parquet"
    candidates = pd.read_parquet(candidate_path)
    target_mask = candidates["legs_json"].map(
        lambda value: _candidate_target(value) == target
    )
    target_candidates = candidates.loc[target_mask].copy()
    if target_candidates.empty:
        raise StrategyPricingCanaryError(
            "Current Strategy publication has no candidates for the exact target"
        )
    checks = _strategy_checks(target_candidates)
    if checks["fully_priced_candidate_rows"] < 1:
        raise StrategyPricingCanaryError(
            "Strategy has not attached full exact-leg pricing coverage"
        )
    reports_path = strategy.run_directory / "strategy-model-reports.json"
    reports = _read_json(reports_path)
    health = reports.get("health_states")
    if not isinstance(health, Mapping):
        raise StrategyPricingCanaryError("Strategy health states are not published")

    return {
        "status": "PASS",
        "read_only": True,
        "orders_placed": 0,
        "target_snapshot_for": target.isoformat(),
        "observed_at": observed_at.isoformat(),
        "deadline_at": deadline_at.isoformat(),
        "symbols": list(symbols),
        "loop_a": {
            "status": "EXACT_ALL_SYMBOL_READINESS_VERIFIED",
            "ready_at": readiness.ready_at.isoformat(),
            "receipt": str(readiness.receipt_path),
        },
        "pricing": {
            "status": pricing.terminal_status,
            "published_at": pricing.published_at.isoformat(),
            "receipt": str(pricing.receipt_path),
            "baseline_rows": len(predictions),
            "sidecar_rows": len(shadow),
            "source_counts": pricing_source_counts,
        },
        "strategy": {
            "published_at": strategy.receipt.get("published_at"),
            "receipt": str(strategy.run_directory / "publication.json"),
            **checks,
            "health_states": dict(health),
        },
    }


def _strategy_checks(frame: pd.DataFrame) -> dict[str, int]:
    required = {
        "score_basis",
        "scenario_coverage_score",
        "raw_profit_probability",
        "calibrated_profit_probability",
        "decision_score",
        "pricing_status",
        "pricing_source",
        "pricing_leg_coverage",
        "pricing_missing_reason",
        "surface_quality_pass",
        "liquidity_policy_pass",
        "all_option_quotes_valid",
    }
    missing = sorted(required.difference(frame.columns))
    if missing:
        raise StrategyPricingCanaryError(
            "Strategy candidate visibility contract is missing: " + ", ".join(missing)
        )
    scenario = pd.to_numeric(frame["scenario_coverage_score"], errors="coerce")
    if not scenario.map(math.isfinite).all() or not scenario.between(0.0, 1.0).all():
        raise StrategyPricingCanaryError("Scenario coverage is invalid")
    heuristic = frame["score_basis"].eq(SCENARIO_COVERAGE_SCORE_BASIS)
    probabilities = frame[
        [
            "raw_profit_probability",
            "calibrated_profit_probability",
            "decision_score",
        ]
    ].apply(pd.to_numeric, errors="coerce")
    if not probabilities.loc[heuristic].isna().all().all():
        raise StrategyPricingCanaryError(
            "A heuristic scenario value is masquerading as model probability"
        )
    fitted = ~heuristic
    allowed_fitted = frame["score_basis"].isin(
        {
            BSGP_CALIBRATED_MODEL_SCORE_BASIS,
            BLACK_SCHOLES_CALIBRATED_MODEL_SCORE_BASIS,
        }
    )
    if not allowed_fitted.loc[fitted].all() or not probabilities.loc[
        fitted
    ].notna().all().all():
        raise StrategyPricingCanaryError("Calibrated candidate score contract is invalid")
    coverage = pd.to_numeric(frame["pricing_leg_coverage"], errors="coerce")
    pricing_source = frame["pricing_source"].astype("string").str.upper()
    fully_priced = coverage.ge(1.0 - 1e-12) & pricing_source.isin(
        {"BSGP", "BLACK_SCHOLES"}
    )
    if frame["pricing_status"].astype("string").str.strip().eq("").any():
        raise StrategyPricingCanaryError("Pricing status is not visible")
    for column in (
        "surface_quality_pass",
        "liquidity_policy_pass",
        "all_option_quotes_valid",
    ):
        if frame[column].isna().any():
            raise StrategyPricingCanaryError(f"Quality state is not visible: {column}")
    quality_pass = (
        frame["surface_quality_pass"].astype(bool)
        & frame["liquidity_policy_pass"].astype(bool)
        & frame["all_option_quotes_valid"].astype(bool)
    )
    hundred_percent_scenario = heuristic & scenario.ge(1.0 - 1e-12)
    return {
        "candidate_rows": len(frame),
        "fully_priced_candidate_rows": int(fully_priced.sum()),
        "calibrated_candidate_rows": int(fitted.sum()),
        "heuristic_candidate_rows": int(heuristic.sum()),
        "all_positive_scenario_rows_without_probability": int(
            hundred_percent_scenario.sum()
        ),
        "quality_passing_rows": int(quality_pass.sum()),
        "quality_warning_rows": int((~quality_pass).sum()),
    }


def _candidate_target(value: object) -> pd.Timestamp | None:
    try:
        legs = json.loads(str(value))
    except (TypeError, ValueError, json.JSONDecodeError):
        return None
    if not isinstance(legs, list):
        return None
    targets = {
        _utc(leg.get("target_snapshot_for"), label="candidate leg target")
        for leg in legs
        if isinstance(leg, Mapping)
        and str(leg.get("asset", "")).upper() == "OPTION"
        and leg.get("target_snapshot_for") is not None
    }
    return next(iter(targets)) if len(targets) == 1 else None


def _read_json(path: Path) -> Mapping[str, object]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise StrategyPricingCanaryError(f"Canary artifact is unreadable: {path}") from exc
    if not isinstance(payload, Mapping):
        raise StrategyPricingCanaryError(f"Canary artifact is malformed: {path}")
    return payload


def _utc(value: object, *, label: str) -> pd.Timestamp:
    timestamp = pd.to_datetime(value, utc=True, errors="coerce")
    if pd.isna(timestamp):
        raise StrategyPricingCanaryError(f"Invalid {label}")
    return pd.Timestamp(timestamp)


def _watchlist(path: Path) -> tuple[str, ...]:
    if not path.is_file():
        raise FileNotFoundError(f"Canary watchlist does not exist: {path}")
    return tuple(
        dict.fromkeys(
            line.split("#", 1)[0].strip().upper()
            for line in path.read_text(encoding="utf-8").splitlines()
            if line.split("#", 1)[0].strip()
        )
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Read-only bounded canary for one regular-session Loop A/Pricing/Strategy target."
        )
    )
    parser.add_argument("--target", required=True, help="Exact UTC quarter-hour target.")
    parser.add_argument("--watchlist", type=Path, default=DEFAULT_WATCHLIST)
    parser.add_argument("--timeout-seconds", type=float, default=600.0)
    parser.add_argument("--poll-seconds", type=float, default=5.0)
    datastore = parser.add_mutually_exclusive_group()
    datastore.add_argument(
        "--datastore-target",
        choices=tuple(DATASTORE_TARGETS),
        default=None,
    )
    datastore.add_argument("--datastore", type=Path, default=None)
    args = parser.parse_args(argv)
    try:
        report = run_canary(
            resolve_datastore_dir(root_dir=args.datastore, target=args.datastore_target),
            target_snapshot_for=args.target,
            symbols=_watchlist(args.watchlist),
            timeout_seconds=args.timeout_seconds,
            poll_seconds=args.poll_seconds,
        )
    except Exception as exc:
        print(
            json.dumps(
                {"status": "FAIL", "error": f"{type(exc).__name__}: {exc}"},
                indent=2,
                sort_keys=True,
            ),
            file=sys.stderr,
        )
        return 1
    print(json.dumps(report, indent=2, sort_keys=True, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = ["StrategyPricingCanaryError", "run_canary"]
