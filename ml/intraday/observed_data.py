"""Research-only inputs using observed candles before causal feature creation.

The live dataset builder is unchanged. This adapter preserves exact wall-clock
labels and acquisition receipts while testing indicators computed over genuinely
observed candles. Removed synthetic intervals remain visible as elapsed gaps.
"""
from __future__ import annotations

from copy import deepcopy
from dataclasses import replace
import hashlib
import json

import pandas as pd

from .data import Dataset, build_dataset, exact_target, utc, validate_candles
from .prediction import ContractError, STEP


FEATURE_VERSION = "intraday-observed-only-causal-features-v1"
FEATURE_POLICY = (
    "exclude_synthetic_candles_before_causal_features_observed_steps_explicit_elapsed_gaps_v1"
)
LABEL_PROVENANCE = "observed_reference_and_exact_observed_target_only"


def build_observed_dataset(
    candles: pd.DataFrame,
    *,
    cutoff,
    reference_at,
    lookback_days: float | None = None,
) -> Dataset:
    """Build a distinct observed-only research dataset without changing originals.

    A synthetic exact reference is rejected, never replaced by a nearby candle.
    All synthetic candles, including mixed observed/synthetic aggregates, are
    removed before indicators, warmup, and exact timestamp label lookup. The
    existing builder retains its cutoff, joint receipt, and chronological purge
    contracts; no missing interval is filled and the 45-minute target is fixed.

    Manifest exclusion counts describe all validated supplied candles. Snapshot
    exclusion counts describe only candles available by this cutoff and no later
    than this reference, so future synthetic rows cannot change its fingerprint.
    """
    cutoff, reference = utc(cutoff), utc(reference_at)
    exact_target(reference)
    frame = validate_candles(candles)
    reference_rows = frame.loc[frame.opened_at.eq(reference)]
    if reference_rows.empty:
        raise ContractError("Exact observed reference candle is missing; no shifting is permitted")
    if bool(reference_rows.synthetic.iloc[0]):
        raise ContractError("Exact reference candle is synthetic; observed-only research cannot shift or fill it")

    synthetic = frame.synthetic
    excluded_total = int(synthetic.sum())
    causal_scope = (
        frame.opened_at.le(reference)
        & frame.available_at.le(cutoff)
        & (frame.opened_at + STEP).le(cutoff)
    )
    excluded_causal = int((synthetic & causal_scope).sum())
    observed = frame.loc[~synthetic].copy().reset_index(drop=True)
    observed.attrs = deepcopy(candles.attrs)
    dataset = build_dataset(
        observed,
        cutoff=cutoff,
        reference_at=reference,
        lookback_days=lookback_days,
        synthetic_policy="exclude",
    )

    provenance = {
        "feature_version": FEATURE_VERSION,
        "feature_policy": FEATURE_POLICY,
        "label_provenance": LABEL_PROVENANCE,
        "feature_provenance": "observed_candles_only_including_warmup",
    }
    snapshot = deepcopy(dataset.inference["snapshot"])
    snapshot.update(provenance)
    snapshot["synthetic_candles_excluded"] = excluded_causal
    snapshot["synthetic_candle_exclusion_scope"] = "available_at_cutoff_and_no_later_than_reference"
    canonical = json.dumps(snapshot, sort_keys=True, separators=(",", ":"), allow_nan=False)
    inference = {
        **dataset.inference,
        **provenance,
        "snapshot": snapshot,
        "snapshot_sha256": hashlib.sha256(canonical.encode()).hexdigest(),
    }
    manifest = {
        **dataset.manifest,
        **provenance,
        "synthetic_candles_excluded": excluded_total,
        "synthetic_candles_excluded_at_cutoff": excluded_causal,
        "synthetic_candle_exclusion_scope": "all_validated_supplied_candles",
    }
    return replace(dataset, inference=inference, manifest=manifest)
