from __future__ import annotations

import math
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Mapping, Sequence

import pandas as pd

from datafetching.fmp_energy_context import fmp_energy_context_path
from ml.contracts import FeatureSet, MLContractError
from ml.datasets.families import (
    CME_FRESHNESS,
    ENERGY_FRESHNESS,
    LIFECYCLE_FRESHNESS,
    OPTION_FRESHNESS,
    QUOTE_FRESHNESS,
    load_bar_shape_features,
    load_sec_event_features,
    load_weekly_context_features,
)
from ml.datasets.point_in_time import (
    backward_asof_by_symbol,
    backward_asof_shared,
)
from ml.datasets.technical import (
    TechnicalDatasetConfig,
    assemble_technical_feature_frame,
)
from ml.feature_registry import DEFAULT_FEATURE_REGISTRY
from ml.horizons import (
    DEFAULT_HORIZON_SPECIFICATIONS,
    INTERNAL_HORIZON_ORDER,
    HorizonSpecification,
    feature_contract_horizon,
)
from ml.rolling_samples import build_rolling_samples
from ml.timing import NO_ELIGIBLE_SOURCE_DATA, utc_timestamp
from ml.universe import initial_universe_membership
from technicals.parquet_io import BarDataset, discover_bar_datasets


@dataclass(frozen=True)
class RouteMaterialization:
    symbol: str
    horizon: str
    status: str
    samples: pd.DataFrame
    source_files: tuple[Path, ...]
    error: str | None = None


@dataclass(frozen=True)
class RollingMaterialization:
    samples: pd.DataFrame
    routes: tuple[RouteMaterialization, ...]
    source_files: tuple[Path, ...]
    datastore_root: Path | None = None

    @property
    def route_statuses(self) -> dict[tuple[str, str], RouteMaterialization]:
        return {
            (route.symbol, route.horizon): route for route in self.routes
        }


def materialize_rolling_samples(
    datastore_root: Path,
    *,
    symbols: Sequence[str],
    provider: str = "databento",
    specifications: Mapping[
        str, HorizonSpecification
    ] = DEFAULT_HORIZON_SPECIFICATIONS,
    assumed_round_trip_cost: float = 0.001,
    materialized_at: object | None = None,
    reporter: Callable[[str], None] | None = print,
) -> RollingMaterialization:

    print(f"SLOWDOWN CHECK: [{int(time.time() * 1000)}] 1A: ROLLING MAT")
    root = Path(datastore_root)
    if not root.is_dir():
        raise FileNotFoundError(f"Datastore does not exist: {root}")
    clean_symbols = tuple(
        dict.fromkeys(
            str(symbol).strip().upper()
            for symbol in symbols
            if str(symbol).strip()
        )
    )
    if not clean_symbols:
        return RollingMaterialization(
            samples=pd.DataFrame(),
            routes=(),
            source_files=(),
            datastore_root=root,
        )
    clean_provider = str(provider).strip().lower()
    created = utc_timestamp(materialized_at)
    routes: list[RouteMaterialization] = []
    sample_frames: list[pd.DataFrame] = []
    all_sources: list[Path] = []
    required_bar_timeframes = {
        timeframe
        for specification in specifications.values()
        for timeframe in (
            specification.source_timeframe,
            specification.target_price_timeframe,
        )
        if timeframe is not None
    }
    bar_dataset_cache: dict[str, dict[str, BarDataset]] = {}
    bar_dataset_errors: dict[str, Exception] = {}
    source_cache: dict[
        tuple[str, str], tuple[pd.DataFrame, pd.DataFrame, BarDataset, tuple[Path, ...]]
    ] = {}
    price_frame_cache: dict[tuple[str, str], pd.DataFrame] = {}
    parquet_cache: dict[tuple[Path, ...], pd.DataFrame] = {}
    derived_cache: dict[str, pd.DataFrame] = {}

    print(f"SLOWDOWN CHECK: [{int(time.time() * 1000)}] 1B: ROLLING MAT")
    for horizon, specification in specifications.items():
        if horizon != specification.horizon:
            raise ValueError(
                "Rolling specification key must match its horizon: "
                f"{horizon!r} != {specification.horizon!r}"
            )
        print(f"SLOWDOWN CHECK: [{int(time.time() * 1000)}] 1C: ROLLING MAT")
        for symbol in clean_symbols:
            try:
                if symbol not in bar_dataset_cache:
                    if symbol in bar_dataset_errors:
                        raise bar_dataset_errors[symbol]
                    try:
                        bar_dataset_cache[symbol] = _bar_datasets(
                            root,
                            symbol=symbol,
                            provider=clean_provider,
                            timeframes=required_bar_timeframes,
                        )
                    except Exception as exc:
                        bar_dataset_errors[symbol] = exc
                        raise
                # The 4h route deliberately consumes native 1h inputs, so it
                # shares this exact cache entry with the 1h route.
                cache_key = (symbol, specification.source_timeframe)
                if cache_key not in source_cache:
                    source_cache[cache_key] = _load_operational_sources(
                        root,
                        symbol=symbol,
                        provider=clean_provider,
                        timeframe=specification.source_timeframe,
                        bars=bar_dataset_cache[symbol][
                            specification.source_timeframe
                        ],
                    )
                market_regime, breakout_pressure, bars, source_files = (
                    source_cache[cache_key]
                )
                target_timeframe = (
                    specification.target_price_timeframe
                    or specification.source_timeframe
                )
                target_provider = (
                    specification.target_price_provider or clean_provider
                )
                if target_provider != clean_provider:
                    raise MLContractError(
                        f"Rolling {horizon} target provider "
                        f"{target_provider!r} does not match requested provider "
                        f"{clean_provider!r}."
                    )
                target_bars = bar_dataset_cache[symbol][target_timeframe]
                _validate_target_price_adjustment_basis(
                    bars,
                    target_bars=target_bars,
                )
                source_files = tuple(
                    dict.fromkeys(
                        (*source_files, *target_bars.source_files)
                    )
                )
                effective_from = max(
                    _minimum_timestamp(market_regime, "timestamp"),
                    _minimum_timestamp(breakout_pressure, "timestamp"),
                )
                membership = initial_universe_membership(
                    [symbol],
                    effective_from_by_symbol={symbol: effective_from},
                )
                features = assemble_technical_feature_frame(
                    market_regime,
                    breakout_pressure,
                    membership,
                    config=TechnicalDatasetConfig(
                        feature_set=specification.feature_set,
                        required_timeframe=specification.source_timeframe,
                        processing_delay=specification.processing_delay,
                        temporal_mode=(
                            "intraday-hour"
                            if specification.source_timeframe == "1h"
                            else "daily"
                        ),
                    ),
                )
                features["horizon"] = horizon
                features, additional_sources = _attach_loop_a_features(
                    root,
                    features,
                    symbols=(symbol,),
                    horizon=horizon,
                    source_timeframe=specification.source_timeframe,
                    provider=clean_provider,
                    feature_set_name=specification.feature_set,
                    parquet_cache=parquet_cache,
                    derived_cache=derived_cache,
                )
                source_files = tuple(
                    dict.fromkeys((*source_files, *additional_sources))
                )
                source_price_key = (symbol, bars.timeframe)
                if source_price_key not in price_frame_cache:
                    price_frame_cache[source_price_key] = _price_frame(bars)
                price_frame = price_frame_cache[source_price_key]
                target_price_key = (symbol, target_bars.timeframe)
                if target_price_key not in price_frame_cache:
                    price_frame_cache[target_price_key] = _price_frame(
                        target_bars
                    )
                target_price_frame = price_frame_cache[target_price_key]
                samples = build_rolling_samples(
                    features,
                    target_price_frame,
                    specification=specification,
                    assumed_round_trip_cost=assumed_round_trip_cost,
                    materialized_at=created,
                    source_adjusted_prices=price_frame,
                )
                status = "READY" if not samples.empty else NO_ELIGIBLE_SOURCE_DATA
                route = RouteMaterialization(
                    symbol=symbol,
                    horizon=horizon,
                    status=status,
                    samples=samples,
                    source_files=source_files,
                )
                if not samples.empty:
                    sample_frames.append(samples)
                all_sources.extend(source_files)
                _report(
                    reporter,
                    f"[rolling {horizon}] {symbol}: {status}; "
                    f"samples={len(samples)}",
                )
            except Exception as exc:
                route = RouteMaterialization(
                    symbol=symbol,
                    horizon=horizon,
                    status=NO_ELIGIBLE_SOURCE_DATA,
                    samples=pd.DataFrame(),
                    source_files=(),
                    error=_safe_route_error(exc, datastore_root=root),
                )
                _report(
                    reporter,
                    f"[rolling {horizon}] {symbol}: "
                    f"{NO_ELIGIBLE_SOURCE_DATA}; {route.error}",
                )
            routes.append(route)
        print(f"SLOWDOWN CHECK: [{int(time.time() * 1000)}] 1D: ROLLING MAT")
    print(f"SLOWDOWN CHECK: [{int(time.time() * 1000)}] 1E: ROLLING MAT")

    print(f"SLOWDOWN CHECK: [{int(time.time() * 1000)}] 1F: ROLLING MAT")
    samples = (
        pd.concat(sample_frames, ignore_index=True, sort=False)
        if sample_frames
        else pd.DataFrame()
    )
    if not samples.empty:
        order = {
            horizon: index
            for index, horizon in enumerate(INTERNAL_HORIZON_ORDER)
        }
        samples["__horizon_order"] = samples["horizon"].map(order)
        if samples["__horizon_order"].isna().any():
            unknown = sorted(
                set(samples.loc[samples["__horizon_order"].isna(), "horizon"])
            )
            raise MLContractError(
                "Materialized samples contain unsupported horizons: "
                + ", ".join(str(value) for value in unknown)
            )
        samples = samples.sort_values(
            [
                "__horizon_order",
                "information_available_at",
                "symbol",
                "id",
            ],
            kind="mergesort",
        ).drop(columns="__horizon_order").reset_index(drop=True)
    print(f"SLOWDOWN CHECK: [{int(time.time() * 1000)}] 1G: ROLLING MAT")
    return RollingMaterialization(
        samples=samples,
        routes=tuple(routes),
        source_files=tuple(dict.fromkeys(all_sources)),
        datastore_root=root,
    )


def _load_operational_sources(
    root: Path,
    *,
    symbol: str,
    provider: str,
    timeframe: str,
    bars: BarDataset,
) -> tuple[pd.DataFrame, pd.DataFrame, BarDataset, tuple[Path, ...]]:
    market_regime_path = _technical_path(
        root,
        symbol=symbol,
        calculation="market-regime",
        provider=provider,
        timeframe=timeframe,
    )
    breakout_path = _technical_path(
        root,
        symbol=symbol,
        calculation="breakout-pressure",
        provider=provider,
        timeframe=timeframe,
    )
    market_regime = _read_required_parquet(market_regime_path)
    breakout_pressure = _read_required_parquet(breakout_path)
    if bars.provider != provider or bars.timeframe != timeframe:
        raise ValueError(
            "Preloaded bar dataset does not match the operational source: "
            f"expected {provider}/{timeframe}, observed "
            f"{bars.provider}/{bars.timeframe}."
        )
    _validate_price_adjustment_basis(
        market_regime,
        breakout_pressure,
        bars=bars,
    )
    return (
        market_regime,
        breakout_pressure,
        bars,
        tuple(dict.fromkeys((market_regime_path, breakout_path, *bars.source_files))),
    )


def _attach_loop_a_features(
    root: Path,
    decisions: pd.DataFrame,
    *,
    symbols: Sequence[str],
    horizon: str,
    source_timeframe: str,
    provider: str,
    feature_set_name: str,
    parquet_cache: dict[tuple[Path, ...], pd.DataFrame],
    derived_cache: dict[str, pd.DataFrame],
) -> tuple[pd.DataFrame, tuple[Path, ...]]:
    feature_horizon = feature_contract_horizon(horizon)
    feature_set = DEFAULT_FEATURE_REGISTRY.feature_set(
        feature_set_name,
        require_active=True,
        horizon=feature_horizon,
    )
    output = decisions.copy()
    source_files: list[Path] = []

    if mapping := _family_values(feature_set, "bar"):
        paths = tuple(
            root
            / "stocks"
            / symbol
            / "technicals"
            / "bar-shape"
            / provider
            / f"{source_timeframe}.parquet"
            for symbol in symbols
        )
        source = _read_required_sources(
            paths,
            family="technical bar-shape",
            cache=parquet_cache,
        )
        output = load_bar_shape_features(
            output,
            source,
            value_columns=mapping,
        )
        source_files.extend(paths)

    if mapping := _family_values(feature_set, "weekly"):
        paths = tuple(
            root
            / "stocks"
            / symbol
            / "technicals"
            / "weekly-context"
            / provider
            / "1w.parquet"
            for symbol in symbols
        )
        source = _read_required_sources(
            paths,
            family="technical weekly-context",
            cache=parquet_cache,
        )
        output = load_weekly_context_features(
            output,
            source,
            value_columns=mapping,
            freshness=pd.Timedelta(days=8),
        )
        source_files.extend(paths)

    if mapping := _family_values(feature_set, "life"):
        paths = tuple(
            root
            / "stocks"
            / symbol
            / "signals"
            / "technical-lifecycle"
            / "consensus"
            / "daily.parquet"
            for symbol in symbols
        )
        source = _read_required_sources(
            paths,
            family="technical lifecycle",
            cache=parquet_cache,
        )
        output = _join_symbol_values(
            output,
            source,
            family="life",
            value_columns=mapping,
            freshness=LIFECYCLE_FRESHNESS[feature_horizon],
        )
        source_files.extend(paths)

    if mapping := _family_values(feature_set, "fdir"):
        paths = _partitioned_stock_paths(
            root,
            symbols,
            ("fundamentals", "fundamental-direction", "fmp"),
        )
        source = _read_required_sources(
            paths,
            family="FMP fundamental-direction",
            cache=parquet_cache,
        )
        output = _join_symbol_values(
            output,
            source,
            family="fdir",
            value_columns=mapping,
            available_column="effective_from",
            tie_breakers=("period_end_date", "period_type"),
        )
        source_files.extend(paths)

    if mapping := _family_values(feature_set, "fund"):
        paths = _partitioned_stock_paths(
            root,
            symbols,
            ("fundamentals", "point-in-time", "fmp"),
        )
        source = _read_required_sources(
            paths,
            family="FMP point-in-time fundamentals",
            cache=parquet_cache,
        )
        output = _join_symbol_values(
            output,
            source,
            family="fund",
            value_columns=mapping,
            tie_breakers=("period_end_date", "period_type"),
        )
        source_files.extend(paths)

    if mapping := _family_values(feature_set, "ftlife"):
        paths = tuple(
            root
            / "stocks"
            / symbol
            / "signals"
            / "fundamental-technical-lifecycle"
            / "consensus"
            / "daily.parquet"
            for symbol in symbols
        )
        source = _read_required_sources(
            paths,
            family="fundamental-technical lifecycle",
            cache=parquet_cache,
        )
        output = _join_symbol_values(
            output,
            source,
            family="ftlife",
            value_columns=mapping,
            available_column="timestamp",
            freshness=LIFECYCLE_FRESHNESS[feature_horizon],
        )
        source_files.extend(paths)

    if mapping := _family_values(feature_set, "quote"):
        paths = _stock_glob_paths(
            root,
            symbols,
            ("quotes", "features", "quote-liquidity", "schwab"),
        )
        source = _read_required_sources(
            paths,
            family="Schwab quote-liquidity",
            cache=parquet_cache,
        )
        output = _join_symbol_values(
            output,
            source,
            family="quote",
            value_columns=mapping,
            freshness=QUOTE_FRESHNESS[feature_horizon],
        )
        source_files.extend(paths)

    if mapping := _family_values(feature_set, "opt"):
        paths = _stock_glob_paths(
            root,
            symbols,
            ("options", "features", "option-quality", "schwab"),
        )
        source = _read_required_sources(
            paths,
            family="Schwab option-quality",
            cache=parquet_cache,
        )
        output = _join_symbol_values(
            output,
            source,
            family="opt",
            value_columns=mapping,
            tie_breakers=("snapshot_for",),
            freshness=OPTION_FRESHNESS[feature_horizon],
        )
        source_files.extend(paths)

    if mapping := _family_values(feature_set, "energy"):
        path = fmp_energy_context_path(root)
        source = _read_required_sources(
            (path,),
            family="FMP energy-context",
            cache=parquet_cache,
        )
        output = _join_shared_values(
            output,
            source,
            family="energy",
            value_columns=mapping,
            freshness=ENERGY_FRESHNESS[feature_horizon],
        )
        source_files.append(path)

    if mapping := _family_values(feature_set, "macro"):
        paths = _fred_source_paths(root)
        cache_key = "fred-current-context"
        if cache_key not in derived_cache:
            sources = _read_required_sources(
                paths,
                family="FRED macro",
                cache=parquet_cache,
            )
            derived_cache[cache_key] = _derive_current_fred_context(sources)
        output = _join_shared_values(
            output,
            derived_cache[cache_key],
            family="macro",
            value_columns=mapping,
            freshness=pd.Timedelta(days=120),
        )
        source_files.extend(paths)

    if mapping := _family_values(feature_set, "sec"):
        paths = _stock_glob_paths(
            root,
            symbols,
            ("corporate", "sec-events", "sec"),
        )
        source = _read_required_sources(
            paths,
            family="SEC event",
            cache=parquet_cache,
        )
        output = load_sec_event_features(
            output,
            source,
            value_columns=mapping,
            freshness=None,
        )
        source_files.extend(paths)

    if mapping := _family_values(feature_set, "cme"):
        derived_paths = tuple(
            sorted(
                (
                    root
                    / "pools"
                    / "cme"
                    / "features"
                    / "cross-asset-context"
                    / "databento"
                ).glob("*.parquet")
            )
        )
        if derived_paths:
            source = _read_required_sources(
                derived_paths,
                family="Databento CME cross-asset context",
                cache=parquet_cache,
            )
            cme_paths = derived_paths
        else:
            cme_paths = _cme_normalized_source_paths(root)
            cache_key = "cme-current-context"
            if cache_key not in derived_cache:
                source_frames = [
                    _read_required_sources(
                        (path,),
                        family=f"Databento CME {path.parent.parent.parent.name}",
                        cache=parquet_cache,
                    )
                    for path in cme_paths
                ]
                derived_cache[cache_key] = _derive_current_cme_context(
                    *source_frames
                )
            source = derived_cache[cache_key]
        output = _join_shared_values(
            output,
            source,
            family="cme",
            value_columns=mapping,
            freshness=CME_FRESHNESS[feature_horizon],
        )
        source_files.extend(cme_paths)

    missing = [
        feature.name
        for feature in feature_set.features
        if feature.name not in output.columns
    ]
    if missing:
        raise MLContractError(
            "Assembled model matrix is missing required Loop A features: "
            + ", ".join(missing)
        )
    return output, tuple(dict.fromkeys(source_files))


def _family_values(
    feature_set: FeatureSet,
    family: str,
) -> dict[str, str]:
    return {
        feature.name: feature.source_column
        for feature in feature_set.for_family(family)
    }


def _read_required_sources(
    paths: Sequence[Path],
    *,
    family: str,
    cache: dict[tuple[Path, ...], pd.DataFrame],
) -> pd.DataFrame:
    ordered = tuple(dict.fromkeys(Path(path) for path in paths))
    missing = [path for path in ordered if not path.is_file()]
    if missing:
        rendered = ", ".join(str(path) for path in missing)
        raise FileNotFoundError(
            f"Required {family} Parquet input is absent: {rendered}"
        )
    if not ordered:
        raise FileNotFoundError(f"Required {family} Parquet input is absent")
    if ordered not in cache:
        frames = [pd.read_parquet(path) for path in ordered]
        combined = pd.concat(frames, ignore_index=True, sort=False)
        if combined.empty:
            raise ValueError(f"Required {family} Parquet input is empty")
        cache[ordered] = combined
    return cache[ordered].copy()


def _join_symbol_values(
    decisions: pd.DataFrame,
    source: pd.DataFrame,
    *,
    family: str,
    value_columns: Mapping[str, str],
    available_column: str = "available_at",
    tie_breakers: Sequence[str] = (),
    freshness: pd.Timedelta | None = None,
) -> pd.DataFrame:
    required = {"symbol", available_column, *value_columns.values()}
    missing = sorted(required.difference(source.columns))
    if missing:
        raise MLContractError(
            f"{family} source is missing required model columns: "
            + ", ".join(missing)
        )
    prepared = source.copy()
    prepared["symbol"] = prepared["symbol"].astype("string").str.upper()
    prepared["available_at"] = pd.to_datetime(
        prepared[available_column],
        utc=True,
        errors="coerce",
    )
    prepared = prepared.dropna(subset=["symbol", "available_at"])
    requested_symbols = {
        str(symbol).strip().upper()
        for symbol in decisions["symbol"].dropna()
    }
    prepared = prepared.loc[
        prepared["symbol"].isin(requested_symbols)
    ].copy()
    order = [
        "symbol",
        "available_at",
        *(
            column
            for column in tie_breakers
            if column in prepared.columns
        ),
    ]
    prepared = (
        prepared.sort_values(order, kind="mergesort")
        .drop_duplicates(["symbol", "available_at"], keep="last")
        .reset_index(drop=True)
    )
    _require_family_value(
        prepared,
        value_columns=value_columns,
        family=family,
    )
    return backward_asof_by_symbol(
        decisions,
        prepared,
        family=family,
        value_columns=value_columns,
        freshness=freshness,
        natural_key_columns=("symbol", "available_at"),
    )


def _join_shared_values(
    decisions: pd.DataFrame,
    source: pd.DataFrame,
    *,
    family: str,
    value_columns: Mapping[str, str],
    freshness: pd.Timedelta | None = None,
) -> pd.DataFrame:
    required = {"available_at", *value_columns.values()}
    missing = sorted(required.difference(source.columns))
    if missing:
        raise MLContractError(
            f"{family} source is missing required model columns: "
            + ", ".join(missing)
        )
    prepared = source.copy()
    prepared["available_at"] = pd.to_datetime(
        prepared["available_at"],
        utc=True,
        errors="coerce",
    )
    prepared = (
        prepared.dropna(subset=["available_at"])
        .sort_values("available_at", kind="mergesort")
        .drop_duplicates("available_at", keep="last")
        .reset_index(drop=True)
    )
    _require_family_value(
        prepared,
        value_columns=value_columns,
        family=family,
    )
    return backward_asof_shared(
        decisions,
        prepared,
        family=family,
        value_columns=value_columns,
        freshness=freshness,
        natural_key_columns=("available_at",),
    )


def _require_family_value(
    source: pd.DataFrame,
    *,
    value_columns: Mapping[str, str],
    family: str,
) -> None:
    numeric = source.loc[:, list(value_columns.values())].apply(
        pd.to_numeric,
        errors="coerce",
    )
    if not numeric.notna().any(axis=None):
        raise ValueError(
            f"Required {family} feature family has no populated numeric values"
        )


def _partitioned_stock_paths(
    root: Path,
    symbols: Sequence[str],
    parts: Sequence[str],
) -> tuple[Path, ...]:
    paths: list[Path] = []
    missing: list[Path] = []
    for symbol in symbols:
        directory = (root / "stocks" / symbol).joinpath(*parts)
        matches = sorted(directory.glob("*.parquet"))
        if matches:
            paths.extend(matches)
        else:
            missing.append(directory)
    if missing:
        raise FileNotFoundError(
            "Required stock feature Parquet input is absent under: "
            + ", ".join(str(path) for path in missing)
        )
    return tuple(paths)


def _stock_glob_paths(
    root: Path,
    symbols: Sequence[str],
    parts: Sequence[str],
) -> tuple[Path, ...]:
    return _partitioned_stock_paths(root, symbols, parts)


def _fred_source_paths(root: Path) -> tuple[Path, ...]:
    locations = (
        ("FEDERALFUNDS", "FEDFUNDS", "FEDERALFUNDS_FEDFUNDS.parquet"),
        ("CPI", "CPIAUCSL", "CPI_CPIAUCSL.parquet"),
        ("UNEMPLOYMENTRATE", "UNRATE", "UNEMPLOYMENTRATE_UNRATE.parquet"),
        ("GDP", "GDP", "GDP_GDP.parquet"),
    )
    return tuple(
        root
        / "pools"
        / "macro"
        / group
        / series
        / "fred"
        / "normalized"
        / filename
        for group, series, filename in locations
    )


def _derive_current_fred_context(source: pd.DataFrame) -> pd.DataFrame:
    required = {"series", "date", "value", "fetched_at"}
    missing = sorted(required.difference(source.columns))
    if missing:
        raise MLContractError(
            "FRED normalized inputs are missing columns: " + ", ".join(missing)
        )
    prepared = source.copy()
    prepared["series"] = prepared["series"].astype("string").str.upper()
    prepared["date"] = pd.to_datetime(
        prepared["date"],
        utc=True,
        errors="coerce",
    )
    prepared["fetched_at"] = pd.to_datetime(
        prepared["fetched_at"],
        utc=True,
        errors="coerce",
    )
    prepared["value"] = pd.to_numeric(prepared["value"], errors="coerce")
    prepared = prepared.dropna(
        subset=["series", "date", "fetched_at", "value"]
    )

    selected_rows: list[pd.Series] = []

    def latest(series: str) -> pd.Series:
        values = prepared.loc[prepared["series"].eq(series)].sort_values("date")
        if values.empty:
            raise ValueError(f"Required FRED series is absent: {series}")
        row = values.iloc[-1]
        selected_rows.append(row)
        return row

    def prior(series: str, date: pd.Timestamp, months: int) -> pd.Series:
        cutoff = date - pd.DateOffset(months=months)
        values = prepared.loc[
            prepared["series"].eq(series)
            & prepared["date"].le(cutoff)
        ].sort_values("date")
        if values.empty:
            raise ValueError(
                f"Required FRED lag is absent: {series} at {months} months"
            )
        row = values.iloc[-1]
        selected_rows.append(row)
        return row

    fed = latest("FEDFUNDS")
    cpi = latest("CPIAUCSL")
    cpi_prior = prior("CPIAUCSL", pd.Timestamp(cpi["date"]), 12)
    unemployment = latest("UNRATE")
    unemployment_prior = prior(
        "UNRATE",
        pd.Timestamp(unemployment["date"]),
        1,
    )
    gdp = latest("GDP")
    gdp_prior = prior("GDP", pd.Timestamp(gdp["date"]), 12)
    available_at = max(pd.Timestamp(row["fetched_at"]) for row in selected_rows)
    return pd.DataFrame(
        [
            {
                "available_at": available_at,
                "macro__fed_funds_level": float(fed["value"]),
                "macro__cpi_yoy": (
                    float(cpi["value"]) / float(cpi_prior["value"]) - 1.0
                ),
                "macro__unemployment_change": (
                    float(unemployment["value"])
                    - float(unemployment_prior["value"])
                ),
                "macro__gdp_yoy": (
                    float(gdp["value"]) / float(gdp_prior["value"]) - 1.0
                ),
            }
        ]
    )


def _cme_normalized_source_paths(root: Path) -> tuple[Path, ...]:
    datasets = (
        "cme_context_ohlcv-1m",
        "cme_context_bbo-1m",
        "cme_context_mbp-10",
    )
    paths: list[Path] = []
    for dataset in datasets:
        canonical = (
            root
            / "pools"
            / "cme"
            / "CME_CONTEXT"
            / dataset
            / "databento"
            / "normalized"
            / f"CME_CONTEXT_{dataset}.parquet"
        )
        status = (
            root
            / "pools"
            / "cme"
            / "CME_CONTEXT"
            / f"{dataset}_status"
            / "databento"
            / "normalized"
            / f"CME_CONTEXT_{dataset}_status.parquet"
        )
        paths.append(
            canonical
            if canonical.is_file() or not status.is_file()
            else status
        )
    return tuple(paths)


_CME_ROOT = re.compile(r"^(RTY|NQ|ES|GC|CL)(?:$|[.\-_/])", re.IGNORECASE)
_CME_ROOTS = ("NQ", "ES", "RTY", "GC", "CL")


def _derive_current_cme_context(
    ohlcv: pd.DataFrame,
    bbo: pd.DataFrame,
    mbp: pd.DataFrame,
) -> pd.DataFrame:
    bars = _prepare_cme_rows(ohlcv)
    windows: dict[str, dict[pd.Timestamp, pd.DataFrame]] = {}
    for root in _CME_ROOTS:
        root_windows: dict[pd.Timestamp, pd.DataFrame] = {}
        root_bars = bars.loc[bars["_cme_root"].eq(root)]
        for start, group in root_bars.groupby(
            root_bars["timestamp"].dt.floor("1h"),
            sort=True,
        ):
            ordered = group.sort_values("timestamp", kind="mergesort")
            expected_start = pd.Timestamp(start)
            expected_end = expected_start + pd.Timedelta(minutes=59)
            if (
                ordered["timestamp"].iloc[0] == expected_start
                and ordered["timestamp"].iloc[-1] == expected_end
            ):
                root_windows[pd.Timestamp(start)] = ordered
        windows[root] = root_windows
    common = set.intersection(
        *(set(windows[root]) for root in _CME_ROOTS)
    )
    latest_receipt = bars["fetched_at"].max()
    common = {
        start
        for start in common
        if start + pd.Timedelta(hours=1) <= latest_receipt
    }
    if not common:
        raise ValueError(
            "Required Databento CME inputs have no causal common one-hour "
            "endpoint window for NQ, ES, RTY, GC, and CL"
        )
    window_start = max(common)
    window_end = window_start + pd.Timedelta(hours=1)
    by_root = {
        root: windows[root][window_start] for root in _CME_ROOTS
    }

    returns: dict[str, float] = {}
    evidence = list(by_root.values())
    for root, frame in by_root.items():
        start = float(pd.to_numeric(frame.iloc[0]["open"], errors="raise"))
        end = float(pd.to_numeric(frame.iloc[-1]["close"], errors="raise"))
        if root == "CL":
            delta = end - start
            returns[root] = (
                0.0
                if delta == 0.0
                else math.copysign(
                    math.log1p(abs(delta) / max(abs(start), 1e-12)),
                    delta,
                )
            )
        else:
            if start <= 0.0 or end <= 0.0:
                raise ValueError(
                    f"Required CME {root} return has nonpositive endpoints"
                )
            returns[root] = math.log(end / start)

    quotes = _prepare_cme_rows(bbo)
    quote_window = quotes.loc[
        quotes["timestamp"].ge(window_start)
        & quotes["timestamp"].lt(window_end)
    ]
    spreads: list[float] = []
    quote_evidence: list[pd.DataFrame] = []
    for root in _CME_ROOTS:
        rows = quote_window.loc[
            quote_window["_cme_root"].eq(root)
        ].sort_values("timestamp")
        if rows.empty:
            spreads = []
            break
        row = rows.tail(1)
        bid = float(pd.to_numeric(row["bid_px_00"].iloc[0], errors="coerce"))
        ask = float(pd.to_numeric(row["ask_px_00"].iloc[0], errors="coerce"))
        if not (math.isfinite(bid) and math.isfinite(ask) and 0 < bid < ask):
            spreads = []
            break
        spreads.append((ask - bid) / ((ask + bid) / 2.0))
        quote_evidence.append(row)
    relative_spread = (
        sum(spreads) / len(spreads) if len(spreads) == len(_CME_ROOTS) else None
    )
    evidence.extend(quote_evidence)

    book = _prepare_cme_rows(mbp)
    book_window = book.loc[
        book["timestamp"].ge(window_end - pd.Timedelta(minutes=15))
        & book["timestamp"].lt(window_end)
    ]
    bid_columns = [
        column for column in book.columns if re.fullmatch(r"bid_sz_\d{2}", column)
    ]
    ask_columns = [
        column for column in book.columns if re.fullmatch(r"ask_sz_\d{2}", column)
    ]
    imbalances: list[float] = []
    book_evidence: list[pd.DataFrame] = []
    for root in _CME_ROOTS:
        rows = book_window.loc[
            book_window["_cme_root"].eq(root)
        ].sort_values("timestamp")
        if rows.empty or not bid_columns or not ask_columns:
            imbalances = []
            break
        row = rows.tail(1)
        bid_size = float(
            row[bid_columns].apply(pd.to_numeric, errors="coerce").sum(axis=1).iloc[0]
        )
        ask_size = float(
            row[ask_columns].apply(pd.to_numeric, errors="coerce").sum(axis=1).iloc[0]
        )
        total = bid_size + ask_size
        if not math.isfinite(total) or total <= 0:
            imbalances = []
            break
        imbalances.append((bid_size - ask_size) / total)
        book_evidence.append(row)
    book_imbalance = (
        sum(imbalances) / len(imbalances)
        if len(imbalances) == len(_CME_ROOTS)
        else None
    )
    evidence.extend(book_evidence)

    receipts = pd.concat(evidence, ignore_index=True, sort=False)
    fetched = pd.to_datetime(
        receipts["fetched_at"],
        utc=True,
        errors="coerce",
    ).dropna()
    if fetched.empty:
        raise ValueError("Required Databento CME inputs lack receipt timestamps")
    return pd.DataFrame(
        [
            {
                "available_at": fetched.max(),
                "window_start": window_start,
                "window_end": window_end,
                "nq_return": returns["NQ"],
                "es_return": returns["ES"],
                "rty_minus_es_return": returns["RTY"] - returns["ES"],
                "nq_minus_es_return": returns["NQ"] - returns["ES"],
                "gold_return": returns["GC"],
                "crude_return": returns["CL"],
                "relative_spread": relative_spread,
                "book_imbalance": book_imbalance,
            }
        ]
    )


def _prepare_cme_rows(frame: pd.DataFrame) -> pd.DataFrame:
    required = {"symbol", "timestamp", "fetched_at"}
    missing = sorted(required.difference(frame.columns))
    if missing:
        raise MLContractError(
            "Databento CME input is missing columns: " + ", ".join(missing)
        )
    prepared = frame.copy()
    prepared["timestamp"] = pd.to_datetime(
        prepared["timestamp"],
        utc=True,
        errors="coerce",
    )
    prepared["fetched_at"] = pd.to_datetime(
        prepared["fetched_at"],
        utc=True,
        errors="coerce",
    )
    prepared["_cme_root"] = prepared["symbol"].map(
        lambda value: (
            match.group(1).upper()
            if (match := _CME_ROOT.match(str(value or "").strip()))
            else None
        )
    )
    return prepared.dropna(
        subset=["timestamp", "fetched_at", "_cme_root"]
    ).copy()


def _bar_dataset(
    root: Path,
    *,
    symbol: str,
    provider: str,
    timeframe: str,
) -> BarDataset:
    return _bar_datasets(
        root,
        symbol=symbol,
        provider=provider,
        timeframes={timeframe},
    )[timeframe]


def _bar_datasets(
    root: Path,
    *,
    symbol: str,
    provider: str,
    timeframes: set[str],
) -> dict[str, BarDataset]:
    required = {
        str(timeframe).strip().lower()
        for timeframe in timeframes
        if str(timeframe).strip()
    }
    discovered = discover_bar_datasets(
        root,
        symbol=symbol,
        providers=(provider,),
        timeframes=required,
    )
    result: dict[str, BarDataset] = {}
    counts: dict[str, int] = {}
    for dataset in discovered:
        if dataset.provider != provider or dataset.timeframe not in required:
            continue
        counts[dataset.timeframe] = counts.get(dataset.timeframe, 0) + 1
        result[dataset.timeframe] = dataset
    invalid = {
        timeframe: counts.get(timeframe, 0)
        for timeframe in required
        if counts.get(timeframe, 0) != 1
    }
    if invalid:
        details = ", ".join(
            f"{timeframe}={count}"
            for timeframe, count in sorted(invalid.items())
        )
        raise ValueError(
            f"Expected one adjusted bar dataset per required timeframe for "
            f"{symbol}/{provider}; observed {details}."
        )
    return result


def _price_frame(dataset: BarDataset) -> pd.DataFrame:
    frame = dataset.frame.copy()
    frame["symbol"] = dataset.symbol
    frame["provider"] = dataset.provider
    frame["timeframe"] = dataset.timeframe
    frame["price_adjustment_status"] = dataset.adjustment_status
    frame["split_event_count"] = dataset.split_event_count
    return frame


def _validate_price_adjustment_basis(
    market_regime: pd.DataFrame,
    breakout_pressure: pd.DataFrame,
    *,
    bars: BarDataset,
) -> None:
    expected_status = str(bars.adjustment_status)
    expected_split_count = int(bars.split_event_count)
    for name, frame in (
        ("market-regime", market_regime),
        ("breakout-pressure", breakout_pressure),
    ):
        required = {"price_adjustment_status", "split_event_count"}
        missing = sorted(required.difference(frame.columns))
        if missing:
            raise ValueError(
                f"{name} is missing price-adjustment columns: "
                + ", ".join(missing)
            )
        statuses = set(
            frame["price_adjustment_status"].dropna().astype(str)
        )
        split_counts = set(
            pd.to_numeric(
                frame["split_event_count"],
                errors="coerce",
            ).dropna().astype(int)
        )
        if statuses != {expected_status}:
            raise ValueError(
                f"{name} price adjustment status does not match current bars: "
                f"features={sorted(statuses)}, bars={expected_status}"
            )
        if split_counts != {expected_split_count}:
            raise ValueError(
                f"{name} split event count does not match current bars: "
                f"features={sorted(split_counts)}, bars={expected_split_count}"
            )


def _validate_target_price_adjustment_basis(
    source_bars: BarDataset,
    *,
    target_bars: BarDataset,
) -> None:
    if (
        target_bars.adjustment_status != source_bars.adjustment_status
        or target_bars.split_event_count != source_bars.split_event_count
        or target_bars.split_events_json != source_bars.split_events_json
    ):
        raise ValueError(
            "Target-price adjustment basis does not match the native source "
            f"bars: source={source_bars.adjustment_status}/"
            f"{source_bars.split_event_count}, target="
            f"{target_bars.adjustment_status}/{target_bars.split_event_count}."
        )


def _technical_path(
    root: Path,
    *,
    symbol: str,
    calculation: str,
    provider: str,
    timeframe: str,
) -> Path:
    return (
        root
        / "stocks"
        / symbol
        / "technicals"
        / calculation
        / provider
        / f"{timeframe}.parquet"
    )


def _read_required_parquet(path: Path) -> pd.DataFrame:
    if not path.is_file():
        raise FileNotFoundError(f"Required Parquet does not exist: {path}")
    frame = pd.read_parquet(path)
    if frame.empty:
        raise ValueError(f"Required Parquet is empty: {path}")
    return frame


def _minimum_timestamp(frame: pd.DataFrame, column: str) -> pd.Timestamp:
    if column not in frame:
        raise ValueError(f"Required timestamp column is missing: {column}")
    values = pd.to_datetime(frame[column], utc=True, errors="coerce").dropna()
    if values.empty:
        raise ValueError(f"No valid timestamps exist in {column}")
    return values.min()


def _report(
    reporter: Callable[[str], None] | None,
    message: str,
) -> None:
    if reporter is not None:
        reporter(message)


def _safe_route_error(
    error: Exception,
    *,
    datastore_root: Path,
) -> str:
    message = f"{type(error).__name__}: {error}"
    candidates = {
        str(Path(datastore_root)),
        str(Path(datastore_root).resolve()),
    }
    for candidate in sorted(candidates, key=len, reverse=True):
        if candidate:
            message = message.replace(candidate, "DATASTORE")
    return message
