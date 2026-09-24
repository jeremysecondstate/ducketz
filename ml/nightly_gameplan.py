from __future__ import annotations

import argparse
import json
import math
import os
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Callable, Mapping, Sequence
from zoneinfo import ZoneInfo

import joblib
import numpy as np
import pandas as pd
from sklearn.compose import ColumnTransformer
from sklearn.ensemble import HistGradientBoostingClassifier
from sklearn.impute import SimpleImputer
from sklearn.metrics import brier_score_loss, log_loss
from sklearn.neural_network import MLPClassifier
from sklearn.linear_model import LogisticRegression
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import OneHotEncoder, StandardScaler

from datafetching.databento_opra_history import OPRA_STRATEGY_HISTORY_SCHEMAS
from datafetching.parquet_store import DATASTORE_TARGETS, resolve_datastore_dir
from datafetching.runtime_lock import exclusive_runtime_lock
from ml.artifacts import (
    create_timestamp_directory,
    file_checksum,
    utc_timestamp,
    verify_manifest,
    write_manifest,
)
from ml.training_progress import fit_with_progress
from ml.calibration import IdentityCalibrator, fit_probability_calibrator
from ml.current_publication import read_current_publication
from ml.gameplan_estimators import ProbabilityBlend as _ProbabilityBlend, PriorProbabilityShrinkage
from ml.gameplan_source_selection import (
    GAMEPLAN_SOURCE_SELECTION_VERSION, ARCHIVE_SOURCE_SELECTION_VERSION, SOURCE_SELECTION_VERSIONS, SOURCE_SELECTION_COLUMNS,
    select_prior_session_sources, source_selection_contract,
)
from ml.gameplan_promotion import (
    DIRECTIONAL_PROMOTION_POLICY, STRICT_PROMOTION_POLICY, build_promotion_gate,
)
from ml.gameplan_development_selection import (
    DEVELOPMENT_SELECTION_POLICY,
    development_selection_policy,
    WEEKLY_PROBABILITY_SHRINKAGE_POLICY, WEEKLY_PROBABILITY_SHRINKAGE_WEIGHTS,
    logistic_regularization_candidates,
    logistic_regularization_policy,
    select_development_calibrator,
)
from ml.gameplan_probability_target import (
    LEGACY_COST_TARGET, RAW_DIRECTION_TARGET, probability_target_contract,
    probability_target_metadata, resolve_probability_target,
)
from ml.independent_stock_targets import (
    STOCK_TARGET_CONTRACT_VERSION,
    STOCK_CALENDAR_FEATURE_CONTRACT, STOCK_CALENDAR_FEATURE_NAMES, with_stock_calendar_features,
    build_stock_current_groups,
    build_stock_training_groups,
)
from ml.stock_target_prices import (
    CANONICAL_STOCK_PRICE_SOURCE, STOCK_PRICE_SOURCES,
    load_stock_target_prices, stock_price_dataset,
)
from ml.stock_trader.market_features import (
    INDEPENDENT_MARKET_FEATURE_CONTRACT, frozen_market_feature_values,
)
from ml.strategy_publication import read_current_strategy_publication
from ml.strategy_selection.contracts import (
    BLACK_SCHOLES_CALIBRATED_MODEL_SCORE_BASIS,
    BSGP_CALIBRATED_MODEL_SCORE_BASIS,
    OPRA_EXECUTION_CALIBRATED_MODEL_SCORE_BASIS,
)


GAMEPLAN_VERSION = "immutable-overnight-gameplan-v2"
GAMEPLAN_POINTER_VERSION = "immutable-overnight-gameplan-pointer-v1"
GAMEPLAN_RECEIPT_VERSION = "immutable-overnight-gameplan-receipt-v1"
FORECAST_CONTRACT_VERSION = "overnight-path-forecast-grid-v1"
TARGET_CONTRACT_VERSION = "overnight-path-targets-v2"
EXECUTION_AUTHORITY = "ADVISORY_PAPER_ONLY"
SCHEDULE_TIMEZONE = ZoneInfo("America/Los_Angeles")
ACTION_START_HOUR = 4
ACTION_END_HOUR = 17
HOURLY_ANCHORS = tuple(range(ACTION_START_HOUR, ACTION_END_HOUR + 1))
FOUR_HOUR_ANCHORS = (4, 8, 12, 16)
DAILY_HORIZONS = tuple(f"1w-d{offset}" for offset in range(1, 6))
MODEL_GROUPS = ("1h", "4h", "1d", "1w")
EXPECTED_FORECASTS_PER_SYMBOL = 24
EXPECTED_OPTION_INTENTS_PER_SYMBOL = 24
ASSUMED_ROUND_TRIP_COST = 0.001
TARGET_BOUNDARY_TOLERANCE = pd.Timedelta(minutes=5)
MINIMUM_OPTION_PROFIT_PROBABILITY = 0.55
MINIMUM_OPTION_DIRECTION_EDGE = 0.05

_FEATURE_PREFIXES = (
    "mr__",
    "bp__",
    "bar__",
    "life__",
    "quote__",
    "opt__",
    "opx__",
    "energy__",
    "cme__",
    "weekly__",
    "fdir__",
    "fund__",
    "ftlife__",
    "macro__",
    "sec__",
)
_FITTED_OPTION_SCORE_BASES = frozenset(
    {
        BSGP_CALIBRATED_MODEL_SCORE_BASIS,
        BLACK_SCHOLES_CALIBRATED_MODEL_SCORE_BASIS,
        OPRA_EXECUTION_CALIBRATED_MODEL_SCORE_BASIS,
    }
)
@dataclass(frozen=True)
class NightlyGameplanResult:
    run_directory: Path
    action_date: date
    forecast_rows: int
    option_intent_rows: int
    evaluated_prior_rows: int
    published_at: pd.Timestamp


@dataclass(frozen=True)
class GameplanPublication:
    run_directory: Path
    manifest: Mapping[str, object]
    receipt: Mapping[str, object]
    pointer: Mapping[str, object]


def run_nightly_gameplan_once(
    datastore_root: Path,
    *,
    run_timestamp: object | None = None,
    reporter: Callable[[str], None] | None = print,
    stock_only: bool = False,
    independent_stock_horizons: bool = False,
    stock_price_source: str = CANONICAL_STOCK_PRICE_SOURCE,
    probability_target_contract: str | None = None,
    archive_history: bool = False,
) -> NightlyGameplanResult:
    """Train, freeze, and atomically publish one next-session gameplan.

    The output is intentionally advisory/paper-only.  A separate executor may
    consume it, but this module has no broker dependency and cannot submit an
    order.  The run directory is immutable after its receipt is published.
    """

    if independent_stock_horizons and not stock_only:
        raise ValueError("Independent stock horizons require explicit stock-only preparation")
    price_dataset = stock_price_dataset(stock_price_source)
    if stock_price_source != CANONICAL_STOCK_PRICE_SOURCE and not independent_stock_horizons:
        raise ValueError("An alternate stock price source requires explicit independent stock horizons")
    target_contract = STOCK_TARGET_CONTRACT_VERSION if independent_stock_horizons else TARGET_CONTRACT_VERSION
    probability_target = resolve_probability_target(
        probability_target_contract if probability_target_contract is not None else
        RAW_DIRECTION_TARGET if independent_stock_horizons else LEGACY_COST_TARGET)
    if not independent_stock_horizons and probability_target != LEGACY_COST_TARGET:
        raise ValueError("Raw-price direction requires explicit independent stock horizons")
    probability_metadata = probability_target_metadata(probability_target)
    root = Path(datastore_root).resolve()
    created = utc_timestamp(run_timestamp)
    loop_b = read_current_publication(root)
    strategy = None if stock_only else read_current_strategy_publication(root)
    samples_path = loop_b.run_directory / "samples.parquet"
    predictions_path = loop_b.run_directory / "predictions.parquet"
    strategy_inputs = (
        () if strategy is None else (
            strategy.run_directory / "strategy-candidates.parquet",
            strategy.run_directory / "manifest.json",
            strategy.run_directory / "publication.json",
        )
    )
    source_strategy = (
        None if strategy is None else strategy.run_directory.relative_to(root).as_posix()
    )
    if not all(path.is_file() for path in (samples_path, predictions_path, *strategy_inputs)):
        raise RuntimeError("Nightly gameplan inputs are incomplete")

    samples = pd.read_parquet(samples_path)
    candidates = (
        pd.DataFrame(columns=["symbol", "horizon"])
        if stock_only else pd.read_parquet(strategy_inputs[0])
    )
    symbols = _configured_symbols(loop_b.manifest, samples)
    feature_columns = _feature_columns(loop_b.manifest, samples)
    source_feature_columns = feature_columns
    if independent_stock_horizons:
        feature_columns = tuple(dict.fromkeys((*feature_columns, *STOCK_CALENDAR_FEATURE_NAMES)))
    sources = (
        select_prior_session_sources(samples, symbols=symbols, available_at=created,
                                     feature_columns=source_feature_columns)
        if independent_stock_horizons else
        _overnight_sources(samples, symbols=symbols, available_at=created)
    )
    operational_sources = sources
    archive = None
    archive_files = ()
    if archive_history:
        if not independent_stock_horizons or stock_price_source != "xnas-itch-archive-v1":
            raise ValueError("Archive history requires independent XNAS stock preparation")
        from ml.gameplan_archive_features import load_archive_feature_sources
        from ml.gameplan_archive_integration import combine_archive_sources
        from ml.gameplan_archive_seconds import verify_second_minute_overlap
        from dataclasses import replace
        archive = combine_archive_sources(
            load_archive_feature_sources(root, symbols=symbols, available_at=created),
            operational_sources, feature_columns=source_feature_columns)
        seconds_report, seconds_files = verify_second_minute_overlap(root, symbols=symbols, available_at=created)
        archive = replace(archive, report={**archive.report, "second_minute_consistency": seconds_report},
                          source_files=tuple(dict.fromkeys((*archive.source_files, *seconds_files))))
        archive.sources.attrs["source_selection"] = archive.report
        sources = archive.sources
        archive_files = archive.source_files
        feature_columns = tuple(dict.fromkeys((*archive.feature_columns, *STOCK_CALENDAR_FEATURE_NAMES)))
    selection_version = source_selection_contract(sources) if independent_stock_horizons else None
    source_selection_report = sources.attrs.get("source_selection", {})
    current_sources, action_date = _current_overnight_sources(
        sources,
        symbols=symbols,
        as_of=created,
    )
    action_start = _local_timestamp(action_date, ACTION_START_HOUR)
    action_end = _local_timestamp(action_date, ACTION_END_HOUR)
    if created >= action_start:
        raise RuntimeError(
            "A new immutable gameplan cannot be published after the 04:00 PT "
            f"action window begins: action_date={action_date.isoformat()}"
        )

    cursor_files, opra_freshness = _verify_opra_history(
        root,
        symbols=symbols,
        action_date=action_date,
        required_completed_through=pd.to_datetime(
            current_sources["source_feature_cutoff" if independent_stock_horizons else "bar_end_timestamp"],
            utc=True, errors="coerce"
        ).max().date(),
    )
    if stock_price_source == CANONICAL_STOCK_PRICE_SOURCE:
        minute_bars, minute_bar_files = _load_equity_minute_bars(root, symbols=symbols)
        price_source_report = {"source_contract": stock_price_source, "dataset": price_dataset,
                               "schema": "ohlcv-1m", "source_policy": "existing_canonical_equity_minutes"}
    else:
        minute_bars, minute_bar_files, price_source_report = load_stock_target_prices(
            root, symbols=symbols, source_contract=stock_price_source,
        )
    if independent_stock_horizons:
        groups = build_stock_training_groups(
            sources, feature_columns=feature_columns, minute_bars=minute_bars, available_at=created,
            price_source_contract=stock_price_source,
            probability_target=probability_target,
        )
        if archive is not None:
            from ml.gameplan_archive_integration import exclude_quality_intervals
            groups = exclude_quality_intervals(groups, archive.report.get("excluded_intervals", ()),
                                              split_boundaries=(*archive.report.get("split_boundaries", ()),
                                                  *archive.report.get("target_discontinuity_boundaries", ())))
        for group, frame in groups.items():
            if frame.empty or frame["target"].nunique() != 2:
                raise RuntimeError(
                    f"Independent stock {group} training labels unavailable: "
                    f"rows={len(frame)}; exact_boundary_quality={frame.attrs.get('target_boundary_quality')}"
                )
    else:
        groups = _build_training_groups(
            samples, sources=sources, feature_columns=feature_columns, minute_bars=minute_bars,
        )
    # Save evaluation independently before fitting, even if this new plan fails.
    from ml.gameplan_evaluation import evaluate_saved_gameplans, saved_independent_observation_sources

    evaluation_groups = dict(groups)
    evaluation_price_files = [*minute_bar_files, *archive_files]
    if independent_stock_horizons and any((root / "ml/nightly-gameplan-runs").glob("*/receipt.json")):
        legacy_sources = _overnight_sources(samples, symbols=symbols, available_at=created)
        legacy_bars = minute_bars
        if stock_price_source != CANONICAL_STOCK_PRICE_SOURCE:
            legacy_bars, legacy_files = _load_equity_minute_bars(root, symbols=symbols)
            evaluation_price_files.extend(legacy_files)
        legacy_hourly, legacy_four = _intraday_outcomes(sources=legacy_sources, feature_columns=(), minute_bars=legacy_bars)
        legacy_daily, legacy_weekly = _daily_weekly_outcomes(samples, feature_columns=())
        evaluation_groups.update({"legacy/1h": legacy_hourly, "legacy/4h": legacy_four,
                                  "legacy/1d": legacy_daily, "legacy/1w": legacy_weekly})
        for historical_source, selection_contract in saved_independent_observation_sources(root):
            if (historical_source, selection_contract) == (stock_price_source, selection_version):
                continue
            if historical_source == CANONICAL_STOCK_PRICE_SOURCE:
                historical_bars = legacy_bars
            else:
                historical_bars, historical_files, _ = load_stock_target_prices(
                    root, symbols=symbols, source_contract=historical_source,
                )
                evaluation_price_files.extend(historical_files)
            historical_sources = operational_sources if selection_contract == GAMEPLAN_SOURCE_SELECTION_VERSION else legacy_sources
            if selection_contract == ARCHIVE_SOURCE_SELECTION_VERSION:
                from ml.gameplan_archive_features import load_archive_feature_sources
                historical_archive = archive or load_archive_feature_sources(root, symbols=symbols, available_at=created)
                historical_sources = historical_archive.sources
                evaluation_price_files.extend(historical_archive.source_files)
            historical = build_stock_training_groups(historical_sources, feature_columns=(),
                minute_bars=historical_bars, available_at=created, price_source_contract=historical_source)
            if selection_contract == ARCHIVE_SOURCE_SELECTION_VERSION:
                from ml.gameplan_archive_integration import exclude_quality_intervals
                historical = exclude_quality_intervals(historical, historical_archive.report.get("excluded_intervals", ()),
                                                       split_boundaries=(*historical_archive.report.get("split_boundaries", ()),
                                                           *historical_archive.report.get("target_discontinuity_boundaries", ())))
            evaluation_groups.update({f"{historical_source}/{selection_contract}/{group}": frame for group, frame in historical.items()})
    evaluation = evaluate_saved_gameplans(
        root, observed_groups=evaluation_groups, evaluated_at=created,
        input_files=(samples_path, *dict.fromkeys(evaluation_price_files)),
    )
    prior_evaluations = evaluation.evaluations
    current = (
        build_stock_current_groups(current_sources, feature_columns=feature_columns,
                                   price_source_contract=stock_price_source,
                                   probability_target=probability_target)
        if independent_stock_horizons else _build_current_groups(
            samples, current_sources=current_sources, action_date=action_date,
            as_of=created, symbols=symbols, feature_columns=feature_columns,
        )
    )
    run = create_timestamp_directory(
        root / "ml" / "nightly-gameplan-runs",
        timestamp=created,
    )
    if reporter is not None:
        reporter(
            "Nightly gameplan: fitting four chronological HGB/MLP challenger "
            f"groups for {action_date.isoformat()}"
        )

    forecast_frames: list[pd.DataFrame] = []
    model_reports: dict[str, object] = {}
    model_output_names: list[str] = []
    training_cohort_names: list[str] = []
    champion_input_files: list[Path] = []
    for group in MODEL_GROUPS:
        if independent_stock_horizons:
            cohort_name = f"training-cohort-{group}.parquet"
            groups[group].to_parquet(run / cohort_name, index=False)
            training_cohort_names.append(cohort_name)
        trained = _fit_group_model(
            groups[group],
            current=current[group],
            feature_columns=feature_columns,
            group=group,
            model_directory=run / "models" / group,
            trained_at=created,
        )
        if independent_stock_horizons and trained["report"]["promotion_gate"]["status"] != "PROMOTED":
            from ml.gameplan_champions import latest_promoted_champion, retain_champion
            champion = latest_promoted_champion(root, group=group, action_date=action_date,
                symbols=symbols, price_source=stock_price_source, before=created,
                source_selection_contract=selection_version,
                probability_target=probability_target)
            if champion is not None:
                trained, retained_outputs = retain_champion(trained, champion=champion,
                    current=current[group], run=run, group=group, frozen_at=created)
                model_output_names.extend(retained_outputs)
                champion_input_files.extend(champion["files"])
        forecast_frames.append(trained["forecasts"])
        model_reports[group] = trained["report"]
        model_output_names.append(
            (Path("models") / group / "model.joblib").as_posix()
        )
        if reporter is not None:
            report = trained["report"]
            reporter(
                "Nightly gameplan model: "
                f"group={group}; selected={report['selected_family']}; "
                f"status={report['promotion_gate']['status']}; "
                f"assessment_rows={report['partitions']['assessment_rows']}"
            )

    forecasts = pd.concat(forecast_frames, ignore_index=True, sort=False)
    forecasts = _finalize_forecasts(
        forecasts,
        symbols=symbols,
        action_date=action_date,
        frozen_at=created,
        action_start=action_start,
        action_end=action_end,
        opra_freshness=opra_freshness,
        target_contract_version=target_contract,
    )
    option_intents = _option_intents(
        forecasts,
        candidates=candidates,
        strategy_run=None if strategy is None else strategy.run_directory,
        action_date=action_date,
        stock_only=stock_only,
    )

    forecasts_name = "forecasts.parquet"
    intents_name = "option-strategy-intents.parquet"
    evaluations_name = "prior-gameplan-evaluations.parquet"
    reports_name = "model-reports.json"
    gameplan_name = "gameplan.json"
    forecasts.to_parquet(run / forecasts_name, index=False)
    option_intents.to_parquet(run / intents_name, index=False)
    prior_evaluations.to_parquet(run / evaluations_name, index=False)
    _write_json_atomic(run / reports_name, model_reports)

    model_status_counts = {
        str(key): int(value)
        for key, value in forecasts["model_status"].value_counts().items()
    }
    option_status_counts = {
        str(key): int(value)
        for key, value in option_intents["plan_status"].value_counts().items()
    }
    plan_payload = {
        "schema_version": GAMEPLAN_VERSION,
        **probability_metadata,
        "preparation_scope": "STOCK_ONLY" if stock_only else "STOCK_AND_OPTIONS_RESEARCH",
        "forecast_contract_version": FORECAST_CONTRACT_VERSION,
        "target_contract_version": target_contract,
        **({"target_price_source_contract": stock_price_source, "target_price_dataset": price_dataset,
            "stock_price_source": price_source_report,
            "target_calendar_feature_contract": STOCK_CALENDAR_FEATURE_CONTRACT,
            "source_selection_contract": selection_version,
            "source_selection": source_selection_report} if independent_stock_horizons else {}),
        "action_date": action_date.isoformat(),
        "timezone": str(SCHEDULE_TIMEZONE),
        "action_window": {
            "start": action_start.isoformat(),
            "end": action_end.isoformat(),
            "definition": "inclusive forecast anchors from 04:00 through 17:00 PT",
        },
        "frozen_at": created.isoformat(),
        "immutable": True,
        "execution_authority": EXECUTION_AUTHORITY,
        "broker_orders_enabled": False,
        "orders_placed": 0,
        "symbols": list(symbols),
        "forecast_grid": {
            "1h": [f"{hour:02d}:00" for hour in (range(4, 17) if independent_stock_horizons else HOURLY_ANCHORS)],
            "4h": [f"{hour:02d}:00" for hour in FOUR_HOUR_ANCHORS],
            "1d": [f"D+{offset}" for offset in range(1, 6)],
            "1w": ["D+1 through D+5 direct weekly target"],
            "per_symbol": EXPECTED_FORECASTS_PER_SYMBOL,
            "total": len(forecasts),
            **({"hourly_research_route": "1h@gap", "independent_stock_horizons": True} if independent_stock_horizons else {}),
        },
        "model_status_counts": model_status_counts,
        "option_strategy_plan": {
            "preparation_status": "NOT_PREPARED_STOCK_ONLY" if stock_only else "PREPARED",
            "rows": len(option_intents),
            "status_counts": option_status_counts,
            "exact_candidate_is_frozen_when_available": True,
            "execution_requires_same_candidate_to_pass_fresh_quote_revalidation": True,
            "revalidation_may_only_execute_or_skip": True,
            "revalidation_may_not_substitute_or_rewrite": True,
        },
        "opra_history": opra_freshness,
        "prior_gameplan_evaluated_rows": int(
            prior_evaluations["evaluation_status"].eq("EVALUATED").sum()
        ),
        "prior_directional_forecasts_evaluated_rows": int(
            prior_evaluations["evaluation_status"].eq("EVALUATED").sum()
        ),
        "evaluation_scope": {
            "directional_forecasts": "AUTOMATIC_WHEN_MATURED",
            "saved_evaluation_run": evaluation.run_directory.relative_to(root).as_posix(),
            "first_gameplan_action_date": "2026-09-04",
            "option_intents": "REQUIRES_EXACT_LEG_EXECUTION_OR_COUNTERFACTUAL_EVIDENCE",
            "realized_option_profit_loss_available": False,
        },
        "source_authorities": {
            "loop_b_run": loop_b.run_directory.relative_to(root).as_posix(),
            "strategy_run": source_strategy,
        },
        "limitations": [
            "Offline assessment is not proof of future profit.",
            "No broker order can be submitted by this artifact.",
            "An option intent is skipped when its exact frozen legs lack fresh, "
            "valid execution evidence.",
            "The prior-gameplan evaluation artifact currently scores directional "
            "forecasts only; it does not infer realized option profit or loss "
            "without exact-leg execution or explicitly labeled counterfactual evidence.",
        ],
    }
    _write_json_atomic(run / gameplan_name, plan_payload)
    archive_outputs = ()
    if archive is not None:
        history_report = {**archive.report, "orders_placed": 0,
            "training_cohorts": {group: {"rows": len(frame),
                "first_action_date": str(frame.action_date.min()),
                "last_action_date": str(frame.action_date.max()),
                "by_symbol": {str(symbol): {"rows": len(rows), "first_action_date": str(rows.action_date.min()),
                    "last_action_date": str(rows.action_date.max())} for symbol, rows in frame.groupby("symbol")},
                "quality_excluded_rows": frame.attrs.get("archive_quality_excluded_rows", 0)}
                for group, frame in groups.items()}}
        _write_json_atomic(run / "archive-history.json", history_report)
        archive_outputs = ("archive-history.json",)

    output_names = (
        forecasts_name,
        intents_name,
        evaluations_name,
        reports_name,
        gameplan_name,
        *model_output_names,
        *training_cohort_names,
        *archive_outputs,
    )
    write_manifest(
        run,
        run_timestamp=created,
        input_files=(
            samples_path,
            predictions_path,
            loop_b.run_directory / "manifest.json",
            loop_b.run_directory / "publication.json",
            *strategy_inputs,
            *cursor_files,
            *minute_bar_files,
            *archive_files,
            *champion_input_files,
            evaluation.run_directory / "receipt.json",
            evaluation.run_directory / "evaluations.parquet",
        ),
        output_files=output_names,
        model_name="nightly-path-hgb-mlp-challenger",
        feature_columns=feature_columns,
        target_column=("target_raw_price_direction" if probability_target == RAW_DIRECTION_TARGET
                       else "target_cost_adjusted_positive"),
        configuration={
            "schema_version": GAMEPLAN_VERSION,
            **probability_metadata,
            "preparation_scope": "STOCK_ONLY" if stock_only else "STOCK_AND_OPTIONS_RESEARCH",
            "forecast_contract_version": FORECAST_CONTRACT_VERSION,
            "target_contract_version": target_contract,
            **({"target_price_source_contract": stock_price_source, "target_price_dataset": price_dataset,
                "stock_price_source": price_source_report,
                "target_calendar_feature_contract": STOCK_CALENDAR_FEATURE_CONTRACT,
                "source_selection_contract": selection_version,
                "archive_history": archive_history,
                "source_selection": source_selection_report} if independent_stock_horizons else {}),
            "action_date": action_date.isoformat(),
            "timezone": str(SCHEDULE_TIMEZONE),
            "execution_authority": EXECUTION_AUTHORITY,
            "broker_orders_enabled": False,
            "orders_placed": 0,
            "symbols": list(symbols),
            "opra_history": opra_freshness,
            "source_loop_b_run": loop_b.run_directory.relative_to(root).as_posix(),
            "source_strategy_run": source_strategy,
            "publication_contract": {
                "pointer": "ml/nightly-gameplan-latest/run.json",
                "receipt": "receipt.json",
                "immutable_run": True,
            },
        },
        datastore_root=root,
    )
    published_at = utc_timestamp()
    if published_at >= action_start:
        raise RuntimeError(
            "The completed gameplan missed the 04:00 PT publication boundary; "
            "its immutable pointer was not advanced. "
            f"action_date={action_date.isoformat()}; "
            f"finished_at={published_at.isoformat()}"
        )
    _publish_gameplan(
        root,
        run=run,
        action_date=action_date,
        published_at=published_at,
        source_loop_b=loop_b.run_directory.relative_to(root).as_posix(),
        source_strategy=source_strategy,
    )
    if reporter is not None:
        reporter(
            "Nightly gameplan published: "
            f"forecasts={len(forecasts)}; option_intents={len(option_intents)}; "
            f"run={run}"
        )
    return NightlyGameplanResult(
        run_directory=run,
        action_date=action_date,
        forecast_rows=len(forecasts),
        option_intent_rows=len(option_intents),
        evaluated_prior_rows=int(
            prior_evaluations["evaluation_status"].eq("EVALUATED").sum()
        ),
        published_at=published_at,
    )


def _configured_symbols(
    manifest: Mapping[str, object],
    samples: pd.DataFrame,
) -> tuple[str, ...]:
    configuration = manifest.get("configuration")
    raw = configuration.get("symbols") if isinstance(configuration, Mapping) else None
    values = raw if isinstance(raw, (list, tuple)) else samples["symbol"].unique()
    symbols = tuple(
        dict.fromkeys(str(value).strip().upper() for value in values if str(value).strip())
    )
    if not symbols or len(symbols) != len(values):
        raise RuntimeError(
            "Nightly gameplan requires a nonempty, unique configured universe; "
            f"observed={symbols}"
        )
    observed = set(samples["symbol"].dropna().astype(str).str.upper())
    if observed != set(symbols):
        raise RuntimeError(
            f"Gameplan samples and configured universe disagree: {symbols}; observed={sorted(observed)}"
        )
    return symbols


def _feature_columns(
    manifest: Mapping[str, object],
    samples: pd.DataFrame,
) -> tuple[str, ...]:
    raw = manifest.get("feature_columns")
    candidates = (
        tuple(str(value) for value in raw)
        if isinstance(raw, list)
        else tuple(
            column
            for column in samples.columns
            if str(column).startswith(_FEATURE_PREFIXES)
        )
    )
    features = tuple(
        column
        for column in candidates
        if column in samples.columns
        and str(column).startswith(_FEATURE_PREFIXES)
        and pd.api.types.is_numeric_dtype(samples[column])
    )
    if not features:
        raise RuntimeError("Nightly gameplan found no numeric model features")
    return features


def _overnight_sources(
    samples: pd.DataFrame,
    *,
    symbols: Sequence[str],
    available_at: pd.Timestamp,
) -> pd.DataFrame:
    hourly = samples.loc[
        samples["horizon"].astype("string").eq("1h")
        & samples["symbol"].astype("string").str.upper().isin(symbols)
    ].copy()
    starts = pd.to_datetime(hourly["target_window_start"], utc=True, errors="coerce")
    information = pd.to_datetime(
        hourly["information_available_at"], utc=True, errors="coerce"
    )
    start_local = starts.dt.tz_convert(SCHEDULE_TIMEZONE)
    information_local = information.dt.tz_convert(SCHEDULE_TIMEZONE)
    hourly["action_date"] = start_local.dt.date
    prior_close = information_local.dt.date < hourly["action_date"]
    exact_anchor = start_local.dt.hour.eq(4) & start_local.dt.minute.eq(0)
    eligible = hourly.loc[
        exact_anchor & prior_close & information.le(available_at)
    ].copy()
    eligible["decision_timestamp"] = pd.to_datetime(
        eligible["information_available_at"], utc=True, errors="coerce"
    )
    eligible = (
        eligible.sort_values(
            ["symbol", "action_date", "information_available_at"],
            kind="stable",
        )
        .groupby(["symbol", "action_date"], sort=False, as_index=False)
        .tail(1)
        .reset_index(drop=True)
    )
    if eligible.empty:
        raise RuntimeError("No prior-close 04:00 PT source rows are available")
    return eligible


def _current_overnight_sources(
    sources: pd.DataFrame,
    *,
    symbols: Sequence[str],
    as_of: pd.Timestamp,
) -> tuple[pd.DataFrame, date]:
    starts = pd.to_datetime(sources["source_action_start" if "source_action_start" in sources else "target_window_start"],
                            utc=True, errors="coerce")
    future = sources.loc[starts.gt(as_of)].copy()
    if future.empty:
        raise RuntimeError("No future 04:00 PT source row exists for a new gameplan")
    next_date = min(future["action_date"])
    current = future.loc[future["action_date"].eq(next_date)].copy()
    observed = set(current["symbol"].astype("string").str.upper())
    if observed != set(symbols) or len(current) != len(symbols):
        raise RuntimeError(
            "Future overnight source coverage is incomplete: "
            f"action_date={next_date}; observed={sorted(observed)}"
        )
    return current.sort_values("symbol", kind="stable").reset_index(drop=True), next_date


def _build_training_groups(
    samples: pd.DataFrame,
    *,
    sources: pd.DataFrame,
    feature_columns: Sequence[str],
    minute_bars: pd.DataFrame,
    enforce_boundary_alignment: bool = True,
) -> dict[str, pd.DataFrame]:
    hourly, four_hour = _intraday_outcomes(
        sources=sources,
        feature_columns=feature_columns,
        minute_bars=minute_bars,
        enforce_boundary_alignment=enforce_boundary_alignment,
    )
    daily, weekly = _daily_weekly_outcomes(
        samples,
        feature_columns=feature_columns,
    )
    groups = {"1h": hourly, "4h": four_hour, "1d": daily, "1w": weekly}
    for group, frame in groups.items():
        if frame.empty or frame["target"].nunique() != 2:
            raise RuntimeError(f"Nightly {group} training labels are unavailable")
    return groups


def _intraday_outcomes(
    *,
    sources: pd.DataFrame,
    feature_columns: Sequence[str],
    minute_bars: pd.DataFrame,
    enforce_boundary_alignment: bool = True,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    # The opt-out exists only to reproduce historical artifacts in audit scripts.
    # Training and cumulative evaluation always use the default policy.
    bars = minute_bars.copy()
    timestamps = pd.to_datetime(bars["timestamp"], utc=True, errors="coerce")
    local = timestamps.dt.tz_convert(SCHEDULE_TIMEZONE)
    bars["action_date"] = local.dt.date
    bars["start_hour"] = local.dt.hour
    bars = bars.loc[
        timestamps.notna() & bars["start_hour"].between(4, 16)
    ].copy()
    bars["timestamp"] = timestamps.loc[bars.index]
    bars = bars.sort_values(["symbol", "timestamp"], kind="stable")
    source_columns = [
        "symbol",
        "action_date",
        "decision_timestamp",
        *feature_columns,
    ]
    source_frame = sources.loc[:, source_columns].copy()
    source_frame = source_frame.rename(
        columns={"decision_timestamp": "source_decision_timestamp"}
    )

    hourly_windows = _clock_window_outcomes(
        bars,
        sources=sources,
        feature_columns=feature_columns,
        windows=tuple((anchor, anchor - 1, anchor) for anchor in range(5, 18)),
        route_prefix="1h",
    )
    gaps = _overnight_gap_outcomes_from_minutes(
        bars,
        sources=sources,
        feature_columns=feature_columns,
    )
    hourly_output = pd.concat(
        [gaps.assign(route="1h@04:00"), hourly_windows],
        ignore_index=True,
        sort=False,
    )
    hourly_output["model_group"] = "1h"

    four_windows = _clock_window_outcomes(
        bars,
        sources=sources,
        feature_columns=feature_columns,
        windows=((8, 4, 8), (12, 8, 12), (16, 12, 16)),
        route_prefix="4h",
    )
    four_frames = [gaps.assign(route="4h@04:00"), four_windows]
    four_output = pd.concat(four_frames, ignore_index=True, sort=False)
    four_output["model_group"] = "4h"
    return tuple(
        _admit_boundary_aligned_outcomes(
            _clean_outcomes(frame), enforce=enforce_boundary_alignment,
        )
        for frame in (hourly_output, four_output)
    )


def _boundary_observations(
    start: pd.Timestamp,
    end: pd.Timestamp,
    observed_open: pd.Timestamp,
    observed_close: pd.Timestamp,
) -> dict[str, object]:
    start_gap = abs(observed_open - start)
    end_gap = abs(observed_close - end)
    return {
        "observed_open_timestamp": observed_open,
        "observed_close_timestamp": observed_close,
        "target_start_gap_seconds": start_gap.total_seconds(),
        "target_end_gap_seconds": end_gap.total_seconds(),
        "target_boundary_aligned": bool(
            start_gap <= TARGET_BOUNDARY_TOLERANCE
            and end_gap <= TARGET_BOUNDARY_TOLERANCE
            and observed_open < observed_close
        ),
    }


def _admit_boundary_aligned_outcomes(
    frame: pd.DataFrame, *, enforce: bool = True,
) -> pd.DataFrame:
    aligned = frame.get(
        "target_boundary_aligned", pd.Series(False, index=frame.index, dtype=bool),
    ).fillna(False).astype(bool)
    rejected = frame.loc[~aligned]
    output = frame.loc[aligned].copy() if enforce else frame.copy()
    example_columns = [
        "symbol", "route", "action_date", "target_window_start", "target_window_end",
        "observed_open_timestamp", "observed_close_timestamp",
        "target_start_gap_seconds", "target_end_gap_seconds",
    ]
    output.attrs["target_boundary_quality"] = {
        "policy": "clock-window-boundaries-within-five-minutes-v1",
        "enforced": enforce,
        "maximum_boundary_gap_seconds": TARGET_BOUNDARY_TOLERANCE.total_seconds(),
        "candidate_rows": len(frame),
        "aligned_rows": int(aligned.sum()),
        "excluded_rows": len(rejected) if enforce else 0,
        "misaligned_rows_by_route": {
            str(route): int(count)
            for route, count in rejected.groupby("route").size().items()
        },
        "misaligned_examples": [
            {column: str(row[column]) for column in example_columns if column in row}
            for row in rejected.head(12).to_dict("records")
        ],
    }
    return output.reset_index(drop=True)


def _clock_window_outcomes(
    bars: pd.DataFrame,
    *,
    sources: pd.DataFrame,
    feature_columns: Sequence[str],
    windows: Sequence[tuple[int, int, int]],
    route_prefix: str,
) -> pd.DataFrame:
    records: list[dict[str, object]] = []
    source_lookup = {
        (str(row["symbol"]), row["action_date"]): row
        for row in sources.to_dict("records")
    }
    for anchor, start_hour, end_hour in windows:
        selected = bars.loc[
            bars["start_hour"].ge(start_hour) & bars["start_hour"].lt(end_hour)
        ]
        for (symbol, action), group in selected.groupby(
            ["symbol", "action_date"], sort=True
        ):
            source = source_lookup.get((str(symbol), action))
            if source is None or group.empty:
                continue
            open_price = _finite_or_none(group.iloc[0].get("open"))
            close_price = _finite_or_none(group.iloc[-1].get("close"))
            if (
                open_price is None or close_price is None
                or open_price <= 0.0 or close_price <= 0.0
            ):
                continue
            start = _local_timestamp(action, start_hour)
            end = _local_timestamp(action, end_hour)
            raw_return = close_price / open_price - 1.0
            cost = _finite_or_none(source.get("assumed_round_trip_cost"))
            cost = ASSUMED_ROUND_TRIP_COST if cost is None else cost
            records.append(
                {
                    **{column: source.get(column) for column in feature_columns},
                    "symbol": str(symbol),
                    "action_date": action,
                    "decision_timestamp": source["decision_timestamp"],
                    "route": f"{route_prefix}@{anchor:02d}:00",
                    "target_window_start": start,
                    "target_window_end": end,
                    **_boundary_observations(
                        start, end, group.iloc[0]["timestamp"],
                        group.iloc[-1]["timestamp"] + pd.Timedelta(minutes=1),
                    ),
                    "observed_return": raw_return,
                    "target": int(raw_return - cost > 0.0),
                }
            )
    return pd.DataFrame(records)


def _overnight_gap_outcomes_from_minutes(
    bars: pd.DataFrame,
    *,
    sources: pd.DataFrame,
    feature_columns: Sequence[str],
) -> pd.DataFrame:
    daily = {
        symbol: {
            action: group.sort_values("timestamp", kind="stable")
            for action, group in symbol_group.groupby("action_date", sort=True)
        }
        for symbol, symbol_group in bars.groupby("symbol", sort=False)
    }
    records: list[dict[str, object]] = []
    for source in sources.to_dict("records"):
        symbol = str(source["symbol"])
        action = source["action_date"]
        symbol_days = daily.get(symbol, {})
        current = symbol_days.get(action)
        prior_dates = [value for value in symbol_days if value < action]
        if current is None or not prior_dates:
            continue
        prior = symbol_days[max(prior_dates)]
        open_price = _finite_or_none(current.iloc[0].get("open"))
        close_price = _finite_or_none(prior.iloc[-1].get("close"))
        if (
            open_price is None or close_price is None
            or open_price <= 0.0 or close_price <= 0.0
        ):
            continue
        start = _local_timestamp(max(prior_dates), 17)
        end = _local_timestamp(action, 4)
        raw_return = open_price / close_price - 1.0
        cost = _finite_or_none(source.get("assumed_round_trip_cost"))
        cost = ASSUMED_ROUND_TRIP_COST if cost is None else cost
        records.append(
            {
                **{column: source.get(column) for column in feature_columns},
                "symbol": symbol,
                "action_date": action,
                "decision_timestamp": source["decision_timestamp"],
                "target_window_start": start,
                "target_window_end": end,
                **_boundary_observations(
                    start, end, prior.iloc[-1]["timestamp"] + pd.Timedelta(minutes=1),
                    current.iloc[0]["timestamp"],
                ),
                "observed_return": raw_return,
                "target": int(raw_return - cost > 0.0),
            }
        )
    return pd.DataFrame(records)


def _load_equity_minute_bars(
    root: Path,
    *,
    symbols: Sequence[str],
) -> tuple[pd.DataFrame, tuple[Path, ...]]:
    frames: list[pd.DataFrame] = []
    files: list[Path] = []
    for symbol in symbols:
        folder = root / "stocks" / symbol / "bars" / "1m" / "databento" / "normalized"
        paths = tuple(sorted(folder.glob("*_ohlcv-1m_1m.parquet")))
        if not paths:
            raise RuntimeError(f"Canonical one-minute equity bars are missing: {symbol}")
        for path in paths:
            frame = pd.read_parquet(
                path,
                columns=["timestamp", "open", "close"],
            )
            frame["symbol"] = symbol
            frames.append(frame)
            files.append(path)
    bars = pd.concat(frames, ignore_index=True, sort=False)
    bars["timestamp"] = pd.to_datetime(bars["timestamp"], utc=True, errors="coerce")
    bars = bars.dropna(subset=["timestamp", "open", "close"])
    return bars, tuple(files)


def _daily_weekly_outcomes(
    samples: pd.DataFrame,
    *,
    feature_columns: Sequence[str],
) -> tuple[pd.DataFrame, pd.DataFrame]:
    components = samples.loc[
        samples["horizon"].astype("string").isin(DAILY_HORIZONS)
    ].copy()
    components = components.sort_values(
        ["symbol", "decision_timestamp", "horizon", "information_available_at"],
        kind="stable",
    ).drop_duplicates(["symbol", "decision_timestamp", "horizon"], keep="last")
    complete = components.loc[
        components["label_status"].astype("string").eq("COMPLETE")
    ].copy()
    complete["route"] = complete["horizon"].map(
        {horizon: f"1d@D+{index}" for index, horizon in enumerate(DAILY_HORIZONS, 1)}
    )
    complete["target"] = pd.to_numeric(
        complete["target_cost_adjusted_positive"], errors="coerce"
    )
    complete["observed_return"] = pd.to_numeric(
        complete["forward_raw_return"], errors="coerce"
    )
    complete["model_group"] = "1d"
    daily = _clean_outcomes(
        complete.loc[:, [
            "symbol",
            "decision_timestamp",
            "target_window_start",
            "target_window_end",
            "route",
            "target",
            "observed_return",
            "model_group",
            *feature_columns,
        ]]
    )

    d1 = components.loc[components["horizon"].astype("string").eq("1w-d1")].copy()
    d5 = components.loc[components["horizon"].astype("string").eq("1w-d5")].copy()
    key = ["symbol", "decision_timestamp"]
    merged = d1.merge(
        d5.loc[:, [*key, "target_close", "target_window_end", "label_status"]],
        on=key,
        how="inner",
        suffixes=("_d1", "_d5"),
    )
    raw_return = (
        pd.to_numeric(merged["target_close_d5"], errors="coerce")
        / pd.to_numeric(merged["target_open"], errors="coerce")
        - 1.0
    )
    cost = pd.to_numeric(
        merged["assumed_round_trip_cost"], errors="coerce"
    ).fillna(ASSUMED_ROUND_TRIP_COST)
    merged["route"] = "1w@D+5"
    merged["target"] = raw_return.sub(cost).gt(0.0).astype(int)
    merged["observed_return"] = raw_return
    merged["target_window_start"] = merged["target_window_start"]
    merged["target_window_end"] = merged["target_window_end_d5"]
    merged["model_group"] = "1w"
    weekly_complete = (
        merged["label_status_d1"].astype("string").eq("COMPLETE")
        & merged["label_status_d5"].astype("string").eq("COMPLETE")
        & np.isfinite(raw_return)
    )
    weekly = _clean_outcomes(
        merged.loc[weekly_complete, [
            "symbol",
            "decision_timestamp",
            "target_window_start",
            "target_window_end",
            "route",
            "target",
            "observed_return",
            "model_group",
            *feature_columns,
        ]]
    )
    return daily, weekly


def _clean_outcomes(frame: pd.DataFrame) -> pd.DataFrame:
    required = [
        "symbol",
        "decision_timestamp",
        "target_window_start",
        "target_window_end",
        "route",
        "target",
        "observed_return",
        "model_group",
    ]
    output = frame.copy()
    if output.empty:
        output = output.reindex(columns=list(dict.fromkeys([*output.columns, *required])))
    for column in ("decision_timestamp", "target_window_start", "target_window_end"):
        output[column] = pd.to_datetime(output[column], utc=True, errors="coerce")
    output["target"] = pd.to_numeric(output["target"], errors="coerce")
    output["observed_return"] = pd.to_numeric(
        output["observed_return"], errors="coerce"
    )
    output = output.dropna(subset=required)
    output = output.loc[output["target"].isin([0, 1])].copy()
    output["target"] = output["target"].astype(int)
    return output.sort_values(
        ["decision_timestamp", "symbol", "route"], kind="stable"
    ).reset_index(drop=True)


def _build_current_groups(
    samples: pd.DataFrame,
    *,
    current_sources: pd.DataFrame,
    action_date: date,
    as_of: pd.Timestamp,
    symbols: Sequence[str],
    feature_columns: Sequence[str],
) -> dict[str, pd.DataFrame]:
    hourly_rows: list[dict[str, object]] = []
    four_rows: list[dict[str, object]] = []
    for source in current_sources.to_dict("records"):
        base = {
            **{column: source.get(column) for column in feature_columns},
            "symbol": str(source["symbol"]).upper(),
            "decision_timestamp": source["decision_timestamp"],
            "information_available_at": source["information_available_at"],
            "action_date": action_date,
        }
        for anchor in HOURLY_ANCHORS:
            if anchor == 4:
                start = pd.Timestamp(source["bar_end_timestamp"])
                end = _local_timestamp(action_date, 4)
                semantics = "prior_17_close_to_04_open_gap"
            else:
                start = _local_timestamp(action_date, anchor - 1)
                end = _local_timestamp(action_date, anchor)
                semantics = "preceding_one_hour_action_window_return"
            hourly_rows.append(
                {
                    **base,
                    "model_group": "1h",
                    "route": f"1h@{anchor:02d}:00",
                    "forecast_anchor_local": f"{anchor:02d}:00",
                    "target_window_start": start,
                    "target_window_end": end,
                    "target_semantics": semantics,
                }
            )
        for anchor in FOUR_HOUR_ANCHORS:
            if anchor == 4:
                start = pd.Timestamp(source["bar_end_timestamp"])
                end = _local_timestamp(action_date, 4)
                semantics = "prior_17_close_to_04_open_gap_checkpoint"
            else:
                start = _local_timestamp(action_date, anchor - 4)
                end = _local_timestamp(action_date, anchor)
                semantics = "four_hour_action_window_return"
            four_rows.append(
                {
                    **base,
                    "model_group": "4h",
                    "route": f"4h@{anchor:02d}:00",
                    "forecast_anchor_local": f"{anchor:02d}:00",
                    "target_window_start": start,
                    "target_window_end": end,
                    "target_semantics": semantics,
                }
            )

    components = samples.loc[
        samples["symbol"].astype("string").str.upper().isin(symbols)
        & samples["horizon"].astype("string").isin(DAILY_HORIZONS)
        & ~samples["label_status"].astype("string").eq("COMPLETE")
        & pd.to_datetime(samples["target_window_end"], utc=True, errors="coerce").gt(as_of)
    ].copy()
    components = (
        components.sort_values("information_available_at", kind="stable")
        .groupby(["symbol", "horizon"], sort=False, as_index=False)
        .tail(1)
        .reset_index(drop=True)
    )
    if len(components) != len(symbols) * 5:
        raise RuntimeError(
            "The next five daily component forecasts are incomplete: "
            f"expected={len(symbols) * 5}; observed={len(components)}"
        )
    components["model_group"] = "1d"
    components["route"] = components["horizon"].map(
        {horizon: f"1d@D+{index}" for index, horizon in enumerate(DAILY_HORIZONS, 1)}
    )
    components["forecast_anchor_local"] = components["route"].str.removeprefix("1d@")
    components["target_semantics"] = "regular_session_open_to_close_return"
    daily = components.loc[:, [
        "symbol",
        "decision_timestamp",
        "information_available_at",
        "target_window_start",
        "target_window_end",
        "model_group",
        "route",
        "forecast_anchor_local",
        "target_semantics",
        *feature_columns,
    ]].copy()

    d1 = components.loc[components["horizon"].astype("string").eq("1w-d1")].copy()
    d5 = components.loc[components["horizon"].astype("string").eq("1w-d5")].copy()
    weekly = d1.merge(
        d5.loc[:, ["symbol", "target_window_end"]],
        on="symbol",
        how="inner",
        suffixes=("", "_d5"),
        validate="one_to_one",
    )
    weekly["model_group"] = "1w"
    weekly["route"] = "1w@D+5"
    weekly["forecast_anchor_local"] = "D+5"
    weekly["target_semantics"] = "D+1_regular_open_to_D+5_regular_close_return"
    weekly["target_window_end"] = weekly["target_window_end_d5"]
    weekly = weekly.loc[:, [
        "symbol",
        "decision_timestamp",
        "information_available_at",
        "target_window_start",
        "target_window_end",
        "model_group",
        "route",
        "forecast_anchor_local",
        "target_semantics",
        *feature_columns,
    ]].copy()
    return {
        "1h": pd.DataFrame(hourly_rows),
        "4h": pd.DataFrame(four_rows),
        "1d": daily,
        "1w": weekly,
    }


def _fit_group_model(
    samples: pd.DataFrame,
    *,
    current: pd.DataFrame,
    feature_columns: Sequence[str],
    group: str,
    model_directory: Path,
    trained_at: pd.Timestamp,
) -> dict[str, object]:
    selection_contract = source_selection_contract(current)
    if source_selection_contract(samples) != selection_contract:
        raise RuntimeError("Training and current Gameplan source selection contracts disagree")
    probability_target = probability_target_contract(current)
    if probability_target_contract(samples) != probability_target:
        raise RuntimeError("Training and current Gameplan probability target contracts disagree")
    probability_metadata = probability_target_metadata(probability_target)
    if probability_target == RAW_DIRECTION_TARGET:
        _verify_probability_cohort(samples, probability_target)
    independent_selection = (
        "target_contract_version" in current
        and bool(current.target_contract_version.eq(STOCK_TARGET_CONTRACT_VERSION).all())
    )
    if independent_selection:
        samples = with_stock_calendar_features(samples)
        current = with_stock_calendar_features(current)
        feature_columns = tuple(dict.fromkeys((*feature_columns, *STOCK_CALENDAR_FEATURE_NAMES)))
    target_quality = samples.attrs.get("target_boundary_quality")
    if target_quality and target_quality.get("excluded_rows", 0):
        print(json.dumps({
            "training_event": "FIT_WARNING", "fit": f"gameplan/{group}/target-quality",
            "message": "Excluded returns whose price observations miss the target boundaries.",
            "target_boundary_quality": target_quality,
        }), flush=True)
    partitions = _chronological_partitions(samples, group=group)
    minimum_non_null = max(20, int(math.ceil(len(partitions["train"]) * 0.01)))
    admitted = tuple(
        column
        for column in feature_columns
        if int(partitions["train"][column].notna().sum()) >= minimum_non_null
        and int(partitions["train"][column].nunique(dropna=True)) > 1
    )
    if not admitted:
        raise RuntimeError(f"No varying causal features are available for {group}")
    categorical = ("symbol", "route")
    train_matrix = _model_frame(partitions["train"], admitted, categorical)
    selection_matrix = _model_frame(partitions["selection"], admitted, categorical)
    target_train = partitions["train"]["target"].astype(int).to_numpy()
    target_selection = partitions["selection"]["target"].astype(int).to_numpy()
    tree = _estimator("tree", admitted, categorical)
    neural = _estimator("neural", admitted, categorical)
    fit_with_progress(tree, train_matrix, target_train, label=f"gameplan/{group}/tree-selection")
    fit_with_progress(neural, train_matrix, target_train, label=f"gameplan/{group}/neural-selection")
    tree_selection = tree.predict_proba(selection_matrix)[:, 1]
    neural_selection = neural.predict_proba(selection_matrix)[:, 1]
    candidates: list[tuple[str, float | None, np.ndarray]] = [
        ("hist-gradient", 0.0, tree_selection),
        ("mlp", 1.0, neural_selection),
    ]
    for weight in (0.25, 0.50, 0.75):
        candidates.append(
            (
                f"hist-gradient-mlp-{weight:.2f}",
                weight,
                (1.0 - weight) * tree_selection + weight * neural_selection,
            )
        )
    logistic_candidates = {}
    if independent_selection:
        for regularization_c in logistic_regularization_candidates(group, probability_target=probability_target):
            logistic = _estimator("logistic", admitted, categorical)
            if regularization_c != 1.0:
                logistic.set_params(classifier__C=regularization_c)
            name = f"regularized-logistic-c{regularization_c:g}"
            fit_with_progress(logistic, train_matrix, target_train,
                              label=f"gameplan/{group}/logistic-c{regularization_c:g}-selection")
            candidates.append((name, None, logistic.predict_proba(selection_matrix)[:, 1]))
            logistic_candidates[name] = regularization_c
    shrinkage = {name: (name, 1.0) for name, _, _ in candidates}
    shrinkage_policy = (WEEKLY_PROBABILITY_SHRINKAGE_POLICY
                        if independent_selection and group == "1w" and probability_target == RAW_DIRECTION_TARGET else None)
    selection_prior = float(target_train.mean())
    if shrinkage_policy is not None:
        for name, neural_weight, probability in tuple(candidates):
            for weight in WEEKLY_PROBABILITY_SHRINKAGE_WEIGHTS:
                if weight == 1.0:
                    continue
                candidate_name = f"{name}-prior-shrinkage-w{weight:g}"
                candidates.append((candidate_name, neural_weight,
                                   weight * probability + (1.0 - weight) * selection_prior))
                shrinkage[candidate_name] = (name, weight)
    selection_metrics = {
        name: _proper_scores(target_selection, probability)
        for name, _weight, probability in candidates
    }
    selected_name, selected_weight, _ = min(
        candidates,
        key=lambda candidate: (selection_metrics[candidate[0]]["log_loss"], shrinkage[candidate[0]][1] != 1.0),
    )
    selected_base_name, selected_shrinkage_weight = shrinkage[selected_name]

    fit_frame = pd.concat(
        [partitions["train"], partitions["selection"]],
        ignore_index=True,
        sort=False,
    )
    fit_matrix = _model_frame(fit_frame, admitted, categorical)
    fit_target = fit_frame["target"].astype(int).to_numpy()
    final_tree = _estimator("tree", admitted, categorical)
    final_neural = _estimator("neural", admitted, categorical)
    fit_with_progress(final_tree, fit_matrix, fit_target, label=f"gameplan/{group}/tree-final")
    fit_with_progress(final_neural, fit_matrix, fit_target, label=f"gameplan/{group}/neural-final")
    if independent_selection:
        final_logistic = _estimator("logistic", admitted, categorical)
        selected_logistic_c = logistic_candidates.get(selected_base_name)
        if selected_logistic_c is not None and selected_logistic_c != 1.0:
            final_logistic.set_params(classifier__C=selected_logistic_c)
        fit_with_progress(final_logistic, fit_matrix, fit_target, label=f"gameplan/{group}/logistic-final")
    if selected_base_name in logistic_candidates:
        estimator: object = final_logistic
    elif selected_weight <= 0.0:
        estimator = final_tree
    elif selected_weight >= 1.0:
        estimator = final_neural
    else:
        estimator = _ProbabilityBlend(
            final_tree,
            final_neural,
            neural_weight=selected_weight,
        )
    fitted_prior = float(fit_target.mean())
    if selected_shrinkage_weight != 1.0:
        estimator = PriorProbabilityShrinkage(estimator, prior_probability=fitted_prior,
                                              weight=selected_shrinkage_weight)
    shrinkage_metadata = {
        "selected_base_family": selected_base_name,
        "probability_shrinkage_policy": shrinkage_policy,
        "selected_probability_shrinkage_weight": selected_shrinkage_weight,
        "selection_shrinkage_prior": selection_prior if shrinkage_policy else None,
        "fitted_shrinkage_prior": fitted_prior if shrinkage_policy else None,
        "probability_shrinkage_weights": list(WEEKLY_PROBABILITY_SHRINKAGE_WEIGHTS) if shrinkage_policy else [1.0],
    }

    calibration_matrix = _model_frame(
        partitions["calibration"], admitted, categorical
    )
    calibration_target = partitions["calibration"]["target"].astype(int).to_numpy()
    calibration_raw = estimator.predict_proba(calibration_matrix)[:, 1]
    calibration_selection = None
    if independent_selection:
        calibrator, calibration_selection = select_development_calibrator(
            partitions["calibration"], calibration_raw,
            probability_target=probability_target,
        )
    elif np.unique(calibration_target).size == 2:
        calibrator = fit_probability_calibrator(
            "platt",
            calibration_raw,
            calibration_target,
            platt_regularization_c=0.1,
            clip_to_observed_probability_range=True,
            require_nondecreasing=True,
        )
    else:
        calibrator = IdentityCalibrator()
    assessment_matrix = _model_frame(
        partitions["assessment"], admitted, categorical
    )
    assessment_target = partitions["assessment"]["target"].astype(int).to_numpy()
    assessment_raw = estimator.predict_proba(assessment_matrix)[:, 1]
    assessment_probability = calibrator.predict(assessment_raw)
    calibration_diagnostics = _calibration_signal_diagnostics(
        calibrator, calibration_raw, calibration_target, assessment_probability,
    )
    if not calibration_diagnostics["information_available"]:
        print(json.dumps({
            "training_event": "FIT_WARNING", "fit": f"gameplan/{group}/calibration",
            "message": "Calibration has no usable ranking; this group remains research only.",
            "calibration": calibration_diagnostics,
        }), flush=True)
    assessment = _proper_scores(assessment_target, assessment_probability)
    assessment_raw_scores = _proper_scores(assessment_target, assessment_raw)
    calibration_raw_scores = _proper_scores(calibration_target, calibration_raw)
    base_rate = float(fit_target.mean())
    baseline = _proper_scores(
        assessment_target,
        np.full(len(assessment_target), base_rate, dtype=float),
    )
    gate = build_promotion_gate(assessment, baseline, calibration_diagnostics,
        int(partitions["assessment"]["decision_timestamp"].nunique()),
        policy_version=DIRECTIONAL_PROMOTION_POLICY if independent_selection else STRICT_PROMOTION_POLICY)
    gate_checks = gate["checks"]
    if not all(gate_checks.values()):
        print(json.dumps({
            "training_event": "FIT_WARNING", "fit": f"gameplan/{group}/assessment",
            "message": "Held-out assessment did not support promotion; inspect data and model quality before retrying.",
            "failed_checks": [name for name, passed in gate_checks.items() if not passed],
            "raw": assessment_raw_scores, "calibrated": assessment,
            "training_base_rate": baseline,
        }), flush=True)
    route_metrics: dict[str, object] = {}
    assessment_frame = partitions["assessment"].copy()
    assessment_frame["probability"] = assessment_probability
    for route, frame in assessment_frame.groupby("route", sort=True):
        route_metrics[str(route)] = _proper_scores(
            frame["target"].astype(int).to_numpy(),
            frame["probability"].to_numpy(),
        )
    support_by_symbol, assessment_by_symbol = _symbol_target_support(
        samples, fitted=fit_frame, assessment=assessment_frame,
        symbols=current["symbol"].astype("string").unique(),
    )

    model_directory.mkdir(parents=True, exist_ok=False)
    model_path = model_directory / "model.joblib"
    temporary = model_path.with_suffix(".joblib.tmp")
    joblib.dump(
        {
            "schema_version": GAMEPLAN_VERSION,
            **probability_metadata,
            "group": group,
            "estimator": estimator,
            "calibrator": calibrator,
            "feature_columns": admitted,
            "categorical_columns": categorical,
            "selected_family": selected_name,
            "selected_neural_weight": selected_weight,
            "selected_logistic_regularization_c": logistic_candidates.get(selected_base_name),
            **shrinkage_metadata,
            "selection_metrics": selection_metrics,
            "directional_promotion_policy": gate["policy_version"],
            "logistic_regularization_policy": logistic_regularization_policy(group, probability_target=probability_target) if independent_selection else None,
            "development_selection_policy": development_selection_policy(probability_target) if independent_selection else None,
            "target_calendar_feature_contract": STOCK_CALENDAR_FEATURE_CONTRACT if independent_selection else None,
            "source_selection_contract": selection_contract,
            "calibration_selection": calibration_selection,
            "trained_at": trained_at.isoformat(),
        },
        temporary,
    )
    temporary.replace(model_path)

    current_matrix = _model_frame(current, admitted, categorical)
    current_raw = estimator.predict_proba(current_matrix)[:, 1]
    current_probability = calibrator.predict(current_raw)
    forecasts = current.loc[:, [
        "symbol",
        "model_group",
        "route",
        "forecast_anchor_local",
        "decision_timestamp",
        "information_available_at",
        "target_window_start",
        "target_window_end",
        "target_semantics",
        *(column for column in ("target_role", "execution_eligible", "target_contract_version", "trading_hours", "trading_segments_json", "target_price_source_contract", "target_price_dataset", *SOURCE_SELECTION_COLUMNS) if column in current),
    ]].copy()
    for name, value in probability_metadata.items():
        forecasts[name] = value
    forecasts["assumed_round_trip_cost"] = current.get("assumed_round_trip_cost", ASSUMED_ROUND_TRIP_COST)
    if independent_selection:
        # Freeze the same causal inputs used by the current feature row. An
        # incomplete map remains explicitly unsupported by market-aware sizing.
        forecasts["enrichment_feature_values_json"] = [
            json.dumps(frozen_market_feature_values(row, required=False), sort_keys=True, allow_nan=False)
            for row in current.to_dict("records")
        ]
        forecasts["enrichment_feature_contract"] = INDEPENDENT_MARKET_FEATURE_CONTRACT
        forecasts["target_calendar_feature_contract"] = STOCK_CALENDAR_FEATURE_CONTRACT
        forecasts[list(STOCK_CALENDAR_FEATURE_NAMES)] = current[list(STOCK_CALENDAR_FEATURE_NAMES)]
    forecasts["raw_probability"] = current_raw
    forecasts["calibrated_probability"] = current_probability
    forecasts["model_family"] = selected_name
    forecasts["neural_weight"] = selected_weight
    forecasts["calibration_method"] = getattr(calibrator, "method", "none")
    forecasts["calibration_status"] = calibration_diagnostics["status"]
    forecasts["model_status"] = gate["status"]
    for column, key in (("symbol_target_history_rows", "admitted_rows"),
                        ("symbol_fitted_target_rows", "fitted_rows"),
                        ("symbol_assessment_rows", "assessment_rows")):
        forecasts[column] = forecasts["symbol"].map(
            {symbol: counts[key] for symbol, counts in support_by_symbol.items()}
        ).fillna(0).astype(int)
    forecasts.loc[forecasts.symbol_fitted_target_rows.eq(0), "model_status"] = "RESEARCH_NO_TARGET_HISTORY"
    if "target_contract_version" in forecasts:
        independent = forecasts.target_contract_version.eq(STOCK_TARGET_CONTRACT_VERSION)
        for column, key in (("symbol_route_target_history_rows", "admitted_rows_by_route"),
                            ("symbol_route_fitted_target_rows", "fitted_rows_by_route"),
                            ("symbol_route_assessment_rows", "assessment_rows_by_route")):
            forecasts[column] = [
                int(support_by_symbol.get(str(symbol), {}).get(key, {}).get(str(route), 0))
                for symbol, route in zip(forecasts.symbol, forecasts.route)
            ]
        unsupported_route = independent & forecasts.symbol_route_fitted_target_rows.eq(0)
        forecasts.loc[unsupported_route, "model_status"] = "RESEARCH_NO_TARGET_HISTORY"
    forecasts["model_artifact"] = (Path("models") / group / "model.joblib").as_posix()
    forecasts["option_feature_count"] = sum(
        column.startswith(("opt__", "opx__")) for column in admitted
    )
    report = {
        "schema_version": GAMEPLAN_VERSION,
        **probability_metadata,
        "group": group,
        "target_contract_version": str(current["target_contract_version"].iloc[0]) if "target_contract_version" in current else TARGET_CONTRACT_VERSION,
        **({"target_price_source_contract": str(current["target_price_source_contract"].iloc[0]),
            "target_price_dataset": str(current["target_price_dataset"].iloc[0])}
           if "target_price_source_contract" in current else {}),
        "selected_family": selected_name,
        **shrinkage_metadata,
        "selected_neural_weight": selected_weight,
        "both_hist_gradient_and_mlp_trained": True,
        "regularized_logistic_trained": independent_selection,
        "logistic_regularization_candidates": list(logistic_regularization_candidates(group, probability_target=probability_target)) if independent_selection else [],
        "selected_logistic_regularization_c": logistic_candidates.get(selected_base_name),
        "logistic_regularization_policy": logistic_regularization_policy(group, probability_target=probability_target) if independent_selection else None,
        "development_selection_policy": development_selection_policy(probability_target) if independent_selection else None,
        "target_calendar_feature_contract": STOCK_CALENDAR_FEATURE_CONTRACT if independent_selection else None,
        "source_selection_contract": selection_contract,
        "target_calendar_feature_names": list(STOCK_CALENDAR_FEATURE_NAMES) if independent_selection else [],
        "calibration_selection": calibration_selection,
        "selection_metrics": selection_metrics,
        "calibration_method": getattr(calibrator, "method", "none"),
        "calibration_diagnostics": calibration_diagnostics,
        "target_boundary_quality": target_quality,
        "calibration_raw_scores": calibration_raw_scores,
        "assessment_raw_scores": assessment_raw_scores,
        "calibration_assessment_change": {
            "brier_score": assessment["brier_score"] - assessment_raw_scores["brier_score"],
            "log_loss": assessment["log_loss"] - assessment_raw_scores["log_loss"],
            "interpretation": "Negative changes mean calibration improved the raw scores on held-out assessment.",
        },
        "assessment": assessment,
        "training_base_rate_assessment": baseline,
        "assessment_by_route": route_metrics,
        "assessment_by_symbol": assessment_by_symbol,
        "target_support_by_symbol": support_by_symbol,
        "promotion_gate": gate,
        "features": {
            "admitted_count": len(admitted),
            "admitted": list(admitted),
            "option_market_features": [
                column
                for column in admitted
                if column.startswith(("opt__", "opx__"))
            ],
        },
        "partitions": {
            name + "_rows": len(frame)
            for name, frame in partitions.items()
        },
        "partition_decision_clusters": {
            name: int(frame["decision_timestamp"].nunique())
            for name, frame in partitions.items()
        },
        "model_file": {
            "path": model_path.relative_to(model_directory.parent.parent).as_posix(),
            "size": model_path.stat().st_size,
            "checksum_sha256": file_checksum(model_path),
        },
    }
    return {"forecasts": forecasts, "report": report}


def _symbol_target_support(
    samples: pd.DataFrame, *, fitted: pd.DataFrame, assessment: pd.DataFrame,
    symbols: Sequence[str],
) -> tuple[dict[str, object], dict[str, object]]:
    support, metrics = {}, {}
    for symbol in sorted(set(map(str, symbols)) | set(samples["symbol"].astype(str))):
        counts = {}
        for name, source in (("admitted", samples), ("fitted", fitted), ("assessment", assessment)):
            frame = source.loc[source["symbol"].astype(str).eq(symbol)]
            counts[f"{name}_rows"] = len(frame)
            counts[f"{name}_decision_clusters"] = int(frame["decision_timestamp"].nunique())
            counts[f"{name}_rows_by_route"] = {
                str(route): int(count) for route, count in frame.groupby("route").size().items()
            }
        held_out = assessment.loc[assessment["symbol"].astype(str).eq(symbol)]
        metrics[symbol] = (
            _proper_scores(held_out["target"].astype(int).to_numpy(), held_out["probability"].to_numpy())
            if not held_out.empty else None
        )
        support[symbol] = counts
    return support, metrics


def _calibration_signal_diagnostics(
    calibrator: object, raw_probability: object, target: object,
    assessment_probability: object,
) -> dict[str, object]:
    raw = np.asarray(raw_probability, dtype=float)
    calibrated = np.asarray(calibrator.predict(raw), dtype=float)
    assessed = np.asarray(assessment_probability, dtype=float)
    labels = np.asarray(target, dtype=int)
    constrained = bool(getattr(calibrator, "nondecreasing_constraint_active", False))
    has_classes = np.unique(labels).size == 2
    # A constant base-rate map is a valid calibration fallback, but it carries
    # no model ranking and must not pass as a promoted directional forecast.
    has_information = bool(
        has_classes and not constrained and np.ptp(calibrated) > 1e-12
        and np.ptp(assessed) > 1e-12
    )
    if not has_classes:
        status = "INSUFFICIENT_CALIBRATION_CLASSES"
    elif constrained or np.ptp(calibrated) <= 1e-12:
        status = "FLAT_CALIBRATION"
    elif np.ptp(assessed) <= 1e-12:
        status = "FLAT_ASSESSMENT"
    else:
        status = "DIRECTIONAL_INFORMATION_AVAILABLE"
    model = getattr(calibrator, "model", None)
    slope = getattr(model, "coef_", None)
    return {
        "status": status,
        "information_available": has_information,
        "nondecreasing_constraint_active": constrained,
        "calibration_rows": len(labels), "calibration_positive_rate": float(labels.mean()),
        "raw_probability_range": [float(raw.min()), float(raw.max())],
        "calibrated_probability_range": [float(calibrated.min()), float(calibrated.max())],
        "assessment_probability_range": [float(assessed.min()), float(assessed.max())],
        "platt_slope": float(np.asarray(slope).reshape(-1)[0]) if slope is not None else None,
    }


def _chronological_partitions(
    frame: pd.DataFrame,
    *,
    group: str,
) -> dict[str, pd.DataFrame]:
    ordered = frame.sort_values("decision_timestamp", kind="stable").copy()
    clusters = pd.Index(
        pd.to_datetime(ordered["decision_timestamp"], utc=True)
        .drop_duplicates()
        .sort_values()
    )
    if len(clusters) < 40:
        raise RuntimeError(
            f"Nightly {group} needs at least 40 decision clusters; observed={len(clusters)}"
        )
    holdout = min(63, max(10, len(clusters) // 8))
    if len(clusters) - 3 * holdout < 20:
        holdout = max(5, (len(clusters) - 20) // 3)
    train_end = len(clusters) - 3 * holdout
    selection_end = train_end + holdout
    calibration_end = selection_end + holdout
    cluster_sets = {
        "train": clusters[:train_end],
        "selection": clusters[train_end:selection_end],
        "calibration": clusters[selection_end:calibration_end],
        "assessment": clusters[calibration_end:],
    }
    partitions = {
        name: ordered.loc[ordered["decision_timestamp"].isin(values)].copy()
        for name, values in cluster_sets.items()
    }
    names = ("train", "selection", "calibration", "assessment")
    for left_name, right_name in zip(names, names[1:]):
        right = partitions[right_name]
        boundary = pd.to_datetime(
            right["target_window_start"], utc=True, errors="coerce"
        ).min()
        partitions[left_name] = partitions[left_name].loc[
            pd.to_datetime(
                partitions[left_name]["target_window_end"],
                utc=True,
                errors="coerce",
            ).lt(boundary)
        ].copy()
    for name, partition in partitions.items():
        if partition.empty or partition["target"].nunique() != 2:
            raise RuntimeError(
                f"Nightly {group} {name} partition lacks two target classes"
            )
        partitions[name] = partition.reset_index(drop=True)
    return partitions


def _estimator(
    family: str,
    numeric: Sequence[str],
    categorical: Sequence[str],
) -> Pipeline:
    numeric_steps: list[tuple[str, object]] = [
        (
            "imputer",
            SimpleImputer(
                strategy="median",
                add_indicator=True,
                keep_empty_features=True,
            ),
        )
    ]
    if family in {"neural", "logistic"}:
        numeric_steps.append(("scale", StandardScaler()))
    transformer = ColumnTransformer(
        (
            ("numeric", Pipeline(numeric_steps), list(numeric)),
            (
                "categorical",
                OneHotEncoder(handle_unknown="ignore", sparse_output=False),
                list(categorical),
            ),
        ),
        sparse_threshold=0.0,
    )
    if family == "tree":
        classifier: object = HistGradientBoostingClassifier(
            learning_rate=0.05,
            max_iter=120,
            max_leaf_nodes=15,
            l2_regularization=1.0,
            class_weight="balanced",
            random_state=20260904,
        )
    elif family == "neural":
        classifier = MLPClassifier(
            hidden_layer_sizes=(48, 24),
            activation="relu",
            solver="adam",
            alpha=0.01,
            batch_size=256,
            learning_rate_init=0.001,
            max_iter=180,
            early_stopping=True,
            validation_fraction=0.15,
            n_iter_no_change=12,
            random_state=20260904,
        )
    elif family == "logistic":
        classifier = LogisticRegression(C=1.0, solver="lbfgs", max_iter=1000, random_state=20260904)
    else:
        raise ValueError(f"Unknown model family: {family}")
    return Pipeline((('preprocess', transformer), ('classifier', classifier)))


def _model_frame(
    frame: pd.DataFrame,
    numeric: Sequence[str],
    categorical: Sequence[str],
) -> pd.DataFrame:
    output = frame.loc[:, [*numeric, *categorical]].copy()
    for column in numeric:
        output[column] = pd.to_numeric(output[column], errors="coerce")
    for column in categorical:
        output[column] = output[column].astype("string").fillna("UNKNOWN")
    return output


def _proper_scores(target: np.ndarray, probability: np.ndarray) -> dict[str, float]:
    labels = np.asarray(target, dtype=int)
    values = np.clip(np.asarray(probability, dtype=float), 1e-6, 1.0 - 1e-6)
    bins = np.minimum((values * 10).astype(int), 9)
    ece = 0.0
    for index in range(10):
        mask = bins == index
        if mask.any():
            ece += float(mask.mean()) * abs(
                float(values[mask].mean()) - float(labels[mask].mean())
            )
    return {
        "rows": int(len(labels)),
        "positive_rate": float(labels.mean()),
        "mean_probability": float(values.mean()),
        "brier_score": float(brier_score_loss(labels, values)),
        "log_loss": float(log_loss(labels, values, labels=[0, 1])),
        "expected_calibration_error_10_bin": float(ece),
        "direction_accuracy_at_0_5": float(
            ((values >= 0.5).astype(int) == labels).mean()
        ),
    }


def _finalize_forecasts(
    forecasts: pd.DataFrame,
    *,
    symbols: Sequence[str],
    action_date: date,
    frozen_at: pd.Timestamp,
    action_start: pd.Timestamp,
    action_end: pd.Timestamp,
    opra_freshness: Mapping[str, object] | None,
    target_contract_version: str = TARGET_CONTRACT_VERSION,
) -> pd.DataFrame:
    expected = len(symbols) * EXPECTED_FORECASTS_PER_SYMBOL
    if len(forecasts) != expected:
        raise RuntimeError(
            f"Forecast grid is incomplete: expected={expected}; observed={len(forecasts)}"
        )
    natural = forecasts[["symbol", "route"]].astype("string")
    if natural.duplicated().any():
        raise RuntimeError("Forecast grid contains duplicate symbol/route rows")
    output = forecasts.copy()
    probability = pd.to_numeric(output["calibrated_probability"], errors="coerce")
    if probability.isna().any() or ~probability.between(0.0, 1.0).all():
        raise RuntimeError("Forecast probabilities are invalid")
    output.insert(
        0,
        "id",
        [
            f"{action_date.isoformat()}:{str(symbol).upper()}:{route}"
            for symbol, route in zip(output["symbol"], output["route"])
        ],
    )
    output["action_date"] = action_date.isoformat()
    if target_contract_version == STOCK_TARGET_CONTRACT_VERSION:
        output["action_anchor_local"] = [
            pd.Timestamp(row["target_window_start"]).tz_convert(SCHEDULE_TIMEZONE).strftime("%H:%M")
            if row.get("target_role") == "EXECUTION" and row.get("execution_eligible") is True else None
            for row in output.to_dict("records")
        ]
    else:
        output["action_anchor_local"] = [
            _action_anchor_for_route(str(group), str(route))
            for group, route in zip(output["model_group"], output["route"])
        ]
    from ml.stock_direction_policy import BULLISH_PROBABILITY, BEARISH_PROBABILITY, STOCK_DIRECTION_POLICY_VERSION
    output["direction"] = np.select(
        [probability.gt(BULLISH_PROBABILITY), probability.lt(BEARISH_PROBABILITY)],
        ["BULLISH", "BEARISH"],
        default="NO_EDGE",
    )
    output.loc[output["model_status"].eq("RESEARCH_NO_TARGET_HISTORY"), "direction"] = "NO_EDGE"
    output["direction_policy_version"] = STOCK_DIRECTION_POLICY_VERSION
    output["direction_up_threshold"] = BULLISH_PROBABILITY
    output["direction_down_threshold"] = BEARISH_PROBABILITY
    output["frozen_at"] = frozen_at
    output["action_window_start"] = action_start
    output["action_window_end"] = action_end
    output["forecast_contract_version"] = FORECAST_CONTRACT_VERSION
    output["target_contract_version"] = target_contract_version
    output["execution_authority"] = EXECUTION_AUTHORITY
    output["broker_orders_enabled"] = False
    output["opra_completed_through"] = (
        str(opra_freshness["completed_through"]) if opra_freshness is not None else None
    )
    order = {group: index for index, group in enumerate(MODEL_GROUPS)}
    output["__group_order"] = output["model_group"].map(order)
    output["__anchor_order"] = output["route"].map(_route_order)
    return output.sort_values(
        ["symbol", "__group_order", "__anchor_order"], kind="stable"
    ).drop(columns=["__group_order", "__anchor_order"]).reset_index(drop=True)


def _option_intents(
    forecasts: pd.DataFrame,
    *,
    candidates: pd.DataFrame,
    strategy_run: Path | None,
    action_date: date,
    stock_only: bool = False,
) -> pd.DataFrame:
    candidates = candidates.copy()
    candidates["candidate_rank"] = pd.to_numeric(
        candidates.get("candidate_rank"), errors="coerce"
    )
    latest_completed_session = _latest_candidate_session_by_symbol(candidates)
    rows: list[dict[str, object]] = []
    for forecast in forecasts.to_dict("records"):
        group = str(forecast["model_group"])
        route = str(forecast["route"])
        strategy_horizon = _strategy_horizon_for_route(group, route)
        candidate = None if stock_only else _select_option_candidate_for_route(
            candidates,
            symbol=str(forecast["symbol"]),
            horizon=strategy_horizon,
            direction=str(forecast["direction"]),
        )
        fitted = bool(
            candidate is not None
            and str(candidate.get("score_basis")) in _FITTED_OPTION_SCORE_BASES
            and _finite_or_none(candidate.get("decision_score")) is not None
        )
        planning_evidence_gate = bool(
            fitted
            and _truth(candidate.get("all_option_quotes_valid"))
            and _finite_or_none(candidate.get("max_relative_spread")) is not None
            and float(candidate.get("max_relative_spread")) <= 0.35
            and (
                str(candidate.get("score_basis"))
                == OPRA_EXECUTION_CALIBRATED_MODEL_SCORE_BASIS
                or
                (_finite_or_none(candidate.get("minimum_open_interest")) or 0.0) >= 1.0
                or (_finite_or_none(candidate.get("total_volume")) or 0.0) >= 10.0
            )
            and _candidate_uses_latest_completed_session(
                candidate,
                latest_completed_session.get(str(forecast["symbol"]).upper()),
            )
        )
        model_promoted = forecast["model_status"] == "PROMOTED"
        direction_probability = _finite_or_none(
            forecast.get("calibrated_probability")
        )
        direction_edge = (
            abs(direction_probability - 0.5)
            if direction_probability is not None
            else 0.0
        )
        option_session_route = _option_session_route_is_tradable(group, route)
        candidate_probability = (
            _finite_or_none(candidate.get("decision_score"))
            if candidate is not None
            else None
        )
        expected_return = (
            _finite_or_none(candidate.get("expected_return_on_risk"))
            if candidate is not None
            else None
        )
        expected_profit = (
            _finite_or_none(candidate.get("expected_net_profit"))
            if candidate is not None
            else None
        )
        exposure_matches = _candidate_exposure_matches_direction(
            candidate,
            direction=str(forecast["direction"]),
        )
        if stock_only:
            status = "NO_TRADE_STOCK_ONLY"
            reason = "Stock preparation only. Options research was not prepared for this Gameplan."
        elif candidate is None:
            status = "NO_TRADE_NO_FROZEN_CANDIDATE"
            reason = "No exact candidate was constructed for this frozen route."
        elif not fitted:
            status = "NO_TRADE_NO_FITTED_PROFIT_PROBABILITY"
            reason = (
                "The candidate has Scenario Coverage only; it is not an ML profit "
                "probability."
            )
        elif not planning_evidence_gate:
            status = "NO_TRADE_COMPLETED_SESSION_EVIDENCE_GATE_FAILED"
            reason = (
                str(candidate.get("pricing_missing_reason") or "")
                or "Exact option legs do not represent the latest completed session."
            )
        elif not model_promoted:
            status = "NO_TRADE_DIRECTION_MODEL_NOT_PROMOTED"
            reason = "The matching overnight direction model failed its offline gate."
        elif not option_session_route:
            status = "NO_TRADE_OPTION_MARKET_CLOSED_AT_ANCHOR"
            reason = (
                "The stock forecast remains valid, but this route has no complete "
                "listed-options execution window."
            )
        elif direction_edge < MINIMUM_OPTION_DIRECTION_EDGE:
            status = "NO_TRADE_INSUFFICIENT_DIRECTION_EDGE"
            reason = "The frozen direction probability is too close to 0.50."
        elif not exposure_matches:
            status = "NO_TRADE_EXPOSURE_DIRECTION_MISMATCH"
            reason = "The candidate's net option exposure opposes the frozen forecast."
        elif (
            candidate_probability is None
            or candidate_probability < MINIMUM_OPTION_PROFIT_PROBABILITY
        ):
            status = "NO_TRADE_PROFIT_PROBABILITY_BELOW_THRESHOLD"
            reason = (
                "Calibrated profit probability is below the frozen 0.55 paper gate."
            )
        elif (
            expected_return is None
            or expected_profit is None
            or expected_return <= 0.0
            or expected_profit <= 0.0
        ):
            status = "NO_TRADE_NONPOSITIVE_EXPECTED_RETURN"
            reason = "Modeled expected return or expected net profit is not positive."
        else:
            status = "PAPER_ENTER_ONLY_IF_SAME_LEGS_REVALIDATE"
            reason = (
                "Latest-completed-session evidence was accepted for overnight "
                "planning. The exact same legs and limit must pass a next-session "
                "quote check; otherwise skip."
            )
        rows.append(
            {
                "id": f"{action_date.isoformat()}:{forecast['symbol']}:{route}:OPTION",
                "symbol": forecast["symbol"],
                "route": route,
                "model_group": group,
                "forecast_anchor_local": forecast["forecast_anchor_local"],
                "action_anchor_local": forecast.get("action_anchor_local"),
                "direction": forecast["direction"],
                "direction_probability_up": forecast["calibrated_probability"],
                "plan_status": status,
                "reason": reason,
                "strategy_horizon": strategy_horizon,
                "strategy_name": None if candidate is None else candidate.get("strategy_name"),
                "strategy_display_name": None if candidate is None else candidate.get("strategy_display_name"),
                "candidate_key": None if candidate is None else candidate.get("candidate_key"),
                "candidate_rank": None if candidate is None else candidate.get("candidate_rank"),
                "legs_json": None if candidate is None else candidate.get("legs_json"),
                "entry_net_credit": None if candidate is None else candidate.get("entry_net_credit"),
                "entry_net_debit": None if candidate is None else candidate.get("entry_net_debit"),
                "capital_required": None if candidate is None else candidate.get("capital_required"),
                "max_profit": None if candidate is None else candidate.get("max_profit"),
                "max_loss": None if candidate is None else candidate.get("max_loss"),
                "decision_score": None if candidate is None else candidate.get("decision_score"),
                "score_basis": None if candidate is None else candidate.get("score_basis"),
                "pricing_status": None if candidate is None else candidate.get("pricing_status"),
                "pricing_source": None if candidate is None else candidate.get("pricing_source"),
                "maximum_quote_staleness_seconds": None if candidate is None else candidate.get("maximum_quote_staleness_seconds"),
                "target_window_start": forecast["target_window_start"],
                "target_window_end": forecast["target_window_end"],
                "strategy_source_run": None if stock_only or strategy_run is None else strategy_run.name,
                "execution_authority": EXECUTION_AUTHORITY,
                "broker_orders_enabled": False,
                "same_legs_revalidation_only": True,
            }
        )
    output = pd.DataFrame(rows)
    expected = forecasts["symbol"].nunique() * EXPECTED_OPTION_INTENTS_PER_SYMBOL
    if len(output) != expected:
        raise RuntimeError("Option-intent grid is incomplete")
    return output


def _select_option_candidate_for_route(
    candidates: pd.DataFrame,
    *,
    symbol: str,
    horizon: str,
    direction: str,
) -> dict[str, object] | None:
    selected = candidates.loc[
        candidates["symbol"].astype("string").str.upper().eq(symbol.upper())
        & candidates["horizon"].astype("string").str.lower().eq(horizon.lower())
    ].copy()
    if selected.empty:
        return None
    net_delta = pd.to_numeric(selected.get("net_delta"), errors="coerce")
    clean_direction = direction.strip().upper()
    if clean_direction == "BULLISH" and net_delta.gt(0.0).any():
        selected = selected.loc[net_delta.gt(0.0)].copy()
    elif clean_direction == "BEARISH" and net_delta.lt(0.0).any():
        selected = selected.loc[net_delta.lt(0.0)].copy()
    selected["__fitted"] = selected["score_basis"].isin(
        _FITTED_OPTION_SCORE_BASES
    ).astype(int)
    selected["__score"] = pd.to_numeric(
        selected.get("decision_score"), errors="coerce"
    ).fillna(float("-inf"))
    selected["__return"] = pd.to_numeric(
        selected.get("expected_return_on_risk"), errors="coerce"
    ).fillna(float("-inf"))
    selected["__rank"] = pd.to_numeric(
        selected.get("candidate_rank"), errors="coerce"
    ).fillna(float("inf"))
    row = selected.sort_values(
        ["__fitted", "__score", "__return", "__rank", "candidate_key"],
        ascending=[False, False, False, True, True],
        kind="mergesort",
    ).iloc[0]
    return row.drop(labels=["__fitted", "__score", "__return", "__rank"]).to_dict()


def _candidate_exposure_matches_direction(
    candidate: Mapping[str, object] | None,
    *,
    direction: str,
) -> bool:
    if candidate is None:
        return False
    net_delta = _finite_or_none(candidate.get("net_delta"))
    if net_delta is None:
        return False
    clean = direction.strip().upper()
    return (clean == "BULLISH" and net_delta > 0.0) or (
        clean == "BEARISH" and net_delta < 0.0
    )


def _option_session_route_is_tradable(group: str, route: str) -> bool:
    """Admit only anchors with a complete listed-options execution window."""

    if group == "1h":
        anchor = _route_anchor_hour(route)
        return anchor is not None and 8 <= anchor <= 13
    if group == "4h":
        return _route_anchor_hour(route) == 12
    return True


def _route_anchor_hour(route: str) -> int | None:
    try:
        return int(str(route).split("@", maxsplit=1)[1].split(":", maxsplit=1)[0])
    except (IndexError, TypeError, ValueError):
        return None


def _action_anchor_for_route(group: str, route: str) -> str | None:
    """Return when a frozen intraday forecast is first actionable.

    Route suffixes are forecast endpoints.  The opening-gap checkpoints are
    actionable at 04:00; later intraday checkpoints are actionable at the
    start of their one- or four-hour target window.
    """

    anchor = _route_anchor_hour(route)
    if anchor is None:
        return None
    clean_group = str(group).strip().lower()
    if clean_group == "1h":
        return f"{(4 if anchor == 4 else anchor - 1):02d}:00"
    if clean_group == "4h":
        return f"{(4 if anchor == 4 else anchor - 4):02d}:00"
    return None


def _latest_candidate_session_by_symbol(
    candidates: pd.DataFrame,
) -> dict[str, pd.Timestamp]:
    latest: dict[str, pd.Timestamp] = {}
    for row in candidates.to_dict("records"):
        symbol = str(row.get("symbol") or "").upper()
        for timestamp in _candidate_session_timestamps(row):
            if symbol not in latest or timestamp > latest[symbol]:
                latest[symbol] = timestamp
    return latest


def _candidate_uses_latest_completed_session(
    candidate: Mapping[str, object],
    expected: pd.Timestamp | None,
) -> bool:
    if expected is None:
        return False
    observed = _candidate_session_timestamps(candidate)
    return bool(observed) and all(timestamp == expected for timestamp in observed)


def _candidate_session_timestamps(
    candidate: Mapping[str, object],
) -> tuple[pd.Timestamp, ...]:
    try:
        raw = json.loads(str(candidate.get("legs_json") or "[]"))
    except (TypeError, ValueError, json.JSONDecodeError):
        return ()
    timestamps: list[pd.Timestamp] = []
    for leg in raw:
        if not isinstance(leg, Mapping) or str(leg.get("asset")).upper() != "OPTION":
            continue
        value = pd.to_datetime(
            leg.get("target_snapshot_for") or leg.get("quote_timestamp"),
            utc=True,
            errors="coerce",
        )
        if pd.isna(value):
            return ()
        timestamps.append(pd.Timestamp(value))
    return tuple(timestamps)


def _strategy_horizon_for_route(group: str, route: str) -> str:
    if group == "1h":
        return "1h"
    if group == "4h":
        return "4h"
    if group == "1w":
        return "1w"
    if route == "1d@D+1":
        return "1d"
    return route.replace("1d@D+", "1w-d")


def _evaluate_prior_gameplan(
    root: Path,
    *,
    observed_groups: Mapping[str, pd.DataFrame],
    evaluated_at: pd.Timestamp,
) -> pd.DataFrame:
    from ml.gameplan_evaluation import evaluate_saved_gameplans

    return evaluate_saved_gameplans(
        root, observed_groups=observed_groups, evaluated_at=evaluated_at,
    ).evaluations


def _verify_opra_history(
    root: Path,
    *,
    symbols: Sequence[str],
    action_date: date,
    required_completed_through: date,
) -> tuple[tuple[Path, ...], dict[str, object]]:
    state_root = (
        root
        / "market-data"
        / "databento"
        / "opra"
        / "OPRA.PILLAR"
        / "state"
        / "symbol-history"
    )
    files: list[Path] = []
    completed: list[date] = []
    verified: list[str] = []
    for symbol in symbols:
        for schema in OPRA_STRATEGY_HISTORY_SCHEMAS:
            path = state_root / symbol / f"{schema}.json"
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
                through = pd.Timestamp(str(payload["completed_through"])).date()
            except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
                raise RuntimeError(
                    f"OPRA production cursor is unreadable: {symbol}/{schema}"
                ) from exc
            if "replay_coverage" in payload:
                from datafetching.opra_replay_fallback import verify_replay_cursor_coverage
                verify_replay_cursor_coverage(
                    root, symbol=symbol, schema=schema, cursor=payload,
                )
            if through < required_completed_through:
                raise RuntimeError(
                    "OPRA history is stale for the next gameplan: "
                    f"{symbol}/{schema} completed_through={through}; "
                    f"required={required_completed_through}; "
                    f"action_date={action_date}"
                )
            files.append(path)
            completed.append(through)
            verified.append(f"{symbol}/{schema}")
    common = min(completed)
    return tuple(files), {
        "status": "CURRENT_THROUGH_PRIOR_ACTION_DAY",
        "completed_through": common.isoformat(),
        "required_completed_through": required_completed_through.isoformat(),
        "coverage_semantics": "exclusive cursor; includes every session before this date",
        "verified_cursor_count": len(verified),
        "verified_cursors": verified,
        "production_schemas": list(OPRA_STRATEGY_HISTORY_SCHEMAS),
    }


def _publish_gameplan(
    root: Path,
    *,
    run: Path,
    action_date: date,
    published_at: pd.Timestamp,
    source_loop_b: str,
    source_strategy: str | None,
) -> None:
    runs_root = (root / "ml" / "nightly-gameplan-runs").resolve()
    if run.resolve().parent != runs_root:
        raise RuntimeError("Gameplan run escapes immutable run root")
    manifest = verify_manifest(run)
    _verify_source_selection_metadata(run, manifest)
    _verify_probability_target_metadata(run, manifest)
    receipt = {
        "schema_version": GAMEPLAN_RECEIPT_VERSION,
        **({key: manifest["configuration"][key] for key in ("probability_target_contract", "gameplan_variant")}
           if "probability_target_contract" in manifest.get("configuration", {}) else {}),
        "run_path": run.relative_to(root).as_posix(),
        "run_timestamp": str(manifest["run_timestamp"]),
        "action_date": action_date.isoformat(),
        "published_at": published_at.isoformat(),
        "manifest_checksum_sha256": file_checksum(run / "manifest.json"),
        "source_loop_b_run": source_loop_b,
        "source_strategy_run": source_strategy,
        "preparation_scope": manifest.get("configuration", {}).get("preparation_scope", "STOCK_AND_OPTIONS_RESEARCH"),
        "execution_authority": EXECUTION_AUTHORITY,
        "broker_orders_enabled": False,
        "orders_placed": 0,
    }
    receipt_path = run / "receipt.json"
    _write_json_atomic(receipt_path, receipt)
    pointer = {
        "schema_version": GAMEPLAN_POINTER_VERSION,
        "current": {
            "run_path": receipt["run_path"],
            "action_date": receipt["action_date"],
            "published_at": receipt["published_at"],
            "manifest_checksum_sha256": receipt["manifest_checksum_sha256"],
            "receipt_checksum_sha256": file_checksum(receipt_path),
        },
    }
    _write_json_atomic(
        root / "ml" / "nightly-gameplan-latest" / "run.json",
        pointer,
    )
    read_current_gameplan(root)



def _verify_source_selection_metadata(run: Path, manifest: Mapping) -> None:
    """Bind the prospective feature policy across immutable publication outputs."""
    config = manifest.get("configuration", {})
    contract = config.get("source_selection_contract")
    if contract is None:
        return  # Historical publications retain their original reader contract.
    if (contract not in SOURCE_SELECTION_VERSIONS
            or config.get("target_contract_version") != STOCK_TARGET_CONTRACT_VERSION
            or config.get("source_selection", {}).get("source_selection_contract") != contract):
        raise RuntimeError("Gameplan source selection configuration is invalid")
    outputs = manifest.get("output_files", {})
    if contract == ARCHIVE_SOURCE_SELECTION_VERSION and "archive-history.json" not in outputs:
        raise RuntimeError("Archive history evidence is not manifest-bound")
    if (config.get("archive_history") is True) != (contract == ARCHIVE_SOURCE_SELECTION_VERSION):
        raise RuntimeError("Archive history configuration and feature contract disagree")
    if contract == ARCHIVE_SOURCE_SELECTION_VERSION:
        history = json.loads((run / "archive-history.json").read_text(encoding="utf-8"))
        if (any(history.get(key) != value for key, value in config["source_selection"].items())
                or history.get("orders_placed") != 0
                or set(history.get("training_cohorts", {})) != set(MODEL_GROUPS)):
            raise RuntimeError("Archive history report and source selection configuration disagree")
    required = ("gameplan.json", "model-reports.json", "forecasts.parquet",
                *(f"training-cohort-{group}.parquet" for group in MODEL_GROUPS))
    if not set(required).issubset(outputs):
        raise RuntimeError("Gameplan source selection evidence is not manifest-bound")
    plan = json.loads((run / "gameplan.json").read_text(encoding="utf-8"))
    reports = json.loads((run / "model-reports.json").read_text(encoding="utf-8"))
    if (plan.get("source_selection_contract") != contract
            or plan.get("source_selection") != config.get("source_selection")
            or any(reports.get(group, {}).get("source_selection_contract") != contract for group in MODEL_GROUPS)):
        raise RuntimeError("Gameplan model reports and source selection configuration disagree")
    for name in ("forecasts.parquet", *(f"training-cohort-{group}.parquet" for group in MODEL_GROUPS)):
        frame = pd.read_parquet(run / name)
        if (frame.empty or not set(SOURCE_SELECTION_COLUMNS).issubset(frame)
                or source_selection_contract(frame) != contract):
            raise RuntimeError("Gameplan rows lack their source selection evidence")
        if contract == ARCHIVE_SOURCE_SELECTION_VERSION:
            from ml.gameplan_archive_integration import validate_archive_feature_clocks
            validate_archive_feature_clocks(frame)


def _verify_probability_cohort(frame: pd.DataFrame, contract: str) -> None:
    required = {"observed_return", "assumed_round_trip_cost", "target", "target_raw_price_direction",
                "target_cost_adjusted_positive", "probability_target_contract", "gameplan_variant"}
    if frame.empty or not required.issubset(frame):
        raise RuntimeError("Gameplan cohort lacks separate raw-direction and cost-adjusted target evidence")
    raw = pd.to_numeric(frame.observed_return, errors="coerce")
    cost = pd.to_numeric(frame.assumed_round_trip_cost, errors="coerce")
    expected = {"target_raw_price_direction": raw.gt(0).astype(int),
                "target_cost_adjusted_positive": raw.gt(cost).astype(int)}
    expected["target"] = expected["target_raw_price_direction" if contract == RAW_DIRECTION_TARGET
                                  else "target_cost_adjusted_positive"]
    if (not np.isfinite(raw).all() or not np.isfinite(cost).all() or cost.lt(0).any()
            or probability_target_contract(frame) != contract
            or not frame.gameplan_variant.eq(probability_target_metadata(contract)["gameplan_variant"]).all()
            or any(not pd.to_numeric(frame[name], errors="coerce").eq(values).all()
                   for name, values in expected.items())):
        raise RuntimeError("Gameplan cohort labels disagree with their probability target contract")


def _verify_probability_target_metadata(run: Path, manifest: Mapping, receipt: Mapping | None = None) -> None:
    """New target semantics are bound everywhere; absent historical metadata stays legacy."""
    config = manifest.get("configuration", {})
    if "probability_target_contract" not in config:
        if "forecasts.parquet" in manifest.get("output_files", {}):
            historical = pd.read_parquet(run / "forecasts.parquet")
            if probability_target_contract(historical) != LEGACY_COST_TARGET:
                raise RuntimeError("Gameplan raw-direction rows lack manifest probability target provenance")
        return
    contract = probability_target_contract(config)
    metadata = probability_target_metadata(contract)
    if any(config.get(key) != value for key, value in metadata.items()):
        raise RuntimeError("Gameplan probability target and variant disagree")
    if receipt is not None and any(receipt.get(key) != value for key, value in metadata.items()):
        raise RuntimeError("Gameplan receipt and probability target disagree")
    if contract != RAW_DIRECTION_TARGET:
        return
    expected_target = "target_raw_price_direction"
    if manifest.get("target_column") != expected_target:
        raise RuntimeError("Gameplan manifest does not identify the raw-direction fitting target")
    outputs = manifest.get("output_files", {})
    required = ("gameplan.json", "model-reports.json", "forecasts.parquet",
                *(f"training-cohort-{group}.parquet" for group in MODEL_GROUPS))
    if not set(required).issubset(outputs):
        raise RuntimeError("Gameplan probability target evidence is not manifest-bound")
    plan = json.loads((run / "gameplan.json").read_text(encoding="utf-8"))
    reports = json.loads((run / "model-reports.json").read_text(encoding="utf-8"))
    for item in (plan, *(reports.get(group, {}) for group in MODEL_GROUPS)):
        if any(item.get(key) != value for key, value in metadata.items()):
            raise RuntimeError("Gameplan model reports and probability target disagree")
    for name in ("forecasts.parquet", *(f"training-cohort-{group}.parquet" for group in MODEL_GROUPS)):
        frame = pd.read_parquet(run / name)
        if frame.empty or any(key not in frame or not frame[key].eq(value).all() for key, value in metadata.items()):
            raise RuntimeError("Gameplan rows lack their probability target evidence")
        if name.startswith("training-cohort-"):
            _verify_probability_cohort(frame, contract)
    for group in MODEL_GROUPS:
        report = reports[group]
        name = report.get("model_file", {}).get("path")
        if name not in outputs or not (run / str(name)).resolve().is_relative_to(run.resolve()):
            raise RuntimeError("Gameplan probability model is not manifest-bound")
        payload = joblib.load(run / name)
        if any(payload.get(key) != value for key, value in metadata.items()):
            raise RuntimeError("Gameplan fitted model and probability target disagree")


def read_gameplan_run(datastore_root: Path, run_directory: Path) -> GameplanPublication:
    """Verify a saved publication without following the latest pointer."""
    root = Path(datastore_root).resolve()
    run = Path(run_directory).resolve()
    if run.parent != (root / "ml/nightly-gameplan-runs").resolve():
        raise RuntimeError("Saved Gameplan escapes immutable run root")
    manifest = verify_manifest(run)
    _verify_source_selection_metadata(run, manifest)
    receipt_path = run / "receipt.json"
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    _verify_probability_target_metadata(run, manifest, receipt)
    relative = run.relative_to(root).as_posix()
    if (
        receipt.get("schema_version") != GAMEPLAN_RECEIPT_VERSION
        or receipt.get("run_path") != relative
        or receipt.get("manifest_checksum_sha256") != file_checksum(run / "manifest.json")
        or receipt.get("execution_authority") != EXECUTION_AUTHORITY
        or receipt.get("broker_orders_enabled") is not False
        or receipt.get("orders_placed") != 0
        or str(manifest.get("run_timestamp")) != str(receipt.get("run_timestamp"))
        or manifest.get("configuration", {}).get("action_date") != receipt.get("action_date")
    ):
        raise RuntimeError("Saved Gameplan manifest and receipt disagree")
    pointer = {"schema_version": GAMEPLAN_POINTER_VERSION, "current": {
        "run_path": relative, "action_date": receipt["action_date"],
        "published_at": receipt["published_at"],
        "manifest_checksum_sha256": receipt["manifest_checksum_sha256"],
        "receipt_checksum_sha256": file_checksum(receipt_path),
    }}
    return GameplanPublication(run, manifest, receipt, pointer)


def read_current_gameplan(datastore_root: Path) -> GameplanPublication:
    root = Path(datastore_root).resolve()
    pointer_path = root / "ml" / "nightly-gameplan-latest" / "run.json"
    try:
        pointer = json.loads(pointer_path.read_text(encoding="utf-8"))
    except (OSError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"Gameplan pointer is unreadable: {pointer_path}") from exc
    current = pointer.get("current") if isinstance(pointer, Mapping) else None
    if (
        not isinstance(current, Mapping)
        or pointer.get("schema_version") != GAMEPLAN_POINTER_VERSION
    ):
        raise RuntimeError("Gameplan pointer contract is invalid")
    relative = Path(str(current.get("run_path") or ""))
    if relative.is_absolute() or not str(relative):
        raise RuntimeError("Gameplan pointer path is invalid")
    run = (root / relative).resolve()
    if run.parent != (root / "ml" / "nightly-gameplan-runs").resolve():
        raise RuntimeError("Gameplan pointer escapes immutable run root")
    manifest = verify_manifest(run)
    _verify_source_selection_metadata(run, manifest)
    receipt_path = run / "receipt.json"
    try:
        receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    except (OSError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise RuntimeError("Gameplan receipt is unreadable") from exc
    expected = {
        "run_path": relative.as_posix(),
        "action_date": receipt.get("action_date"),
        "published_at": receipt.get("published_at"),
        "manifest_checksum_sha256": file_checksum(run / "manifest.json"),
        "receipt_checksum_sha256": file_checksum(receipt_path),
    }
    _verify_probability_target_metadata(run, manifest, receipt)
    if (
        receipt.get("schema_version") != GAMEPLAN_RECEIPT_VERSION
        or dict(current) != expected
        or receipt.get("manifest_checksum_sha256")
        != file_checksum(run / "manifest.json")
        or receipt.get("execution_authority") != EXECUTION_AUTHORITY
        or receipt.get("broker_orders_enabled") is not False
        or receipt.get("orders_placed") != 0
        or str(manifest.get("run_timestamp")) != str(receipt.get("run_timestamp"))
    ):
        raise RuntimeError("Gameplan pointer, manifest, and receipt disagree")
    return GameplanPublication(run, manifest, receipt, pointer)


def _local_timestamp(value: date, hour: int) -> pd.Timestamp:
    return pd.Timestamp(
        year=value.year,
        month=value.month,
        day=value.day,
        hour=hour,
        tz=SCHEDULE_TIMEZONE,
    ).tz_convert("UTC")


def _route_order(route: object) -> int:
    value = str(route)
    if "@" not in value:
        return 0
    suffix = value.split("@", 1)[1]
    if suffix == "gap":
        return 99
    if suffix.startswith("D+"):
        return int(suffix.removeprefix("D+"))
    return int(suffix.split(":", 1)[0])


def _finite_or_none(value: object) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _truth(value: object) -> bool:
    return bool(value) if not pd.isna(value) else False


def _write_json_atomic(path: Path, payload: Mapping[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + f".tmp-{os.getpid()}")
    try:
        temporary.write_text(
            json.dumps(dict(payload), indent=2, sort_keys=True, default=str) + "\n",
            encoding="utf-8",
        )
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Train HGB and neural challengers and publish the immutable next-session "
            "04:00-17:00 PT advisory/paper gameplan."
        )
    )
    datastore = parser.add_mutually_exclusive_group()
    datastore.add_argument("--datastore", type=Path, default=None)
    datastore.add_argument(
        "--datastore-target",
        choices=tuple(DATASTORE_TARGETS),
        default="pc",
    )
    parser.add_argument("--once", action="store_true", help="Compatibility flag")
    parser.add_argument("--archive-history", action="store_true",
                        help="Use verified historical XNAS feature rows; preserves price and model quality gates")
    parser.add_argument(
        "--stock-only", action="store_true",
        help="Prepare stock forecasts with explicit no-trade options intents, without Strategy models or candidates",
    )
    parser.add_argument("--independent-stock-horizons", action="store_true",
                        help="Use observed extended-session independent-stock-targets-v1 labels and entry/exit windows")
    parser.add_argument("--stock-price-source", choices=tuple(STOCK_PRICE_SOURCES),
                        default=CANONICAL_STOCK_PRICE_SOURCE,
                        help="Explicit single-dataset price source for independent target labels and evaluation")
    parser.add_argument("--probability-target-contract", choices=(RAW_DIRECTION_TARGET, LEGACY_COST_TARGET),
                        default=None,
                        help="Frozen positive-class meaning; new independent plans default to raw price direction")
    args = parser.parse_args(argv)
    root = resolve_datastore_dir(
        root_dir=args.datastore,
        target=None if args.datastore is not None else args.datastore_target,
    )
    lock = root / ".ducketz-nightly-gameplan.lock"
    with exclusive_runtime_lock(lock, process_name="Duckets nightly gameplan"):
        try:
            run_nightly_gameplan_once(root, stock_only=args.stock_only,
                                      independent_stock_horizons=args.independent_stock_horizons,
                                      stock_price_source=args.stock_price_source,
                                      archive_history=args.archive_history,
                                      probability_target_contract=args.probability_target_contract)
        except Exception as exc:
            print(f"Nightly gameplan failed: {type(exc).__name__}: {exc}")
            return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "ACTION_END_HOUR",
    "ACTION_START_HOUR",
    "EXECUTION_AUTHORITY",
    "EXPECTED_FORECASTS_PER_SYMBOL",
    "FOUR_HOUR_ANCHORS",
    "GAMEPLAN_VERSION",
    "GameplanPublication",
    "HOURLY_ANCHORS",
    "NightlyGameplanResult",
    "read_current_gameplan",
    "run_nightly_gameplan_once",
]
