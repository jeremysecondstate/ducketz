"""Inspect local 15m prerequisites without starting, enabling or calling anything.

The installed implementation has no live intraday trader. This report therefore
always keeps manual_live_start_ready false, even with complete fresh forecasts.
It reads saved evidence only; it does not establish current provider or broker
availability, authenticate, train, open an account ledger or supervise workers.
"""
from __future__ import annotations

import argparse
import ast
from contextlib import closing, contextmanager
from dataclasses import asdict
from datetime import datetime, timezone
from decimal import Decimal
import hashlib
import json
from pathlib import Path
import re
import sqlite3
from tempfile import TemporaryDirectory

from .execution_policy import submission_gate
from .prediction import ContractError, PACIFIC, STEP, TARGET_VERSION, digest, instant, number

VERSION = "intraday-read-only-readiness-v1"
SYMBOL = re.compile(r"[A-Z][A-Z0-9.-]{0,15}")
HASH = re.compile(r"[a-f0-9]{64}")
SOURCE_IDS = {name: f"databento:{name}:ohlcv-1m" for name in ("EQUS.MINI", "XNAS.ITCH")}
LIVE_GAPS = (
    "INTRADAY_LIVE_BROKER_ADAPTER_AND_MANUAL_SESSION_WORKER_NOT_IMPLEMENTED",
    "GAMEPLAN_AND_15M_SHARED_LIVE_RESERVATION_INTEGRATION_NOT_IMPLEMENTED",
    "SHARED_CANCELLATION_STATUS_INTEGRATION_WITH_STREAM_SPECIFIC_POLICIES_NOT_IMPLEMENTED",
    "SCOUT_DISTRIBUTED_RESERVATION_PROTOCOL_NOT_IMPLEMENTED",
    "PER_SYMBOL_TA_SCORE_RECIPES_AND_BUY_SELL_REFERENCE_BINDINGS_NOT_SELECTED",
    "HALF_SHARE_TIES_AND_ROUNDED_EQUALITY_POLICY_NOT_SELECTED",
    "LONGER_ALLOCATION_LIMITS_PACING_AND_CONTENTION_POLICY_NOT_SELECTED",
    "REPRICING_CADENCE_AND_INCOMPLETE_ENSEMBLE_EXECUTION_POLICY_NOT_SELECTED",
    "FINAL_INTRADAY_AND_NEXT_OPEN_TARGET_CONTRACTS_NOT_SELECTED",
)
PUBLIC_SOURCES = (
    "ml/intraday/readiness.py", "ml/intraday/runtime.py", "ml/intraday/store.py",
    "ml/intraday/execution_policy.py", "ml/intraday/technical_sizing.py",
    "ml/account_gameplan/authority.py",
)


def _json(path):
    if path.stat().st_size > 1024 * 1024:
        raise ValueError("Oversized binding")
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("Duplicate JSON key")
            result[key] = value
        return result
    value = json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=unique)
    if not isinstance(value, dict):
        raise ValueError("JSON object required")
    return value


def _symbols(value):
    if not isinstance(value, (list, tuple)) or not value or len(value) > 256:
        raise ValueError("Explicit symbol membership required")
    if any(not isinstance(symbol, str) or not SYMBOL.fullmatch(symbol) for symbol in value):
        raise ValueError("Invalid symbol")
    if len(set(value)) != len(value):
        raise ValueError("Duplicate symbol")
    return tuple(value)


def _bindings(datastore, profile_path, watchlist_path):
    result = {"status": "unavailable", "actor": None, "machine": None,
              "symbols": [], "account_binding": "not_inspected", "findings": []}
    if not profile_path.is_file():
        result["findings"].append("LOCAL_PROFILE_MISSING")
        return result
    try:
        profile = _json(profile_path)
        actor, machine = profile.get("actor"), profile.get("machine")
        if actor not in ("Atlas", "Scout") or machine != {"Atlas": "pc-original", "Scout": "pc-new"}[actor]:
            raise ValueError("Invalid PC identity")
        symbols = _symbols(profile.get("symbols"))
        result.update(actor=actor, machine=machine)
    except (OSError, ValueError, TypeError, KeyError):
        result.update(status="invalid", findings=["LOCAL_PROFILE_INVALID"])
        return result
    if not watchlist_path.is_file():
        result["findings"].append("LOCAL_WATCHLIST_MISSING")
        return result
    try:
        watch = _symbols(tuple(line.strip() for line in watchlist_path.read_text(encoding="utf-8").splitlines()
                               if line.strip() and not line.lstrip().startswith("#")))
        if set(watch) != set(symbols):
            raise ValueError("Membership mismatch")
        account_path = datastore / "state/account-gameplan/config.json"
        if account_path.exists():
            result["account_binding"] = "invalid"
            account = _json(account_path)
            members = _symbols(account["participants"][machine])
            if account.get("machine_id") != machine or set(members) != set(symbols):
                raise ValueError("Account membership mismatch")
            result["account_binding"] = "membership_verified"
        else:
            result["account_binding"] = "absent_research_only"
            pinned = profile.get("symbol_profile_path")
            if not isinstance(pinned, str) or Path(pinned).resolve() != watchlist_path.resolve():
                raise ValueError("Missing pinned watchlist")
        result.update(status="verified", symbols=list(symbols))
    except (OSError, ValueError, TypeError, KeyError):
        result.update(status="invalid", findings=["LOCAL_MEMBERSHIP_OR_ACCOUNT_BINDING_INVALID"])
    return result


def _source(value):
    if "source_dataset" not in value and "source_id" not in value:
        return "Unrecorded"
    dataset = value.get("source_dataset")
    if not isinstance(dataset, str) or value.get("source_id") != SOURCE_IDS.get(dataset):
        raise ValueError("Unknown or mismatched source identity")
    return dataset


class SnapshotChanged(OSError):
    """The original files changed while a bounded readiness observation ran."""


class SnapshotCodecUnavailable(ValueError):
    """Stored price precision cannot be checked with the installed codec."""


def _snapshot_files(path):
    """Read database and recovery files without opening original files in SQLite.

    SHM is a reconstructible WAL index, not database content. SQLite may create
    it even through mode=ro. It is therefore reconstructed only in the temporary
    directory, using the actual WAL bytes; immutable=1 is deliberately avoided.
    A rollback journal is retained too, so a hot journal cannot be ignored.
    """
    result = {}
    for suffix in ("", "-wal", "-journal"):
        source = Path(str(path) + suffix)
        try:
            result[suffix] = source.read_bytes()
        except FileNotFoundError:
            if not suffix:
                raise
    return result


@contextmanager
def _read_only_snapshot(path):
    # Two identical reads are required. A changing store remains unavailable;
    # this diagnostic never repairs, checkpoints or locks the original store.
    files = _snapshot_files(path)
    if _snapshot_files(path) != files:
        raise SnapshotChanged("Database changed during read")
    with TemporaryDirectory(prefix="ducketz-readiness-") as directory:
        copied = Path(directory) / "shadow.sqlite3"
        for suffix, content in files.items():
            Path(str(copied) + suffix).write_bytes(content)
        with closing(sqlite3.connect(copied.as_uri()+"?mode=ro", uri=True, timeout=1)) as db:
            db.execute("PRAGMA query_only=ON")
            db.execute("BEGIN")
            try:
                yield db
            finally:
                db.rollback()


def _forecast(row):
    key, pc, symbol, cycle_id, reference_at, issued_at, raw = row
    value = json.loads(raw)
    if not isinstance(value, dict):
        raise ValueError("Invalid forecast")
    original = dict(value)
    forecast_id = original.pop("forecast_id", None)
    if forecast_id != key or not HASH.fullmatch(str(key)) or digest(original) != key:
        raise ValueError("Changed immutable forecast")
    indexed = dict(pc=pc, symbol=symbol, cycle_id=cycle_id, reference_at=reference_at, issued_at=issued_at)
    if any(value.get(field) != item for field, item in indexed.items()):
        raise ValueError("Changed indexed identity")
    if not isinstance(symbol, str) or not SYMBOL.fullmatch(symbol):
        raise ValueError("Invalid symbol")
    reference, issue = instant(reference_at), instant(issued_at)
    recorded = instant(value["recorded_at"])
    through, available = instant(value["data_through"]), instant(value["information_available_at"])
    cutoff = instant(value["training_cutoff"])
    target = instant(value["target_close"])
    if (reference.minute % 15 or reference.second or reference.microsecond
            or through != reference + STEP or instant(value["target_open"]) != reference + 2*STEP
            or target != reference + 3*STEP or value.get("target_version") != TARGET_VERSION
            or not through <= available <= cutoff <= issue or issue < through):
        raise ValueError("Forecast information or target timing invalid")
    mode, basis = value.get("mode"), value.get("availability_basis")
    if mode not in ("shadow", "historical_replay") or basis not in ("observed", "assumed_historical"):
        raise ValueError("Invalid mode or availability")
    if mode == "shadow" and (basis != "observed" or recorded < issue):
        raise ValueError("Shadow forecast must record actual information and issue")
    number(value["reference_open"], positive=True)
    configured, successful = value.get("configured_count"), value.get("successful_count")
    models = value.get("models")
    if (type(configured) is not int or not 1 <= configured <= 128 or type(successful) is not int
            or not 1 <= successful <= configured or not isinstance(models, list) or len(models) != configured):
        raise ValueError("Invalid model counts")
    names, probabilities = set(), []
    for model in models:
        name = model.get("name")
        if not isinstance(name, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", name) or name in names:
            raise ValueError("Invalid model identity")
        names.add(name)
        if model.get("status") == "success":
            if not isinstance(model.get("version"), str) or not HASH.fullmatch(model["version"]):
                raise ValueError("Successful model version required")
            probability = number(model.get("probability_not_down"))
            if probability > 1:
                raise ValueError("Invalid probability")
            probabilities.append(probability)
        elif model.get("status") != "failed" or model.get("probability_not_down") is not None:
            raise ValueError("Invalid failed model output")
    if len(probabilities) != successful:
        raise ValueError("Successful count mismatch")
    mean = sum(probabilities) / len(probabilities)
    full = successful == configured
    if (abs(number(value["probability_not_down"]) - mean) > Decimal("1e-12")
            or value.get("ensemble_status") != ("full" if full else "incomplete")
            or value.get("classification") != ("not-down" if mean >= Decimal(".5") else "down")):
        raise ValueError("Invalid consensus")
    if "issue_after_target" in value and value["issue_after_target"] is not (issue >= target):
        raise ValueError("Late marker differs from the fixed target")
    return {"symbol": symbol, "source_dataset": _source(value), "mode": mode, "availability_basis": basis,
            "forecast_id": key, "reference_at": reference.isoformat(), "data_through": through.isoformat(),
            "information_available_at": available.isoformat(), "training_cutoff": cutoff.isoformat(),
            "issued_at": issue.isoformat(), "recorded_at": recorded.isoformat(), "target_close": target.isoformat(),
            "configured_count": configured, "successful_count": successful, "ensemble_status": "full" if full else "incomplete",
            "model_versions": {model["name"]: model["version"] for model in models if model.get("status") == "success"},
            "issue_after_target": issue >= target}


def _reference_candle_matches(stored, authoritative):
    if number(stored, positive=True) == number(authoritative, positive=True):
        return True
    # Runtime freezes candle frames through pandas' documented JSON codec at
    # ten decimal places. Inference retains the original float. Reproduce that
    # representation exactly, keeping inference authoritative, with no tolerance.
    try:
        from pandas import DataFrame
    except ImportError as exc:
        raise SnapshotCodecUnavailable("Stored price codec unavailable") from exc
    serialized = json.loads(DataFrame({"open": [float(authoritative)]}).to_json(orient="records", double_precision=10))
    return number(stored, positive=True) == number(serialized[0]["open"], positive=True)


def _bound_snapshot(db, forecast, row):
    """Verify stored input bindings and clocks, not provider or fit availability."""
    original = json.loads(row[-1])
    key = original.get("snapshot_hash")
    if not isinstance(key, str) or not HASH.fullmatch(key):
        raise ValueError("Missing bound snapshot")
    saved = db.execute("SELECT s.id,s.payload FROM snapshots s JOIN symbol_inputs i ON i.snapshot_id=s.id "
                       "WHERE i.cycle_id=? AND i.symbol=?", (row[3], row[2])).fetchall()
    if len(saved) != 1 or saved[0][0] != key:
        raise ValueError("Snapshot binding differs from the forecast")
    snapshot = json.loads(saved[0][1])
    if not isinstance(snapshot, dict) or digest(snapshot) != key:
        raise ValueError("Snapshot evidence changed")
    cutoff = instant(forecast["training_cutoff"])
    reference = instant(forecast["reference_at"])
    inference = snapshot["inference"]
    if instant(snapshot["cutoff"]) != cutoff or inference.get("symbol") != forecast["symbol"]:
        raise ValueError("Snapshot scope or cutoff changed")
    for field in ("reference_at", "data_through", "information_available_at", "target_open", "target_close"):
        if instant(inference[field]) != instant(original[field]):
            raise ValueError("Snapshot inference timing changed")
    if (number(inference["reference_open"], positive=True) != number(original["reference_open"], positive=True)
            or inference.get("target_version") != TARGET_VERSION
            or inference.get("availability_kind") != ("actual" if forecast["availability_basis"] == "observed" else "assumed_historical_close")):
        raise ValueError("Snapshot reference or availability changed")
    if forecast["source_dataset"] != "Unrecorded" and (
            inference.get("source_dataset") != original["source_dataset"] or inference.get("source") != original["source_id"]):
        raise ValueError("Snapshot source changed")
    labels = snapshot["label_available_at"]
    targets = snapshot["target_close_at"]
    references = snapshot["reference_at"]
    if not isinstance(labels, list) or not labels or not len(labels) == len(targets) == len(references):
        raise ValueError("Training clock evidence required")
    for label, target, training_reference in zip(labels, targets, references):
        if not instant(training_reference)+3*STEP == instant(target) <= instant(label) <= cutoff:
            raise ValueError("Training label immature or target changed")
    candles = snapshot["candles"]
    if not isinstance(candles, list) or not candles:
        raise ValueError("Recorded input candles required")
    found_reference = False
    opened = set()
    for candle in candles:
        start, available = instant(candle["opened_at"]), instant(candle["available_at"])
        if (candle["symbol"] != forecast["symbol"] or start in opened or start > reference
                or start.minute % 15 or start.second or start.microsecond or not start+STEP <= available <= cutoff):
            raise ValueError("Input candle scope or availability invalid")
        opened.add(start)
        if forecast["source_dataset"] != "Unrecorded" and (
                candle.get("source_dataset") != original["source_dataset"] or candle.get("source_id") != original["source_id"]):
            raise ValueError("Input candle source changed")
        if start == reference:
            if (candle.get("availability_kind") != inference["availability_kind"]
                    or available > instant(inference["information_available_at"])):
                raise ValueError("Reference availability differs from inference evidence")
            found_reference = _reference_candle_matches(candle["open"], inference["reference_open"])
    if not found_reference:
        raise ValueError("Reference open absent from recorded inputs")
    return {"status": "verified_recorded_inputs", "snapshot_hash": key, "training_rows": len(labels),
            "recorded_candle_count": len(candles), "latest_label_available_at": max(map(instant, labels)).isoformat(),
            "feature_calculations_reconstructed": False, "fitted_artifacts_checked": False}


def _forecast_store(datastore, binding, now):
    result = {"status": "missing", "findings": [], "forecast_count": 0, "invalid_forecast_count": 0,
              "future_record_count": 0, "outside_membership_count": 0, "latest_forecasts": [],
              "missing_shadow_symbols": list(binding["symbols"])}
    path = datastore / "intraday/shadow.sqlite3"
    if not path.is_file():
        result["findings"].append("SHADOW_STORE_MISSING")
        return result
    if binding["status"] != "verified":
        result.update(status="not_inspected", findings=["VERIFIED_LOCAL_BINDING_REQUIRED"])
        return result
    latest = {}
    try:
        with _read_only_snapshot(path) as db:
            for row in db.execute("SELECT id,pc,symbol,cycle_id,reference_at,issued_at,payload FROM forecasts WHERE pc=? ORDER BY issued_at,id", (binding["actor"],)):
                result["forecast_count"] += 1
                try:
                    value = _forecast(row)
                except (ValueError, TypeError, KeyError, AttributeError):
                    result["invalid_forecast_count"] += 1
                    continue
                if value["symbol"] not in binding["symbols"]:
                    result["outside_membership_count"] += 1
                    continue
                if instant(value["issued_at"]) > now or instant(value["recorded_at"]) > now:
                    result["future_record_count"] += 1
                    continue
                key = (value["symbol"], value["source_dataset"], value["mode"])
                previous = latest.get(key)
                if previous is None or (instant(value["issued_at"]), instant(value["recorded_at"]), value["forecast_id"]) > (
                        instant(previous[0]["issued_at"]), instant(previous[0]["recorded_at"]), previous[0]["forecast_id"]):
                    latest[key] = (value, row)
            for value, row in latest.values():
                try:
                    value["bound_data"] = _bound_snapshot(db, value, row)
                except SnapshotCodecUnavailable:
                    value["bound_data"] = {"status": "unavailable_serialization_verification", "findings": ["BOUND_SNAPSHOT_SERIALIZATION_VERIFIER_UNAVAILABLE"]}
                except (ValueError, TypeError, KeyError, AttributeError):
                    value["bound_data"] = {"status": "invalid_recorded_inputs", "findings": ["BOUND_SNAPSHOT_OR_INFORMATION_CLOCK_INVALID"]}
        result["read_method"] = "stable_main_and_wal_copy_with_read_only_sqlite_transaction"
        result["invalid_bound_data_count"] = sum(value["bound_data"]["status"] != "verified_recorded_inputs" for value, _ in latest.values())
        result["status"] = "invalid_evidence" if result["invalid_forecast_count"] or result["invalid_bound_data_count"] else "read"
        if result["invalid_forecast_count"]:
            result["findings"].append("IMMUTABLE_FORECAST_OR_CONTRACT_INVALID")
        if result["outside_membership_count"]:
            result["findings"].append("SAVED_FORECAST_OUTSIDE_LOCAL_MEMBERSHIP")
        if result["invalid_bound_data_count"]:
            result["findings"].append("BOUND_SNAPSHOT_OR_INFORMATION_CLOCK_INVALID")
    except SnapshotChanged:
        result.update(status="unavailable", findings=["SHADOW_STORE_CHANGED_DURING_READ"])
        return result
    except (sqlite3.Error, OSError):
        result.update(status="unreadable", findings=["SHADOW_STORE_UNREADABLE"])
        return result
    expected = now.replace(minute=(now.minute//15)*15, second=0, microsecond=0)-STEP
    shadows = set()
    for key, (value, _) in sorted(latest.items()):
        reference = instant(value["reference_at"])
        value["issue_age_seconds"] = (now-instant(value["issued_at"])).total_seconds()
        value["expected_completed_reference_at"] = expected.isoformat()
        if value["mode"] == "historical_replay":
            value["data_state"] = "historical_replay"
        else:
            shadows.add(value["symbol"])
            value["data_state"] = "current_reference" if reference == expected else "stale_reference"
        value["target_state"] = "late_issue" if value["issue_after_target"] else "target_elapsed" if now >= instant(value["target_close"]) else "target_pending"
        value["forecast_data_evidence"] = "verified_immutable_forecast_and_recorded_information_clock"
        # This is a descriptive research state, never an execution gate.
        value["shadow_full_current"] = (value["mode"] == "shadow" and value["data_state"] == "current_reference"
                                         and value["ensemble_status"] == "full" and value["target_state"] == "target_pending"
                                         and value["bound_data"]["status"] == "verified_recorded_inputs")
        result["latest_forecasts"].append(value)
    result["missing_shadow_symbols"] = sorted(set(binding["symbols"])-shadows)
    result["latest_state_counts"] = {
        key: sum(value.get(key) is True for value in result["latest_forecasts"])
        for key in ("shadow_full_current", "issue_after_target")}
    result["latest_mode_counts"] = {mode: sum(row["mode"] == mode for row in result["latest_forecasts"])
                                    for mode in ("shadow", "historical_replay")}
    return result


def _source_capabilities(repository):
    evidence = {}
    for relative in PUBLIC_SOURCES:
        path = repository / relative
        evidence[relative] = {"present": path.is_file()}
        if path.is_file():
            try:
                evidence[relative]["sha256"] = hashlib.sha256(path.read_bytes()).hexdigest()
            except OSError:
                evidence[relative]["readable"] = False
    horizons = []
    authority_path = repository / "ml/account_gameplan/authority.py"
    try:
        tree = ast.parse(authority_path.read_text(encoding="utf-8"))
        for node in tree.body:
            if isinstance(node, ast.Assign) and any(isinstance(target, ast.Name) and target.id == "HORIZONS" for target in node.targets):
                value = node.value
                if isinstance(value, ast.Call) and isinstance(value.func, ast.Name) and value.func.id == "frozenset" and len(value.args) == 1:
                    value = value.args[0]
                admitted = ast.literal_eval(value)
                if isinstance(admitted, (list, tuple, set, frozenset)) and all(item in ("15m", "1h", "4h", "1d", "1w") for item in admitted):
                    horizons = sorted(admitted)
    except (OSError, SyntaxError, ValueError, TypeError):
        pass
    return {"declaration_basis": "current_readiness_implementation_capabilities_not_runtime_activation",
            "research_forecasting_source_present": all(evidence[path]["present"] for path in ("ml/intraday/runtime.py", "ml/intraday/store.py")),
            "offline_cutoff_replacement_helper_present": evidence["ml/intraday/execution_policy.py"]["present"],
            "existing_shared_authority_horizons": horizons, "existing_shared_authority_supports_15m": "15m" in horizons,
            "live_execution_implemented": False, "manual_live_start_ready": False,
            "missing_integration": [gap for gap in LIVE_GAPS if not gap.startswith("SCOUT_")],
            "future_scout_integration": [gap for gap in LIVE_GAPS if gap.startswith("SCOUT_")],
            "public_source_evidence": evidence}


def inspect_readiness(*, repository, datastore, profile, watchlist, as_of=None):
    """Return a bounded local report. Caller-selected output never activates trading."""
    try:
        now = instant(as_of or datetime.now(timezone.utc))
    except ContractError:
        return {"schema_version": VERSION, "status": "INPUT_INVALID", "findings": ["TIMEZONE_AWARE_AS_OF_REQUIRED"],
                "live_execution_implemented": False, "manual_live_start_ready": False, "orders_placed": 0}
    repository, datastore, profile, watchlist = map(Path, (repository, datastore, profile, watchlist))
    binding = _bindings(datastore, profile, watchlist)
    saved = _forecast_store(datastore, binding, now)
    capabilities = _source_capabilities(repository)
    gate = submission_gate(as_of=now, eligible_session=now.astimezone(PACIFIC).date())
    return {"schema_version": VERSION, "observed_at": now.isoformat(), "status": "MANUAL_LIVE_START_NOT_IMPLEMENTED",
            "read_only": True, "live_execution_implemented": False, "manual_live_start_ready": False,
            "local_binding": binding, "saved_forecasting": saved, "capabilities": capabilities,
            "submission_time": {**asdict(gate), "eligible_session_not_verified": True},
            "provider_availability_verified": False, "broker_reconciliation_performed": False,
            "execution_authorized": False, "activation_changed": False, "orders_placed": 0,
            "scope": "Saved research evidence and public source capabilities only; no fitted-model performance or live account readiness claim"}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repository", type=Path, default=Path(__file__).resolve().parents[2])
    parser.add_argument("--datastore", type=Path, required=True)
    parser.add_argument("--profile", type=Path, required=True)
    parser.add_argument("--watchlist", type=Path, required=True)
    parser.add_argument("--as-of", help="Optional timezone-aware evidence cutoff; never a live issue time")
    args = parser.parse_args(argv)
    result = inspect_readiness(repository=args.repository, datastore=args.datastore, profile=args.profile,
                               watchlist=args.watchlist, as_of=args.as_of)
    print(json.dumps(result, sort_keys=True, allow_nan=False))
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
