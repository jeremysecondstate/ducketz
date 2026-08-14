from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd

from ml.artifacts import utc_timestamp
from ml.option_pricing.policies import OPTION_PRICING_POLICY_VERSION
from ml.option_pricing.publication import (
    LEGACY_OPTION_PRICING_PUBLICATION_VERSION,
    OPTION_PRICING_PUBLICATION_VERSION,
    V2_OPTION_PRICING_PUBLICATION_VERSION,
    pricing_pointer_path,
    read_option_pricing_publication_at,
    verified_option_pricing_history,
)
from ml.option_pricing.reporting import SURFACE_VERSION


LEGACY_SURFACE_VERSION = "option-pricing-compact-surface-v1"
V2_SURFACE_VERSION = "option-pricing-compact-surface-v2"
LEGACY_PRICING_POLICY_VERSION = "black-scholes-rbf-residual-v1"
LEGACY_NORMALIZATION_POLICY = (
    "legacy-v1-max-row-available-and-verified-publication-v1"
)
NATIVE_NORMALIZATION_POLICY = "receipt-bounded-first-availability-v2"
V2_PRICING_POLICY_VERSION = "black-scholes-rbf-residual-v2"

OPX_VALUE_COLUMNS = (
    "causal_coverage",
    "median_normalized_residual",
    "median_predictive_standard_deviation",
    "median_model_edge_in_half_spreads",
    "positive_edge_fraction",
    "negative_edge_fraction",
    "raw_arbitrage_violation_rate",
    "constrained_arbitrage_violation_rate",
    "interval_80_coverage",
    "interval_95_coverage",
    "median_relative_bid_ask_spread",
)


class PricingEvidenceUnavailable(FileNotFoundError):
    """No verified Pricing rows can be joined at the requested causal cutoff."""


class PricingEvidenceContractError(RuntimeError):
    """Verified Pricing authority contains incompatible or malformed evidence."""


def _one_or_mixed(frame: pd.DataFrame, column: str) -> object:
    if column not in frame:
        return pd.NA
    values = frame[column].dropna().astype("string").str.strip()
    values = values.loc[values.ne("")].drop_duplicates()
    if len(values) == 1:
        return values.iloc[0]
    return "MIXED" if len(values) else pd.NA


def _read_latest_verified_compact_pricing_features(
    datastore_root: Path,
    *,
    available_not_after: object,
    _publication: object | None = None,
) -> tuple[pd.DataFrame, tuple[Path, ...]]:
    """Return verified compact Pricing rows with a canonical availability clock.

    A publication is selected only after the current pointer, receipt chain,
    manifest, checksums, and selected physical Parquet contract have verified.
    Native v2 rows remain strict.  Exact legacy v1 rows are normalized in memory
    and are never written back to the immutable publication.
    """

    root = Path(datastore_root)
    cutoff = utc_timestamp(available_not_after)
    if not pricing_pointer_path(root).is_file():
        raise PricingEvidenceUnavailable(
            "No Pricing publication authority exists at the causal cutoff"
        )
    if _publication is None:
        try:
            publication = read_option_pricing_publication_at(
                root,
                available_not_after=cutoff,
            )
        except FileNotFoundError as exc:
            raise PricingEvidenceUnavailable(str(exc)) from exc
    else:
        publication = _publication

    run = publication.run_directory
    published = utc_timestamp(publication.receipt.get("published_at"))
    publication_version = str(publication.receipt.get("schema_version", ""))
    path = run / "pricing-surfaces.parquet"
    try:
        source = pd.read_parquet(path)
    except Exception as exc:
        raise PricingEvidenceContractError(
            "Verified Pricing surface could not be decoded"
        ) from exc
    if source.empty:
        raise PricingEvidenceUnavailable(
            "Verified Pricing publication has no compact surface rows"
        )

    common_required = {
        "symbol",
        "target_snapshot_for",
        "available_at",
        "contract_count",
        "surface_quality_pass",
        "pricing_policy_version",
        "schema_version",
        "automated_action_allowed",
        *OPX_VALUE_COLUMNS,
    }
    required = set(common_required)
    if publication_version in {
        OPTION_PRICING_PUBLICATION_VERSION,
        V2_OPTION_PRICING_PUBLICATION_VERSION,
    }:
        required.add("first_available_at")
        expected_surface_version = (
            SURFACE_VERSION
            if publication_version == OPTION_PRICING_PUBLICATION_VERSION
            else V2_SURFACE_VERSION
        )
        expected_policy_version = (
            OPTION_PRICING_POLICY_VERSION
            if publication_version == OPTION_PRICING_PUBLICATION_VERSION
            else V2_PRICING_POLICY_VERSION
        )
        normalization_policy = NATIVE_NORMALIZATION_POLICY
        legacy_normalized = False
    elif publication_version == LEGACY_OPTION_PRICING_PUBLICATION_VERSION:
        expected_surface_version = LEGACY_SURFACE_VERSION
        expected_policy_version = LEGACY_PRICING_POLICY_VERSION
        normalization_policy = LEGACY_NORMALIZATION_POLICY
        legacy_normalized = True
        if "first_available_at" in source.columns:
            raise PricingEvidenceContractError(
                "Legacy Pricing publication does not have the exact v1 surface shape"
            )
    else:  # The publication reader should already have rejected this.
        raise PricingEvidenceContractError(
            f"Unsupported verified Pricing publication version: {publication_version}"
        )
    if missing := sorted(required.difference(source.columns)):
        raise PricingEvidenceContractError(
            "Compact Pricing surface is missing: " + ", ".join(missing)
        )
    if not source["schema_version"].eq(expected_surface_version).all():
        observed = sorted(
            str(value) for value in source["schema_version"].dropna().unique()
        )
        raise PricingEvidenceContractError(
            "Compact Pricing surface schema version is mixed or incompatible: "
            + ", ".join(observed or ["<missing>"])
        )
    if not source["pricing_policy_version"].eq(expected_policy_version).all():
        raise PricingEvidenceContractError(
            "Compact Pricing surface policy version is incompatible"
        )
    if not source["automated_action_allowed"].eq(False).all():
        raise PricingEvidenceContractError(
            "Pricing surface unexpectedly authorizes automation"
        )
    symbols = source["symbol"].astype("string")
    if symbols.isna().any() or symbols.str.strip().eq("").any():
        raise PricingEvidenceContractError(
            "Compact Pricing surface contains a malformed symbol"
        )
    if source["surface_quality_pass"].isna().any():
        raise PricingEvidenceContractError(
            "Compact Pricing surface contains a missing quality decision"
        )
    source = source.copy()
    source["symbol"] = symbols.str.strip().str.upper()

    target = pd.to_datetime(
        source["target_snapshot_for"], utc=True, errors="coerce"
    )
    original_available = pd.to_datetime(
        source["available_at"], utc=True, errors="coerce"
    )
    if target.isna().any() or original_available.isna().any():
        raise PricingEvidenceContractError(
            "Compact Pricing surface contains malformed target or availability clocks"
        )
    if legacy_normalized:
        canonical_available = original_available.where(
            original_available.ge(published),
            published,
        )
    else:
        if original_available.gt(published).any():
            raise PricingEvidenceContractError(
                "Pricing surface availability follows its verified publication receipt"
            )
        first_available = pd.to_datetime(
            source["first_available_at"], utc=True, errors="coerce"
        )
        if (
            first_available.isna().any()
            or not original_available.eq(first_available).all()
        ):
            raise PricingEvidenceContractError(
                "Compact Pricing surface first availability is invalid"
            )
        canonical_available = first_available.where(
            first_available.ge(published), published
        )
    if not target.lt(canonical_available).all():
        raise PricingEvidenceContractError(
            "Compact Pricing surface target must precede canonical first availability"
        )

    source["target_snapshot_for"] = target
    source["_pricing_original_available_at"] = original_available
    source["available_at"] = canonical_available
    source["first_available_at"] = canonical_available
    source = source.loc[canonical_available.le(cutoff)].copy()
    if source.empty:
        raise PricingEvidenceUnavailable(
            "No verified Pricing surface was causal by the cutoff"
        )

    contract_count = pd.to_numeric(source["contract_count"], errors="coerce")
    if contract_count.isna().any() or contract_count.lt(0).any():
        raise PricingEvidenceContractError(
            "Compact Pricing surface contract counts are malformed"
        )
    source["contract_count"] = contract_count

    for column in OPX_VALUE_COLUMNS:
        numeric = pd.to_numeric(source[column], errors="coerce")
        if (~np.isfinite(numeric.loc[numeric.notna()])).any():
            raise PricingEvidenceContractError(
                f"Compact Pricing surface contains a non-finite {column} value"
            )

    provenance = {
        "source_publication_version": publication_version,
        "source_surface_version": expected_surface_version,
        "source_policy_version": expected_policy_version,
        "normalization_policy": normalization_policy,
        "legacy_normalized": legacy_normalized,
        "authority_published_at": published,
        "authority_run_path": run.relative_to(root.resolve()).as_posix(),
    }
    rows: list[dict[str, object]] = []
    for key, group in source.groupby(
        ["symbol", "target_snapshot_for", "available_at"], sort=True
    ):
        weights = group["contract_count"].astype(float)
        if float(weights.sum()) <= 0:
            continue
        row: dict[str, object] = {
            "symbol": str(key[0]).strip().upper(),
            "target_snapshot_for": key[1],
            "available_at": key[2],
            "first_available_at": key[2],
            "_pricing_original_available_at": group[
                "_pricing_original_available_at"
            ].max(),
            "_pricing_source_publication_version": publication_version,
            "_pricing_source_surface_version": expected_surface_version,
            "_pricing_source_policy_version": expected_policy_version,
            "_pricing_normalization_policy": normalization_policy,
            "_pricing_legacy_normalized": legacy_normalized,
            "_pricing_authority_published_at": published,
            "_pricing_authority_run_path": provenance["authority_run_path"],
            "surface_quality_pass": bool(
                group["surface_quality_pass"].eq(True).all()
            ),
            "source_provider": _one_or_mixed(group, "source_provider"),
        }
        for column in OPX_VALUE_COLUMNS:
            values = pd.to_numeric(group[column], errors="coerce")
            valid = values.notna() & weights.gt(0)
            row[column] = (
                float(np.average(values.loc[valid], weights=weights.loc[valid]))
                if valid.any()
                else np.nan
            )
        rows.append(row)
    result = pd.DataFrame(rows)
    if result.empty:
        raise PricingEvidenceUnavailable(
            "Verified Pricing surfaces had no weighted feature rows"
        )
    result.attrs["pricing_evidence"] = {
        key: (value.isoformat() if isinstance(value, pd.Timestamp) else value)
        for key, value in provenance.items()
    }
    return result, (
        path,
        run / "manifest.json",
        run / "publication.json",
    )


def read_verified_compact_pricing_features(
    datastore_root: Path,
    *,
    available_not_after: object,
) -> tuple[pd.DataFrame, tuple[Path, ...]]:
    """Read append-only verified surface history and select causal generations.

    Every reachable generation is checksum-verified. Repeated natural surfaces
    are resolved to the newest generation whose receipt-bounded first
    availability is no later than the requested cutoff. Immutable prior
    generations remain readable and are never rewritten.
    """

    root = Path(datastore_root).resolve()
    cutoff = utc_timestamp(available_not_after)
    if not pricing_pointer_path(root).is_file():
        raise PricingEvidenceUnavailable(
            "No Pricing publication authority exists at the causal cutoff"
        )
    history = verified_option_pricing_history(
        root, available_not_after=cutoff
    )
    if not history:
        raise PricingEvidenceUnavailable(
            "No verified Pricing generation was available by the causal cutoff"
        )
    frames: list[pd.DataFrame] = []
    sources: list[Path] = []
    for generation_order, publication in enumerate(history):
        published = utc_timestamp(publication.receipt.get("published_at"))
        try:
            frame, frame_sources = _read_latest_verified_compact_pricing_features(
                root,
                # The generation receipt is already bounded by ``history``.
                # Legacy v1 rows may carry a later row-level availability clock;
                # retain them once that clock, too, is inside the caller's cutoff.
                available_not_after=cutoff,
                _publication=publication,
            )
        except PricingEvidenceUnavailable:
            continue
        frame = frame.copy()
        frame["model_generation"] = publication.run_directory.name
        frame["_pricing_generation_order"] = generation_order
        frame["_pricing_generation_published_at"] = published
        if "source_provider" not in frame:
            frame["source_provider"] = pd.NA
        frame["evidence_lane"] = frame["source_provider"].map(
            lambda value: (
                "PROSPECTIVE_OPRA"
                if str(value).strip().lower() == "databento-opra"
                else "PROSPECTIVE_SCHWAB"
                if str(value).strip().lower() == "schwab"
                else "MIXED_OR_LEGACY"
            )
        )
        frame["fallback_used"] = frame["source_provider"].map(
            lambda value: str(value).strip().lower() == "schwab"
        )
        frames.append(frame)
        sources.extend(frame_sources)
    if not frames:
        raise PricingEvidenceUnavailable(
            "Verified Pricing history has no compact surface rows"
        )
    combined = pd.concat(frames, ignore_index=True, sort=False)
    available = pd.to_datetime(
        combined["first_available_at"], utc=True, errors="coerce"
    )
    combined = combined.loc[available.le(cutoff)].copy()
    if combined.empty:
        raise PricingEvidenceUnavailable(
            "No verified Pricing surface was causal by the cutoff"
        )
    # Loop B consumes a symbol/target compact surface. A later immutable model
    # generation supersedes the same natural target without deleting history.
    combined = (
        combined.sort_values(
            [
                "symbol",
                "target_snapshot_for",
                "_pricing_generation_order",
                "_pricing_generation_published_at",
            ],
            kind="stable",
        )
        .drop_duplicates(["symbol", "target_snapshot_for"], keep="last")
        .sort_values(["target_snapshot_for", "symbol"], kind="stable")
        .reset_index(drop=True)
    )
    provenance_columns = {
        "source_publication_version": "_pricing_source_publication_version",
        "source_surface_version": "_pricing_source_surface_version",
        "source_policy_version": "_pricing_source_policy_version",
        "normalization_policy": "_pricing_normalization_policy",
        "legacy_normalized": "_pricing_legacy_normalized",
        "authority_published_at": "_pricing_authority_published_at",
        "authority_run_path": "_pricing_authority_run_path",
    }
    provenance: dict[str, object] = {}
    for public_name, column in provenance_columns.items():
        values = combined[column].drop_duplicates() if column in combined else pd.Series(dtype="object")
        if len(values) == 1:
            value = values.iloc[0]
            provenance[public_name] = (
                value.isoformat() if isinstance(value, pd.Timestamp) else value
            )
        elif len(values) > 1:
            provenance[public_name] = "MIXED"
    combined.attrs["pricing_evidence"] = {
        **provenance,
        "history_policy": "append-only-newest-causal-generation-v1",
        "verified_generation_count": len(history),
        "selected_natural_surface_count": len(combined),
        "available_not_after": cutoff.isoformat(),
    }
    return combined, tuple(dict.fromkeys(sources))


def describe_verified_compact_pricing_features(
    datastore_root: Path,
    *,
    available_not_after: object,
) -> dict[str, object]:
    """Return read-only closed-cycle diagnostics for the latest causal authority."""

    root = Path(datastore_root).resolve()
    cutoff = utc_timestamp(available_not_after)
    empty: dict[str, object] = {
        "authority_path": None,
        "publication_version": None,
        "surface_version": None,
        "published_at": None,
        "publication_age_seconds": None,
        "legacy_normalization_used": False,
        "fresh_horizons": (),
        "status": "UNAVAILABLE",
    }
    if not pricing_pointer_path(root).is_file():
        return empty
    try:
        publication = read_option_pricing_publication_at(
            root,
            available_not_after=cutoff,
        )
    except FileNotFoundError:
        return empty
    published = utc_timestamp(publication.receipt.get("published_at"))
    publication_version = str(publication.receipt.get("schema_version", ""))
    diagnostics = {
        **empty,
        "authority_path": publication.run_directory.relative_to(root).as_posix(),
        "publication_version": publication_version,
        "surface_version": (
            LEGACY_SURFACE_VERSION
            if publication_version == LEGACY_OPTION_PRICING_PUBLICATION_VERSION
            else SURFACE_VERSION
        ),
        "published_at": published.isoformat(),
        "publication_age_seconds": max(0.0, (cutoff - published).total_seconds()),
        "legacy_normalization_used": (
            publication_version == LEGACY_OPTION_PRICING_PUBLICATION_VERSION
        ),
        "status": "VERIFIED_NO_USABLE_ROWS",
    }
    try:
        frame, _sources = read_verified_compact_pricing_features(
            root,
            available_not_after=cutoff,
        )
    except PricingEvidenceUnavailable:
        return diagnostics
    freshness_by_horizon = {
        "1h": pd.Timedelta(hours=2),
        "4h": pd.Timedelta(hours=4),
        "1d": pd.Timedelta(days=2),
        "1w": pd.Timedelta(days=8),
    }
    first = pd.to_datetime(frame["first_available_at"], utc=True, errors="coerce")
    target = pd.to_datetime(frame["target_snapshot_for"], utc=True, errors="coerce")
    diagnostics["fresh_horizons"] = tuple(
        horizon
        for horizon, freshness in freshness_by_horizon.items()
        if pd.concat((first + freshness, target + freshness), axis=1)
        .min(axis=1)
        .ge(cutoff)
        .any()
    )
    diagnostics["status"] = "VERIFIED"
    return diagnostics


__all__ = [
    "LEGACY_NORMALIZATION_POLICY",
    "LEGACY_PRICING_POLICY_VERSION",
    "LEGACY_SURFACE_VERSION",
    "NATIVE_NORMALIZATION_POLICY",
    "OPX_VALUE_COLUMNS",
    "PricingEvidenceContractError",
    "PricingEvidenceUnavailable",
    "describe_verified_compact_pricing_features",
    "read_verified_compact_pricing_features",
]
