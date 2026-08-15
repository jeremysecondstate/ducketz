from __future__ import annotations

import json
import math
from datetime import datetime
from pathlib import Path
from typing import Any, Mapping

import pandas as pd

from datafetching.decision_time import DecisionClock
from datafetching.ids import (
    ID_COLUMN,
    add_readable_id,
    without_internal_identity_columns,
)
from datafetching.layout import safe_token
from datafetching.runtime_lock import exclusive_runtime_lock
from options import OptionSnapshotOutput
from options.features import (
    calculate_option_snapshot_features,
    load_realized_volatility_evidence,
)
from options.publication import (
    option_writer_lock_path,
    publish_option_snapshot,
)

LEGACY_OPTION_CHAIN_SCHEMA_VERSION = "1.1.0"
OPTION_CHAIN_SCHEMA_VERSION = "option-market-evidence-v2"
_SNAPSHOT_KEY = ("symbol", "snapshot_for", "available_at")
_CONTRACT_KEY = (*_SNAPSHOT_KEY, "contract_symbol")


def persist_schwab_option_snapshot(
    datastore_root: Path,
    *,
    symbol: str,
    payload: Mapping[str, Any],
    clock: DecisionClock,
    fetched_at: datetime | pd.Timestamp | None = None,
    quote_cutoff_at: datetime | pd.Timestamp | None = None,
    regime_available_not_after: datetime | pd.Timestamp | None = None,
    pricing_barrier: Mapping[str, object] | None = None,
    receipt_published_at: datetime | pd.Timestamp | None = None,
    capture_provenance: Mapping[str, object] | None = None,
    update_legacy_monthly_mirrors: bool = True,
    acquire_writer_lock: bool = True,
) -> OptionSnapshotOutput:
    if acquire_writer_lock:
        with exclusive_runtime_lock(
            option_writer_lock_path(datastore_root),
            process_name="Duckets Options writer",
        ):
            return _persist_schwab_option_snapshot(
                datastore_root,
                symbol=symbol,
                payload=payload,
                clock=clock,
                fetched_at=fetched_at,
                quote_cutoff_at=quote_cutoff_at,
                regime_available_not_after=regime_available_not_after,
                pricing_barrier=pricing_barrier,
                receipt_published_at=receipt_published_at,
                capture_provenance=capture_provenance,
                update_legacy_monthly_mirrors=update_legacy_monthly_mirrors,
            )
    return _persist_schwab_option_snapshot(
        datastore_root,
        symbol=symbol,
        payload=payload,
        clock=clock,
        fetched_at=fetched_at,
        quote_cutoff_at=quote_cutoff_at,
        regime_available_not_after=regime_available_not_after,
        pricing_barrier=pricing_barrier,
        receipt_published_at=receipt_published_at,
        capture_provenance=capture_provenance,
        update_legacy_monthly_mirrors=update_legacy_monthly_mirrors,
    )


def persist_provider_option_snapshot(
    datastore_root: Path,
    *,
    provider: str,
    dataset: str,
    symbol: str,
    raw: pd.DataFrame,
    contracts: pd.DataFrame,
    features: pd.DataFrame,
    request_started_at: datetime | pd.Timestamp | None = None,
    pricing_barrier: Mapping[str, object] | None = None,
    receipt_published_at: datetime | pd.Timestamp | None = None,
    acquire_writer_lock: bool = True,
):
    """Commit already-normalized OPRA or Schwab evidence through one contract."""

    def commit():
        return publish_option_snapshot(
            datastore_root,
            provider=provider,
            dataset=dataset,
            symbol=symbol,
            raw=raw,
            contracts=contracts,
            features=features,
            request_started_at=request_started_at,
            pricing_barrier=pricing_barrier,
            receipt_published_at=receipt_published_at,
        )

    if not acquire_writer_lock:
        return commit()
    with exclusive_runtime_lock(
        option_writer_lock_path(datastore_root),
        process_name="Duckets Options writer",
    ):
        return commit()


def normalize_databento_opra_option_snapshot(
    quotes: pd.DataFrame,
    definitions: pd.DataFrame,
    *,
    symbol: str,
    target_snapshot_for: object,
    received_at: object,
    dataset: str = "OPRA.PILLAR",
    schema: str = "cbbo-1s",
    maximum_quote_staleness_seconds: int = 20 * 60,
) -> pd.DataFrame:
    """Normalize injected OPRA L1 rows without making a provider request."""

    clean_symbol = str(symbol).strip().upper()
    target = _as_utc_timestamp(pd.Timestamp(target_snapshot_for))
    available = _as_utc_timestamp(pd.Timestamp(received_at))
    if str(dataset).strip().upper() != "OPRA.PILLAR":
        raise ValueError("Prospective OPRA evidence must use OPRA.PILLAR")
    if str(schema).strip().lower() != "cbbo-1s":
        raise ValueError("Prospective OPRA evidence must use cbbo-1s")
    if maximum_quote_staleness_seconds < 0:
        raise ValueError("OPRA quote staleness bound cannot be negative")
    if available < target:
        raise ValueError("OPRA receipt cannot predate its target")
    required_quotes = {
        "provider",
        "dataset",
        "source_schema",
        "symbol",
        "target_snapshot_for",
        "contract_symbol",
        "quote_timestamp",
        "market_event_timestamp",
        "provider_interval_end_at",
        "provider_received_at",
        "provider_sent_at",
        "local_received_at",
        "publisher_id",
        "bid",
        "ask",
    }
    required_definitions = {
        "provider",
        "dataset",
        "source_schema",
        "target_snapshot_for",
        "contract_symbol",
        "symbol",
        "expiration_date",
        "call_put",
        "strike",
        "multiplier",
        "standard_contract",
        "definition_active",
        "definition_effective_at",
        "definition_activation_at",
        "definition_market_event_at",
        "definition_provider_received_at",
        "definition_provider_sent_at",
        "definition_local_received_at",
        "exercise_style",
        "settlement_type",
        "contract_semantics_source",
        "cfi",
        "security_type",
        "publisher_id",
    }
    if missing := sorted(required_quotes.difference(quotes.columns)):
        raise ValueError("OPRA quotes are missing: " + ", ".join(missing))
    if missing := sorted(required_definitions.difference(definitions.columns)):
        raise ValueError("OPRA definitions are missing: " + ", ".join(missing))
    _validate_opra_frame_identity(
        quotes,
        symbol=clean_symbol,
        target_snapshot_for=target,
        schema="cbbo-1s",
        label="quote",
    )
    _validate_opra_frame_identity(
        definitions,
        symbol=clean_symbol,
        target_snapshot_for=target,
        schema="definition",
        label="definition",
    )
    _reject_divergent_opra_duplicates(
        quotes,
        keys=("contract_symbol", "quote_timestamp"),
        label="quote",
        ignored_columns=("local_received_at", "provider_sent_at"),
    )
    _reject_divergent_opra_duplicates(
        definitions,
        keys=("contract_symbol", "definition_effective_at"),
        label="definition",
        ignored_columns=(
            "definition_local_received_at",
            "definition_provider_sent_at",
        ),
    )
    definition_rows = definitions.loc[
        definitions["symbol"].astype("string").str.upper().eq(clean_symbol)
    ].copy()
    definition_rows["definition_effective_at"] = pd.to_datetime(
        definition_rows["definition_effective_at"], utc=True, errors="coerce"
    )
    definition_market_event = _opra_timestamp_column(
        definition_rows,
        "definition_market_event_at",
        default=definition_rows["definition_effective_at"],
    )
    definition_activation = _opra_timestamp_column(
        definition_rows,
        "definition_activation_at",
        default=pd.Series(pd.NaT, index=definition_rows.index),
    )
    definition_provider_received = _opra_timestamp_column(
        definition_rows,
        "definition_provider_received_at",
        default=definition_rows["definition_effective_at"],
    )
    definition_provider_sent = _opra_timestamp_column(
        definition_rows,
        "definition_provider_sent_at",
        default=definition_provider_received,
    )
    definition_local_received = _opra_timestamp_column(
        definition_rows,
        "definition_local_received_at",
        default=pd.Series(available, index=definition_rows.index),
    )
    definition_active = (
        definition_rows["definition_active"].fillna(False).astype(bool)
        if "definition_active" in definition_rows
        else pd.Series(True, index=definition_rows.index)
    )
    definition_expiration = pd.to_datetime(
        definition_rows["expiration_date"], utc=True, errors="coerce"
    )
    definition_strike = pd.to_numeric(definition_rows["strike"], errors="coerce")
    definition_multiplier = pd.to_numeric(
        definition_rows["multiplier"], errors="coerce"
    )
    definition_call_put = (
        definition_rows["call_put"].astype("string").str.strip().str.upper()
    )
    definition_standard = definition_rows["standard_contract"].fillna(False).astype(bool)
    definition_exercise = (
        definition_rows["exercise_style"].astype("string").str.strip().str.upper()
    )
    definition_settlement = (
        definition_rows["settlement_type"].astype("string").str.strip().str.upper()
    )
    definition_security_type = (
        definition_rows["security_type"].astype("string").str.strip().str.upper()
    )
    definition_publisher = pd.to_numeric(
        definition_rows["publisher_id"], errors="coerce"
    )
    definition_cfi = definition_rows["cfi"].astype("string").str.strip().str.upper()
    cfi_call_put = definition_cfi.str[1].map({"C": "CALL", "P": "PUT"})
    cfi_exercise = definition_cfi.str[2].map(
        {"A": "AMERICAN", "E": "EUROPEAN", "B": "BERMUDAN"}
    )
    cfi_settlement = definition_cfi.str[4].map(
        {
            "P": "PHYSICAL",
            "C": "CASH",
            "N": "NON_DELIVERABLE",
            "E": "ELECT_AT_EXERCISE",
        }
    )
    definition_rows = (
        definition_rows.loc[
            definition_rows["definition_effective_at"].notna()
            & definition_rows["definition_effective_at"].le(target)
            & definition_activation.notna()
            & definition_activation.le(target)
            & definition_market_event.notna()
            & definition_market_event.le(definition_provider_received)
            & definition_provider_received.notna()
            & definition_provider_sent.notna()
            & definition_local_received.notna()
            & definition_provider_received.le(definition_provider_sent)
            & definition_provider_sent.le(
                definition_local_received + pd.Timedelta(seconds=5)
            )
            & definition_local_received.le(available)
            & definition_active
            & definition_expiration.notna()
            & definition_expiration.gt(target.normalize())
            & definition_strike.gt(0.0)
            & definition_multiplier.eq(100.0)
            & definition_call_put.isin(("CALL", "PUT"))
            & definition_standard
            & definition_exercise.isin(("AMERICAN", "EUROPEAN"))
            & definition_settlement.isin(
                ("PHYSICAL", "CASH", "NON_DELIVERABLE", "ELECT_AT_EXERCISE")
            )
            & definition_security_type.eq("OPT")
            & definition_publisher.eq(30)
            & definition_cfi.str.fullmatch(r"O[CP][AEB]S[PCNE]S", na=False)
            & cfi_call_put.eq(definition_call_put)
            & cfi_exercise.eq(definition_exercise)
            & cfi_settlement.eq(definition_settlement)
        ]
        .sort_values("definition_effective_at", kind="stable")
        .drop_duplicates("contract_symbol", keep="last")
    )
    merged = quotes.merge(
        definition_rows,
        on="contract_symbol",
        how="inner",
        validate="many_to_one",
        suffixes=("", "_definition"),
    )
    merged["quote_timestamp"] = pd.to_datetime(
        merged["quote_timestamp"], utc=True, errors="coerce"
    )
    market_event = _opra_timestamp_column(
        merged,
        "market_event_timestamp",
        default=merged["quote_timestamp"],
    )
    provider_received = _opra_timestamp_column(
        merged,
        "provider_received_at",
        default=merged["quote_timestamp"],
    )
    provider_sent = _opra_timestamp_column(
        merged,
        "provider_sent_at",
        default=provider_received,
    )
    provider_interval_end = _opra_timestamp_column(
        merged,
        "provider_interval_end_at",
        default=provider_received,
    )
    local_received = _opra_timestamp_column(
        merged,
        "local_received_at",
        default=pd.Series(available, index=merged.index),
    )
    bid = pd.to_numeric(merged["bid"], errors="coerce")
    ask = pd.to_numeric(merged["ask"], errors="coerce")
    staleness = (target - merged["quote_timestamp"]).dt.total_seconds()
    publisher_valid = (
        pd.to_numeric(merged["publisher_id"], errors="coerce").eq(30)
        if "publisher_id" in merged
        else pd.Series(True, index=merged.index)
    )
    valid = (
        bid.gt(0.0)
        & ask.gt(bid)
        & staleness.between(0.0, float(maximum_quote_staleness_seconds))
        & market_event.notna()
        & market_event.eq(merged["quote_timestamp"])
        & market_event.lt(target)
        & provider_interval_end.notna()
        & market_event.lt(provider_interval_end)
        & provider_interval_end.eq(provider_received)
        & provider_interval_end.le(target)
        & provider_received.notna()
        & provider_sent.notna()
        & local_received.notna()
        & market_event.le(provider_received)
        & provider_received.le(provider_sent)
        & provider_sent.le(local_received + pd.Timedelta(seconds=5))
        & local_received.le(available)
        & publisher_valid
    )
    merged = (
        merged.loc[
            merged["quote_timestamp"].notna()
            & merged["quote_timestamp"].lt(target)
            & valid
        ]
        .sort_values("quote_timestamp", kind="stable")
        .drop_duplicates("contract_symbol", keep="last")
    )
    bid = pd.to_numeric(merged["bid"], errors="coerce")
    ask = pd.to_numeric(merged["ask"], errors="coerce")
    valid = bid.gt(0.0) & ask.gt(bid)
    market_event = market_event.loc[merged.index]
    provider_received = provider_received.loc[merged.index]
    provider_sent = provider_sent.loc[merged.index]
    local_received = local_received.loc[merged.index]
    definition_at = pd.to_datetime(
        merged.get("definition_effective_at"), utc=True, errors="coerce"
    )
    exercise = merged.get(
        "exercise_style", pd.Series(pd.NA, index=merged.index, dtype="string")
    )
    exercise_known = (
        exercise.astype("string").str.strip().str.upper().isin(("AMERICAN", "EUROPEAN"))
    )
    settlement = merged.get(
        "settlement_type", pd.Series(pd.NA, index=merged.index, dtype="string")
    )
    settlement_known = settlement.astype("string").str.strip().ne("") & settlement.notna()
    output = pd.DataFrame(
        {
            "provider": "databento-opra",
            "dataset": dataset,
            "source_schema": schema,
            "source": "databento-opra",
            "symbol": clean_symbol,
            "underlying_symbol": clean_symbol,
            "contract_symbol": merged["contract_symbol"].astype("string"),
            "target_snapshot_for": target,
            "snapshot_for": target,
            "first_available_at": available,
            "available_at": available,
            "fetched_at": available,
            "provider_ingested_at": provider_received,
            "event_timestamp": market_event,
            "quote_timestamp": merged["quote_timestamp"],
            "market_event_timestamp": market_event,
            "market_event_clock_status": merged.get("market_event_clock_status"),
            "provider_interval_end_at": pd.to_datetime(
                merged.get("provider_interval_end_at"), utc=True, errors="coerce"
            ),
            "provider_received_at": provider_received,
            "provider_receipt_clock_status": merged.get(
                "provider_receipt_clock_status"
            ),
            "provider_sent_at": provider_sent,
            "local_received_at": local_received,
            "last_trade_event_at": pd.to_datetime(
                merged.get("last_trade_event_at"), utc=True, errors="coerce"
            ),
            "bid": bid,
            "ask": ask,
            "midpoint": (bid + ask) / 2.0,
            "bid_size": pd.to_numeric(merged.get("bid_size"), errors="coerce"),
            "ask_size": pd.to_numeric(merged.get("ask_size"), errors="coerce"),
            "trade_price": pd.to_numeric(
                merged.get("trade_price"), errors="coerce"
            ),
            "trade_size": pd.to_numeric(
                merged.get("trade_size"), errors="coerce"
            ),
            "underlying_price": pd.to_numeric(
                merged.get("underlying_price"), errors="coerce"
            ),
            "interest_rate": pd.to_numeric(
                merged.get("interest_rate"), errors="coerce"
            ),
            "dividend_yield": pd.to_numeric(
                merged.get("dividend_yield"), errors="coerce"
            ),
            "implied_volatility": pd.to_numeric(
                merged.get("implied_volatility"), errors="coerce"
            ),
            "volume": pd.to_numeric(merged.get("volume"), errors="coerce"),
            "open_interest": pd.to_numeric(
                merged.get("open_interest"), errors="coerce"
            ),
            "strike": pd.to_numeric(merged["strike"], errors="coerce"),
            "expiration_date": pd.to_datetime(
                merged["expiration_date"], utc=True, errors="coerce"
            ),
            "call_put": merged["call_put"].astype("string").str.upper(),
            "multiplier": pd.to_numeric(merged["multiplier"], errors="coerce"),
            "standard_contract": merged["standard_contract"].fillna(False).astype(bool),
            "mini": False,
            "non_standard": ~merged["standard_contract"].fillna(False).astype(bool),
            "adjusted": ~merged["standard_contract"].fillna(False).astype(bool),
            "publisher_id": merged.get("publisher_id"),
            "venue": merged.get("venue"),
            "quote_staleness_seconds": staleness.loc[merged.index],
            "quote_valid": valid,
            "quote_quality_status": valid.map(
                {True: "VALID", False: "REJECTED"}
            ),
            "definition_as_of": definition_at,
            "exercise_style": exercise,
            "exercise_style_status": exercise_known.map(
                {True: "POINT_IN_TIME_REFERENCE", False: "AMBIGUOUS_UNVERIFIED"}
            ),
            "settlement_type": settlement,
            "settlement_status": settlement_known.map(
                {True: "POINT_IN_TIME_REFERENCE", False: "AMBIGUOUS_UNVERIFIED"}
            ),
            "settlement_reference": merged.get("settlement_reference"),
            "contract_semantics_source": merged.get("contract_semantics_source"),
            "cfi": merged.get("cfi"),
            "security_type": merged.get("security_type"),
            "definition_market_event_at": pd.to_datetime(
                merged.get("definition_market_event_at"), utc=True, errors="coerce"
            ),
            "definition_activation_at": pd.to_datetime(
                merged.get("definition_activation_at"), utc=True, errors="coerce"
            ),
            "definition_provider_received_at": pd.to_datetime(
                merged.get("definition_provider_received_at"),
                utc=True,
                errors="coerce",
            ),
            "definition_provider_sent_at": pd.to_datetime(
                merged.get("definition_provider_sent_at"), utc=True, errors="coerce"
            ),
            "definition_local_received_at": pd.to_datetime(
                merged.get("definition_local_received_at"), utc=True, errors="coerce"
            ),
            "evidence_lane": "PROSPECTIVE_OPRA",
            "fallback_used": False,
            "source_file": merged.get("source_file"),
            "source_checksum_sha256": merged.get("source_checksum_sha256"),
            "schema_version": OPTION_CHAIN_SCHEMA_VERSION,
            "policy_version": "provider-neutral-option-snapshot-v2",
        }
    )
    if output.empty:
        raise RuntimeError(f"OPRA snapshot returned no contracts for {clean_symbol}")
    return output.reset_index(drop=True)


def _validate_opra_frame_identity(
    frame: pd.DataFrame,
    *,
    symbol: str,
    target_snapshot_for: pd.Timestamp,
    schema: str,
    label: str,
) -> None:
    if frame.empty:
        raise ValueError(f"OPRA {label} evidence is empty")
    expected = {
        "provider": "databento-opra",
        "dataset": "OPRA.PILLAR",
        "source_schema": schema,
    }
    for column, required in expected.items():
        values = frame[column]
        if values.isna().any():
            raise ValueError(f"OPRA {label} rows have mismatched {column}")
        observed = {
            str(value).strip().lower() if column != "dataset" else str(value).strip().upper()
            for value in values.unique()
        }
        comparison = required.lower() if column != "dataset" else required.upper()
        if observed != {comparison}:
            raise ValueError(f"OPRA {label} rows have mismatched {column}")
    for column in ("symbol", "underlying_symbol"):
        if column in frame:
            if frame[column].isna().any():
                raise ValueError(f"OPRA {label} rows have mismatched symbol")
            observed_symbols = {
                str(value).strip().upper() for value in frame[column].unique()
            }
            if observed_symbols != {symbol}:
                raise ValueError(f"OPRA {label} rows have mismatched symbol")
    for column in ("target_snapshot_for", "snapshot_for"):
        if column not in frame:
            continue
        observed_targets = pd.to_datetime(frame[column], utc=True, errors="coerce")
        if observed_targets.isna().any() or not observed_targets.eq(target_snapshot_for).all():
            raise ValueError(f"OPRA {label} rows have mismatched target")


def _reject_divergent_opra_duplicates(
    frame: pd.DataFrame,
    *,
    keys: tuple[str, ...],
    label: str,
    ignored_columns: tuple[str, ...] = (),
) -> None:
    if frame.empty or not frame.duplicated(list(keys), keep=False).any():
        return
    semantic_columns = [
        column
        for column in sorted(frame.columns)
        if column not in set(ignored_columns)
    ]
    duplicates = frame.loc[frame.duplicated(list(keys), keep=False)]
    for _key, group in duplicates.groupby(list(keys), dropna=False, sort=False):
        normalized = group.loc[:, semantic_columns].astype("string").fillna("<NA>")
        if len(normalized.drop_duplicates()) > 1:
            raise ValueError(f"Divergent duplicate OPRA {label} evidence is forbidden")


def _opra_timestamp_column(
    frame: pd.DataFrame,
    column: str,
    *,
    default: pd.Series,
) -> pd.Series:
    values = frame[column] if column in frame else default
    return pd.to_datetime(values, utc=True, errors="coerce")


def _persist_schwab_option_snapshot(
    datastore_root: Path,
    *,
    symbol: str,
    payload: Mapping[str, Any],
    clock: DecisionClock,
    fetched_at: datetime | pd.Timestamp | None = None,
    quote_cutoff_at: datetime | pd.Timestamp | None = None,
    regime_available_not_after: datetime | pd.Timestamp | None = None,
    pricing_barrier: Mapping[str, object] | None = None,
    receipt_published_at: datetime | pd.Timestamp | None = None,
    capture_provenance: Mapping[str, object] | None = None,
    update_legacy_monthly_mirrors: bool = True,
) -> OptionSnapshotOutput:
    observed_at = _as_utc_timestamp(fetched_at)
    cutoff_at = (
        _as_utc_timestamp(quote_cutoff_at)
        if quote_cutoff_at is not None
        else observed_at
    )
    if cutoff_at > observed_at:
        raise ValueError("Option quote cutoff cannot follow local receipt availability")
    contracts = normalize_schwab_option_chain(
        payload,
        symbol=symbol,
        clock=clock,
        fetched_at=observed_at,
        quote_cutoff_at=cutoff_at,
    )
    _validate_receipt_keys(contracts, keys=_CONTRACT_KEY, label="input")
    regime_cutoff = (
        _as_utc_timestamp(regime_available_not_after)
        if regime_available_not_after is not None
        else cutoff_at
    )
    volatility_evidence = load_realized_volatility_evidence(
        datastore_root,
        symbol=symbol,
        as_of=regime_cutoff,
    )
    features = calculate_option_snapshot_features(
        contracts,
        realized_volatility_evidence=volatility_evidence,
    )
    features["realized_volatility_evidence_cutoff_at"] = regime_cutoff

    snapshot_for = _as_utc_timestamp(clock.decision_timestamp)
    month = observed_at.strftime("%Y-%m")
    root = Path(datastore_root) / "stocks" / safe_token(symbol.strip().upper()) / "options"
    contracts_path = root / "chains" / "schwab" / "normalized" / f"{month}.parquet"
    raw_path = root / "chains" / "schwab" / "raw" / f"{month}.parquet"
    features_path = root / "features" / "option-quality" / "schwab" / f"{month}.parquet"

    raw = pd.DataFrame(
        [
            {
                "symbol": symbol.strip().upper(),
                "source": "schwab",
                "provider": "schwab",
                "dataset": "SCHWAB_CHAIN",
                "snapshot_for": snapshot_for,
                "target_snapshot_for": snapshot_for,
                "decision_timestamp": snapshot_for,
                "decision_bar_timestamp": _as_utc_timestamp(clock.bar_timestamp),
                "decision_provider": clock.provider,
                "decision_timeframe": clock.timeframe,
                "decision_source_file": str(clock.source_file),
                "quote_cutoff_at": cutoff_at,
                "realized_volatility_evidence_cutoff_at": regime_cutoff,
                "underlying_quote_timestamp": _underlying_quote_timestamp(payload),
                "fetched_at": observed_at,
                "available_at": observed_at,
                "first_available_at": observed_at,
                "provider_ingested_at": observed_at,
                "payload_json": json.dumps(payload, default=str, sort_keys=True),
                "capture_provenance_json": (
                    json.dumps(
                        dict(capture_provenance),
                        default=str,
                        sort_keys=True,
                    )
                    if capture_provenance is not None
                    else None
                ),
                "response_received_at": (
                    _as_utc_timestamp(capture_provenance["response_received_at"])
                    if capture_provenance is not None
                    and capture_provenance.get("response_received_at") is not None
                    else observed_at
                ),
                "schema_version": OPTION_CHAIN_SCHEMA_VERSION,
                "policy_version": "provider-neutral-option-snapshot-v2",
            }
        ]
    )
    # The immutable receipt is the authoritative generation boundary.  Commit it
    # before updating the legacy monthly mirrors so a first-ever publication can
    # never expose a mixture of monthly files to receipt-aware readers.
    committed = publish_option_snapshot(
        datastore_root,
        symbol=symbol,
        raw=raw,
        contracts=contracts,
        features=features,
        provider="schwab",
        dataset="SCHWAB_CHAIN",
        request_started_at=cutoff_at,
        pricing_barrier=pricing_barrier,
        receipt_published_at=receipt_published_at,
    )
    if update_legacy_monthly_mirrors:
        _atomic_upsert(raw_path, raw, keys=_SNAPSHOT_KEY)
        _atomic_upsert(contracts_path, contracts, keys=_CONTRACT_KEY)
        _atomic_upsert(features_path, features, keys=_SNAPSHOT_KEY)
    return OptionSnapshotOutput(
        contracts_path,
        features_path,
        raw_path,
        len(contracts),
        committed.receipt_path,
        committed.directory,
    )


def normalize_schwab_option_chain(
    payload: Mapping[str, Any],
    *,
    symbol: str,
    clock: DecisionClock,
    fetched_at: datetime | pd.Timestamp | None = None,
    quote_cutoff_at: datetime | pd.Timestamp | None = None,
) -> pd.DataFrame:
    if not isinstance(payload, Mapping):
        raise TypeError("Schwab option-chain payload must be a mapping.")

    observed_at = _as_utc_timestamp(fetched_at)
    cutoff_at = (
        _as_utc_timestamp(quote_cutoff_at)
        if quote_cutoff_at is not None
        else observed_at
    )
    if cutoff_at > observed_at:
        raise ValueError("Option quote cutoff cannot follow local receipt availability")
    snapshot_for = _as_utc_timestamp(clock.decision_timestamp)
    clean_symbol = symbol.strip().upper()
    underlying = payload.get("underlying")
    underlying = underlying if isinstance(underlying, Mapping) else {}
    underlying_price = _first_number(
        payload,
        ("underlyingPrice",),
        fallback=underlying,
        fallback_keys=("mark", "last", "lastPrice", "close"),
    )
    underlying_quote_timestamp = _underlying_quote_timestamp(payload)
    interest_rate = _decimal_rate(_number(payload.get("interestRate")))
    dividend_yield = _decimal_rate(
        _first_number(payload, ("dividendYield",), fallback=underlying, fallback_keys=("divYield",))
    )

    rows: list[dict[str, Any]] = []
    for call_put, map_name in (("CALL", "callExpDateMap"), ("PUT", "putExpDateMap")):
        expiration_map = payload.get(map_name)
        if not isinstance(expiration_map, Mapping):
            continue
        for expiration_key, strikes in expiration_map.items():
            if not isinstance(strikes, Mapping):
                continue
            map_expiration, map_dte = _expiration_parts(expiration_key)
            for strike_key, contracts in strikes.items():
                if not isinstance(contracts, list):
                    continue
                for contract in contracts:
                    if not isinstance(contract, Mapping):
                        continue
                    strike = _number(contract.get("strikePrice")) or _number(strike_key)
                    expiration = _date_value(contract.get("expirationDate")) or map_expiration
                    dte = _number(contract.get("daysToExpiration"))
                    dte = map_dte if dte is None else dte
                    bid = _number(contract.get("bid"))
                    ask = _number(contract.get("ask"))
                    mark = _number(contract.get("mark"))
                    mark = (bid + ask) / 2.0 if mark is None and bid is not None and ask is not None else mark
                    contract_underlying = _number(contract.get("underlyingPrice")) or underlying_price
                    intrinsic = _number(contract.get("intrinsicValue"))
                    if intrinsic is None and strike is not None and contract_underlying is not None:
                        intrinsic = max(
                            contract_underlying - strike if call_put == "CALL" else strike - contract_underlying,
                            0.0,
                        )
                    quote_timestamp = _epoch_timestamp(contract.get("quoteTimeInLong") or contract.get("quoteTime"))
                    trade_timestamp = _epoch_timestamp(contract.get("tradeTimeInLong") or contract.get("tradeTime"))
                    quote_after_cutoff = (
                        not pd.isna(quote_timestamp)
                        and quote_timestamp > cutoff_at
                    )
                    quote_crossed = (
                        bid is not None
                        and ask is not None
                        and ask < bid
                    )
                    quote_locked = (
                        bid is not None
                        and ask is not None
                        and ask == bid
                    )
                    quote_mid = (
                        (bid + ask) / 2.0
                        if bid is not None and ask is not None
                        else None
                    )
                    quote_mid_nonpositive = (
                        quote_mid is not None and quote_mid <= 0
                    )
                    rows.append(
                        {
                            "symbol": clean_symbol,
                            "source": "schwab",
                            "provider": "schwab",
                            "dataset": "SCHWAB_CHAIN",
                            "underlying_symbol": clean_symbol,
                            "snapshot_for": snapshot_for,
                            "target_snapshot_for": snapshot_for,
                            "decision_timestamp": snapshot_for,
                            "decision_bar_timestamp": _as_utc_timestamp(clock.bar_timestamp),
                            "decision_provider": clock.provider,
                            "decision_timeframe": clock.timeframe,
                            "decision_source_file": str(clock.source_file),
                            "quote_cutoff_at": cutoff_at,
                            "underlying_quote_timestamp": underlying_quote_timestamp,
                            "fetched_at": observed_at,
                            "available_at": observed_at,
                            "first_available_at": observed_at,
                            "provider_ingested_at": observed_at,
                            "decision_lag_seconds": max(
                                0.0,
                                (observed_at - snapshot_for).total_seconds(),
                            ),
                            "contract_symbol": str(contract.get("symbol") or "").strip(),
                            "description": str(contract.get("description") or "").strip(),
                            "call_put": call_put,
                            "expiration_date": expiration,
                            "days_to_expiration": dte,
                            "strike": strike,
                            "underlying_price": contract_underlying,
                            "bid": bid,
                            "ask": ask,
                            "midpoint": quote_mid,
                            "mark": mark,
                            "last": _number(contract.get("last")),
                            "trade_price": _number(contract.get("last")),
                            "trade_size": _number(
                                contract.get("lastSize") or contract.get("tradeSize")
                            ),
                            "bid_size": _number(contract.get("bidSize")),
                            "ask_size": _number(contract.get("askSize")),
                            "volume": _number(contract.get("totalVolume") or contract.get("volume")),
                            "open_interest": _number(contract.get("openInterest")),
                            "implied_volatility": _decimal_volatility(
                                _number(contract.get("volatility") or contract.get("impliedVolatility"))
                            ),
                            "delta": _number(contract.get("delta")),
                            "gamma": _number(contract.get("gamma")),
                            "theta": _number(contract.get("theta")),
                            "vega": _number(contract.get("vega")),
                            "rho": _number(contract.get("rho")),
                            "theoretical_value": _number(contract.get("theoreticalOptionValue")),
                            "intrinsic_value": intrinsic,
                            "time_value": None if mark is None or intrinsic is None else mark - intrinsic,
                            "in_the_money": _boolean(contract.get("inTheMoney")),
                            "mini": _boolean(contract.get("mini")),
                            "non_standard": _boolean(contract.get("nonStandard")),
                            "adjusted": _boolean(contract.get("nonStandard")),
                            "standard_contract": not bool(
                                _boolean(contract.get("mini"))
                                or _boolean(contract.get("nonStandard"))
                            ),
                            "multiplier": _number(contract.get("multiplier")) or 100.0,
                            "exercise_style": str(
                                contract.get("exerciseStyle") or ""
                            ).strip().upper()
                            or None,
                            "exercise_style_status": (
                                "POINT_IN_TIME_REFERENCE"
                                if contract.get("exerciseStyle")
                                else "AMBIGUOUS_UNVERIFIED"
                            ),
                            "settlement_type": str(contract.get("settlementType") or "").strip(),
                            "settlement_reference": str(
                                contract.get("settlementReference") or ""
                            ).strip()
                            or None,
                            "expiration_type": str(contract.get("expirationType") or "").strip(),
                            "interest_rate": interest_rate,
                            "dividend_yield": dividend_yield,
                            "quote_timestamp": quote_timestamp,
                            "event_timestamp": quote_timestamp,
                            "trade_timestamp": trade_timestamp,
                            "quote_staleness_seconds": _staleness(observed_at, quote_timestamp),
                            "quote_after_cutoff": quote_after_cutoff,
                            "underlying_quote_after_cutoff": (
                                not pd.isna(underlying_quote_timestamp)
                                and underlying_quote_timestamp > cutoff_at
                            ),
                            "quote_crossed": quote_crossed,
                            "quote_locked": quote_locked,
                            "quote_mid_nonpositive": quote_mid_nonpositive,
                            "quote_valid": bool(
                                bid is not None
                                and ask is not None
                                and quote_mid is not None
                                and quote_mid > 0
                                and not quote_crossed
                                and not quote_locked
                                and not quote_after_cutoff
                            ),
                            "quote_quality_status": (
                                "VALID"
                                if bid is not None
                                and ask is not None
                                and quote_mid is not None
                                and quote_mid > 0
                                and not quote_crossed
                                and not quote_locked
                                and not quote_after_cutoff
                                else "REJECTED"
                            ),
                            "definition_as_of": quote_timestamp,
                            "publisher_id": None,
                            "venue": "SCHWAB_AGGREGATED",
                            "evidence_lane": "PROSPECTIVE_SCHWAB",
                            "fallback_used": True,
                            "source_file": None,
                            "source_checksum_sha256": None,
                            "relative_bid_ask_spread": _relative_spread(bid, ask),
                            "schema_version": OPTION_CHAIN_SCHEMA_VERSION,
                            "policy_version": "provider-neutral-option-snapshot-v2",
                        }
                    )

    frame = pd.DataFrame(rows)
    if frame.empty:
        raise RuntimeError(f"Schwab option chain returned no contracts for {clean_symbol}.")
    missing_symbol = frame["contract_symbol"].eq("")
    frame.loc[missing_symbol, "contract_symbol"] = frame.loc[missing_symbol].apply(
        lambda row: f"{clean_symbol}:{row['expiration_date']}:{row['call_put']}:{row['strike']}",
        axis=1,
    )
    frame["expiration_date"] = pd.to_datetime(frame["expiration_date"], utc=True, errors="coerce")
    return frame.sort_values(
        [
            "snapshot_for",
            "available_at",
            "expiration_date",
            "strike",
            "call_put",
            "contract_symbol",
        ]
    ).reset_index(drop=True)


def _atomic_upsert(path: Path, incoming: pd.DataFrame, *, keys: tuple[str, ...]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    existing = (
        _prepare_snapshot_frame(pd.read_parquet(path))
        if path.is_file()
        else pd.DataFrame()
    )
    incoming = _prepare_snapshot_frame(incoming)
    _validate_receipt_keys(incoming, keys=keys, label="input")
    if not existing.empty:
        _validate_receipt_keys(existing, keys=keys, label="history")

    existing_by_key = {
        _receipt_key(row, keys): row
        for row in existing.to_dict("records")
    }
    additions: list[dict[str, Any]] = []
    for row in incoming.to_dict("records"):
        key = _receipt_key(row, keys)
        prior = existing_by_key.get(key)
        if prior is None:
            additions.append(row)
            continue
        if _canonical_record(prior) != _canonical_record(row):
            raise ValueError(
                "Option receipt is immutable and conflicts with an existing row: "
                + "|".join(key)
            )

    if not additions and path.is_file():
        return

    columns = list(dict.fromkeys([*existing.columns, *incoming.columns]))
    additions_frame = pd.DataFrame(additions).reindex(columns=columns)
    output = (
        additions_frame
        if existing.empty
        else pd.concat(
            [existing.reindex(columns=columns), additions_frame],
            ignore_index=True,
            sort=False,
        )
    )
    output = _prepare_snapshot_frame(output)
    output = output.sort_values(list(keys), kind="stable")
    output = add_readable_id(output.reset_index(drop=True), key_columns=keys)
    temporary = path.with_suffix(".tmp.parquet")
    output.reset_index(drop=True).to_parquet(temporary, index=False)
    temporary.replace(path)


def _prepare_snapshot_frame(frame: pd.DataFrame) -> pd.DataFrame:
    output = frame.drop(columns=[ID_COLUMN], errors="ignore").copy()
    output = without_internal_identity_columns(output)
    if "snapshot_for" not in output.columns:
        if "decision_timestamp" in output.columns:
            output["snapshot_for"] = output["decision_timestamp"]
        elif "timestamp" in output.columns:
            output["snapshot_for"] = output["timestamp"]
    if "decision_timestamp" not in output.columns and "snapshot_for" in output.columns:
        output["decision_timestamp"] = output["snapshot_for"]
    if "available_at" not in output.columns and "fetched_at" in output.columns:
        output["available_at"] = output["fetched_at"]
    if "fetched_at" not in output.columns and "available_at" in output.columns:
        output["fetched_at"] = output["available_at"]

    temporal_columns = [
        column
        for column in output.columns
        if column in {"timestamp", "expiration_date"}
        or column.endswith("_timestamp")
        or column.endswith("_at")
        or column == "snapshot_for"
    ]
    for column in temporal_columns:
        output[column] = pd.to_datetime(output[column], utc=True, errors="coerce")
    if "symbol" in output.columns:
        output["symbol"] = output["symbol"].astype("string").str.strip().str.upper()
    return output


def _validate_receipt_keys(
    frame: pd.DataFrame,
    *,
    keys: tuple[str, ...],
    label: str,
) -> None:
    missing = [key for key in keys if key not in frame.columns]
    if missing:
        raise ValueError(
            f"Option receipt {label} is missing natural key columns: "
            + ", ".join(missing)
        )
    if frame.loc[:, list(keys)].isna().any(axis=None):
        raise ValueError(f"Option receipt {label} has missing natural key values")
    if frame.duplicated(list(keys), keep=False).any():
        raise ValueError(f"Option receipt {label} contains duplicate natural keys")


def _receipt_key(
    row: Mapping[str, Any],
    keys: tuple[str, ...],
) -> tuple[str, ...]:
    return tuple(_key_value(row.get(key)) for key in keys)


def _key_value(value: object) -> str:
    if isinstance(value, pd.Timestamp):
        return _as_utc_timestamp(value).isoformat()
    return str(value)


def _canonical_record(row: Mapping[str, Any]) -> str:
    return json.dumps(
        {
            str(key): _json_value(value)
            for key, value in sorted(row.items(), key=lambda item: str(item[0]))
            if str(key) != ID_COLUMN
        },
        sort_keys=True,
        separators=(",", ":"),
    )


def _json_value(value: object) -> object:
    try:
        if pd.isna(value):
            return None
    except (TypeError, ValueError):
        pass
    if isinstance(value, pd.Timestamp):
        return _as_utc_timestamp(value).isoformat()
    if isinstance(value, datetime):
        return _as_utc_timestamp(value).isoformat()
    if isinstance(value, Path):
        return str(value)
    if hasattr(value, "item"):
        try:
            return _json_value(value.item())
        except (TypeError, ValueError):
            pass
    if isinstance(value, Mapping):
        return {
            str(key): _json_value(item)
            for key, item in sorted(value.items(), key=lambda pair: str(pair[0]))
        }
    if isinstance(value, (list, tuple)):
        return [_json_value(item) for item in value]
    return value


def _expiration_parts(value: object) -> tuple[str, float | None]:
    parts = str(value or "").split(":", 1)
    return parts[0].strip(), _number(parts[1]) if len(parts) > 1 else None


def _date_value(value: object) -> str:
    text = str(value or "").strip()
    return text[:10] if len(text) >= 10 else text


def _underlying_quote_timestamp(
    payload: Mapping[str, Any],
) -> pd.Timestamp | pd.NaT:
    underlying = payload.get("underlying")
    underlying = underlying if isinstance(underlying, Mapping) else {}
    for mapping in (underlying, payload):
        for key in (
            "quoteTimeInLong",
            "quoteTime",
            "bidTime",
            "askTime",
            "lastTradeTime",
            "tradeTimeInLong",
            "tradeTime",
        ):
            timestamp = _epoch_timestamp(mapping.get(key))
            if not pd.isna(timestamp):
                return timestamp
    return pd.NaT


def _epoch_timestamp(value: object) -> pd.Timestamp | pd.NaT:
    number = _number(value)
    if number is not None:
        return pd.to_datetime(
            number,
            unit="ms" if abs(number) > 10**11 else "s",
            utc=True,
            errors="coerce",
        )
    text = str(value or "").strip()
    if not text:
        return pd.NaT
    return pd.to_datetime(text, utc=True, errors="coerce")


def _staleness(observed_at: pd.Timestamp, quote_timestamp: pd.Timestamp | pd.NaT) -> float | None:
    return (
        None
        if pd.isna(quote_timestamp)
        else (observed_at - quote_timestamp).total_seconds()
    )


def _relative_spread(bid: float | None, ask: float | None) -> float | None:
    if bid is None or ask is None or ask <= bid:
        return None
    midpoint = (bid + ask) / 2.0
    if midpoint <= 0:
        return None
    return (ask - bid) / midpoint


def _first_number(
    primary: Mapping[str, Any],
    keys: tuple[str, ...],
    *,
    fallback: Mapping[str, Any],
    fallback_keys: tuple[str, ...],
) -> float | None:
    for mapping, candidates in ((primary, keys), (fallback, fallback_keys)):
        for key in candidates:
            value = _number(mapping.get(key))
            if value is not None:
                return value
    return None


def _number(value: object) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _boolean(value: object) -> bool | None:
    if isinstance(value, bool):
        return value
    text = str(value or "").strip().lower()
    if text in {"true", "1", "yes"}:
        return True
    if text in {"false", "0", "no"}:
        return False
    return None


def _decimal_rate(value: float | None) -> float | None:
    return None if value is None else value / 100.0 if abs(value) > 0.20 else value


def _decimal_volatility(value: float | None) -> float | None:
    return None if value is None else value / 100.0 if abs(value) > 3.0 else value


def _as_utc_timestamp(value: datetime | pd.Timestamp | None) -> pd.Timestamp:
    if value is None:
        return pd.Timestamp.now(tz="UTC")
    parsed = pd.Timestamp(value)
    return parsed.tz_localize("UTC") if parsed.tzinfo is None else parsed.tz_convert("UTC")
