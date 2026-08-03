from __future__ import annotations

from typing import Mapping

import numpy as np
import pandas as pd

TIMEFRAME_WEIGHTS = {
    "5m": 0.05,
    "30m": 0.10,
    "1h": 0.20,
    "1d": 0.35,
    "1w": 0.30,
}
TIMEFRAME_HALF_LIFE_DAYS = {
    "5m": 1.5,
    "30m": 2.0,
    "1h": 3.0,
    "1d": 10.0,
    "1w": 28.0,
}
TIMEFRAME_DURATIONS = {
    "5m": pd.Timedelta(minutes=5),
    "30m": pd.Timedelta(minutes=30),
    "1h": pd.Timedelta(hours=1),
    "1d": pd.Timedelta(days=1),
    "1w": pd.Timedelta(days=7),
}
SHORT_TIMEFRAMES = ("5m", "30m", "1h")
LONG_TIMEFRAMES = ("1d", "1w")


def build_daily_technical_consensus(
    technical_frames: Mapping[tuple[str, str], pd.DataFrame],
) -> pd.DataFrame:
    """Combine providers within timeframes, then combine canonical timeframes."""
    prepared = _prepare_frames(technical_frames)
    if not prepared:
        raise ValueError(
            "Lifecycle signal requires at least one market-regime technical frame "
            f"from: {', '.join(TIMEFRAME_WEIGHTS)}."
        )

    output = pd.DataFrame({"timestamp": _daily_anchors(prepared)})
    output["signal_date"] = output["timestamp"].dt.date.astype(str)
    columns: dict[str, dict[str, str]] = {}

    for timeframe in TIMEFRAME_WEIGHTS:
        providers = {
            provider: frame
            for (provider, candidate), frame in prepared.items()
            if candidate == timeframe
        }
        score, confidence, age_days, provider_count, quality = _aggregate_timeframe(
            output["timestamp"], providers, timeframe=timeframe
        )
        prefix = f"technical_{timeframe}"
        output[f"{prefix}_score"] = score
        output[f"{prefix}_confidence"] = confidence
        output[f"{prefix}_age_days"] = age_days
        output[f"{prefix}_provider_count"] = provider_count
        output[f"{prefix}_quality"] = quality
        columns[timeframe] = {
            "score": f"{prefix}_score",
            "provider_count": f"{prefix}_provider_count",
            "quality": f"{prefix}_quality",
        }

    base_weights = pd.Series(TIMEFRAME_WEIGHTS, dtype=float)
    available_weight = pd.Series(0.0, index=output.index)
    effective_weight = pd.Series(0.0, index=output.index)
    weighted_score = pd.Series(0.0, index=output.index)

    for timeframe, names in columns.items():
        score = pd.to_numeric(output[names["score"]], errors="coerce")
        quality = pd.to_numeric(output[names["quality"]], errors="coerce").clip(0.0, 1.0)
        base_weight = base_weights[timeframe]
        row_weight = base_weight * quality.fillna(0.0)
        available_weight += score.notna().astype(float) * base_weight
        effective_weight += row_weight
        weighted_score += score.fillna(0.0) * row_weight

    output["technical_consensus_score"] = (
        weighted_score / effective_weight.where(effective_weight > 0)
    ).clip(0.0, 100.0)
    output["technical_timeframe_coverage"] = (
        100.0 * available_weight / base_weights.sum()
    ).clip(0.0, 100.0)
    output["technical_consensus_confidence"] = (
        100.0 * effective_weight / base_weights.sum()
    ).clip(0.0, 100.0)
    output["technical_effective_weight"] = effective_weight
    output["technical_timeframes_available"] = sum(
        output[names["score"]].notna().astype("int64") for names in columns.values()
    )
    output["technical_provider_observations"] = sum(
        pd.to_numeric(output[names["provider_count"]], errors="coerce")
        .fillna(0)
        .astype("int64")
        for names in columns.values()
    )
    output["short_term_technical_score"] = _term_consensus(
        output, columns, SHORT_TIMEFRAMES
    )
    output["long_term_technical_score"] = _term_consensus(
        output, columns, LONG_TIMEFRAMES
    )
    output["technical_term_spread"] = (
        output["short_term_technical_score"] - output["long_term_technical_score"]
    )
    output["technical_consensus_change_5d"] = output["technical_consensus_score"].diff(5)
    momentum_score = _bounded_score(output["technical_consensus_change_5d"], scale=8.0)
    timing_base = output["short_term_technical_score"].fillna(
        output["technical_consensus_score"]
    )
    output["timing_score"] = (
        timing_base * 0.65 + momentum_score.fillna(50.0) * 0.35
    ).clip(0.0, 100.0)
    return output


def _prepare_frames(
    technical_frames: Mapping[tuple[str, str], pd.DataFrame],
) -> dict[tuple[str, str], pd.DataFrame]:
    prepared: dict[tuple[str, str], pd.DataFrame] = {}
    for (provider, timeframe), frame in technical_frames.items():
        clean_provider = str(provider).strip().lower()
        clean_timeframe = str(timeframe).strip().lower()
        if clean_timeframe not in TIMEFRAME_WEIGHTS or frame.empty:
            continue
        if not {"timestamp", "technical_score"}.issubset(frame.columns):
            continue
        normalized = frame.copy()
        normalized["timestamp"] = pd.to_datetime(
            normalized["timestamp"], utc=True, errors="coerce"
        )
        normalized["technical_score"] = pd.to_numeric(
            normalized["technical_score"], errors="coerce"
        )
        confidence_column = (
            "confidence_score"
            if "confidence_score" in normalized.columns
            else "technical_confidence"
            if "technical_confidence" in normalized.columns
            else None
        )
        normalized["technical_confidence"] = (
            pd.to_numeric(normalized[confidence_column], errors="coerce")
            if confidence_column
            else 70.0
        )
        normalized["technical_confidence"] = normalized[
            "technical_confidence"
        ].fillna(50.0).clip(0.0, 100.0)
        normalized = (
            normalized.dropna(subset=["timestamp", "technical_score"])
            .sort_values("timestamp")
            .drop_duplicates("timestamp", keep="last")
            .reset_index(drop=True)
        )
        if normalized.empty:
            continue
        normalized["available_at"] = (
            normalized["timestamp"] + TIMEFRAME_DURATIONS[clean_timeframe]
        )
        prepared[(clean_provider, clean_timeframe)] = normalized[
            ["available_at", "technical_score", "technical_confidence"]
        ]
    return prepared


def _daily_anchors(
    technical_frames: Mapping[tuple[str, str], pd.DataFrame],
) -> pd.Series:
    availability = pd.concat(
        [frame["available_at"] for frame in technical_frames.values()],
        ignore_index=True,
    ).dropna()
    dates = pd.Series(availability.dt.floor("D").drop_duplicates().sort_values())
    return (
        dates + pd.Timedelta(days=1) - pd.Timedelta(microseconds=1)
    ).reset_index(drop=True)


def _aggregate_timeframe(
    anchors: pd.Series,
    provider_frames: Mapping[str, pd.DataFrame],
    *,
    timeframe: str,
) -> tuple[pd.Series, pd.Series, pd.Series, pd.Series, pd.Series]:
    if not provider_frames:
        empty = pd.Series(np.nan, index=anchors.index, dtype=float)
        zeros = pd.Series(0, index=anchors.index, dtype="int64")
        return empty, empty.copy(), empty.copy(), zeros, empty.copy()

    scores: list[pd.Series] = []
    confidences: list[pd.Series] = []
    ages: list[pd.Series] = []
    qualities: list[pd.Series] = []
    anchor_frame = pd.DataFrame({"timestamp": anchors}).sort_values("timestamp")
    half_life = TIMEFRAME_HALF_LIFE_DAYS[timeframe]

    for provider, frame in sorted(provider_frames.items()):
        right = frame.sort_values("available_at").rename(
            columns={
                "technical_score": f"{provider}__score",
                "technical_confidence": f"{provider}__confidence",
                "available_at": f"{provider}__available_at",
            }
        )
        joined = pd.merge_asof(
            anchor_frame,
            right,
            left_on="timestamp",
            right_on=f"{provider}__available_at",
            direction="backward",
            allow_exact_matches=True,
        )
        age = (
            joined["timestamp"] - joined[f"{provider}__available_at"]
        ).dt.total_seconds() / 86_400.0
        age = age.clip(lower=0.0)
        freshness = np.power(0.5, age / half_life)
        confidence = pd.to_numeric(
            joined[f"{provider}__confidence"], errors="coerce"
        ).clip(0.0, 100.0)
        score = pd.to_numeric(joined[f"{provider}__score"], errors="coerce")
        quality = (confidence / 100.0 * freshness).where(score.notna())
        scores.append(score.rename(provider))
        confidences.append(confidence.rename(provider))
        ages.append(age.where(score.notna()).rename(provider))
        qualities.append(quality.rename(provider))

    score_frame = pd.concat(scores, axis=1)
    confidence_frame = pd.concat(confidences, axis=1)
    age_frame = pd.concat(ages, axis=1)
    quality_frame = pd.concat(qualities, axis=1)
    quality_sum = quality_frame.sum(axis=1, min_count=1)
    score = (
        (score_frame * quality_frame).sum(axis=1, min_count=1)
        / quality_sum.where(quality_sum > 0)
    )
    confidence = (
        (confidence_frame * quality_frame).sum(axis=1, min_count=1)
        / quality_sum.where(quality_sum > 0)
    )
    age_days = (
        (age_frame * quality_frame).sum(axis=1, min_count=1)
        / quality_sum.where(quality_sum > 0)
    )
    provider_count = score_frame.notna().sum(axis=1).astype("int64")
    quality = quality_frame.max(axis=1, skipna=True)
    return score, confidence, age_days, provider_count, quality


def _term_consensus(
    frame: pd.DataFrame,
    columns: Mapping[str, Mapping[str, str]],
    timeframes: tuple[str, ...],
) -> pd.Series:
    numerator = pd.Series(0.0, index=frame.index)
    denominator = pd.Series(0.0, index=frame.index)
    for timeframe in timeframes:
        score = pd.to_numeric(frame[columns[timeframe]["score"]], errors="coerce")
        quality = pd.to_numeric(
            frame[columns[timeframe]["quality"]], errors="coerce"
        ).fillna(0.0)
        weight = TIMEFRAME_WEIGHTS[timeframe] * quality
        numerator += score.fillna(0.0) * weight
        denominator += weight
    return (numerator / denominator.where(denominator > 0)).clip(0.0, 100.0)


def _bounded_score(values: pd.Series, *, scale: float) -> pd.Series:
    return pd.Series(
        50.0 + 50.0 * np.tanh(values.to_numpy(dtype=float) / scale),
        index=values.index,
        dtype=float,
    )
