from __future__ import annotations

from datetime import datetime, timezone
from typing import Mapping

import numpy as np
import pandas as pd

from signals.consensus import build_daily_technical_consensus
from signals.fundamental_context import attach_lifecycle_fundamentals

CALCULATION_NAME = "fundamental-technical-lifecycle"
CALCULATION_VERSION = "1.0.0"


def calculate_fundamental_technical_lifecycle(
    technical_frames: Mapping[tuple[str, str], pd.DataFrame],
    fundamentals: pd.DataFrame,
    *,
    symbol: str,
) -> pd.DataFrame:
    """Build a daily lifecycle signal without blending away disagreement.

    Technical bars are conservatively treated as available at the end of their
    timeframe. Fundamental rows become available only at their filing-time
    ``effective_from`` timestamp.
    """
    clean_symbol = symbol.strip().upper()
    if not clean_symbol:
        raise ValueError("symbol is required")

    output = build_daily_technical_consensus(technical_frames)
    output.insert(0, "symbol", clean_symbol)
    output = attach_lifecycle_fundamentals(output, fundamentals)

    fundamental_score = pd.to_numeric(output["fundamental_score"], errors="coerce")
    technical_score = pd.to_numeric(output["technical_consensus_score"], errors="coerce")
    fundamental_confidence = (
        pd.to_numeric(output["fundamental_confidence"], errors="coerce")
        .clip(0.0, 100.0)
        .fillna(0.0)
    )
    technical_confidence = (
        pd.to_numeric(output["technical_consensus_confidence"], errors="coerce")
        .clip(0.0, 100.0)
        .fillna(0.0)
    )
    output["lifecycle_confidence"] = np.sqrt(
        fundamental_confidence * technical_confidence
    ).clip(0.0, 100.0)
    output["fundamental_technical_spread"] = fundamental_score - technical_score
    output["agreement_strength"] = (
        100.0 - output["fundamental_technical_spread"].abs()
    ).clip(0.0, 100.0)
    output["divergence_strength"] = (
        output["fundamental_technical_spread"].abs()
        * output["lifecycle_confidence"]
        / 100.0
    ).clip(0.0, 100.0)
    output["lifecycle_phase"] = _classify_phase(output)
    output["setup_quality"] = _setup_quality(output)
    output["calculation"] = CALCULATION_NAME
    output["calculation_version"] = CALCULATION_VERSION
    output["generated_at"] = datetime.now(timezone.utc).isoformat()

    result = output.loc[output["technical_consensus_score"].notna()].reset_index(drop=True)
    if result.empty:
        raise ValueError("Lifecycle signal produced no initialized technical consensus rows.")
    return result


def _classify_phase(frame: pd.DataFrame) -> pd.Series:
    fundamental = pd.to_numeric(frame["fundamental_score"], errors="coerce")
    technical = pd.to_numeric(frame["technical_consensus_score"], errors="coerce")
    technical_confidence = pd.to_numeric(
        frame["technical_consensus_confidence"], errors="coerce"
    ).fillna(0.0)
    fundamental_change = pd.to_numeric(
        frame["fundamental_change_1q"], errors="coerce"
    ).fillna(0.0)
    technical_change = pd.to_numeric(
        frame["technical_consensus_change_5d"], errors="coerce"
    ).fillna(0.0)
    term_spread = pd.to_numeric(
        frame["technical_term_spread"], errors="coerce"
    ).fillna(0.0)

    available = (
        fundamental.notna()
        & technical.notna()
        & technical_confidence.ge(20.0)
    )
    fundamental_high = fundamental >= 60.0
    fundamental_low = fundamental <= 40.0
    technical_high = technical >= 60.0
    technical_low = technical <= 40.0
    turning_up = (technical_change >= 3.0) | (term_spread >= 5.0)
    fundamental_recovering = fundamental_change >= 5.0

    conditions = [
        ~available,
        fundamental_high & technical_high,
        fundamental_low & technical_low,
        fundamental_high & (technical < 55.0) & turning_up,
        fundamental_low & technical_high & fundamental_recovering,
        fundamental_low & technical_high,
        (~fundamental_high)
        & (~fundamental_low)
        & (technical >= 55.0)
        & turning_up,
    ]
    choices = [
        "INSUFFICIENT_DATA",
        "CONFIRMED_EXPANSION",
        "CONFIRMED_CONTRACTION",
        "EARLY_ACCUMULATION",
        "RECOVERY_ATTEMPT",
        "LATE_CYCLE_DISTRIBUTION",
        "RECOVERY_ATTEMPT",
    ]
    return pd.Series(
        np.select(conditions, choices, default="TRANSITION_MIXED"),
        index=frame.index,
        dtype="object",
    )


def _setup_quality(frame: pd.DataFrame) -> pd.Series:
    phase = frame["lifecycle_phase"].astype(str)
    confidence = pd.to_numeric(frame["lifecycle_confidence"], errors="coerce").fillna(0.0)
    fundamental = pd.to_numeric(frame["fundamental_score"], errors="coerce")
    technical = pd.to_numeric(frame["technical_consensus_score"], errors="coerce")
    clarity = (
        fundamental.sub(50.0).abs() + technical.sub(50.0).abs()
    ).clip(0.0, 100.0)
    agreement = pd.to_numeric(frame["agreement_strength"], errors="coerce").fillna(0.0)
    divergence = pd.to_numeric(frame["divergence_strength"], errors="coerce").fillna(0.0)
    timing = pd.to_numeric(frame["timing_score"], errors="coerce").fillna(50.0)
    timing_extremity = (timing - 50.0).abs() * 2.0

    strength = pd.Series(25.0, index=frame.index)
    confirmed = phase.isin({"CONFIRMED_EXPANSION", "CONFIRMED_CONTRACTION"})
    divergence_phase = phase.isin(
        {"EARLY_ACCUMULATION", "LATE_CYCLE_DISTRIBUTION"}
    )
    recovery = phase.eq("RECOVERY_ATTEMPT")
    insufficient = phase.eq("INSUFFICIENT_DATA")

    strength.loc[confirmed] = (
        clarity.loc[confirmed] * 0.60 + agreement.loc[confirmed] * 0.40
    )
    strength.loc[divergence_phase] = (
        divergence.loc[divergence_phase] * 0.70
        + timing_extremity.loc[divergence_phase] * 0.30
    )
    strength.loc[recovery] = (
        timing.loc[recovery] * 0.55 + divergence.loc[recovery] * 0.45
    )
    strength.loc[insufficient] = 0.0
    return (strength * 0.60 + confidence * 0.40).clip(0.0, 100.0)
