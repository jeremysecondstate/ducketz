"""Explicit real-observation alternatives for NEW independent stock targets.

No acquisition, filling or mixed-source returns occurs here. Both endpoints of
each target must pass the original five-minute rule in the first eligible source
in SOURCE_ORDER. Missing observations do not establish that no trades occurred.
Mutable provider payloads are identified by their exact hash at this read; their
original historical versions are not claimed to have been recovered.
"""
from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pandas as pd

from ml.artifacts import file_checksum


SOURCE_ORDER = (
    "databento:XNAS.ITCH:ohlcv-1m",
    "databento:EQUS.MINI:ohlcv-1m",
    "schwab:pricehistory:1m",
    "schwab:pricehistory:5m",
)
FMP_OBSERVATION_SOURCE = "fmp:historical-chart:5min"
SOURCE_ORDER_V2 = (*SOURCE_ORDER, FMP_OBSERVATION_SOURCE)
OBSERVATION_COLUMNS = (
    "observation_source", "observation_dataset", "observation_interval_seconds",
    "observation_input_sha256", "observation_recorded_at",
)


def source_order_for_contract(source_contract):
    """Freeze the original four-feed policy; FMP is only part of version two."""
    from ml.stock_target_prices import OBSERVED_MULTI_STOCK_PRICE_SOURCE_V2
    return SOURCE_ORDER_V2 if source_contract == OBSERVED_MULTI_STOCK_PRICE_SOURCE_V2 else SOURCE_ORDER


def genuine_observation_mask(frame):
    """Check explicit provider/native markers before projecting columns away."""
    valid = pd.Series(True, index=frame.index)
    for column in ("synthetic", "is_synthetic", "no_trade", "is_no_trade", "source_empty", "is_source_empty"):
        if column in frame:
            valid &= frame[column].map(lambda value: isinstance(value, (bool, np.bool_)) and not bool(value))
    return valid


def select_observation_revisions(bars):
    """Choose latest eligible dated provider revisions, exposing ambiguity.

    The caller first applies its actual evidence cutoff. Unknown or simultaneous
    conflicting source observations remain unavailable, never arbitrarily chosen.
    """
    identity = ["symbol", "timestamp", *(["observation_source"] if "observation_source" in bars else [])]
    result = bars.copy()
    if "observation_source" in result and "observation_recorded_at" in result:
        recorded = pd.to_datetime(result.observation_recorded_at, utc=True, errors="coerce")
        revisable = result.observation_source.str.startswith(("schwab:pricehistory:", "fmp:historical-chart:")) & recorded.notna()
        result = result.assign(_revision_clock=recorded)
        latest = result.groupby(identity, dropna=False)._revision_clock.transform('max')
        result = result.loc[~revisable | recorded.eq(latest)].drop(columns='_revision_clock')
    result = result.drop_duplicates([*identity, "open", "close"])
    conflict = result.duplicated(identity, keep=False)
    return result.loc[~conflict], int(conflict.sum())


def completed_observation_bars(prices, now, *, evidence_available_at=None):
    """Bound market events and receipt availability by their separate clocks.

    A retrospective review may receive completed-session observations after
    that session closes. Its outcome horizon stays frozen at ``now`` while
    dated receipts are admitted only through the actual review clock. Ordinary
    consumers retain the single-clock behavior when no separate clock is given.
    """
    now = pd.Timestamp(now)
    evidence_cutoff = pd.Timestamp(now if evidence_available_at is None else evidence_available_at)
    if (pd.isna(now) or now.tzinfo is None or pd.isna(evidence_cutoff)
            or evidence_cutoff.tzinfo is None or evidence_cutoff < now):
        raise ValueError("Observation event and evidence cutoffs must be aware and correctly ordered")
    if not genuine_observation_mask(prices).all():
        raise ValueError("Observed prices contain synthetic/no-trade or nonboolean native markers")
    bars = prices.copy()
    bars["symbol"] = bars.symbol.astype(str).str.upper()
    bars["timestamp"] = pd.to_datetime(bars.timestamp, utc=True, errors="raise")
    seconds = bars.get("observation_interval_seconds", pd.Series(60, index=bars.index))
    ends = bars.timestamp.add(pd.to_timedelta(seconds, unit="s"))
    eligible = ends.le(now)
    if "observation_recorded_at" in bars:
        recorded = pd.to_datetime(bars.observation_recorded_at, utc=True, errors="coerce")
        eligible &= recorded.isna() | (recorded.le(evidence_cutoff) & ends.le(recorded))
    bars = bars.loc[eligible]
    bars, conflicts = select_observation_revisions(bars)
    from ml.stock_target_prices import OBSERVED_MULTI_STOCK_PRICE_SOURCES
    if (conflicts and prices.attrs.get("stock_price_source", {}).get("source_contract")
            not in OBSERVED_MULTI_STOCK_PRICE_SOURCES):
        raise ValueError("Observed prices contain conflicting source clocks")
    return bars.sort_values(["symbol", "timestamp"], kind="stable")


def choose_target_source(bars, target):
    """Return one source's series only when BOTH target endpoints qualify."""
    from ml.independent_stock_targets import _target_observations, _valid_target_observations
    if "observation_source" not in bars:
        return bars
    targets = pd.DataFrame([target])
    order = source_order_for_contract(target.get("target_price_source_contract",
        bars.attrs.get("stock_price_source", {}).get("source_contract")))
    for source in order:
        candidate = bars.loc[bars.observation_source.eq(source)]
        observed = _target_observations(targets, by_symbol={str(target["symbol"]): candidate})
        if _valid_target_observations(targets, observed).iloc[0]:
            return candidate
    # No eligible pair: retain one feed for truthful missing-boundary diagnostics.
    return next((bars.loc[bars.observation_source.eq(s)] for s in order
                 if bars.observation_source.eq(s).any()), bars.iloc[:0])


def choose_boundary_source(bars, boundary, *, close, source_contract=None):
    from ml.gameplan_price_bands import _observation
    if "observation_source" not in bars:
        return bars
    order = source_order_for_contract(source_contract or bars.attrs.get("stock_price_source", {}).get("source_contract"))
    for source in order:
        candidate = bars.loc[bars.observation_source.eq(source)]
        if _observation(candidate, boundary, close=close) is not None:
            return candidate
    return next((bars.loc[bars.observation_source.eq(s)] for s in order
                 if bars.observation_source.eq(s).any()), bars.iloc[:0])


def planning_source_view(prices, forecasts, *, now):
    """Use one real source per symbol for all planning sample ratios and anchor.

    Selection uses the current preceding-session observed close, never a model
    score or target return. This planning policy is separately recorded from the
    per-target label policy. Missing anchors remain missing.
    """
    from ml.stock_target_prices import OBSERVED_MULTI_STOCK_PRICE_SOURCES
    source_contract = prices.attrs.get("stock_price_source", {}).get("source_contract")
    if source_contract not in OBSERVED_MULTI_STOCK_PRICE_SOURCES:
        return prices
    import exchange_calendars as xcals
    from ml.gameplan_price_bands import _clock
    bars = completed_observation_bars(prices, now)
    dates = pd.to_datetime(forecasts.action_date).dt.date
    if dates.empty:
        return bars
    calendar = xcals.get_calendar("XNYS", start=min(dates) - pd.Timedelta(days=14), end=max(dates) + pd.Timedelta(days=1))
    frames, selected = [], {}
    for symbol, forecasts_for_symbol in forecasts.groupby("symbol", sort=True):
        days = pd.to_datetime(forecasts_for_symbol.action_date).dt.date.unique()
        if len(days) != 1:
            raise ValueError("Multi-source planning requires one frozen action date per symbol")
        previous = pd.Timestamp(calendar.previous_session(pd.Timestamp(days[0]))).date()
        chosen = choose_boundary_source(bars.loc[bars.symbol.eq(symbol)], _clock(previous, 17), close=True,
                                        source_contract=source_contract)
        frames.append(chosen)
        selected[str(symbol)] = str(chosen.observation_source.iloc[0]) if len(chosen) else None
    result = pd.concat(frames, ignore_index=True) if frames else bars.iloc[:0]
    # Planning consumes endpoint prices only. The legacy XNAS loader validates
    # endpoint prices; fallback readers additionally validate native OHLCV.
    result = result.loc[:, ["symbol", "timestamp", "open", "close", *OBSERVATION_COLUMNS]]
    result.attrs = {**prices.attrs, "planning_source_selection": selected,
                    "planning_observation_policy": "one_source_per_symbol_history_and_reference_no_fill"}
    return result


def _read_bound(path):
    before = file_checksum(path)
    frame = pd.read_parquet(path)
    if file_checksum(path) != before:
        raise RuntimeError(f"Stock observation payload changed during read: {path}")
    return frame, before


def _valid(frame, *, source, symbol, digest, interval, dataset, recorded_at=None):
    required = {"timestamp", "open", "high", "low", "close", "volume"}
    if not required.issubset(frame):
        raise ValueError(f"Observed stock source lacks OHLCV: {source}")
    native = genuine_observation_mask(frame)
    result = frame.loc[:, list(required)].copy()
    result["timestamp"] = pd.to_datetime(result.timestamp, utc=True, errors="coerce")
    for column in ("open", "high", "low", "close", "volume"):
        result[column] = pd.to_numeric(result[column], errors="coerce")
    prices = result[["open", "high", "low", "close"]]
    valid = (native & result.timestamp.notna() & np.isfinite(result[[*prices, "volume"]]).all(axis=1)
             & prices.gt(0).all(axis=1) & result.volume.gt(0)
             & result.high.ge(prices.max(axis=1)) & result.low.le(prices.min(axis=1)))
    if recorded_at is not None and pd.notna(recorded_at):
        valid &= result.timestamp.add(pd.Timedelta(seconds=interval)).le(pd.to_datetime(recorded_at, utc=True))
    # Zero-volume placeholders have no positive evidence of a trade. They are
    # unavailable, not synthetic observations or proof that no trades occurred.
    result = result.loc[valid].copy()
    result = result.assign(symbol=symbol, observation_source=source,
                           observation_dataset=dataset, observation_interval_seconds=interval,
                           observation_input_sha256=digest,
                           observation_recorded_at=recorded_at)
    return result, int((~valid).sum())


def load_observed_stock_sources(root: Path, *, symbols, source_contract="observed-multi-source-stock-v1"):
    from datafetching.databento_archive import discover_archive_partitions
    from ml.stock_target_prices import (
        OBSERVED_MULTI_STOCK_PRICE_SOURCES, OBSERVED_MULTI_STOCK_PRICE_SOURCE_V2, XNAS_STOCK_PRICE_SOURCE,
        load_stock_target_prices, stock_price_dataset,
    )
    root = Path(root).resolve()
    if source_contract not in OBSERVED_MULTI_STOCK_PRICE_SOURCES:
        raise ValueError("Observed stock loader requires a versioned multi-source contract")
    symbols = tuple(str(symbol).strip().upper() for symbol in symbols)
    if (not symbols or len(set(symbols)) != len(symbols)
            or any(not s or not s.replace(".", "").isalnum() for s in symbols)):
        raise ValueError("Observed stock source requires unique valid symbols")
    frames, files, inventory = [], [], []
    available = discover_archive_partitions(root, market="us-equities", dataset="XNAS.ITCH",
                                           schema="ohlcv-1m", verify_payload=True)
    xnas_symbols = tuple(s for s in symbols if any(p.request["symbol_scope"] == [s] for p in available))
    if xnas_symbols:
        native, paths, report = load_stock_target_prices(root, symbols=xnas_symbols,
                                                        source_contract=XNAS_STOCK_PRICE_SOURCE)
        native.attrs = {}
        frames.append(native)
        files.extend(paths)
        inventory.append({"source": SOURCE_ORDER[0], "rows": len(native),
                          "source_evidence": report})
    for symbol in symbols:
        # Raw EQUS rows retain the dataset/schema/adjustment identity which the
        # seven-column normalized projection deliberately does not contain.
        for path in sorted((root / "stocks" / symbol / "bars/1m/databento/raw").glob("*_ohlcv-1m_1m_raw.parquet")):
            raw, digest = _read_bound(path)
            expected = {"provider_dataset": "EQUS.MINI", "source_schema": "ohlcv-1m",
                        "aggregation_method": "native", "corporate_action_adjustment": "none"}
            if any(key not in raw or set(raw[key].dropna().astype(str)) != {value}
                   for key, value in expected.items()):
                raise ValueError(f"EQUS fallback raw identity is unavailable or inconsistent: {path}")
            if "symbol" not in raw or set(raw.symbol.astype(str).str.upper()) != {symbol}:
                raise ValueError(f"EQUS fallback symbol identity is inconsistent: {path}")
            frame, omitted = _valid(raw.rename(columns={"ts_event": "timestamp"}), source=SOURCE_ORDER[1],
                                    symbol=symbol, digest=digest, interval=60, dataset="EQUS.MINI")
            frames.append(frame); files.append(path)
            inventory.append({"path": str(path), "sha256": digest, "source": SOURCE_ORDER[1],
                              "rows": len(frame), "invalid_or_zero_volume_rows_excluded": omitted,
                              "original_row_acquisition_time": "unavailable_in_legacy_raw_projection"})
        for minutes in (1, 5):
            source = f"schwab:pricehistory:{minutes}m"
            folder = root / "stocks" / symbol / f"bars/{minutes}m/schwab/raw"
            history = folder.parent / "raw-history"
            retained = []
            for receipt in sorted(history.glob('*.json')):
                evidence = json.loads(receipt.read_text(encoding='utf-8'))
                digest = receipt.stem
                path = history / f'{digest}.parquet'
                if (evidence.get('version') != 'schwab-price-history-raw-receipt-v1'
                        or evidence.get('sha256') != digest or file_checksum(path) != digest):
                    raise RuntimeError(f'Schwab retained raw receipt failed verification: {receipt}')
                retained.append(path)
                files.append(receipt)
            for path in sorted(set(folder.glob("*pricehistory*.parquet")) | set(retained)):
                payloads, digest = _read_bound(path)
                files.append(path)
                for item in payloads.to_dict("records"):
                    if (str(item.get("symbol")).upper() != symbol or item.get("source") != "schwab"
                            or item.get("canonical_timeframe") != f"{minutes}m"
                            or item.get("provider_frequency_type") != "minute"
                            or int(item.get("provider_frequency", 0)) != minutes):
                        raise ValueError(f"Schwab fallback raw identity is inconsistent: {path}")
                    payload = json.loads(item["payload_json"])
                    candles = pd.DataFrame(payload.get("candles", ()))
                    if candles.empty:
                        continue
                    candles["timestamp"] = pd.to_datetime(candles["datetime"], unit="ms", utc=True)
                    frame, omitted = _valid(candles, source=source, symbol=symbol, digest=digest,
                                            interval=minutes * 60, dataset="SCHWAB.PRICEHISTORY",
                                            recorded_at=item.get("fetched_at"))
                    frames.append(frame)
                    inventory.append({"path": str(path), "sha256": digest, "source": source,
                                      "payload_record_id": str(item.get("id")), "rows": len(frame),
                                      "invalid_or_zero_volume_rows_excluded": omitted})
        if source_contract == OBSERVED_MULTI_STOCK_PRICE_SOURCE_V2:
            from datafetching.fmp_price_history_receipts import load_native_five_minute_history
            fmp, fmp_files, fmp_inventory = load_native_five_minute_history(root, symbol=symbol)
            if not fmp.empty:
                frames.append(fmp)
            files.extend(fmp_files)
            inventory.extend(fmp_inventory)
    if not frames:
        raise RuntimeError("No source-identified real stock observations are available")
    bars = pd.concat(frames, ignore_index=True, sort=False)
    bars["timestamp"] = pd.to_datetime(bars.timestamp, utc=True)
    bars["observation_recorded_at"] = pd.to_datetime(bars.observation_recorded_at, utc=True, errors="coerce")
    # A revision conflict within one dataset is not resolved by arbitrary file
    # order; both conflicting clocks are unavailable to this policy.
    identities = ["symbol", "observation_source", "timestamp"]
    bars = bars.drop_duplicates([*identities, "open", "close", "observation_recorded_at"])
    # Preserve dated revisions until the consuming run applies its evidence
    # cutoff. Selecting today's revision here would erase earlier as-of inputs.
    bars = bars.sort_values(identities, kind="stable").reset_index(drop=True)
    _, conflict_count = select_observation_revisions(bars)
    report = {
        "source_contract": source_contract,
        "dataset": stock_price_dataset(source_contract),
        "source_order": list(source_order_for_contract(source_contract)), "policy": "first_source_with_both_observed_boundaries_within_five_minutes",
        "source_policy": "one_source_per_target_no_fill_no_cross_source_endpoint_pair",
        "source_coverage_claim": "provider-specific observations; not consolidated or proof of no trades",
        "historical_reconstruction": "input_hashes_bind_current_raw_bytes; missing_old_versions_not_recovered",
        "synthetic_rows": 0, "conflicting_source_clock_rows_excluded": conflict_count,
        "revision_policy": "latest_dated_provider_receipt_at_consumer_evidence_cutoff; simultaneous_conflicts_unavailable",
        "inventory": inventory,
        "by_symbol_source": [{"symbol": s, "source": k, "rows": len(f),
                              "first": f.timestamp.min().isoformat(), "last": f.timestamp.max().isoformat()}
                             for (s, k), f in bars.groupby(["symbol", "observation_source"], sort=True)],
        "unavailable_symbols": sorted(set(symbols) - set(bars.symbol)),
    }
    bars.attrs["stock_price_source"] = report
    return bars, tuple(dict.fromkeys(files)), report
