"""Bounded, private, offline Loop B feature comparisons.

Nothing in this module imports a production trainer, resolves a latest pointer,
loads a fitted model, or publishes predictions. The caller must first construct
an eligible NONSEALED cohort and freeze the historic protected IDs/boundaries.
The resulting receipts identify this research fit, not an original production fit.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
import hashlib
import json
import math
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence
import warnings

import numpy as np
import pandas as pd
from sklearn.exceptions import ConvergenceWarning
from sklearn.linear_model import LogisticRegression

from ml.preprocessing import SemanticFeatureTransformer


VERSION = "loop-b-private-research-v2"
IDENTITY_COLUMNS = ("sample_id", "symbol", "decision_at", "target_start", "target_end", "y")
TIME_COLUMNS = ("decision_at", "target_start", "target_end")


def canonical_json(value: Any) -> bytes:
    return (json.dumps(value, sort_keys=True, indent=2, allow_nan=False) + "\n").encode("utf-8")


def sha256_file(path: str | Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def hash_files(paths: Iterable[str | Path]) -> dict[str, str]:
    """Explicit paths only: never enumerate a datastore or discover inputs."""
    return {str(Path(path).resolve(strict=True)): sha256_file(path) for path in paths}


def verify_hashes(expected: Mapping[str, str]) -> None:
    for path, digest in expected.items():
        if sha256_file(path) != digest:
            raise ValueError(f"frozen input/source changed: {path}")


def ids_receipt(ids: Iterable[str]) -> dict[str, Any]:
    values = sorted(str(value) for value in ids)
    if len(values) != len(set(values)):
        raise ValueError("duplicate sample IDs in receipt")
    return {"count": len(values), "sha256": hashlib.sha256(canonical_json(values)).hexdigest(), "ids": values}


def _utc(value: Any) -> pd.Timestamp:
    result = pd.Timestamp(value)
    if pd.isna(result) or result.tzinfo is None:
        raise ValueError("timestamps must be nonmissing and explicitly timezone aware")
    return result.tz_convert("UTC")


def validate_cohort(frame: pd.DataFrame, *, excluded_ids: Iterable[str], development_end: Any) -> pd.DataFrame:
    """Fail closed; exclusions must be selected without accessing sealed outcomes."""
    missing = set(IDENTITY_COLUMNS) - set(frame.columns)
    if missing:
        raise ValueError(f"missing cohort columns: {sorted(missing)}")
    if frame.empty or frame.sample_id.isna().any() or frame.symbol.isna().any():
        raise ValueError("cohort must have rows, unique IDs and symbols")
    result = frame.copy()
    result["sample_id"] = result.sample_id.astype(str)
    result["symbol"] = result.symbol.astype(str)
    if result.sample_id.duplicated().any() or result.sample_id.eq("").any() or result.symbol.eq("").any():
        raise ValueError("cohort sample IDs must be unique and nonempty")
    if set(result.sample_id) & set(map(str, excluded_ids)):
        raise ValueError("cohort intersects frozen protected/excluded sample IDs")
    if not result.y.isin([0, 1]).all():
        raise ValueError("y must contain only finite binary outcomes")
    for column in TIME_COLUMNS:
        result[column] = pd.to_datetime(result[column].map(_utc), utc=True)
    if "label_available_at" in result:
        result["label_available_at"] = pd.to_datetime(result.label_available_at.map(_utc), utc=True)
        if (result.label_available_at < result.target_end).any():
            raise ValueError("label availability cannot precede target end")
    if (result.decision_at > result.target_start).any() or (result.target_start > result.target_end).any():
        raise ValueError("expected decision_at <= target_start <= target_end")
    cutoff = _utc(development_end)
    if (result.target_end >= cutoff).any():
        raise ValueError("cohort target outcomes extend beyond frozen development boundary")
    if "label_available_at" in result and (result.label_available_at >= cutoff).any():
        raise ValueError("cohort label availability reaches frozen protected boundary")
    return result.sort_values(["target_start", "decision_at", "symbol", "sample_id"]).reset_index(drop=True)


@dataclass(frozen=True)
class ResearchConfig:
    n_folds: int = 3
    initial_training_fraction: float = 0.5
    min_train_dates: int = 20
    min_validation_dates: int = 7
    min_finite_fraction: float = 0.2
    clip_lower: float = 0.0025
    clip_upper: float = 0.9975
    logistic_c: float = 1.0
    max_iter: int = 1000
    random_seed: int = 20261011
    bootstrap_repetitions: int = 4000
    block_days: int = 5
    min_uncertainty_days: int = 40
    familywise_alpha: float = 0.05
    total_comparisons: int = 36
    min_absolute_logloss_improvement: float = 0.005
    min_improving_fold_fraction: float = 2 / 3

    def __post_init__(self) -> None:
        if not 1 <= self.n_folds <= 10 or min(self.min_train_dates, self.min_validation_dates) < 2:
            raise ValueError("invalid bounded fold configuration")
        if not 0 < self.initial_training_fraction < 1:
            raise ValueError("invalid initial training fraction")
        if not 0 < self.min_finite_fraction <= 1 or not 0 <= self.clip_lower < self.clip_upper <= 1:
            raise ValueError("invalid train-only preprocessing configuration")
        if self.logistic_c != 1.0 or not 1 <= self.max_iter <= 2000:
            raise ValueError("research model is fixed C=1 with bounded iterations")
        if not 100 <= self.bootstrap_repetitions <= 10000 or self.block_days < 1:
            raise ValueError("invalid bounded bootstrap configuration")
        if not 0 < self.familywise_alpha < 1 or self.min_uncertainty_days < 2 or self.total_comparisons < 1:
            raise ValueError("invalid uncertainty configuration")


@dataclass(frozen=True)
class PurgedFold:
    name: str
    train_ids: tuple[str, ...]
    validation_ids: tuple[str, ...]
    purged_ids: tuple[str, ...]
    validation_purged_ids: tuple[str, ...]
    validation_decision_start: str
    train_target_end: str
    train_label_available_at: str


def make_purged_folds(frame: pd.DataFrame, config: ResearchConfig) -> list[PurgedFold]:
    """Expanding training, grouped target starts and strictly observable labels.

    Every symbol/decision sharing a target start stays together. A whole training
    group is purged unless its latest target end precedes the earliest validation
    decision. Thus training labels cannot overlap the validation target windows
    or require observations unavailable at the validation decision. Adjacent
    validation blocks also discard groups whose outcomes reach the next block's
    first decision; their correlated outcomes cannot straddle OOS fold boundaries.
    """
    working = frame.copy()
    if "label_available_at" not in working:
        working["label_available_at"] = working.target_end
    grouped = working.groupby("target_start", sort=True).agg(
        target_end=("target_end", "max"), decision_at=("decision_at", "min"),
        label_available_at=("label_available_at", "max"),
    )
    dates = grouped.index.floor("D").unique().sort_values()
    initial_count = max(config.min_train_dates, int(len(dates) * config.initial_training_fraction))
    remaining = len(dates) - initial_count
    count = min(config.n_folds, remaining // config.min_validation_dates)
    if count < 1:
        return []
    chunks = np.array_split(np.arange(initial_count, len(dates)), count)
    folds: list[PurgedFold] = []
    for i, indices in enumerate(chunks):
        validation_groups = grouped.loc[grouped.index.floor("D").isin(dates[indices])]
        nominal_validation_groups = validation_groups.index
        if i + 1 < len(chunks):
            next_decision = grouped.loc[grouped.index.floor("D").isin(dates[chunks[i + 1]])].decision_at.min()
            validation_groups = validation_groups.loc[validation_groups.target_end < next_decision]
        if validation_groups.index.floor("D").nunique() < config.min_validation_dates:
            continue
        boundary = validation_groups.decision_at.min()
        possible_train = grouped.loc[grouped.index.floor("D") < dates[int(indices[0])]]
        train_groups = possible_train.loc[(possible_train.target_end < boundary) & (possible_train.label_available_at < boundary)]
        if train_groups.index.floor("D").nunique() < config.min_train_dates:
            continue
        train = frame.loc[frame.target_start.isin(train_groups.index)]
        validation = frame.loc[frame.target_start.isin(validation_groups.index)]
        purged = frame.loc[frame.target_start.isin(possible_train.index.difference(train_groups.index))]
        validation_purged = frame.loc[frame.target_start.isin(nominal_validation_groups.difference(validation_groups.index))]
        folds.append(PurgedFold(
            name=f"fold-{i + 1}", train_ids=tuple(train.sample_id),
            validation_ids=tuple(validation.sample_id), purged_ids=tuple(purged.sample_id),
            validation_purged_ids=tuple(validation_purged.sample_id),
            validation_decision_start=boundary.isoformat(), train_target_end=train.target_end.max().isoformat(),
            train_label_available_at=train_groups.label_available_at.max().isoformat(),
        ))
    return folds


def _numeric(frame: pd.DataFrame, columns: Sequence[str]) -> pd.DataFrame:
    return frame.loc[:, list(columns)].apply(pd.to_numeric, errors="coerce").replace([np.inf, -np.inf], np.nan)


def feature_coverage(frame: pd.DataFrame, columns: Sequence[str]) -> dict[str, Any]:
    """Descriptive only; admission is recomputed on each training fold."""
    values = _numeric(frame, columns)

    def describe(subset: pd.DataFrame) -> dict[str, Any]:
        finite = subset.notna()
        return {"rows": len(subset), "finite_cells": int(finite.sum().sum()),
                "rows_any_finite": int(finite.any(axis=1).sum()),
                "rows_all_finite": int(finite.all(axis=1).sum()),
                "finite_by_column": {column: int(finite[column].sum()) for column in columns}}

    redundant: dict[str, str] = {}
    for i, column in enumerate(columns):
        for prior in columns[:i]:
            if values[column].equals(values[prior]):
                redundant[column] = prior
                break
    return {**describe(values), "columns": list(columns),
            "empty": [column for column in columns if values[column].notna().sum() == 0],
            "constant_nonmissing": [column for column in columns if values[column].nunique() == 1],
            "exact_redundant": redundant,
            "by_symbol": {str(symbol): describe(values.loc[indices]) for symbol, indices in frame.groupby("symbol").groups.items()},
            "by_month": {str(month): describe(values.loc[indices]) for month, indices in frame.groupby(frame.decision_at.dt.strftime("%Y-%m")).groups.items()}}


@dataclass
class TrainPreprocessor:
    input_columns: list[str]
    semantic_versions: dict[str, str]
    semantic_caps: dict[str, float | None]
    columns: list[str]
    median: list[float]
    lower: list[float]
    upper: list[float]
    mean: list[float]
    scale: list[float]
    rejected: dict[str, str]

    def transform(self, frame: pd.DataFrame) -> np.ndarray:
        semantic = SemanticFeatureTransformer(self.semantic_versions)
        semantic.feature_names_in_ = np.asarray(self.input_columns, dtype=object)
        semantic.training_caps_ = self.semantic_caps
        transformed = semantic.transform(_numeric(frame, self.input_columns))
        values = transformed[self.columns].to_numpy(dtype=float)
        values = np.where(np.isfinite(values), values, np.asarray(self.median))
        values = np.clip(values, self.lower, self.upper)
        return (values - np.asarray(self.mean)) / np.asarray(self.scale)


def fit_train_preprocessor(frame: pd.DataFrame, columns: Sequence[str], config: ResearchConfig,
                           transform_versions: Mapping[str, str] | None = None) -> TrainPreprocessor:
    versions = {column: (transform_versions or {}).get(column, "identity-v1") for column in columns}
    semantic = SemanticFeatureTransformer(versions)
    values = semantic.fit_transform(_numeric(frame, columns))
    admitted: list[str] = []
    rejected: dict[str, str] = {}
    for column in columns:
        if values[column].notna().mean() < config.min_finite_fraction:
            rejected[column] = "insufficient_training_availability"
        elif values[column].nunique() <= 1:
            rejected[column] = "constant_in_training"
        elif any(values[column].equals(values[prior]) for prior in admitted):
            rejected[column] = "exact_duplicate_in_training"
        else:
            admitted.append(column)
    measured = values[admitted]
    median = measured.median().to_numpy(dtype=float)
    imputed = measured.fillna(dict(zip(admitted, median)))
    lower = imputed.quantile(config.clip_lower).to_numpy(dtype=float)
    upper = imputed.quantile(config.clip_upper).to_numpy(dtype=float)
    transformed = np.clip(imputed.to_numpy(dtype=float), lower, upper)
    mean = transformed.mean(axis=0)
    scale = transformed.std(axis=0)
    # Rare nonconstant measurements can collapse under clipping. Such a column
    # contributes zero; the model remains well-defined without validation data.
    scale[scale == 0] = 1.0
    return TrainPreprocessor(list(columns), versions, dict(semantic.training_caps_), admitted,
                             median.tolist(), lower.tolist(), upper.tolist(), mean.tolist(), scale.tolist(), rejected)


def target_weights(frame: pd.DataFrame) -> np.ndarray:
    """Equal target starts, then symbols, then decisions within symbol/target."""
    decisions = frame.groupby(["target_start", "symbol"])["sample_id"].transform("size")
    symbols = frame.groupby("target_start")["symbol"].transform("nunique")
    return 1.0 / (decisions.to_numpy(dtype=float) * symbols.to_numpy(dtype=float))


def _cluster_means(frame: pd.DataFrame, columns: Sequence[str]) -> pd.DataFrame:
    return frame.groupby(["target_start", "symbol"])[list(columns)].mean().groupby("target_start").mean()


def _row_losses(y: np.ndarray, probability: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    p = np.clip(probability, 1e-12, 1 - 1e-12)
    return -(y * np.log(p) + (1 - y) * np.log(1 - p)), (y - p) ** 2


def probability_metrics(predictions: pd.DataFrame) -> dict[str, Any]:
    """Average symbols/decisions within target starts before averaging targets."""
    scored = predictions.copy()
    scored["logloss"], scored["brier"] = _row_losses(scored.y.to_numpy(), scored.probability.to_numpy())
    clusters = _cluster_means(scored, ["logloss", "brier"])
    weights = target_weights(scored)
    bins = np.minimum((scored.probability.to_numpy() * 10).astype(int), 9)
    reliability = []
    ece = 0.0
    for bucket in range(10):
        selected = bins == bucket
        if not selected.any():
            continue
        w = weights[selected]
        mean_probability = float(np.average(scored.probability.to_numpy()[selected], weights=w))
        observed = float(np.average(scored.y.to_numpy()[selected], weights=w))
        ece += float(w.sum() / weights.sum()) * abs(mean_probability - observed)
        reliability.append({"bin": bucket, "rows": int(selected.sum()), "target_weight": float(w.sum()),
                            "probability": mean_probability, "observed": observed})
    return {"rows": len(scored), "target_clusters": len(clusters), "logloss": float(clusters.logloss.mean()),
            "brier": float(clusters.brier.mean()), "ece_10_bins": ece, "reliability": reliability}


def paired_uncertainty(baseline: pd.DataFrame, candidate: pd.DataFrame, config: ResearchConfig, comparisons: int) -> dict[str, Any]:
    """Paired moving-block bootstrap, after collapsing shared targets then days.

    Days are UTC target-start dates. A block contains consecutive OBSERVED days
    and preserves nearby temporal dependence. Confidence concerns this fixed
    cohort/model design; it is not an independent estimate of future regimes.
    """
    keys = ["sample_id", "symbol", "target_start", "y", "fold"]
    paired = baseline[keys + ["probability"]].merge(candidate[keys + ["probability"]], on=keys, validate="one_to_one", suffixes=("_baseline", "_candidate"))
    if len(paired) != len(baseline) or len(paired) != len(candidate):
        raise ValueError("paired predictions do not have identical sample membership")
    base_loss, base_brier = _row_losses(paired.y.to_numpy(), paired.probability_baseline.to_numpy())
    new_loss, new_brier = _row_losses(paired.y.to_numpy(), paired.probability_candidate.to_numpy())
    paired["logloss_delta"] = new_loss - base_loss
    paired["brier_delta"] = new_brier - base_brier
    clusters = _cluster_means(paired, ["logloss_delta", "brier_delta"])
    daily = clusters.groupby(clusters.index.floor("D")).mean()
    result: dict[str, Any] = {
        "method": "paired-moving-observed-day-block-bootstrap-after-target-cluster-means",
        "sign": "candidate_minus_baseline_negative_is_better", "target_clusters": len(clusters),
        "observed_days": len(daily), "block_days": config.block_days,
        "approximate_nonoverlapping_blocks": len(daily) // config.block_days,
        "cluster_mean_logloss_delta": float(clusters.logloss_delta.mean()),
        "day_mean_logloss_delta": float(daily.logloss_delta.mean()),
        "cluster_mean_brier_delta": float(clusters.brier_delta.mean()),
        "familywise_alpha": config.familywise_alpha, "comparisons": comparisons,
        "uncertainty_status": "insufficient_temporal_blocks", "logloss_upper_bound": None,
    }
    if len(daily) < max(config.min_uncertainty_days, 6 * config.block_days):
        return result
    rng = np.random.default_rng(config.random_seed)
    day_sums = clusters.logloss_delta.groupby(clusters.index.floor("D")).sum().to_numpy()
    day_counts = clusters.logloss_delta.groupby(clusters.index.floor("D")).size().to_numpy()
    values = daily.logloss_delta.to_numpy()
    blocks_needed = math.ceil(len(values) / config.block_days)
    draws = np.empty(config.bootstrap_repetitions)
    for i in range(len(draws)):
        starts = rng.integers(0, len(values) - config.block_days + 1, size=blocks_needed)
        indices = np.concatenate([np.arange(start, start + config.block_days) for start in starts])[: len(values)]
        draws[i] = day_sums[indices].sum() / day_counts[indices].sum()
    adjusted_alpha = config.familywise_alpha / max(1, comparisons)
    result.update(uncertainty_status="estimated", bootstrap_repetitions=len(draws),
                  adjusted_one_sided_alpha=adjusted_alpha,
                  logloss_upper_bound=float(np.quantile(draws, 1 - adjusted_alpha)),
                  logloss_lower_bound=float(np.quantile(draws, adjusted_alpha)))
    return result


class PrivateExperimentStore:
    """Create-only artifact writer confined to an explicit private research root.

    The existing allowed parent must be named scratch, research, or experiments.
    Canonical datastore roots should additionally be supplied as forbidden_roots.
    No pointer/manifest discovery, symlink traversal, replacement, or promotion.
    """
    def __init__(self, root: str | Path, *, private_parent: str | Path, forbidden_roots: Sequence[str | Path] = ()) -> None:
        parent = Path(private_parent).absolute()
        if parent.name.lower() not in {"scratch", "research", "experiments"}:
            raise ValueError("private parent must explicitly be scratch, research, or experiments")
        if parent.resolve(strict=True) != parent or not parent.is_dir():
            raise ValueError("private parent cannot traverse symlinks")
        root_path = Path(root).absolute()
        resolved = root_path.resolve()
        if root_path != resolved or resolved == parent or not resolved.is_relative_to(parent):
            raise ValueError("experiment root must stay below private parent without symlinks")
        for forbidden in forbidden_roots:
            if resolved.is_relative_to(Path(forbidden).resolve()):
                raise ValueError("experiment output intersects a forbidden production root")
        if any(part.lower() in {"latest", "production", "canonical"} for part in resolved.relative_to(parent).parts):
            raise ValueError("experiment root cannot be a production/pointer path")
        resolved.mkdir(parents=True, exist_ok=False)
        self.root = resolved
        self.private_parent = parent

    def write_bytes(self, relative: str, content: bytes) -> str:
        destination = self.root / relative
        if Path(relative).is_absolute() or ".." in Path(relative).parts:
            raise ValueError("artifact path must be relative and contained")
        if self.root.resolve() != self.root or self.private_parent.resolve() != self.private_parent:
            raise ValueError("private root changed to a symlink")
        if destination.resolve() != destination or not destination.resolve().is_relative_to(self.root):
            raise ValueError("artifact cannot traverse symlinks or escape the experiment")
        destination.parent.mkdir(parents=True, exist_ok=True)
        with destination.open("xb") as stream:
            stream.write(content)
        return hashlib.sha256(content).hexdigest()

    def write_json(self, relative: str, content: Any) -> str:
        return self.write_bytes(relative, canonical_json(content))


def _frame_digest(frame: pd.DataFrame, columns: Sequence[str]) -> str:
    # CSV is persisted byte-for-byte as the cohort receipt; float64 precision and
    # deterministic row/column order are retained. No Python object hashing.
    return hashlib.sha256(frame.loc[:, list(columns)].to_csv(index=False, float_format="%.17g").encode("utf-8")).hexdigest()


def run_experiment(
    frame: pd.DataFrame, *, baseline_columns: Sequence[str], families: Mapping[str, Sequence[str]],
    excluded_ids: Iterable[str], development_end: Any, source_hashes: Mapping[str, str],
    input_hashes: Mapping[str, str], reference: Mapping[str, Any], store: PrivateExperimentStore,
    config: ResearchConfig = ResearchConfig(), transform_versions: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """Freeze design and exact IDs before matched fitting; every attempt is saved.

    `reference` must explain cohort construction and original-run provenance.
    Protected outcomes must already have been filtered upstream WITHOUT loading
    sealed label files. This function never expands eligibility or moves cutoffs.
    """
    if not families or set(families) - {"activity", "path", "interaction"}:
        raise ValueError("only the three predeclared candidate families are supported")
    if not source_hashes or not input_hashes or not reference:
        raise ValueError("explicit frozen source/input hashes and provenance are required")
    exclusions = tuple(sorted(map(str, excluded_ids)))
    cohort = validate_cohort(frame, excluded_ids=exclusions, development_end=development_end)
    baseline = list(baseline_columns)
    if len(baseline) != len(set(baseline)):
        raise ValueError("duplicate baseline feature columns")
    candidates = [column for columns in families.values() for column in columns]
    columns = baseline + candidates
    if len(columns) != len(set(columns)) or set(columns) & set(IDENTITY_COLUMNS):
        raise ValueError("candidate/baseline feature columns must be distinct measurements")
    if not set(columns).issubset(cohort.columns):
        raise ValueError("declared feature columns missing from cohort")
    if transform_versions is not None and set(transform_versions) != set(columns):
        raise ValueError("semantic transform mapping must cover exactly all declared features")
    # The caller's reviewed semantic contract is the authority for candidate
    # availability. Explicit forbidden future/target names are never admitted.
    if any(any(token in column.lower() for token in ("target", "future", "outcome", "label")) for column in columns):
        raise ValueError("target/future/outcome/label feature names are prohibited")
    variants = {"baseline": baseline}
    variants.update({name: baseline + list(family) for name, family in families.items()})
    if len(families) > 1:
        variants["all"] = columns
    folds = make_purged_folds(cohort, config)
    verify_hashes(source_hashes)
    verify_hashes(input_hashes)
    frozen_columns = list(IDENTITY_COLUMNS) + (["label_available_at"] if "label_available_at" in cohort else []) + columns
    cohort_digest = _frame_digest(cohort, frozen_columns)
    specification = {
        "version": VERSION, "config": asdict(config), "reference": dict(reference),
        "cohort_sha256": cohort_digest, "development_end": _utc(development_end).isoformat(),
        "source_hashes": dict(source_hashes), "input_hashes": dict(input_hashes),
        "variants": variants, "protected_exclusions": ids_receipt(exclusions),
        "transform_versions": dict(transform_versions or {column: "identity-v1" for column in columns}),
        "eligible_membership": ids_receipt(cohort.sample_id),
        "folds": [{**asdict(fold), "train_receipt": ids_receipt(fold.train_ids),
                   "validation_receipt": ids_receipt(fold.validation_ids),
                   "train_purge_receipt": ids_receipt(fold.purged_ids),
                   "validation_purge_receipt": ids_receipt(fold.validation_purged_ids)} for fold in folds],
        "primary_metric": "equal-target-cluster mean logloss; smaller is better",
        "acceptance_rule": (
            f"At least 3 valid folds; improving fold fraction >= {config.min_improving_fold_fraction:.8g}; "
            f"absolute logloss improvement >= {config.min_absolute_logloss_improvement:.8g}; "
            f"strict majority of symbols improve; at least {config.min_uncertainty_days} OOS target dates; "
            "no aggregate Brier deterioration; paired observed-day block bootstrap one-sided upper bound <0 "
            f"after Bonferroni correction over all {config.total_comparisons} predeclared comparisons. "
            "Retain means further research only."
        ),
        "probability_calibration": "none fitted; raw logistic probabilities evaluated with weighted reliability bins",
        "training": "C=1 unweighted-class logistic; each target cluster total weight 1; training-fold-only admission, median, quantile clipping and scaling",
        "missing_indicators": "none; deliberately fixed for all matched research variants",
        "label_availability": "explicit label_available_at" if "label_available_at" in cohort else "caller-supplied target_end; no additional delay inferred",
        "production_state_changes": False,
    }
    spec_hash = store.write_json("specification.json", specification)
    store.write_bytes("cohort.csv", cohort[frozen_columns].to_csv(index=False, float_format="%.17g").encode("utf-8"))
    coverage = {name: feature_coverage(cohort, feature_columns) for name, feature_columns in variants.items()}
    store.write_json("coverage.json", coverage)
    predictions: list[pd.DataFrame] = []
    attempts: list[dict[str, Any]] = []
    by_id = cohort.set_index("sample_id", drop=False)
    for fold in folds:
        train = by_id.loc[list(fold.train_ids)].reset_index(drop=True)
        validation = by_id.loc[list(fold.validation_ids)].reset_index(drop=True)
        for variant, feature_columns in variants.items():
            attempt: dict[str, Any] = {"fold": fold.name, "variant": variant, "status": "started", "training_rows": len(train), "validation_rows": len(validation)}
            store.write_json(f"attempts/{fold.name}-{variant}-started.json", attempt)
            try:
                preprocessor = fit_train_preprocessor(train, feature_columns, config, transform_versions)
                weights = target_weights(train)
                model_payload: dict[str, Any] = {"preprocessor": asdict(preprocessor)}
                if train.y.nunique() < 2 or not preprocessor.columns:
                    # Jeffreys-smoothed training-only prior handles degenerate
                    # early folds identically for every matched variant.
                    prior = float((np.dot(weights, train.y) + 0.5) / (weights.sum() + 1))
                    probability = np.full(len(validation), prior)
                    model_payload.update(kind="training_prior", probability=prior)
                else:
                    model = LogisticRegression(C=config.logistic_c, max_iter=config.max_iter, random_state=config.random_seed, class_weight=None, solver="lbfgs")
                    with warnings.catch_warnings():
                        warnings.simplefilter("error", ConvergenceWarning)
                        model.fit(preprocessor.transform(train), train.y.to_numpy(), sample_weight=weights)
                    probability = model.predict_proba(preprocessor.transform(validation))[:, 1]
                    model_payload.update(kind="logistic_regression", coefficients=model.coef_.tolist(), intercept=model.intercept_.tolist(), classes=model.classes_.tolist(), iterations=model.n_iter_.tolist())
                prediction = validation[list(IDENTITY_COLUMNS)].copy()
                prediction["probability"] = probability
                prediction["fold"] = fold.name
                prediction["variant"] = variant
                predictions.append(prediction)
                attempt.update(status="succeeded", admitted=preprocessor.columns, rejected=preprocessor.rejected, metrics=probability_metrics(prediction))
                store.write_json(f"models/{fold.name}-{variant}.json", model_payload)
            except (ValueError, FloatingPointError, ConvergenceWarning) as error:
                attempt.update(status="failed", error_type=type(error).__name__, error=str(error))
            attempts.append(attempt)
            store.write_json(f"attempts/{fold.name}-{variant}-finished.json", attempt)
    output = pd.concat(predictions, ignore_index=True) if predictions else pd.DataFrame()
    summary: dict[str, Any] = {"version": VERSION, "specification_sha256": spec_hash, "cohort_sha256": cohort_digest,
                               "folds": len(folds), "attempts": attempts, "results": {}, "comparisons": {},
                               "status": "completed" if folds else "insufficient_history"}
    if not folds or output.empty:
        reason = "insufficient temporal support for a purged fold" if not folds else "no matched attempts succeeded"
        summary["comparisons"] = {variant: {"disposition": "inconclusive", "reason": reason}
                                  for variant in variants if variant != "baseline"}
        if folds:
            summary["status"] = "failed_attempts"
    if not output.empty:
        store.write_bytes("predictions.csv", output.to_csv(index=False, float_format="%.17g").encode("utf-8"))
        for variant, rows in output.groupby("variant", sort=False):
            summary["results"][variant] = {"overall": probability_metrics(rows),
                "by_fold": {name: probability_metrics(part) for name, part in rows.groupby("fold")},
                "by_symbol": {name: probability_metrics(part) for name, part in rows.groupby("symbol")},
                "by_month": {name: probability_metrics(part) for name, part in rows.groupby(rows.target_start.dt.strftime("%Y-%m"))}}
        base_predictions = output.loc[output.variant == "baseline"]
        for variant in variants:
            if variant == "baseline":
                continue
            candidate_predictions = output.loc[output.variant == variant]
            if len(base_predictions) != len(candidate_predictions) or set(candidate_predictions.fold) != {fold.name for fold in folds}:
                summary["comparisons"][variant] = {"disposition": "inconclusive", "reason": "not all matched attempts succeeded"}
                continue
            uncertainty = paired_uncertainty(base_predictions, candidate_predictions, config, config.total_comparisons)
            base_result = summary["results"]["baseline"]
            candidate_result = summary["results"][variant]
            improving = sum(candidate_result["by_fold"][fold.name]["logloss"] < base_result["by_fold"][fold.name]["logloss"] for fold in folds)
            relative = (base_result["overall"]["logloss"] - candidate_result["overall"]["logloss"]) / base_result["overall"]["logloss"]
            absolute = base_result["overall"]["logloss"] - candidate_result["overall"]["logloss"]
            symbols = sorted(base_result["by_symbol"])
            improving_symbols = sum(candidate_result["by_symbol"][symbol]["logloss"] < base_result["by_symbol"][symbol]["logloss"] for symbol in symbols)
            brier_ok = candidate_result["overall"]["brier"] <= base_result["overall"]["brier"]
            accepted = (len(folds) >= 3 and improving / len(folds) >= config.min_improving_fold_fraction
                        and absolute >= config.min_absolute_logloss_improvement and brier_ok
                        and improving_symbols > len(symbols) / 2 and uncertainty["observed_days"] >= config.min_uncertainty_days
                        and uncertainty["logloss_upper_bound"] is not None and uncertainty["logloss_upper_bound"] < 0)
            disposition = "retain_for_further_research" if accepted else "inconclusive"
            if uncertainty["uncertainty_status"] == "estimated" and len(folds) >= 3 and relative <= 0:
                disposition = "reject"
            summary["comparisons"][variant] = {"disposition": disposition, "acceptance_rule_met": accepted,
                "relative_logloss_improvement": relative, "improving_folds": improving, "brier_no_worse": brier_ok,
                "absolute_logloss_improvement": absolute, "improving_symbols": improving_symbols, "symbols": len(symbols),
                "uncertainty": uncertainty}
    # Any changed bytes invalidate the experiment, including otherwise attractive
    # results. The frozen record and attempts remain available for diagnosis.
    try:
        verify_hashes(source_hashes)
        verify_hashes(input_hashes)
        if _frame_digest(cohort, frozen_columns) != cohort_digest:
            raise ValueError("in-memory cohort changed during experiment")
    except (ValueError, OSError) as error:
        store.write_json("invalidated.json", {"status": "invalidated", "reason": str(error)})
        raise
    summary["input_source_stability"] = "verified_before_and_after"
    store.write_json("results.json", summary)
    return summary
