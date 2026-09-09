from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

import pandas as pd

from ml.sequence_encoder.publication import (
    SequencePublicationError,
    read_current_sequence_publication,
    resolve_current_sequence_output,
)


@dataclass(frozen=True)
class SequenceConsumerResult:
    status: str
    consumer: str
    distributions: pd.DataFrame
    matched_routes: int
    requested_routes: int
    details: Mapping[str, object]


def load_sequence_distributions(
    datastore_root: Path,
    *,
    routes: pd.DataFrame,
    consumer: str,
    as_of: object,
) -> SequenceConsumerResult:
    """Load causal shadow distributions for Loop B, Strategy, or Loop C.

    This function intentionally returns no trading authority.  A malformed or
    future-available sequence artifact fails closed instead of silently falling
    back to an unverified prediction.
    """

    clean_consumer = str(consumer).strip().upper()
    if clean_consumer not in {"LOOP_B", "OPTIONS_STRATEGY", "LOOP_C_OBSERVE"}:
        raise ValueError(f"Unsupported sequence consumer: {consumer}")
    base_keys = ("symbol", "horizon", "decision_timestamp")
    one_hour_keys = (*base_keys, "target_window_start")
    required = set(one_hour_keys)
    missing = sorted(required.difference(routes.columns))
    if missing:
        raise ValueError("Sequence routes are missing: " + ", ".join(missing))
    requested = routes.loc[:, list(one_hour_keys)].copy()
    requested["symbol"] = requested["symbol"].astype("string").str.upper()
    requested["horizon"] = requested["horizon"].astype("string")
    requested["decision_timestamp"] = pd.to_datetime(
        requested["decision_timestamp"], utc=True, errors="coerce"
    )
    requested["target_window_start"] = pd.to_datetime(
        requested["target_window_start"], utc=True, errors="coerce"
    )
    requested_one_hour_mask = requested["horizon"].eq("1h").fillna(False)
    requested_one_hour = requested.loc[requested_one_hour_mask].dropna(
        subset=list(one_hour_keys)
    ).drop_duplicates(list(one_hour_keys))
    requested_legacy = requested.loc[~requested_one_hour_mask].dropna(
        subset=list(base_keys)
    ).drop_duplicates(list(base_keys))
    requested = pd.concat(
        [requested_one_hour, requested_legacy], ignore_index=True, sort=False
    )
    cutoff = _utc(as_of, "as_of")
    if requested.empty:
        return SequenceConsumerResult(
            status="NO_ROUTES",
            consumer=clean_consumer,
            distributions=pd.DataFrame(),
            matched_routes=0,
            requested_routes=0,
            details={
                "reason": "The consumer supplied no valid routes.",
                "authority": "NONE",
                "automated_action_allowed": False,
            },
        )
    try:
        publication = read_current_sequence_publication(datastore_root)
        path = resolve_current_sequence_output(datastore_root, "distributions.parquet")
    except SequencePublicationError as exc:
        return SequenceConsumerResult(
            status="UNAVAILABLE",
            consumer=clean_consumer,
            distributions=pd.DataFrame(),
            matched_routes=0,
            requested_routes=len(requested),
            details={"reason": str(exc), "authority": "NONE"},
        )
    published_at = _utc(publication.receipt.get("published_at"), "published_at")
    if published_at > cutoff:
        return SequenceConsumerResult(
            status="UNAVAILABLE",
            consumer=clean_consumer,
            distributions=pd.DataFrame(),
            matched_routes=0,
            requested_routes=len(requested),
            details={
                "reason": "The current sequence publication was not available as of the consumer cutoff.",
                "authority": "NONE",
                "publication_available_at": published_at.isoformat(),
                "consumer_cutoff": cutoff.isoformat(),
                "automated_action_allowed": False,
            },
        )
    distributions = pd.read_parquet(path)
    required_distribution = (
        "symbol",
        "horizon",
        "decision_timestamp",
        "target_window_start",
        "information_available_at",
        "prediction_created_at",
        "prediction_status",
        "automated_action_allowed",
    )
    missing_distribution = sorted(
        set(required_distribution).difference(distributions.columns)
    )
    if missing_distribution:
        raise SequencePublicationError(
            "Sequence distributions are missing: "
            + ", ".join(missing_distribution)
        )
    for column in (
        "decision_timestamp",
        "target_window_start",
        "information_available_at",
        "prediction_created_at",
    ):
        distributions[column] = pd.to_datetime(
            distributions[column], utc=True, errors="coerce"
        )
    distributions["symbol"] = distributions["symbol"].astype("string").str.upper()
    distributions["horizon"] = distributions["horizon"].astype("string")
    if distributions.loc[:, list(required_distribution)].isna().any(axis=None):
        raise SequencePublicationError("Sequence distributions contain null contract fields")
    if distributions["automated_action_allowed"].astype("boolean").fillna(True).any():
        raise SequencePublicationError(
            "Sequence shadow distribution unexpectedly authorizes automated action"
        )
    if distributions["information_available_at"].gt(
        distributions["decision_timestamp"]
    ).any():
        raise SequencePublicationError(
            "Sequence distribution consumes evidence after its decision"
        )
    if distributions["prediction_created_at"].gt(cutoff).any():
        distributions = distributions.loc[
            distributions["prediction_created_at"].le(cutoff)
        ].copy()
    distribution_one_hour = distributions["horizon"].eq("1h")
    if distributions.loc[distribution_one_hour].duplicated(
        list(one_hour_keys), keep=False
    ).any() or distributions.loc[~distribution_one_hour].duplicated(
        list(base_keys), keep=False
    ).any():
        raise SequencePublicationError("Sequence distributions have duplicate routes")
    matched_frames: list[pd.DataFrame] = []
    if not requested_one_hour.empty:
        matched_frames.append(
            requested_one_hour.loc[:, list(one_hour_keys)].merge(
                distributions.loc[distribution_one_hour],
                on=list(one_hour_keys),
                how="inner",
                validate="one_to_one",
            )
        )
    if not requested_legacy.empty:
        matched_frames.append(
            requested_legacy.loc[:, list(base_keys)].merge(
                distributions.loc[~distribution_one_hour],
                on=list(base_keys),
                how="inner",
                validate="one_to_one",
            )
        )
    matched = (
        pd.concat(matched_frames, ignore_index=True, sort=False)
        if matched_frames
        else distributions.iloc[0:0].copy()
    )
    status = "READY_SHADOW" if len(matched) == len(requested) else "PARTIAL_SHADOW"
    return SequenceConsumerResult(
        status=status,
        consumer=clean_consumer,
        distributions=matched,
        matched_routes=len(matched),
        requested_routes=len(requested),
        details={
            "authority": "SHADOW_ONLY",
            "run_directory": str(publication.run_directory),
            "published_at": published_at.isoformat(),
            "source_files": [
                str(publication.run_directory / "manifest.json"),
                str(publication.run_directory / "publication.json"),
                str(path),
            ],
            "coverage": len(matched) / max(len(requested), 1),
            "automated_action_allowed": False,
        },
    )


def shadow_consumer_summary(result: SequenceConsumerResult) -> dict[str, object]:
    return {
        "status": result.status,
        "consumer": result.consumer,
        "matched_routes": result.matched_routes,
        "requested_routes": result.requested_routes,
        "coverage": result.matched_routes / max(result.requested_routes, 1),
        "authority": result.details.get("authority", "NONE"),
        "automated_action_allowed": False,
        "details": dict(result.details),
    }


def safe_load_sequence_distributions(
    datastore_root: Path,
    *,
    routes: pd.DataFrame,
    consumer: str,
    as_of: object,
) -> SequenceConsumerResult:
    """Return a reportable fail-closed status for a non-authoritative consumer.

    Loop B and Options Strategies record this shadow lane without allowing a
    broken optional publication to interrupt their established production
    authority.  Loop C calls the strict loader directly because its own output
    must fail closed when its model evidence is invalid.
    """

    try:
        return load_sequence_distributions(
            datastore_root,
            routes=routes,
            consumer=consumer,
            as_of=as_of,
        )
    except Exception as exc:
        return SequenceConsumerResult(
            status="INVALID_SHADOW",
            consumer=str(consumer).strip().upper(),
            distributions=pd.DataFrame(),
            matched_routes=0,
            requested_routes=len(routes),
            details={
                "reason": str(exc),
                "authority": "NONE",
                "automated_action_allowed": False,
            },
        )


def sequence_source_files(result: SequenceConsumerResult) -> tuple[Path, ...]:
    raw = result.details.get("source_files")
    if not isinstance(raw, (list, tuple)):
        return ()
    return tuple(Path(str(value)) for value in raw if str(value).strip())


def _utc(value: object, label: str) -> pd.Timestamp:
    timestamp = pd.to_datetime(value, utc=True, errors="coerce")
    if pd.isna(timestamp):
        raise ValueError(f"{label} must be a valid timestamp")
    return pd.Timestamp(timestamp)


__all__ = [
    "SequenceConsumerResult",
    "load_sequence_distributions",
    "safe_load_sequence_distributions",
    "sequence_source_files",
    "shadow_consumer_summary",
]
