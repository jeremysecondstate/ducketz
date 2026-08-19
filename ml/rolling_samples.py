from __future__ import annotations

import json
from math import isfinite
from typing import Final

import numpy as np
import pandas as pd

from datafetching.ids import add_readable_id
from ml.calendars import (
    ExchangeSessionCalendar,
    attach_official_intraday_sessions,
    calendar_for_horizon,
)
from ml.contracts import MLContractError
from ml.horizons import HorizonSpecification, is_weekly_horizon
from ml.timing import utc_timestamp

ROLLING_SAMPLE_CONTEXT_COLUMNS: Final = (
    "id",
    "symbol",
    "venue",
    "currency",
    "provider",
    "exchange_calendar",
    "exchange_session",
    "horizon",
)
ROLLING_SAMPLE_TIME_COLUMNS: Final = (
    "decision_timestamp",
    "information_available_at",
    "target_window_start",
    "target_window_end",
    "actionable_until",
    "label_available_at",
)
ROLLING_SAMPLE_TARGET_COLUMNS: Final = (
    "target_definition_version",
    "target_specification",
    "target_open",
    "target_close",
    "forward_raw_return",
    "forward_cost_adjusted_return",
    "target_cost_adjusted_positive",
    "label_status",
    "label_exclusion_reason",
    "previous_period_direction",
)


def build_rolling_samples(
    feature_frame: pd.DataFrame,
    adjusted_prices: pd.DataFrame,
    *,
    specification: HorizonSpecification,
    assumed_round_trip_cost: float,
    materialized_at: object | None = None,
    source_adjusted_prices: pd.DataFrame | None = None,
) -> pd.DataFrame:
    """Build one point-in-time sample path for a single rolling horizon."""

    if not isfinite(assumed_round_trip_cost) or not (
        0.0 <= assumed_round_trip_cost < 1.0
    ):
        raise ValueError(
            "assumed_round_trip_cost must be finite and satisfy 0 <= cost < 1"
        )
    created = utc_timestamp(materialized_at)
    features = _prepare_features(feature_frame, specification=specification)
    if features.empty:
        return _empty_samples(feature_frame)
    if specification.horizon in {"1h", "4h"}:
        target_prices = _prepare_intraday_target_prices(
            features,
            adjusted_prices,
            specification=specification,
        )
        if source_adjusted_prices is None:
            raise MLContractError(
                f"Rolling {specification.horizon} samples require separate "
                "native 1h source prices for previous-period direction."
            )
        source_prices = _prepare_prices(
            features,
            source_adjusted_prices,
            specification=specification,
        )
    else:
        target_prices = _prepare_prices(
            features,
            adjusted_prices,
            specification=specification,
        )
        source_prices = target_prices

    pieces: list[pd.DataFrame] = []
    grouping = ["symbol", "provider", "exchange_calendar"]
    for _, group in features.groupby(grouping, sort=True, dropna=False):
        symbol = str(group["symbol"].iloc[0])
        provider = str(group["provider"].iloc[0])
        exchange_calendar = str(group["exchange_calendar"].iloc[0])
        symbol_target_prices = target_prices.loc[
            target_prices["symbol"].astype(str).eq(symbol)
            & target_prices["provider"].astype(str).eq(provider)
        ].copy()
        symbol_source_prices = source_prices.loc[
            source_prices["symbol"].astype(str).eq(symbol)
            & source_prices["provider"].astype(str).eq(provider)
        ].copy()
        if symbol_target_prices.empty:
            raise MLContractError(
                f"No adjusted target prices exist for {symbol}/{provider}."
            )
        if symbol_source_prices.empty:
            raise MLContractError(
                f"No adjusted source prices exist for {symbol}/{provider}."
            )
        minimum_session = min(
            group["exchange_session"].min(),
            symbol_target_prices["exchange_session"].min(),
            symbol_source_prices["exchange_session"].min(),
        )
        maximum_session = max(
            group["exchange_session"].max(),
            symbol_target_prices["exchange_session"].max(),
            symbol_source_prices["exchange_session"].max(),
        )
        calendar = calendar_for_horizon(
            exchange_calendar,
            minimum_session=minimum_session,
            maximum_session=maximum_session,
            future_padding_days=120,
        )
        pieces.append(
            _build_symbol_samples(
                group,
                symbol_target_prices,
                source_prices=symbol_source_prices,
                calendar=calendar,
                specification=specification,
                assumed_round_trip_cost=assumed_round_trip_cost,
                materialized_at=created,
            )
        )

    result = pd.concat(pieces, ignore_index=True, sort=False)
    if result.empty:
        return result
    result = result.reset_index(drop=True)
    result = add_readable_id(
        result,
        key_columns=("symbol", "horizon", "decision_timestamp"),
    )
    result["target_cost_adjusted_positive"] = pd.to_numeric(
        result["target_cost_adjusted_positive"], errors="coerce"
    ).astype("Int8")
    if result["id"].duplicated().any():
        raise MLContractError("Rolling sample id must be unique")
    if result.duplicated(
        ["symbol", "horizon", "decision_timestamp"]
    ).any():
        raise MLContractError(
            "Rolling samples must be unique by symbol, horizon, and decision timestamp"
        )
    complete = result["label_status"].eq("COMPLETE")
    if (
        result.loc[complete, "label_available_at"] > materialized_at_to_series(
            created, result.loc[complete].index
        )
    ).any():
        raise MLContractError(
            "A rolling label was marked complete before it became available"
        )
    if not (
        result["information_available_at"] < result["target_window_start"]
    ).all():
        raise MLContractError(
            "Rolling target windows must begin after information availability"
        )
    horizons = result["horizon"].astype(str)
    weekly_aggregate = horizons.eq("1w")
    weekly_components = horizons.str.fullmatch(
        r"1w-d[1-5]"
    )
    valid_actionability = (
        result["information_available_at"] < result["actionable_until"]
    ) & (
        (
            ~(weekly_aggregate | weekly_components)
            & (
                result["actionable_until"]
                <= result["target_window_start"]
            )
        )
        | (
            weekly_components
            & (
                result["actionable_until"]
                == result["target_window_end"]
            )
        )
        | (
            weekly_aggregate
            & (
                result["actionable_until"]
                > result["target_window_start"]
            )
            & (
                result["actionable_until"]
                <= result["target_window_end"]
            )
        )
    )
    if not valid_actionability.all():
        raise MLContractError(
            "Rolling actionability deadlines must follow information "
            "availability; ordinary routes cannot exceed target entry, and "
            "remaining-week routes expire within their target window"
        )
    return result.sort_values(
        ["information_available_at", "symbol", "horizon", "id"],
        kind="mergesort",
    ).reset_index(drop=True)


def _build_symbol_samples(
    features: pd.DataFrame,
    target_prices: pd.DataFrame,
    *,
    source_prices: pd.DataFrame,
    calendar: ExchangeSessionCalendar,
    specification: HorizonSpecification,
    assumed_round_trip_cost: float,
    materialized_at: pd.Timestamp,
) -> pd.DataFrame:
    if specification.horizon == "1h":
        windows = _hour_windows(
            features,
            target_prices,
            source_prices=source_prices,
            calendar=calendar,
            target_minute_count=60,
        )
    elif specification.horizon == "4h":
        windows = _hour_windows(
            features,
            target_prices,
            source_prices=source_prices,
            calendar=calendar,
            target_minute_count=180,
        )
    elif specification.horizon == "1d":
        windows = _daily_windows(features, target_prices, calendar=calendar)
    elif is_weekly_horizon(specification.horizon):
        windows = _weekly_windows(
            features,
            target_prices,
            calendar=calendar,
            route_horizon=specification.horizon,
        )
    else:
        raise MLContractError(
            f"Unsupported rolling target horizon: {specification.horizon!r}"
        )

    rows: list[dict[str, object]] = []
    for feature_index, target in windows:
        source = features.loc[feature_index]
        information_available_at = pd.Timestamp(source["decision_timestamp"])
        if information_available_at > materialized_at:
            continue

        target_start = pd.Timestamp(target["target_window_start"])
        target_end = pd.Timestamp(target["target_window_end"])
        label_available_at = (
            target_end + specification.processing_delay
        )
        target_open = _finite_positive_or_none(target.get("target_open"))
        target_close = _finite_positive_or_none(target.get("target_close"))
        prices_present = (
            bool(target.get("constituent_prices_complete", True))
            and target_open is not None
            and target_close is not None
        )
        complete = prices_present and label_available_at <= materialized_at
        raw_return = (
            float(target_close / target_open - 1.0)
            if complete and target_open is not None and target_close is not None
            else np.nan
        )
        cost_adjusted = (
            raw_return - assumed_round_trip_cost if complete else np.nan
        )
        target_value: object = (
            int(cost_adjusted > 0.0) if complete else pd.NA
        )
        published_target_open = target_open if complete else None
        published_target_close = target_close if complete else None
        row = source.to_dict()
        row.update(
            {
                "horizon": specification.horizon,
                "target_definition_version": (
                    specification.target_definition_version
                ),
                "target_specification": json.dumps(
                    specification.as_dict(),
                    sort_keys=True,
                    separators=(",", ":"),
                ),
                "information_available_at": information_available_at,
                "target_window_start": target_start,
                "target_window_end": target_end,
                "actionable_until": pd.Timestamp(
                    target.get("actionable_until", target_start)
                ),
                "label_available_at": label_available_at,
                "target_open": published_target_open,
                "target_close": published_target_close,
                "forward_raw_return": raw_return,
                "forward_cost_adjusted_return": cost_adjusted,
                "target_cost_adjusted_positive": target_value,
                "label_status": "COMPLETE" if complete else "INCOMPLETE_LABEL",
                "label_exclusion_reason": (
                    None
                    if complete
                    else (
                        "target_window_not_mature"
                        if label_available_at > materialized_at
                        else "complete_target_prices_unavailable"
                    )
                ),
                "previous_period_direction": target[
                    "previous_period_direction"
                ],
                "assumed_round_trip_cost": float(assumed_round_trip_cost),
            }
        )
        rows.append(row)
    return pd.DataFrame(rows)


def _hour_windows(
    features: pd.DataFrame,
    target_prices: pd.DataFrame,
    *,
    source_prices: pd.DataFrame,
    calendar: ExchangeSessionCalendar,
    target_minute_count: int,
) -> list[tuple[int, dict[str, object]]]:
    if target_minute_count < 1:
        raise ValueError("target_minute_count must be positive")
    target_price_lookup = {
        pd.Timestamp(row.bar_timestamp): row
        for row in target_prices.itertuples(index=False)
    }
    source_lookup = {
        pd.Timestamp(row.bar_timestamp): row
        for row in source_prices.itertuples(index=False)
        if bool(getattr(row, "intraday_interval_eligible", False))
    }
    records: list[tuple[int, dict[str, object]]] = []
    for index, row in features.iterrows():
        available = pd.Timestamp(row["decision_timestamp"])
        target_window = calendar.target_window_after(
            available,
            eligible_minute_count=target_minute_count,
        )
        constituent_prices = [
            target_price_lookup.get(timestamp)
            for timestamp in target_window.constituent_timestamps
        ]
        first_price = constituent_prices[0]
        final_price = constituent_prices[-1]
        source_price = source_lookup.get(pd.Timestamp(row["bar_timestamp"]))
        records.append(
            (
                int(index),
                {
                    "target_window_start": target_window.start_timestamp,
                    "target_window_end": target_window.end_timestamp,
                    "target_open": (
                        getattr(first_price, "open", None)
                        if first_price is not None
                        else None
                    ),
                    "target_close": (
                        getattr(final_price, "close", None)
                        if final_price is not None
                        else None
                    ),
                    "constituent_prices_complete": all(
                        target_price is not None
                        for target_price in constituent_prices
                    ),
                    "previous_period_direction": _direction(source_price),
                },
            )
        )
    return records


def _daily_windows(
    features: pd.DataFrame,
    prices: pd.DataFrame,
    *,
    calendar: ExchangeSessionCalendar,
) -> list[tuple[int, dict[str, object]]]:
    return _next_session_windows(features, prices, calendar=calendar)


def _weekly_windows(
    features: pd.DataFrame,
    prices: pd.DataFrame,
    *,
    calendar: ExchangeSessionCalendar,
    route_horizon: str,
) -> list[tuple[int, dict[str, object]]]:
    """Build one dynamic remaining-week route at each daily decision."""

    leads = {
        "1w-d1": 1,
        "1w-d2": 2,
        "1w-d3": 3,
        "1w-d4": 4,
        "1w-d5": 5,
    }
    if route_horizon != "1w" and route_horizon not in leads:
        raise MLContractError(
            f"Unsupported remaining-week target route: {route_horizon!r}"
        )
    price_lookup = {
        pd.Timestamp(row.exchange_session): row
        for row in prices.itertuples(index=False)
    }
    records: list[tuple[int, dict[str, object]]] = []
    for index, row in features.iterrows():
        resolved = calendar.horizon(
            decision_session=row["exchange_session"],
            decision_timestamp=row["decision_timestamp"],
            future_session_count=5,
        )
        first_session = resolved.future_sessions[0]
        if route_horizon == "1w":
            remaining_week_sessions: list[pd.Timestamp] = []
            for future_session in resolved.future_sessions:
                remaining_week_sessions.append(pd.Timestamp(future_session))
                if calendar.is_final_session_of_exchange_week(future_session):
                    break
            final_session_is_week_end = (
                bool(remaining_week_sessions)
                and calendar.is_final_session_of_exchange_week(
                    remaining_week_sessions[-1]
                )
            )
            if not final_session_is_week_end:
                raise MLContractError(
                    "Could not resolve the final remaining exchange-week session"
                )
            start_session = first_session
            end_session = remaining_week_sessions[-1]
            actionable_until = calendar.session_close(first_session)
        else:
            component_session = resolved.future_sessions[leads[route_horizon] - 1]
            start_session = component_session
            end_session = component_session
            actionable_until = calendar.session_close(component_session)
        start_price = price_lookup.get(start_session)
        end_price = price_lookup.get(end_session)
        source_price = price_lookup.get(pd.Timestamp(row["exchange_session"]))
        records.append(
            (
                int(index),
                {
                    "target_window_start": calendar.session_open(start_session),
                    "target_window_end": calendar.session_close(end_session),
                    "actionable_until": actionable_until,
                    "target_open": (
                        getattr(start_price, "open", None)
                        if start_price is not None
                        else None
                    ),
                    "target_close": (
                        getattr(end_price, "close", None)
                        if end_price is not None
                        else None
                    ),
                    "previous_period_direction": _direction(source_price),
                },
            )
        )
    return records


def _next_session_windows(
    features: pd.DataFrame,
    prices: pd.DataFrame,
    *,
    calendar: ExchangeSessionCalendar,
) -> list[tuple[int, dict[str, object]]]:
    price_lookup = {
        pd.Timestamp(row.exchange_session): row
        for row in prices.itertuples(index=False)
    }
    records: list[tuple[int, dict[str, object]]] = []
    for index, row in features.iterrows():
        horizon = calendar.horizon(
            decision_session=row["exchange_session"],
            decision_timestamp=row["decision_timestamp"],
            future_session_count=1,
        )
        target_price = price_lookup.get(horizon.entry_session)
        source_price = price_lookup.get(pd.Timestamp(row["exchange_session"]))
        records.append(
            (
                int(index),
                {
                    "target_window_start": horizon.entry_timestamp,
                    "target_window_end": horizon.exit_timestamp,
                    "actionable_until": horizon.entry_timestamp,
                    "target_open": (
                        getattr(target_price, "open", None)
                        if target_price is not None
                        else None
                    ),
                    "target_close": (
                        getattr(target_price, "close", None)
                        if target_price is not None
                        else None
                    ),
                    "previous_period_direction": _direction(source_price),
                },
            )
        )
    return records


def _prepare_features(
    frame: pd.DataFrame,
    *,
    specification: HorizonSpecification,
) -> pd.DataFrame:
    required = {
        "id",
        "symbol",
        "provider",
        "exchange_calendar",
        "exchange_session",
        "bar_timestamp",
        "bar_end_timestamp",
        "decision_timestamp",
        "feature_available_at",
        "feature_set",
    }
    missing = sorted(required.difference(frame.columns))
    if missing:
        raise MLContractError(
            "Rolling feature frame is missing columns: " + ", ".join(missing)
        )
    prepared = frame.copy()
    for column in (
        "bar_timestamp",
        "bar_end_timestamp",
        "decision_timestamp",
        "feature_available_at",
    ):
        prepared[column] = pd.to_datetime(
            prepared[column], utc=True, errors="coerce"
        )
    prepared["exchange_session"] = pd.to_datetime(
        prepared["exchange_session"], errors="coerce"
    ).dt.normalize()
    if prepared[
        [
            "bar_timestamp",
            "bar_end_timestamp",
            "decision_timestamp",
            "feature_available_at",
            "exchange_session",
        ]
    ].isna().any().any():
        raise MLContractError("Rolling features contain invalid timestamps")
    if not prepared["feature_set"].eq(specification.feature_set).all():
        raise MLContractError(
            f"Rolling {specification.horizon} features must use "
            f"{specification.feature_set}"
        )
    if not prepared["decision_timestamp"].eq(
        prepared["feature_available_at"]
    ).all():
        raise MLContractError(
            "Rolling information availability must equal the versioned decision time"
        )
    return prepared.sort_values(
        ["decision_timestamp", "symbol"], kind="mergesort"
    ).reset_index(drop=True)


def _prepare_prices(
    features: pd.DataFrame,
    frame: pd.DataFrame,
    *,
    specification: HorizonSpecification,
) -> pd.DataFrame:
    required = {"provider", "timestamp", "open", "close"}
    missing = sorted(required.difference(frame.columns))
    if missing:
        raise MLContractError(
            "Rolling adjusted prices are missing columns: " + ", ".join(missing)
        )
    mapping = features.loc[
        :,
        [
            "symbol",
            "exchange_calendar",
        ],
    ].drop_duplicates()
    if mapping.duplicated("symbol").any():
        raise MLContractError("Rolling symbol-to-calendar mapping is ambiguous")

    prices = frame.copy()
    if "symbol" not in prices:
        if len(mapping) != 1:
            raise MLContractError(
                "Prices without symbol can only be used for one symbol"
            )
        prices["symbol"] = str(mapping["symbol"].iloc[0])
    prices["symbol"] = prices["symbol"].astype(str).str.strip().str.upper()
    prices = prices.merge(
        mapping,
        on="symbol",
        how="left",
        validate="many_to_one",
    )
    if prices["exchange_calendar"].isna().any():
        raise MLContractError("Every rolling price must resolve to an exchange calendar")
    prices["bar_timestamp"] = pd.to_datetime(
        prices["timestamp"], utc=True, errors="coerce"
    )
    if "bar_end_timestamp" in prices:
        prices["bar_end_timestamp"] = pd.to_datetime(
            prices["bar_end_timestamp"], utc=True, errors="coerce"
        )
    else:
        prices["bar_end_timestamp"] = pd.NaT
    for column in ("open", "close"):
        prices[column] = pd.to_numeric(prices[column], errors="coerce")
    values = prices[["open", "close"]].to_numpy(dtype=float)
    if not np.isfinite(values).all() or (values <= 0.0).any():
        raise MLContractError("Rolling adjusted prices must be positive and finite")

    pieces: list[pd.DataFrame] = []
    for exchange_calendar, group in prices.groupby(
        "exchange_calendar", sort=False
    ):
        rough = group["bar_timestamp"].dt.tz_convert("UTC").dt.tz_localize(
            None
        ).dt.normalize()
        calendar = ExchangeSessionCalendar(
            str(exchange_calendar),
            start=rough.min() - pd.Timedelta(days=14),
            end=rough.max() + pd.Timedelta(days=120),
        )
        part = group.copy()
        if specification.source_timeframe == "1h":
            if part["bar_end_timestamp"].isna().any():
                raise MLContractError(
                    "Hourly prices require complete bar_end_timestamp timing"
                )
            part = attach_official_intraday_sessions(
                part,
                calendar_column="exchange_calendar",
                bar_timestamp_column="bar_timestamp",
                bar_end_column="bar_end_timestamp",
                include_extended_hours=specification.horizon == "1h",
            )
        else:
            daily_sessions = group["bar_timestamp"].dt.tz_convert(
                "UTC"
            ).dt.tz_localize(None).dt.normalize()
            valid = daily_sessions.isin(calendar.sessions)
            if not valid.all():
                bad = sorted(
                    {
                        value.date().isoformat()
                        for value in daily_sessions.loc[~valid]
                    }
                )
                raise MLContractError(
                    "Daily prices map to non-session dates: "
                    + ", ".join(bad[:10])
                )
            part["exchange_session"] = daily_sessions
            part["intraday_interval_eligible"] = False
        pieces.append(part)
    prepared = pd.concat(pieces, ignore_index=True, sort=False)
    if prepared.duplicated(
        ["symbol", "provider", "bar_timestamp"]
    ).any():
        raise MLContractError("Rolling adjusted prices contain duplicate bars")
    return prepared


def _prepare_intraday_target_prices(
    features: pd.DataFrame,
    frame: pd.DataFrame,
    *,
    specification: HorizonSpecification,
) -> pd.DataFrame:
    if (
        specification.target_price_provider is None
        or specification.target_price_timeframe is None
    ):
        raise MLContractError(
            f"Rolling {specification.horizon} lacks target-price metadata."
        )
    required = {"provider", "timestamp", "open", "close"}
    missing = sorted(required.difference(frame.columns))
    if missing:
        raise MLContractError(
            "Rolling adjusted target prices are missing columns: "
            + ", ".join(missing)
        )
    mapping = features.loc[
        :,
        [
            "symbol",
            "exchange_calendar",
        ],
    ].drop_duplicates()
    if mapping.duplicated("symbol").any():
        raise MLContractError("Rolling symbol-to-calendar mapping is ambiguous")

    prices = frame.copy()
    if "symbol" not in prices:
        if len(mapping) != 1:
            raise MLContractError(
                "Target prices without symbol can only be used for one symbol"
            )
        prices["symbol"] = str(mapping["symbol"].iloc[0])
    prices["symbol"] = prices["symbol"].astype(str).str.strip().str.upper()
    prices["provider"] = (
        prices["provider"].astype(str).str.strip().str.lower()
    )
    if not prices["provider"].eq(specification.target_price_provider).all():
        raise MLContractError(
            f"Rolling {specification.horizon} targets require "
            f"{specification.target_price_provider} prices."
        )
    if "timeframe" in prices:
        timeframes = prices["timeframe"].astype(str).str.strip().str.lower()
        if not timeframes.eq(specification.target_price_timeframe).all():
            raise MLContractError(
                f"Rolling {specification.horizon} targets require native "
                f"{specification.target_price_timeframe} prices."
            )
    prices = prices.merge(
        mapping,
        on="symbol",
        how="left",
        validate="many_to_one",
    )
    if prices["exchange_calendar"].isna().any():
        raise MLContractError(
            "Every rolling target price must resolve to an exchange calendar"
        )
    prices["bar_timestamp"] = pd.to_datetime(
        prices["timestamp"], utc=True, errors="coerce"
    )
    expected_end = prices["bar_timestamp"] + pd.Timedelta(minutes=1)
    if "bar_end_timestamp" in prices:
        prices["bar_end_timestamp"] = pd.to_datetime(
            prices["bar_end_timestamp"], utc=True, errors="coerce"
        )
        if not prices["bar_end_timestamp"].eq(expected_end).all():
            raise MLContractError(
                "Native 1m target prices must use exact interval-open timestamps"
            )
    else:
        prices["bar_end_timestamp"] = expected_end
    for column in ("open", "close"):
        prices[column] = pd.to_numeric(prices[column], errors="coerce")
    values = prices[["open", "close"]].to_numpy(dtype=float)
    if (
        prices["bar_timestamp"].isna().any()
        or not np.isfinite(values).all()
        or (values <= 0.0).any()
    ):
        raise MLContractError(
            "Rolling adjusted target prices must be valid, positive, and finite"
        )

    pieces: list[pd.DataFrame] = []
    for exchange_calendar, group in prices.groupby(
        "exchange_calendar", sort=False
    ):
        rough = (
            group["bar_timestamp"]
            .dt.tz_convert("UTC")
            .dt.tz_localize(None)
            .dt.normalize()
        )
        calendar = ExchangeSessionCalendar(
            str(exchange_calendar),
            start=rough.min() - pd.Timedelta(days=14),
            end=rough.max() + pd.Timedelta(days=120),
        )
        part = group.copy()
        part["exchange_session"] = (
            group["bar_timestamp"]
            .dt.tz_convert(calendar.exchange_timezone)
            .dt.tz_localize(None)
            .dt.normalize()
        )
        part["intraday_interval_eligible"] = False
        pieces.append(part)
    prepared = pd.concat(pieces, ignore_index=True, sort=False)
    if prepared.duplicated(
        ["symbol", "provider", "bar_timestamp"]
    ).any():
        raise MLContractError(
            "Rolling adjusted target prices contain duplicate bars"
        )
    return prepared


def _direction(row: object | None) -> float:
    if row is None:
        return np.nan
    open_value = _finite_positive_or_none(getattr(row, "open", None))
    close_value = _finite_positive_or_none(getattr(row, "close", None))
    if open_value is None or close_value is None:
        return np.nan
    return float(close_value > open_value)


def _finite_positive_or_none(value: object) -> float | None:
    if value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if isfinite(number) and number > 0.0 else None


def _empty_samples(feature_frame: pd.DataFrame) -> pd.DataFrame:
    output = feature_frame.iloc[0:0].copy()
    for column in (
        *ROLLING_SAMPLE_CONTEXT_COLUMNS,
        *ROLLING_SAMPLE_TIME_COLUMNS,
        *ROLLING_SAMPLE_TARGET_COLUMNS,
        "assumed_round_trip_cost",
    ):
        if column not in output:
            output[column] = pd.Series(dtype="object")
    return output


def materialized_at_to_series(
    value: pd.Timestamp,
    index: pd.Index,
) -> pd.Series:
    return pd.Series(value, index=index, dtype="datetime64[ns, UTC]")
