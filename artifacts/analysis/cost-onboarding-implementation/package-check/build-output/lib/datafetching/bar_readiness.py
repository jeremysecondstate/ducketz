from __future__ import annotations

import hashlib
import json
import math
import os
import tempfile
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Mapping, Sequence

import pandas as pd

from datafetching.decision_time import (
    DecisionClock,
    completed_bar_clock_for_target,
    completed_bar_close,
    expected_quarter_hour_target,
    is_eligible_option_target,
)
from datafetching.layout import safe_token
from ml.artifacts import file_checksum


BAR_READINESS_VERSION = "loop-a-bar-readiness-v1"
BAR_READINESS_RECEIPT_VERSION = "loop-a-bar-readiness-receipt-v1"
BAR_READINESS_POINTER_VERSION = "loop-a-bar-readiness-pointer-v1"
FROZEN_BAR_READINESS_VERSION = "loop-a-bar-readiness-v2"
FROZEN_BAR_READINESS_RECEIPT_VERSION = "loop-a-bar-readiness-receipt-v2"


class BarReadinessError(RuntimeError):
    """Loop A's immutable target-bar readiness evidence failed closed."""


@dataclass(frozen=True)
class BarReadiness:
    target_snapshot_for: pd.Timestamp
    ready_at: pd.Timestamp
    loop_a_generation: str
    symbols: tuple[str, ...]
    bars: Mapping[str, Mapping[str, object]]
    directory: Path
    readiness_path: Path
    receipt_path: Path
    receipt_checksum_sha256: str
    coordination: Mapping[str, object]

    @property
    def evidence_files(self) -> tuple[Path, ...]:
        frozen = tuple(
            Path(str(raw["source_file"]))
            for raw in self.bars.values()
            if isinstance(raw, Mapping)
            and raw.get("source_file_checksum_sha256")
        )
        return tuple(dict.fromkeys((*frozen, self.readiness_path, self.receipt_path)))

    def decision_clock(self, symbol: str) -> DecisionClock:
        clean = str(symbol).strip().upper()
        raw = self.bars.get(clean)
        if not isinstance(raw, Mapping):
            raise BarReadinessError(f"Bar readiness does not cover {clean}")
        return DecisionClock(
            decision_timestamp=self.target_snapshot_for,
            bar_timestamp=_utc(raw.get("bar_timestamp"), "bar_timestamp"),
            provider=str(raw.get("provider", "")),
            timeframe=str(raw.get("timeframe", "")),
            source_file=Path(str(raw.get("source_file", ""))),
        )

    def close(self, symbol: str) -> float:
        raw = self.bars.get(str(symbol).strip().upper())
        if not isinstance(raw, Mapping):
            raise BarReadinessError(f"Bar readiness does not cover {symbol}")
        value = float(raw.get("close", float("nan")))
        if not pd.notna(value) or not float("-inf") < value < float("inf") or value <= 0:
            raise BarReadinessError(f"Bar readiness close is invalid for {symbol}")
        return value


@dataclass(frozen=True)
class BarReadinessWait:
    status: str
    observed_at: pd.Timestamp
    deadline_at: pd.Timestamp
    readiness: BarReadiness | None = None
    detail: str = ""


def publish_bar_readiness(
    datastore_root: Path,
    *,
    target_snapshot_for: object,
    symbols: Sequence[str],
    loop_a_generation: str,
    as_of: object | None = None,
    clock: Callable[[], object] | None = None,
) -> BarReadiness:
    """Atomically freeze one coherent, all-symbol target-bar boundary."""

    root = Path(datastore_root).resolve()
    target = _utc(target_snapshot_for, "target_snapshot_for")
    if target != expected_quarter_hour_target(target):
        raise ValueError("Bar readiness target must be a quarter-hour boundary")
    if not is_eligible_option_target(target):
        raise ValueError(
            "Bar readiness target must be inside the regular XNYS option-market window"
        )
    clean_symbols = tuple(
        dict.fromkeys(str(value).strip().upper() for value in symbols if str(value).strip())
    )
    if not clean_symbols:
        raise ValueError("Bar readiness requires at least one symbol")
    generation = str(loop_a_generation).strip()
    if not generation:
        raise ValueError("Bar readiness requires a Loop A generation")
    observed = _utc(as_of if as_of is not None else _now(), "as_of")
    destination = _directory(root, target)
    if destination.is_dir():
        existing = read_bar_readiness(root, target_snapshot_for=target)
        if existing.symbols != clean_symbols:
            raise BarReadinessError(
                "Existing target readiness has a different all-symbol scope"
            )
        _publish_pointer(root, existing)
        return existing

    bars: dict[str, dict[str, object]] = {}
    for symbol in clean_symbols:
        decision = completed_bar_clock_for_target(
            root,
            symbol=symbol,
            target_snapshot_for=target,
            as_of=observed,
        )
        close = completed_bar_close(decision)
        row = {
            "symbol": symbol,
            "target_snapshot_for": target.isoformat(),
            "bar_timestamp": decision.bar_timestamp.isoformat(),
            "provider": decision.provider,
            "timeframe": decision.timeframe,
            "source_file": str(decision.source_file.resolve()),
            "close": close,
        }
        row["row_checksum_sha256"] = _semantic_checksum(row)
        bars[symbol] = row

    ready_at = _utc((clock or _now)(), "ready_at")
    parent = destination.parent
    parent.mkdir(parents=True, exist_ok=True)
    staging = Path(
        tempfile.mkdtemp(
            prefix=f".{destination.name}.tmp-{os.getpid()}-",
            dir=parent,
        )
    )
    readiness = {
        "schema_version": BAR_READINESS_VERSION,
        "target_snapshot_for": target.isoformat(),
        "ready_at": ready_at.isoformat(),
        "loop_a_generation": generation,
        "symbols": list(clean_symbols),
        "bars": bars,
    }
    readiness_path = staging / "readiness.json"
    receipt_path = staging / "receipt.json"
    try:
        _write_json(readiness_path, readiness)
        receipt = {
            "schema_version": BAR_READINESS_RECEIPT_VERSION,
            "target_snapshot_for": target.isoformat(),
            "ready_at": ready_at.isoformat(),
            "loop_a_generation": generation,
            "run_path": destination.relative_to(root).as_posix(),
            "readiness_checksum_sha256": file_checksum(readiness_path),
            "symbol_count": len(clean_symbols),
        }
        _write_json(receipt_path, receipt)
        staging.replace(destination)
    except BaseException:
        # An interrupted private directory has no authority and is ignored.
        raise
    published = read_bar_readiness(root, target_snapshot_for=target)
    _publish_pointer(root, published)
    return published


def publish_frozen_bar_readiness(
    datastore_root: Path,
    *,
    target_snapshot_for: object,
    symbols: Sequence[str],
    loop_a_generation: str,
    exact_bars: Mapping[str, Mapping[str, object]],
    coordination: Mapping[str, object],
    clock: Callable[[], object] | None = None,
) -> BarReadiness:
    """Atomically publish exact provider rows as immutable target evidence."""

    root = Path(datastore_root).resolve()
    target = _utc(target_snapshot_for, "target_snapshot_for")
    if target != expected_quarter_hour_target(target):
        raise ValueError("Bar readiness target must be a quarter-hour boundary")
    if not is_eligible_option_target(target):
        raise ValueError(
            "Bar readiness target must be inside the regular XNYS option-market window"
        )
    clean_symbols = tuple(
        dict.fromkeys(str(value).strip().upper() for value in symbols if str(value).strip())
    )
    if not clean_symbols:
        raise ValueError("Bar readiness requires at least one symbol")
    if set(exact_bars) != set(clean_symbols):
        raise BarReadinessError("Frozen readiness bars do not match the all-symbol scope")
    generation = str(loop_a_generation).strip()
    if not generation:
        raise ValueError("Bar readiness requires a Loop A generation")
    destination = _directory(root, target)
    if destination.is_dir():
        existing = read_bar_readiness(root, target_snapshot_for=target)
        if existing.symbols != clean_symbols:
            raise BarReadinessError(
                "Existing target readiness has a different all-symbol scope"
            )
        _publish_pointer(root, existing)
        return existing

    ready_at = _utc((clock or _now)(), "ready_at")
    parent = destination.parent
    parent.mkdir(parents=True, exist_ok=True)
    staging = Path(
        tempfile.mkdtemp(
            prefix=f".{destination.name}.tmp-{os.getpid()}-",
            dir=parent,
        )
    )
    bars_directory = staging / "bars"
    bars_directory.mkdir()
    bars: dict[str, dict[str, object]] = {}
    try:
        for symbol in clean_symbols:
            raw = exact_bars[symbol]
            normalized = _validated_frozen_bar(raw, symbol=symbol, target=target)
            staged_bar_path = bars_directory / f"{safe_token(symbol)}.parquet"
            final_bar_path = destination / "bars" / staged_bar_path.name
            pd.DataFrame(
                [
                    {
                        "timestamp": normalized["timestamp"],
                        "open": normalized["open"],
                        "high": normalized["high"],
                        "low": normalized["low"],
                        "close": normalized["close"],
                        "volume": normalized["volume"],
                    }
                ]
            ).to_parquet(staged_bar_path, index=False)
            row = {
                "symbol": symbol,
                "target_snapshot_for": target.isoformat(),
                "bar_timestamp": normalized["timestamp"].isoformat(),
                "provider": normalized["provider"],
                "dataset": normalized["dataset"],
                "schema": normalized["schema"],
                "timeframe": normalized["timeframe"],
                "source_file": str(final_bar_path.resolve()),
                "source_file_checksum_sha256": file_checksum(staged_bar_path),
                "open": normalized["open"],
                "high": normalized["high"],
                "low": normalized["low"],
                "close": normalized["close"],
                "volume": normalized["volume"],
            }
            row["row_checksum_sha256"] = _semantic_checksum(row)
            bars[symbol] = row

        readiness = {
            "schema_version": FROZEN_BAR_READINESS_VERSION,
            "target_snapshot_for": target.isoformat(),
            "ready_at": ready_at.isoformat(),
            "loop_a_generation": generation,
            "symbols": list(clean_symbols),
            "bars": bars,
            "coordination": dict(coordination),
        }
        readiness_path = staging / "readiness.json"
        receipt_path = staging / "receipt.json"
        _write_json(readiness_path, readiness)
        receipt = {
            "schema_version": FROZEN_BAR_READINESS_RECEIPT_VERSION,
            "target_snapshot_for": target.isoformat(),
            "ready_at": ready_at.isoformat(),
            "loop_a_generation": generation,
            "run_path": destination.relative_to(root).as_posix(),
            "readiness_checksum_sha256": file_checksum(readiness_path),
            "symbol_count": len(clean_symbols),
            "coordination": dict(coordination),
        }
        _write_json(receipt_path, receipt)
        staging.replace(destination)
    except BaseException:
        # An interrupted private directory has no authority and is ignored.
        raise
    published = read_bar_readiness(root, target_snapshot_for=target)
    _publish_pointer(root, published)
    return published


def read_bar_readiness(
    datastore_root: Path,
    *,
    target_snapshot_for: object,
    required_symbols: Sequence[str] | None = None,
) -> BarReadiness:
    root = Path(datastore_root).resolve()
    target = _utc(target_snapshot_for, "target_snapshot_for")
    directory = _directory(root, target)
    readiness_path = directory / "readiness.json"
    receipt_path = directory / "receipt.json"
    try:
        readiness = json.loads(readiness_path.read_text(encoding="utf-8"))
        receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    except (OSError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise BarReadinessError(
            f"No verified Loop A bar-readiness receipt exists for {target.isoformat()}"
        ) from exc
    if not isinstance(readiness, Mapping) or not isinstance(receipt, Mapping):
        raise BarReadinessError("Bar-readiness metadata is malformed")
    symbols = _symbols(readiness.get("symbols"))
    bars = readiness.get("bars")
    if not isinstance(bars, Mapping) or set(bars) != set(symbols):
        raise BarReadinessError("Bar-readiness symbol inventory is incoherent")
    ready_at = _utc(readiness.get("ready_at"), "ready_at")
    expected_path = directory.relative_to(root).as_posix()
    readiness_version = readiness.get("schema_version")
    receipt_version = receipt.get("schema_version")
    version_pair_valid = (readiness_version, receipt_version) in {
        (BAR_READINESS_VERSION, BAR_READINESS_RECEIPT_VERSION),
        (FROZEN_BAR_READINESS_VERSION, FROZEN_BAR_READINESS_RECEIPT_VERSION),
    }
    if (
        not version_pair_valid
        or _utc(readiness.get("target_snapshot_for"), "readiness target") != target
        or _utc(receipt.get("target_snapshot_for"), "receipt target") != target
        or _utc(receipt.get("ready_at"), "receipt ready_at") != ready_at
        or receipt.get("loop_a_generation") != readiness.get("loop_a_generation")
        or receipt.get("run_path") != expected_path
        or receipt.get("readiness_checksum_sha256") != file_checksum(readiness_path)
        or int(receipt.get("symbol_count", -1)) != len(symbols)
        or (
            readiness_version == FROZEN_BAR_READINESS_VERSION
            and (
                not isinstance(readiness.get("coordination"), Mapping)
                or not isinstance(receipt.get("coordination"), Mapping)
                or dict(readiness["coordination"]) != dict(receipt["coordination"])
            )
        )
    ):
        raise BarReadinessError("Bar-readiness receipt verification failed")
    for symbol, raw in bars.items():
        if not isinstance(raw, Mapping):
            raise BarReadinessError(f"Bar readiness for {symbol} is malformed")
        semantic = {key: value for key, value in raw.items() if key != "row_checksum_sha256"}
        if (
            str(raw.get("symbol", "")).strip().upper() != symbol
            or _utc(raw.get("target_snapshot_for"), "bar target") != target
            or raw.get("row_checksum_sha256") != _semantic_checksum(semantic)
        ):
            raise BarReadinessError(f"Bar readiness row verification failed for {symbol}")
        if readiness_version == FROZEN_BAR_READINESS_VERSION:
            _verify_frozen_bar_evidence(
                directory,
                symbol=symbol,
                target=target,
                raw=raw,
            )
    required = _symbols(required_symbols) if required_symbols is not None else symbols
    if any(symbol not in bars for symbol in required):
        raise BarReadinessError("Bar readiness does not cover the required symbol scope")
    return BarReadiness(
        target_snapshot_for=target,
        ready_at=ready_at,
        loop_a_generation=str(readiness.get("loop_a_generation", "")),
        symbols=symbols,
        bars={str(key): dict(value) for key, value in bars.items()},
        directory=directory,
        readiness_path=readiness_path,
        receipt_path=receipt_path,
        receipt_checksum_sha256=file_checksum(receipt_path),
        coordination=(
            dict(readiness.get("coordination"))
            if isinstance(readiness.get("coordination"), Mapping)
            else {}
        ),
    )


def wait_for_bar_readiness(
    datastore_root: Path,
    *,
    target_snapshot_for: object,
    required_symbols: Sequence[str],
    timeout_seconds: float,
    clock: Callable[[], object] | None = None,
    sleeper: Callable[[float], None] = time.sleep,
    monotonic_clock: Callable[[], float] = time.monotonic,
    poll_seconds: float = 0.25,
) -> BarReadinessWait:
    """Wait monotonically for one exact immutable all-symbol readiness receipt."""

    if timeout_seconds < 0:
        raise ValueError("Bar readiness timeout cannot be negative")
    if poll_seconds <= 0:
        raise ValueError("Bar readiness poll interval must be positive")
    now = clock or _now
    started_at = _utc(now(), "wait_started_at")
    deadline_at = started_at + pd.Timedelta(seconds=float(timeout_seconds))
    monotonic_deadline = monotonic_clock() + float(timeout_seconds)
    last_error = ""
    while True:
        try:
            readiness = read_bar_readiness(
                datastore_root,
                target_snapshot_for=target_snapshot_for,
                required_symbols=required_symbols,
            )
            observed_at = max(_utc(now(), "observed_at"), readiness.ready_at)
            if readiness.ready_at <= deadline_at and observed_at <= deadline_at:
                return BarReadinessWait(
                    status="READY",
                    observed_at=observed_at,
                    deadline_at=deadline_at,
                    readiness=readiness,
                )
            last_error = (
                "Verified Loop A readiness became authoritative after the Pricing "
                f"deadline ({readiness.ready_at.isoformat()} > {deadline_at.isoformat()})."
            )
        except BarReadinessError as exc:
            last_error = f"{type(exc).__name__}: {exc}"
        remaining = monotonic_deadline - monotonic_clock()
        if remaining <= 0:
            observed_at = max(_utc(now(), "observed_at"), deadline_at)
            return BarReadinessWait(
                status="DEADLINE_MISSED",
                observed_at=observed_at,
                deadline_at=deadline_at,
                detail=last_error,
            )
        sleeper(min(float(poll_seconds), remaining))


def bar_readiness_pointer_path(datastore_root: Path) -> Path:
    return Path(datastore_root) / "loop-a" / "bar-readiness-latest" / "run.json"


def _publish_pointer(root: Path, readiness: BarReadiness) -> None:
    _write_json_atomic(
        bar_readiness_pointer_path(root),
        {
            "schema_version": BAR_READINESS_POINTER_VERSION,
            "current": {
                "run_path": readiness.directory.relative_to(root).as_posix(),
                "target_snapshot_for": readiness.target_snapshot_for.isoformat(),
                "ready_at": readiness.ready_at.isoformat(),
                "receipt_checksum_sha256": readiness.receipt_checksum_sha256,
            },
        },
    )


def _directory(root: Path, target: pd.Timestamp) -> Path:
    return root / "loop-a" / "bar-readiness" / str(target.value)


def _validated_frozen_bar(
    raw: Mapping[str, object],
    *,
    symbol: str,
    target: pd.Timestamp,
) -> dict[str, object]:
    timestamp = _utc(
        raw.get("timestamp", raw.get("bar_timestamp")),
        f"{symbol} frozen bar timestamp",
    )
    if timestamp + pd.Timedelta(minutes=1) != target:
        raise BarReadinessError(
            f"Frozen readiness for {symbol} is not the exact completed target bar"
        )
    provider = str(raw.get("provider", "")).strip().lower()
    dataset = str(raw.get("dataset", "")).strip().upper()
    schema = str(raw.get("schema", "")).strip().lower()
    timeframe = str(raw.get("timeframe", "")).strip().lower()
    if (
        provider != "databento"
        or dataset != "EQUS.MINI"
        or schema != "ohlcv-1m"
        or timeframe != "1m"
    ):
        raise BarReadinessError(
            "Frozen readiness must be canonical Databento EQUS.MINI ohlcv-1m"
        )
    values: dict[str, float] = {}
    for field in ("open", "high", "low", "close", "volume"):
        try:
            value = float(raw.get(field, float("nan")))
        except (TypeError, ValueError) as exc:
            raise BarReadinessError(
                f"Frozen readiness {field} is invalid for {symbol}"
            ) from exc
        if not math.isfinite(value):
            raise BarReadinessError(
                f"Frozen readiness {field} is invalid for {symbol}"
            )
        values[field] = value
    if (
        min(values["open"], values["high"], values["low"], values["close"])
        <= 0
        or values["high"] < max(values["open"], values["low"], values["close"])
        or values["low"] > min(values["open"], values["high"], values["close"])
        or values["volume"] < 0
    ):
        raise BarReadinessError(f"Frozen readiness OHLCV is incoherent for {symbol}")
    return {
        "timestamp": timestamp,
        "provider": provider,
        "dataset": dataset,
        "schema": schema,
        "timeframe": timeframe,
        **values,
    }


def _verify_frozen_bar_evidence(
    directory: Path,
    *,
    symbol: str,
    target: pd.Timestamp,
    raw: Mapping[str, object],
) -> None:
    source_file = Path(str(raw.get("source_file", ""))).resolve()
    if source_file.parent != (directory / "bars").resolve():
        raise BarReadinessError(
            f"Frozen readiness source path escapes its target directory for {symbol}"
        )
    expected_checksum = str(raw.get("source_file_checksum_sha256", ""))
    if not source_file.is_file() or file_checksum(source_file) != expected_checksum:
        raise BarReadinessError(
            f"Frozen readiness source checksum failed for {symbol}"
        )
    try:
        frame = pd.read_parquet(source_file)
    except Exception as exc:
        raise BarReadinessError(
            f"Frozen readiness source is unreadable for {symbol}"
        ) from exc
    required = {"timestamp", "open", "high", "low", "close", "volume"}
    if len(frame) != 1 or not required.issubset(frame.columns):
        raise BarReadinessError(
            f"Frozen readiness source row is malformed for {symbol}"
        )
    observed = {
        **frame.iloc[0].to_dict(),
        "provider": raw.get("provider"),
        "dataset": raw.get("dataset"),
        "schema": raw.get("schema"),
        "timeframe": raw.get("timeframe"),
    }
    normalized = _validated_frozen_bar(observed, symbol=symbol, target=target)
    for field in ("open", "high", "low", "close", "volume"):
        if not math.isclose(
            float(normalized[field]),
            float(raw.get(field, float("nan"))),
            rel_tol=1e-12,
            abs_tol=1e-12,
        ):
            raise BarReadinessError(
                f"Frozen readiness metadata disagrees with its source for {symbol}"
            )


def _symbols(values: object) -> tuple[str, ...]:
    if isinstance(values, (str, bytes)) or not isinstance(values, Sequence):
        raise BarReadinessError("Bar-readiness symbols must be an array")
    output = tuple(
        dict.fromkeys(str(value).strip().upper() for value in values if str(value).strip())
    )
    if not output:
        raise BarReadinessError("Bar-readiness symbols cannot be empty")
    return output


def _semantic_checksum(payload: Mapping[str, object]) -> str:
    encoded = json.dumps(
        dict(payload), sort_keys=True, separators=(",", ":"), default=str
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _write_json(path: Path, payload: Mapping[str, object]) -> None:
    path.write_text(
        json.dumps(dict(payload), indent=2, sort_keys=True, default=str) + "\n",
        encoding="utf-8",
    )


def _write_json_atomic(path: Path, payload: Mapping[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + f".tmp-{os.getpid()}")
    try:
        _write_json(temporary, payload)
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def _utc(value: object, label: str) -> pd.Timestamp:
    timestamp = pd.to_datetime(value, utc=True, errors="coerce")
    if pd.isna(timestamp):
        raise BarReadinessError(f"Invalid bar-readiness {label}")
    return pd.Timestamp(timestamp)


def _now() -> datetime:
    return datetime.now(timezone.utc)


__all__ = [
    "BAR_READINESS_VERSION",
    "FROZEN_BAR_READINESS_VERSION",
    "BarReadiness",
    "BarReadinessError",
    "bar_readiness_pointer_path",
    "publish_bar_readiness",
    "publish_frozen_bar_readiness",
    "read_bar_readiness",
]
