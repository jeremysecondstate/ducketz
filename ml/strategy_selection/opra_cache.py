from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
from bisect import bisect_left
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Mapping, Sequence

import pandas as pd

from datafetching.databento_opra_history import (
    canonical_root,
    record_consumer_usage,
    verify_partition,
)
from datafetching.decision_time import is_eligible_option_target
from ml.artifacts import file_checksum, utc_timestamp
from ml.option_pricing.opra import (
    DEFAULT_EMULATED_PREDICTION_LATENCY_SECONDS,
    normalize_cbbo_records,
    normalize_definition_records,
)
from ml.option_pricing_opra_replay import (
    REPLAY_RECEIPT_VERSION,
    REPLAY_VERSION,
    opra_replay_source_fingerprint,
)
from options.features import (
    OPTION_FEATURE_SCHEMA_VERSION,
    OPTION_FEATURE_VERSION,
    OPTION_SURFACE_QUALITY_POLICY_VERSION,
)
from options.snapshot import OPTION_CHAIN_SCHEMA_VERSION


OPRA_STRATEGY_CACHE_VERSION = "strategy-opra-observed-chain-cache-v2"
OPRA_STRATEGY_CACHE_RECEIPT_VERSION = (
    "strategy-opra-observed-chain-cache-receipt-v1"
)
OPRA_STRATEGY_CACHE_POINTER_VERSION = (
    "strategy-opra-observed-chain-cache-pointer-v2"
)

_DAILY_WEEKLY_ENTRY_DELAY = pd.Timedelta(minutes=30)
_DAILY_WEEKLY_ENTRY_WINDOW = pd.Timedelta(minutes=15)
_EXIT_DELAYS = {
    "1h": pd.Timedelta(hours=2),
    "4h": pd.Timedelta(hours=6),
    "1d": pd.Timedelta(days=2),
    "1w": pd.Timedelta(days=4),
    "1w-d1": pd.Timedelta(days=2),
    "1w-d2": pd.Timedelta(days=2),
    "1w-d3": pd.Timedelta(days=2),
    "1w-d4": pd.Timedelta(days=2),
    "1w-d5": pd.Timedelta(days=2),
}


@dataclass(frozen=True)
class OpraStrategyCache:
    run_directory: Path
    cache_key: str
    contracts: pd.DataFrame
    surfaces: pd.DataFrame
    source_files: tuple[Path, ...]
    reused: bool


_MEMORY_CACHES: dict[tuple[str, int], OpraStrategyCache] = {}


@dataclass(frozen=True)
class _Partition:
    directory: Path
    manifest_path: Path
    receipt_path: Path
    parquet_path: Path
    manifest: Mapping[str, object]
    symbol: str
    start: pd.Timestamp
    end: pd.Timestamp

    @property
    def metadata_files(self) -> tuple[Path, Path]:
        return self.manifest_path, self.receipt_path


@dataclass(frozen=True, order=True)
class _StrategyInterval:
    symbol: str
    lower: pd.Timestamp
    upper: pd.Timestamp
    snapshot_for: pd.Timestamp
    purpose: str


def ensure_opra_strategy_cache(
    datastore_root: Path,
    *,
    samples: pd.DataFrame,
    symbols: Sequence[str],
    published_at: object | None = None,
) -> OpraStrategyCache | None:
    """Build or reuse compact observed OPRA entry/exit surfaces for Strategy.

    Only the earliest receipt in each Strategy entry or exit window is retained.
    Raw OPRA partitions remain immutable evidence, while recurring Strategy runs
    read this checksum-sealed compact view instead of multi-gigabyte CBBO files.
    """

    root = Path(datastore_root).resolve()
    clean_symbols = tuple(
        dict.fromkeys(
            str(value).strip().upper() for value in symbols if str(value).strip()
        )
    )
    if not clean_symbols:
        return None
    cbbo = _cheap_partitions(root, schema="cbbo-1m", symbols=clean_symbols)
    if not cbbo:
        cbbo = _cheap_partitions(root, schema="cbbo-1s", symbols=clean_symbols)
    if not cbbo:
        return None
    cbbo_schema = str(cbbo[0].manifest.get("schema") or "")
    definitions = _cheap_partitions(
        root,
        schema="definition",
        symbols=clean_symbols,
    )
    if not definitions:
        return None
    archive_start = min(item.start for item in cbbo)
    archive_end = max(item.end for item in cbbo)
    intervals = _strategy_intervals(
        samples,
        symbols=clean_symbols,
        archive_start=archive_start,
        archive_end=archive_end,
    )
    if not intervals:
        return None
    relevant_cbbo = tuple(
        item
        for item in cbbo
        if any(
            interval.symbol == item.symbol
            and interval.lower < item.end
            and interval.upper >= item.start
            for interval in intervals
        )
    )
    if not relevant_cbbo:
        return None
    source_fingerprint = _source_fingerprint((*relevant_cbbo, *definitions))
    request_fingerprint = _request_fingerprint(intervals)
    cache_key = hashlib.sha256(
        f"{source_fingerprint}:{request_fingerprint}".encode("utf-8")
    ).hexdigest()
    existing = load_opra_strategy_cache(root, expected_cache_key=cache_key)
    if existing is not None:
        return existing
    replay_verification_files = _verified_replay_seal(
        root,
        symbols=clean_symbols,
    )
    replay_sealed = bool(replay_verification_files)

    selected_quotes: list[pd.DataFrame] = []
    consumed_partitions: list[_Partition] = []
    for partition in relevant_cbbo:
        relevant = tuple(
            interval
            for interval in intervals
            if interval.symbol == partition.symbol
            and interval.lower < partition.end
            and interval.upper >= partition.start
        )
        selected_surfaces = _selected_surface_times(
            partition.parquet_path, relevant
        )
        if not selected_surfaces:
            continue
        if not replay_sealed:
            verify_partition(partition.directory, datastore_root=root)
        raw = pd.read_parquet(
            partition.parquet_path,
            columns=(
                "ts_recv",
                "symbol",
                "bid_px_00",
                "ask_px_00",
                "publisher_id",
            ),
            filters=[
                (
                    "ts_recv",
                    "in",
                    list(
                        dict.fromkeys(
                            quote_time
                            for quote_time, _snapshot_for in selected_surfaces
                        )
                    ),
                )
            ],
        )
        normalized = normalize_cbbo_records(raw)
        quote_times = pd.to_datetime(
            normalized["quote_timestamp"], utc=True, errors="coerce"
        )
        for quote_time, snapshot_for in selected_surfaces:
            surface = normalized.loc[quote_times.eq(quote_time)].copy()
            surface = surface.loc[
                surface["contract_symbol"]
                .astype("string")
                .map(_occ_parent_symbol)
                .eq(partition.symbol)
            ]
            if surface.empty:
                continue
            surface["parent_symbol"] = partition.symbol
            surface["strategy_snapshot_for"] = snapshot_for
            selected_quotes.append(surface)
        consumed_partitions.append(partition)
    if not selected_quotes:
        return None
    quotes = (
        pd.concat(selected_quotes, ignore_index=True, sort=False)
        .sort_values(
            ["strategy_snapshot_for", "quote_timestamp", "contract_symbol"],
            kind="stable",
        )
        .drop_duplicates(
            ["strategy_snapshot_for", "quote_timestamp", "contract_symbol"],
            keep="last",
        )
        .reset_index(drop=True)
    )

    definition_frames: list[pd.DataFrame] = []
    consumed_definitions: list[_Partition] = []
    for partition in definitions:
        if partition.symbol not in clean_symbols:
            continue
        if not replay_sealed:
            verify_partition(partition.directory, datastore_root=root)
        normalized = normalize_definition_records(
            pd.read_parquet(partition.parquet_path)
        )
        normalized = normalized.loc[
            normalized["symbol"].astype("string").str.upper().eq(partition.symbol)
        ]
        if not normalized.empty:
            definition_frames.append(normalized)
            consumed_definitions.append(partition)
    if not definition_frames:
        return None
    definition_frame = (
        pd.concat(definition_frames, ignore_index=True, sort=False)
        .sort_values(["definition_effective_at", "contract_symbol"], kind="stable")
        .drop_duplicates(
            ["contract_symbol", "definition_effective_at"], keep="last"
        )
        .reset_index(drop=True)
    )
    merged = pd.merge_asof(
        quotes.sort_values("quote_timestamp", kind="stable"),
        definition_frame.sort_values("definition_effective_at", kind="stable"),
        left_on="quote_timestamp",
        right_on="definition_effective_at",
        by="contract_symbol",
        direction="backward",
        allow_exact_matches=True,
    )
    merged = merged.loc[
        merged["expiration_date"].notna()
        & merged["call_put"].isin(("call", "put"))
        & merged["strike"].notna()
    ].copy()
    if merged.empty:
        return None

    bar_files: list[Path] = []
    underlying = pd.Series(float("nan"), index=merged.index)
    for symbol in clean_symbols:
        mask = merged["symbol"].astype("string").str.upper().eq(symbol)
        if not mask.any():
            continue
        values, paths = _underlying_prices(
            root,
            symbol=symbol,
            timestamps=pd.DatetimeIndex(merged.loc[mask, "quote_timestamp"]),
        )
        underlying.loc[mask] = values.to_numpy()
        bar_files.extend(paths)
    bid = pd.to_numeric(merged["bid"], errors="coerce")
    ask = pd.to_numeric(merged["ask"], errors="coerce")
    midpoint = (bid + ask) / 2.0
    contracts = pd.DataFrame(
        {
            "symbol": merged["symbol"].astype("string").str.upper(),
            "snapshot_for": pd.to_datetime(
                merged["strategy_snapshot_for"], utc=True, errors="coerce"
            ),
            "available_at": pd.to_datetime(
                merged["quote_timestamp"], utc=True, errors="coerce"
            ),
            "contract_symbol": merged["contract_symbol"].astype("string"),
            "call_put": merged["call_put"].astype("string").str.upper(),
            "expiration_date": pd.to_datetime(
                merged["expiration_date"], utc=True, errors="coerce"
            ),
            "strike": pd.to_numeric(merged["strike"], errors="coerce"),
            "underlying_price": pd.to_numeric(underlying, errors="coerce"),
            "bid": bid,
            "ask": ask,
            "open_interest": float("nan"),
            "volume": float("nan"),
            "delta": float("nan"),
            "gamma": float("nan"),
            "theta": float("nan"),
            "vega": float("nan"),
            "multiplier": pd.to_numeric(merged["multiplier"], errors="coerce"),
            "mini": False,
            "non_standard": ~merged["standard_contract"].fillna(False).astype(bool),
            "quote_valid": bid.ge(0.0) & ask.gt(bid),
            "relative_bid_ask_spread": (ask - bid)
            / midpoint.where(midpoint.gt(0.0)),
            "quote_staleness_seconds": (
                pd.to_datetime(
                    merged["quote_timestamp"], utc=True, errors="coerce"
                )
                - pd.to_datetime(
                    merged["strategy_snapshot_for"], utc=True, errors="coerce"
                )
            ).dt.total_seconds(),
            "quote_timestamp": pd.to_datetime(
                merged["quote_timestamp"], utc=True, errors="coerce"
            ),
            "source_provider": "databento-opra",
            "liquidity_evidence_basis": "OPRA_VALID_BBO_SPREAD",
            "fallback_used": False,
            "schema_version": OPTION_CHAIN_SCHEMA_VERSION,
        }
    )
    contracts = contracts.loc[
        contracts["underlying_price"].gt(0.0)
        & contracts["expiration_date"].gt(contracts["snapshot_for"].dt.normalize())
    ].reset_index(drop=True)
    if contracts.empty:
        return None
    surfaces = _surface_rows(contracts)
    evidence_by_path = {
        item.directory: item
        for item in (*consumed_partitions, *consumed_definitions)
    }
    evidence_partitions = tuple(evidence_by_path.values())
    published = utc_timestamp(published_at)
    cache = _publish_cache(
        root,
        cache_key=cache_key,
        source_fingerprint=source_fingerprint,
        request_fingerprint=request_fingerprint,
        cbbo_schema=cbbo_schema,
        contracts=contracts,
        surfaces=surfaces,
        partitions=evidence_partitions,
        bar_files=tuple(dict.fromkeys(bar_files)),
        published_at=published,
        verification_files=replay_verification_files,
        verification_basis=(
            "SEALED_OPRA_PRICING_REPLAY"
            if replay_sealed
            else "DIRECT_PARTITION_VERIFICATION"
        ),
    )
    record_consumer_usage(
        root,
        consumer="options-strategy-observed-outcome-cache",
        schemas=("definition", cbbo_schema),
        rows=len(contracts),
        source_files=tuple(item.parquet_path for item in evidence_partitions),
        refresh_health=False,
    )
    return cache


def strategy_opra_prediction_clocks(
    datastore_root: Path,
    *,
    samples: pd.DataFrame,
    symbols: Sequence[str],
) -> Mapping[pd.Timestamp, tuple[pd.Timestamp, pd.Timestamp]]:
    """Return causal completed-bar clocks that overlap verified OPRA history."""

    root = Path(datastore_root).resolve()
    clean_symbols = tuple(
        dict.fromkeys(
            str(value).strip().upper()
            for value in symbols
            if str(value).strip()
        )
    )
    partitions = _cheap_partitions(
        root, schema="cbbo-1m", symbols=clean_symbols
    )
    if not partitions:
        partitions = _cheap_partitions(
            root, schema="cbbo-1s", symbols=clean_symbols
        )
    if not partitions:
        return {}
    return _prediction_clocks_from_samples(
        samples,
        symbols=clean_symbols,
        archive_start=min(item.start for item in partitions),
        archive_end=max(item.end for item in partitions),
    )


def load_opra_strategy_cache(
    datastore_root: Path,
    *,
    expected_cache_key: str | None = None,
    symbols: Sequence[str] | None = None,
    available_not_before: object | None = None,
    available_not_after: object | None = None,
    latest_snapshot_only: bool = False,
) -> OpraStrategyCache | None:
    root = Path(datastore_root).resolve()
    pointer = root / "ml" / "strategy-opra-history-latest" / "run.json"
    if not pointer.is_file():
        return None
    clean_symbols = tuple(
        dict.fromkeys(
            str(value).strip().upper()
            for value in (symbols or ())
            if str(value).strip()
        )
    )
    windowed_read = bool(
        clean_symbols
        or available_not_before is not None
        or available_not_after is not None
        or latest_snapshot_only
    )
    memory_key = (str(root), pointer.stat().st_mtime_ns)
    remembered = None if windowed_read else _MEMORY_CACHES.get(memory_key)
    if remembered is not None and (
        expected_cache_key is None or remembered.cache_key == expected_cache_key
    ):
        return remembered
    try:
        pointer_payload = json.loads(pointer.read_text(encoding="utf-8"))
        current = pointer_payload["current"]
        run = (root / str(current["run_path"])).resolve()
        if root not in run.parents:
            raise ValueError("cache run escapes datastore")
        manifest_path = run / "manifest.json"
        receipt_path = run / "receipt.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
        if (
            pointer_payload.get("schema_version")
            != OPRA_STRATEGY_CACHE_POINTER_VERSION
            or receipt.get("schema_version")
            != OPRA_STRATEGY_CACHE_RECEIPT_VERSION
            or manifest.get("schema_version") != OPRA_STRATEGY_CACHE_VERSION
            or receipt.get("manifest_checksum_sha256") != file_checksum(manifest_path)
            or current.get("receipt_checksum_sha256") != file_checksum(receipt_path)
            or (
                expected_cache_key is not None
                and manifest.get("cache_key") != expected_cache_key
            )
        ):
            return None
        outputs = manifest.get("outputs")
        if not isinstance(outputs, Mapping):
            return None
        for name in ("contracts.parquet", "surfaces.parquet"):
            path = run / name
            item = outputs.get(name)
            if (
                not isinstance(item, Mapping)
                or not path.is_file()
                or path.stat().st_size != int(item.get("size_bytes", -1))
                or file_checksum(path) != item.get("checksum_sha256")
            ):
                return None
        source_files = [
            run / "contracts.parquet",
            run / "surfaces.parquet",
            manifest_path,
            receipt_path,
            pointer,
        ]
        for item in manifest.get("lineage_metadata_files", ()):
            if isinstance(item, Mapping):
                path = (root / str(item.get("path") or "")).resolve()
                if path.is_file() and file_checksum(path) == item.get(
                    "checksum_sha256"
                ):
                    source_files.append(path)
        parquet_filters: list[tuple[str, str, object]] = []
        if clean_symbols:
            parquet_filters.append(("symbol", "in", list(clean_symbols)))
        if available_not_before is not None:
            parquet_filters.append(
                ("available_at", ">=", _utc(available_not_before).to_pydatetime())
            )
        if available_not_after is not None:
            parquet_filters.append(
                ("available_at", "<=", _utc(available_not_after).to_pydatetime())
            )
        read_filters = parquet_filters or None
        surfaces = pd.read_parquet(
            run / "surfaces.parquet", filters=read_filters
        )
        contract_filters = list(parquet_filters)
        if latest_snapshot_only and not surfaces.empty:
            ordered = surfaces.sort_values(
                ["snapshot_for", "available_at"], kind="stable"
            )
            latest = ordered.iloc[-1]
            latest_snapshot_for = pd.Timestamp(
                latest["snapshot_for"]
            ).to_pydatetime()
            latest_available_at = pd.Timestamp(
                latest["available_at"]
            ).to_pydatetime()
            surfaces = surfaces.loc[
                pd.to_datetime(
                    surfaces["snapshot_for"], utc=True, errors="coerce"
                ).eq(_utc(latest_snapshot_for))
                & pd.to_datetime(
                    surfaces["available_at"], utc=True, errors="coerce"
                ).eq(_utc(latest_available_at))
            ].copy()
            contract_filters.extend(
                (
                    ("snapshot_for", "==", latest_snapshot_for),
                    ("available_at", "==", latest_available_at),
                )
            )
        loaded = OpraStrategyCache(
            run_directory=run,
            cache_key=str(manifest["cache_key"]),
            contracts=pd.read_parquet(
                run / "contracts.parquet", filters=contract_filters or None
            ),
            surfaces=surfaces,
            source_files=tuple(dict.fromkeys(source_files)),
            reused=True,
        )
        if not windowed_read:
            _MEMORY_CACHES.clear()
            _MEMORY_CACHES[memory_key] = loaded
        return loaded
    except (KeyError, OSError, ValueError, json.JSONDecodeError):
        return None


def _cheap_partitions(
    root: Path,
    *,
    schema: str,
    symbols: Sequence[str],
) -> tuple[_Partition, ...]:
    canonical = canonical_root(root)
    selected = set(symbols)
    partitions: list[_Partition] = []
    for receipt_path in sorted(
        canonical.glob(f"{schema}/*/dates/*/segments/*/receipt.json")
    ):
        directory = receipt_path.parent
        manifest_path = directory / "manifest.json"
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
            normalized = manifest["normalized"]
            parquet = directory / str(normalized["path"])
            parent_symbols = tuple(
                symbol
                for symbol in _parent_symbols(manifest)
                if symbol in selected
            )
            if not parent_symbols:
                continue
            if (
                receipt.get("manifest_checksum_sha256") != file_checksum(manifest_path)
                or not parquet.is_file()
                or parquet.stat().st_size != int(normalized["size_bytes"])
            ):
                continue
            start = _utc(manifest.get("partition_start"))
            end = _utc(manifest.get("partition_end"))
        except (KeyError, OSError, ValueError, json.JSONDecodeError):
            continue
        for symbol in parent_symbols:
            partitions.append(
                _Partition(
                    directory,
                    manifest_path,
                    receipt_path,
                    parquet,
                    manifest,
                    symbol,
                    start,
                    end,
                )
            )
    return tuple(partitions)


def _strategy_intervals(
    samples: pd.DataFrame,
    *,
    symbols: Sequence[str],
    archive_start: pd.Timestamp,
    archive_end: pd.Timestamp,
) -> tuple[_StrategyInterval, ...]:
    required = {
        "symbol",
        "horizon",
        "bar_end_timestamp",
        "decision_timestamp",
        "information_available_at",
        "target_window_start",
        "target_window_end",
        "label_status",
    }
    if samples.empty or not required.issubset(samples.columns):
        return ()
    selected = samples.loc[
        samples["label_status"].astype("string").eq("COMPLETE")
        & samples["symbol"].astype("string").str.upper().isin(symbols)
        & samples["horizon"].astype("string").isin(_EXIT_DELAYS)
    ].copy()
    prediction_clocks = _prediction_clocks_from_samples(
        selected,
        symbols=symbols,
        archive_start=archive_start,
        archive_end=archive_end,
    )
    intervals: set[_StrategyInterval] = set()
    for row in selected.to_dict("records"):
        symbol = str(row["symbol"]).strip().upper()
        pricing_target = _utc(row["bar_end_timestamp"])
        clock = prediction_clocks.get(pricing_target)
        if clock is None:
            continue
        _prediction_created, prediction_available = clock
        target_start = _utc(row["target_window_start"])
        entry_lower, entry_upper = strategy_entry_bounds(
            row,
            prediction_available=prediction_available,
        )
        entry_lower = max(entry_lower, archive_start)
        entry_upper = min(
            entry_upper,
            archive_end - pd.Timedelta(nanoseconds=1),
        )
        if entry_lower <= entry_upper:
            intervals.add(
                _StrategyInterval(
                    symbol,
                    entry_lower,
                    entry_upper,
                    pricing_target,
                    "ENTRY_AFTER_PREDICTION",
                )
            )
        exit_lower = max(_utc(row["target_window_end"]), archive_start)
        exit_upper = min(
            _utc(row["target_window_end"])
            + _EXIT_DELAYS[str(row["horizon"])],
            archive_end - pd.Timedelta(nanoseconds=1),
        )
        if exit_lower <= exit_upper:
            intervals.add(
                _StrategyInterval(
                    symbol,
                    exit_lower,
                    exit_upper,
                    _utc(row["target_window_end"]),
                    "EXIT_OUTCOME",
                )
            )
    return tuple(sorted(intervals))


def strategy_entry_bounds(
    sample: Mapping[str, object],
    *,
    prediction_available: object,
) -> tuple[pd.Timestamp, pd.Timestamp]:
    """Return the causal option-entry search window for one Strategy sample."""

    available = _utc(prediction_available) + pd.Timedelta(nanoseconds=1)
    target_start = _utc(sample["target_window_start"])
    horizon = str(sample["horizon"]).strip().lower()
    if horizon in {"1d", "1w", "1w-d1", "1w-d2", "1w-d3", "1w-d4", "1w-d5"}:
        lower = max(available, target_start + _DAILY_WEEKLY_ENTRY_DELAY)
        return lower, target_start + _DAILY_WEEKLY_ENTRY_DELAY + _DAILY_WEEKLY_ENTRY_WINDOW
    return available, target_start - pd.Timedelta(nanoseconds=1)


def _prediction_clocks_from_samples(
    samples: pd.DataFrame,
    *,
    symbols: Sequence[str],
    archive_start: pd.Timestamp,
    archive_end: pd.Timestamp,
) -> dict[pd.Timestamp, tuple[pd.Timestamp, pd.Timestamp]]:
    required = {
        "symbol",
        "horizon",
        "bar_end_timestamp",
        "decision_timestamp",
        "information_available_at",
        "target_window_start",
        "label_status",
    }
    if samples.empty or not required.issubset(samples.columns):
        return {}
    selected = samples.loc[
        samples["label_status"].astype("string").eq("COMPLETE")
        & samples["symbol"].astype("string").str.upper().isin(symbols)
        & samples["horizon"].astype("string").isin(_EXIT_DELAYS)
    ]
    grouped: dict[pd.Timestamp, list[pd.Timestamp]] = {}
    target_starts: dict[pd.Timestamp, list[pd.Timestamp]] = {}
    for row in selected.to_dict("records"):
        target = _utc(row["bar_end_timestamp"])
        if (
            target < archive_start
            or target >= archive_end
            or target != target.floor("15min")
            or not is_eligible_option_target(target)
        ):
            continue
        created = max(
            _utc(row["decision_timestamp"]),
            _utc(row["information_available_at"]),
        )
        if created <= target:
            continue
        grouped.setdefault(target, []).append(created)
        target_starts.setdefault(target, []).append(
            _utc(row["target_window_start"])
        )
    output: dict[pd.Timestamp, tuple[pd.Timestamp, pd.Timestamp]] = {}
    for target, created_values in grouped.items():
        created = max(created_values)
        available = created + pd.Timedelta(
            seconds=DEFAULT_EMULATED_PREDICTION_LATENCY_SECONDS
        )
        if available >= min(target_starts[target]):
            continue
        output[target] = (created, available)
    return dict(sorted(output.items()))


def _selected_surface_times(
    path: Path,
    intervals: Sequence[_StrategyInterval],
) -> tuple[tuple[pd.Timestamp, pd.Timestamp], ...]:
    candidate_times: set[pd.Timestamp] = set()
    for interval in intervals:
        start = interval.lower.ceil("min")
        end = interval.upper.floor("min")
        if start <= end:
            candidate_times.update(
                pd.Timestamp(value)
                for value in pd.date_range(start, end, freq="1min")
            )
    if not candidate_times:
        return ()
    clocks = pd.read_parquet(
        path,
        columns=["ts_recv"],
        filters=[("ts_recv", "in", list(sorted(candidate_times)))],
    )
    timestamps = clocks["ts_recv"] if "ts_recv" in clocks else clocks.index
    available = pd.DatetimeIndex(
        pd.to_datetime(timestamps, utc=True, errors="coerce")
    ).dropna().unique().sort_values()
    available_ns = tuple(pd.Timestamp(value).value for value in available)
    selected: set[tuple[pd.Timestamp, pd.Timestamp]] = set()
    for interval in intervals:
        position = bisect_left(available_ns, interval.lower.value)
        if position < len(available):
            value = pd.Timestamp(available[position])
            if value <= interval.upper:
                selected.add((value, interval.snapshot_for))
    return tuple(sorted(selected))


def _underlying_prices(
    root: Path,
    *,
    symbol: str,
    timestamps: pd.DatetimeIndex,
) -> tuple[pd.Series, tuple[Path, ...]]:
    folder = root / "stocks" / symbol / "bars" / "1m" / "databento" / "normalized"
    paths = tuple(sorted(folder.glob("*.parquet")))
    if not paths:
        return pd.Series(float("nan"), index=range(len(timestamps))), ()
    frames = [pd.read_parquet(path, columns=["timestamp", "close"]) for path in paths]
    bars = (
        pd.concat(frames, ignore_index=True, sort=False)
        .sort_values("timestamp", kind="stable")
        .drop_duplicates("timestamp", keep="last")
    )
    bar_end = pd.DatetimeIndex(
        pd.to_datetime(bars["timestamp"], utc=True, errors="coerce")
        + pd.Timedelta(minutes=1)
    )
    closes = pd.to_numeric(bars["close"], errors="coerce").to_numpy()
    positions = bar_end.searchsorted(timestamps, side="right") - 1
    values = [
        closes[position] if 0 <= position < len(closes) else float("nan")
        for position in positions
    ]
    return pd.Series(values, index=range(len(timestamps))), paths


def _surface_rows(contracts: pd.DataFrame) -> pd.DataFrame:
    grouped = contracts.groupby(
        ["symbol", "snapshot_for", "available_at"],
        sort=True,
        as_index=False,
    )
    rows = grouped.agg(
        valid_contract_count=("quote_valid", "sum"),
        call_put_count=("call_put", "nunique"),
    )
    rows["surface_quality_pass"] = (
        rows["valid_contract_count"].ge(4) & rows["call_put_count"].eq(2)
    )
    rows["source_provider"] = "databento-opra"
    rows["surface_quality_basis"] = "OPRA_VALID_BBO_CALL_PUT_COVERAGE"
    rows["fallback_used"] = False
    rows["surface_quality_policy_version"] = OPTION_SURFACE_QUALITY_POLICY_VERSION
    rows["calculation_version"] = OPTION_FEATURE_VERSION
    rows["schema_version"] = OPTION_FEATURE_SCHEMA_VERSION
    return rows.drop(columns=["valid_contract_count", "call_put_count"])


def _publish_cache(
    root: Path,
    *,
    cache_key: str,
    source_fingerprint: str,
    request_fingerprint: str,
    cbbo_schema: str,
    contracts: pd.DataFrame,
    surfaces: pd.DataFrame,
    partitions: Sequence[_Partition],
    bar_files: Sequence[Path],
    published_at: pd.Timestamp,
    verification_files: Sequence[Path] = (),
    verification_basis: str = "DIRECT_PARTITION_VERIFICATION",
) -> OpraStrategyCache:
    runs = root / "ml" / "strategy-opra-history-runs"
    runs.mkdir(parents=True, exist_ok=True)
    destination = runs / cache_key[:24]
    if destination.exists():
        raise RuntimeError(f"Unverified OPRA Strategy cache already exists: {destination}")
    staging = Path(tempfile.mkdtemp(prefix=f".{destination.name}.tmp-", dir=runs))
    try:
        contracts_path = staging / "contracts.parquet"
        surfaces_path = staging / "surfaces.parquet"
        contracts.to_parquet(contracts_path, index=False, compression="zstd")
        surfaces.to_parquet(surfaces_path, index=False, compression="zstd")
        metadata_files = tuple(
            dict.fromkeys(
                [
                    path
                    for partition in partitions
                    for path in partition.metadata_files
                ]
                + [Path(path) for path in verification_files]
            )
        )
        manifest = {
            "schema_version": OPRA_STRATEGY_CACHE_VERSION,
            "cache_key": cache_key,
            "provider": "databento-opra",
            "dataset": "OPRA.PILLAR",
            "cbbo_schema": cbbo_schema,
            "published_at": published_at.isoformat(),
            "source_fingerprint": source_fingerprint,
            "request_fingerprint": request_fingerprint,
            "archive_verification_basis": verification_basis,
            "symbols": sorted(set(contracts["symbol"].astype(str))),
            "surface_count": len(surfaces),
            "contract_count": len(contracts),
            "earliest_surface": pd.Timestamp(
                surfaces["snapshot_for"].min()
            ).isoformat(),
            "latest_surface": pd.Timestamp(
                surfaces["snapshot_for"].max()
            ).isoformat(),
            "lineage_metadata_files": [
                {
                    "path": path.resolve().relative_to(root).as_posix(),
                    "checksum_sha256": file_checksum(path),
                }
                for path in metadata_files
            ],
            "underlying_bar_files": [
                {
                    "path": path.resolve().relative_to(root).as_posix(),
                    "checksum_sha256": file_checksum(path),
                }
                for path in bar_files
            ],
            "outputs": {
                path.name: {
                    "size_bytes": path.stat().st_size,
                    "checksum_sha256": file_checksum(path),
                }
                for path in (contracts_path, surfaces_path)
            },
        }
        _write_json(staging / "manifest.json", manifest)
        receipt = {
            "schema_version": OPRA_STRATEGY_CACHE_RECEIPT_VERSION,
            "cache_key": cache_key,
            "published_at": published_at.isoformat(),
            "manifest_checksum_sha256": file_checksum(staging / "manifest.json"),
        }
        _write_json(staging / "receipt.json", receipt)
        staging.replace(destination)
    finally:
        if staging.exists():
            try:
                staging.rmdir()
            except OSError:
                pass
    pointer = root / "ml" / "strategy-opra-history-latest" / "run.json"
    _write_json_atomic(
        pointer,
        {
            "schema_version": OPRA_STRATEGY_CACHE_POINTER_VERSION,
            "current": {
                "run_path": destination.relative_to(root).as_posix(),
                "published_at": published_at.isoformat(),
                "receipt_checksum_sha256": file_checksum(destination / "receipt.json"),
            },
        },
    )
    loaded = load_opra_strategy_cache(root, expected_cache_key=cache_key)
    if loaded is None:
        raise RuntimeError("Published OPRA Strategy cache failed verification")
    return OpraStrategyCache(
        loaded.run_directory,
        loaded.cache_key,
        loaded.contracts,
        loaded.surfaces,
        loaded.source_files,
        False,
    )


def _verified_replay_seal(
    root: Path,
    *,
    symbols: Sequence[str],
) -> tuple[Path, ...]:
    pointer = root / "ml" / "option-pricing-opra-replay-latest" / "run.json"
    try:
        pointer_payload = json.loads(pointer.read_text(encoding="utf-8"))
        current = pointer_payload["current"]
        run = (root / str(current["run_path"])).resolve()
        if root not in run.parents:
            return ()
        manifest_path = run / "manifest.json"
        receipt_path = run / "receipt.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
        clean_symbols = {
            str(value).strip().upper()
            for value in symbols
            if str(value).strip()
        }
        expected_fingerprint = opra_replay_source_fingerprint(
            root,
            symbols=tuple(sorted(clean_symbols)),
        )
        outputs = manifest.get("outputs")
        if (
            pointer_payload.get("schema_version")
            != "option-pricing-opra-causal-replay-pointer-v1"
            or current.get("receipt_checksum_sha256")
            != file_checksum(receipt_path)
            or receipt.get("schema_version") != REPLAY_RECEIPT_VERSION
            or receipt.get("manifest_checksum_sha256")
            != file_checksum(manifest_path)
            or manifest.get("schema_version") != REPLAY_VERSION
            or manifest.get("opra_source_fingerprint") != expected_fingerprint
            or not set(manifest.get("symbols", ())).issuperset(clean_symbols)
            or not isinstance(outputs, Mapping)
        ):
            return ()
        for name in (
            "pricing-samples.parquet",
            "pricing-predictions.parquet",
            "pricing-evaluations.parquet",
        ):
            path = run / name
            item = outputs.get(name)
            if (
                not isinstance(item, Mapping)
                or not path.is_file()
                or path.stat().st_size != int(item.get("size_bytes", -1))
            ):
                return ()
        return pointer, manifest_path, receipt_path
    except (KeyError, OSError, ValueError, json.JSONDecodeError):
        return ()


def _parent_symbol(manifest: Mapping[str, object]) -> str:
    values = _parent_symbols(manifest)
    return values[0] if values else ""


def _parent_symbols(manifest: Mapping[str, object]) -> tuple[str, ...]:
    request = manifest.get("request")
    values = request.get("symbols") if isinstance(request, Mapping) else None
    raw_values = (
        values
        if isinstance(values, list) and values
        else (manifest.get("symbol_scope"),)
    )
    return tuple(
        dict.fromkeys(
            str(value or "").strip().upper().removesuffix(".OPT")
            for value in raw_values
            if str(value or "").strip()
        )
    )


def _occ_parent_symbol(value: object) -> str:
    match = re.match(r"^([A-Z.]{1,6})\s*\d{6}[CP]", str(value).strip().upper())
    return match.group(1) if match else ""


def _source_fingerprint(partitions: Iterable[_Partition]) -> str:
    digest = hashlib.sha256()
    for item in sorted(partitions, key=lambda value: value.directory.as_posix()):
        normalized = item.manifest.get("normalized")
        digest.update(item.directory.as_posix().encode("utf-8"))
        digest.update(
            str(
                normalized.get("checksum_sha256")
                if isinstance(normalized, Mapping)
                else ""
            ).encode("utf-8")
        )
        digest.update(file_checksum(item.receipt_path).encode("utf-8"))
    return digest.hexdigest()


def _request_fingerprint(
    intervals: Sequence[_StrategyInterval],
) -> str:
    digest = hashlib.sha256()
    for interval in intervals:
        digest.update(
            (
                f"{interval.symbol}|{interval.lower.value}|"
                f"{interval.upper.value}|{interval.snapshot_for.value}|"
                f"{interval.purpose}"
            ).encode("utf-8")
        )
    return digest.hexdigest()


def _write_json(path: Path, payload: Mapping[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(dict(payload), indent=2, sort_keys=True, default=str) + "\n",
        encoding="utf-8",
    )


def _write_json_atomic(path: Path, payload: Mapping[str, object]) -> None:
    temporary = path.with_suffix(path.suffix + f".tmp-{os.getpid()}")
    try:
        _write_json(temporary, payload)
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def _utc(value: object) -> pd.Timestamp:
    timestamp = pd.to_datetime(value, utc=True, errors="coerce")
    if pd.isna(timestamp):
        raise ValueError(f"Invalid OPRA Strategy cache timestamp: {value!r}")
    return pd.Timestamp(timestamp)


def _occ_underlying(value: object) -> str:
    match = re.match(r"^([A-Z.]{1,6})\s*\d{6}[CP]", str(value).strip().upper())
    return match.group(1) if match else ""


__all__ = [
    "OPRA_STRATEGY_CACHE_POINTER_VERSION",
    "OPRA_STRATEGY_CACHE_RECEIPT_VERSION",
    "OPRA_STRATEGY_CACHE_VERSION",
    "OpraStrategyCache",
    "ensure_opra_strategy_cache",
    "load_opra_strategy_cache",
    "strategy_entry_bounds",
    "strategy_opra_prediction_clocks",
]
