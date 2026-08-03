from __future__ import annotations

import math
from pathlib import Path
from typing import Mapping, Sequence

import pandas as pd

from datafetching.calculated_features import write_immutable_feature_partition

ENERGY_CONTEXT_NAME = "wti-or-proxy"
ENERGY_CONTEXT_CALCULATION = "energy-context"
ENERGY_CONTEXT_CALCULATION_VERSION = "1.0.0"
ENERGY_CONTEXT_SCHEMA_VERSION = "energy-context-v1"
ENERGY_INSTRUMENT_POLICY_VERSION = "fmp-clusd-direct-uso-proxy-v1"
ENERGY_RETURN_TRANSFORM_VERSION = "signed-log1p-abs-start-v1"
ENERGY_CANONICAL_INSTRUMENT = "WTI"
ENERGY_CANONICAL_SYMBOL = "CLUSD"
ENERGY_PROXY_SYMBOL = "USO"

ENERGY_CONTEXT_COLUMNS = (
    "context_name",
    "canonical_instrument",
    "canonical_symbol",
    "provider_instrument",
    "instrument_kind",
    "instrument_chain",
    "instrument_policy_version",
    "return_transform_version",
    "observed_at",
    "fetched_at",
    "available_at",
    "calculation",
    "calculation_version",
    "schema_version",
    "price",
    "wti_or_proxy_return",
    "instrument_changed",
    "chain_complete",
)
ENERGY_CONTEXT_NATURAL_KEY = (
    "canonical_instrument",
    "provider_instrument",
    "available_at",
    "calculation_version",
)


class FmpEnergyContextNotReady(ValueError):
    """Raised when persisted FMP rows cannot yet form energy context."""


class FmpEnergyContextQualityError(ValueError):
    """Raised when persisted FMP energy evidence is ambiguous or invalid."""


def normalize_fmp_quote_timestamps(
    rows: Sequence[Mapping[str, object]],
) -> list[dict[str, object]]:
    """Normalize Unix seconds and attach immutable local receipt availability."""

    normalized: list[dict[str, object]] = []
    for raw in rows:
        row = dict(raw)
        for column in ("timestamp", "fmp_timestamp"):
            if column not in row or row[column] in (None, ""):
                continue
            observed = _fmp_provider_timestamp(row[column])
            row[column] = observed.isoformat()
        if row.get("fetched_at") not in (None, ""):
            row["available_at"] = _utc_timestamp(
                row["fetched_at"],
                field="fetched_at",
            )
        normalized.append(row)
    return normalized


def calculate_fmp_energy_context(source: pd.DataFrame) -> pd.DataFrame:
    """Canonicalize CLUSD/USO identity and derive only same-chain returns."""

    if source.empty:
        raise FmpEnergyContextNotReady("Persisted FMP WTI quote history is empty")
    fetched_column = _first_column(source, ("fetched_at",))
    timestamp_column = _first_column(source, ("timestamp", "fmp_timestamp"))
    price_column = _first_column(
        source,
        ("price", "fmp_price", "last", "last_price"),
    )
    provider_column = _first_column(
        source,
        ("provider_symbol", "fmp_symbol"),
    )
    canonical_column = _first_column(source, ("symbol",))
    if (
        fetched_column is None
        or timestamp_column is None
        or price_column is None
        or provider_column is None
        or canonical_column is None
    ):
        raise FmpEnergyContextNotReady(
            "Persisted FMP energy rows require symbol, provider_symbol, "
            "timestamp, fetched_at, and price"
        )

    rows: list[dict[str, object]] = []
    for raw in source.to_dict("records"):
        canonical_symbol = str(raw.get(canonical_column) or "").strip().upper()
        if canonical_symbol != ENERGY_CANONICAL_SYMBOL:
            continue
        provider = str(raw.get(provider_column) or "").strip().upper()
        proxy_for = str(raw.get("proxy_fallback_for") or "").strip().upper()
        is_proxy = _bool_value(raw.get("is_proxy_fallback"))
        if provider == ENERGY_CANONICAL_SYMBOL and not proxy_for and not is_proxy:
            kind = "direct_commodity"
        elif (
            provider == ENERGY_PROXY_SYMBOL
            and proxy_for == ENERGY_CANONICAL_SYMBOL
            and is_proxy
        ):
            kind = "exchange_traded_proxy"
        else:
            raise FmpEnergyContextQualityError(
                "FMP WTI context has an unrecognized direct/proxy identity: "
                f"{canonical_symbol}/{provider}/{proxy_for or '-'}"
            )

        observed_at = _fmp_provider_timestamp(raw.get(timestamp_column))
        fetched_at = _utc_timestamp(
            raw.get(fetched_column),
            field="fetched_at",
        )
        if observed_at > fetched_at:
            raise FmpEnergyContextQualityError(
                "FMP provider quote timestamp is after local receipt"
            )
        price = _finite_number(raw.get(price_column), field="price")
        rows.append(
            {
                "context_name": ENERGY_CONTEXT_NAME,
                "canonical_instrument": ENERGY_CANONICAL_INSTRUMENT,
                "canonical_symbol": ENERGY_CANONICAL_SYMBOL,
                "provider_instrument": provider,
                "instrument_kind": kind,
                "instrument_chain": (
                    f"{ENERGY_CANONICAL_SYMBOL}:{provider}:{kind}"
                ),
                "instrument_policy_version": ENERGY_INSTRUMENT_POLICY_VERSION,
                "return_transform_version": ENERGY_RETURN_TRANSFORM_VERSION,
                "observed_at": observed_at,
                "fetched_at": fetched_at,
                "available_at": fetched_at,
                "calculation": ENERGY_CONTEXT_CALCULATION,
                "calculation_version": ENERGY_CONTEXT_CALCULATION_VERSION,
                "schema_version": ENERGY_CONTEXT_SCHEMA_VERSION,
                "price": price,
            }
        )

    if not rows:
        raise FmpEnergyContextNotReady(
            "Persisted FMP history contains no canonical CLUSD/USO rows"
        )
    output = pd.DataFrame(rows).sort_values(
        ["available_at", "observed_at", "provider_instrument"],
        kind="stable",
    ).reset_index(drop=True)
    if output.duplicated(["available_at"], keep=False).any():
        raise FmpEnergyContextQualityError(
            "FMP energy history contains ambiguous duplicate receipt times"
        )

    previous_identity = output["instrument_chain"].shift(1)
    previous_price = output["price"].shift(1)
    has_previous = previous_identity.notna()
    same_chain = has_previous & output["instrument_chain"].eq(previous_identity)
    output["instrument_changed"] = (
        has_previous & ~same_chain
    ).astype("boolean")
    output["chain_complete"] = same_chain.astype("boolean")
    output["wti_or_proxy_return"] = pd.Series(
        [
            _signed_log_return(float(prior), float(current))
            if complete
            else math.nan
            for prior, current, complete in zip(
                previous_price,
                output["price"],
                same_chain,
                strict=True,
            )
        ],
        dtype=float,
    )
    return output.loc[:, ENERGY_CONTEXT_COLUMNS]


def persist_fmp_energy_context(
    datastore_root: Path,
    frame: pd.DataFrame,
) -> Path:
    return write_immutable_feature_partition(
        fmp_energy_context_path(datastore_root),
        frame,
        columns=ENERGY_CONTEXT_COLUMNS,
        natural_key=ENERGY_CONTEXT_NATURAL_KEY,
    )


def materialize_fmp_energy_context(datastore_root: Path) -> Path | None:
    """Read the already-persisted CLUSD quote file and append derived rows."""

    root = Path(datastore_root)
    source_folder = (
        root
        / "pools"
        / "macro"
        / ENERGY_CANONICAL_SYMBOL
        / "quote"
        / "fmp"
        / "normalized"
    )
    source_paths = tuple(sorted(source_folder.glob("*.parquet")))
    if not source_paths:
        raise FmpEnergyContextNotReady(
            f"Persisted FMP energy source is not ready: {source_folder}"
        )
    source = pd.concat(
        [pd.read_parquet(path) for path in source_paths],
        ignore_index=True,
        sort=False,
    )
    calculated = calculate_fmp_energy_context(source)
    output_path = fmp_energy_context_path(root)
    if output_path.is_file():
        existing = pd.read_parquet(
            output_path,
            columns=list(ENERGY_CONTEXT_NATURAL_KEY),
        )
        calculated = calculated.merge(
            existing,
            on=list(ENERGY_CONTEXT_NATURAL_KEY),
            how="left",
            indicator=True,
        ).loc[lambda values: values["_merge"].eq("left_only")]
        calculated = calculated.drop(columns=["_merge"])
    if calculated.empty:
        return None
    return persist_fmp_energy_context(root, calculated)


def fmp_energy_context_path(datastore_root: Path) -> Path:
    return (
        Path(datastore_root)
        / "pools"
        / "macro"
        / "features"
        / ENERGY_CONTEXT_CALCULATION
        / "fmp"
        / "quote.parquet"
    )


def _fmp_provider_timestamp(value: object) -> pd.Timestamp:
    """Parse provider Unix seconds, including legacy nanosecond-misparsed rows."""

    if isinstance(value, str):
        text = value.strip()
        try:
            number = float(text)
        except ValueError:
            number = math.nan
        if math.isfinite(number):
            return _unix_seconds(number)
    elif isinstance(value, (int, float)) and not isinstance(value, bool):
        number = float(value)
        if math.isfinite(number):
            return _unix_seconds(number)

    parsed = pd.to_datetime(value, utc=True, errors="coerce")
    if pd.isna(parsed):
        raise FmpEnergyContextQualityError(
            "FMP energy quote has no valid provider timestamp"
        )
    timestamp = pd.Timestamp(parsed)
    # Earlier generic persistence interpreted Unix seconds as nanoseconds. In
    # that representation the Timestamp's nanosecond value is the original
    # provider seconds value, so it can be recovered without guessing a date.
    if timestamp.year == 1970 and 1_000_000_000 <= timestamp.value <= 4_000_000_000:
        return _unix_seconds(timestamp.value)
    return timestamp


def _unix_seconds(value: float | int) -> pd.Timestamp:
    parsed = pd.to_datetime(value, unit="s", utc=True, errors="coerce")
    if pd.isna(parsed):
        raise FmpEnergyContextQualityError(
            "FMP energy quote has an invalid Unix-seconds timestamp"
        )
    return pd.Timestamp(parsed)


def _signed_log_return(start: float, end: float) -> float:
    delta = end - start
    if delta == 0:
        return 0.0
    return math.copysign(
        math.log1p(abs(delta) / max(abs(start), 1e-12)),
        delta,
    )


def _first_column(
    frame: pd.DataFrame,
    candidates: Sequence[str],
) -> str | None:
    lookup = {str(column).strip().lower(): str(column) for column in frame.columns}
    return next((lookup[candidate] for candidate in candidates if candidate in lookup), None)


def _utc_timestamp(value: object, *, field: str) -> pd.Timestamp:
    parsed = pd.to_datetime(value, utc=True, errors="coerce")
    if pd.isna(parsed):
        raise FmpEnergyContextQualityError(
            f"FMP energy quote has no valid {field}"
        )
    return pd.Timestamp(parsed)


def _finite_number(value: object, *, field: str) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise FmpEnergyContextQualityError(
            f"FMP energy quote has no numeric {field}"
        ) from exc
    if not math.isfinite(number):
        raise FmpEnergyContextQualityError(
            f"FMP energy quote has no finite {field}"
        )
    return number


def _bool_value(value: object) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return value == 1
    return str(value or "").strip().lower() in {"true", "yes", "y", "1"}
