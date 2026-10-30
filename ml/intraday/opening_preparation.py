"""Prepare the original 04:00 target without a trader, broker or execution queue.

The existing model owner consumes System 1A's immutable 03:30 reference after
03:45 plus its publication offset. Original full fits and issue clocks are
retained. Only an already admitted manual session may adopt those predictions;
preparation itself never grants order authority or clears a manual stop.
"""
from __future__ import annotations

import argparse
from dataclasses import asdict
from datetime import timedelta
import json
from pathlib import Path
import sqlite3
import time

from .prediction import ContractError, PACIFIC, STEP, digest, encoded, instant

VERSION = "intraday-opening-preparation-v1"
CACHE_NAME = "opening-preparation.sqlite3"


def opening_window(now, *, publication_offset_seconds=30):
    """Original reference/target clocks, in UTC, on this Pacific calendar date."""
    current = instant(now)
    target = instant(current.astimezone(PACIFIC).replace(hour=4, minute=0, second=0, microsecond=0))
    offset = float(publication_offset_seconds)
    if not 0 <= offset < 900:
        raise ContractError("Opening publication offset must be within a quarter-hour")
    reference = target - 2 * STEP
    return reference, reference + STEP + timedelta(seconds=offset), target, target + STEP


def _opening_forecast(forecast):
    reference, _, target, close = opening_window(forecast["reference_at"])
    return (instant(forecast["reference_at"]) == reference
        and instant(forecast["target_open"]) == target
        and instant(forecast["target_close"]) == close
        and forecast.get("mode") == "live" and forecast.get("availability_basis") == "observed"
        and reference + STEP <= instant(forecast["information_available_at"]) <= instant(forecast["issued_at"])
        and instant(forecast["training_cutoff"]) <= instant(forecast["issued_at"])
        and instant(forecast["issued_at"]) < close
        and not forecast.get("execution_blocked_reason"))


def opening_handoff_valid(forecast, *, session_started_at):
    """A narrow prior-issue exception; manual order authority remains separate.

    Reconstruct the exact scientific original, including all probabilities,
    diagnostics, directions and timestamps. The only changed fields are the
    new manual cycle and this explicit admission link.
    """
    try:
        link = forecast["opening_handoff"]
        if (not isinstance(link, dict) or set(link) != {"schema", "source_forecast_id", "source_cycle_id",
                "session_id", "session_started_at", "admitted_at", "configuration_hash"}
                or link["schema"] != VERSION or not _opening_forecast(forecast)
                or instant(link["session_started_at"]) != instant(session_started_at)):
            return False
        _, _, target, close = opening_window(forecast["reference_at"])
        start, admitted = instant(session_started_at), instant(link["admitted_at"])
        if (start.astimezone(PACIFIC).date() != target.astimezone(PACIFIC).date()
                or not max(target, start, instant(forecast["issued_at"])) <= admitted < close):
            return False
        expected_cycle = digest(dict(pc=forecast["pc"], reference_at=forecast["reference_at"],
            configuration_hash=link["configuration_hash"], mode="live", experiment=link["session_id"]))
        if forecast["cycle_id"] != expected_cycle:
            return False
        source = dict(forecast)
        source.pop("forecast_id", None)
        source.pop("opening_handoff")
        source["cycle_id"] = link["source_cycle_id"]
        return digest(source) == link["source_forecast_id"]
    except (KeyError, TypeError, ValueError):
        return False


class PreparedForecasts:
    """Non-executing publication sink; no execution queue exists in this store."""
    def __init__(self, store, *, actor, symbols, configuration, reference):
        self.store, self.actor, self.symbols = store, actor, tuple(symbols)
        self.configuration, self.reference = configuration, instant(reference)
        with store.connect() as db:
            db.execute("""CREATE TABLE IF NOT EXISTS opening_sources(
                forecast_id TEXT PRIMARY KEY,session_date TEXT NOT NULL,metadata TEXT NOT NULL)""")

    def enqueue(self, forecast):
        from .split_handoff import ready_scientific_configuration_hashes
        if (not _opening_forecast(forecast) or forecast["pc"] != self.actor
                or forecast["symbol"] not in self.symbols or instant(forecast["reference_at"]) != self.reference):
            raise ContractError("Only this owner's exact opening prediction can be prepared")
        value = {"schema": VERSION, "actor": self.actor, "symbols": list(self.symbols),
            "configuration_hash": digest(self.configuration),
            "scientific_configuration_hashes": list(ready_scientific_configuration_hashes(self.configuration)),
            "source_forecast_id": forecast["forecast_id"], "source_cycle_id": forecast["cycle_id"]}
        with self.store.connect() as db:
            old = db.execute("SELECT metadata FROM opening_sources WHERE forecast_id=?",
                             (forecast["forecast_id"],)).fetchone()
            raw = encoded(value)
            if old is not None and old[0] != raw:
                raise ContractError("Opening publication provenance is immutable")
            db.execute("INSERT OR IGNORE INTO opening_sources VALUES(?,?,?)", (
                forecast["forecast_id"], self.reference.astimezone(PACIFIC).date().isoformat(), raw))


def adopt_opening(*, store, queue, session, config, state, clock, stopped, assigned_symbols,
                  max_symbols=None):
    """Adopt completed evidence once, without fitting or changing its clocks."""
    from .split_handoff import ready_scientific_configuration_hashes
    from .store import ShadowStore
    now = instant(clock())
    reference, _, target, close = opening_window(now)
    if stopped() or not target <= now < close:
        return None
    source_path = store.path.parent / CACHE_NAME
    if not source_path.is_file():
        return None
    configuration = asdict(config)
    configuration_hash = digest(configuration)
    science_hashes = set(ready_scientific_configuration_hashes(configuration))
    accepted = dict(state.get("opening_adopted", {}))
    day = target.astimezone(PACIFIC).date().isoformat()
    try:
        with sqlite3.connect(source_path.resolve().as_uri() + "?mode=ro", uri=True, timeout=.25) as db:
            if db.execute("SELECT 1 FROM sqlite_master WHERE name='opening_sources'").fetchone() is None:
                return None
            rows = db.execute("""SELECT f.id,f.pc,f.symbol,f.cycle_id,f.reference_at,f.issued_at,f.payload,s.metadata,c.payload
                FROM opening_sources s JOIN forecasts f ON f.id=s.forecast_id JOIN cycles c ON c.id=f.cycle_id
                WHERE s.session_date=? ORDER BY f.symbol""", (day,)).fetchall()
    except (OSError, sqlite3.DatabaseError) as exc:
        store.event(session["session_id"], "opening_preparation_unavailable", {
            "error_type": type(exc).__name__, "ordinary_model_work_continues": True})
        return None
    adopted = []
    for row in rows:
        if stopped() or instant(clock()) >= close:
            break
        if max_symbols is not None and len(adopted) >= max_symbols:
            break
        if row[2] not in assigned_symbols or accepted.get(row[2]) == row[0]:
            continue
        try:
            source = ShadowStore._forecast_value(row[:7])
            metadata, cycle = json.loads(row[7]), json.loads(row[8])
            if (metadata.get("schema") != VERSION or metadata.get("actor") != session["actor"]
                    or metadata.get("symbols") != list(session["symbols"])
                    or not science_hashes.intersection(metadata.get("scientific_configuration_hashes", []))
                    or metadata.get("source_forecast_id") != source["forecast_id"]
                    or metadata.get("source_cycle_id") != source["cycle_id"]
                    or cycle.get("configuration_hash") != metadata.get("configuration_hash")
                    or cycle.get("pc") != session["actor"] or source["pc"] != session["actor"]
                    or cycle.get("symbols") != list(session["symbols"])
                    or instant(source["reference_at"]) != reference or not _opening_forecast(source)):
                raise ContractError("Opening preparation differs from this local session")
            # A current runner with another recipe may not relabel the prepared
            # prediction. Only explicitly equivalent int/float offset hashes
            # are admitted by the existing scientific configuration matcher.
            cycle_id = digest(dict(pc=session["actor"], reference_at=reference.isoformat(),
                configuration_hash=configuration_hash, mode="live", experiment=session["session_id"]))
            admitted_at = instant(clock())
            link = {"schema": VERSION, "source_forecast_id": source["forecast_id"],
                "source_cycle_id": source["cycle_id"], "session_id": session["session_id"],
                "session_started_at": session["started_at"], "admitted_at": admitted_at.isoformat(),
                "configuration_hash": configuration_hash}
            # A claim is retained even across a crash during publication. A
            # later manually started session cannot replay the same prediction.
            with store.connect() as db:
                db.execute("""CREATE TABLE IF NOT EXISTS intraday_opening_claims(
                    source_forecast_id TEXT PRIMARY KEY,session_id TEXT NOT NULL,link TEXT NOT NULL)""")
                db.execute("BEGIN IMMEDIATE")
                old = db.execute("SELECT session_id,link FROM intraday_opening_claims WHERE source_forecast_id=?",
                                 (source["forecast_id"],)).fetchone()
                if old and old[0] != session["session_id"]:
                    continue
                if old:
                    link = json.loads(old[1])
                else:
                    db.execute("INSERT INTO intraday_opening_claims VALUES(?,?,?)",
                               (source["forecast_id"], session["session_id"], encoded(link)))
            payload = {**source, "cycle_id": cycle_id, "opening_handoff": link}
            payload.pop("forecast_id")
            if not opening_handoff_valid(payload, session_started_at=session["started_at"]):
                raise ContractError("Opening prediction cannot be admitted to this manual session")
            retained = {**cycle, "experiment": session["session_id"], "configuration_hash": configuration_hash,
                "started_at": session["started_at"], "opening_source_cycle_id": source["cycle_id"]}
            if stopped() or instant(clock()) >= close:
                break
            store.begin_cycle(cycle_id, retained)
            mirrored = store.save_forecast(payload)
            if {key: value for key, value in mirrored.items() if key not in {"forecast_id", "cycle_id", "opening_handoff"}} != {
                    key: value for key, value in source.items() if key not in {"forecast_id", "cycle_id"}}:
                raise ContractError("Opening admission changed original scientific evidence")
            if stopped() or instant(clock()) >= close:
                break
            queue.enqueue(mirrored)
            accepted[source["symbol"]] = source["forecast_id"]
            adopted.append(source["symbol"])
            store.event(session["session_id"], "opening_forecast_adopted", {
                "source_forecast_id": source["forecast_id"], "forecast_id": mirrored["forecast_id"],
                "original_issued_at": source["issued_at"], "target_open": source["target_open"],
                "target_close": source["target_close"], "admitted_at": link["admitted_at"], "refitted": False},
                symbol=source["symbol"])
        except (OSError, ValueError, KeyError, TypeError, sqlite3.DatabaseError) as exc:
            store.event(session["session_id"], "opening_forecast_rejected", {
                "source_forecast_id": row[0], "error_type": type(exc).__name__, "other_symbols_continue": True},
                symbol=row[2])
    if adopted:
        return {"status": "PUBLISHED", "symbols": adopted, "state": {**state, "opening_adopted": accepted},
                "opening_prepared": True}
    return None


def run_preparation(*, datastore, actor, symbols, config, wait=False, clock=None, pause=time.sleep,
                    stopped=lambda: False, runner_factory=None):
    """Existing model-owner invocation; collection remains entirely with 1A."""
    from datafetching.runtime_lock import exclusive_runtime_lock
    from .data import equity_sessions
    from .runtime import ShadowRunner
    from .split_handoff import ReadySnapshots
    from .split_model import model_pass
    from .store import ShadowStore, utcnow
    clock = clock or utcnow
    root = Path(datastore) / "intraday"
    reference, ready_after, target, close = opening_window(clock(),
        publication_offset_seconds=config.publication_offset_seconds)
    if not any(session.contains(reference) and session.contains(reference + 2 * STEP)
               for session in equity_sessions(reference, close)):
        return {"status": "NO_OPENING_MARKET_SESSION", "target_open": target.isoformat()}
    if instant(clock()) >= target:
        return {"status": "OPENING_ADMISSION_CLOSED", "target_open": target.isoformat(),
                "existing_predictions_preserved": True}
    if instant(clock()) < ready_after and not wait:
        return {"status": "WAITING_FOR_COMPLETED_OPENING_REFERENCE", "ready_after": ready_after.isoformat()}
    root.mkdir(parents=True, exist_ok=True)
    with exclusive_runtime_lock(root / "model-supervision/opening-preparation.lock",
                                process_name="intraday-opening-preparation"):
        while instant(clock()) < ready_after:
            if stopped():
                return {"status": "STOPPED", "orders_placed": 0}
            pause(min(1., max(0., (ready_after - instant(clock())).total_seconds())))
        store = ShadowStore(root / CACHE_NAME)
        handoff = ReadySnapshots(root / "system-1a/ready", actor=actor, symbols=symbols, clock=clock)
        science = ShadowStore(root / "opening-preparation/models.sqlite3",
            snapshot_directory=handoff.root / "snapshots")
        sink = PreparedForecasts(store, actor=actor, symbols=symbols, configuration=asdict(config), reference=reference)
        experiment = "opening-preparation:" + actor.lower() + ":" + target.astimezone(PACIFIC).date().isoformat()
        session = {"session_id": experiment, "actor": actor, "symbols": list(symbols),
            "started_at": instant(clock()).isoformat(), "close_at": close.isoformat(), "opening_preparation": True}
        state, completed = {}, set()
        while not stopped() and instant(clock()) < target:
            result = model_pass(store=store, queue=sink, session=session, config=config, handoff=handoff,
                state=state, clock=clock, stopped=stopped, runner_factory=runner_factory or ShadowRunner,
                science_store=science)
            state = result["state"]
            completed.update(result.get("symbols", []))
            if set(symbols) <= completed or not wait:
                break
            pause(1.)
        report = {"schema": VERSION, "status": "PREPARED" if set(symbols) <= completed else "PARTIAL_OPENING_PREPARATION",
            "actor": actor, "reference_at": reference.isoformat(), "target_open": target.isoformat(),
            "target_close": close.isoformat(), "completed_symbols": sorted(completed),
            "missing_symbols": sorted(set(symbols) - completed), "finished_at": instant(clock()).isoformat(),
            "ready_before_open": set(symbols) <= completed and instant(clock()) <= target,
            "orders_placed": 0, "broker_calls": 0, "trader_started": False}
        store.event(experiment, "opening_preparation_finished", report)
        return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--datastore", type=Path, required=True)
    parser.add_argument("--profile", type=Path, required=True)
    parser.add_argument("--watchlist", type=Path, required=True)
    parser.add_argument("--wait", action="store_true", help="Wait for this morning's reference; never start a trader")
    parser.add_argument("--publication-offset-seconds", type=float, default=30)
    parser.add_argument("--symbol-workers", type=int)
    parser.add_argument("--lookback-days", type=int)
    args = parser.parse_args(argv)
    from .resource_policy import manual_symbol_workers
    from .runtime import ResearchConfig, local_symbols, with_local_data_policy
    actor, symbols = local_symbols(args.profile, args.watchlist, args.datastore)
    config = with_local_data_policy(ResearchConfig(publication_offset_seconds=args.publication_offset_seconds,
        symbol_workers=manual_symbol_workers(args.datastore, actor=actor, explicit=args.symbol_workers),
        lookback_days=args.lookback_days), args.datastore)
    result = run_preparation(datastore=args.datastore, actor=actor, symbols=symbols, config=config, wait=args.wait)
    print(json.dumps(result, sort_keys=True))
    return 0 if result["status"] in {"PREPARED", "NO_OPENING_MARKET_SESSION"} else 2


if __name__ == "__main__":
    raise SystemExit(main())
