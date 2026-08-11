from __future__ import annotations

from pathlib import Path
from typing import Mapping, Sequence

import pandas as pd

from datafetching.calculated_features import write_immutable_feature_partition
from datafetching.layout import safe_token

FRED_VINTAGE_SCHEMA_VERSION = "fred-vintage-v2"
ALFRED_VINTAGE_AVAILABILITY_BASIS = (
    "ALFRED_REALTIME_START_DATE_END_OF_DAY_AMERICA_CHICAGO_V1"
)
LOCAL_RECEIPT_AVAILABILITY_BASIS = "LOCAL_ACQUISITION_MAX_PROVIDER_V1"
MACRO_CALCULATION = "macro-release-context"
MACRO_CALCULATION_VERSION = "1.0.0"
MACRO_SCHEMA_VERSION = "macro-release-context-v1"
CURRENT_RATE_CONTEXT_NAME = "fred-current-receipt-rate"
CURRENT_RATE_CALCULATION = "macro-current-receipt-rate"

FRED_VINTAGE_COLUMNS = (
    "series_name",
    "observation_date",
    "realtime_start",
    "realtime_end",
    "release_at",
    "release_time_precision",
    "fetched_at",
    "available_at",
    "availability_basis",
    "value",
    "unit",
    "frequency",
    "schema_version",
)
FRED_VINTAGE_NATURAL_KEY = (
    "series_name",
    "observation_date",
    "realtime_start",
)
MACRO_FEATURE_COLUMNS = (
    "context_name",
    "available_at",
    "calculation",
    "calculation_version",
    "schema_version",
    "fed_funds_available_at",
    "cpi_available_at",
    "unemployment_available_at",
    "gdp_available_at",
    "macro__fed_funds_level",
    "macro__cpi_yoy",
    "macro__unemployment_change",
    "macro__gdp_yoy",
)

_SERIES_ALIASES = {
    "GDP": "GDP",
    "CPI": "CPIAUCSL",
    "CPIAUCSL": "CPIAUCSL",
    "UNEMPLOYMENTRATE": "UNRATE",
    "UNRATE": "UNRATE",
    "FEDERALFUNDS": "FEDFUNDS",
    "FEDFUNDS": "FEDFUNDS",
}


def normalize_fred_vintage_rows(
    rows: Sequence[Mapping[str, object]] | pd.DataFrame,
) -> pd.DataFrame:
    """Validate actual release/vintage identity; current revised rows fail closed."""

    frame = rows.copy() if isinstance(rows, pd.DataFrame) else pd.DataFrame(rows)
    aliases = {
        "series": "series_name",
        "date": "observation_date",
        "realtimeStart": "realtime_start",
        "realtimeEnd": "realtime_end",
        "releaseDate": "release_at",
        "fetchedAt": "fetched_at",
    }
    frame = frame.rename(
        columns={
            source: target
            for source, target in aliases.items()
            if source in frame and target not in frame
        }
    )
    required = {
        "series_name",
        "observation_date",
        "realtime_start",
        "release_at",
        "fetched_at",
        "value",
    }
    missing = sorted(required.difference(frame.columns))
    if missing:
        raise ValueError(
            "Current revised FRED history is not a point-in-time vintage source; "
            "missing: "
            + ", ".join(missing)
        )
    output = pd.DataFrame(index=frame.index)
    output["series_name"] = frame["series_name"].map(_canonical_series)
    for column in (
        "observation_date",
        "realtime_start",
        "realtime_end",
        "release_at",
        "fetched_at",
    ):
        source = (
            frame[column]
            if column in frame
            else pd.Series(pd.NaT, index=frame.index)
        )
        output[column] = pd.to_datetime(source, utc=True, errors="coerce")
    output["realtime_end"] = output["realtime_end"].fillna(
        pd.Timestamp.max.tz_localize("UTC")
    )
    default_available_at = pd.concat(
        [output["release_at"], output["fetched_at"]],
        axis=1,
    ).max(axis=1)
    basis_source = (
        frame["availability_basis"]
        if "availability_basis" in frame
        else pd.Series(LOCAL_RECEIPT_AVAILABILITY_BASIS, index=frame.index)
    )
    output["availability_basis"] = basis_source.astype("string").str.strip()
    precision_source = (
        frame["release_time_precision"]
        if "release_time_precision" in frame
        else pd.Series("TIMESTAMP", index=frame.index)
    )
    output["release_time_precision"] = (
        precision_source.astype("string").str.strip().str.upper()
    )
    alfred_vintage = output["availability_basis"].eq(
        ALFRED_VINTAGE_AVAILABILITY_BASIS
    )
    output["available_at"] = default_available_at
    output.loc[alfred_vintage, "available_at"] = output.loc[
        alfred_vintage, "release_at"
    ]
    output["value"] = pd.to_numeric(frame["value"], errors="coerce")
    output["unit"] = (
        frame["unit"]
        if "unit" in frame
        else pd.Series("", index=frame.index)
    ).astype("string")
    frequency_source = (
        frame["frequency"]
        if "frequency" in frame
        else frame["cadence"]
        if "cadence" in frame
        else pd.Series("", index=frame.index)
    )
    output["frequency"] = frequency_source.astype("string")
    output["schema_version"] = FRED_VINTAGE_SCHEMA_VERSION
    supported_basis = {
        ALFRED_VINTAGE_AVAILABILITY_BASIS,
        LOCAL_RECEIPT_AVAILABILITY_BASIS,
    }
    unsupported_basis = sorted(
        set(output["availability_basis"].dropna().astype(str)).difference(
            supported_basis
        )
    )
    if unsupported_basis:
        raise ValueError(
            "FRED vintage rows contain unsupported availability basis: "
            + ", ".join(unsupported_basis)
        )
    if (
        output.loc[alfred_vintage, "release_time_precision"].ne("DATE").any()
        or output.loc[alfred_vintage, "fetched_at"].lt(
            output.loc[alfred_vintage, "release_at"]
        ).any()
    ):
        raise ValueError(
            "ALFRED vintage rows require date-precision provider timing and "
            "a later actual local acquisition"
        )
    if output[
        [
            "series_name",
            "observation_date",
            "realtime_start",
            "release_at",
            "release_time_precision",
            "fetched_at",
            "available_at",
            "availability_basis",
            "value",
        ]
    ].isna().any().any():
        raise ValueError("FRED vintage rows contain invalid required values")
    if output.duplicated(list(FRED_VINTAGE_NATURAL_KEY)).any():
        raise ValueError("FRED vintage rows contain duplicate vintage identities")
    return output.reindex(columns=FRED_VINTAGE_COLUMNS).sort_values(
        ["series_name", "available_at", "observation_date"]
    ).reset_index(drop=True)


def persist_fred_vintages(
    datastore_root: Path,
    frame: pd.DataFrame,
) -> tuple[Path, ...]:
    values = normalize_fred_vintage_rows(frame)
    paths: list[Path] = []
    for (series, year), partition in values.groupby(
        [
            values["series_name"],
            values["realtime_start"].dt.year,
        ]
    ):
        path = (
            Path(datastore_root)
            / "pools"
            / "macro-vintages"
            / safe_token(str(series))
            / "fred"
            / f"{int(year):04d}.parquet"
        )
        incoming = _drop_replayed_vintages(path, partition)
        if incoming.empty and path.is_file():
            paths.append(path)
            continue
        paths.append(
            write_immutable_feature_partition(
                path,
                incoming,
                columns=FRED_VINTAGE_COLUMNS,
                natural_key=FRED_VINTAGE_NATURAL_KEY,
            )
        )
    return tuple(paths)


def derive_macro_release_features(vintages: pd.DataFrame) -> pd.DataFrame:
    values = normalize_fred_vintage_rows(vintages)
    required_series = {"GDP", "CPIAUCSL", "UNRATE", "FEDFUNDS"}
    if not required_series.issubset(set(values["series_name"])):
        missing = sorted(required_series.difference(values["series_name"]))
        raise ValueError("Macro derivation is missing series: " + ", ".join(missing))

    rows: list[dict[str, object]] = []
    for available_at in values["available_at"].drop_duplicates().sort_values():
        known = values.loc[values["available_at"].le(available_at)].copy()
        by_series = {
            series: _latest_vintage_by_observation(
                known.loc[known["series_name"].eq(series)]
            )
            for series in required_series
        }
        fed_funds, fed_funds_available = _latest_value_with_availability(
            by_series["FEDFUNDS"]
        )
        cpi_yoy, cpi_available = _lag_change(
            by_series["CPIAUCSL"],
            months=12,
            ratio=True,
        )
        unemployment_change, unemployment_available = _lag_change(
            by_series["UNRATE"],
            months=1,
            ratio=False,
        )
        gdp_yoy, gdp_available = _lag_change(
            by_series["GDP"],
            months=12,
            ratio=True,
        )
        rows.append(
            {
                "context_name": "fred-release-context",
                "available_at": available_at,
                "calculation": MACRO_CALCULATION,
                "calculation_version": MACRO_CALCULATION_VERSION,
                "schema_version": MACRO_SCHEMA_VERSION,
                "fed_funds_available_at": fed_funds_available,
                "cpi_available_at": cpi_available,
                "unemployment_available_at": unemployment_available,
                "gdp_available_at": gdp_available,
                "macro__fed_funds_level": fed_funds,
                "macro__cpi_yoy": cpi_yoy,
                "macro__unemployment_change": unemployment_change,
                "macro__gdp_yoy": gdp_yoy,
            }
        )
    return pd.DataFrame(rows, columns=MACRO_FEATURE_COLUMNS)


def persist_macro_release_features(
    datastore_root: Path,
    frame: pd.DataFrame,
) -> tuple[Path, ...]:
    if frame.empty:
        return ()
    values = frame.copy()
    values["available_at"] = pd.to_datetime(
        values["available_at"], utc=True, errors="coerce"
    )
    paths: list[Path] = []
    for year, partition in values.groupby(values["available_at"].dt.year):
        if pd.isna(year):
            raise ValueError("Macro release features require available_at")
        path = (
            Path(datastore_root)
            / "pools"
            / "macro"
            / "features"
            / "release-context"
            / "fred"
            / f"{int(year):04d}.parquet"
        )
        paths.append(
            write_immutable_feature_partition(
                path,
                partition,
                columns=MACRO_FEATURE_COLUMNS,
                natural_key=(
                    "context_name",
                    "available_at",
                    "calculation_version",
                ),
            )
        )
    return tuple(paths)


def derive_current_fred_rate_receipt(
    rows: Sequence[Mapping[str, object]] | pd.DataFrame,
) -> pd.DataFrame:
    """Create one causal FEDFUNDS observation for decisions after this receipt.

    This deliberately uses the local FRED fetch time as availability.  It makes
    the current value usable for future live decisions, but it does not claim
    that a current-revised history was available for any earlier backtest.
    """

    frame = rows.copy() if isinstance(rows, pd.DataFrame) else pd.DataFrame(rows)
    series_column = next(
        (
            column
            for column in ("series", "series_name", "provider_symbol")
            if column in frame
        ),
        None,
    )
    required = {"fetched_at", "value"}
    missing = sorted(required.difference(frame.columns))
    if series_column is None:
        missing.append("series")
    observation_column = next(
        (column for column in ("date", "observation_date") if column in frame),
        None,
    )
    if observation_column is None:
        missing.append("observation_date")
    if missing:
        raise ValueError(
            "Current FRED rate receipt is missing: " + ", ".join(missing)
        )
    assert series_column is not None
    assert observation_column is not None
    values = frame.copy()
    values["_series"] = values[series_column].astype("string").str.upper()
    values = values.loc[values["_series"].eq("FEDFUNDS")].copy()
    if "source" in values:
        values = values.loc[
            values["source"].astype("string").str.lower().eq("fred")
        ]
    values["_fetched_at"] = pd.to_datetime(
        values["fetched_at"], utc=True, errors="coerce"
    )
    values["_observation_date"] = pd.to_datetime(
        values[observation_column],
        utc=True,
        errors="coerce",
    )
    values["_value"] = pd.to_numeric(values["value"], errors="coerce")
    values = values.dropna(
        subset=["_fetched_at", "_observation_date", "_value"]
    )
    values = values.loc[values["_value"].between(-20.0, 100.0)]
    if values.empty:
        raise ValueError("No valid current FRED FEDFUNDS receipt is available")
    latest = values.sort_values(
        ["_observation_date", "_fetched_at"], kind="stable"
    ).iloc[-1]
    available_at = pd.Timestamp(latest["_fetched_at"])
    return pd.DataFrame(
        [
            {
                "context_name": CURRENT_RATE_CONTEXT_NAME,
                "available_at": available_at,
                "calculation": CURRENT_RATE_CALCULATION,
                "calculation_version": MACRO_CALCULATION_VERSION,
                "schema_version": MACRO_SCHEMA_VERSION,
                "fed_funds_available_at": available_at,
                "cpi_available_at": pd.NaT,
                "unemployment_available_at": pd.NaT,
                "gdp_available_at": pd.NaT,
                "macro__fed_funds_level": float(latest["_value"]),
                "macro__cpi_yoy": float("nan"),
                "macro__unemployment_change": float("nan"),
                "macro__gdp_yoy": float("nan"),
            }
        ],
        columns=MACRO_FEATURE_COLUMNS,
    )


def persist_current_fred_rate_receipt(
    datastore_root: Path,
    rows: Sequence[Mapping[str, object]] | pd.DataFrame,
) -> tuple[Path, ...]:
    """Persist a current receipt without granting it historical availability."""

    return persist_macro_release_features(
        datastore_root,
        derive_current_fred_rate_receipt(rows),
    )


def derive_alfred_rate_release_features(
    vintages: pd.DataFrame,
) -> pd.DataFrame:
    """Derive point-in-time FEDFUNDS levels from verified ALFRED vintages.

    ``available_at`` is the conservative provider-vintage clock.  The distinct
    local acquisition clock remains in the immutable vintage evidence and is
    never rewritten as historical availability.
    """

    values = normalize_fred_vintage_rows(vintages)
    values = values.loc[
        values["series_name"].eq("FEDFUNDS")
        & values["availability_basis"].eq(ALFRED_VINTAGE_AVAILABILITY_BASIS)
    ].copy()
    if values.empty:
        raise ValueError("No verified ALFRED FEDFUNDS vintages are available")
    rows: list[dict[str, object]] = []
    for available_at in values["available_at"].drop_duplicates().sort_values():
        known = _latest_vintage_by_observation(
            values.loc[values["available_at"].le(available_at)]
        )
        rate, rate_available_at = _latest_value_with_availability(known)
        if rate is None or rate_available_at is None:
            continue
        rows.append(
            {
                "context_name": "fred-alfred-vintage-rate",
                "available_at": available_at,
                "calculation": "macro-alfred-vintage-rate",
                "calculation_version": MACRO_CALCULATION_VERSION,
                "schema_version": MACRO_SCHEMA_VERSION,
                "fed_funds_available_at": rate_available_at,
                "cpi_available_at": pd.NaT,
                "unemployment_available_at": pd.NaT,
                "gdp_available_at": pd.NaT,
                "macro__fed_funds_level": rate,
                "macro__cpi_yoy": float("nan"),
                "macro__unemployment_change": float("nan"),
                "macro__gdp_yoy": float("nan"),
            }
        )
    return pd.DataFrame(rows, columns=MACRO_FEATURE_COLUMNS)


def materialize_current_fred_rate_receipt(
    datastore_root: Path,
) -> tuple[Path, ...]:
    """Bridge the latest normalized FEDFUNDS fetch into the causal rate lane."""

    root = Path(datastore_root).resolve()
    source = (
        root
        / "pools"
        / "macro"
        / "FEDERALFUNDS"
        / "FEDFUNDS"
        / "fred"
        / "normalized"
        / "FEDERALFUNDS_FEDFUNDS.parquet"
    )
    if not source.is_file():
        raise FileNotFoundError(
            "No normalized FRED FEDFUNDS receipt exists; run the FRED lane first"
        )
    return persist_current_fred_rate_receipt(root, pd.read_parquet(source))


def _latest_vintage_by_observation(frame: pd.DataFrame) -> pd.DataFrame:
    return (
        frame.sort_values(["observation_date", "realtime_start", "available_at"])
        .drop_duplicates("observation_date", keep="last")
        .sort_values("observation_date")
        .reset_index(drop=True)
    )


def _latest_value_with_availability(
    frame: pd.DataFrame,
) -> tuple[float | None, pd.Timestamp | None]:
    if frame.empty:
        return None, None
    row = frame.iloc[-1]
    value = row["value"]
    if pd.isna(value):
        return None, None
    return float(value), pd.Timestamp(row["available_at"])


def _lag_change(
    frame: pd.DataFrame,
    *,
    months: int,
    ratio: bool,
) -> tuple[float | None, pd.Timestamp | None]:
    if frame.empty:
        return None, None
    current_row = frame.iloc[-1]
    current_date = pd.Timestamp(current_row["observation_date"]).normalize()
    prior_date = current_date - pd.DateOffset(months=months)
    prior_rows = frame.loc[
        pd.to_datetime(
            frame["observation_date"],
            utc=True,
            errors="coerce",
        ).dt.normalize().eq(prior_date)
    ]
    if prior_rows.empty:
        return None, None
    prior_row = prior_rows.iloc[-1]
    current = float(current_row["value"])
    prior = float(prior_row["value"])
    availability = max(
        pd.Timestamp(current_row["available_at"]),
        pd.Timestamp(prior_row["available_at"]),
    )
    if ratio:
        return (
            (None, None)
            if prior == 0
            else (current / abs(prior) - 1.0, availability)
        )
    return current - prior, availability


def _drop_replayed_vintages(
    path: Path,
    incoming: pd.DataFrame,
) -> pd.DataFrame:
    """Preserve the first local receipt for a stable provider vintage identity."""

    if not path.is_file() or incoming.empty:
        return incoming
    existing = pd.read_parquet(path).drop(columns=["id"], errors="ignore")
    for column in ("observation_date", "realtime_start"):
        existing[column] = pd.to_datetime(
            existing[column], utc=True, errors="coerce"
        )
    compare_columns = (
        "release_at",
        "release_time_precision",
        "availability_basis",
        "value",
        "unit",
        "frequency",
        "schema_version",
    )
    keep: list[bool] = []
    for _, row in incoming.iterrows():
        matches = existing.loc[
            existing["series_name"].astype(str).eq(str(row["series_name"]))
            & existing["observation_date"].eq(row["observation_date"])
            & existing["realtime_start"].eq(row["realtime_start"])
        ]
        if matches.empty:
            keep.append(True)
            continue
        prior = matches.iloc[0]
        conflict = any(
            not _equal(prior.get(column), row.get(column))
            for column in compare_columns
        )
        if conflict:
            raise ValueError(
                "A FRED vintage identity changed after its first receipt"
            )
        keep.append(False)
    return incoming.loc[keep].reset_index(drop=True)


def _equal(left: object, right: object) -> bool:
    if pd.isna(left) and pd.isna(right):
        return True
    if isinstance(left, pd.Timestamp) or isinstance(right, pd.Timestamp):
        return pd.Timestamp(left) == pd.Timestamp(right)
    try:
        return bool(float(left) == float(right))
    except (TypeError, ValueError):
        return str(left) == str(right)


def _canonical_series(value: object) -> str:
    normalized = str(value or "").strip().upper()
    try:
        return _SERIES_ALIASES[normalized]
    except KeyError as exc:
        raise ValueError(f"Unsupported FRED series: {value!r}") from exc
