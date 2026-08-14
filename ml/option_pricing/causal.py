from __future__ import annotations

import math
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Mapping, Sequence

import numpy as np
import pandas as pd

from datafetching.decision_time import (
    DecisionClock,
    completed_bar_clock_for_target,
    completed_bar_close as read_completed_bar_close,
    latest_completed_bar_clock,
)
from ml.option_pricing.black_scholes import (
    black_scholes_price,
    implied_volatility,
    target_years_to_expiration,
)
from ml.option_pricing.policies import (
    ContractSelectionPolicy,
    OPTION_PRICING_CONTRACT_POLICY_VERSION,
    OPTION_PRICING_DIVIDEND_POLICY_VERSION,
    OPTION_PRICING_EXPIRATION_POLICY_VERSION,
    OPTION_PRICING_POLICY_VERSION,
    OPTION_PRICING_RATE_POLICY_VERSION,
    OPTION_PRICING_SCHEMA_VERSION,
    OPTION_PRICING_TIMING_POLICY_VERSION,
    OPTION_PRICING_VOLATILITY_POLICY_VERSION,
    SEMANTIC_FEATURE_COLUMNS,
)
from options.publication import (
    CommittedOptionSnapshot,
    canonical_option_snapshots,
)
from ml.option_pricing.target_outcome import TARGET_OUTCOME_PROOF_COLUMNS


@dataclass(frozen=True)
class CausalSampleBatch:
    samples: pd.DataFrame
    source_files: tuple[Path, ...]
    status: str
    reason: str
    target_snapshot_for: pd.Timestamp | None


def select_strictly_earlier_snapshot(
    snapshots: Sequence[CommittedOptionSnapshot],
    *,
    target_snapshot_for: object,
    prediction_created_at: object,
) -> CommittedOptionSnapshot | None:
    """Choose the newest receipt strictly earlier on both causal clocks."""

    target = _utc(target_snapshot_for, "target_snapshot_for")
    created = _utc(prediction_created_at, "prediction_created_at")
    eligible = _strictly_earlier_snapshots(
        snapshots,
        target_snapshot_for=target,
        prediction_created_at=created,
    )
    return eligible[0] if eligible else None


def _strictly_earlier_snapshots(
    snapshots: Sequence[CommittedOptionSnapshot],
    *,
    target_snapshot_for: object,
    prediction_created_at: object,
) -> tuple[CommittedOptionSnapshot, ...]:
    target = _utc(target_snapshot_for, "target_snapshot_for")
    created = _utc(prediction_created_at, "prediction_created_at")
    eligible = [
        snapshot
        for snapshot in snapshots
        if snapshot.snapshot_for < target
        and _snapshot_receipt_available_at(snapshot) < created
    ]
    # A Schwab snapshot is identified by its natural market target, not by the
    # number of times it was republished.  Later receipts remain useful
    # lineage diagnostics, but they may neither replace the earliest causal
    # source nor multiply its influence.
    earliest_by_target: dict[pd.Timestamp, CommittedOptionSnapshot] = {}
    for snapshot in eligible:
        previous = earliest_by_target.get(snapshot.snapshot_for)
        if previous is None or (
            _snapshot_receipt_available_at(snapshot), snapshot.directory.as_posix()
        ) < (
            _snapshot_receipt_available_at(previous), previous.directory.as_posix()
        ):
            earliest_by_target[snapshot.snapshot_for] = snapshot
    return tuple(
        sorted(
            earliest_by_target.values(),
            key=lambda snapshot: (
                snapshot.snapshot_for,
                _snapshot_receipt_available_at(snapshot),
            ),
            reverse=True,
        )
    )


def build_live_prediction_inputs(
    datastore_root: Path,
    *,
    symbol: str,
    prediction_created_at: object,
    target_snapshot_for: object | None = None,
    decision_clock: DecisionClock | None = None,
    target_underlying_price: float | None = None,
    target_source_files: Sequence[Path] | None = None,
    contract_policy: ContractSelectionPolicy | None = None,
    rate_observations: pd.DataFrame | None = None,
    allow_source_chain_carry_fallback: bool = True,
) -> CausalSampleBatch:
    """Resolve a pre-quote target bar and materialize strictly lagged inputs."""

    root = Path(datastore_root)
    clean_symbol = str(symbol).strip().upper()
    created = _utc(prediction_created_at, "prediction_created_at")
    if decision_clock is not None:
        clock = decision_clock
        if target_snapshot_for is not None and pd.Timestamp(
            clock.decision_timestamp
        ) != _utc(target_snapshot_for, "target_snapshot_for"):
            raise ValueError("Provided decision clock does not match the cycle target")
    elif target_snapshot_for is not None:
        clock = completed_bar_clock_for_target(
            root,
            symbol=clean_symbol,
            target_snapshot_for=target_snapshot_for,
            as_of=created,
        )
    else:
        clock = latest_completed_bar_clock(root, symbol=clean_symbol, as_of=created)
    target = pd.Timestamp(clock.decision_timestamp)
    snapshots_by_provider = {
        provider: canonical_option_snapshots(
            root,
            symbol=clean_symbol,
            provider=provider,
        )[0]
        for provider in ("databento-opra", "schwab")
    }
    snapshots = tuple(
        snapshot
        for provider in ("databento-opra", "schwab")
        for snapshot in snapshots_by_provider[provider]
    )
    observed_target = [
        snapshot
        for snapshot in snapshots
        if snapshot.snapshot_for == target
        and _snapshot_receipt_available_at(snapshot) < created
    ]
    if observed_target:
        first_observation = min(
            observed_target,
            key=_snapshot_receipt_available_at,
        )
        return CausalSampleBatch(
            pd.DataFrame(),
            (first_observation.receipt_path,),
            "TARGET_ALREADY_OBSERVED",
            "A verified Options receipt for the target was visible before prediction.",
            target,
        )
    provider_sources = tuple(
        (
            provider,
            _strictly_earlier_snapshots(
                snapshots_by_provider[provider],
                target_snapshot_for=target,
                prediction_created_at=created,
            ),
        )
        for provider in ("databento-opra", "schwab")
    )
    if not any(sources for _provider, sources in provider_sources):
        return CausalSampleBatch(
            pd.DataFrame(),
            (),
            "SOURCE_SURFACE_UNAVAILABLE",
            "No strictly earlier committed OPRA or Schwab surface was available.",
            target,
        )
    underlying = (
        float(target_underlying_price)
        if target_underlying_price is not None
        else completed_bar_close(clock)
    )
    if not math.isfinite(underlying) or underlying <= 0.0:
        raise ValueError("Target underlying price must be finite and positive")
    policy = contract_policy or ContractSelectionPolicy()
    first_materialized: CausalSampleBatch | None = None
    target_files = (
        tuple(target_source_files)
        if target_source_files is not None
        else (clock.source_file,)
    )
    consulted_source_files: list[Path] = []
    maximum_candidates = 16
    for provider, sources in provider_sources:
        for source_index, source in enumerate(sources[:maximum_candidates]):
            consulted_source_files.extend(
                (source.contracts_path, source.receipt_path)
            )
            source_contracts = pd.read_parquet(source.contracts_path)
            if provider == "databento-opra":
                source_spot = pd.to_numeric(
                    source_contracts.get("underlying_price"), errors="coerce"
                )
                if source_spot is None or not np.isfinite(source_spot).any():
                    # OPRA quotes do not carry the equity spot. Bind the option
                    # surface to the exact receipt-visible Loop A bar for the
                    # source target instead of borrowing the later target spot.
                    try:
                        source_clock = completed_bar_clock_for_target(
                            root,
                            symbol=clean_symbol,
                            target_snapshot_for=source.snapshot_for,
                            as_of=_snapshot_receipt_available_at(source),
                        )
                        source_contracts["underlying_price"] = completed_bar_close(
                            source_clock
                        )
                        consulted_source_files.append(source_clock.source_file)
                    except (FileNotFoundError, RuntimeError, ValueError):
                        continue
            candidate_mask = _source_contract_candidate_mask(
                source_contracts,
                target_underlying_price=underlying,
                target_snapshot_for=target,
                policy=policy,
            )
            if not candidate_mask.any():
                continue
            samples = build_causal_samples(
                source_contracts.loc[candidate_mask].reset_index(drop=True),
                target_contracts=None,
                target_underlying_price=underlying,
                source_snapshot_for=source.snapshot_for,
                source_available_at=_snapshot_receipt_available_at(source),
                target_snapshot_for=target,
                source_provider=provider,
                prediction_mode="LIVE",
                prediction_created_at=created,
                prediction_available_at=created,
                provider_ingested_at=_snapshot_receipt_available_at(source),
                evidence_lane=(
                    "PROSPECTIVE_OPRA"
                    if provider == "databento-opra"
                    else "PROSPECTIVE_SCHWAB"
                ),
                fallback_used=provider == "schwab",
                datastore_root=root,
                contract_policy=policy,
                rate_observations=rate_observations,
                allow_source_chain_carry_fallback=allow_source_chain_carry_fallback,
            )
            available = samples["sample_status"].eq("AVAILABLE").any()
            if available and source_index:
                reason = (
                    f"Skipped {source_index} newer causal {provider} receipt(s) "
                    "with no usable contract; selected the newest surface that "
                    "passed the unchanged source contract."
                )
            elif available and provider == "schwab":
                reason = (
                    "Canonical OPRA evidence was unavailable or ineligible; "
                    "selected explicit causal Schwab fallback."
                )
            elif available:
                reason = "Selected canonical OPRA market evidence."
            else:
                reason = f"No {provider} contracts passed the causal feature contract."
            batch = CausalSampleBatch(
                samples,
                tuple(dict.fromkeys((*target_files, *consulted_source_files))),
                "READY" if available else "NO_ELIGIBLE_CONTRACTS",
                reason,
                target,
            )
            if available:
                return batch
            if first_materialized is None:
                first_materialized = batch
    if first_materialized is not None:
        return replace(
            first_materialized,
            source_files=tuple(
                dict.fromkeys((*target_files, *consulted_source_files))
            ),
        )

    # Preserve detailed exclusion rows when every bounded candidate failed the
    # cheap quote precheck. This keeps diagnostics honest while avoiding an
    # expensive full materialization for every known-stale receipt.
    diagnostic_provider, diagnostic_sources = next(
        (provider, sources)
        for provider, sources in provider_sources
        if sources
    )
    newest = diagnostic_sources[0]
    source_contracts = pd.read_parquet(newest.contracts_path)
    samples = build_causal_samples(
        source_contracts,
        target_contracts=None,
        target_underlying_price=underlying,
        source_snapshot_for=newest.snapshot_for,
        source_available_at=_snapshot_receipt_available_at(newest),
        target_snapshot_for=target,
        source_provider=diagnostic_provider,
        prediction_mode="LIVE",
        prediction_created_at=created,
        prediction_available_at=created,
        provider_ingested_at=_snapshot_receipt_available_at(newest),
        evidence_lane=(
            "PROSPECTIVE_OPRA"
            if diagnostic_provider == "databento-opra"
            else "PROSPECTIVE_SCHWAB"
        ),
        fallback_used=diagnostic_provider == "schwab",
        datastore_root=root,
        contract_policy=policy,
        rate_observations=rate_observations,
        allow_source_chain_carry_fallback=allow_source_chain_carry_fallback,
    )
    return CausalSampleBatch(
        samples,
        tuple(dict.fromkeys((*target_files, *consulted_source_files))),
        "NO_ELIGIBLE_CONTRACTS",
        "No contracts passed the causal feature contract.",
        target,
    )


def completed_bar_close(clock: DecisionClock) -> float:
    return read_completed_bar_close(clock)


def build_causal_samples(
    source_contracts: pd.DataFrame,
    *,
    target_contracts: pd.DataFrame | None,
    target_underlying_price: float,
    source_snapshot_for: object,
    source_available_at: object,
    target_snapshot_for: object,
    source_provider: str,
    prediction_mode: str,
    observed_available_at: object | None = None,
    prediction_created_at: object | None = None,
    prediction_available_at: object | None = None,
    provider_ingested_at: object | None = None,
    evidence_lane: str | None = None,
    fallback_used: bool | None = None,
    datastore_root: Path | None = None,
    contract_policy: ContractSelectionPolicy | None = None,
    rate_observations: pd.DataFrame | None = None,
    allow_source_chain_carry_fallback: bool = True,
) -> pd.DataFrame:
    """Build the six-feature causal contract without target quote leakage."""

    policy = contract_policy or ContractSelectionPolicy()
    source_time = _utc(source_snapshot_for, "source_snapshot_for")
    source_available = _utc(source_available_at, "source_available_at")
    target_time = _utc(target_snapshot_for, "target_snapshot_for")
    prediction_created = (
        _utc(prediction_created_at, "prediction_created_at")
        if prediction_created_at is not None
        else target_time
    )
    prediction_available = (
        _utc(prediction_available_at, "prediction_available_at")
        if prediction_available_at is not None
        else prediction_created
    )
    ingested_at = (
        _utc(provider_ingested_at, "provider_ingested_at")
        if provider_ingested_at is not None
        else None
    )
    mode = str(prediction_mode).strip().upper()
    if mode not in {"LIVE", "OFFLINE"}:
        raise ValueError("prediction_mode must be LIVE or OFFLINE")
    if not source_time < target_time:
        raise ValueError("Source option surface must be strictly earlier than target")
    if prediction_created < target_time:
        raise ValueError("Prediction creation cannot precede the market target")
    if prediction_available < prediction_created:
        raise ValueError("Prediction availability cannot precede creation")
    if not math.isfinite(float(target_underlying_price)) or target_underlying_price <= 0.0:
        raise ValueError("Target underlying price must be finite and positive")
    required = {
        "symbol",
        "contract_symbol",
        "call_put",
        "expiration_date",
        "strike",
        "underlying_price",
        "bid",
        "ask",
        "multiplier",
        "mini",
        "non_standard",
    }
    missing = sorted(required.difference(source_contracts.columns))
    if missing:
        raise ValueError("Source option contracts are missing: " + ", ".join(missing))
    source = source_contracts.copy()
    source["expiration_date"] = pd.to_datetime(
        source["expiration_date"], utc=True, errors="coerce"
    )
    for column in (
        "strike",
        "underlying_price",
        "bid",
        "ask",
        "multiplier",
        "interest_rate",
        "dividend_yield",
        "implied_volatility",
        "quote_staleness_seconds",
    ):
        if column not in source:
            source[column] = np.nan
        source[column] = pd.to_numeric(source[column], errors="coerce")
    if "quote_timestamp" not in source:
        source["quote_timestamp"] = pd.NaT
    source["quote_timestamp"] = pd.to_datetime(
        source["quote_timestamp"], utc=True, errors="coerce"
    )
    source = source.sort_values(
        ["expiration_date", "strike", "call_put", "contract_symbol"],
        kind="mergesort",
    ).drop_duplicates("contract_symbol", keep="last")

    resolved_rate, rate_source_at = _surface_rate(
        source,
        source_available_at=source_available,
        rate_observations=rate_observations,
    )
    resolved_dividend, dividend_source_at = _surface_dividend(
        source,
        risk_free_rate=resolved_rate,
        source_snapshot_for=source_time,
        source_available_at=source_available,
        allow_source_chain_fallback=allow_source_chain_carry_fallback,
    )
    rate_by_expiration: dict[pd.Timestamp, object] = {}
    dividend_by_expiration: dict[pd.Timestamp, object] = {}
    if datastore_root is not None:
        from ml.option_pricing.dividends import (
            load_verified_fmp_dividend_history,
            resolve_dividend_for_expiration,
        )
        from ml.option_pricing.rates import (
            load_verified_fmp_treasury_curves,
            resolve_rate_for_expiration,
        )

        curve_nodes = load_verified_fmp_treasury_curves(Path(datastore_root))
        source_symbols = source["symbol"].astype("string").str.upper().dropna().unique()
        dividend_history = {
            str(symbol): load_verified_fmp_dividend_history(
                Path(datastore_root), str(symbol)
            )
            for symbol in source_symbols
        }
        spot = float(target_underlying_price)
        symbol = str(source_symbols[0]) if len(source_symbols) else ""
        for raw_expiration in source["expiration_date"].dropna().unique():
            expiration = pd.Timestamp(raw_expiration)
            try:
                rate_resolution = resolve_rate_for_expiration(
                    prediction_created,
                    expiration,
                    curve_nodes=curve_nodes,
                    fallback_observations=rate_observations,
                )
            except LookupError:
                continue
            dividend_resolution = resolve_dividend_for_expiration(
                symbol,
                prediction_created,
                expiration,
                spot,
                events=dividend_history.get(symbol),
                risk_free_rate=rate_resolution.rate,
                parity_fallback_yield=(
                    resolved_dividend
                    if resolved_dividend is not None
                    and allow_source_chain_carry_fallback
                    else None
                ),
            )
            rate_by_expiration[expiration] = rate_resolution
            dividend_by_expiration[expiration] = dividend_resolution
    source["_resolved_rate"] = source["expiration_date"].map(
        lambda value: (
            rate_by_expiration[pd.Timestamp(value)].rate
            if pd.Timestamp(value) in rate_by_expiration
            else resolved_rate
        )
    )
    source["_resolved_dividend"] = source["expiration_date"].map(
        lambda value: (
            dividend_by_expiration[pd.Timestamp(value)].equivalent_dividend_yield
            if pd.Timestamp(value) in dividend_by_expiration
            else resolved_dividend
        )
    )
    source["_resolved_iv"] = _source_implied_volatilities(
        source,
        source_snapshot_for=source_time,
        risk_free_rate=resolved_rate,
        dividend_yield=resolved_dividend,
    )
    target_definitions = source.loc[
        :,
        [
            "contract_symbol",
            "strike",
            "expiration_date",
            "call_put",
            "_resolved_rate",
            "_resolved_dividend",
        ],
    ].copy()
    target_definitions["underlying_price"] = float(target_underlying_price)
    target_definitions["lagged_implied_volatility"] = interpolate_lagged_iv_surface(
        source,
        target_definitions,
        target_snapshot_for=target_time,
    )

    target_by_contract: dict[str, Mapping[str, object]] = {}
    observed_at = (
        _utc(observed_available_at, "observed_available_at")
        if observed_available_at is not None
        else None
    )
    if target_contracts is not None:
        target = target_contracts.copy()
        if "contract_symbol" not in target:
            raise ValueError("Target option contracts lack contract_symbol")
        if target["contract_symbol"].astype("string").duplicated().any():
            raise ValueError("Target option contracts contain duplicate symbols")
        target_by_contract = {
            str(row["contract_symbol"]): row
            for row in target.to_dict("records")
        }

    source_by_contract = {
        str(row["contract_symbol"]): row
        for row in source.to_dict("records")
    }
    rows: list[dict[str, object]] = []
    for definition in target_definitions.to_dict("records"):
        contract_symbol = str(definition["contract_symbol"])
        source_row = source_by_contract[contract_symbol]
        status, reason = _source_contract_status(
            source_row,
            target_underlying_price=float(target_underlying_price),
            target_snapshot_for=target_time,
            policy=policy,
        )
        lagged_iv = _finite_or_none(definition["lagged_implied_volatility"])
        contract_rate = _finite_or_none(definition.get("_resolved_rate"))
        contract_dividend = _finite_or_none(definition.get("_resolved_dividend"))
        rate_resolution = rate_by_expiration.get(
            pd.Timestamp(definition["expiration_date"])
        )
        dividend_resolution = dividend_by_expiration.get(
            pd.Timestamp(definition["expiration_date"])
        )
        if status == "AVAILABLE" and contract_rate is None:
            status, reason = "RATE_UNAVAILABLE", "No causal lagged rate observation was available."
        if status == "AVAILABLE" and contract_dividend is None:
            status, reason = "DIVIDEND_UNAVAILABLE", "No causal lagged dividend policy resolved."
        if status == "AVAILABLE" and lagged_iv is None:
            status, reason = "VOLATILITY_UNAVAILABLE", "Earlier IV surface cannot interpolate without extrapolation."
        years = target_years_to_expiration(target_time, definition["expiration_date"])
        if status == "AVAILABLE" and years <= 0.0:
            status, reason = "EXPIRED", "Contract expires no later than the target boundary."

        observed_bid = observed_ask = observed_mid = observed_quote = None
        contract_observed_at = observed_at
        if target_contracts is not None:
            observed = target_by_contract.get(contract_symbol)
            if observed is None:
                status, reason = "TARGET_CONTRACT_MISSING", "Target receipt omitted the exact semantic contract."
            elif not _same_semantic_contract(definition, observed):
                status, reason = "TARGET_CONTRACT_MISMATCH", "Target contract semantics changed."
            else:
                observed_bid = _finite_or_none(observed.get("bid"))
                observed_ask = _finite_or_none(observed.get("ask"))
                observed_quote = _timestamp_or_none(observed.get("quote_timestamp"))
                contract_observed_at = (
                    _timestamp_or_none(observed.get("available_at")) or observed_at
                )
                if (
                    observed_bid is None
                    or observed_ask is None
                    or observed_bid <= 0.0
                    or observed_ask <= observed_bid
                ):
                    status, reason = (
                        "TARGET_QUOTE_INVALID",
                        "Target BBO is missing, locked, crossed, or nonpositive.",
                    )
                elif (
                    contract_observed_at is None
                    or contract_observed_at <= prediction_available
                ):
                    status, reason = (
                        "TARGET_AVAILABILITY_INVALID",
                        "Target evidence was not available strictly after the emulated prediction boundary.",
                    )
                elif observed_quote is None or observed_quote <= prediction_available:
                    status, reason = (
                        "TARGET_TIMING_INVALID",
                        "Target quote is not strictly later than the emulated prediction boundary.",
                    )
                else:
                    observed_mid = (observed_bid + observed_ask) / 2.0

        black_scholes = normalized_residual = None
        if status == "AVAILABLE":
            black_scholes = black_scholes_price(
                float(target_underlying_price),
                float(definition["strike"]),
                float(contract_rate),
                float(lagged_iv),
                years,
                float(contract_dividend),
                str(definition["call_put"]),
            )
            if observed_mid is not None:
                normalized_residual = (
                    observed_mid - black_scholes
                ) / float(target_underlying_price)
        source_mid = (
            (float(source_row["bid"]) + float(source_row["ask"])) / 2.0
            if pd.notna(source_row["bid"]) and pd.notna(source_row["ask"])
            else None
        )
        rows.append(
            {
                "symbol": str(source_row["symbol"]).strip().upper(),
                "source_provider": str(source_provider).strip().lower(),
                "prediction_mode": mode,
                "call_put": str(definition["call_put"]).strip().upper(),
                "contract_symbol": contract_symbol,
                "expiration_date": definition["expiration_date"],
                "target_snapshot_for": target_time,
                "market_target_at": target_time,
                "source_snapshot_for": source_time,
                "source_available_at": source_available,
                "source_evidence_available_at": source_available,
                "source_quote_timestamp": _timestamp_or_none(source_row.get("quote_timestamp")),
                "source_quote_staleness_seconds": _finite_or_none(
                    source_row.get("quote_staleness_seconds")
                ),
                "observed_quote_timestamp": observed_quote,
                "observed_available_at": contract_observed_at,
                "outcome_quote_timestamp": observed_quote,
                "outcome_evidence_available_at": contract_observed_at,
                "prediction_created_at": prediction_created,
                "prediction_available_at": prediction_available,
                "provider_ingested_at": ingested_at,
                "outcome_provider": str(source_provider).strip().lower(),
                "evidence_lane": (
                    str(evidence_lane).strip().upper()
                    if evidence_lane is not None
                    else (
                        "OFFLINE_OPRA_BACKFILL"
                        if mode == "OFFLINE"
                        and str(source_provider).strip().lower() == "databento-opra"
                        else "PROSPECTIVE_OPRA"
                        if str(source_provider).strip().lower() == "databento-opra"
                        else "PROSPECTIVE_SCHWAB"
                        if mode == "LIVE"
                        else "OFFLINE_SCHWAB_BOOTSTRAP"
                    )
                ),
                "fallback_used": (
                    bool(fallback_used)
                    if fallback_used is not None
                    else str(source_provider).strip().lower() == "schwab"
                ),
                "underlying_price": float(target_underlying_price),
                "strike": float(definition["strike"]),
                "multiplier": _finite_or_none(source_row.get("multiplier")),
                "risk_free_rate": contract_rate,
                "rate_source_at": (
                    rate_resolution.source_available_at
                    if rate_resolution is not None
                    else rate_source_at
                ),
                "rate_source": (
                    rate_resolution.source
                    if rate_resolution is not None
                    else "PROVIDER_OR_ALFRED_FALLBACK"
                ),
                "lagged_implied_volatility": lagged_iv,
                "volatility_source_at": _timestamp_or_none(source_row.get("quote_timestamp")) or source_available,
                "target_years_to_expiration": years,
                "dividend_yield": contract_dividend,
                "dividend_source_at": (
                    dividend_resolution.dividend_source_available_at
                    if dividend_resolution is not None
                    and dividend_resolution.dividend_source_available_at is not None
                    else dividend_source_at
                ),
                "known_dividend_pv": (
                    dividend_resolution.known_dividend_pv
                    if dividend_resolution is not None
                    else None
                ),
                "equivalent_dividend_yield": contract_dividend,
                "dividend_event_count": (
                    dividend_resolution.dividend_event_count
                    if dividend_resolution is not None
                    else None
                ),
                "next_ex_dividend_date": (
                    pd.Timestamp(dividend_resolution.next_ex_dividend_date, tz="UTC")
                    if dividend_resolution is not None
                    and dividend_resolution.next_ex_dividend_date is not None
                    else None
                ),
                "dividend_source_available_at": (
                    dividend_resolution.dividend_source_available_at
                    if dividend_resolution is not None
                    else dividend_source_at
                ),
                "dividend_confidence": (
                    dividend_resolution.dividend_confidence
                    if dividend_resolution is not None
                    else "PUT_CALL_PARITY_FALLBACK"
                    if contract_dividend is not None
                    else None
                ),
                "contract_definition_as_of": _timestamp_or_none(
                    source_row.get("definition_as_of")
                ),
                "exercise_style": source_row.get("exercise_style"),
                "settlement_type": source_row.get("settlement_type"),
                "settlement_reference": source_row.get("settlement_reference"),
                "source_mid": source_mid,
                "observed_bid": observed_bid,
                "observed_ask": observed_ask,
                "observed_mid": observed_mid,
                "bid_ask_spread": (
                    observed_ask - observed_bid
                    if observed_bid is not None and observed_ask is not None
                    else None
                ),
                "black_scholes_price": black_scholes,
                "normalized_residual": normalized_residual,
                "sample_status": status,
                "exclusion_reason": reason,
                "expiration_policy_version": OPTION_PRICING_EXPIRATION_POLICY_VERSION,
                "timing_policy_version": OPTION_PRICING_TIMING_POLICY_VERSION,
                "rate_policy_version": OPTION_PRICING_RATE_POLICY_VERSION,
                "dividend_policy_version": OPTION_PRICING_DIVIDEND_POLICY_VERSION,
                "volatility_policy_version": OPTION_PRICING_VOLATILITY_POLICY_VERSION,
                "contract_policy_version": OPTION_PRICING_CONTRACT_POLICY_VERSION,
                "schema_version": OPTION_PRICING_SCHEMA_VERSION,
            }
        )
    from ml.option_pricing.weighting import attach_liquidity_weights

    return attach_liquidity_weights(pd.DataFrame(rows))


def interpolate_lagged_iv_surface(
    source_contracts: pd.DataFrame,
    target_contracts: pd.DataFrame,
    *,
    target_snapshot_for: object,
) -> pd.Series:
    """Interpolate an earlier IV surface without strike or tenor extrapolation."""

    target_time = _utc(target_snapshot_for, "target_snapshot_for")
    required_source = {"strike", "underlying_price", "expiration_date", "_resolved_iv"}
    required_target = {"strike", "underlying_price", "expiration_date"}
    if missing := sorted(required_source.difference(source_contracts.columns)):
        raise ValueError("Earlier IV surface is missing: " + ", ".join(missing))
    if missing := sorted(required_target.difference(target_contracts.columns)):
        raise ValueError("IV interpolation targets are missing: " + ", ".join(missing))
    points = source_contracts.copy()
    points["_x"] = np.log(
        pd.to_numeric(points["strike"], errors="coerce")
        / pd.to_numeric(points["underlying_price"], errors="coerce")
    )
    points["_t"] = points["expiration_date"].map(
        lambda value: target_years_to_expiration(target_time, value)
    )
    points["_iv"] = pd.to_numeric(points["_resolved_iv"], errors="coerce")
    points = points.loc[
        np.isfinite(points["_x"])
        & np.isfinite(points["_t"])
        & np.isfinite(points["_iv"])
        & points["_iv"].gt(0.0)
        & points["_t"].gt(0.0)
    ]
    surfaces: list[tuple[float, np.ndarray, np.ndarray]] = []
    for tenor, group in points.groupby("_t", sort=True):
        collapsed = (
            group.groupby("_x", as_index=False, sort=True)["_iv"]
            .median()
            .sort_values("_x")
        )
        surfaces.append(
            (
                float(tenor),
                collapsed["_x"].to_numpy(dtype=float),
                collapsed["_iv"].to_numpy(dtype=float),
            )
        )
    targets = target_contracts.copy()
    target_strike = pd.to_numeric(targets["strike"], errors="coerce")
    target_underlying = pd.to_numeric(
        targets["underlying_price"], errors="coerce"
    )
    targets["_x"] = np.log(target_strike / target_underlying)
    targets["_t"] = targets["expiration_date"].map(
        lambda value: target_years_to_expiration(target_time, value)
    )
    results = pd.Series(np.nan, index=targets.index, dtype="float64")
    surface_tenors = np.array([surface[0] for surface in surfaces], dtype=float)
    for tenor, group in targets.groupby("_t", sort=False, dropna=False):
        if not math.isfinite(float(tenor)) or float(tenor) <= 0.0:
            continue
        exact_positions = np.flatnonzero(
            np.isclose(surface_tenors, float(tenor), rtol=0.0, atol=1e-12)
        )
        if len(exact_positions):
            _, xs, ivs = surfaces[int(exact_positions[0])]
            target_x = pd.to_numeric(group["_x"], errors="coerce")
            in_bounds = (
                target_x.notna()
                & np.isfinite(target_x)
                & target_x.ge(xs[0] - 1e-12)
                & target_x.le(xs[-1] + 1e-12)
            )
            if len(xs) == 1:
                in_bounds &= target_x.sub(xs[0]).abs().le(1e-12)
                results.loc[group.index[in_bounds]] = float(ivs[0])
            elif in_bounds.any():
                results.loc[group.index[in_bounds]] = np.interp(
                    target_x.loc[in_bounds].to_numpy(dtype=float),
                    xs,
                    ivs,
                )
            continue
        for index, row in group.iterrows():
            x_target = _finite_or_none(row.get("_x"))
            if x_target is None:
                continue
            tenor_values: list[tuple[float, float]] = []
            for source_tenor, xs, ivs in surfaces:
                if x_target < xs[0] - 1e-12 or x_target > xs[-1] + 1e-12:
                    continue
                if len(xs) == 1 and not math.isclose(
                    x_target, xs[0], abs_tol=1e-12
                ):
                    continue
                value = (
                    float(ivs[0])
                    if len(xs) == 1
                    else float(np.interp(x_target, xs, ivs))
                )
                tenor_values.append((source_tenor, value))
            if not tenor_values:
                continue
            tenors = np.array(
                [value[0] for value in tenor_values], dtype=float
            )
            tenor_ivs = np.array(
                [value[1] for value in tenor_values], dtype=float
            )
            if (
                float(tenor) < tenors[0] - 1e-12
                or float(tenor) > tenors[-1] + 1e-12
                or (
                    len(tenors) == 1
                    and not math.isclose(
                        float(tenor), tenors[0], abs_tol=1e-12
                    )
                )
            ):
                continue
            results.loc[index] = (
                float(tenor_ivs[0])
                if len(tenors) == 1
                else float(np.interp(float(tenor), tenors, tenor_ivs))
            )
    return results


def model_feature_frame(samples: pd.DataFrame) -> pd.DataFrame:
    """Return only the declared six causal semantic inputs."""

    missing = sorted(set(SEMANTIC_FEATURE_COLUMNS).difference(samples.columns))
    if missing:
        raise ValueError("Pricing samples lack semantic inputs: " + ", ".join(missing))
    matrix = samples.loc[:, list(SEMANTIC_FEATURE_COLUMNS)].apply(
        pd.to_numeric, errors="coerce"
    )
    if not np.isfinite(matrix.to_numpy(dtype=float)).all():
        raise ValueError("Pricing semantic inputs must be finite")
    return matrix


def canonicalize_predictions(predictions: pd.DataFrame) -> pd.DataFrame:
    """Keep the earliest committed prediction per natural contract target."""

    if predictions.empty:
        return predictions.copy()
    required = {
        "symbol",
        "target_snapshot_for",
        "contract_symbol",
        "prediction_created_at",
        "prediction_available_at",
    }
    if missing := sorted(required.difference(predictions.columns)):
        raise ValueError("Pricing predictions are missing: " + ", ".join(missing))
    output = predictions.copy()
    for column in (
        "target_snapshot_for",
        "prediction_created_at",
        "prediction_available_at",
    ):
        output[column] = pd.to_datetime(output[column], utc=True, errors="coerce")
    valid = (
        output["prediction_created_at"].notna()
        & output["prediction_available_at"].notna()
        & output["prediction_available_at"].ge(output["prediction_created_at"])
    )
    output = output.loc[valid].sort_values(
        ["prediction_available_at", "prediction_created_at"], kind="mergesort"
    )
    return output.drop_duplicates(
        ["symbol", "target_snapshot_for", "contract_symbol"], keep="first"
    ).reset_index(drop=True)


def reconcile_predictions(
    predictions: pd.DataFrame,
    *,
    snapshots_by_symbol: Mapping[str, Sequence[CommittedOptionSnapshot]],
    evaluated_at: object,
) -> pd.DataFrame:
    """Reconcile canonical predictions only to exact later option receipts."""

    evaluated = _utc(evaluated_at, "evaluated_at")
    canonical = canonicalize_predictions(predictions)
    contract_cache: dict[Path, pd.DataFrame] = {}
    rows: list[dict[str, object]] = []
    for prediction in canonical.to_dict("records"):
        symbol = str(prediction["symbol"]).strip().upper()
        target = _utc(prediction["target_snapshot_for"], "target_snapshot_for")
        created = _utc(prediction["prediction_created_at"], "prediction_created_at")
        available = _utc(prediction["prediction_available_at"], "prediction_available_at")
        authority_proof_time = _timestamp_or_none(
            prediction.get(TARGET_OUTCOME_PROOF_COLUMNS[2])
        )
        authority_available = (
            authority_proof_time if authority_proof_time is not None else available
        )
        natural_receipts = [
            snapshot
            for snapshot in snapshots_by_symbol.get(symbol, ())
            if snapshot.snapshot_for == target
        ]
        earliest_by_provider: dict[str, CommittedOptionSnapshot] = {}
        for snapshot in natural_receipts:
            prior = earliest_by_provider.get(snapshot.provider)
            if prior is None or (
                _snapshot_receipt_available_at(snapshot),
                snapshot.directory.as_posix(),
            ) < (
                _snapshot_receipt_available_at(prior),
                prior.directory.as_posix(),
            ):
                earliest_by_provider[snapshot.provider] = snapshot
        natural_receipts = list(earliest_by_provider.values())
        status = "PENDING_TARGET_RECEIPT"
        observed: Mapping[str, object] | None = None
        receipt_available: pd.Timestamp | None = None
        authority_proven = False
        if natural_receipts:
            # Knowledge from either provider contaminates a prediction. A
            # later OPRA or Schwab receipt can never rescue a target that was
            # already visible before the prediction publication boundary.
            earliest_receipt = min(
                _snapshot_receipt_available_at(snapshot)
                for snapshot in natural_receipts
            )
            if earliest_receipt <= created or earliest_receipt <= authority_available:
                receipt_available = earliest_receipt
                status = "TARGET_ALREADY_OBSERVED_BEFORE_PREDICTION"
            else:
                # Canonical OPRA is evaluated first regardless of provider
                # receipt ordering. Schwab is used only when OPRA has no valid
                # exact semantic outcome, and the chosen provider is persisted.
                candidates = sorted(
                    natural_receipts,
                    key=lambda snapshot: (
                        0 if snapshot.provider == "databento-opra" else 1,
                        _snapshot_receipt_available_at(snapshot),
                        snapshot.directory.as_posix(),
                    ),
                )
                for snapshot in candidates:
                    candidate_available = _snapshot_receipt_available_at(snapshot)
                    candidate_authority = _prospective_authority_proven(
                        prediction, snapshot
                    )
                    contracts = contract_cache.setdefault(
                        snapshot.contracts_path,
                        pd.read_parquet(snapshot.contracts_path),
                    )
                    exact = contracts.loc[
                        contracts["contract_symbol"]
                        .astype(str)
                        .eq(str(prediction["contract_symbol"]))
                    ]
                    if exact.empty:
                        status = "MISSING_TARGET_CONTRACT"
                        continue
                    if len(exact) != 1 or not _same_semantic_contract(
                        prediction, exact.iloc[0]
                    ):
                        status = "TARGET_CONTRACT_MISMATCH"
                        continue
                    candidate_observed = exact.iloc[0].to_dict()
                    quote_time = _timestamp_or_none(
                        candidate_observed.get("quote_timestamp")
                    )
                    bid = _finite_or_none(candidate_observed.get("bid"))
                    ask = _finite_or_none(candidate_observed.get("ask"))
                    if quote_time is None:
                        status = "TARGET_QUOTE_TIMESTAMP_MISSING"
                        continue
                    if quote_time <= created or quote_time <= authority_available:
                        status = "STALE_PRE_PREDICTION_QUOTE"
                        continue
                    if bid is None or ask is None or bid <= 0.0 or ask <= bid:
                        status = "TARGET_QUOTE_INVALID"
                        continue
                    observed = {
                        **candidate_observed,
                        "provider": snapshot.provider,
                        "provider_ingested_at": candidate_available,
                    }
                    receipt_available = candidate_available
                    authority_proven = candidate_authority
                    status = "COMPLETE"
                    break
        rows.append(
            _evaluation_row(
                prediction,
                observed=observed,
                observed_available_at=receipt_available,
                evaluated_at=evaluated,
                status=status,
                prospective_authority_proven=authority_proven,
            )
        )
    return pd.DataFrame(rows)


def evaluate_offline_predictions(
    predictions: pd.DataFrame,
    samples: pd.DataFrame,
    *,
    evaluated_at: object,
) -> pd.DataFrame:
    """Evaluate OFFLINE predictions against their verified emulated targets."""

    evaluated = _utc(evaluated_at, "evaluated_at")
    offline = canonicalize_predictions(predictions)
    offline = offline.loc[
        offline["prediction_mode"].astype("string").str.upper().eq("OFFLINE")
    ]
    if offline.empty:
        return pd.DataFrame()
    required = {
        "symbol",
        "target_snapshot_for",
        "contract_symbol",
        "observed_bid",
        "observed_ask",
        "observed_quote_timestamp",
        "observed_available_at",
    }
    if missing := sorted(required.difference(samples.columns)):
        raise ValueError("Offline pricing samples are missing: " + ", ".join(missing))
    targets = samples.copy()
    targets["target_snapshot_for"] = pd.to_datetime(
        targets["target_snapshot_for"], utc=True, errors="coerce"
    )
    if targets.duplicated(
        ["symbol", "target_snapshot_for", "contract_symbol"]
    ).any():
        raise ValueError("Offline pricing samples contain duplicate natural targets")
    lookup = {
        (
            str(row["symbol"]).strip().upper(),
            _utc(row["target_snapshot_for"], "target_snapshot_for"),
            str(row["contract_symbol"]),
        ): row
        for row in targets.to_dict("records")
    }
    rows: list[dict[str, object]] = []
    for prediction in offline.to_dict("records"):
        key = (
            str(prediction["symbol"]).strip().upper(),
            _utc(prediction["target_snapshot_for"], "target_snapshot_for"),
            str(prediction["contract_symbol"]),
        )
        sample = lookup.get(key)
        status = "MISSING_OFFLINE_TARGET"
        observed: Mapping[str, object] | None = None
        observed_available: pd.Timestamp | None = None
        if sample is not None:
            if not _same_semantic_contract(prediction, sample):
                status = "TARGET_CONTRACT_MISMATCH"
            else:
                quote = _timestamp_or_none(sample.get("observed_quote_timestamp"))
                created = _utc(prediction["prediction_created_at"], "prediction_created_at")
                available = _utc(prediction["prediction_available_at"], "prediction_available_at")
                bid = _finite_or_none(sample.get("observed_bid"))
                ask = _finite_or_none(sample.get("observed_ask"))
                observed_available = _timestamp_or_none(sample.get("observed_available_at"))
                observed = {
                    **sample,
                    "bid": bid,
                    "ask": ask,
                    "quote_timestamp": quote,
                }
                if quote is None:
                    status = "TARGET_QUOTE_TIMESTAMP_MISSING"
                elif quote <= created or quote <= available:
                    status = "STALE_PRE_PREDICTION_QUOTE"
                elif observed_available is None or observed_available <= available:
                    status = "TARGET_AVAILABILITY_INVALID"
                elif (
                    bid is None
                    or ask is None
                    or bid <= 0.0
                    or ask <= bid
                ):
                    status = "TARGET_QUOTE_INVALID"
                else:
                    status = "COMPLETE"
        rows.append(
            _evaluation_row(
                prediction,
                observed=observed,
                observed_available_at=observed_available,
                evaluated_at=evaluated,
                status=status,
                prospective_authority_proven=False,
            )
        )
    return pd.DataFrame(rows)


def _evaluation_row(
    prediction: Mapping[str, object],
    *,
    observed: Mapping[str, object] | None,
    observed_available_at: pd.Timestamp | None,
    evaluated_at: pd.Timestamp,
    status: str,
    prospective_authority_proven: bool,
) -> dict[str, object]:
    bid = _finite_or_none(observed.get("bid")) if observed is not None else None
    ask = _finite_or_none(observed.get("ask")) if observed is not None else None
    midpoint = (bid + ask) / 2.0 if bid is not None and ask is not None else None
    spread = ask - bid if bid is not None and ask is not None else None
    underlying = _finite_or_none(prediction.get("underlying_price"))
    fair = _finite_or_none(prediction.get("constrained_fair_value"))
    raw = _finite_or_none(prediction.get("raw_fair_value"))
    error = fair - midpoint if fair is not None and midpoint is not None else None
    observed_residual = (
        (midpoint - float(prediction["black_scholes_price"])) / underlying
        if midpoint is not None and underlying not in {None, 0.0}
        else None
    )
    half_spread = spread / 2.0 if spread is not None and spread > 0.0 else None
    complete = status == "COMPLETE"
    prospective = bool(
        complete
        and str(prediction.get("prediction_mode", "")).upper() == "LIVE"
        and str(prediction.get("source_provider", "")).strip().lower()
        in {"databento-opra", "schwab"}
        and prospective_authority_proven
    )
    outcome_quote = (
        _timestamp_or_none(observed.get("quote_timestamp"))
        if observed is not None
        else None
    )
    outcome_provider = (
        observed.get("provider") if observed is not None else None
    ) or prediction.get("source_provider")
    normalized_outcome_provider = str(outcome_provider or "").strip().lower()
    prediction_mode = str(prediction.get("prediction_mode", "")).strip().upper()
    outcome_lane = (
        "OFFLINE_OPRA_BACKFILL"
        if prediction_mode == "OFFLINE"
        else "PROSPECTIVE_OPRA"
        if normalized_outcome_provider == "databento-opra"
        else "PROSPECTIVE_SCHWAB"
        if normalized_outcome_provider == "schwab"
        else prediction.get("evidence_lane")
    )
    outcome_fallback = bool(
        prediction_mode == "LIVE" and normalized_outcome_provider == "schwab"
    )

    def covered(lower_name: str, upper_name: str) -> bool | None:
        lower = _finite_or_none(prediction.get(lower_name))
        upper = _finite_or_none(prediction.get(upper_name))
        return (
            bool(lower <= midpoint <= upper)
            if lower is not None and upper is not None and midpoint is not None
            else None
        )

    return {
        "symbol": str(prediction.get("symbol", "")).strip().upper(),
        "source_provider": prediction.get("source_provider"),
        "prediction_mode": prediction.get("prediction_mode"),
        "call_put": prediction.get("call_put"),
        "contract_symbol": prediction.get("contract_symbol"),
        "expiration_date": prediction.get("expiration_date"),
        "target_snapshot_for": prediction.get("target_snapshot_for"),
        "prediction_created_at": prediction.get("prediction_created_at"),
        "prediction_available_at": prediction.get("prediction_available_at"),
        "observed_quote_timestamp": (
            outcome_quote
        ),
        "observed_available_at": observed_available_at,
        "outcome_quote_timestamp": outcome_quote,
        "outcome_evidence_available_at": observed_available_at,
        "provider_ingested_at": (
            observed.get("provider_ingested_at")
            if observed is not None
            else prediction.get("provider_ingested_at")
        ),
        "outcome_provider": outcome_provider,
        "evidence_lane": outcome_lane,
        "fallback_used": outcome_fallback,
        "evaluated_at": evaluated_at,
        "model_name": prediction.get("model_name"),
        "model_version": prediction.get("model_version"),
        "underlying_price": underlying,
        "strike": prediction.get("strike"),
        "multiplier": prediction.get("multiplier"),
        "lagged_implied_volatility": prediction.get("lagged_implied_volatility"),
        "target_years_to_expiration": prediction.get("target_years_to_expiration"),
        "observed_bid": bid,
        "observed_ask": ask,
        "observed_mid": midpoint,
        "bid_ask_spread": spread,
        "observed_quote_staleness_seconds": (
            (observed_available_at - _timestamp_or_none(observed.get("quote_timestamp"))).total_seconds()
            if observed is not None
            and observed_available_at is not None
            and _timestamp_or_none(observed.get("quote_timestamp")) is not None
            else None
        ),
        "black_scholes_price": prediction.get("black_scholes_price"),
        "predicted_normalized_residual": prediction.get("predicted_normalized_residual"),
        "observed_normalized_residual": observed_residual,
        "raw_fair_value": raw,
        "constrained_fair_value": fair,
        "predictive_standard_deviation": prediction.get("predictive_standard_deviation"),
        "constrained_interval_80_lower": prediction.get("constrained_interval_80_lower"),
        "constrained_interval_80_upper": prediction.get("constrained_interval_80_upper"),
        "constrained_interval_95_lower": prediction.get("constrained_interval_95_lower"),
        "constrained_interval_95_upper": prediction.get("constrained_interval_95_upper"),
        "dollar_error": error if complete else None,
        "normalized_absolute_error": (
            abs(error) / underlying if complete and error is not None and underlying else None
        ),
        "normalized_squared_error": (
            (error / underlying) ** 2 if complete and error is not None and underlying else None
        ),
        "error_in_half_spreads": (
            abs(error) / half_spread if complete and error is not None and half_spread else None
        ),
        "model_edge_in_half_spreads": (
            (fair - midpoint) / half_spread if complete and fair is not None and midpoint is not None and half_spread else None
        ),
        "interval_80_covered": covered("constrained_interval_80_lower", "constrained_interval_80_upper") if complete else None,
        "interval_95_covered": covered("constrained_interval_95_lower", "constrained_interval_95_upper") if complete else None,
        "prospective_eligible": prospective,
        "evaluation_status": status,
        "pricing_policy_version": OPTION_PRICING_POLICY_VERSION,
        "timing_policy_version": OPTION_PRICING_TIMING_POLICY_VERSION,
        "schema_version": OPTION_PRICING_SCHEMA_VERSION,
    }


def _prospective_authority_proven(
    prediction: Mapping[str, object],
    snapshot: CommittedOptionSnapshot,
) -> bool:
    barrier = snapshot.receipt.get("pricing_barrier")
    if not isinstance(barrier, Mapping):
        return _legacy_authority_ordering_proven(prediction, snapshot)
    request = _timestamp_or_none(snapshot.receipt.get("request_started_at"))
    observed = _timestamp_or_none(barrier.get("observed_at"))
    published = _timestamp_or_none(barrier.get("pricing_published_at"))
    prediction_available = _timestamp_or_none(prediction.get("prediction_available_at"))
    target = _timestamp_or_none(barrier.get("target_snapshot_for"))
    expected_target = _timestamp_or_none(prediction.get("target_snapshot_for"))
    proof_run = str(prediction.get(TARGET_OUTCOME_PROOF_COLUMNS[0], ""))
    proof_checksum = str(prediction.get(TARGET_OUTCOME_PROOF_COLUMNS[1], ""))
    proof_published = _timestamp_or_none(
        prediction.get(TARGET_OUTCOME_PROOF_COLUMNS[2])
    )
    return bool(
        barrier.get("status") == "VERIFIED"
        and barrier.get("prospective_credit_allowed") is True
        and request is not None
        and observed is not None
        and published is not None
        and prediction_available is not None
        and target is not None
        and expected_target is not None
        and target == expected_target == snapshot.snapshot_for
        and published == proof_published
        and prediction_available <= proof_published
        and observed <= request
        and published <= request
        and str(barrier.get("pricing_run_path", "")) == proof_run
        and str(barrier.get("pricing_receipt_checksum_sha256", ""))
        == proof_checksum
    )


def _legacy_authority_ordering_proven(
    prediction: Mapping[str, object],
    snapshot: CommittedOptionSnapshot,
) -> bool:
    """Conservative migration proof for pre-barrier immutable receipts."""

    proof_run = str(prediction.get(TARGET_OUTCOME_PROOF_COLUMNS[0], ""))
    if not proof_run.startswith("ml/option-pricing-runs/"):
        return False
    published = _timestamp_or_none(prediction.get(TARGET_OUTCOME_PROOF_COLUMNS[2]))
    if published is None or not snapshot.raw_path.is_file():
        return False
    try:
        raw = pd.read_parquet(snapshot.raw_path, columns=["quote_cutoff_at"])
    except Exception:
        return False
    requests = pd.to_datetime(raw["quote_cutoff_at"], utc=True, errors="coerce").dropna()
    if len(requests) != 1:
        return False
    request_started_at = pd.Timestamp(requests.iloc[0])
    return bool(
        published <= request_started_at < _snapshot_receipt_available_at(snapshot)
    )


def _snapshot_receipt_available_at(
    snapshot: CommittedOptionSnapshot,
) -> pd.Timestamp:
    return (
        snapshot.receipt_published_at
        if snapshot.receipt_published_at is not None
        else snapshot.available_at
    )


def _surface_rate(
    source: pd.DataFrame,
    *,
    source_available_at: pd.Timestamp,
    rate_observations: pd.DataFrame | None,
) -> tuple[float | None, pd.Timestamp | None]:
    provider = pd.to_numeric(source.get("interest_rate"), errors="coerce")
    provider = provider.loc[np.isfinite(provider) & provider.between(-0.20, 1.0)]
    if not provider.empty:
        return float(provider.median()), source_available_at
    if rate_observations is None or rate_observations.empty:
        return None, None
    required = {"available_at", "risk_free_rate"}
    if not required.issubset(rate_observations.columns):
        raise ValueError("Rate observations require available_at and risk_free_rate")
    observations = rate_observations.copy()
    observations["available_at"] = pd.to_datetime(
        observations["available_at"], utc=True, errors="coerce"
    )
    observations["risk_free_rate"] = pd.to_numeric(
        observations["risk_free_rate"], errors="coerce"
    )
    observations = observations.loc[
        observations["available_at"].lt(source_available_at)
        & observations["risk_free_rate"].between(-0.20, 1.0)
    ].sort_values("available_at")
    if observations.empty:
        return None, None
    row = observations.iloc[-1]
    return float(row["risk_free_rate"]), pd.Timestamp(row["available_at"])


def _surface_dividend(
    source: pd.DataFrame,
    *,
    risk_free_rate: float | None,
    source_snapshot_for: pd.Timestamp,
    source_available_at: pd.Timestamp,
    allow_source_chain_fallback: bool = True,
) -> tuple[float | None, pd.Timestamp | None]:
    provider = pd.to_numeric(source.get("dividend_yield"), errors="coerce")
    provider = provider.loc[np.isfinite(provider) & provider.between(-0.20, 0.50)]
    if not provider.empty:
        return float(provider.median()), source_available_at
    if not allow_source_chain_fallback:
        # The Loop-native v1 lane excludes missing carry rather than relying on
        # the legacy single-strike American parity approximation.  A future
        # source-chain policy must be separately versioned and quality-gated.
        return None, None
    if risk_free_rate is None:
        return None, None
    candidates: list[float] = []
    paired = source.copy()
    paired["_mid"] = (paired["bid"] + paired["ask"]) / 2.0
    for (_expiration, strike), group in paired.groupby(
        ["expiration_date", "strike"], dropna=True
    ):
        calls = group.loc[group["call_put"].astype(str).str.upper().eq("CALL")]
        puts = group.loc[group["call_put"].astype(str).str.upper().eq("PUT")]
        if calls.empty or puts.empty:
            continue
        call = calls.iloc[-1]
        put = puts.iloc[-1]
        years = target_years_to_expiration(source_snapshot_for, call["expiration_date"])
        spot = _finite_or_none(call.get("underlying_price"))
        call_mid = _finite_or_none(call.get("_mid"))
        put_mid = _finite_or_none(put.get("_mid"))
        if years <= 0.0 or spot in {None, 0.0} or call_mid is None or put_mid is None:
            continue
        discounted_spot = call_mid - put_mid + float(strike) * math.exp(-risk_free_rate * years)
        if discounted_spot <= 0.0:
            continue
        value = -math.log(discounted_spot / spot) / years
        if math.isfinite(value) and -0.20 <= value <= 0.50:
            candidates.append(value)
    return (
        (float(np.median(candidates)), source_available_at)
        if candidates
        else (None, None)
    )


def _source_implied_volatilities(
    source: pd.DataFrame,
    *,
    source_snapshot_for: pd.Timestamp,
    risk_free_rate: float | None,
    dividend_yield: float | None,
) -> pd.Series:
    supplied = pd.to_numeric(
        source.get("implied_volatility", pd.Series(index=source.index, dtype=float)),
        errors="coerce",
    )
    valid_supplied = supplied.gt(0.0) & supplied.le(5.0) & np.isfinite(supplied)
    output = supplied.where(valid_supplied, np.nan).astype("float64")
    if valid_supplied.all():
        return output
    for index, row in source.loc[~valid_supplied].iterrows():
        row_rate = _finite_or_none(row.get("_resolved_rate"))
        row_dividend = _finite_or_none(row.get("_resolved_dividend"))
        if row_rate is None:
            row_rate = risk_free_rate
        if row_dividend is None:
            row_dividend = dividend_yield
        if row_rate is None or row_dividend is None:
            continue
        bid = _finite_or_none(row.get("bid"))
        ask = _finite_or_none(row.get("ask"))
        spot = _finite_or_none(row.get("underlying_price"))
        strike = _finite_or_none(row.get("strike"))
        if bid is None or ask is None or ask < bid or spot is None or strike is None:
            continue
        years = target_years_to_expiration(source_snapshot_for, row.get("expiration_date"))
        try:
            value = implied_volatility(
                (bid + ask) / 2.0,
                spot,
                strike,
                row_rate,
                years,
                row_dividend,
                str(row.get("call_put")),
            )
        except ValueError:
            value = np.nan
        output.loc[index] = value
    return output


def _source_contract_status(
    row: Mapping[str, object] | pd.Series,
    *,
    target_underlying_price: float,
    target_snapshot_for: pd.Timestamp,
    policy: ContractSelectionPolicy,
) -> tuple[str, str]:
    multiplier = _finite_or_none(row.get("multiplier"))
    mini = _explicit_bool(row.get("mini"))
    nonstandard = _explicit_bool(row.get("non_standard"))
    if mini is not False or nonstandard is not False or multiplier is None or not math.isclose(multiplier, policy.required_multiplier):
        return "NONSTANDARD_CONTRACT", "Contract is mini, adjusted, nonstandard, or not a 100-share contract."
    if "exercise_style" in row:
        exercise_style = str(row.get("exercise_style") or "").strip().upper()
        if exercise_style not in {"AMERICAN", "EUROPEAN"}:
            return (
                "EXERCISE_STYLE_AMBIGUOUS",
                "Point-in-time contract reference lacks a supported exercise style.",
            )
    strike = _finite_or_none(row.get("strike"))
    if strike is None or strike <= 0.0:
        return "INVALID_STRIKE", "Strike must be finite and positive."
    years = target_years_to_expiration(target_snapshot_for, row.get("expiration_date"))
    days = years * 365.0
    if not policy.minimum_days_to_expiration <= days <= policy.maximum_days_to_expiration:
        return "DTE_OUT_OF_RANGE", "Target expiration is outside the pilot DTE range."
    if abs(math.log(strike / target_underlying_price)) > policy.maximum_absolute_log_moneyness:
        return "MONEYNESS_OUT_OF_RANGE", "Contract is outside the pilot log-moneyness range."
    bid = _finite_or_none(row.get("bid"))
    ask = _finite_or_none(row.get("ask"))
    if bid is None or ask is None or bid <= 0.0 or ask <= bid:
        return "SOURCE_QUOTE_INVALID", "Earlier BBO is missing, locked, crossed, or nonpositive."
    staleness = _finite_or_none(row.get("quote_staleness_seconds"))
    if staleness is None or staleness < 0.0 or staleness > policy.maximum_source_staleness_seconds:
        return "SOURCE_QUOTE_STALE", "Earlier BBO exceeds the configured staleness window."
    quote_time = _timestamp_or_none(row.get("quote_timestamp"))
    if quote_time is None or quote_time >= target_snapshot_for:
        return "SOURCE_TIMING_INVALID", "Earlier option quote is not strictly before target."
    return "AVAILABLE", ""


def _source_contract_candidate_mask(
    frame: pd.DataFrame,
    *,
    target_underlying_price: float,
    target_snapshot_for: pd.Timestamp,
    policy: ContractSelectionPolicy,
) -> pd.Series:
    """Vectorize the unchanged source contract before expensive IV materialization."""

    required = {
        "expiration_date",
        "strike",
        "bid",
        "ask",
        "multiplier",
        "mini",
        "non_standard",
        "quote_staleness_seconds",
        "quote_timestamp",
    }
    if frame.empty or not required.issubset(frame.columns):
        return pd.Series(False, index=frame.index, dtype=bool)
    strike = pd.to_numeric(frame["strike"], errors="coerce")
    bid = pd.to_numeric(frame["bid"], errors="coerce")
    ask = pd.to_numeric(frame["ask"], errors="coerce")
    multiplier = pd.to_numeric(frame["multiplier"], errors="coerce")
    staleness = pd.to_numeric(
        frame["quote_staleness_seconds"], errors="coerce"
    )
    quote_time = pd.to_datetime(
        frame["quote_timestamp"], utc=True, errors="coerce"
    )
    expiration = pd.to_datetime(
        frame["expiration_date"], utc=True, errors="coerce"
    )
    expiration_days = {
        pd.Timestamp(value): (
            target_years_to_expiration(target_snapshot_for, value) * 365.0
        )
        for value in expiration.dropna().unique()
    }
    days = expiration.map(expiration_days)
    moneyness = np.log(strike / float(target_underlying_price)).abs()
    standard = (
        frame["mini"].map(_explicit_bool).eq(False)
        & frame["non_standard"].map(_explicit_bool).eq(False)
        & np.isclose(
            multiplier,
            float(policy.required_multiplier),
            rtol=1e-9,
            atol=0.0,
        )
    )
    return (
        standard
        & strike.gt(0.0)
        & np.isfinite(strike)
        & days.between(
            float(policy.minimum_days_to_expiration),
            float(policy.maximum_days_to_expiration),
        )
        & moneyness.le(float(policy.maximum_absolute_log_moneyness))
        & np.isfinite(moneyness)
        & bid.gt(0.0)
        & ask.gt(bid)
        & staleness.between(
            0.0,
            float(policy.maximum_source_staleness_seconds),
        )
        & quote_time.notna()
        & quote_time.lt(target_snapshot_for)
    )


def _same_semantic_contract(
    expected: Mapping[str, object],
    observed: Mapping[str, object] | pd.Series,
) -> bool:
    try:
        expected_expiration = pd.Timestamp(expected["expiration_date"]).date()
        observed_expiration = pd.Timestamp(observed["expiration_date"]).date()
        multiplier_matches = True
        if "multiplier" in expected:
            multiplier_matches = math.isclose(
                float(expected["multiplier"]),
                float(observed["multiplier"]),
                abs_tol=1e-9,
            )
        observed_keys = set(
            observed.index if isinstance(observed, pd.Series) else observed.keys()
        )
        expected_keys = set(
            expected.index if isinstance(expected, pd.Series) else expected.keys()
        )
        observed_is_raw_contract = {
            "bid",
            "ask",
            "quote_timestamp",
        }.issubset(observed_keys)
        standard_matches = True
        if observed_is_raw_contract:
            standard_matches = bool(
                "mini" in observed_keys
                and "non_standard" in observed_keys
                and _explicit_bool(observed.get("mini")) is False
                and _explicit_bool(observed.get("non_standard")) is False
            )
        if "mini" in expected_keys or "non_standard" in expected_keys:
            standard_matches = bool(
                standard_matches
                and _explicit_bool(expected.get("mini")) is False
                and _explicit_bool(expected.get("non_standard")) is False
            )
        return bool(
            str(expected["contract_symbol"]) == str(observed["contract_symbol"])
            and str(expected["call_put"]).strip().upper()
            == str(observed["call_put"]).strip().upper()
            and math.isclose(float(expected["strike"]), float(observed["strike"]), abs_tol=1e-9)
            and expected_expiration == observed_expiration
            and multiplier_matches
            and standard_matches
        )
    except (KeyError, TypeError, ValueError):
        return False


def _explicit_bool(value: object) -> bool | None:
    if isinstance(value, (bool, np.bool_)):
        return bool(value)
    normalized = str(value or "").strip().lower()
    if normalized in {"true", "1", "yes"}:
        return True
    if normalized in {"false", "0", "no"}:
        return False
    return None


def _finite_or_none(value: object) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _timestamp_or_none(value: object) -> pd.Timestamp | None:
    timestamp = pd.to_datetime(value, utc=True, errors="coerce")
    return None if pd.isna(timestamp) else pd.Timestamp(timestamp)


def _utc(value: object, label: str) -> pd.Timestamp:
    timestamp = pd.to_datetime(value, utc=True, errors="coerce")
    if pd.isna(timestamp):
        raise ValueError(f"Invalid {label}")
    return pd.Timestamp(timestamp)


__all__ = [
    "CausalSampleBatch",
    "build_causal_samples",
    "build_live_prediction_inputs",
    "canonicalize_predictions",
    "completed_bar_close",
    "evaluate_offline_predictions",
    "interpolate_lagged_iv_surface",
    "model_feature_frame",
    "reconcile_predictions",
    "select_strictly_earlier_snapshot",
]
