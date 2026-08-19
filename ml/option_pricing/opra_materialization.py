from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Mapping, Sequence

import pandas as pd
import pyarrow.parquet as pq

from datafetching.databento_opra_history import (
    iter_verified_partitions,
    record_consumer_usage,
)
from datafetching.decision_time import latest_completed_bar_clock
from ml.option_pricing.causal import build_causal_samples, completed_bar_close
from ml.option_pricing.opra import (
    DEFAULT_EMULATED_PREDICTION_LATENCY_SECONDS,
    normalize_cbbo_records,
    normalize_definition_records,
    point_in_time_definition_asof,
    select_historical_source_target,
)
from ml.option_pricing.policies import ContractSelectionPolicy


@dataclass(frozen=True)
class ClosedOpraLockboxInventory:
    target_snapshot_fors: tuple[pd.Timestamp, ...]
    route_cluster_counts: Mapping[tuple[str, str], int]
    route_request_symbol_counts: Mapping[tuple[str, str], int]
    output_count: int
    outputs: tuple[Mapping[str, object], ...] = ()
    target_values_read: bool = False

    @property
    def cluster_count(self) -> int:
        return len(self.target_snapshot_fors)

    @property
    def start(self) -> pd.Timestamp | None:
        return self.target_snapshot_fors[0] if self.target_snapshot_fors else None

    @property
    def end(self) -> pd.Timestamp | None:
        return self.target_snapshot_fors[-1] if self.target_snapshot_fors else None


@dataclass(frozen=True)
class OpraMaterialization:
    samples: pd.DataFrame
    source_files: tuple[Path, ...]
    errors: Mapping[str, str]
    closed_lockbox: ClosedOpraLockboxInventory


def materialize_committed_opra_history(
    datastore_root: Path,
    *,
    symbols: Sequence[str],
    rate_observations: pd.DataFrame | None,
    contract_policy: ContractSelectionPolicy | None = None,
    target_snapshot_fors: Sequence[object] | None = None,
    prediction_clocks: Mapping[object, Sequence[object]] | None = None,
    allowed_cbbo_paths: Sequence[Path] | None = None,
    allowed_definition_paths: Sequence[Path] | None = None,
) -> tuple[pd.DataFrame, tuple[Path, ...], Mapping[str, str]]:
    materialized = _materialize(
        datastore_root,
        symbols=symbols,
        rate_observations=rate_observations,
        contract_policy=contract_policy,
        locked_targets=(),
        selected_targets=target_snapshot_fors,
        prediction_clocks=prediction_clocks,
        allowed_cbbo_paths=allowed_cbbo_paths,
        allowed_definition_paths=allowed_definition_paths,
    )
    return materialized.samples, materialized.source_files, materialized.errors


def materialize_committed_opra_history_v2(
    datastore_root: Path,
    *,
    symbols: Sequence[str],
    rate_observations: pd.DataFrame | None,
    closed_lockbox_clusters: int,
    contract_policy: ContractSelectionPolicy | None = None,
) -> OpraMaterialization:
    """Build causal pricing rows from verified canonical OPRA partitions.

    The last configured target clusters remain unread as the evaluation lockbox.
    Provider receipt clocks and event clocks remain separate, and target quotes
    are selected strictly after prediction availability.
    """

    if int(closed_lockbox_clusters) < 1:
        raise ValueError("closed_lockbox_clusters must be positive")
    inventory = _canonical_inventory(datastore_root, symbols=symbols)
    targets = _scheduled_targets(inventory["cbbo"])
    locked = tuple(targets[-int(closed_lockbox_clusters) :])
    return _materialize(
        datastore_root,
        symbols=symbols,
        rate_observations=rate_observations,
        contract_policy=contract_policy,
        locked_targets=locked,
        prediction_clocks=None,
        inventory=inventory,
    )


def _materialize(
    datastore_root: Path,
    *,
    symbols: Sequence[str],
    rate_observations: pd.DataFrame | None,
    contract_policy: ContractSelectionPolicy | None,
    locked_targets: Sequence[pd.Timestamp],
    selected_targets: Sequence[object] | None = None,
    prediction_clocks: Mapping[object, Sequence[object]] | None = None,
    allowed_cbbo_paths: Sequence[Path] | None = None,
    allowed_definition_paths: Sequence[Path] | None = None,
    inventory: Mapping[str, object] | None = None,
) -> OpraMaterialization:
    root = Path(datastore_root).resolve()
    clean_symbols = tuple(
        dict.fromkeys(str(value).strip().upper() for value in symbols if str(value).strip())
    )
    clocks = _normalize_prediction_clocks(prediction_clocks)
    requested_targets = (
        tuple(sorted({_utc(value) for value in selected_targets}))
        if selected_targets is not None
        else ()
    )
    loaded = inventory or _canonical_inventory(
        root,
        symbols=clean_symbols,
        requested_targets=requested_targets,
        prediction_clocks=clocks,
        allowed_cbbo_paths=allowed_cbbo_paths,
        allowed_definition_paths=allowed_definition_paths,
    )
    definitions = loaded["definitions"]
    cbbo = loaded["cbbo"]
    source_files = tuple(loaded["source_files"])
    errors = dict(loaded["errors"])
    if definitions.empty or cbbo.empty:
        return OpraMaterialization(
            pd.DataFrame(),
            source_files,
            errors,
            _lockbox_inventory(cbbo, locked_targets, source_files),
        )

    targets = (
        requested_targets
        if selected_targets is not None
        else tuple(sorted({*_scheduled_targets(cbbo), *clocks}))
    )
    locked = set(locked_targets)
    samples: list[pd.DataFrame] = []
    consumed = list(source_files)
    for target in targets:
        if target in locked:
            continue
        for symbol in clean_symbols:
            route = f"{symbol}@{target.isoformat()}"
            try:
                symbol_quotes = cbbo.loc[
                    cbbo["underlying_symbol"].eq(symbol)
                ]
                prediction_created, prediction_available = clocks.get(
                    target,
                    (
                        target + pd.Timedelta(seconds=1),
                        target
                        + pd.Timedelta(
                            seconds=DEFAULT_EMULATED_PREDICTION_LATENCY_SECONDS
                        ),
                    ),
                )
                source_quotes, target_quotes = select_historical_source_target(
                    symbol_quotes,
                    target_snapshot_for=target,
                    prediction_available_at=prediction_available,
                )
                if source_quotes.empty or target_quotes.empty:
                    raise ValueError("Required backward source or forward outcome CBBO is missing")
                source_time = pd.Timestamp(source_quotes["quote_timestamp"].max())
                target_observed_at = pd.Timestamp(target_quotes["quote_timestamp"].max())
                definitions_asof = point_in_time_definition_asof(
                    definitions.loc[definitions["symbol"].astype("string").str.upper().eq(symbol)],
                    source_time,
                )
                if definitions_asof.empty:
                    raise ValueError("No point-in-time definition existed by source surface")
                source_clock = latest_completed_bar_clock(root, symbol=symbol, as_of=source_time)
                target_clock = latest_completed_bar_clock(root, symbol=symbol, as_of=target)
                if pd.Timestamp(target_clock.decision_timestamp) != target:
                    raise ValueError("No exact completed underlying bar at OPRA target")
                source_contracts = _opra_contract_frame(
                    source_quotes,
                    definitions_asof,
                    underlying_price=completed_bar_close(source_clock),
                    target_snapshot_for=target,
                )
                target_contracts = _opra_contract_frame(
                    target_quotes,
                    definitions_asof,
                    underlying_price=completed_bar_close(target_clock),
                    target_snapshot_for=target,
                )
                frame = build_causal_samples(
                    source_contracts,
                    target_contracts=target_contracts,
                    target_underlying_price=completed_bar_close(target_clock),
                    source_snapshot_for=source_time,
                    source_available_at=source_time,
                    target_snapshot_for=target,
                    source_provider="databento-opra",
                    prediction_mode="OFFLINE",
                    observed_available_at=target_observed_at,
                    prediction_created_at=prediction_created,
                    prediction_available_at=prediction_available,
                    provider_ingested_at=loaded.get("latest_published_at"),
                    evidence_lane="OFFLINE_OPRA_STANDARD_HISTORY",
                    fallback_used=False,
                    contract_policy=contract_policy,
                    rate_observations=rate_observations,
                    datastore_root=root,
                )
                if not frame.empty:
                    samples.append(frame)
                    consumed.extend((source_clock.source_file, target_clock.source_file))
            except Exception as exc:
                errors[route] = f"{type(exc).__name__}: {exc}"
    output = pd.concat(samples, ignore_index=True, sort=False) if samples else pd.DataFrame()
    unique_files = tuple(dict.fromkeys(Path(value) for value in consumed))
    if not output.empty:
        record_consumer_usage(
            root,
            consumer="active-pricing",
            schemas=("definition", str(loaded["cbbo_schema"])),
            rows=len(output),
            source_files=source_files,
        )
    return OpraMaterialization(
        output,
        unique_files,
        errors,
        _lockbox_inventory(cbbo, locked_targets, source_files),
    )


def _canonical_inventory(
    datastore_root: Path,
    *,
    symbols: Sequence[str],
    requested_targets: Sequence[pd.Timestamp] = (),
    prediction_clocks: Mapping[
        pd.Timestamp, tuple[pd.Timestamp, pd.Timestamp]
    ] | None = None,
    allowed_cbbo_paths: Sequence[Path] | None = None,
    allowed_definition_paths: Sequence[Path] | None = None,
) -> Mapping[str, object]:
    root = Path(datastore_root).resolve()
    clean_symbols = {str(value).strip().upper() for value in symbols if str(value).strip()}
    selected_cbbo = {Path(value).resolve() for value in allowed_cbbo_paths or ()}
    selected_definitions = {Path(value).resolve() for value in allowed_definition_paths or ()}
    cbbo_schema = _preferred_cbbo_schema(
        root,
        symbols=clean_symbols,
        allowed_paths=selected_cbbo,
    )
    definitions: list[pd.DataFrame] = []
    definition_paths: list[Path] = []
    cbbo_paths: list[Path] = []
    metadata_files: list[Path] = []
    errors: dict[str, str] = {}
    published: list[pd.Timestamp] = []
    for verified in iter_verified_partitions(
        root, schemas=("definition", cbbo_schema)
    ):
        manifest = verified["manifest"]
        schema = str(manifest["schema"])
        directory = Path(verified["directory"])
        parquet = directory / str(manifest["normalized"]["path"])
        if schema == "definition" and selected_definitions and parquet.resolve() not in selected_definitions:
            continue
        if schema.startswith("cbbo-") and selected_cbbo and parquet.resolve() not in selected_cbbo:
            continue
        try:
            if schema == "definition":
                definition_paths.append(parquet)
            else:
                cbbo_paths.append(parquet)
            metadata_files.extend((directory / "manifest.json", directory / "receipt.json"))
            published.append(pd.Timestamp(manifest["published_at"]))
        except Exception as exc:
            errors[str(parquet)] = f"{type(exc).__name__}: {exc}"

    requested = tuple(sorted({_utc(value) for value in requested_targets}))
    if requested:
        definition_paths = list(
            _definition_paths_for_targets(definition_paths, requested)
        )
        cbbo_paths = list(_paths_on_target_dates(cbbo_paths, requested))

    for parquet in definition_paths:
        try:
            frame = normalize_definition_records(pd.read_parquet(parquet))
            frame = frame.loc[
                frame["symbol"].astype("string").str.upper().isin(clean_symbols)
            ]
            if not frame.empty:
                definitions.append(frame)
        except Exception as exc:
            errors[str(parquet)] = f"{type(exc).__name__}: {exc}"

    clocks = dict(prediction_clocks or {})
    available_by_path: dict[Path, pd.DatetimeIndex] = {}
    targets: set[pd.Timestamp] = set(requested)
    if requested:
        direct_times = pd.DatetimeIndex(
            _history_quote_times(
                pd.DatetimeIndex([]),
                targets=requested,
                prediction_clocks=clocks,
                assume_minute_grid=True,
            )
        )
        available_by_path = {
            parquet: direct_times for parquet in cbbo_paths
        }
    else:
        for parquet in cbbo_paths:
            try:
                available = _partition_quote_timestamps(parquet)
                available_by_path[parquet] = available
                local = available.tz_convert("America/New_York")
                mask = pd.Index(local.strftime("%H:%M")).isin(
                    ("10:00", "11:30", "13:30", "15:00")
                )
                targets.update(pd.Timestamp(value) for value in available[mask])
            except Exception as exc:
                errors[str(parquet)] = f"{type(exc).__name__}: {exc}"

    cbbo_frames: list[pd.DataFrame] = []
    selected_targets = tuple(sorted(targets))
    for parquet, available in available_by_path.items():
        selected_times = _history_quote_times(
            available,
            targets=selected_targets,
            prediction_clocks=clocks,
            assume_minute_grid=bool(requested),
        )
        if not selected_times:
            continue
        try:
            names = set(pq.ParquetFile(parquet).schema_arrow.names)
            columns = [
                column
                for column in (
                    "ts_recv",
                    "ts_event",
                    "raw_symbol",
                    "symbol",
                    "bid_px_00",
                    "bid_px",
                    "bid",
                    "ask_px_00",
                    "ask_px",
                    "ask",
                    "publisher_id",
                )
                if not names or column in names
            ]
            raw = pd.read_parquet(
                parquet,
                columns=columns,
                filters=[("ts_recv", "in", list(selected_times))],
            )
            frame = normalize_cbbo_records(raw)
            frame = frame.loc[
                frame["contract_symbol"]
                .astype("string")
                .map(_underlying)
                .isin(clean_symbols)
            ]
            frame = _compact_history_quotes(
                frame,
                targets=selected_targets,
                prediction_clocks=clocks,
            )
            if not frame.empty:
                cbbo_frames.append(frame)
        except Exception as exc:
            errors[str(parquet)] = f"{type(exc).__name__}: {exc}"

    definition_frame = (
        pd.concat(definitions, ignore_index=True, sort=False)
        .sort_values("definition_effective_at", kind="stable")
        .drop_duplicates(["contract_symbol", "definition_effective_at"], keep="last")
        .reset_index(drop=True)
        if definitions
        else pd.DataFrame()
    )
    cbbo_frame = (
        pd.concat(cbbo_frames, ignore_index=True, sort=False)
        .sort_values(["quote_timestamp", "contract_symbol"], kind="stable")
        .drop_duplicates(["quote_timestamp", "contract_symbol"], keep="last")
        .reset_index(drop=True)
        if cbbo_frames
        else pd.DataFrame()
    )
    if not cbbo_frame.empty:
        cbbo_frame["underlying_symbol"] = (
            cbbo_frame["contract_symbol"].astype("string").map(_underlying)
        )
    source_files = tuple(
        dict.fromkeys(
            (*definition_paths, *cbbo_paths, *metadata_files)
        )
    )
    return {
        "definitions": definition_frame,
        "cbbo": cbbo_frame,
        "cbbo_schema": cbbo_schema,
        "source_files": source_files,
        "errors": errors,
        "latest_published_at": max(published).isoformat() if published else None,
    }


def _preferred_cbbo_schema(
    root: Path,
    *,
    symbols: set[str],
    allowed_paths: set[Path],
) -> str:
    if allowed_paths:
        return (
            "cbbo-1m"
            if any("cbbo-1m" in path.parts for path in allowed_paths)
            else "cbbo-1s"
        )
    canonical = Path(root) / "market-data" / "databento" / "opra" / "OPRA.PILLAR"
    for schema in ("cbbo-1m", "cbbo-1s"):
        for manifest_path in canonical.glob(
            f"{schema}/*/dates/*/segments/*/manifest.json"
        ):
            try:
                bucket = manifest_path.relative_to(canonical).parts[1]
                parent = str(bucket).strip().upper().removesuffix(".OPT")
                if not symbols or parent in symbols:
                    return schema
            except (IndexError, OSError, ValueError):
                continue
    return "cbbo-1m"


def _partition_quote_timestamps(path: Path) -> pd.DatetimeIndex:
    clocks = pd.read_parquet(path, columns=["ts_recv"])
    values = clocks["ts_recv"] if "ts_recv" in clocks.columns else clocks.index
    return pd.DatetimeIndex(
        pd.to_datetime(values, utc=True, errors="coerce")
    ).dropna().unique().sort_values()


def _definition_paths_for_targets(
    paths: Sequence[Path],
    targets: Sequence[pd.Timestamp],
) -> tuple[Path, ...]:
    """Select the latest same/prior-day definition snapshot per target route."""

    by_route: dict[str, list[tuple[pd.Timestamp, Path]]] = {}
    for path in paths:
        parts = path.parts
        try:
            schema_index = parts.index("definition")
            date_index = parts.index("dates", schema_index)
            route = parts[schema_index + 1]
            date = pd.Timestamp(parts[date_index + 1], tz="UTC")
        except (ValueError, IndexError, TypeError):
            continue
        by_route.setdefault(route, []).append((date, path))
    target_dates = tuple(sorted({target.normalize() for target in targets}))
    selected: list[Path] = []
    for route_values in by_route.values():
        ordered = sorted(route_values)
        for target_date in target_dates:
            eligible = [
                path for date, path in ordered if date <= target_date
            ]
            if eligible:
                selected.append(eligible[-1])
    return tuple(dict.fromkeys(selected))


def _paths_on_target_dates(
    paths: Sequence[Path],
    targets: Sequence[pd.Timestamp],
) -> tuple[Path, ...]:
    selected_dates = {
        target.strftime("%Y-%m-%d") for target in targets
    }
    selected: list[Path] = []
    for path in paths:
        parts = path.parts
        try:
            date_index = parts.index("dates")
            date = parts[date_index + 1]
        except (ValueError, IndexError):
            continue
        if date in selected_dates:
            selected.append(path)
    return tuple(dict.fromkeys(selected))


def _history_quote_times(
    available: pd.DatetimeIndex,
    *,
    targets: Sequence[pd.Timestamp],
    prediction_clocks: Mapping[
        pd.Timestamp, tuple[pd.Timestamp, pd.Timestamp]
    ] | None = None,
    assume_minute_grid: bool = False,
) -> tuple[pd.Timestamp, ...]:
    clocks = dict(prediction_clocks or {})
    selected: set[pd.Timestamp] = set()
    for target in targets:
        lower = target - pd.Timedelta(minutes=5)
        _created, prediction_available = clocks.get(
            target,
            (
                target + pd.Timedelta(seconds=1),
                target
                + pd.Timedelta(
                    seconds=DEFAULT_EMULATED_PREDICTION_LATENCY_SECONDS
                ),
            ),
        )
        upper = prediction_available + pd.Timedelta(minutes=5)
        if assume_minute_grid:
            selected.update(
                pd.Timestamp(value)
                for value in pd.date_range(
                    lower.ceil("min"),
                    upper.floor("min"),
                    freq="1min",
                )
            )
        else:
            left = int(available.searchsorted(lower, side="left"))
            right = int(available.searchsorted(upper, side="right"))
            selected.update(pd.Timestamp(value) for value in available[left:right])
    return tuple(sorted(selected))


def _compact_history_quotes(
    frame: pd.DataFrame,
    *,
    targets: Sequence[pd.Timestamp],
    prediction_clocks: Mapping[
        pd.Timestamp, tuple[pd.Timestamp, pd.Timestamp]
    ] | None = None,
) -> pd.DataFrame:
    if frame.empty:
        return frame
    timestamps = pd.to_datetime(
        frame["quote_timestamp"], utc=True, errors="coerce"
    )
    clocks = dict(prediction_clocks or {})
    retained: list[pd.DataFrame] = []
    for target in targets:
        source = frame.loc[
            timestamps.lt(target)
            & timestamps.ge(target - pd.Timedelta(minutes=5))
        ]
        if not source.empty:
            retained.append(
                source.sort_values("quote_timestamp", kind="stable")
                .drop_duplicates("contract_symbol", keep="last")
            )
        _created, outcome_boundary = clocks.get(
            target,
            (
                target + pd.Timedelta(seconds=1),
                target
                + pd.Timedelta(
                    seconds=DEFAULT_EMULATED_PREDICTION_LATENCY_SECONDS
                ),
            ),
        )
        observed = frame.loc[
            timestamps.gt(outcome_boundary)
            & timestamps.le(outcome_boundary + pd.Timedelta(minutes=5))
        ]
        if not observed.empty:
            retained.append(
                observed.sort_values("quote_timestamp", kind="stable")
                .drop_duplicates("contract_symbol", keep="first")
            )
        marker = frame.loc[timestamps.eq(target)].head(1)
        if not marker.empty:
            retained.append(marker)
    if not retained:
        return frame.iloc[0:0].copy()
    return (
        pd.concat(retained, ignore_index=True, sort=False)
        .sort_values(["quote_timestamp", "contract_symbol"], kind="stable")
        .drop_duplicates(["quote_timestamp", "contract_symbol"], keep="last")
        .reset_index(drop=True)
    )


def _scheduled_targets(cbbo: pd.DataFrame) -> tuple[pd.Timestamp, ...]:
    if cbbo.empty:
        return ()
    timestamps = pd.to_datetime(cbbo["quote_timestamp"], utc=True, errors="coerce").dropna()
    local = timestamps.dt.tz_convert("America/New_York")
    mask = local.dt.strftime("%H:%M").isin(("10:00", "11:30", "13:30", "15:00"))
    return tuple(sorted({pd.Timestamp(value) for value in timestamps.loc[mask]}))


def _lockbox_inventory(
    cbbo: pd.DataFrame,
    locked_targets: Sequence[pd.Timestamp],
    source_files: Sequence[Path],
) -> ClosedOpraLockboxInventory:
    locked = tuple(sorted({_utc(value) for value in locked_targets}))
    counts: dict[tuple[str, str], int] = {}
    symbol_counts: dict[tuple[str, str], int] = {}
    for target in locked:
        target_rows = cbbo.loc[pd.to_datetime(cbbo["quote_timestamp"], utc=True).eq(target)]
        routes = {
            (_underlying(contract), "CALL" if _call_put(contract) == "C" else "PUT")
            for contract in target_rows.get("contract_symbol", pd.Series(dtype="string"))
            if _underlying(contract) and _call_put(contract)
        }
        for route in routes:
            counts[route] = counts.get(route, 0) + 1
            symbol_counts[route] = symbol_counts.get(route, 0) + int(
                target_rows["contract_symbol"].astype("string").map(
                    lambda value: _underlying(value) == route[0] and _call_put(value) == route[1][0]
                ).sum()
            )
    return ClosedOpraLockboxInventory(
        target_snapshot_fors=locked,
        route_cluster_counts=counts,
        route_request_symbol_counts=symbol_counts,
        output_count=len(source_files),
        outputs=tuple({"path": Path(value).as_posix()} for value in source_files),
    )


def _opra_contract_frame(
    quotes: pd.DataFrame,
    definitions: pd.DataFrame,
    *,
    underlying_price: float,
    target_snapshot_for: pd.Timestamp,
) -> pd.DataFrame:
    merged = quotes.merge(
        definitions,
        on="contract_symbol",
        how="inner",
        validate="one_to_one",
        suffixes=("", "_definition"),
    )
    output = pd.DataFrame(
        {
            "symbol": merged["symbol"].astype("string").str.upper(),
            "contract_symbol": merged["contract_symbol"],
            "call_put": merged["call_put"].astype("string").str.upper(),
            "expiration_date": merged["expiration_date"],
            "strike": merged["strike"],
            "underlying_price": float(underlying_price),
            "bid": merged["bid"],
            "ask": merged["ask"],
            "multiplier": merged["multiplier"],
            "mini": False,
            "non_standard": ~merged["standard_contract"].fillna(False).astype(bool),
            "definition_as_of": merged["definition_effective_at"],
            "exercise_style": merged.get("exercise_style", "AMBIGUOUS"),
            "settlement_type": merged.get("settlement_type", "AMBIGUOUS"),
            "settlement_reference": merged.get("settlement_reference", ""),
            "interest_rate": float("nan"),
            "dividend_yield": float("nan"),
            "implied_volatility": float("nan"),
            "quote_timestamp": merged["quote_timestamp"],
            "available_at": merged["quote_timestamp"],
            "quote_staleness_seconds": (
                target_snapshot_for
                - pd.to_datetime(merged["quote_timestamp"], utc=True, errors="coerce")
            ).dt.total_seconds().clip(lower=0),
        }
    )
    expiration = pd.to_datetime(output["expiration_date"], utc=True, errors="coerce")
    valid = (
        expiration.gt(target_snapshot_for.normalize())
        & output["strike"].gt(0)
        & output["multiplier"].eq(100)
        & ~output["non_standard"]
        & output["ask"].ge(output["bid"])
        & output["ask"].gt(0)
    )
    return output.loc[valid].reset_index(drop=True)


def _underlying(value: object) -> str:
    import re

    match = re.match(r"^([A-Z.]{1,6})\s*\d{6}[CP]", str(value).strip().upper())
    return match.group(1) if match else ""


def _call_put(value: object) -> str:
    import re

    match = re.match(r"^[A-Z.]{1,6}\s*\d{6}([CP])", str(value).strip().upper())
    return match.group(1) if match else ""


def _utc(value: object) -> pd.Timestamp:
    timestamp = pd.to_datetime(value, utc=True, errors="coerce")
    if pd.isna(timestamp):
        raise ValueError("Invalid OPRA timestamp")
    return pd.Timestamp(timestamp)


def _normalize_prediction_clocks(
    values: Mapping[object, Sequence[object]] | None,
) -> dict[pd.Timestamp, tuple[pd.Timestamp, pd.Timestamp]]:
    output: dict[pd.Timestamp, tuple[pd.Timestamp, pd.Timestamp]] = {}
    for raw_target, raw_clocks in (values or {}).items():
        if len(raw_clocks) != 2:
            raise ValueError(
                "Each OPRA prediction clock must contain created and available times"
            )
        target = _utc(raw_target)
        created = _utc(raw_clocks[0])
        available = _utc(raw_clocks[1])
        if not target < created <= available:
            raise ValueError(
                "OPRA prediction clocks must satisfy target < created <= available"
            )
        output[target] = (created, available)
    return output


__all__ = [
    "ClosedOpraLockboxInventory",
    "OpraMaterialization",
    "materialize_committed_opra_history",
    "materialize_committed_opra_history_v2",
]
