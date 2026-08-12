from __future__ import annotations

import json
import time
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from datafetching.quote_liquidity import (
    QUOTE_LIQUIDITY_CALCULATION,
    QUOTE_LIQUIDITY_CALCULATION_VERSION,
    QUOTE_LIQUIDITY_QUALITY_POLICY_VERSION,
    QUOTE_LIQUIDITY_SCHEMA_VERSION,
)
from ml.strategy_selection.candidates import (
    construct_strategy_candidates,
    evaluate_candidate_outcome,
)
from ml.strategy_selection.chain import (
    SchwabChainHistory,
    entry_chain_receipt,
    exit_chain_receipt,
)
from ml.strategy_selection.contracts import (
    BLACK_SCHOLES_CALIBRATED_MODEL_SCORE_BASIS,
    CALIBRATED_MODEL_SCORE_BASIS,
    SCENARIO_PRIOR_SCORE_BASIS,
    STRATEGY_CANDIDATE_SCHEMA_VERSION,
    STRATEGY_MODEL_POLICY_VERSION,
    STRATEGY_RANKING_POLICY_VERSION,
    StrategyModel,
    StrategySelectionPolicy,
)
from ml.option_pricing.strategy_shadow import StrategyPricingEvidenceCatalog
from ml.strategy_selection.market_state import (
    _candidate_prior,
    infer_market_state,
    score_market_state_prior,
)
from ml.strategy_selection.model import (
    PRICING_NUMERIC_FEATURES,
    fit_or_reuse_strategy_model,
    partition_strategy_outcomes,
    score_strategy_candidates,
)
from ml.strategy_selection.registry import (
    STRATEGY_REGISTRY,
    validate_strategy_registry,
)
from ml.strategy_selection.runtime import run_strategy_selection
from ml.strategy_selection.runtime import _samples_with_possible_receipts
from options.features import (
    OPTION_FEATURE_SCHEMA_VERSION,
    OPTION_FEATURE_VERSION,
    OPTION_SELECTION_POLICY_VERSION,
    OPTION_SURFACE_QUALITY_POLICY_VERSION,
)
from options.snapshot import OPTION_CHAIN_SCHEMA_VERSION


_POLICY = StrategySelectionPolicy(
    minimum_train_decisions=4,
    calibration_decisions=2,
    assessment_decisions=2,
)
_EXPECTED_STRATEGIES = {
    "long_call",
    "long_put",
    "long_straddle",
    "long_strangle",
    "covered_call",
    "buy_write",
    "protective_put",
    "collar",
    "cash_secured_put",
    "covered_strangle",
    "wheel",
    "bull_call_spread",
    "bear_put_spread",
    "bull_put_spread",
    "bear_call_spread",
    "long_call_butterfly",
    "long_put_butterfly",
    "short_call_butterfly",
    "short_put_butterfly",
    "iron_butterfly",
    "reverse_iron_butterfly",
    "long_call_condor",
    "long_put_condor",
    "iron_condor",
    "reverse_iron_condor",
    "long_call_calendar",
    "long_put_calendar",
    "bull_call_diagonal",
    "poor_mans_covered_call",
    "bear_put_diagonal",
    "double_diagonal",
    "call_ratio_backspread",
    "put_ratio_backspread",
    "stock_repair_covered_ratio",
    "box_spread",
    "reaccelerating_bull",
    "phoenix_collar",
    "twin_peak_fly",
    "crash_and_squeeze_barbell",
    "range_to_trend_relay",
}


def test_entry_chain_receipt_uses_the_causal_interval_between_clocks() -> None:
    decision = pd.Timestamp("2026-07-31T20:05:00Z")
    target_start = pd.Timestamp("2026-07-31T21:00:00Z")
    receipts = (
        (decision - pd.Timedelta(minutes=5), decision + pd.Timedelta(minutes=5)),
        (decision + pd.Timedelta(minutes=10), decision + pd.Timedelta(minutes=11)),
        (decision + pd.Timedelta(minutes=25), decision + pd.Timedelta(minutes=26)),
        (decision + pd.Timedelta(minutes=40), decision + pd.Timedelta(minutes=41)),
    )
    history = SchwabChainHistory(
        symbol="GOOG",
        contracts=pd.concat(
            [
                _contracts_for_receipt(
                    snapshot_for=snapshot_for,
                    available_at=available_at,
                    underlying=100.0,
                )
                for snapshot_for, available_at in receipts
            ],
            ignore_index=True,
        ),
        surfaces=pd.concat(
            [
                _surface_for_receipt(
                    snapshot_for=snapshot_for,
                    available_at=available_at,
                )
                for snapshot_for, available_at in receipts
            ],
            ignore_index=True,
        ),
        quotes=pd.DataFrame(),
        source_files=(),
    )

    selected = entry_chain_receipt(
        history,
        minimum_snapshot_for=decision - pd.Timedelta(minutes=5),
        information_available_at=decision,
        target_window_start=target_start,
        known_at=decision + pd.Timedelta(minutes=9),
    )
    assert selected is not None
    assert selected.surface["snapshot_for"] == decision - pd.Timedelta(minutes=5)

    earliest = entry_chain_receipt(
        history,
        minimum_snapshot_for=decision - pd.Timedelta(minutes=5),
        information_available_at=decision,
        target_window_start=target_start,
        known_at=decision + pd.Timedelta(minutes=35),
        receipt_choice="earliest",
    )
    latest = entry_chain_receipt(
        history,
        minimum_snapshot_for=decision - pd.Timedelta(minutes=5),
        information_available_at=decision,
        target_window_start=target_start,
        known_at=decision + pd.Timedelta(minutes=35),
        receipt_choice="latest",
    )

    assert earliest is not None
    assert earliest.surface["snapshot_for"] == decision - pd.Timedelta(minutes=5)
    assert earliest.available_at == decision + pd.Timedelta(minutes=5)
    assert earliest.contracts["snapshot_for"].eq(
        earliest.surface["snapshot_for"]
    ).all()
    assert latest is not None
    assert latest.surface["snapshot_for"] == decision + pd.Timedelta(minutes=25)
    assert latest.available_at == decision + pd.Timedelta(minutes=26)
    assert latest.contracts["available_at"].eq(latest.available_at).all()


def test_indexed_receipt_and_contract_lookup_matches_full_frame_scan() -> None:
    start = pd.Timestamp("2026-07-01T13:00:00Z")
    receipt_times = [start + pd.Timedelta(minutes=15 * index) for index in range(120)]
    contracts = pd.concat(
        [
            _contracts_for_receipt(
                snapshot_for=snapshot,
                available_at=snapshot + pd.Timedelta(minutes=2),
                underlying=100.0 + index / 100.0,
            )
            for index, snapshot in enumerate(receipt_times)
        ],
        ignore_index=True,
    ).sample(frac=1.0, random_state=17)
    surfaces = pd.concat(
        [
            _surface_for_receipt(
                snapshot_for=snapshot,
                available_at=snapshot + pd.Timedelta(minutes=2),
            )
            for snapshot in receipt_times
        ],
        ignore_index=True,
    ).sample(frac=1.0, random_state=23)
    history = SchwabChainHistory(
        symbol="GOOG",
        contracts=contracts,
        surfaces=surfaces,
        quotes=pd.DataFrame(),
        source_files=(),
    )

    for index in range(0, 135, 3):
        information = start + pd.Timedelta(minutes=15 * index - 4)
        target_start = information + pd.Timedelta(minutes=42)
        minimum_snapshot = information - pd.Timedelta(minutes=20)
        known_at = target_start - pd.Timedelta(minutes=1)
        for choice in ("earliest", "latest"):
            indexed = entry_chain_receipt(
                history,
                minimum_snapshot_for=minimum_snapshot,
                information_available_at=information,
                target_window_start=target_start,
                known_at=known_at,
                receipt_choice=choice,
            )
            scanned = _scanned_entry_receipt(
                history,
                minimum_snapshot=minimum_snapshot,
                information=information,
                target_start=target_start,
                known_at=known_at,
                choice=choice,
            )
            assert _receipt_identity(indexed) == scanned

        target_end = information + pd.Timedelta(minutes=10)
        indexed_exit = exit_chain_receipt(
            history,
            target_window_end=target_end,
            maximum_delay=pd.Timedelta(minutes=35),
        )
        scanned_exit = _scanned_exit_receipt(
            history,
            target_end=target_end,
            maximum_delay=pd.Timedelta(minutes=35),
        )
        assert _receipt_identity(indexed_exit) == scanned_exit


def test_history_coverage_prefilter_only_skips_scan_proven_impossible_rows() -> None:
    start = pd.Timestamp("2026-07-01T13:00:00Z")
    receipt_times = [start + pd.Timedelta(hours=index) for index in range(8)]
    history = SchwabChainHistory(
        symbol="GOOG",
        contracts=pd.concat(
            [
                _contracts_for_receipt(
                    snapshot_for=value,
                    available_at=value + pd.Timedelta(minutes=5),
                    underlying=100.0,
                )
                for value in receipt_times
            ],
            ignore_index=True,
        ),
        surfaces=pd.concat(
            [
                _surface_for_receipt(
                    snapshot_for=value,
                    available_at=value + pd.Timedelta(minutes=5),
                )
                for value in receipt_times
            ],
            ignore_index=True,
        ),
        quotes=pd.DataFrame(),
        source_files=(),
    )
    samples = pd.DataFrame(
        [
            {
                "sample_key": index,
                "symbol": "GOOG",
                "horizon": "1h",
                "bar_end_timestamp": decision,
                "information_available_at": decision,
                "target_window_start": decision + pd.Timedelta(minutes=40),
                "target_window_end": decision + pd.Timedelta(hours=1),
            }
            for index, decision in enumerate(
                pd.date_range(start - pd.Timedelta(hours=3), periods=15, freq="1h")
            )
        ]
    )
    retained, _failures = _samples_with_possible_receipts(
        samples,
        histories={"GOOG": history},
        strictly_before=None,
    )
    skipped = samples.loc[~samples["sample_key"].isin(retained["sample_key"])]
    assert not skipped.empty
    for sample in skipped.to_dict("records"):
        entry = _scanned_entry_receipt(
            history,
            minimum_snapshot=pd.Timestamp(sample["bar_end_timestamp"]),
            information=pd.Timestamp(sample["information_available_at"]),
            target_start=pd.Timestamp(sample["target_window_start"]),
            known_at=pd.Timestamp(sample["target_window_start"])
            - pd.Timedelta(nanoseconds=1),
            choice="earliest",
        )
        exit_receipt = _scanned_exit_receipt(
            history,
            target_end=pd.Timestamp(sample["target_window_end"]),
            maximum_delay=pd.Timedelta(hours=2),
        )
        assert entry is None or exit_receipt is None


def test_registry_covers_the_complete_spreads_strategy_universe() -> None:
    validate_strategy_registry()

    assert set(STRATEGY_REGISTRY) == _EXPECTED_STRATEGIES
    assert len(STRATEGY_REGISTRY) == 40
    assert not any(
        definition.risk_form == "UNLIMITED_UNCOVERED"
        for definition in STRATEGY_REGISTRY.values()
    )
    assert {
        name
        for name, definition in STRATEGY_REGISTRY.items()
        if definition.lifecycle
    } == {"wheel", "range_to_trend_relay"}


def test_exact_chain_candidate_construction_audits_every_strategy() -> None:
    sample = _sample(0)
    contracts = _contracts_for_receipt(
        snapshot_for=sample["decision_timestamp"],
        available_at=sample["decision_timestamp"] + pd.Timedelta(minutes=5),
        underlying=100.0,
    )
    surface = _surface_for_receipt(
        snapshot_for=sample["decision_timestamp"],
        available_at=sample["decision_timestamp"] + pd.Timedelta(minutes=5),
    ).iloc[0]
    quote = _quote_for_receipt(
        available_at=sample["decision_timestamp"] + pd.Timedelta(minutes=5),
        underlying=100.0,
    ).iloc[0]

    candidates, audit = construct_strategy_candidates(
        sample,
        contracts,
        surface=surface,
        stock_quote=quote,
        policy=_POLICY,
    )

    assert len(audit) == len(STRATEGY_REGISTRY)
    assert set(audit["strategy_name"]) == set(STRATEGY_REGISTRY)
    assert set(audit["construction_status"]) == {
        "CONSTRUCTED",
        "LIFECYCLE_TRACKED",
    }
    assert audit["candidate_count"].eq(4).all()
    assert len(candidates) == 4 * len(STRATEGY_REGISTRY)
    assert candidates["authorization_status"].eq("AUTHORIZED_SPREADS").all()
    assert candidates["candidate_key"].is_unique
    assert candidates["max_relative_spread"].le(
        _POLICY.maximum_relative_bid_ask_spread
    ).all()
    assert candidates["minimum_open_interest"].ge(
        _POLICY.minimum_open_interest
    ).all()
    assert candidates["surface_quality_pass"].all()
    assert candidates["all_option_quotes_valid"].all()
    assert candidates["liquidity_policy_pass"].all()
    assert candidates["risk_calculation_status"].isin(
        {
            "EXPIRATION_PAYOFF_EXACT",
            "PATH_DEPENDENT_CONSERVATIVE_ASSIGNMENT_BOUND",
        }
    ).all()

    long_call = candidates.loc[candidates["strategy_name"].eq("long_call")].iloc[0]
    legs = json.loads(long_call["legs_json"])
    assert len(legs) == 1
    assert long_call["entry_cash_flow"] == pytest.approx(
        -float(legs[0]["ask"]) * 100.0 - _POLICY.per_contract_fee
    )


def test_quality_diagnostics_do_not_suppress_usable_chain_values() -> None:
    sample = _sample(0)
    available_at = sample["decision_timestamp"] + pd.Timedelta(minutes=5)
    contracts = _contracts_for_receipt(
        snapshot_for=sample["decision_timestamp"],
        available_at=available_at,
        underlying=100.0,
    )
    contracts["quote_staleness_seconds"] = 10_000.0
    surface = _surface_for_receipt(
        snapshot_for=sample["decision_timestamp"],
        available_at=available_at,
    ).iloc[0].copy()
    surface["surface_quality_pass"] = False
    quote = _quote_for_receipt(
        available_at=available_at,
        underlying=100.0,
    ).iloc[0]

    candidates, audit = construct_strategy_candidates(
        sample,
        contracts,
        surface=surface,
        stock_quote=quote,
        policy=_POLICY,
    )

    assert len(candidates) == 4 * len(STRATEGY_REGISTRY)
    assert not candidates["surface_quality_pass"].any()
    assert not candidates["liquidity_policy_pass"].any()
    assert candidates["maximum_quote_staleness_seconds"].eq(10_000.0).all()
    assert audit["candidate_count"].eq(4).all()


def test_missing_quote_timestamps_do_not_suppress_numerically_usable_bbos() -> None:
    sample = _sample(0)
    available_at = sample["decision_timestamp"] + pd.Timedelta(minutes=5)
    contracts = _contracts_for_receipt(
        snapshot_for=sample["decision_timestamp"],
        available_at=available_at,
        underlying=100.0,
    )
    contracts["quote_timestamp"] = pd.NaT
    contracts["quote_staleness_seconds"] = np.nan
    surface = _surface_for_receipt(
        snapshot_for=sample["decision_timestamp"],
        available_at=available_at,
    ).iloc[0]

    candidates, audit = construct_strategy_candidates(
        sample,
        contracts,
        surface=surface,
        stock_quote=_quote_for_receipt(
            available_at=available_at,
            underlying=100.0,
        ).iloc[0],
        policy=_POLICY,
    )

    assert len(candidates) == 4 * len(STRATEGY_REGISTRY)
    assert audit["candidate_count"].eq(4).all()
    assert candidates["maximum_quote_staleness_seconds"].isna().all()
    assert not candidates["liquidity_policy_pass"].any()
    for legs_text in candidates["legs_json"]:
        assert "NaN" not in legs_text
        for leg in json.loads(legs_text):
            if leg["asset"] == "OPTION":
                assert leg["quote_timestamp"] is None
                assert leg["quote_staleness_seconds"] is None


def test_missing_optional_liquidity_diagnostics_remain_null() -> None:
    sample = _sample(0)
    available_at = sample["decision_timestamp"] + pd.Timedelta(minutes=5)
    contracts = _contracts_for_receipt(
        snapshot_for=sample["decision_timestamp"],
        available_at=available_at,
        underlying=100.0,
    )
    contracts[
        [
            "relative_bid_ask_spread",
            "open_interest",
            "volume",
            "delta",
            "gamma",
            "theta",
            "vega",
        ]
    ] = np.nan
    candidates, _audit = construct_strategy_candidates(
        sample,
        contracts,
        surface=_surface_for_receipt(
            snapshot_for=sample["decision_timestamp"],
            available_at=available_at,
        ).iloc[0],
        stock_quote=_quote_for_receipt(
            available_at=available_at,
            underlying=100.0,
        ).iloc[0],
        policy=_POLICY,
    )

    assert not candidates.empty
    assert candidates[
        [
            "mean_relative_spread",
            "max_relative_spread",
            "minimum_open_interest",
            "total_volume",
            "net_delta",
            "net_gamma",
            "net_theta",
            "net_vega",
        ]
    ].isna().all(axis=None)
    assert not candidates["liquidity_policy_pass"].any()
    option_leg = next(
        leg
        for leg in json.loads(candidates.iloc[0]["legs_json"])
        if leg["asset"] == "OPTION"
    )
    assert option_leg["relative_bid_ask_spread"] is None
    assert option_leg["open_interest"] is None
    assert option_leg["volume"] is None
    assert option_leg["delta"] is None
    scored = score_market_state_prior(
        candidates,
        state=infer_market_state(
            sample,
            surface=_surface_for_receipt(
                snapshot_for=sample["decision_timestamp"],
                available_at=available_at,
            ).iloc[0],
            probability_up=0.65,
        ),
        policy=_POLICY,
    )
    assert scored["decision_score"].map(np.isfinite).all()


def test_market_state_prior_scores_every_exact_candidate_without_fake_calibration() -> None:
    sample = _sample(0)
    available_at = sample["decision_timestamp"] + pd.Timedelta(minutes=5)
    contracts = _contracts_for_receipt(
        snapshot_for=sample["decision_timestamp"],
        available_at=available_at,
        underlying=100.0,
    )
    surface = _surface_for_receipt(
        snapshot_for=sample["decision_timestamp"],
        available_at=available_at,
    ).iloc[0]
    candidates, _audit = construct_strategy_candidates(
        sample,
        contracts,
        surface=surface,
        stock_quote=_quote_for_receipt(
            available_at=available_at,
            underlying=100.0,
        ).iloc[0],
        policy=_POLICY,
    )
    state = infer_market_state(sample, surface=surface, probability_up=0.70)
    scored = score_market_state_prior(candidates, state=state, policy=_POLICY)

    assert len(scored) == 4 * len(STRATEGY_REGISTRY)
    assert scored["raw_profit_probability"].between(0.0, 1.0).all()
    assert scored["calibrated_profit_probability"].isna().all()
    assert scored["expected_net_profit"].notna().all()
    assert scored["expected_return_on_risk"].notna().all()
    assert scored["decision_score"].equals(scored["raw_profit_probability"])
    assert scored["decision_score"].between(0.0, 1.0).all()
    assert scored["score_basis"].eq(SCENARIO_PRIOR_SCORE_BASIS).all()
    assert scored["schema_version"].eq(STRATEGY_CANDIDATE_SCHEMA_VERSION).all()
    assert scored["candidate_rank"].tolist() == list(range(1, len(scored) + 1))
    assert scored["model_status"].eq("PRICING_SCENARIO").all()
    assert scored["direction_probability_up"].eq(0.70).all()
    assert scored["market_expected_absolute_move"].gt(0.0).all()


def test_scenario_prior_clamps_only_floating_point_probability_residue() -> None:
    sample = _sample(0)
    state = infer_market_state(
        sample,
        surface={},
        probability_up=0.5156449255489672,
    )
    prior = _candidate_prior(
        {
            "underlying_price": 100.0,
            "net_delta": 0.0,
            "net_gamma": 0.0,
            "net_theta": 0.0,
            "max_loss": 100.0,
            "max_profit": 100.0,
            "capital_required": 100.0,
            "pricing_mode": "ACTIVE",
            "pricing_source": "BSGP",
            "pricing_leg_coverage": 1.0,
            "pricing_candidate_edge": 100.0,
            "pricing_uncertainty": 0.01,
            "legs_json": json.dumps(
                [
                    {
                        "asset": "OPTION",
                        "quantity": 1.0,
                        "multiplier": 100.0,
                        "bid": 1.0,
                        "ask": 1.0,
                    }
                ]
            ),
        },
        state=state,
        policy=_POLICY,
    )

    assert prior["probability"] == 1.0


def test_outcomes_use_conservative_exit_bbo_and_do_not_fake_lifecycle_labels() -> None:
    sample = _sample(0)
    entry_time = sample["decision_timestamp"] + pd.Timedelta(minutes=5)
    entry_contracts = _contracts_for_receipt(
        snapshot_for=sample["decision_timestamp"],
        available_at=entry_time,
        underlying=100.0,
    )
    entry_surface = _surface_for_receipt(
        snapshot_for=sample["decision_timestamp"],
        available_at=entry_time,
    ).iloc[0]
    entry_quote = _quote_for_receipt(
        available_at=entry_time,
        underlying=100.0,
    ).iloc[0]
    candidates, _ = construct_strategy_candidates(
        sample,
        entry_contracts,
        surface=entry_surface,
        stock_quote=entry_quote,
        policy=_POLICY,
    )
    exit_time = sample["target_window_end"] + pd.Timedelta(minutes=5)
    exit_contracts = _contracts_for_receipt(
        snapshot_for=sample["target_window_end"],
        available_at=exit_time,
        underlying=104.0,
    )
    exit_surface = _surface_for_receipt(
        snapshot_for=sample["target_window_end"],
        available_at=exit_time,
    ).iloc[0]
    exit_quote = _quote_for_receipt(
        available_at=exit_time,
        underlying=104.0,
    ).iloc[0]

    candidate = candidates.loc[candidates["strategy_name"].eq("long_call")].iloc[0]
    outcome = evaluate_candidate_outcome(
        candidate,
        exit_contracts,
        exit_surface=exit_surface,
        exit_stock_quote=exit_quote,
        policy=_POLICY,
    )
    leg = json.loads(candidate["legs_json"])[0]
    exit_leg = exit_contracts.loc[
        exit_contracts["contract_symbol"].eq(leg["contract_symbol"])
    ].iloc[0]
    expected = (
        float(candidate["entry_cash_flow"])
        + float(exit_leg["bid"]) * float(leg["multiplier"])
        - _POLICY.per_contract_fee
    )
    assert outcome["outcome_status"] == "COMPLETE"
    assert outcome["net_profit"] == pytest.approx(expected)

    wheel = candidates.loc[candidates["strategy_name"].eq("wheel")].iloc[0]
    wheel_outcome = evaluate_candidate_outcome(
        wheel,
        exit_contracts,
        exit_surface=exit_surface,
        exit_stock_quote=exit_quote,
        policy=_POLICY,
    )
    assert wheel_outcome["outcome_status"] == "LIFECYCLE_PATH_REQUIRED"
    assert "profitable" not in wheel_outcome


def test_strategy_partitioning_is_expanding_and_keeps_decisions_intact() -> None:
    rows: list[dict[str, object]] = []
    for index in range(10):
        start = pd.Timestamp("2026-01-01", tz="UTC") + pd.Timedelta(days=index)
        for strategy_index, strategy in enumerate(("long_call", "long_put", "iron_condor")):
            rows.append(
                {
                    "symbol": "GOOG",
                    "horizon": "1d",
                    "candidate_key": f"{strategy}|w1",
                    "strategy_name": strategy,
                    "decision_timestamp": start - pd.Timedelta(hours=1),
                    "target_window_start": start,
                    "target_window_end": start + pd.Timedelta(hours=6),
                    "outcome_status": "COMPLETE",
                    "profitable": (index + strategy_index) % 2,
                    "net_profit": 10.0 if (index + strategy_index) % 2 else -5.0,
                    "return_on_risk": 0.1 if (index + strategy_index) % 2 else -0.05,
                }
            )
    partitions = partition_strategy_outcomes(pd.DataFrame(rows), policy=_POLICY)

    assert partitions.train_decisions == 6
    assert partitions.calibration_decisions == 2
    assert partitions.assessment_decisions == 2
    for frame in (partitions.train, partitions.calibration, partitions.assessment):
        assert frame.groupby("target_window_start").size().eq(3).all()
    assert partitions.train["target_window_end"].max() < partitions.calibration[
        "target_window_start"
    ].min()
    assert partitions.calibration["target_window_end"].max() < partitions.assessment[
        "target_window_start"
    ].min()


def test_strategy_partition_natural_key_accepts_two_symbols_and_keeps_shared_time_cluster() -> None:
    rows: list[dict[str, object]] = []
    for index in range(10):
        start = pd.Timestamp("2026-02-01", tz="UTC") + pd.Timedelta(days=index)
        for symbol in ("GOOG", "NVDA"):
            rows.append(
                {
                    "symbol": symbol,
                    "horizon": "1d",
                    "candidate_key": "long_call|w1",
                    "strategy_name": "long_call",
                    "decision_timestamp": start - pd.Timedelta(hours=1),
                    "target_window_start": start,
                    "target_window_end": start + pd.Timedelta(hours=6),
                    "outcome_status": "COMPLETE",
                    "profitable": int((index + (symbol == "NVDA")) % 2 == 0),
                    "net_profit": 10.0,
                    "return_on_risk": 0.1,
                }
            )

    partitions = partition_strategy_outcomes(pd.DataFrame(rows), policy=_POLICY)

    assert partitions.train_decisions == 6
    assert partitions.calibration_decisions == 2
    assert partitions.assessment_decisions == 2
    for frame in (partitions.train, partitions.calibration, partitions.assessment):
        assert frame.groupby("target_window_start").size().eq(2).all()
        assert set(frame["symbol"]) == {"GOOG", "NVDA"}


def test_strategy_partition_natural_key_rejects_exact_duplicate() -> None:
    row = {
        "symbol": "GOOG",
        "horizon": "1d",
        "candidate_key": "long_call|w1",
        "strategy_name": "long_call",
        "decision_timestamp": pd.Timestamp("2026-02-01T15:00:00Z"),
        "target_window_start": pd.Timestamp("2026-02-01T16:00:00Z"),
        "target_window_end": pd.Timestamp("2026-02-01T22:00:00Z"),
        "outcome_status": "COMPLETE",
        "profitable": 1,
        "net_profit": 10.0,
        "return_on_risk": 0.1,
    }

    with pytest.raises(ValueError, match="duplicate decision candidates"):
        partition_strategy_outcomes(pd.DataFrame([row, dict(row)]), policy=_POLICY)


def test_strategy_model_fits_only_training_and_calibration_partitions(
    tmp_path: Path,
) -> None:
    outcomes = _model_outcomes()
    partitions = partition_strategy_outcomes(outcomes, policy=_POLICY)

    model = fit_or_reuse_strategy_model(
        tmp_path,
        horizon="1d",
        partitions=partitions,
        policy=_POLICY,
        input_files=(),
        trained_at=pd.Timestamp("2026-08-01T12:00:00Z"),
    )

    evidence = model.offline_evaluation
    assert set(PRICING_NUMERIC_FEATURES).issubset(model.numeric_features)
    assert "pricing_source" in model.categorical_features
    assert evidence["assessment_used_for_training"] is False
    assert evidence["assessment_used_for_calibration"] is False
    assert evidence["assessment_used_for_ranking_policy_selection"] is False
    assert evidence["real_lockbox_used"] is False
    assert evidence["ranking_rule"] == (
        "highest_calibrated_probability_then_expected_return_on_risk_"
        "then_candidate_key_per_decision"
    )
    assert evidence["raw_model"]["log_loss"] >= 0.0
    assert evidence["calibrated_model"]["brier_score"] >= 0.0
    assert evidence["expected_return_model"]["mean_absolute_error"] >= 0.0
    assert evidence["assessment_decisions"] == _POLICY.assessment_decisions
    policies = evidence["ranking_policy_assessment"]
    assert set(policies) == {
        "probability_first",
        "expected_return_first_benchmark",
    }
    assert policies["probability_first"]["role"] == "ACTIVE"
    assert policies["expected_return_first_benchmark"]["role"] == "BENCHMARK"
    for policy_evidence in policies.values():
        assert policy_evidence["decision_count"] == _POLICY.assessment_decisions
        assert 0.0 <= policy_evidence["top_candidate_profitable_rate"] <= 1.0
        assert np.isfinite(policy_evidence["mean_realized_return_on_risk"])
        assert np.isfinite(policy_evidence["total_net_profit"])
        assert policy_evidence["probability_calibration"]["log_loss"] >= 0.0
        assert policy_evidence["probability_calibration"]["brier_score"] >= 0.0
    manifest = json.loads(
        (model.artifact_directory / "manifest.json").read_text(encoding="utf-8")
    )
    assert manifest["model_policy_version"] == STRATEGY_MODEL_POLICY_VERSION
    assert manifest["ranking_policy_version"] == STRATEGY_RANKING_POLICY_VERSION
    assert manifest["assessment_policy_selection"] == "fixed_before_assessment"
    assert manifest["preprocessing_policy_version"] == (
        "training-quantiles-0.25-99.75-v1"
    )

    scoring_candidates = partitions.assessment.head(6).copy()
    scoring_candidates[
        [
            "mean_relative_spread",
            "max_relative_spread",
            "minimum_open_interest",
            "total_volume",
            "maximum_quote_staleness_seconds",
        ]
    ] = np.nan
    scored = score_strategy_candidates(model, scoring_candidates)
    assert scored["raw_profit_probability"].between(0.0, 1.0).all()
    assert scored["calibrated_profit_probability"].between(0.0, 1.0).all()
    assert scored["direction_probability_up"].eq(0.70).all()
    assert scored["decision_score"].equals(
        scored["calibrated_profit_probability"]
    )
    assert scored["decision_score"].between(0.0, 1.0).all()
    assert scored["score_basis"].eq(CALIBRATED_MODEL_SCORE_BASIS).all()
    assert scored["candidate_rank"].tolist() == list(range(1, len(scored) + 1))
    assert "probability_threshold" not in scored


def test_fitted_ranking_is_probability_first_and_stable() -> None:
    model = StrategyModel(
        horizon="1d",
        estimator=_ColumnProbabilityEstimator(),
        return_estimator=_ColumnReturnEstimator(),
        calibrator=_IdentityProbabilityCalibrator(),
        numeric_features=("test_probability", "test_return_residual"),
        categorical_features=(),
        artifact_directory=Path("fixture-model"),
        offline_evaluation={},
    )
    candidates = pd.DataFrame(
        [
            _score_candidate("probability-first", probability=0.82, expected_return=0.04),
            _score_candidate("lottery", probability=0.21, expected_return=0.90),
            _score_candidate("tie-b", probability=0.60, expected_return=0.10),
            _score_candidate("tie-a", probability=0.60, expected_return=0.10),
        ]
    )

    ranked = score_strategy_candidates(model, candidates)
    reranked = score_strategy_candidates(
        model,
        candidates.sample(frac=1.0, random_state=19).reset_index(drop=True),
    )

    expected = ["probability-first", "tie-a", "tie-b", "lottery"]
    assert ranked["candidate_key"].tolist() == expected
    assert reranked["candidate_key"].tolist() == expected
    assert ranked.loc[0, "expected_return_on_risk"] < ranked.loc[3, "expected_return_on_risk"]
    assert ranked["candidate_rank"].tolist() == [1, 2, 3, 4]
    assert ranked["decision_score"].equals(ranked["calibrated_profit_probability"])
    assert ranked["decision_score"].between(0.0, 1.0).all()


def test_strategy_runtime_refuses_any_real_lockbox_cluster(tmp_path: Path) -> None:
    sample = pd.DataFrame([_sample(0)])
    sample["label_status"] = "COMPLETE"
    prediction = _prediction_for_sample(_sample(0))

    with pytest.raises(RuntimeError, match="lockbox"):
        run_strategy_selection(
            tmp_path,
            samples=sample,
            predictions=pd.DataFrame([prediction]),
            forbidden_target_starts={"1d": [sample.loc[0, "target_window_start"]]},
            run_timestamp=pd.Timestamp("2026-08-01T12:00:00Z"),
            input_available_at=pd.Timestamp("2026-08-01T12:00:00Z"),
            policy=_POLICY,
        )


def test_historical_exit_receipt_cannot_cross_real_lockbox_boundary() -> None:
    target_end = pd.Timestamp("2026-07-02T21:00:00Z")
    available_at = target_end + pd.Timedelta(minutes=5)
    contracts = _contracts_for_receipt(
        snapshot_for=target_end,
        available_at=available_at,
        underlying=101.0,
    )
    contracts["__surface_quality"] = True
    surfaces = _surface_for_receipt(
        snapshot_for=target_end,
        available_at=available_at,
    )
    history = SchwabChainHistory(
        symbol="GOOG",
        contracts=contracts,
        surfaces=surfaces,
        quotes=pd.DataFrame(),
        source_files=(),
    )

    assert exit_chain_receipt(
        history,
        target_window_end=target_end,
        maximum_delay=pd.Timedelta(hours=2),
        strictly_before=target_end + pd.Timedelta(minutes=1),
    ) is None


def test_runtime_publishes_prior_rank_with_missing_quote_age_diagnostics(
    tmp_path: Path,
) -> None:
    live = _sample(0)
    live["label_status"] = "PENDING"
    _write_chain_history(tmp_path, [live])
    earlier_snapshot = live["decision_timestamp"] + pd.Timedelta(minutes=10)
    earlier_available = live["decision_timestamp"] + pd.Timedelta(minutes=11)
    latest_available = live["decision_timestamp"] + pd.Timedelta(minutes=16)
    contracts_path = (
        tmp_path
        / "stocks"
        / "GOOG"
        / "options"
        / "chains"
        / "schwab"
        / "normalized"
        / "2026-07.parquet"
    )
    contracts = pd.concat(
        [
            _contracts_for_receipt(
                snapshot_for=earlier_snapshot,
                available_at=earlier_available,
                underlying=99.5,
            ),
            pd.read_parquet(contracts_path),
        ],
        ignore_index=True,
    )
    contracts["quote_timestamp"] = contracts["available_at"] - pd.Timedelta(
        seconds=5
    )
    latest_contracts = contracts["available_at"].eq(latest_available)
    contracts.loc[latest_contracts, "quote_timestamp"] = pd.NaT
    contracts.loc[latest_contracts, "quote_staleness_seconds"] = np.nan
    contracts.to_parquet(contracts_path, index=False)
    surface_path = (
        tmp_path
        / "stocks"
        / "GOOG"
        / "options"
        / "features"
        / "option-quality"
        / "schwab"
        / "2026-07.parquet"
    )
    pd.concat(
        [
            _surface_for_receipt(
                snapshot_for=earlier_snapshot,
                available_at=earlier_available,
            ),
            pd.read_parquet(surface_path),
        ],
        ignore_index=True,
    ).to_parquet(surface_path, index=False)
    quote_path = (
        tmp_path
        / "stocks"
        / "GOOG"
        / "quotes"
        / "features"
        / "quote-liquidity"
        / "schwab"
        / "2026-07.parquet"
    )
    pd.concat(
        [
            _quote_for_receipt(
                available_at=earlier_available,
                underlying=99.5,
            ),
            pd.read_parquet(quote_path),
        ],
        ignore_index=True,
    ).to_parquet(quote_path, index=False)

    full_rebuild_started = time.perf_counter()
    result = run_strategy_selection(
        tmp_path,
        samples=pd.DataFrame([live]),
        predictions=pd.DataFrame([_prediction_for_sample(live)]),
        forbidden_target_starts={
            "1d": [live["target_window_start"] + pd.Timedelta(days=100)]
        },
        run_timestamp=live["decision_timestamp"] + pd.Timedelta(minutes=20),
        input_available_at=(
            live["decision_timestamp"] + pd.Timedelta(minutes=20)
        ),
        policy=_POLICY,
    )

    assert result.model_reports["1d"]["status"] == "MODEL_NOT_FIT"
    assert len(result.candidates) == 4 * len(STRATEGY_REGISTRY)
    assert result.candidates["model_status"].eq("PRICING_SCENARIO").all()
    assert result.candidates["raw_profit_probability"].notna().all()
    assert result.candidates["calibrated_profit_probability"].isna().all()
    assert result.candidates["expected_return_on_risk"].notna().all()
    assert result.candidates["decision_score"].equals(
        result.candidates["raw_profit_probability"]
    )
    assert result.candidates["score_basis"].eq(SCENARIO_PRIOR_SCORE_BASIS).all()
    assert result.candidates["maximum_quote_staleness_seconds"].isna().all()
    assert not result.candidates["liquidity_policy_pass"].any()
    assert result.candidates["entry_available_at"].eq(latest_available).all()
    assert result.candidates["candidate_rank"].tolist() == list(
        range(1, len(result.candidates) + 1)
    )
    assert all(
        leg["quote_staleness_seconds"] is None
        for legs_text in result.candidates["legs_json"]
        for leg in json.loads(legs_text)
        if leg["asset"] == "OPTION"
    )


def test_offline_strategy_runtime_builds_trains_and_ranks_from_schwab_receipts(
    tmp_path: Path,
) -> None:
    samples = [_sample(index) for index in range(10)]
    live = _sample(10)
    for sample in samples:
        sample["label_status"] = "COMPLETE"
    live["label_status"] = "PENDING"
    _write_chain_history(tmp_path, [*samples, live])
    pricing_catalog = _pricing_catalog_for_samples([*samples, live])

    full_rebuild_started = time.perf_counter()
    result = run_strategy_selection(
        tmp_path,
        samples=pd.DataFrame([*samples, live]),
        predictions=pd.DataFrame([_prediction_for_sample(live)]),
        forbidden_target_starts={
            "1d": [live["target_window_start"] + pd.Timedelta(days=100)]
        },
        run_timestamp=live["decision_timestamp"] + pd.Timedelta(minutes=20),
        input_available_at=(
            live["decision_timestamp"] + pd.Timedelta(minutes=20)
        ),
        policy=_POLICY,
        pricing_mode="active",
        pricing_catalog=pricing_catalog,
    )

    full_rebuild_seconds = time.perf_counter() - full_rebuild_started
    assert result.models_trained == 1
    assert result.models_reused == 0
    assert result.model_reports["1d"]["status"] == "MODEL_FIT"
    assert result.model_reports["1d"]["real_lockbox_used"] is False
    assert len(result.audit) == len(STRATEGY_REGISTRY)
    assert set(result.audit["strategy_name"]) == set(STRATEGY_REGISTRY)
    assert len(result.candidates) == 4 * len(STRATEGY_REGISTRY)
    assert result.candidates["candidate_rank"].tolist() == list(
        range(1, len(result.candidates) + 1)
    )
    assert result.candidates["model_status"].eq("MODEL_FIT").all()
    assert result.candidates["decision_score"].equals(
        result.candidates["calibrated_profit_probability"]
    )
    assert result.candidates["score_basis"].eq(
        BLACK_SCHOLES_CALIBRATED_MODEL_SCORE_BASIS
    ).all()
    assert result.candidates["pricing_mode"].eq("ACTIVE").all()
    assert result.candidates["pricing_status"].eq(
        "Black-Scholes fallback"
    ).all()
    assert result.candidates["pricing_leg_coverage"].eq(1.0).all()
    assert "recommendation_action" not in result.candidates
    assert result.source_files
    assert full_rebuild_seconds < 300.0

    incremental_started = time.perf_counter()
    incremental = run_strategy_selection(
        tmp_path,
        samples=pd.DataFrame([*samples, live]),
        predictions=pd.DataFrame([_prediction_for_sample(live)]),
        forbidden_target_starts={
            "1d": [live["target_window_start"] + pd.Timedelta(days=100)]
        },
        run_timestamp=live["decision_timestamp"] + pd.Timedelta(minutes=21),
        input_available_at=live["decision_timestamp"] + pd.Timedelta(minutes=21),
        policy=_POLICY,
        pricing_mode="active",
        pricing_catalog=pricing_catalog,
    )
    incremental_seconds = time.perf_counter() - incremental_started
    assert incremental.models_trained == 0
    assert incremental.models_reused == 1
    assert (
        incremental.model_reports["1d"]["incremental_outcome_cache_hits"]
        == len(samples)
    )
    assert incremental.model_reports["1d"]["incremental_outcome_cache_misses"] == 0
    assert incremental_seconds < 60.0
    print(
        json.dumps(
            {
                "fixture": "temporary-datastore-active-pricing-strategy",
                "full_rebuild_seconds": full_rebuild_seconds,
                "historical_observations": len(samples),
                "incremental_seconds": incremental_seconds,
                "live_candidate_rows": len(result.candidates),
            },
            sort_keys=True,
        )
    )


def _sample(index: int) -> dict[str, object]:
    decision = pd.Timestamp("2026-07-01T15:00:00Z") + pd.Timedelta(days=index)
    return {
        "symbol": "GOOG",
        "horizon": "1d",
        "bar_end_timestamp": decision - pd.Timedelta(minutes=5),
        "decision_timestamp": decision,
        "information_available_at": decision,
        "target_window_start": decision + pd.Timedelta(minutes=30),
        "target_window_end": decision + pd.Timedelta(hours=6),
        "label_status": "PENDING",
        "previous_period_direction": float(1 if index % 2 else 0),
        "opt__iv_minus_realized": 0.05 + index * 0.001,
        "opt__atm_move_richness": 1.0 + (index % 3) * 0.1,
        "technical__close_vs_sma_20": (-1.0 if index % 2 else 1.0) * 0.02,
        "mr__atr_percent": 2.0,
        "mr__trend_score": 65.0,
        "mr__regime_strength": 55.0,
        "mr__range_position": 0.65,
        "bp__breakout_strength_score": 40.0,
        "bp__range_contraction_score": 35.0,
    }


def _prediction_for_sample(sample: dict[str, object]) -> dict[str, object]:
    return {
        "symbol": sample["symbol"],
        "horizon": sample["horizon"],
        "decision_timestamp": sample["decision_timestamp"],
        "target_window_start": sample["target_window_start"],
        "target_window_end": sample["target_window_end"],
        "prediction_created_at": pd.Timestamp(sample["decision_timestamp"])
        + pd.Timedelta(minutes=10),
        "prediction_mode": "LIVE",
        "prediction_status": "PREDICTED",
        "calibrated_probability": 0.65,
    }


def _contracts_for_receipt(
    *,
    snapshot_for: pd.Timestamp,
    available_at: pd.Timestamp,
    underlying: float,
) -> pd.DataFrame:
    rows: list[dict[str, object]] = []
    expirations = (
        pd.Timestamp("2026-09-18", tz="UTC"),
        pd.Timestamp("2026-10-16", tz="UTC"),
        pd.Timestamp("2026-12-18", tz="UTC"),
    )
    for expiration in expirations:
        for strike in range(70, 131, 5):
            for call_put in ("CALL", "PUT"):
                intrinsic = max(
                    underlying - strike
                    if call_put == "CALL"
                    else strike - underlying,
                    0.0,
                )
                mid = intrinsic + 2.5 + abs(strike - underlying) * 0.01
                bid = max(mid - 0.05, 0.01)
                ask = mid + 0.05
                rows.append(
                    {
                        "symbol": "GOOG",
                        "snapshot_for": snapshot_for,
                        "available_at": available_at,
                        "contract_symbol": (
                            f"GOOG-{expiration.date()}-{call_put}-{strike}"
                        ),
                        "call_put": call_put,
                        "expiration_date": expiration,
                        "strike": float(strike),
                        "underlying_price": underlying,
                        "bid": bid,
                        "ask": ask,
                        "open_interest": 500.0,
                        "volume": 100.0,
                        "delta": 0.50 if call_put == "CALL" else -0.50,
                        "gamma": 0.02,
                        "theta": -0.03,
                        "vega": 0.10,
                        "multiplier": 100.0,
                        "mini": False,
                        "non_standard": False,
                        "quote_valid": True,
                        "relative_bid_ask_spread": (ask - bid) / mid,
                        "quote_staleness_seconds": 5.0,
                        "quote_timestamp": available_at - pd.Timedelta(seconds=1),
                        "schema_version": OPTION_CHAIN_SCHEMA_VERSION,
                    }
                )
    return pd.DataFrame(rows)


def _surface_for_receipt(
    *,
    snapshot_for: pd.Timestamp,
    available_at: pd.Timestamp,
) -> pd.DataFrame:
    return pd.DataFrame(
        [
            {
                "symbol": "GOOG",
                "snapshot_for": snapshot_for,
                "available_at": available_at,
                "surface_quality_pass": True,
                "atm_days_to_expiration": 30.0,
                "atm_straddle_implied_move": 0.08,
                "realized_expected_absolute_move_atm_horizon": 0.07,
                "realized_volatility_20d": 0.30,
                "surface_quality_policy_version": (
                    OPTION_SURFACE_QUALITY_POLICY_VERSION
                ),
                "selection_policy_version": OPTION_SELECTION_POLICY_VERSION,
                "calculation_version": OPTION_FEATURE_VERSION,
                "schema_version": OPTION_FEATURE_SCHEMA_VERSION,
            }
        ]
    )


def _quote_for_receipt(
    *,
    available_at: pd.Timestamp,
    underlying: float,
) -> pd.DataFrame:
    return pd.DataFrame(
        [
            {
                "symbol": "GOOG",
                "source": "schwab",
                "quote_event_at": available_at - pd.Timedelta(seconds=1),
                "fetched_at": available_at,
                "available_at": available_at,
                "calculation": QUOTE_LIQUIDITY_CALCULATION,
                "calculation_version": QUOTE_LIQUIDITY_CALCULATION_VERSION,
                "schema_version": QUOTE_LIQUIDITY_SCHEMA_VERSION,
                "quality_policy_version": QUOTE_LIQUIDITY_QUALITY_POLICY_VERSION,
                "bid": underlying - 0.05,
                "ask": underlying + 0.05,
                "mid": underlying,
                "relative_bid_ask_spread": 0.001,
                "quote_staleness_seconds": 1.0,
                "quote_quality_pass": True,
            }
        ]
    )


def _write_chain_history(
    root: Path,
    samples: list[dict[str, object]],
) -> None:
    contracts: list[pd.DataFrame] = []
    surfaces: list[pd.DataFrame] = []
    quotes: list[pd.DataFrame] = []
    for index, sample in enumerate(samples):
        decision = pd.Timestamp(sample["decision_timestamp"])
        entry_snapshot = decision + pd.Timedelta(minutes=15)
        entry_at = decision + pd.Timedelta(minutes=16)
        entry_underlying = 100.0 + index * 0.5
        contracts.append(
            _contracts_for_receipt(
                snapshot_for=entry_snapshot,
                available_at=entry_at,
                underlying=entry_underlying,
            )
        )
        surfaces.append(
            _surface_for_receipt(
                snapshot_for=entry_snapshot,
                available_at=entry_at,
            )
        )
        quotes.append(
            _quote_for_receipt(
                available_at=entry_at,
                underlying=entry_underlying,
            )
        )
        if sample["label_status"] != "COMPLETE":
            continue
        exit_at = pd.Timestamp(sample["target_window_end"]) + pd.Timedelta(minutes=5)
        exit_underlying = entry_underlying + (3.0 if index % 2 else -3.0)
        contracts.append(
            _contracts_for_receipt(
                snapshot_for=pd.Timestamp(sample["target_window_end"]),
                available_at=exit_at,
                underlying=exit_underlying,
            )
        )
        surfaces.append(
            _surface_for_receipt(
                snapshot_for=pd.Timestamp(sample["target_window_end"]),
                available_at=exit_at,
            )
        )
        quotes.append(
            _quote_for_receipt(
                available_at=exit_at,
                underlying=exit_underlying,
            )
        )

    stock_root = root / "stocks" / "GOOG"
    contract_path = (
        stock_root
        / "options"
        / "chains"
        / "schwab"
        / "normalized"
        / "2026-07.parquet"
    )
    surface_path = (
        stock_root
        / "options"
        / "features"
        / "option-quality"
        / "schwab"
        / "2026-07.parquet"
    )
    quote_path = (
        stock_root
        / "quotes"
        / "features"
        / "quote-liquidity"
        / "schwab"
        / "2026-07.parquet"
    )
    for path in (contract_path, surface_path, quote_path):
        path.parent.mkdir(parents=True, exist_ok=True)
    pd.concat(contracts, ignore_index=True).to_parquet(contract_path, index=False)
    pd.concat(surfaces, ignore_index=True).to_parquet(surface_path, index=False)
    pd.concat(quotes, ignore_index=True).to_parquet(quote_path, index=False)


def _pricing_catalog_for_samples(
    samples: list[dict[str, object]],
) -> StrategyPricingEvidenceCatalog:
    rows: list[dict[str, object]] = []
    for index, sample in enumerate(samples):
        target = pd.Timestamp(sample["decision_timestamp"]) + pd.Timedelta(minutes=15)
        available_at = target + pd.Timedelta(seconds=30)
        underlying = 100.0 + index * 0.5
        source = "BLACK_SCHOLES"
        contracts = _contracts_for_receipt(
            snapshot_for=target,
            available_at=target + pd.Timedelta(minutes=1),
            underlying=underlying,
        )
        for contract in contracts.to_dict("records"):
            fair = (float(contract["bid"]) + float(contract["ask"])) / 2.0
            rows.append(
                {
                    "symbol": contract["symbol"],
                    "target_snapshot_for": target,
                    "contract_symbol": contract["contract_symbol"],
                    "call_put": contract["call_put"],
                    "expiration_date": contract["expiration_date"],
                    "strike": contract["strike"],
                    "multiplier": contract["multiplier"],
                    "underlying_price": underlying,
                    "source_snapshot_for": target - pd.Timedelta(minutes=15),
                    "source_available_at": target - pd.Timedelta(minutes=14),
                    "prediction_created_at": available_at,
                    "prediction_available_at": available_at,
                    "model_published_at": pd.NaT,
                    "fair_value": fair,
                    "fair_value_95_lower": max(0.0, fair - 1.0),
                    "fair_value_95_upper": fair + 1.0,
                    "predictive_standard_deviation": 0.50,
                    "residual_shrinkage": 0.0,
                    "pricing_source": source,
                    "pricing_evidence_status": "BASELINE_COPIED",
                    "input_staleness_seconds": 5.0,
                    "evidence_lane": (
                        "OFFLINE_SCHWAB_BOOTSTRAP"
                        if sample["label_status"] == "COMPLETE"
                        else "LIVE"
                    ),
                }
            )
    return StrategyPricingEvidenceCatalog(pd.DataFrame(rows), ())


def _model_outcomes() -> pd.DataFrame:
    rows: list[dict[str, object]] = []
    strategies = ("long_call", "long_put", "iron_condor")
    for index in range(10):
        sample = _sample(index)
        for strategy_index, strategy in enumerate(strategies):
            profitable = int((index + strategy_index) % 2 == 0)
            rows.append(
                {
                    **sample,
                    "candidate_key": f"{strategy}|w1",
                    "strategy_name": strategy,
                    "strategy_family": "TEST",
                    "risk_form": "DEFINED_RISK",
                    "expiration_structure": "SINGLE",
                    "stock_requirement": "NONE",
                    "cash_requirement": "NORMAL_BUYING_POWER",
                    "outcome_status": "COMPLETE",
                    "profitable": profitable,
                    "net_profit": 25.0 if profitable else -15.0,
                    "return_on_risk": 0.10 if profitable else -0.06,
                    "underlying_price": 100.0 + index,
                    "width_steps": 1,
                    "leg_count": strategy_index + 1,
                    "entry_net_credit": 1.0 if strategy == "iron_condor" else 0.0,
                    "entry_net_debit": 0.0 if strategy == "iron_condor" else 2.0,
                    "max_profit": 100.0,
                    "max_loss": 200.0,
                    "capital_required": 200.0,
                    "net_delta": 50.0 - strategy_index * 50.0,
                    "net_gamma": 1.0,
                    "net_theta": -1.0,
                    "net_vega": 2.0,
                    "mean_relative_spread": 0.02,
                    "max_relative_spread": 0.03,
                    "minimum_open_interest": 100.0,
                    "total_volume": 50.0,
                    "entry_debit_to_underlying": 0.02,
                    "max_loss_to_underlying": 0.02,
                    "net_delta_per_share": 0.5 - strategy_index * 0.5,
                    "market_expected_absolute_move": 0.04,
                    "market_expected_realized_volatility": 0.30,
                    "market_uncertainty": 0.80,
                    "market_trend_persistence": 0.60,
                    "market_mean_reversion_tendency": 0.40,
                    "strategy_prior__profit_probability": 0.55,
                    "strategy_prior__expected_return_on_risk": 0.01,
                    "direction_probability_up": 0.70,
                    "direction_alignment": (
                        np.sign(50.0 - strategy_index * 50.0) * 0.40
                    ),
                    "pricing_leg_coverage": 1.0,
                    "pricing_candidate_edge": 5.0 + strategy_index,
                    "pricing_conservative_edge": -2.0,
                    "pricing_edge_to_friction": 0.5,
                    "pricing_uncertainty": 10.0,
                    "pricing_probability_favorable": 0.65,
                    "pricing_relative_edge": 0.0005,
                    "pricing_model_age_seconds": 60.0,
                    "pricing_residual_shrinkage": 0.75,
                    "pricing_source": "BSGP",
                }
            )
    return pd.DataFrame(rows)


def _score_candidate(
    candidate_key: str,
    *,
    probability: float,
    expected_return: float,
) -> dict[str, object]:
    return {
        "candidate_key": candidate_key,
        "test_probability": probability,
        "test_return_residual": expected_return,
        "strategy_prior__expected_return_on_risk": 0.0,
        "capital_required": 100.0,
        "max_loss": 100.0,
        "max_profit": 1_000.0,
    }


class _ColumnProbabilityEstimator:
    @staticmethod
    def predict_proba(frame: pd.DataFrame) -> np.ndarray:
        probability = frame["test_probability"].to_numpy(dtype=float)
        return np.column_stack((1.0 - probability, probability))


class _ColumnReturnEstimator:
    @staticmethod
    def predict(frame: pd.DataFrame) -> np.ndarray:
        return frame["test_return_residual"].to_numpy(dtype=float)


class _IdentityProbabilityCalibrator:
    @staticmethod
    def predict(probability: np.ndarray) -> np.ndarray:
        return np.asarray(probability, dtype=float)


def _scanned_entry_receipt(
    history: SchwabChainHistory,
    *,
    minimum_snapshot: pd.Timestamp,
    information: pd.Timestamp,
    target_start: pd.Timestamp,
    known_at: pd.Timestamp,
    choice: str,
) -> tuple[object, ...] | None:
    cutoff = min(known_at, target_start - pd.Timedelta(nanoseconds=1))
    frame = history.surfaces
    eligible = frame.loc[
        frame["available_at"].ge(information)
        & frame["available_at"].le(cutoff)
        & frame["snapshot_for"].ge(minimum_snapshot)
        & frame["snapshot_for"].le(cutoff)
    ].sort_values(["available_at", "snapshot_for"], kind="mergesort")
    if eligible.empty:
        return None
    surface = eligible.iloc[0 if choice == "earliest" else -1]
    return _scanned_identity(history, surface)


def _scanned_exit_receipt(
    history: SchwabChainHistory,
    *,
    target_end: pd.Timestamp,
    maximum_delay: pd.Timedelta,
) -> tuple[object, ...] | None:
    upper = target_end + maximum_delay
    frame = history.surfaces
    eligible = frame.loc[
        frame["available_at"].ge(target_end)
        & frame["available_at"].le(upper)
        & frame["snapshot_for"].ge(target_end)
        & frame["snapshot_for"].le(upper)
    ].sort_values(["available_at", "snapshot_for"], kind="mergesort")
    if eligible.empty:
        return None
    return _scanned_identity(history, eligible.iloc[0])


def _scanned_identity(
    history: SchwabChainHistory,
    surface: pd.Series,
) -> tuple[object, ...]:
    contracts = history.contracts.loc[
        history.contracts["symbol"].astype("string").str.upper().eq(
            str(surface["symbol"]).upper()
        )
        & history.contracts["snapshot_for"].eq(surface["snapshot_for"])
        & history.contracts["available_at"].eq(surface["available_at"])
    ]
    return (
        pd.Timestamp(surface["snapshot_for"]),
        pd.Timestamp(surface["available_at"]),
        tuple(sorted(contracts["contract_symbol"].astype(str))),
    )


def _receipt_identity(receipt: object | None) -> tuple[object, ...] | None:
    if receipt is None:
        return None
    return (
        pd.Timestamp(receipt.surface["snapshot_for"]),
        pd.Timestamp(receipt.surface["available_at"]),
        tuple(sorted(receipt.contracts["contract_symbol"].astype(str))),
    )
