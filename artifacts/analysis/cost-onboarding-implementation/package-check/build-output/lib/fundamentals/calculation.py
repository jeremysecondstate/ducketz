from __future__ import annotations

from datetime import datetime, timezone
from typing import Iterable

import numpy as np
import pandas as pd

CALCULATION_NAME = "fundamental-direction"
CALCULATION_VERSION = "1.0.0"
COMPONENT_WEIGHTS = {
    "earnings_momentum_score": 0.25,
    "cash_conversion_score": 0.25,
    "accrual_quality_score": 0.20,
    "balance_sheet_score": 0.15,
    "tax_quality_score": 0.10,
    "investment_dilution_score": 0.05,
}
METRIC_ALIASES = {
    "revenue": ("income", "revenue"),
    "cost_of_revenue": ("income", "cost_of_revenue"),
    "operating_income": ("income", "operating_income"),
    "net_income": ("income", "net_income"),
    "income_before_tax": ("income", "income_before_tax"),
    "income_tax_expense": ("income", "income_tax_expense", "income_tax_expense_benefit"),
    "research_and_development": (
        "income",
        "research_and_development_expenses",
        "research_and_development_expense",
    ),
    "diluted_shares": (
        "income",
        "weighted_average_shs_out_dil",
        "weighted_average_shares_outstanding_diluted",
    ),
    "receivables": (
        "balance",
        "net_receivables",
        "accounts_receivables",
        "accounts_receivable_net",
    ),
    "inventory": ("balance", "inventory", "inventory_net"),
    "cash": (
        "balance",
        "cash_and_cash_equivalents",
        "cash_and_short_term_investments",
        "cash_and_cash_equivalents_at_carrying_value",
    ),
    "short_term_investments": ("balance", "short_term_investments"),
    "current_assets": ("balance", "total_current_assets"),
    "current_liabilities": ("balance", "total_current_liabilities"),
    "total_debt": (
        "balance",
        "total_debt",
        "short_term_debt_plus_long_term_debt",
        "long_term_debt",
    ),
    "stockholders_equity": ("balance", "total_stockholders_equity", "stockholders_equity"),
    "ppe": ("balance", "property_plant_equipment_net", "property_plant_and_equipment_net"),
    "total_assets": ("balance", "total_assets"),
    "cfo": (
        "cash",
        "net_cash_provided_by_operating_activities",
        "operating_cash_flow",
        "net_cash_provided_by_used_in_operating_activities",
    ),
    "free_cash_flow": ("cash", "free_cash_flow"),
    "capex": (
        "cash",
        "capital_expenditure",
        "investments_in_property_plant_and_equipment",
        "payments_to_acquire_property_plant_and_equipment",
    ),
    "stock_based_compensation": ("cash", "stock_based_compensation", "share_based_compensation"),
}


def calculate_fundamental_direction(
    income_statement: pd.DataFrame,
    balance_sheet: pd.DataFrame,
    cash_flow_statement: pd.DataFrame,
    *,
    symbol: str,
    period_type: str,
) -> pd.DataFrame:
    """Calculate a transparent, filing-time 0-100 fundamental direction score."""
    cadence = period_type.strip().lower()
    if cadence not in {"quarterly", "annual"}:
        raise ValueError("period_type must be quarterly or annual")
    clean_symbol = symbol.strip().upper()
    if not clean_symbol:
        raise ValueError("symbol is required")

    statements = [
        _prepare_statement(income_statement, "income", clean_symbol),
        _prepare_statement(balance_sheet, "balance", clean_symbol),
        _prepare_statement(cash_flow_statement, "cash", clean_symbol),
    ]
    if any(frame.empty for frame in statements):
        raise ValueError("Fundamental direction requires income, balance-sheet, and cash-flow history.")
    merged = statements[0].merge(statements[1], on=["period_end_date", "period"], how="outer")
    merged = merged.merge(statements[2], on=["period_end_date", "period"], how="outer")
    merged = merged.sort_values(["period_end_date", "period"]).drop_duplicates(
        ["period_end_date", "period"], keep="last"
    ).reset_index(drop=True)

    merged["accepted_date"] = _row_max_datetime(
        merged, [column for column in merged if column.endswith("__accepted_date")]
    )
    merged["filing_date"] = _row_max_datetime(
        merged, [column for column in merged if column.endswith("__filing_date")]
    )
    merged["effective_from"] = merged["accepted_date"].fillna(merged["filing_date"])
    estimated = merged["effective_from"].isna()
    merged.loc[estimated, "effective_from"] = merged.loc[estimated, "period_end_date"] + pd.Timedelta(days=90)
    merged["latest_source_fetch"] = _row_max_datetime(
        merged, [column for column in merged if column.endswith("__fetched_at")]
    )

    metrics = pd.DataFrame(
        {
            name: _metric(merged, aliases[0], *aliases[1:])
            for name, aliases in METRIC_ALIASES.items()
        }
    )
    metrics["cash_total"] = metrics["cash"].fillna(0.0) + metrics["short_term_investments"].fillna(0.0)
    metrics["free_cash_flow"] = metrics["free_cash_flow"].fillna(metrics["cfo"] - metrics["capex"].abs())
    lag = 4 if cadence == "quarterly" else 1
    growth = {column: _growth(metrics[column], lag) for column in metrics}

    operating_margin = metrics["operating_income"] / _nonzero(metrics["revenue"].abs())
    fcf_margin = metrics["free_cash_flow"] / _nonzero(metrics["revenue"].abs())
    cfo_to_income = metrics["cfo"] / _nonzero(metrics["net_income"].abs())
    receivables_mismatch = growth["receivables"] - growth["revenue"]
    inventory_mismatch = growth["inventory"] - growth["cost_of_revenue"]
    debt_cfo_mismatch = growth["total_debt"] - growth["cfo"]
    cash_to_debt = metrics["cash_total"] / _nonzero(metrics["total_debt"].abs())
    current_ratio = metrics["current_assets"] / _nonzero(metrics["current_liabilities"].abs())
    effective_tax_rate = metrics["income_tax_expense"] / _nonzero(metrics["income_before_tax"])
    capex_coverage = metrics["cfo"] / _nonzero(metrics["capex"].abs())
    stock_comp_intensity = metrics["stock_based_compensation"] / _nonzero(metrics["revenue"].abs())
    rd_intensity_change = (
        metrics["research_and_development"] / _nonzero(metrics["revenue"].abs())
    ).diff(lag).abs()

    components = pd.DataFrame(
        {
            "earnings_momentum_score": _mean_scores(
                _score(growth["revenue"], 0.20),
                _score(growth["operating_income"], 0.30),
                _score(growth["net_income"], 0.30),
                _score(operating_margin.diff(lag), 0.05),
            ),
            "cash_conversion_score": _mean_scores(
                _score(growth["cfo"], 0.30),
                _score(fcf_margin, 0.12),
                _score(cfo_to_income.clip(-2.0, 4.0) - 1.0, 0.75),
            ),
            "accrual_quality_score": _mean_scores(
                _score((metrics["cfo"] - metrics["net_income"]) / _nonzero(metrics["total_assets"].abs()), 0.08),
                _score(receivables_mismatch, 0.20, inverse=True),
                _score(inventory_mismatch, 0.20, inverse=True),
            ),
            "balance_sheet_score": _mean_scores(
                _score(cash_to_debt.clip(0.0, 5.0) - 0.50, 0.75),
                _score(current_ratio.clip(0.0, 5.0) - 1.20, 0.80),
                _score(debt_cfo_mismatch, 0.30, inverse=True),
            ),
            "tax_quality_score": _mean_scores(
                (100.0 - effective_tax_rate.diff(lag).abs() / 0.25 * 100.0).clip(0.0, 100.0),
                (100.0 - (growth["income_tax_expense"] - growth["income_before_tax"]).abs() / 0.50 * 100.0).clip(0.0, 100.0),
                (100.0 - (effective_tax_rate - 0.21).abs() / 0.30 * 100.0)
                .clip(0.0, 100.0)
                .where(metrics["income_before_tax"] > 0),
            ),
            "investment_dilution_score": _mean_scores(
                _score(capex_coverage.clip(-2.0, 6.0) - 1.50, 1.25),
                _score(stock_comp_intensity - 0.05, 0.08, inverse=True),
                _score(growth["diluted_shares"], 0.08, inverse=True),
                _score(rd_intensity_change, 0.08, inverse=True),
            ),
        }
    ).clip(0.0, 100.0)

    weighted = sum(components[column].fillna(0.0) * weight for column, weight in COMPONENT_WEIGHTS.items())
    available = sum(components[column].notna().astype(float) * weight for column, weight in COMPONENT_WEIGHTS.items())
    fundamental_score = weighted / available.where(available > 0)
    agreement = (1.0 - components.std(axis=1, ddof=0) / 50.0).clip(0.0, 1.0)
    component_coverage = components.notna().mean(axis=1)
    metric_coverage = metrics[list(METRIC_ALIASES)].notna().mean(axis=1)
    history_periods = pd.Series(np.arange(1, len(merged) + 1), index=merged.index, dtype=float)
    history_maturity = (history_periods / (12.0 if cadence == "quarterly" else 5.0)).clip(0.0, 1.0)
    confidence = 100.0 * (
        component_coverage * 0.35
        + metric_coverage * 0.25
        + history_maturity * 0.20
        + agreement.fillna(0.0) * 0.10
        + pd.Series(np.where(estimated, 0.60, 1.0), index=merged.index) * 0.10
    )

    result = pd.DataFrame(
        {
            "symbol": clean_symbol,
            "fiscal_period": merged["period"],
            "period_type": cadence,
            "period_end_date": merged["period_end_date"],
            "filing_date": merged["filing_date"],
            "accepted_date": merged["accepted_date"],
            "effective_from": merged["effective_from"],
            "effective_date_estimated": estimated,
            "fundamental_score": fundamental_score.clip(0.0, 100.0),
            "fundamental_confidence": confidence.clip(0.0, 100.0),
            **{column: components[column] for column in components},
            "component_agreement": agreement * 100.0,
            "component_coverage": component_coverage * 100.0,
            "metric_coverage": metric_coverage * 100.0,
            "history_periods": history_periods.astype("int64"),
            "revenue_growth": growth["revenue"],
            "operating_income_growth": growth["operating_income"],
            "net_income_growth": growth["net_income"],
            "cfo_growth": growth["cfo"],
            "receivables_growth_minus_revenue_growth": receivables_mismatch,
            "inventory_growth_minus_cost_of_revenue_growth": inventory_mismatch,
            "cfo_growth_minus_net_income_growth": growth["cfo"] - growth["net_income"],
            "debt_growth_minus_cfo_growth": debt_cfo_mismatch,
            "operating_margin": operating_margin,
            "free_cash_flow_margin": fcf_margin,
            "cfo_to_net_income": cfo_to_income,
            "cash_to_debt": cash_to_debt,
            "current_ratio": current_ratio,
            "effective_tax_rate": effective_tax_rate,
            "calculation": CALCULATION_NAME,
            "calculation_version": CALCULATION_VERSION,
            "source": "fmp",
            "latest_source_fetch": merged["latest_source_fetch"],
            "calculated_at": datetime.now(timezone.utc).isoformat(),
        }
    )
    result["fundamental_label"] = pd.cut(
        result["fundamental_score"],
        [-np.inf, 20.0, 40.0, 60.0, 80.0, np.inf],
        labels=["STRONG_DETERIORATION", "DETERIORATION", "MIXED", "IMPROVEMENT", "STRONG_IMPROVEMENT"],
        right=False,
    ).astype("object")
    initialized = history_periods.ge(lag + 1) & components.notna().sum(axis=1).ge(4)
    result = result.loc[initialized & result["fundamental_score"].notna()].sort_values("effective_from").reset_index(drop=True)
    if result.empty:
        raise ValueError(f"Fundamental direction needs at least {lag + 1} usable {cadence} periods and four score components.")
    return result


def _prepare_statement(frame: pd.DataFrame, prefix: str, symbol: str) -> pd.DataFrame:
    if frame.empty:
        return pd.DataFrame()
    prepared = frame.copy()
    prepared.columns = [_column_name(column) for column in prepared]
    if "symbol" in prepared:
        prepared = prepared.loc[prepared["symbol"].astype(str).str.strip().str.upper().eq(symbol)].copy()
    date_column = next(
        (column for column in ("date", "period_end_date", "periodenddate", "calendar_date", "calendardate") if column in prepared),
        None,
    )
    if prepared.empty or date_column is None:
        return pd.DataFrame()
    prepared["period_end_date"] = pd.to_datetime(prepared[date_column], utc=True, errors="coerce")
    if "period" not in prepared:
        prepared["period"] = "FY"
    prepared["period"] = prepared["period"].astype(str).str.strip().str.upper().replace({"": "FY", "NAN": "FY", "NONE": "FY"})
    for canonical, aliases in {
        "accepted_date": ("accepted_date", "accepteddate"),
        "filing_date": ("filing_date", "filingdate"),
        "fetched_at": ("fetched_at", "fetchedat"),
    }.items():
        source = next((alias for alias in aliases if alias in prepared), None)
        prepared[canonical] = pd.to_datetime(prepared[source], utc=True, errors="coerce") if source else pd.NaT
    prepared = prepared.dropna(subset=["period_end_date"]).sort_values(
        ["period_end_date", "accepted_date", "filing_date", "fetched_at"], kind="stable"
    ).drop_duplicates(["period_end_date", "period"], keep="last")
    excluded = {"period_end_date", "period", date_column, "symbol"}
    payload = {
        f"{prefix}__{column}": prepared[column].to_numpy()
        for column in prepared
        if column not in excluded
    }
    return pd.concat(
        [
            prepared[["period_end_date", "period"]].reset_index(drop=True),
            pd.DataFrame(payload),
        ],
        axis=1,
    )


def _metric(frame: pd.DataFrame, prefix: str, *names: str) -> pd.Series:
    for name in names:
        for column in (f"{prefix}__{name}", f"{prefix}__{name.replace('_', '')}"):
            if column in frame:
                values = pd.to_numeric(frame[column], errors="coerce")
                if values.notna().any():
                    return values
    return pd.Series(np.nan, index=frame.index, dtype=float)


def _growth(values: pd.Series, lag: int) -> pd.Series:
    previous = values.shift(lag)
    return ((values - previous) / _nonzero(previous.abs())).clip(-2.0, 2.0)


def _nonzero(values: pd.Series) -> pd.Series:
    return values.where(values.abs() > 1e-12)


def _score(values: pd.Series, scale: float, *, inverse: bool = False) -> pd.Series:
    direction = -1.0 if inverse else 1.0
    return pd.Series(50.0 + direction * 50.0 * np.tanh(values.to_numpy(dtype=float) / scale), index=values.index)


def _mean_scores(*values: pd.Series) -> pd.Series:
    return pd.concat(values, axis=1).mean(axis=1, skipna=True)


def _row_max_datetime(frame: pd.DataFrame, columns: Iterable[str]) -> pd.Series:
    selected = list(columns)
    if not selected:
        return pd.Series(pd.NaT, index=frame.index, dtype="datetime64[ns, UTC]")
    return pd.concat(
        [pd.to_datetime(frame[column], utc=True, errors="coerce").rename(column) for column in selected], axis=1
    ).max(axis=1)


def _column_name(value: object) -> str:
    text = str(value).strip()
    output: list[str] = []
    for index, character in enumerate(text):
        if character.isupper() and index and text[index - 1].islower():
            output.append("_")
        output.append(character.lower() if character.isalnum() else "_")
    clean = "".join(output)
    while "__" in clean:
        clean = clean.replace("__", "_")
    return clean.strip("_") or "value"
