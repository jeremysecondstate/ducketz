"""Nightly local 15-minute model preparation; never starts or calls a trader.

Refreshes the existing OHLCV lane with its overlap/upsert implementation, audits
the local trader's previous session, evaluates causal model candidates and
atomically selects portable configurations. Fitted research objects, raw data,
execution state and audit logs stay on this PC. Scheduled wake times do not
impose a deadline on a running preparation.
"""
from __future__ import annotations

import argparse
from contextlib import closing
from dataclasses import asdict, replace
from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import re
import sqlite3
import tempfile
from typing import Callable
import uuid

import numpy as np
import pandas as pd

from .data import (FEATURE_VERSION, build_dataset, causal_features,
                   equity_sessions, load_local_history, utc, validate_candles)
from .prediction import ContractError, PACIFIC, STEP, TARGET_VERSION

VERSION = "intraday-model-supervision-v1"
ACTIVE_PATH = Path("intraday/model-supervision/active.json")
STATE_PATH = Path("intraday/model-supervision/selection-state.json")
DEFAULT_INPUTS = ("OHLCV",)


def _now():
    return datetime.now(timezone.utc)


def _json(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _hash(value):
    return hashlib.sha256(_json(value).encode("utf-8")).hexdigest()


def _failure(exc):
    """Useful local repair details with credential-bearing fragments removed."""
    message = str(exc)
    for key, value in os.environ.items():
        if len(value) >= 6 and re.search(r"KEY|TOKEN|SECRET|PASSWORD|CREDENTIAL", key, re.I):
            message = message.replace(value, "[redacted]")
    message = re.sub(r"(?i)(bearer\s+)[A-Za-z0-9_.~+/-]+", r"\1[redacted]", message)
    message = re.sub(r"(?i)(api[_-]?key|token|password|secret)(\s*[:=]\s*)\S+", r"\1\2[redacted]", message)
    return {"failure_type": type(exc).__name__, "message": message[:1000]}


def _atomic_json(path, value):
    """Publish a complete fsynced document; readers retain an old or new file."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=path.name + ".", suffix=".pending", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(_json(value) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


class AuditLog:
    def __init__(self, root, run_id, clock):
        self.root, self.run_id, self.clock = Path(root), run_id, clock
        self.root.mkdir(parents=True, exist_ok=True)

    def event(self, kind, **details):
        value = {"schema_version": VERSION, "run_id": self.run_id,
                 "recorded_at": utc(self.clock()).isoformat(), "kind": kind, **details}
        with (self.root / "events.jsonl").open("a", encoding="utf-8", newline="\n") as handle:
            handle.write(_json(value) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        return value


def installed_source(profile):
    """Record source evidence without making coordination state a run condition."""
    try:
        active = json.loads((Path(profile).parent / "active.json").read_text(encoding="utf-8-sig"))
        return {"commit": active.get("commit"), "manifest_sha256": active.get("manifest_sha256")}
    except (OSError, ValueError, TypeError):
        return {"status": "source_record_unavailable"}


def latest_completed_session(as_of):
    now = utc(as_of)
    sessions = equity_sessions(now - pd.Timedelta(days=20), now)
    completed = [session for session in sessions if utc(session.closed_at) <= now]
    if not completed:
        raise ContractError("No completed XNAS trading session in the recent calendar")
    return max(completed, key=lambda value: utc(value.closed_at))


def refresh_ohlcv(datastore, symbols, *, source_dataset="EQUS.MINI", clock=_now):
    """Use the established minute continuation, native raw retention and dedup.

    This lane trains only on OHLCV-derived features. No options, macro, broker,
    peer, Gameplan or other trader data is acquired. The existing authenticated
    provider binding determines the dataset; a requested dataset must match it.
    """
    from app.services.databento_market_data import DatabentoMarketDataProvider
    from datafetching.databento_fetch import (_fetch_native_results_many,
                                             _load_repository_environment,
                                             _persist_native_results)
    from datafetching.loop_a_cycle import datastore_cycle_lock
    from datafetching.parquet_store import ParquetStore

    _load_repository_environment()
    provider = DatabentoMarketDataProvider(native_schemas=("ohlcv-1m",))
    if provider.dataset != source_dataset:
        raise ContractError("Existing provider dataset differs from selected training source")
    if source_dataset != "EQUS.MINI":
        raise ContractError("Nightly continuation currently supports canonical EQUS.MINI history")
    specs = tuple(provider.native_specs())
    if len(specs) != 1 or specs[0].schema != "ohlcv-1m" or specs[0].frequency != "1m":
        raise ContractError("Existing native minute schema is unavailable")
    store = ParquetStore(datastore)
    started = utc(clock())
    evidence = {}
    # This existing datastore mutex protects actual file upserts against other
    # writers. It neither reads nor alters any trader or nightly stop state.
    with datastore_cycle_lock(store.root_dir):
        native = _fetch_native_results_many(provider, tuple(symbols), "continuation", store)
        observed = utc(clock())
        for symbol in symbols:
            rows = native.get(symbol, [])
            try:
                persisted = _persist_native_results(symbol, store, provider=provider,
                    profile="continuation", observed_at=observed.to_pydatetime(),
                    native_results=rows, batch_watchlist_symbol_count=len(symbols), run_derived=False)
            except Exception as exc:
                evidence[symbol] = {"status": "error", "phase": "OHLCV_persistence", **_failure(exc)}
                continue
            successful = [row for row in rows if row[4] is None and row[3] is not None]
            failures = [type(row[4]).__name__ for row in rows if row[4] is not None]
            coverage_end = max((utc(row[3].end) for row in successful), default=None)
            evidence[symbol] = {"status": "refreshed" if successful and not failures else "error",
                "source_dataset": source_dataset, "inputs": list(DEFAULT_INPUTS),
                "started_at": started.isoformat(), "completed_at": observed.isoformat(),
                "requested_through": coverage_end.isoformat() if coverage_end is not None else None,
                "requested_from": min((utc(row[3].start) for row in successful)).isoformat() if successful else None,
                "data_files": persisted.data_files, "error_files": persisted.error_files,
                "failure_types": failures, "failures": [_failure(row[4]) for row in rows if row[4] is not None],
                "continuation_policy": "existing_minute_overlap_idempotent_upsert",
                "returned_minute_rows": sum(len(row[1]) for row in successful)}
    return evidence


def training_snapshot(history, *, cutoff, lookback_days=None, synthetic_policy="include", population_contract=None):
    """Admit every mature exact label, including the end of a closed session.

    build_dataset supplies the validated inference contract. Its live inference
    truncation would otherwise omit the freshest matured rows after a closed
    session's last eligible reference. Offline selection uses full causal
    history, with no feature or label available after this frozen cutoff.
    """
    cutoff = utc(cutoff)
    from .coverage import population_frame
    population_contract = population_contract or history.manifest.get("population_contract")
    frame = validate_candles(population_frame(history.frame, population_contract))
    frame = frame.loc[frame.available_at.le(cutoff) & (frame.opened_at + STEP).le(cutoff)].reset_index(drop=True)
    if frame.empty:
        raise ContractError("No completed training candles at the refreshed cutoff")
    eligible = frame
    if "session_close" in frame:
        eligible = frame.loc[(frame.opened_at + 3 * STEP).le(pd.to_datetime(frame.session_close, utc=True))]
    if eligible.empty:
        raise ContractError("No completed reference with an intraday target")
    reference = eligible.opened_at.max()
    base = build_dataset(history.frame, cutoff=cutoff, reference_at=reference,
                         lookback_days=lookback_days, synthetic_policy=synthetic_policy,
                         population_contract=population_contract)
    features = causal_features(frame)
    lookup = frame.set_index("opened_at", drop=False)
    lower = cutoff - pd.Timedelta(days=lookback_days) if lookback_days is not None else frame.opened_at.min()
    X, y, refs, closes, availability = [], [], [], [], []
    synthetic_included, synthetic_excluded = 0, 0
    for position, row in enumerate(frame.itertuples()):
        ref, target_open, target_close = row.opened_at, row.opened_at + 2 * STEP, row.opened_at + 3 * STEP
        if ref < lower or target_close > cutoff or target_open not in lookup.index:
            continue
        target = lookup.loc[target_open]
        if str(target.session_id) != str(row.session_id) or target.available_at > cutoff:
            continue
        # Each historical feature uses only observations available by its own
        # decision time; the new snapshot cutoff cannot repair past leakage.
        if frame.iloc[max(0, position - 20):position + 1].available_at.gt(row.available_at).any():
            continue
        values = features.loc[ref].to_numpy(dtype=float)
        if not np.isfinite(values).all():
            continue
        if row.synthetic or target.synthetic:
            if synthetic_policy == "exclude":
                synthetic_excluded += 1
                continue
            synthetic_included += 1
        X.append(values)
        y.append(int(float(target.close) >= float(row.open)))
        refs.append(ref)
        closes.append(target_close)
        # A completed target alone does not make its training row observable:
        # its own feature candle may have arrived after the target receipt.
        # The history check above also binds every trailing feature input to
        # this row's actual receipt, matching build_dataset's joint cutoff.
        availability.append(max(target.available_at, row.available_at))
    if not X:
        raise ContractError("No mature finite labels in the refreshed training snapshot")
    manifest = {**base.manifest, "training_cutoff": cutoff.isoformat(), "training_rows": len(X),
        "first_training_reference": min(refs).isoformat(), "last_training_reference": max(refs).isoformat(),
        "label_available_at_max": max(availability).isoformat(), "freshest_candle_open": frame.opened_at.max().isoformat(),
        "synthetic_labels_included": synthetic_included, "synthetic_labels_excluded": synthetic_excluded,
        "source_history": history.manifest, "training_inputs": list(DEFAULT_INPUTS),
        "offline_training_policy": "all_matured_same_session_labels_full_causal_snapshot"}
    return replace(base, X=np.asarray(X, dtype=float), y=np.asarray(y, dtype=np.uint8),
                   training_reference_at=pd.DatetimeIndex(refs), target_close_at=pd.DatetimeIndex(closes),
                   label_available_at=pd.DatetimeIndex(availability), manifest=manifest)


def attach_market_inputs(snapshot, datastore, symbol, *, cutoff):
    """Attach the same receipt-causal saved context used by the live runner."""
    from .market_inputs import MarketInputStore, augment_snapshot
    from . import feature_support
    from ml.market_provenance import capture_runtime
    inputs = {"X": snapshot.X.tolist(), "y": snapshot.y.tolist(),
              "reference_at": [value.isoformat() for value in snapshot.training_reference_at],
              "inference_X": snapshot.inference_X.tolist(), "feature_names": list(snapshot.feature_names),
              "manifest": snapshot.manifest}
    joined = augment_snapshot(inputs, MarketInputStore(Path(datastore)), symbol=symbol,
                              inference_at=cutoff, cutoff=cutoff)
    feature_contract = dict(joined["manifest"].get("feature_contract", {}))
    feature_contract.setdefault("price", FEATURE_VERSION)
    feature_contract.setdefault("market_inputs", joined["market_inputs"]["version"])
    feature_contract["market_support"] = feature_support.VERSION
    manifest = {**joined["manifest"], "market_inputs": joined["market_inputs"],
                "feature_contract": feature_contract,
                "training_inputs": [*DEFAULT_INPUTS, "receipt_causal_market_context"],
                "market_context_availability": "only_original_receipts_at_or_before_each_historical_decision_no_backfill"}
    manifest["market_input_runtime_provenance"] = capture_runtime(["ml.intraday.market_inputs",
        "ml.intraday.market_input_research", "ml.intraday.market_input_schwab_stream", feature_support.__name__],
        object_root=Path(datastore) / "intraday/model-supervision/source-objects")
    return replace(snapshot, X=np.asarray(joined["X"], dtype=float),
                   inference_X=np.asarray(joined["inference_X"], dtype=float),
                   feature_names=tuple(joined["feature_names"]), manifest=manifest)


def _metrics(rows):
    if not rows:
        return {"scored": 0, "accuracy": None, "brier": None, "log_loss": None}
    y, p = np.asarray([row[0] for row in rows], dtype=float), np.asarray([row[1] for row in rows], dtype=float)
    clipped = np.clip(p, 1e-15, 1 - 1e-15)
    return {"scored": len(rows), "accuracy": float(np.mean((p >= .5) == y)),
            "brier": float(np.mean((p - y) ** 2)),
            "log_loss": float(np.mean(-y * np.log(clipped) - (1 - y) * np.log(1 - clipped)))}


def session_results(datastore, actor, symbol, session, *, history=None):
    """Read only this machine's own 15m evidence, never another trader's state.

    Missing recorded targets may be evaluated from refreshed same-source
    historical OHLCV, explicitly labelled reconstructed research outcomes.
    This does not overwrite live receipt evidence or execution ledgers.
    """
    result = {"session_id": session.session_id, "opened_at": session.opened_at,
              "closed_at": session.closed_at, "mode": "live", "forecasts": 0,
              "missing_outcomes": 0, "recorded_outcomes": 0, "historical_reconstructions": 0}
    models, ensemble, source_counts = {}, [], {}
    path = Path(datastore) / "intraday/shadow.sqlite3"
    candles = {} if history is None else {utc(row["opened_at"]): row for row in history.frame.to_dict("records")}
    if path.is_file():
        with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)) as db:
            rows = db.execute("SELECT id,payload FROM forecasts WHERE pc=? AND symbol=? ORDER BY rowid", (actor, symbol)).fetchall()
            for forecast_id, raw in rows:
                forecast = json.loads(raw)
                ref = utc(forecast["reference_at"])
                if forecast.get("mode") != "live" or not session.contains(ref):
                    continue
                result["forecasts"] += 1
                saved = db.execute("SELECT payload FROM outcomes WHERE forecast_id=? ORDER BY rowid DESC LIMIT 1", (forecast_id,)).fetchone()
                outcome = json.loads(saved[0]) if saved else {}
                label = outcome.get("target_not_down") if outcome.get("status") in {"scored", "synthetic"} else None
                if label is not None:
                    result["recorded_outcomes"] += 1
                else:
                    target = candles.get(utc(forecast["target_open"]))
                    if (target is not None and target.get("source_dataset") == forecast.get("source_dataset")
                            and target.get("source_id") == forecast.get("source_id")
                            and target.get("population_contract") == forecast.get("population_contract")
                            and str(target.get("session_id")) == session.session_id):
                        label = float(target["close"]) >= float(forecast["reference_open"])
                        result["historical_reconstructions"] += 1
                if label is None:
                    result["missing_outcomes"] += 1
                    continue
                probability = float(forecast["probability_not_down"])
                if not math.isfinite(probability) or not 0 <= probability <= 1:
                    raise ContractError("Invalid saved live ensemble probability")
                ensemble.append((int(label), probability))
                source = str(forecast.get("source_dataset", "unrecorded"))
                source_counts[source] = source_counts.get(source, 0) + 1
                for model in forecast.get("models", []):
                    if model.get("status") != "success":
                        continue
                    p = float(model["probability_not_down"])
                    if not math.isfinite(p) or not 0 <= p <= 1:
                        raise ContractError("Invalid saved model probability")
                    models.setdefault(model["name"], []).append((int(label), p))
    result.update(ensemble=_metrics(ensemble), models={name: _metrics(rows) for name, rows in models.items()},
                  source_dataset_counts=source_counts,
                  outcome_limitation="Historical reconstruction is research evidence; observed live receipts remain unchanged")
    path = Path(datastore) / "intraday/execution/holdings.sqlite3"
    totals = {"status": "not_recorded", "fill_count": 0, "buy_quantity": 0, "sell_quantity": 0,
              "buy_notional": 0.0, "sell_notional": 0.0, "realized_pnl": None,
              "limitation": "Fill notionals are cash flow, not profit; commissions and inventory cost basis are not inferred"}
    if path.is_file():
        with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)) as db:
            rows = db.execute("SELECT f.quantity,f.price,f.executed_at,r.side FROM fills f "
                              "JOIN reservations r ON r.id=f.reservation JOIN allocations a ON a.id=r.allocation "
                              "WHERE a.symbol=? AND a.horizon='15m'", (symbol,)).fetchall()
        totals["status"] = "recorded"
        for quantity, price, executed, side in rows:
            if utc(session.opened_at) <= utc(executed) < utc(session.closed_at):
                prefix = "buy" if side == "BUY" else "sell"
                totals["fill_count"] += 1
                totals[prefix + "_quantity"] += int(quantity)
                totals[prefix + "_notional"] += int(quantity) * float(price)
    result["execution"] = totals
    return result


def _load_state(path, actor):
    if not Path(path).is_file():
        return {"schema_version": VERSION, "actor": actor, "nights": {}}
    value = json.loads(Path(path).read_text(encoding="utf-8"))
    if (value.get("schema_version") != VERSION or value.get("actor") != actor
            or not isinstance(value.get("nights"), dict)
            or any(not isinstance(boundaries, dict) for boundaries in value["nights"].values())):
        raise ContractError("Model selection state belongs to a different actor or schema")
    for boundaries in value["nights"].values():
        for boundary in boundaries.values():
            utc(boundary)
    return value


class _ActiveEntryErrors(ContractError):
    """Carry independently valid incumbents through preserved-file recovery."""
    def __init__(self, recovered, symbol_errors):
        super().__init__("Malformed active model entries were excluded independently")
        self.recovered = recovered
        self.symbol_errors = symbol_errors


def _load_active(path, actor, source_dataset):
    if not Path(path).is_file():
        return {"schema_version": VERSION, "actor": actor, "source_dataset": source_dataset,
                "target_version": TARGET_VERSION, "symbols": {}}
    value = json.loads(Path(path).read_text(encoding="utf-8"))
    if (not isinstance(value, dict) or value.get("schema_version") != VERSION or value.get("actor") != actor
            or value.get("source_dataset") != source_dataset or value.get("target_version") != TARGET_VERSION
            or not isinstance(value.get("symbols"), dict)):
        raise ContractError("Active model configuration differs from this local preparation")
    from .models import configured_models
    valid, errors = {}, {}
    for symbol, entry in value["symbols"].items():
        try:
            if not isinstance(entry, dict) or not entry.get("selection_id"):
                raise ContractError("Active model symbol entry is malformed")
            utc(entry["selected_at"])
            configured_models(entry["models"])
            valid[symbol] = entry
        except (ValueError, TypeError, KeyError) as exc:
            errors[symbol] = _failure(exc)
    if errors:
        raise _ActiveEntryErrors({**value, "symbols": valid}, errors)
    return value


def _recover_metadata(path, loader, fallback, audit):
    """Retain broken local metadata and repair it without delaying OHLCV work."""
    try:
        return loader(), None
    except Exception as exc:
        evidence = _failure(exc)
        recovered = fallback
        if isinstance(exc, _ActiveEntryErrors):
            recovered = exc.recovered
            evidence.update(symbol_errors=exc.symbol_errors,
                            retained_symbols=list(recovered["symbols"]))
        path = Path(path)
        if path.is_file():
            backup = path.with_name(path.stem + ".preserved-" + uuid.uuid4().hex + path.suffix)
            raw = path.read_bytes()
            with backup.open("xb") as handle:
                handle.write(raw)
                handle.flush()
                os.fsync(handle.fileno())
            evidence.update(preserved_path=str(backup), sha256=hashlib.sha256(raw).hexdigest())
        audit.event("metadata_recovered", path=str(path), **evidence)
        return recovered, evidence


def supervise(*, datastore, profile, watchlist, config=None, log_dir=None,
              refresh: Callable = refresh_ohlcv, history_loader: Callable = load_local_history,
              selector: Callable | None = None, clock: Callable = _now):
    """One recoverable wake, all symbols independent; old selections survive errors."""
    from .models import configured_models
    from .full_model_selection import SelectionConfig, select_models
    from .runtime import local_symbols

    config = dict(config or {})
    allowed = {"schema_version", "source_dataset", "lookback_days", "synthetic_policy", "selection", "candidates"}
    if set(config) - allowed:
        raise ContractError("Unknown supervisor configuration fields")
    source_dataset, synthetic_policy = config.get("source_dataset", "EQUS.MINI"), config.get("synthetic_policy", "include")
    if synthetic_policy not in {"include", "exclude"}:
        raise ContractError("Explicit synthetic training policy must be include or exclude")
    lookback_days = config.get("lookback_days")
    if lookback_days is not None and (type(lookback_days) is not int or lookback_days < 1):
        raise ContractError("Model supervision lookback_days requires a positive integer or all history")
    actor, symbols = local_symbols(profile, watchlist, datastore)
    root, begun = Path(datastore), utc(clock())
    from .coverage import load_data_policy
    data_policy = load_data_policy(root)
    population_contract = data_policy.get("population_contract")
    if data_policy.get("source_dataset", source_dataset) != source_dataset:
        raise ContractError("Local data policy differs from supervised source")
    night = begun.tz_convert(PACIFIC).date().isoformat()
    run_id = begun.strftime("%Y%m%dT%H%M%SZ") + "-" + uuid.uuid4().hex[:12]
    audit = AuditLog(log_dir or root / "intraday/model-supervision/logs", run_id, clock)
    report = {"schema_version": VERSION, "run_id": run_id, "actor": actor, "night": night,
              "started_at": begun.isoformat(), "source_dataset": source_dataset,
              "installed_source": installed_source(profile), "training_inputs": list(DEFAULT_INPUTS),
              "manual_start_only": True, "trader_started": False, "broker_calls": 0, "symbols": {}}
    report["data_policy"] = data_policy
    if data_policy.get("market_inputs_enabled", False):
        report["training_inputs"] = [*DEFAULT_INPUTS, "receipt_causal_market_context"]
    from ml.market_provenance import capture_runtime
    from . import feature_support
    report["runtime_provenance"] = capture_runtime(
        [__name__, "ml.intraday.data", "ml.intraday.coverage", "ml.intraday.models", "ml.intraday.full_model_selection",
         feature_support.__name__],
        object_root=root / "intraday/model-supervision/source-objects")
    audit.event("run_started", actor=actor, symbols=list(symbols), source_dataset=source_dataset)
    active_path, state_path = root / ACTIVE_PATH, root / STATE_PATH
    empty_active = {"schema_version": VERSION, "actor": actor, "source_dataset": source_dataset,
                    "target_version": TARGET_VERSION, "symbols": {}}
    active, active_error = _recover_metadata(active_path,
        lambda: _load_active(active_path, actor, source_dataset), empty_active, audit)
    state, state_error = _recover_metadata(state_path, lambda: _load_state(state_path, actor),
        {"schema_version": VERSION, "actor": actor, "nights": {}}, audit)
    if active_error or state_error:
        report["metadata_repairs"] = {"active": active_error, "state": state_error}
    if state_error:
        for symbol, value in active["symbols"].items():
            if value.get("holdout_night") == night and value.get("holdout_start"):
                state["nights"].setdefault(night, {})[symbol] = value["holdout_start"]
        report["holdout_history_limitation"] = "Preserved damaged state; restored known active boundaries, other prior inspected holdouts are unknown"
    session = latest_completed_session(begun)
    report["previous_session"] = asdict(session)
    selection_config = SelectionConfig(**config.get("selection", {}))
    candidates = configured_models(config["candidates"]) if config.get("candidates") is not None else None
    selector = selector or select_models
    try:
        refreshed = refresh(root, symbols, source_dataset=source_dataset, clock=clock)
        audit.event("refresh_completed", results=refreshed)
    except Exception as exc:
        refreshed = {symbol: {"status": "error", **_failure(exc)} for symbol in symbols}
        audit.event("refresh_failed", **_failure(exc))
    frozen_cutoff = utc(clock())
    report["training_cutoff"] = frozen_cutoff.isoformat()
    for symbol in symbols:
        item = {"refresh": refreshed.get(symbol, {"status": "error", "failure_type": "MissingRefreshResult"}),
                "incumbent_retained": symbol in active["symbols"]}
        report["symbols"][symbol] = item
        try:
            item["previous_session_results"] = session_results(root, actor, symbol, session)
        except Exception as exc:
            item["previous_session_results"] = {"status": "error", **_failure(exc),
                                                "session_id": session.session_id}
        try:
            if item["refresh"].get("status") != "refreshed":
                raise ContractError("OHLCV refresh failed for this symbol")
            requested_through = item["refresh"].get("requested_through")
            if requested_through is None or utc(requested_through) < utc(session.closed_at):
                raise ContractError("Provider coverage does not reach the latest completed trading session")
            history_options = {"source_dataset": source_dataset, "end": frozen_cutoff}
            if population_contract is not None:
                history_options["population_contract"] = population_contract
            history = history_loader(root, symbol, **history_options)
            item["freshness"] = {"provider_available_through": requested_through,
                "provider_lag_seconds": max(0.0, (frozen_cutoff - utc(requested_through)).total_seconds()),
                "freshest_observed_minute": history.manifest.get("last_minute_open"),
                "freshest_observed_candle": history.manifest.get("last_candle_open"),
                "missing_intervals": history.manifest.get("aggregation", {}).get("incomplete_intervals"),
                "policy": "refreshed_to_latest_advertised_provider_data_no_gap_fabrication"}
            try:
                item["previous_session_results"] = session_results(root, actor, symbol, session, history=history)
            except Exception as exc:
                item["previous_session_results"] = {"status": "error", **_failure(exc),
                                                    "session_id": session.session_id}
            snapshot = training_snapshot(history, cutoff=frozen_cutoff, lookback_days=config.get("lookback_days"),
                                         synthetic_policy=synthetic_policy, population_contract=population_contract)
            if data_policy.get("market_inputs_enabled", False):
                snapshot = attach_market_inputs(snapshot, root, symbol, cutoff=frozen_cutoff)
            item["training_snapshot"] = snapshot.manifest
            prior_holdout = state["nights"].get(night, {}).get(symbol)
            chosen = selector(snapshot, candidates=candidates, config=selection_config, previous_holdout_start=prior_holdout)
            item["selection"] = chosen.report
            if chosen.holdout_start:
                state["nights"].setdefault(night, {})[symbol] = chosen.holdout_start
                _atomic_json(state_path, state)
            if chosen.selected_models:
                selected_at = utc(clock()).isoformat()
                chosen_config = {"models": [asdict(model) for model in chosen.selected_models],
                    "selected_at": selected_at, "holdout_start": chosen.holdout_start, "holdout_night": night,
                    "feature_names": list(snapshot.feature_names), "feature_version": FEATURE_VERSION,
                    "target_version": TARGET_VERSION, "source_dataset": source_dataset,
                    "training_cutoff": frozen_cutoff.isoformat(), "synthetic_policy": synthetic_policy,
                    "lookback_days": config.get("lookback_days"), "max_training_rows": selection_config.max_rows,
                    "report_sha256": _hash(chosen.report)}
                chosen_config.update(population_contract=population_contract,
                    feature_contract=snapshot.manifest.get("feature_contract", {"price": FEATURE_VERSION}),
                    market_input_version=snapshot.manifest.get("market_input_version"))
                selection_id = _hash(chosen_config)
                chosen_config["selection_id"] = selection_id
                next_active = json.loads(_json(active))
                next_active["symbols"][symbol] = chosen_config
                next_active["updated_at"] = selected_at
                _atomic_json(active_path, next_active)
                active = next_active
                item.update(status="activated", selection_id=selection_id, incumbent_retained=False)
            else:
                item["status"] = "incumbent_retained" if item["incumbent_retained"] else "no_selection"
            audit.event("symbol_completed", symbol=symbol, result=item)
        except Exception as exc:
            item.update(status="error", **_failure(exc))
            audit.event("symbol_failed", symbol=symbol, result=item)
    report.update(completed_at=utc(clock()).isoformat(),
                  status="partial" if any(item["status"] == "error" for item in report["symbols"].values()) else "complete")
    _atomic_json(audit.root / (run_id + ".json"), report)
    audit.event("run_completed", status=report["status"], actor=actor)
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--datastore", type=Path, required=True)
    parser.add_argument("--profile", type=Path, required=True)
    parser.add_argument("--watchlist", type=Path, required=True)
    parser.add_argument("--config", type=Path)
    parser.add_argument("--log-dir", type=Path)
    args = parser.parse_args(argv)
    from datafetching.runtime_lock import exclusive_runtime_lock
    config = json.loads(args.config.read_text(encoding="utf-8")) if args.config else None
    with exclusive_runtime_lock(args.datastore / "locks/intraday-model-supervision.lock",
                                process_name="local 15m model supervision"):
        result = supervise(datastore=args.datastore, profile=args.profile, watchlist=args.watchlist,
                           config=config, log_dir=args.log_dir)
    print(_json({"run_id": result["run_id"], "status": result["status"],
                 "symbols": {symbol: value["status"] for symbol, value in result["symbols"].items()}}), flush=True)
    return 0 if result["status"] == "complete" else 1


if __name__ == "__main__":
    raise SystemExit(main())
