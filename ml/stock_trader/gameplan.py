from __future__ import annotations

import os
from pathlib import Path
from typing import Mapping
from zoneinfo import ZoneInfo

import pandas as pd

from ml.artifacts import file_checksum
from ml.nightly_gameplan import (
    ASSUMED_ROUND_TRIP_COST,
    EXECUTION_AUTHORITY,
    read_current_gameplan,
)
from ml.stock_trader.contracts import (
    ActivationIntent,
    PredictionSignal,
    STOCK_TRADER_SYMBOLS,
    canonical_sha256,
    finite,
    utc,
)
from ml.stock_trader.control import read_activation_intent
from ml.stock_trader.handoff import consumed_live_prediction_ids
from ml.stock_trader.session import checkpoint_session_for_target


GAMEPLAN_STOCK_ENTRY_GRACE_SECONDS = 5 * 60
GAMEPLAN_STOCK_TRADER_SOURCE = "immutable-nightly-gameplan"
GAMEPLAN_STOCK_ACTIVATION_RELATIVE_PATH = Path(
    "controls/gameplan-stock-trader/operator-intent.txt"
)
GAMEPLAN_STOCK_ACTIVATION_KEY = "CONFIRM_GAMEPLAN_STOCK_TRADING"
GAMEPLAN_TIMEZONE = ZoneInfo("America/Los_Angeles")
_EXPECTED_COUNTS = {"1h": 14, "4h": 4, "1d": 5, "1w": 1}
_LIVE_PRIMARY_HORIZONS = ("1h", "4h")
_FOUR_HOUR_ACTION_HOURS = frozenset((4, 8, 12))


def _entry_deadline(action_start: pd.Timestamp) -> pd.Timestamp:
    """Return the live-entry deadline for a frozen action boundary.

    Schwab's stock session changes from regular to PM extended hours at 13:00
    Pacific. The broker contract deliberately treats that short transition as
    closed, so only that boundary receives ten minutes instead of five. It is
    still the 13:00 prediction generation and cannot be replayed later.
    """

    local = utc(action_start).tz_convert(GAMEPLAN_TIMEZONE)
    grace_seconds = (
        10 * 60 if local.hour == 13 else GAMEPLAN_STOCK_ENTRY_GRACE_SECONDS
    )
    return utc(action_start) + pd.Timedelta(seconds=grace_seconds)


def gameplan_stock_activation_path(datastore_root: Path) -> Path:
    return (
        Path(datastore_root).resolve()
        / GAMEPLAN_STOCK_ACTIVATION_RELATIVE_PATH
    )


def read_gameplan_stock_activation_intent(
    datastore_root: Path,
) -> ActivationIntent:
    """Require both the legacy broker switch and a gameplan-specific switch."""

    root = Path(datastore_root).resolve()
    broker_intent = read_activation_intent(root)
    gameplan_path = gameplan_stock_activation_path(root)
    gameplan_active, gameplan_reason, gameplan_checksum = _read_gameplan_switch(
        gameplan_path
    )
    combined_checksum = canonical_sha256(
        {
            "broker_activation_checksum_sha256": broker_intent.checksum_sha256,
            "gameplan_activation_checksum_sha256": gameplan_checksum,
        }
    )
    active = broker_intent.active and gameplan_active
    if not broker_intent.active:
        reason = f"BROKER_{broker_intent.reason}"
    elif not gameplan_active:
        reason = gameplan_reason
    else:
        reason = "BOTH_STOCK_TRADING_SWITCHES_TRUE"
    return ActivationIntent(
        active=active,
        status="ACTIVE" if active else "INACTIVE",
        reason=reason,
        path=f"{broker_intent.path};{gameplan_path}",
        checksum_sha256=combined_checksum,
    )


def write_gameplan_stock_activation_intent(
    datastore_root: Path,
    *,
    active: bool,
) -> Path:
    if not isinstance(active, bool):
        raise TypeError("active must be an explicit boolean")
    target = gameplan_stock_activation_path(datastore_root)
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_suffix(target.suffix + f".tmp-{os.getpid()}")
    try:
        temporary.write_text(
            f"{GAMEPLAN_STOCK_ACTIVATION_KEY}="
            f"{'TRUE' if active else 'FALSE'}\n",
            encoding="utf-8",
        )
        temporary.replace(target)
    finally:
        temporary.unlink(missing_ok=True)
    return target


def load_current_gameplan_prediction_signals(
    datastore_root: Path,
    *,
    as_of: object,
    target_horizon: object | None = None,
) -> tuple[dict[str, PredictionSignal], tuple[Path, ...]]:
    """Translate the current frozen 1h route into stock-trader signals.

    The live stock engine receives the current forward 1h prediction and only
    promoted longer-horizon context.  This function does not grant order
    authority; the two independent operator switches do that at execution time.
    """

    horizon = _normalize_primary_horizon(target_horizon)
    root = Path(datastore_root).resolve()
    timestamp = utc(as_of)
    local = timestamp.tz_convert(GAMEPLAN_TIMEZONE)
    publication = read_current_gameplan(root)
    action_date = str(publication.receipt.get("action_date") or "")
    if action_date != local.date().isoformat():
        raise ValueError(
            "Current gameplan is not for this stock-trading action date: "
            f"plan={action_date or 'missing'}; now={local.date()}"
        )

    forecasts_path = publication.run_directory / "forecasts.parquet"
    frame = pd.read_parquet(forecasts_path)
    normalized = _validated_forecasts(frame)
    source_files = _gameplan_source_files(root, publication.run_directory)
    source_fingerprint = canonical_sha256(
        {
            "run": publication.run_directory.relative_to(root).as_posix(),
            "files": {
                source.relative_to(root).as_posix(): file_checksum(source)
                for source in source_files
            },
        }
    )

    action_start = local.floor("h").tz_convert("UTC")
    if not 4 <= local.hour < 17:
        return {}, source_files
    deadline = _entry_deadline(action_start)
    if horizon == "4h" and local.hour not in _FOUR_HOUR_ACTION_HOURS:
        return {}, source_files
    primary_rows = normalized.loc[
        normalized["model_group"].eq(horizon)
        & normalized["target_window_start"].eq(action_start)
    ].copy()
    if set(primary_rows["symbol"].astype(str)) != set(STOCK_TRADER_SYMBOLS):
        raise ValueError(
            f"Nightly gameplan does not contain one forward {horizon} route "
            "for every stock-trader symbol at this action hour"
        )

    signals: dict[str, PredictionSignal] = {}
    for symbol in STOCK_TRADER_SYMBOLS:
        row = primary_rows.loc[primary_rows["symbol"].eq(symbol)].iloc[0]
        probability = finite(row.get("calibrated_probability"))
        if probability is None or not 0.0 <= probability <= 1.0:
            raise ValueError(f"{symbol} gameplan probability is invalid")
        promoted = str(row.get("model_status") or "").upper() == "PROMOTED"
        direction = str(row.get("direction") or "").upper()
        if not promoted or direction not in {"BULLISH", "BEARISH"}:
            continue
        direction_probability_mismatch = (
            direction == "BULLISH" and probability < 0.55
        ) or (
            direction == "BEARISH" and probability > 0.45
        )
        if direction_probability_mismatch:
            raise ValueError(f"{symbol} gameplan direction and probability disagree")
        horizon_probabilities = {horizon: probability}
        symbol_rows = normalized.loc[normalized["symbol"].eq(symbol)]
        for context_horizon in ("1h", "4h", "1d", "1w"):
            if context_horizon == horizon:
                continue
            context = _context_probability(
                symbol_rows,
                horizon=context_horizon,
                action_start=action_start,
            )
            if context is not None:
                horizon_probabilities[context_horizon] = context
        if horizon == "1h" and _probabilities_conflict(
            probability,
            horizon_probabilities.get("4h"),
        ):
            # The hourly route controls entry timing.  A simultaneously active
            # four-hour route is confirmation, never an independent order that
            # can fight it.  Opposite actionable directions therefore veto a
            # new entry for this symbol at the shared boundary.
            continue
        signals[symbol] = PredictionSignal(
            symbol=symbol,
            primary_horizon=horizon,
            prediction_id=str(row["id"]),
            decision_timestamp=utc(row["decision_timestamp"]).isoformat(),
            target_window_start=utc(row["target_window_start"]).isoformat(),
            target_window_end=utc(row["target_window_end"]).isoformat(),
            actionable_until=deadline.isoformat(),
            prediction_created_at=utc(row["frozen_at"]).isoformat(),
            calibrated_probability=probability,
            assumed_round_trip_cost=ASSUMED_ROUND_TRIP_COST,
            horizon_probabilities=horizon_probabilities,
            model_name=str(row.get("model_family") or ""),
            model_version=str(row.get("model_artifact") or ""),
            source_fingerprint=source_fingerprint,
            checkpoint_session=checkpoint_session_for_target(action_start),
            target_definition_version=str(
                row.get("target_contract_version") or ""
            ),
        )
    return signals, source_files


def gate_gameplan_execution_signals(
    root: Path,
    signals: Mapping[str, PredictionSignal],
    *,
    as_of: object,
    maximum_target_lead_seconds: float,
    target_horizon: object | None = None,
    expected_target_window_start: object | None = None,
) -> tuple[dict[str, PredictionSignal], dict[str, object], str, str | None]:
    """Apply action-slot, deadline, and exact-once gates to gameplan signals."""

    horizon = _normalize_primary_horizon(target_horizon)
    timestamp = utc(as_of)
    local = timestamp.tz_convert(GAMEPLAN_TIMEZONE)
    action_hour_eligible = 4 <= local.hour < 17 and (
        horizon == "1h" or local.hour in _FOUR_HOUR_ACTION_HOURS
    )
    target = local.floor("h").tz_convert("UTC") if action_hour_eligible else None
    if expected_target_window_start is not None:
        expected = utc(expected_target_window_start)
        if target is None or expected != target:
            raise ValueError("Explicit target does not match the gameplan action slot")

    matching: dict[str, PredictionSignal] = {}
    if target is not None:
        for symbol in STOCK_TRADER_SYMBOLS:
            signal = signals.get(symbol)
            if signal is None or signal.primary_horizon != horizon:
                continue
            try:
                signal_target = utc(signal.target_window_start)
                actionable_until = utc(signal.actionable_until)
            except (TypeError, ValueError):
                continue
            if signal_target == target and actionable_until > timestamp:
                matching[symbol] = signal

    consumed = consumed_live_prediction_ids(Path(root).resolve())
    consumed_seen = {
        signal.prediction_id
        for signal in matching.values()
        if signal.prediction_id in consumed
    }
    available = {
        symbol: signal
        for symbol, signal in matching.items()
        if signal.prediction_id not in consumed
    }
    deadline = _entry_deadline(target) if target is not None else None
    if target is None:
        status = "NO_GAMEPLAN_STOCK_ACTION_SLOT"
        available = {}
    elif timestamp < target:
        status = "GAMEPLAN_STOCK_ACTION_SLOT_NOT_STARTED"
        available = {}
    elif deadline is not None and timestamp >= deadline:
        status = "GAMEPLAN_STOCK_ENTRY_GRACE_EXPIRED"
        available = {}
    elif available:
        status = "GAMEPLAN_STOCK_ACTIONABLE_RECEIPT_VALIDATED"
    elif consumed_seen:
        status = "PREDICTION_GENERATION_ALREADY_CONSUMED"
    else:
        status = "NO_PROMOTED_GAMEPLAN_DIRECTION_EDGE"

    target_iso = target.isoformat() if target is not None else None
    metadata: dict[str, object] = {
        "schema_version": "gameplan-stock-prediction-gate-v1",
        "source": GAMEPLAN_STOCK_TRADER_SOURCE,
        "status": status,
        "started_at": timestamp.isoformat(),
        "completed_at": timestamp.isoformat(),
        "expected_target_window_start": target_iso,
        "entry_deadline": deadline.isoformat() if deadline is not None else None,
        "target_horizon": horizon,
        "maximum_target_lead_seconds": float(maximum_target_lead_seconds),
        "selected_prediction_ids": [
            available[symbol].prediction_id
            for symbol in STOCK_TRADER_SYMBOLS
            if symbol in available
        ],
        "consumed_prediction_ids": sorted(consumed_seen),
        "missing_symbols": [
            symbol for symbol in STOCK_TRADER_SYMBOLS if symbol not in available
        ],
        "fallback_used": False,
    }
    return available, metadata, status, target_iso


def gameplan_prediction_pointer_sources(root: Path) -> tuple[Path, ...]:
    datastore = Path(root).resolve()
    pointer = datastore / "ml" / "nightly-gameplan-latest" / "run.json"
    return (pointer,) if pointer.is_file() else ()


def _validated_forecasts(frame: pd.DataFrame) -> pd.DataFrame:
    required = {
        "id",
        "symbol",
        "model_group",
        "route",
        "decision_timestamp",
        "information_available_at",
        "target_window_start",
        "target_window_end",
        "calibrated_probability",
        "model_family",
        "model_status",
        "direction",
        "frozen_at",
        "action_date",
        "target_contract_version",
        "execution_authority",
        "broker_orders_enabled",
    }
    missing = sorted(required.difference(frame.columns))
    if missing:
        raise ValueError(
            "Nightly gameplan forecasts are missing columns: " + ", ".join(missing)
        )
    if frame.empty or frame["id"].duplicated().any():
        raise ValueError("Nightly gameplan forecast grid is empty or duplicated")
    data = frame.copy()
    data["symbol"] = data["symbol"].astype("string").str.strip().str.upper()
    data["model_group"] = (
        data["model_group"].astype("string").str.strip().str.lower()
    )
    for column in (
        "decision_timestamp",
        "information_available_at",
        "target_window_start",
        "target_window_end",
        "frozen_at",
    ):
        data[column] = pd.to_datetime(data[column], utc=True, errors="coerce")
    if data[
        [
            "decision_timestamp",
            "information_available_at",
            "target_window_start",
            "target_window_end",
            "frozen_at",
        ]
    ].isna().any().any():
        raise ValueError("Nightly gameplan contains an invalid timestamp")
    probability = pd.to_numeric(data["calibrated_probability"], errors="coerce")
    if probability.isna().any() or not probability.between(0.0, 1.0).all():
        raise ValueError("Nightly gameplan contains an invalid probability")
    data["calibrated_probability"] = probability
    if set(data["symbol"].astype(str)) != set(STOCK_TRADER_SYMBOLS):
        raise ValueError("Nightly gameplan stock universe differs from the trader")
    for symbol, rows in data.groupby("symbol", sort=False):
        counts = rows["model_group"].value_counts().to_dict()
        if any(int(counts.get(group, 0)) != count for group, count in _EXPECTED_COUNTS.items()):
            raise ValueError(f"{symbol} nightly gameplan does not have a 14/4/5/1 grid")
    if not data["execution_authority"].astype("string").eq(
        EXECUTION_AUTHORITY
    ).all():
        raise ValueError("Nightly forecasts have an unexpected source authority")
    if data["broker_orders_enabled"].astype("boolean").fillna(True).any():
        raise ValueError("Nightly forecasts unexpectedly claim broker authority")
    return data


def _context_probability(
    rows: pd.DataFrame,
    *,
    horizon: str,
    action_start: pd.Timestamp,
) -> float | None:
    candidates = rows.loc[
        rows["model_group"].eq(horizon)
        & rows["model_status"].astype("string").str.upper().eq("PROMOTED")
    ].copy()
    if candidates.empty:
        return None
    if horizon in {"1h", "4h"}:
        candidates = candidates.loc[
            candidates["target_window_start"].le(action_start)
            & candidates["target_window_end"].gt(action_start)
        ]
    else:
        candidates = candidates.sort_values("target_window_start", kind="stable").head(1)
    if candidates.empty:
        return None
    probability = finite(candidates.iloc[0].get("calibrated_probability"))
    return probability if probability is not None and 0.0 <= probability <= 1.0 else None


def _normalize_primary_horizon(value: object | None) -> str:
    horizon = "1h" if value is None else str(value).strip().lower()
    if horizon not in _LIVE_PRIMARY_HORIZONS:
        raise ValueError(
            "Nightly gameplan stock execution horizon must be 1h or 4h"
        )
    return horizon


def _probabilities_conflict(
    primary_probability: float,
    confirming_probability: float | None,
) -> bool:
    if confirming_probability is None:
        return False
    primary_bullish = primary_probability >= 0.55
    primary_bearish = primary_probability <= 0.45
    confirming_bullish = confirming_probability >= 0.55
    confirming_bearish = confirming_probability <= 0.45
    return (primary_bullish and confirming_bearish) or (
        primary_bearish and confirming_bullish
    )


def _gameplan_source_files(root: Path, run: Path) -> tuple[Path, ...]:
    candidates = (
        root / "ml" / "nightly-gameplan-latest" / "run.json",
        run / "manifest.json",
        run / "receipt.json",
        run / "gameplan.json",
        run / "forecasts.parquet",
        run / "option-strategy-intents.parquet",
    )
    missing = [path for path in candidates if not path.is_file()]
    if missing:
        raise ValueError(
            "Nightly gameplan source files are incomplete: "
            + ", ".join(str(path) for path in missing)
        )
    return candidates


def _read_gameplan_switch(path: Path) -> tuple[bool, str, str | None]:
    if not path.is_file():
        return False, "GAMEPLAN_STOCK_INTENT_MISSING", None
    checksum = file_checksum(path)
    try:
        lines = [
            line.strip()
            for line in path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
    except OSError:
        return False, "GAMEPLAN_STOCK_INTENT_UNREADABLE", checksum
    if len(lines) != 1 or "=" not in lines[0]:
        return False, "GAMEPLAN_STOCK_INTENT_MALFORMED", checksum
    key, value = (part.strip() for part in lines[0].split("=", 1))
    if key != GAMEPLAN_STOCK_ACTIVATION_KEY or value not in {"TRUE", "FALSE"}:
        return False, "GAMEPLAN_STOCK_INTENT_MALFORMED", checksum
    return (
        value == "TRUE",
        (
            "GAMEPLAN_STOCK_INTENT_TRUE"
            if value == "TRUE"
            else "GAMEPLAN_STOCK_INTENT_FALSE"
        ),
        checksum,
    )


__all__ = [
    "GAMEPLAN_STOCK_ACTIVATION_KEY",
    "GAMEPLAN_STOCK_ACTIVATION_RELATIVE_PATH",
    "GAMEPLAN_STOCK_ENTRY_GRACE_SECONDS",
    "GAMEPLAN_STOCK_TRADER_SOURCE",
    "gameplan_prediction_pointer_sources",
    "gameplan_stock_activation_path",
    "gate_gameplan_execution_signals",
    "load_current_gameplan_prediction_signals",
    "read_gameplan_stock_activation_intent",
    "write_gameplan_stock_activation_intent",
]
