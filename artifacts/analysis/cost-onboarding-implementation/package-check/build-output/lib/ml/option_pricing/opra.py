from __future__ import annotations

import math
import numbers
import re

import pandas as pd

from ml.artifacts import utc_timestamp


OPRA_PRICE_SCALE = 1_000_000_000
DEFAULT_EMULATED_PREDICTION_LATENCY_SECONDS = 60


def normalize_fixed_price(value: object) -> float:
    if value is None or value is pd.NA or isinstance(value, bool):
        return math.nan
    if isinstance(value, numbers.Integral):
        return float(value) / OPRA_PRICE_SCALE
    result = float(value)
    return result if math.isfinite(result) else math.nan


def normalize_definition_records(records: pd.DataFrame) -> pd.DataFrame:
    """Project provider definitions for point-in-time option matching."""

    if records.index.name and records.index.name not in records.columns:
        records = records.reset_index()
    if records.empty:
        return pd.DataFrame(
            columns=(
                "symbol",
                "contract_symbol",
                "definition_effective_at",
                "expiration_date",
                "call_put",
                "strike",
                "multiplier",
                "standard_contract",
                "exercise_style",
                "settlement_type",
                "settlement_reference",
            )
        )
    raw_symbol_column = _first_column(records, "raw_symbol", "symbol")
    effective_column = _first_column(records, "ts_recv", "ts_event")
    expiration_column = _first_column(records, "expiration", "expiration_date")
    class_column = _first_column(records, "instrument_class", "class")
    strike_column = _first_column(records, "strike_price", "strike")
    multiplier_column = _optional_column(
        records, "contract_multiplier", "unit_of_measure_qty", "multiplier"
    )
    output = pd.DataFrame(index=records.index)
    output["contract_symbol"] = records[raw_symbol_column].astype("string").str.strip()
    if "underlying" in records:
        output["symbol"] = records["underlying"].astype("string").str.strip().str.upper()
    else:
        output["symbol"] = output["contract_symbol"].map(_underlying_from_occ)
    output["definition_effective_at"] = pd.to_datetime(
        records[effective_column], utc=True, errors="coerce"
    )
    expiration = records[expiration_column]
    if pd.api.types.is_numeric_dtype(expiration):
        expiration = pd.to_datetime(expiration, unit="ns", utc=True, errors="coerce")
    else:
        expiration = pd.to_datetime(expiration, utc=True, errors="coerce")
    output["expiration_date"] = expiration.dt.normalize()
    output["call_put"] = records[class_column].map(_normalize_call_put).astype("string")
    output["strike"] = records[strike_column].map(normalize_fixed_price)
    output["multiplier"] = (
        records[multiplier_column].map(_normalize_definition_quantity)
        if multiplier_column is not None
        else math.nan
    )
    occ_root = output["contract_symbol"].map(_underlying_from_occ).astype("string")
    occ_standard_shape = (
        output["call_put"].isin(("call", "put"))
        & occ_root.eq(output["symbol"])
        & occ_root.str.fullmatch(r"[A-Z.]{1,6}", na=False)
    )
    output["multiplier"] = output["multiplier"].where(
        output["multiplier"].notna(),
        pd.Series(100.0, index=output.index).where(occ_standard_shape),
    )
    cfi_column = _optional_column(records, "cfi")
    semantics = (
        records[cfi_column].map(_historical_option_cfi)
        if cfi_column is not None
        else pd.Series([(None, None, None)] * len(records), index=records.index)
    )
    cfi_standard = semantics.map(lambda value: value[2])
    cfi_allows_standard = (
        cfi_standard.eq(True)
        | records[cfi_column].astype("string").str.strip().eq("")
        if cfi_column is not None
        else True
    )
    output["standard_contract"] = (
        output["multiplier"].eq(100)
        & occ_standard_shape
        & cfi_allows_standard
    )
    exercise_column = _optional_column(records, "exercise_style", "exerciseStyle", "exercise")
    settlement_column = _optional_column(records, "settlement_type", "settlementType", "settlement")
    reference_column = _optional_column(records, "settlement_reference", "settlementReference")
    explicit_exercise = (
        records[exercise_column].astype("string").str.strip().str.upper()
        if exercise_column is not None
        else pd.Series(pd.NA, index=records.index, dtype="string")
    )
    explicit_settlement = (
        records[settlement_column].astype("string").str.strip().str.upper()
        if settlement_column is not None
        else pd.Series(pd.NA, index=records.index, dtype="string")
    )
    output["exercise_style"] = explicit_exercise.where(
        explicit_exercise.notna() & explicit_exercise.ne(""),
        semantics.map(lambda value: value[0]),
    ).fillna("AMBIGUOUS")
    output["settlement_type"] = explicit_settlement.where(
        explicit_settlement.notna() & explicit_settlement.ne(""),
        semantics.map(lambda value: value[1]),
    ).fillna("AMBIGUOUS")
    # OPRA's single-name OCC definitions can omit CFI/exercise/settlement even
    # when the raw symbol and parent identify a standard deliverable equity
    # option. Preserve ambiguity for nonstandard shapes and infer only this
    # well-defined OCC contract class.
    output.loc[
        output["standard_contract"] & output["exercise_style"].eq("AMBIGUOUS"),
        "exercise_style",
    ] = "AMERICAN"
    output.loc[
        output["standard_contract"] & output["settlement_type"].eq("AMBIGUOUS"),
        "settlement_type",
    ] = "PHYSICAL"
    output["settlement_reference"] = (
        records[reference_column].astype("string").str.strip()
        if reference_column is not None
        else pd.Series("", index=records.index)
    )
    if cfi_column is not None:
        cfi_reference = records[cfi_column].astype("string").map(
            lambda value: f"OPRA_DEFINITION_CFI:{str(value).strip().upper()}"
            if str(value).strip()
            else ""
        )
        output["settlement_reference"] = output["settlement_reference"].where(
            output["settlement_reference"].astype("string").str.strip().ne(""),
            cfi_reference,
        )
        output["cfi"] = records[cfi_column].astype("string").str.strip().str.upper()
    output["settlement_reference"] = output["settlement_reference"].where(
        output["settlement_reference"].astype("string").str.strip().ne(""),
        pd.Series("OCC_STANDARD_EQUITY_OPTION", index=output.index).where(
            output["standard_contract"]
        ),
    )
    return output.reset_index(drop=True)


def point_in_time_definition_asof(
    definitions: pd.DataFrame, asof: object
) -> pd.DataFrame:
    if definitions.empty:
        return definitions.copy()
    cutoff = utc_timestamp(asof)
    effective = pd.to_datetime(
        definitions["definition_effective_at"], utc=True, errors="coerce"
    )
    eligible = definitions.loc[effective.le(cutoff)].copy()
    if eligible.empty:
        return eligible
    eligible["definition_effective_at"] = pd.to_datetime(
        eligible["definition_effective_at"], utc=True, errors="coerce"
    )
    return (
        eligible.sort_values("definition_effective_at", kind="stable")
        .drop_duplicates("contract_symbol", keep="last")
        .reset_index(drop=True)
    )


def normalize_cbbo_records(records: pd.DataFrame) -> pd.DataFrame:
    """Normalize Databento CBBO timestamps and fixed-point prices."""

    if records.index.name and records.index.name not in records.columns:
        records = records.reset_index()
    if records.empty:
        return pd.DataFrame(
            columns=(
                "contract_symbol",
                "interval_start",
                "quote_timestamp",
                "bid",
                "ask",
                "mid",
                "publisher_id",
            )
        )
    symbol_column = _first_column(records, "raw_symbol", "symbol")
    bid_column = _first_column(records, "bid_px_00", "bid_px", "bid")
    ask_column = _first_column(records, "ask_px_00", "ask_px", "ask")
    output = pd.DataFrame(index=records.index)
    output["contract_symbol"] = records[symbol_column].astype("string").str.strip()
    timestamp_column = _first_column(records, "ts_recv", "ts_event")
    output["quote_timestamp"] = pd.to_datetime(
        records[timestamp_column], utc=True, errors="coerce"
    )
    output["interval_start"] = output["quote_timestamp"] - pd.Timedelta(minutes=1)
    output["bid"] = records[bid_column].map(normalize_fixed_price)
    output["ask"] = records[ask_column].map(normalize_fixed_price)
    output["mid"] = (output["bid"] + output["ask"]) / 2.0
    output["publisher_id"] = (
        pd.to_numeric(records["publisher_id"], errors="coerce")
        if "publisher_id" in records
        else pd.NA
    )
    valid = (
        output["quote_timestamp"].notna()
        & output["bid"].ge(0)
        & output["ask"].gt(0)
        & output["ask"].ge(output["bid"])
    )
    return output.loc[valid].reset_index(drop=True)


def select_historical_source_target(
    cbbo: pd.DataFrame,
    *,
    target_snapshot_for: object,
    prediction_available_at: object | None = None,
    source_staleness: pd.Timedelta = pd.Timedelta(minutes=5),
    target_forward_window: pd.Timedelta = pd.Timedelta(minutes=5),
) -> tuple[pd.DataFrame, pd.DataFrame]:
    target = utc_timestamp(target_snapshot_for)
    outcome_boundary = (
        utc_timestamp(prediction_available_at)
        if prediction_available_at is not None
        else target
    )
    if outcome_boundary < target:
        raise ValueError("Prediction availability cannot precede the market target")
    timestamps = pd.to_datetime(cbbo["quote_timestamp"], utc=True, errors="coerce")
    source = cbbo.loc[
        timestamps.lt(target) & timestamps.ge(target - source_staleness)
    ].copy()
    source["quote_timestamp"] = pd.to_datetime(source["quote_timestamp"], utc=True)
    source = (
        source.sort_values("quote_timestamp", kind="stable")
        .drop_duplicates("contract_symbol", keep="last")
        .reset_index(drop=True)
    )
    observed = cbbo.loc[
        timestamps.gt(outcome_boundary)
        & timestamps.le(outcome_boundary + target_forward_window)
    ].copy()
    observed["quote_timestamp"] = pd.to_datetime(observed["quote_timestamp"], utc=True)
    observed = (
        observed.sort_values("quote_timestamp", kind="stable")
        .drop_duplicates("contract_symbol", keep="first")
        .reset_index(drop=True)
    )
    return source, observed


def _normalize_definition_quantity(value: object) -> float:
    if value is None or value is pd.NA or isinstance(value, bool):
        return math.nan
    result = float(value)
    if not math.isfinite(result):
        return math.nan
    if result in {2_147_483_647.0, 4_294_967_295.0}:
        return math.nan
    return result / OPRA_PRICE_SCALE if abs(result) >= 1_000_000 else result


def _historical_option_cfi(value: object) -> tuple[str | None, str | None, bool | None]:
    cfi = str(value or "").strip().upper()
    if len(cfi) != 6 or cfi[0] != "O" or cfi[1] not in {"C", "P"}:
        return None, None, None
    style = {"A": "AMERICAN", "E": "EUROPEAN", "B": "BERMUDAN"}.get(cfi[2])
    settlement = {
        "P": "PHYSICAL",
        "C": "CASH",
        "N": "NON_DELIVERABLE",
        "E": "ELECT_AT_EXERCISE",
    }.get(cfi[4])
    standardized = {"S": True, "N": False}.get(cfi[5])
    if cfi[3] != "S":
        return None, None, False
    return style, settlement, standardized


def _first_column(frame: pd.DataFrame, *names: str) -> str:
    found = _optional_column(frame, *names)
    if found is None:
        raise ValueError("OPRA records are missing one of: " + ", ".join(names))
    return found


def _optional_column(frame: pd.DataFrame, *names: str) -> str | None:
    return next((name for name in names if name in frame.columns), None)


def _underlying_from_occ(raw_symbol: object) -> str:
    match = re.match(r"^([A-Z.]{1,6})\s*\d{6}[CP]", str(raw_symbol).strip().upper())
    return match.group(1) if match else ""


def _normalize_call_put(value: object) -> str | None:
    token = str(value).strip().upper()
    if token in {"C", "CALL", "1"}:
        return "call"
    if token in {"P", "PUT", "2"}:
        return "put"
    return None


__all__ = [
    "DEFAULT_EMULATED_PREDICTION_LATENCY_SECONDS",
    "OPRA_PRICE_SCALE",
    "normalize_cbbo_records",
    "normalize_definition_records",
    "normalize_fixed_price",
    "point_in_time_definition_asof",
    "select_historical_source_target",
]
