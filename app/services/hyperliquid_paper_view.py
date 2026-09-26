"""Bounded, read-only projections for the H.Y.P.E.R. review workspace.

This module deliberately does not import a runtime or instantiate PaperLedger.
The latest committed cycle and journals are read in one SQLite snapshot. JSON
files provide operational evidence, never replacement portfolio balances.
"""
from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
import json
import math
from pathlib import Path
import re
import sqlite3
import time
from typing import Callable


DEFAULT_DATA_ROOT = Path("C:/DATASTORE/hyperliquid")
ACCOUNT_ROLES = {"alex": "short_perp", "jeremy": "long_perp", "clearpond": "spot"}
SYMBOLS = ("BTC", "ETH", "HYPE", "ZEC")


@dataclass(frozen=True)
class SourceState:
    name: str
    state: str
    observed_at_utc: str | None = None
    age_seconds: float | None = None
    cadence_seconds: float | None = None
    detail: str = ""


@dataclass
class PaperViewSnapshot:
    observed_at_utc: str = ""
    portfolio_observed_at_utc: str | None = None
    pooled: dict = field(default_factory=dict)
    accounts: dict = field(default_factory=dict)
    positions: list[dict] = field(default_factory=list)
    decisions: list[dict] = field(default_factory=list)
    fills: list[dict] = field(default_factory=list)
    transfers: list[dict] = field(default_factory=list)
    equity_history: list[dict] = field(default_factory=list)
    forecasts: list[dict] = field(default_factory=list)
    timings: list[dict] = field(default_factory=list)
    performance: dict = field(default_factory=dict)
    policy: dict = field(default_factory=dict)
    seed: dict = field(default_factory=dict)
    runtime: dict = field(default_factory=dict)
    sources: dict[str, SourceState] = field(default_factory=dict)
    warnings: tuple[str, ...] = ()
    history_sampled: bool = False
    powder: PaperViewSnapshot | None = None


def _mapping(value):
    return value if isinstance(value, dict) else {}


def _number(value):
    if isinstance(value, bool):
        return None
    try:
        value = float(value)
        return value if math.isfinite(value) else None
    except (ValueError, TypeError):
        return None


def _timestamp(value):
    if not isinstance(value, str):
        return None
    try:
        stamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return stamp.timestamp() if stamp.tzinfo is not None else None
    except (ValueError, OverflowError):
        return None


def _qualification(row):
    flag = row.get("qualified")
    return "qualified" if flag is True else "research" if flag is False else "unavailable"


def filter_rows(rows, *, account="all", asset="all", qualification="all", include_passive=True):
    """Filter journals consistently, including either participant in transfers.

    Accountless market decisions apply to every account. Qualification filters
    exclude records without a recorded model qualification; they never infer it
    from P/L. Transfers have no asset/model attribution in the current ledger.
    """
    account = str(account or "all").lower().replace(" ", "")
    asset = str(asset or "all").upper()
    qualification = str(qualification or "all").lower()
    result = []
    for row in rows:
        if account not in {"all", "pooled", "pool"}:
            participants = {row.get("account"), row.get("from_account"), row.get("to_account")}
            if any(participants) and account not in participants:
                continue
        if asset != "ALL" and str(row.get("coin", "")).upper() != asset:
            continue
        if qualification != "all" and row.get("qualification", _qualification(row)) != qualification:
            continue
        if not include_passive and row.get("passive"):
            continue
        result.append(row)
    return result


def _process_matches(pid, module):
    """Inspect process identity without opening or touching runtime lock files."""
    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
        return None
    try:
        import psutil
    except ImportError:
        return None
    try:
        process = psutil.Process(pid)
        return process.is_running() and module in " ".join(process.cmdline())
    except psutil.NoSuchProcess:
        return False
    except (psutil.AccessDenied, OSError):
        return None


class HyperliquidPaperViewService:
    """Read-only service; construct freely and call load_snapshot off the UI thread.

    Journals retain the most recent ``journal_limit`` records. Equity covers the
    full available period, using the last observation in each time bucket when
    necessary. Every SQLite operation shares a time budget and read transaction.
    """

    def __init__(self, data_root=DEFAULT_DATA_ROOT, *, history_limit=6000,
                 journal_limit=500, clock: Callable[[], float] = time.time,
                 read_timeout_seconds=2.0, process_probe=_process_matches):
        self.data_root = Path(data_root).resolve()
        self.history_limit = max(8, min(int(history_limit), 20000))
        self.journal_limit = max(1, min(int(journal_limit), 2000))
        self.clock = clock
        self.read_timeout_seconds = max(.05, min(float(read_timeout_seconds), 5.0))
        self.process_probe = process_probe

    @staticmethod
    def _json(path, warnings, *, optional=False):
        try:
            with path.open("rb") as stream:
                content = stream.read(2 * 1024 * 1024 + 1)
            if len(content) > 2 * 1024 * 1024:
                raise ValueError("JSON exceeds the 2 MiB view limit")
            value = json.loads(content)
            if not isinstance(value, dict):
                raise ValueError("expected an object")
            return value, None
        except FileNotFoundError:
            if not optional:
                warnings.append(f"Missing {path.name}: {path.parent}")
            return {}, "missing"
        except (OSError, ValueError, UnicodeError) as exc:
            warnings.append(f"Cannot read {path.name}: {type(exc).__name__}")
            return {}, "partial"

    @staticmethod
    def _source(name, stamp, now, cadence, *, state=None, detail="", grace=None):
        seconds = _timestamp(stamp)
        age = now - seconds if seconds is not None else None
        if state is None:
            if seconds is None:
                state = "partial"
                detail = detail or "Observation timestamp unavailable"
            elif age < -5:
                state = "partial"
                detail = detail or "Observation timestamp is in the future"
            else:
                state = "stale" if age > (grace if grace is not None else cadence * 2) else "fresh"
        return SourceState(name, state, stamp, age, cadence, detail)

    def _runtime(self, path, key, module, cadence, now, snapshot, warnings):
        payload, error = self._json(path, warnings)
        reported = payload.get("status")
        alive = self.process_probe(payload.get("pid"), module) if payload else None
        state = error
        detail = str(payload.get("last_error") or payload.get("config_error") or "")
        if error is None:
            if reported in {"stopped", "not_started", "failed"} or alive is False:
                state = "stopped"
                detail = detail or ("Runtime process is absent" if alive is False else str(reported))
            elif reported in {"degraded", "error"} or detail or any(payload.get(k) for k in ("errors", "quote_errors", "funding_errors")):
                state = "partial"
                detail = detail or "Runtime reports source errors; inspect operation details"
            elif alive is None:
                state = "partial"
                detail = "Process identity unverified; saved status is not proof of a running process"
        source = self._source(key, payload.get("updated_at_utc"), now, cadence,
                              state=state, detail=detail)
        # A stale observation remains visible even when the PID is still alive.
        if source.age_seconds is not None and source.age_seconds > cadence * 3 and source.state == "fresh":
            source = replace(source, state="stale")
        snapshot.sources[key] = source
        payload = dict(payload)
        payload.update(reported_status=reported, process_alive=alive)
        payload["status"] = ("running" if source.state == "fresh" else source.state)
        return payload

    @staticmethod
    def _decoded(row, warnings):
        row = dict(row)
        details = {}
        if row.get("details_json"):
            try:
                details = json.loads(row["details_json"])
                if not isinstance(details, dict):
                    raise ValueError("Journal details must be an object")
            except (ValueError, TypeError):
                details = {}
                warnings.append("A journal row has incomplete details_json")
                row["details_unavailable"] = True
        result = {**details, **row, "details": details, "source": "paper"}
        result["qualification"] = _qualification(result)
        if "transfer_id" in result:
            result["status"] = "committed"
        return result

    def _ledger(self, snapshot, now, warnings):
        path = self.data_root / "_paper" / "ledger.sqlite3"
        if not path.is_file():
            snapshot.sources["ledger"] = self._source("ledger", None, now, 30, state="missing")
            warnings.append("Paper ledger is missing; no balances have been inferred")
            return
        connection = None
        ledger_warnings = []
        try:
            connection = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True,
                                         timeout=min(.25, self.read_timeout_seconds), isolation_level=None)
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA query_only=ON")
            deadline = time.monotonic() + self.read_timeout_seconds
            connection.set_progress_handler(lambda: int(time.monotonic() > deadline), 2000)
            connection.execute("BEGIN")
            tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}

            def query(table, sql, parameters=()):
                if table not in tables:
                    ledger_warnings.append(f"Ledger table unavailable: {table}")
                    return []
                try:
                    return [dict(row) for row in connection.execute(sql, parameters)]
                except sqlite3.DatabaseError as exc:
                    ledger_warnings.append(f"Cannot read {table}: {exc}")
                    return []

            cycles = query("cycles", "SELECT * FROM cycles ORDER BY rowid DESC LIMIT 1")
            cycle = cycles[0] if cycles else {}
            try:
                state = _mapping(_mapping(json.loads(cycle.get("result_json", "{}"))).get("state"))
            except (TypeError, ValueError):
                state = {}
                ledger_warnings.append("Latest committed cycle has incomplete state")
            snapshot.portfolio_observed_at_utc = cycle.get("timestamp_utc")
            snapshot.accounts = {key: dict(value) for key, value in _mapping(state.get("accounts")).items()
                                 if isinstance(value, dict)}
            snapshot.pooled = dict(_mapping(state.get("pooled")))
            accounts = query("accounts", "SELECT * FROM accounts LIMIT 20")
            seed = query("seed", "SELECT details_json FROM seed WHERE singleton=1")
            if seed:
                try:
                    snapshot.seed = _mapping(json.loads(seed[0]["details_json"]))
                except (TypeError, ValueError):
                    ledger_warnings.append("Opening seed details unavailable")

            # Equity rows from the same cycle override corresponding marked fields.
            latest = query("equity", "SELECT * FROM equity WHERE cycle_id=?", (cycle.get("cycle_id"),)) if cycle else []
            if not latest:
                latest = query("equity", "SELECT * FROM equity WHERE cycle_id=(SELECT cycle_id FROM equity ORDER BY rowid DESC LIMIT 1)")
                if latest:
                    # A damaged/partial ledger may have valuation rows without
                    # the matching cycle. None of the older cycle's marks,
                    # account positions or derived totals belong to this time.
                    if cycle and latest[0].get("cycle_id") != cycle.get("cycle_id"):
                        snapshot.pooled = {}
                        snapshot.accounts = {}
                        ledger_warnings.append("Equity and cycle observations differ; cycle marks and derived fields withheld")
                    snapshot.portfolio_observed_at_utc = latest[0].get("timestamp_utc")
                elif cycle:
                    ledger_warnings.append("Equity rows unavailable for the displayed cycle valuation")
                if not cycle:
                    ledger_warnings.append("Complete cycle state unavailable; marks may be missing")
            account_baselines = {row["account"]: row for row in accounts}
            for row in latest:
                key = row["account"]
                if key == "pooled":
                    snapshot.pooled.update(row)
                else:
                    # Only opening fields can safely supplement an older or
                    # partial valuation. Current cash/transfers must not leak
                    # into a differently timestamped equity observation.
                    baseline = {name: value for name, value in account_baselines.get(key, {}).items()
                                if name in {"initial_cash", "initial_equity", "initial_unrealized_pnl"}}
                    snapshot.accounts[key] = {**baseline, **snapshot.accounts.get(key, {}), **row,
                                              "role": ACCOUNT_ROLES.get(key)}
                    if "net_transfers" not in snapshot.accounts[key]:
                        opening = _number(baseline.get("initial_equity"))
                        equity, pnl = _number(row.get("equity")), _number(row.get("total_pnl"))
                        if None not in (opening, equity, pnl):
                            snapshot.accounts[key]["net_transfers"] = equity - opening - pnl
            if "initial_equity" not in snapshot.pooled and accounts:
                initial = [_number(row.get("initial_equity")) for row in accounts]
                if all(value is not None for value in initial):
                    snapshot.pooled["initial_equity"] = sum(initial)
            # Do not recompute P/L from realized P/L or current account cash.
            for values in [snapshot.pooled, *snapshot.accounts.values()]:
                initial, pnl = _number(values.get("initial_equity")), _number(values.get("total_pnl"))
                values["return_fraction"] = pnl / initial if initial and pnl is not None else None

            recorded_positions = snapshot.pooled.get("positions")
            if not isinstance(recorded_positions, list):
                recorded_positions = []
            marked = {(p.get("account"), p.get("coin"), p.get("kind")): p
                      for p in recorded_positions if isinstance(p, dict)}
            inventory = query("positions", "SELECT * FROM positions ORDER BY account,kind,coin LIMIT 2000")
            for row in inventory:
                key = row.get("account"), row.get("coin"), row.get("kind")
                existing = marked.get(key, {})
                # Reuse marks only for the inventory recorded in the same transaction.
                if existing and (existing.get("quantity") != row.get("quantity") or existing.get("avg_entry") != row.get("avg_entry")):
                    existing = {}
                    ledger_warnings.append("Position differs from latest cycle; current mark unavailable")
                snapshot.positions.append({**existing, **row, "source": "paper",
                    "passive": row.get("kind") == "spot" and row.get("account") != "clearpond",
                    "side": "long" if row.get("quantity", 0) > 0 else "short",
                    "timestamp_utc": snapshot.portfolio_observed_at_utc})
            for table in ("decisions", "fills", "transfers"):
                rows = query(table, f"SELECT * FROM {table} ORDER BY rowid DESC LIMIT ?", (self.journal_limit,))
                setattr(snapshot, table, [self._decoded(row, ledger_warnings) for row in rows])

            bounds = query("equity", "SELECT COUNT(*) AS n, MIN(julianday(timestamp_utc)) AS first, MAX(julianday(timestamp_utc)) AS last FROM equity")
            if bounds and bounds[0]["n"] and bounds[0]["first"] is not None and bounds[0]["last"] is not None:
                count = bounds[0]["n"]
                bucket = max((bounds[0]["last"] - bounds[0]["first"]) / max(1, self.history_limit // 4 - 1), 1 / 86400000)
                snapshot.history_sampled = count > self.history_limit
                # Drawdown uses transfer-adjusted opening equity + total_pnl. The
                # running peak is calculated BEFORE sampling, so omitted peaks
                # still affect the following drawdown observations.
                curve_sql = """WITH curve AS (
                    SELECT e.rowid AS observation_id,e.*,
                      COALESCE(a.initial_equity,(SELECT SUM(initial_equity) FROM accounts)) AS baseline
                    FROM equity e LEFT JOIN accounts a ON a.account=e.account
                ), peaks AS (
                    SELECT *,MAX(baseline,MAX(baseline+total_pnl) OVER
                      (PARTITION BY account ORDER BY observation_id ROWS UNBOUNDED PRECEDING)) AS peak
                    FROM curve
                ), ranked AS (
                    SELECT *,CASE WHEN peak>0 THEN (baseline+total_pnl)/peak-1 END AS drawdown_fraction,
                      ROW_NUMBER() OVER (PARTITION BY account,CASE WHEN ? THEN CAST((julianday(timestamp_utc)-?)/? AS INTEGER)
                        ELSE observation_id END ORDER BY observation_id DESC) AS bucket_rank
                    FROM peaks
                ) SELECT * FROM ranked WHERE bucket_rank=1 ORDER BY observation_id"""
                snapshot.equity_history = query("equity", curve_sql, (snapshot.history_sampled, bounds[0]["first"], bucket))
            if not snapshot.pooled:
                ledger_warnings.append("No committed portfolio valuation is available")
            if any(key not in snapshot.accounts for key in ACCOUNT_ROLES):
                ledger_warnings.append("One or more account valuations are unavailable")
            snapshot.sources["ledger"] = self._source("ledger", snapshot.portfolio_observed_at_utc, now, 30,
                state="partial" if ledger_warnings else None, detail="; ".join(ledger_warnings))
        except (sqlite3.DatabaseError, OSError, ValueError, TypeError) as exc:
            ledger_warnings.append(f"Ledger read unavailable: {type(exc).__name__}: {exc}")
            snapshot.sources["ledger"] = self._source("ledger", snapshot.portfolio_observed_at_utc, now, 30,
                                                       state="error", detail=str(exc))
        finally:
            if connection is not None:
                connection.set_progress_handler(None, 0)
                connection.rollback()
                connection.close()
        warnings.extend(ledger_warnings)

    @staticmethod
    def _tail_events(path, warnings):
        try:
            with path.open("rb") as stream:
                stream.seek(0, 2)
                start = max(0, stream.tell() - 256 * 1024)
                stream.seek(start)
                content = stream.read()
            lines = content.splitlines()
            if start:
                lines = lines[1:]
            events = []
            for line in lines:
                try:
                    event = json.loads(line)
                    if isinstance(event, dict):
                        events.append(event)
                except (ValueError, UnicodeError):
                    warnings.append(f"Skipped incomplete timing event in {path.name}")
            return events
        except FileNotFoundError:
            return []
        except OSError as exc:
            warnings.append(f"Timing journal unavailable: {type(exc).__name__}")
            return []

    def _markets(self, snapshot, now, warnings):
        training_cadence = _number(_mapping(snapshot.runtime.get("models")).get("retrain_seconds"))
        if training_cadence is None or training_cadence <= 0:
            training_cadence = 3600
        for coin in SYMBOLS:
            base = self.data_root / coin / "15m"
            loop, error = self._json(base / "loop_status.json", warnings)
            pointer, pointer_error = self._json(base / "latest.json", warnings)
            summary = {}
            run_id = pointer.get("run_id")
            if isinstance(run_id, str) and re.fullmatch(r"[A-Za-z0-9_-]+", run_id):
                summary, summary_error = self._json(base / "runs" / run_id / "summary.json", warnings)
                pointer_error = pointer_error or summary_error
            last = _mapping(loop.get("last_result"))
            stamp = summary.get("last_close_utc") or last.get("last_close_utc")
            detail = str(loop.get("last_error") or "")
            state = error or pointer_error
            if detail or summary.get("latest_row_missing_features"):
                state = "partial"
                detail = detail or "Latest feature row is incomplete"
            if loop.get("status") in {"stopped", "failed"}:
                state = "stopped"
            snapshot.sources[f"data:{coin}"] = self._source(f"data:{coin}", stamp, now, 900,
                                                            state=state, detail=detail, grace=1020)
            timing = _mapping(loop.get("last_cycle_timing"))
            stages = _mapping(timing.get("timings_seconds"))
            if timing:
                work = _number(stages.get("total_before_publish", stages.get("total_before_return")))
                cycle_seconds = _number(timing.get("total_seconds"))
                queue = _number(timing.get("queue_wait_seconds"))
                residual = (max(0, cycle_seconds - queue - work)
                            if None not in (cycle_seconds, queue, work) else None)
                snapshot.timings.append({"kind": "data", "coin": coin,
                    "at_utc": timing.get("finished_at_utc"), "work_seconds": work,
                    "cycle_seconds": cycle_seconds, "queue_wait_seconds": queue,
                    "poll_wait_seconds": None, "publication_seconds": None,
                    "publication_and_overhead_seconds": residual,
                    "publication_note": "Publication is not separately measured; residual also includes loop overhead",
                    "fetch_seconds": _number(stages.get("fetch_and_normalize")),
                    "feature_seconds": _number(stages.get("feature_build")),
                    "parquet_write_seconds": _number(stages.get("parquet_and_catalog_write")),
                    "before_publish_seconds": _number(stages.get("total_before_publish")),
                    "details": timing})
            model = self.data_root / "_models" / coin / "15m" / "h4"
            prediction, prediction_error = self._json(model / "latest_prediction.json", warnings)
            source = self._source(f"forecast:{coin}", prediction.get("created_at_utc"), now, 900,
                                 state=prediction_error, grace=1020)
            if prediction:
                validation_errors = []
                probability = _number(prediction.get("p_not_down"))
                down = _number(prediction.get("p_down"))
                if probability is None or not 0 <= probability <= 1:
                    validation_errors.append("Forecast P(not-down) unavailable or invalid")
                    probability = down = None
                elif "p_down" in prediction and (down is None or not 0 <= down <= 1
                        or not math.isclose(probability + down, 1, rel_tol=0, abs_tol=1e-9)):
                    validation_errors.append("Forecast probabilities are invalid or do not sum to one")
                    probability = down = None
                bars = _number(prediction.get("horizon_bars"))
                if bars != 4 or prediction.get("interval", "15m") != "15m":
                    validation_errors.append("Forecast horizon unavailable or inconsistent with the 15m/h4 source")
                    bars = None
                if validation_errors:
                    source = replace(source, state="partial", detail="; ".join(validation_errors))
                forecast = {**prediction, "coin": coin, "qualification": _qualification(prediction),
                            "source": "shared_model", "source_state": source.state,
                            "p_not_down": probability, "p_down": down,
                            "horizon_bars": int(bars) if bars is not None else None,
                            "horizon_minutes": bars * 15 if bars is not None else None,
                            "validation_errors": validation_errors}
                record, report = {}, {}
                model_id = prediction.get("model_id")
                if isinstance(model_id, str) and re.fullmatch(r"[A-Za-z0-9_-]+", model_id):
                    record, _ = self._json(model / "runs" / model_id / "record.json", warnings, optional=True)
                    report, _ = self._json(model / "runs" / model_id / "report.json", warnings, optional=True)
                splits = _mapping(report.get("splits"))
                forecast.update(model_published_at_utc=record.get("trained_at_utc"),
                    training_cutoff_utc=_mapping(splits.get("fit")).get("last_decision_close_utc"),
                    training_label_cutoff_utc=_mapping(splits.get("fit")).get("last_label_end_utc"),
                    calibration_cutoff_utc=_mapping(splits.get("calibration")).get("last_decision_close_utc"),
                    provenance={"record": record, "splits": splits})
                snapshot.forecasts.append(forecast)
            snapshot.sources[f"forecast:{coin}"] = source
            candidate, candidate_error = self._json(model / "candidate.json", warnings, optional=True)
            snapshot.sources[f"training:{coin}"] = self._source(f"training:{coin}", candidate.get("trained_at_utc"),
                now, training_cadence, state=candidate_error, detail="Candidate publication; not the fitting-data cutoff")

        events = self._tail_events(self.data_root / "_models" / "_runtime" / "training_events.jsonl", warnings)
        latest = {}
        # Runtime keeps the most recent completed jobs even if an event journal
        # has not been created yet. Event rows below enrich/replace this fallback.
        for job in snapshot.runtime.get("models", {}).get("completed_jobs", []) or []:
            if isinstance(job, dict) and isinstance(job.get("timing"), dict):
                coin = str(job.get("market", "")).split("/")[0]
                if coin in SYMBOLS:
                    latest[coin] = {**job, "coin": coin, "at_utc": job["timing"].get("completed_at_utc")}
        for event in events:
            if event.get("coin") in SYMBOLS and isinstance(event.get("timing"), dict):
                latest[event["coin"]] = event
        for coin, event in latest.items():
            timing = event["timing"]
            stages = _mapping(event.get("model_timings"))
            metrics = {}
            for name in ("fit", "calibration", "assessment"):
                values = [_number(_mapping(value).get(f"{name}_seconds")) for value in stages.values()]
                metrics[f"{name}_seconds"] = sum(values) if values and all(v is not None for v in values) else None
            snapshot.timings.append({**timing, **metrics, "kind": "model", "coin": coin,
                "at_utc": event.get("at_utc"), "outcome": event.get("outcome"),
                "work_seconds": _number(timing.get("worker_seconds")),
                "queue_wait_seconds": _number(timing.get("queue_wait_seconds")),
                "poll_wait_seconds": _number(timing.get("collection_wait_seconds")),
                "publication_seconds": _number(timing.get("publication_seconds")), "details": event})

    def load_snapshot(self):
        now = self.clock()
        snapshot = PaperViewSnapshot(observed_at_utc=datetime.fromtimestamp(now, timezone.utc).isoformat())
        warnings = []
        self._ledger(snapshot, now, warnings)
        paper = self.data_root / "_paper"
        snapshot.runtime = self._runtime(paper / "_runtime" / "status.json", "paper",
            "hyperliquid_paper_runtime", 30, now, snapshot, warnings)
        # Avoid exposing a second, potentially older set of balances to the UI.
        snapshot.runtime.pop("portfolio", None)
        snapshot.runtime["models"] = self._runtime(self.data_root / "_models" / "_runtime" / "status.json",
            "models", "hyperliquid_model_runtime", 5, now, snapshot, warnings)
        snapshot.runtime["coordinator"] = self._runtime(self.data_root / "_coordinator" / "coordinator_status.json",
            "coordinator", "hyperliquid_coordinator", 5, now, snapshot, warnings)
        snapshot.policy, _ = self._json(paper / "policy.json", warnings, optional=True)
        snapshot.performance, error = self._json(paper / "performance.json", warnings, optional=True)
        snapshot.sources["performance"] = self._source("performance", snapshot.performance.get("as_of_utc"),
            now, 300, state=error, detail="Point-in-time export; may lag the authoritative ledger")
        self._markets(snapshot, now, warnings)
        snapshot.warnings = tuple(dict.fromkeys(warnings))
        return snapshot
