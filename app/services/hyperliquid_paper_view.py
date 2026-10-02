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
    real_equity_history: list[dict] = field(default_factory=list)
    real_accounts_error: str = ""
    forecasts: list[dict] = field(default_factory=list)
    model_history: list[dict] = field(default_factory=list)
    timings: list[dict] = field(default_factory=list)
    performance: dict = field(default_factory=dict)
    policy: dict = field(default_factory=dict)
    seed: dict = field(default_factory=dict)
    runtime: dict = field(default_factory=dict)
    market_recipe: dict = field(default_factory=dict)
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


def _journal_probabilities(forecast):
    probability = _number(forecast.get("p_not_down"))
    down = _number(forecast.get("p_down"))
    if probability is None or not 0 <= probability <= 1:
        return None
    if "p_down" in forecast and (down is None or not 0 <= down <= 1
            or not math.isclose(probability + down, 1, rel_tol=0, abs_tol=1e-9)):
        return None
    return probability, down


def journal_display_forecast(row):
    """Return only the valid forecast retained with this activity record.

    Excluded research remains context, never execution attribution. In
    particular, this projection does not populate the journal's signal fields
    or consult the latest market forecast to fill missing historical evidence.
    """
    row = {**_mapping(row.get("details")), **row}
    fields = ("coin", "p_not_down", "p_down", "qualified", "forecast_id", "prediction_id",
              "model_id", "data_run_id", "interval", "horizon_bars", "horizon_minutes",
              "created_at_utc", "forecast_created_at_utc", "decision_close_utc",
              "decision_timestamp_utc", "target_close_utc", "outcome_timestamp_utc", "outcome_at_utc",
              "model_published_at_utc", "model_created_at_utc", "training_cutoff_utc",
              "training_label_cutoff_utc", "calibration_cutoff_utc", "train_end_utc",
              "calibration_end_utc", "per_model")
    has_signal = (any(row.get(key) for key in ("forecast_id", "prediction_id", "model_id"))
                  or row.get("p_not_down") is not None or row.get("qualified") is not None)
    if has_signal:
        forecast = {key: row[key] for key in fields if key in row}
        observed = _mapping(_mapping(row.get("forecast_observation")).get("prediction"))
        forecast_id = row.get("forecast_id") or row.get("prediction_id")
        # A retained observation can supply dimensions and per-model scores,
        # but only for this exact forecast; a shared model ID alone is not enough.
        if (forecast_id and forecast_id == (observed.get("prediction_id") or observed.get("forecast_id"))
                and all(not row.get(key) or row[key] == observed.get(key) for key in ("coin", "model_id"))):
            forecast = {**observed, **forecast}
        usage, provenance = "attributed", "journal"
    else:
        forecast = _mapping(_mapping(row.get("policy")).get("rejected_forecast"))
        if not forecast or forecast.get("qualified") is not False:
            return {}
        usage, provenance = "excluded", "policy.rejected_forecast"
    if forecast.get("coin") and row.get("coin") and forecast["coin"] != row["coin"]:
        return {}
    probabilities = _journal_probabilities(forecast)
    if probabilities is None:
        return {}
    per_model = {}
    for name, value in _mapping(forecast.get("per_model")).items():
        source = value if isinstance(value, dict) else {"p_not_down": value}
        scores = _journal_probabilities(source)
        if scores is not None:
            per_model[name] = {"p_not_down": scores[0], "p_down": scores[1]}
    return {**forecast, "p_not_down": probabilities[0], "p_down": probabilities[1],
            "per_model": per_model, "qualification": _qualification(forecast),
            "usage": usage, "provenance": provenance}


def filter_rows(rows, *, account="all", asset="all", qualification="all", include_passive=True):
    """Filter journals consistently, including either participant in transfers.

    Accountless market decisions apply to every account. Qualification filters
    include retained excluded research, while never treating it as a qualified
    signal or inferring qualification from P/L. Transfers have no asset/model
    attribution in the current ledger.
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
        display = journal_display_forecast(row) if qualification != "all" else {}
        label = display.get("qualification", row.get("qualification", _qualification(row)))
        if qualification != "all" and label != qualification:
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
    full available period, preserving the first and last recorded observations
    and sampling interior time buckets when necessary. Every SQLite operation
    shares a time budget and read transaction.
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
        self._model_history_cache = {}

    def _prediction_history(self, path, warnings):
        """Read a bounded tail of native forecasts without loading model bundles."""
        try:
            stat = path.stat()
            signature = (stat.st_mtime_ns, stat.st_size)
            cached = self._model_history_cache.get(path)
            if cached and cached[0] == signature:
                return cached[1]
            # Parquet row groups are the smallest independently readable unit.
            # Refuse an oversized group rather than silently read an unbounded
            # journal. The latest JSON still provides the current observation.
            import pyarrow.parquet as parquet
            rows, budget, complete = [], 8 * 1024 * 1024, True
            with parquet.ParquetFile(path) as source:
                for index in range(source.num_row_groups - 1, -1, -1):
                    group = source.metadata.row_group(index)
                    if group.total_byte_size > budget or group.num_rows > 100000:
                        warnings.append(f"Model history tail exceeds the read limit: {path.parent}")
                        complete = False
                        break
                    budget -= group.total_byte_size
                    table = source.read_row_group(index)
                    take = min(72 - len(rows), table.num_rows)
                    rows = table.slice(table.num_rows - take, take).to_pylist() + rows
                    if len(rows) >= 72:
                        break
            if complete:
                self._model_history_cache[path] = (signature, rows)
            return rows
        except FileNotFoundError:
            return []
        except (OSError, ValueError, TypeError, ImportError) as exc:
            warnings.append(f"Cannot read model history: {type(exc).__name__}: {path.parent}")
            return []

    def _history_report(self, model, model_id, warnings):
        if not isinstance(model_id, str) or re.fullmatch(r"[A-Za-z0-9_-]+", model_id) is None:
            return {}, {}, "Model identity is unavailable or invalid"
        directory = model / "runs" / model_id
        if not directory.resolve().is_relative_to(model.resolve()):
            return {}, {}, "Model report path leaves its model directory"
        paths = (directory / "record.json", directory / "report.json")
        try:
            signature = tuple((p.stat().st_mtime_ns, p.stat().st_size) for p in paths)
            if any(size > 256 * 1024 for _, size in signature):
                return {}, {}, "Exact model report exceeds the view limit"
            cached = self._model_history_cache.get(directory)
            if cached and cached[0] == signature:
                return cached[1]
            record, _ = self._json(paths[0], warnings, optional=True)
            report, _ = self._json(paths[1], warnings, optional=True)
            result = (record, report, "") if record.get("model_id") == model_id and report else (
                {}, {}, "Exact model record/report is unavailable or has a different identity")
            # A bounded cache avoids repeatedly reading immutable fitted reports.
            if len(self._model_history_cache) >= 512:
                self._model_history_cache.clear()
            self._model_history_cache[directory] = (signature, result)
            return result
        except OSError:
            return {}, {}, "Exact model record/report is unavailable"

    def _model_history(self, snapshot, model, latest, coin, recipe, now, warnings):
        history = self._prediction_history(model / "predictions.parquet", warnings)
        by_id = {str(row.get("prediction_id")): row for row in history if row.get("prediction_id")}
        # The committed parquet record wins when the JSON projection and journal
        # temporarily disagree. No recent value fills a historical missing value.
        latest_key = str(latest.get("prediction_id") or "latest")
        if latest and latest_key not in by_id:
            by_id[latest_key] = latest
        rows = sorted(by_id.values(), key=lambda row: (
            _timestamp(str(row.get("decision_close_utc"))) or 0,
            _timestamp(str(row.get("created_at_utc"))) or 0,
            str(row.get("prediction_id") or "")), reverse=True)[:72]
        latest_id = rows[0].get("prediction_id") if rows else None
        interval, horizon = recipe["interval"], recipe["horizon_bars"]
        for prediction in rows:
            prediction = dict(prediction)
            for key in ("decision_close_utc", "created_at_utc", "target_close_utc"):
                if isinstance(prediction.get(key), datetime):
                    prediction[key] = prediction[key].isoformat()
            errors = []
            if (prediction.get("coin") != coin or prediction.get("interval") != interval
                    or prediction.get("horizon_bars") != horizon):
                errors.append("Forecast market or horizon does not match its journal")
            decision, created, target = (_timestamp(prediction.get(key)) for key in (
                "decision_close_utc", "created_at_utc", "target_close_utc"))
            if (None in (decision, created, target) or created > now + 5 or created < decision
                    or created >= target or target - decision != recipe["candle_seconds"] * horizon):
                errors.append("Forecast timestamps are unavailable or inconsistent")
            ensemble_probability = _journal_probabilities(prediction)
            if ensemble_probability is None:
                errors.append("Ensemble probability is unavailable or invalid")
            per_model = prediction.get("per_model")
            if per_model is None and isinstance(prediction.get("per_model_json"), str):
                try:
                    per_model = json.loads(prediction["per_model_json"])
                except (ValueError, TypeError):
                    errors.append("Saved member probabilities cannot be decoded")
            per_model = {name: value for name, value in _mapping(per_model).items()
                         if isinstance(name, str) and re.fullmatch(r"[A-Za-z0-9_-]{1,80}", name)}
            if len(per_model) > 32:
                errors.append("Saved member count exceeds the view limit")
                per_model = dict(list(per_model.items())[:32])
            record, report, report_error = self._history_report(model, prediction.get("model_id"), warnings)
            if report and any(record.get(key) != expected or report.get(key) != expected
                              for key, expected in (("coin", coin), ("interval", interval), ("horizon_bars", horizon))):
                record, report, report_error = {}, {}, "Exact model report belongs to a different market or horizon"
            if report and prediction.get("qualified") is True and report.get("eligible") is not True:
                errors.append("Saved qualification conflicts with the exact model report")
            model_names = report.get("model_names")
            model_names = {name for name in model_names[:32] if isinstance(name, str)
                           and re.fullmatch(r"[A-Za-z0-9_-]{1,80}", name)} if isinstance(model_names, list) else set(per_model)
            members = sorted(set(per_model) | model_names)[:32]
            weights = _mapping(report.get("ensemble_weights"))
            values = {name: _number(value) for name, value in weights.items()}
            if (set(values) != model_names or not values
                    or any(value is None or value <= 0 for value in values.values())):
                weights = {}
            else:
                scale = max(values.values())
                total = sum(value / scale for value in values.values())
                weights = {name: value / scale / total for name, value in values.items()}
            metrics = _mapping(report.get("metrics"))
            baselines = {name: self._history_metrics(_mapping(metrics.get(name)))
                         for name in ("prior_baseline", "neutral_baseline")}
            reasons = report.get("eligibility_reasons", [])
            reasons = [str(value) for value in reasons[:20]] if isinstance(reasons, list) else []
            if report_error:
                reasons.append(report_error)
            splits = _mapping(report.get("splits"))
            common = {key: prediction.get(key) for key in ("prediction_id", "model_id", "data_run_id",
                "created_at_utc", "decision_close_utc", "target_close_utc", "qualified")}
            common.update(coin=coin, interval=interval, horizon_bars=horizon,
                horizon_minutes=recipe["horizon_minutes"], qualification=_qualification(prediction),
                is_latest=prediction.get("prediction_id") == latest_id,
                forecast_state="invalid" if errors else "expired" if target <= now else "current",
                eligibility_reasons=reasons, baseline_metrics=baselines,
                model_published_at_utc=record.get("trained_at_utc"),
                training_cutoff_utc=_mapping(splits.get("fit")).get("last_decision_close_utc"),
                calibration_cutoff_utc=_mapping(splits.get("calibration")).get("last_decision_close_utc"),
                metric_source="exact_model_report" if report else "unavailable", source="shared_model",
                evaluation_role=report.get("evaluation_role"), report_eligible=report.get("eligible"))
            for name in ("ensemble", *members):
                member = prediction if name == "ensemble" else per_model.get(name)
                member = member if isinstance(member, dict) else {"p_not_down": member}
                probability = _journal_probabilities(member)
                row_errors = list(errors)
                if probability is None and name != "ensemble":
                    row_errors.append("Member probability is unavailable or invalid")
                snapshot.model_history.append({**common, **self._history_metrics(_mapping(metrics.get(name))),
                    "family": name, "weight": weights.get(name),
                    "p_not_down": probability[0] if probability else None,
                    "p_down": probability[1] if probability else None,
                    "forecast_state": "invalid" if row_errors else common["forecast_state"],
                    "validation_errors": row_errors})

    @staticmethod
    def _history_metrics(metrics):
        result = {}
        for key, maximum in (("brier_score", 1), ("log_loss", None), ("accuracy", 1)):
            value = _number(metrics.get(key))
            result[key] = value if value is not None and value >= 0 and (maximum is None or value <= maximum) else None
        rows = metrics.get("rows")
        result["assessment_rows"] = rows if type(rows) is int and rows >= 0 else None
        return result

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
                prepared = (key == "paper" and payload.get("stop_reason") == "prepare_only"
                            and payload.get("lifecycle_phase") == "opening_prepared")
                detail = detail or ("Opening prepared; stopped intentionally before strategy cycles" if prepared else
                                    "Runtime process is absent" if alive is False else str(reported))
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
        if "decision_id" in result or "fill_id" in result:
            result["display_forecast"] = journal_display_forecast(result)
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
                interior_slots = max(0, self.history_limit // 4 - 2)
                bucket = max((bounds[0]["last"] - bounds[0]["first"]) / max(1, interior_slots), 1 / 86400000)
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
                      (PARTITION BY account ORDER BY observation_id ROWS UNBOUNDED PRECEDING)) AS peak,
                      ROW_NUMBER() OVER (PARTITION BY account ORDER BY observation_id) AS first_rank,
                      ROW_NUMBER() OVER (PARTITION BY account ORDER BY observation_id DESC) AS last_rank
                    FROM curve
                ), ranked AS (
                    SELECT *,CASE WHEN peak>0 THEN (baseline+total_pnl)/peak-1 END AS drawdown_fraction,
                      ROW_NUMBER() OVER (PARTITION BY account,CASE WHEN first_rank=1 OR last_rank=1 THEN -1
                        ELSE MIN(?-1,CAST((julianday(timestamp_utc)-?)/? AS INTEGER)) END
                        ORDER BY observation_id DESC) AS bucket_rank
                    FROM peaks
                ) SELECT * FROM ranked WHERE ?=0 OR first_rank=1 OR last_rank=1 OR (? > 0 AND bucket_rank=1)
                  ORDER BY observation_id"""
                snapshot.equity_history = query("equity", curve_sql,
                    (interior_slots, bounds[0]["first"], bucket, snapshot.history_sampled, interior_slots))
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

    def _market_recipe(self, snapshot, now, warnings):
        """Resolve the displayed experiment without importing an execution runtime."""
        from datafetching.hyperliquid_candles import INTERVAL_MS
        from ml.hyperliquid_model_config import DEFAULT_MODEL_CONFIG_PATH, load_config

        models = _mapping(snapshot.runtime.get("models"))
        coordinator = _mapping(snapshot.runtime.get("coordinator"))
        config_path = models.get("config_path") or snapshot.policy.get("model_config")
        if not config_path and self.data_root == DEFAULT_DATA_ROOT.resolve():
            config_path = DEFAULT_MODEL_CONFIG_PATH
        try:
            if config_path:
                model_config = load_config(config_path)
                markets = model_config.load_markets()
                if markets.output_root.resolve() != self.data_root:
                    raise ValueError("Configured model datastore differs from this workspace")
                interval, horizon, symbols = markets.interval, model_config.horizons_bars[0], markets.symbols
            else:
                # Older saved workspaces have no configuration provenance. Their
                # reported recipe takes precedence over the original defaults.
                interval = coordinator.get("interval", "15m")
                horizons = models.get("horizons_bars", [snapshot.runtime.get("horizon_bars", 4)])
                if not isinstance(horizons, (list, tuple)) or not horizons:
                    raise ValueError("Reported forecast horizons are unavailable")
                horizon = horizons[0]
                symbols = coordinator.get("symbols", SYMBOLS)
            if interval not in INTERVAL_MS or type(horizon) is not int or horizon < 1:
                raise ValueError("Unsupported candle interval or forecast horizon")
            if (not isinstance(symbols, (list, tuple)) or not symbols
                    or any(not isinstance(coin, str) or re.fullmatch(r"[A-Z0-9]{1,20}", coin) is None for coin in symbols)):
                raise ValueError("Configured market symbols are unavailable")
            seconds = INTERVAL_MS[interval] / 1000
            snapshot.market_recipe = {"interval": interval, "horizon_bars": horizon,
                "horizon_minutes": seconds * horizon / 60, "candle_seconds": seconds,
                "symbols": list(symbols)}
            return snapshot.market_recipe
        except (OSError, ValueError, TypeError, KeyError, IndexError) as exc:
            detail = f"Market configuration unavailable: {exc}"
            warnings.append(detail)
            snapshot.sources["market_configuration"] = self._source(
                "market_configuration", None, now, None, state="partial", detail=detail)
            return None

    def _markets(self, snapshot, now, warnings):
        recipe = self._market_recipe(snapshot, now, warnings)
        if recipe is None:
            return
        interval, horizon = recipe["interval"], recipe["horizon_bars"]
        candle_seconds, symbols = recipe["candle_seconds"], recipe["symbols"]
        training_cadence = _number(_mapping(snapshot.runtime.get("models")).get("retrain_seconds"))
        if training_cadence is None or training_cadence <= 0:
            training_cadence = 3600
        for coin in symbols:
            base = self.data_root / coin / interval
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
            snapshot.sources[f"data:{coin}"] = self._source(f"data:{coin}", stamp, now, candle_seconds,
                                                            state=state, detail=detail, grace=candle_seconds + 120)
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
            model = self.data_root / "_models" / coin / interval / f"h{horizon}"
            prediction, prediction_error = self._json(model / "latest_prediction.json", warnings)
            self._model_history(snapshot, model, prediction, coin, recipe, now, warnings)
            source = self._source(f"forecast:{coin}", prediction.get("created_at_utc"), now, candle_seconds,
                                 state=prediction_error, grace=candle_seconds + 120)
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
                if bars != horizon or prediction.get("interval", interval) != interval:
                    validation_errors.append(f"Forecast horizon unavailable or inconsistent with the {interval}/h{horizon} source")
                    bars = None
                target = _timestamp(prediction.get("target_close_utc"))
                if target is not None and target <= now:
                    source = replace(source, state="stale", detail="Forecast outcome has already matured")
                if validation_errors:
                    source = replace(source, state="partial", detail="; ".join(validation_errors))
                forecast = {**prediction, "coin": coin, "interval": interval, "qualification": _qualification(prediction),
                            "source": "shared_model", "source_state": source.state,
                            "p_not_down": probability, "p_down": down,
                            "horizon_bars": int(bars) if bars is not None else None,
                            "horizon_minutes": bars * candle_seconds / 60 if bars is not None else None,
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
                identity = str(job.get("market", "")).split("/")
                coin = identity[0]
                if coin in symbols and identity[1:] == [interval, f"h{horizon}"]:
                    latest[coin] = {**job, "coin": coin, "at_utc": job["timing"].get("completed_at_utc")}
        for event in events:
            if (event.get("coin") in symbols and isinstance(event.get("timing"), dict)
                    and event.get("interval", "15m") == interval
                    and event.get("horizon_bars", 4) == horizon):
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
            now, 900, state=error,
            detail="Point-in-time export: every 15 minutes or after trading changes. Current balances and chart read the ledger.")
        self._markets(snapshot, now, warnings)
        snapshot.warnings = tuple(dict.fromkeys(warnings))
        return snapshot
