from __future__ import annotations

from pathlib import Path
from typing import Iterable

import numpy as np
import pandas as pd

from datafetching.calculated_features import write_immutable_feature_partition
from datafetching.layout import safe_token

CALCULATION_NAME = "point-in-time-fundamentals"
CALCULATION_VERSION = "1.0.0"
SCHEMA_VERSION = "point-in-time-fundamentals-v1"

MODEL_VALUE_COLUMNS = (
    "revenue_growth_yoy",
    "operating_margin",
    "operating_margin_change_yoy",
    "free_cash_flow_margin",
    "cfo_to_net_income",
    "cash_to_debt",
    "current_ratio",
    "diluted_share_growth_yoy",
    "stock_comp_to_revenue",
    "net_issuance_to_market_cap",
    "buyback_yield",
    "roic",
    "fcf_yield",
)
POINT_IN_TIME_COLUMNS = (
    "symbol",
    "fiscal_period",
    "period_type",
    "period_end_date",
    "accepted_at",
    "published_at",
    "fetched_at",
    "calculated_at",
    "available_at",
    "market_cap_available_at",
    "lagged_comparison_available_at",
    "effective_date_estimated",
    "constituent_complete",
    "missing_statement_families",
    "statement_version_kind",
    "source",
    "calculation",
    "calculation_version",
    "schema_version",
    *MODEL_VALUE_COLUMNS,
)
POINT_IN_TIME_NATURAL_KEY = (
    "symbol",
    "period_type",
    "period_end_date",
    "available_at",
)


def calculate_point_in_time_fundamentals(
    income: pd.DataFrame,
    balance: pd.DataFrame,
    cash_flow: pd.DataFrame,
    *,
    symbol: str,
    period_type: str,
    calculated_at: object | None = None,
    market_cap_by_period: pd.DataFrame | None = None,
) -> pd.DataFrame:
    """Calculate only the statement versions actually known at this run.

    Existing reconstructed history becomes available at this calculation time;
    it is not assigned a fabricated historical clock. Later amendments create
    later immutable rows and therefore cannot alter an earlier decision.
    """

    cadence = str(period_type).strip().lower()
    if cadence not in {"quarterly", "annual"}:
        raise ValueError("period_type must be quarterly or annual")
    clean_symbol = str(symbol).strip().upper()
    if not clean_symbol:
        raise ValueError("symbol is required")
    completed = _utc_timestamp(
        calculated_at if calculated_at is not None else pd.Timestamp.now(tz="UTC")
    )
    prepared = {
        "income": _latest_known_versions(income, as_of=completed),
        "balance": _latest_known_versions(balance, as_of=completed),
        "cash": _latest_known_versions(cash_flow, as_of=completed),
    }
    lagged_prepared = {
        "income": _latest_known_versions(
            income,
            as_of=completed,
            keep="first",
        ),
        "balance": _latest_known_versions(
            balance,
            as_of=completed,
            keep="first",
        ),
        "cash": _latest_known_versions(
            cash_flow,
            as_of=completed,
            keep="first",
        ),
    }
    market_caps = _prepare_market_caps(
        market_cap_by_period,
        as_of=completed,
    )
    period_ends = sorted(
        set().union(
            *(
                set(frame["period_end_date"].dropna())
                for frame in prepared.values()
                if not frame.empty
            )
        )
    )
    if not period_ends:
        raise ValueError("No statement versions have valid period and availability")

    combined_rows: list[dict[str, object]] = []
    raw_by_period: dict[pd.Timestamp, dict[str, object]] = {}
    lagged_raw_by_period: dict[pd.Timestamp, dict[str, object]] = {}
    lagged_available_by_period: dict[pd.Timestamp, pd.Timestamp | None] = {}
    for period_end in period_ends:
        statements = {
            name: _row_for_period(frame, period_end)
            for name, frame in prepared.items()
        }
        all_rows = [row for row in statements.values() if row is not None]
        if not all_rows:
            continue
        missing_families = tuple(
            name for name, row in statements.items() if row is None
        )
        lagged_rows = [
            row
            for frame in lagged_prepared.values()
            if (row := _row_for_period(frame, period_end)) is not None
        ]
        row_values = _merged_statement_values(all_rows)
        raw_by_period[pd.Timestamp(period_end)] = row_values
        lagged_raw_by_period[pd.Timestamp(period_end)] = (
            _merged_statement_values(lagged_rows)
        )
        lagged_available_by_period[pd.Timestamp(period_end)] = max(
            (
                timestamp
                for raw in lagged_rows
                if (
                    timestamp := _optional_timestamp(raw.get("available_at"))
                )
                is not None
            ),
            default=None,
        )
        publication_times = [
            timestamp
            for raw in all_rows
            for timestamp in (
                _optional_timestamp(raw.get("accepted_at")),
                _optional_timestamp(raw.get("published_at")),
            )
            if timestamp is not None
        ]
        fetched_times = [
            timestamp
            for raw in all_rows
            if (timestamp := _optional_timestamp(raw.get("fetched_at"))) is not None
        ]
        source_availability = [
            timestamp
            for raw in all_rows
            if (timestamp := _optional_timestamp(raw.get("available_at"))) is not None
        ]
        accepted = max(
            (
                timestamp
                for raw in all_rows
                if (timestamp := _optional_timestamp(raw.get("accepted_at")))
                is not None
            ),
            default=None,
        )
        published = max(publication_times, default=None)
        fetched = max(fetched_times, default=None)
        market_cap, market_cap_available = market_caps.get(
            pd.Timestamp(period_end),
            (None, None),
        )
        available = max(
            [
                completed,
                *source_availability,
                *publication_times,
                *fetched_times,
                *((market_cap_available,) if market_cap_available is not None else ()),
            ]
        )
        combined_rows.append(
            {
                "symbol": clean_symbol,
                "fiscal_period": str(
                    row_values.get("period")
                    or row_values.get("fiscal_period")
                    or ""
                ),
                "period_type": cadence,
                "period_end_date": pd.Timestamp(period_end),
                "accepted_at": accepted,
                "published_at": published,
                "fetched_at": fetched,
                "calculated_at": completed,
                "available_at": available,
                "market_cap_available_at": market_cap_available,
                "lagged_comparison_available_at": None,
                "effective_date_estimated": any(
                    bool(raw.get("effective_date_estimated")) for raw in all_rows
                )
                or not publication_times,
                "constituent_complete": not missing_families,
                "missing_statement_families": ",".join(missing_families),
                "statement_version_kind": _statement_version_kind(all_rows),
                "source": "fmp",
                "calculation": CALCULATION_NAME,
                "calculation_version": CALCULATION_VERSION,
                "schema_version": SCHEMA_VERSION,
            }
        )

    output = pd.DataFrame(combined_rows)
    output = output.sort_values("period_end_date").reset_index(drop=True)
    lag = 4 if cadence == "quarterly" else 1
    metrics: list[dict[str, float | None]] = []
    for index, row in output.iterrows():
        current = raw_by_period[pd.Timestamp(row["period_end_date"])]
        prior = (
            lagged_raw_by_period[
                pd.Timestamp(output.iloc[index - lag]["period_end_date"])
            ]
            if index >= lag
            else {}
        )
        if index >= lag:
            output.loc[index, "lagged_comparison_available_at"] = (
                lagged_available_by_period[
                    pd.Timestamp(
                        output.iloc[index - lag]["period_end_date"]
                    )
                ]
            )
        metrics.append(
            _fundamental_metrics(
                current,
                prior,
                market_cap=market_caps.get(
                    pd.Timestamp(row["period_end_date"]),
                    (None, None),
                )[0],
            )
        )
    metric_frame = pd.DataFrame(metrics, columns=MODEL_VALUE_COLUMNS)
    return pd.concat([output, metric_frame], axis=1).reindex(
        columns=POINT_IN_TIME_COLUMNS
    )


def persist_point_in_time_fundamentals(
    datastore_root: Path,
    frame: pd.DataFrame,
    *,
    symbol: str,
    period_type: str,
) -> Path:
    path = (
        Path(datastore_root)
        / "stocks"
        / safe_token(symbol.strip().upper())
        / "fundamentals"
        / "point-in-time"
        / "fmp"
        / f"{safe_token(period_type)}.parquet"
    )
    incoming = _drop_unchanged_versions(path, frame)
    if incoming.empty and path.is_file():
        return path
    return write_immutable_feature_partition(
        path,
        incoming,
        columns=POINT_IN_TIME_COLUMNS,
        natural_key=POINT_IN_TIME_NATURAL_KEY,
    )


def _latest_known_versions(
    frame: pd.DataFrame,
    *,
    as_of: pd.Timestamp,
    keep: str = "last",
) -> pd.DataFrame:
    if frame.empty:
        return pd.DataFrame()
    values = frame.drop(columns=["id"], errors="ignore").copy()
    period_column = _first_present(values, ("period_end_date", "date"))
    if period_column is None:
        return pd.DataFrame()
    values["period_end_date"] = pd.to_datetime(
        values[period_column], utc=True, errors="coerce"
    ).dt.normalize()
    values["accepted_at"] = _publication_series(
        values,
        ("accepted_at", "accepted_date"),
    )
    values["published_at"] = _publication_series(
        values,
        ("published_at", "published_date", "filing_date", "filling_date"),
    )
    values["fetched_at"] = _timestamp_series(values, ("fetched_at",))
    if "available_at" in values:
        values["available_at"] = pd.to_datetime(
            values["available_at"], utc=True, errors="coerce"
        )
    else:
        values["available_at"] = pd.concat(
            [
                values["accepted_at"],
                values["published_at"],
                values["fetched_at"],
            ],
            axis=1,
        ).max(axis=1)
    values["effective_date_estimated"] = values.get(
        "effective_date_estimated",
        values["accepted_at"].isna() & values["published_at"].isna(),
    )
    values = values.loc[
        values["period_end_date"].notna()
        & values["available_at"].notna()
        & values["available_at"].le(as_of)
    ].copy()
    if values.empty:
        return values
    if keep not in {"first", "last"}:
        raise ValueError("keep must be first or last")
    return (
        values.sort_values(["period_end_date", "available_at"])
        .drop_duplicates("period_end_date", keep=keep)
        .reset_index(drop=True)
    )


def _publication_series(
    frame: pd.DataFrame,
    columns: Iterable[str],
) -> pd.Series:
    result = pd.Series(pd.NaT, index=frame.index, dtype="datetime64[ns, UTC]")
    for column in columns:
        if column not in frame:
            continue
        raw = frame[column].astype("string").str.strip()
        parsed = pd.to_datetime(raw, utc=True, errors="coerce")
        date_only = raw.str.fullmatch(r"\d{4}-\d{2}-\d{2}", na=False)
        # Pandas may coerce a provider's date-only field to midnight before this
        # normalizer sees it.  A ``*_date`` field at midnight therefore still
        # carries date precision, while a true ``*_at`` timestamp is preserved.
        if column.endswith("_date"):
            date_only |= (
                parsed.notna()
                & parsed.dt.hour.eq(0)
                & parsed.dt.minute.eq(0)
                & parsed.dt.second.eq(0)
                & parsed.dt.microsecond.eq(0)
            )
        parsed.loc[date_only] = (
            parsed.loc[date_only].dt.normalize() + pd.Timedelta(days=1)
        )
        result = result.fillna(parsed)
    return result


def _timestamp_series(
    frame: pd.DataFrame,
    columns: Iterable[str],
) -> pd.Series:
    result = pd.Series(pd.NaT, index=frame.index, dtype="datetime64[ns, UTC]")
    for column in columns:
        if column in frame:
            result = result.fillna(
                pd.to_datetime(frame[column], utc=True, errors="coerce")
            )
    return result


def _row_for_period(
    frame: pd.DataFrame,
    period_end: pd.Timestamp,
) -> dict[str, object] | None:
    if frame.empty:
        return None
    rows = frame.loc[frame["period_end_date"].eq(period_end)]
    return None if rows.empty else rows.iloc[-1].to_dict()


def _merged_statement_values(
    rows: Iterable[dict[str, object]],
) -> dict[str, object]:
    merged: dict[str, object] = {}
    for row in rows:
        for key, value in row.items():
            if key not in merged or _missing(merged[key]):
                merged[key] = value
    return merged


def _fundamental_metrics(
    current: dict[str, object],
    prior: dict[str, object],
    *,
    market_cap: float | None,
) -> dict[str, float | None]:
    revenue = _value(current, "revenue")
    prior_revenue = _value(prior, "revenue")
    operating_income = _value(current, "operating_income")
    prior_operating_income = _value(prior, "operating_income")
    net_income = _value(current, "net_income")
    cfo = _value(
        current,
        "operating_cash_flow",
        "net_cash_provided_by_operating_activities",
    )
    capex = _value(current, "capital_expenditure", "capital_expenditures")
    free_cash_flow = _value(current, "free_cash_flow")
    if free_cash_flow is None and cfo is not None and capex is not None:
        free_cash_flow = cfo + capex if capex < 0 else cfo - capex
    cash = _value(
        current,
        "cash_and_cash_equivalents",
        "cash_and_short_term_investments",
    )
    debt = _value(current, "total_debt")
    current_assets = _value(current, "total_current_assets")
    current_liabilities = _value(current, "total_current_liabilities")
    diluted_shares = _value(
        current,
        "weighted_average_shs_out_dil",
        "weighted_average_shares_outstanding_diluted",
    )
    prior_diluted_shares = _value(
        prior,
        "weighted_average_shs_out_dil",
        "weighted_average_shares_outstanding_diluted",
    )
    stock_comp = _value(current, "stock_based_compensation")
    issuance = _value(
        current,
        "common_stock_issuance",
        "net_common_stock_issuance",
    )
    repurchases = _value(
        current,
        "common_stock_repurchased",
        "repurchases_of_stock",
    )
    taxes = _value(current, "income_tax_expense")
    pretax = _value(current, "income_before_tax")
    tax_rate = _safe_ratio(taxes, pretax)
    nopat = (
        operating_income * (1.0 - min(max(tax_rate or 0.21, 0.0), 1.0))
        if operating_income is not None
        else None
    )
    invested_capital = _sum_available(
        _value(current, "total_stockholders_equity", "total_equity"),
        debt,
        -cash if cash is not None else None,
    )
    operating_margin = _safe_ratio(operating_income, revenue)
    prior_operating_margin = _safe_ratio(prior_operating_income, prior_revenue)
    return {
        "revenue_growth_yoy": _growth(revenue, prior_revenue),
        "operating_margin": operating_margin,
        "operating_margin_change_yoy": _difference(
            operating_margin,
            prior_operating_margin,
        ),
        "free_cash_flow_margin": _safe_ratio(free_cash_flow, revenue),
        "cfo_to_net_income": _safe_ratio(cfo, net_income),
        "cash_to_debt": _safe_ratio(cash, debt),
        "current_ratio": _safe_ratio(current_assets, current_liabilities),
        "diluted_share_growth_yoy": _growth(
            diluted_shares,
            prior_diluted_shares,
        ),
        "stock_comp_to_revenue": _safe_ratio(stock_comp, revenue),
        "net_issuance_to_market_cap": _safe_ratio(issuance, market_cap),
        "buyback_yield": _safe_ratio(
            abs(repurchases) if repurchases is not None else None,
            market_cap,
        ),
        "roic": _safe_ratio(nopat, invested_capital),
        "fcf_yield": _safe_ratio(free_cash_flow, market_cap),
    }


def _drop_unchanged_versions(path: Path, frame: pd.DataFrame) -> pd.DataFrame:
    if not path.is_file() or frame.empty:
        return frame
    existing = pd.read_parquet(path).drop(columns=["id"], errors="ignore")
    existing["period_end_date"] = pd.to_datetime(
        existing["period_end_date"], utc=True, errors="coerce"
    )
    latest = (
        existing.sort_values("available_at")
        .drop_duplicates(["symbol", "period_type", "period_end_date"], keep="last")
        .set_index(["symbol", "period_type", "period_end_date"])
    )
    keep: list[bool] = []
    compare_columns = [
        "accepted_at",
        "published_at",
        "market_cap_available_at",
        "lagged_comparison_available_at",
        "effective_date_estimated",
        "constituent_complete",
        "missing_statement_families",
        "statement_version_kind",
        "source",
        "calculation",
        "calculation_version",
        "schema_version",
        *MODEL_VALUE_COLUMNS,
    ]
    for _, row in frame.iterrows():
        key = (
            row["symbol"],
            row["period_type"],
            pd.Timestamp(row["period_end_date"]),
        )
        if key not in latest.index:
            keep.append(True)
            continue
        prior = latest.loc[key]
        keep.append(
            any(
                not _equal(prior.get(column), row.get(column))
                for column in compare_columns
            )
        )
    return frame.loc[keep].reset_index(drop=True)


def _statement_version_kind(rows: Iterable[dict[str, object]]) -> str:
    return (
        "ESTIMATED_PUBLICATION_QUARANTINED"
        if any(bool(row.get("effective_date_estimated")) for row in rows)
        else "REPORTED_OR_AMENDED"
    )


def _prepare_market_caps(
    values: pd.DataFrame | None,
    *,
    as_of: pd.Timestamp,
) -> dict[pd.Timestamp, tuple[float | None, pd.Timestamp | None]]:
    """Require explicit availability for every causal market-cap denominator."""

    if values is None:
        return {}
    if not isinstance(values, pd.DataFrame):
        raise ValueError(
            "market_cap_by_period requires period_end_date, market_cap, "
            "and available_at provenance"
        )
    required = {"period_end_date", "market_cap", "available_at"}
    missing = sorted(required.difference(values.columns))
    if missing:
        raise ValueError(
            "market_cap_by_period is missing causal provenance columns: "
            + ", ".join(missing)
        )
    prepared = values.copy()
    prepared["period_end_date"] = pd.to_datetime(
        prepared["period_end_date"], utc=True, errors="coerce"
    ).dt.normalize()
    prepared["available_at"] = pd.to_datetime(
        prepared["available_at"], utc=True, errors="coerce"
    )
    prepared["market_cap"] = pd.to_numeric(
        prepared["market_cap"], errors="coerce"
    )
    if prepared[
        ["period_end_date", "available_at", "market_cap"]
    ].isna().any().any():
        raise ValueError("market_cap_by_period contains invalid causal evidence")
    prepared = prepared.loc[prepared["available_at"].le(as_of)].copy()
    latest = (
        prepared.sort_values(["period_end_date", "available_at"])
        .drop_duplicates("period_end_date", keep="last")
    )
    return {
        pd.Timestamp(row["period_end_date"]): (
            _number(row["market_cap"]),
            pd.Timestamp(row["available_at"]),
        )
        for _, row in latest.iterrows()
    }


def _first_present(frame: pd.DataFrame, names: Iterable[str]) -> str | None:
    return next((name for name in names if name in frame), None)


def _value(row: dict[str, object], *names: str) -> float | None:
    for name in names:
        if name in row and (value := _number(row[name])) is not None:
            return value
    return None


def _number(value: object) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if np.isfinite(number) else None


def _safe_ratio(numerator: float | None, denominator: float | None) -> float | None:
    if numerator is None or denominator is None or denominator == 0:
        return None
    return numerator / denominator


def _growth(current: float | None, prior: float | None) -> float | None:
    if current is None or prior is None or prior == 0:
        return None
    return current / abs(prior) - 1.0


def _difference(left: float | None, right: float | None) -> float | None:
    return None if left is None or right is None else left - right


def _sum_available(*values: float | None) -> float | None:
    present = [value for value in values if value is not None]
    return sum(present) if present else None


def _optional_timestamp(value: object) -> pd.Timestamp | None:
    timestamp = pd.to_datetime(value, utc=True, errors="coerce")
    return None if pd.isna(timestamp) else pd.Timestamp(timestamp)


def _utc_timestamp(value: object) -> pd.Timestamp:
    timestamp = _optional_timestamp(value)
    if timestamp is None:
        raise ValueError("Expected a valid UTC timestamp")
    return timestamp


def _missing(value: object) -> bool:
    try:
        return bool(pd.isna(value))
    except (TypeError, ValueError):
        return False


def _equal(left: object, right: object) -> bool:
    left_missing = _missing(left)
    right_missing = _missing(right)
    if left_missing or right_missing:
        return left_missing and right_missing
    if isinstance(left, pd.Timestamp) or isinstance(right, pd.Timestamp):
        return _optional_timestamp(left) == _optional_timestamp(right)
    return bool(left == right)
