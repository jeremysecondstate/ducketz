"""Offline composition of two frozen Gameplans against one account snapshot.

Package digests prove byte consistency, not producer authenticity. The caller
must independently verify the source receipts and supply their selected package
digests. These informational artifacts neither reserve capital nor place orders.
The native handoff adapter owns the export allowlist and privacy review.
"""
from __future__ import annotations

from collections import Counter
from copy import deepcopy
from datetime import date
from decimal import Decimal, ROUND_FLOOR, ROUND_HALF_UP, ROUND_CEILING
import hashlib
import json
import math
import os
from pathlib import Path
import re
import stat
from typing import Mapping, Sequence

import pandas as pd

from ml.gameplan_cash_ledger import project_direction_trades, SIGNAL_DRIVEN_HOLDING_POLICY
from ml.stock_trader.contracts import StockTraderPolicy
from ml.stock_trader.cross_horizon_fallback import validate_fallback_policy


PACKAGE_SCHEMA = "joint-capital-owner-package-v1"
PLAN_SCHEMA = "joint-capital-plan-v1"
RANKING_POLICY = "existing_global_probability_order_v1"
MAXIMUM_JSON_BYTES = 8 * 1024 * 1024
_HASH = re.compile(r"[0-9a-f]{64}\Z")
_OWNER = re.compile(r"[a-z][a-z0-9_-]{0,63}\Z")
_SYMBOL = re.compile(r"[A-Z][A-Z0-9.-]{0,14}\Z")
_SOURCE_HASHES = {"receipt_sha256", "manifest_sha256", "forecasts_sha256", "price_path_sha256"}
_FORECAST_REQUIRED = {"id", "symbol", "action_date", "model_group", "execution_eligible",
    "target_window_start", "target_window_end", "calibrated_probability", "direction", "model_status"}


def _json_bytes(value: object) -> bytes:
    try:
        return json.dumps(value, sort_keys=True, separators=(",", ":"),
                          ensure_ascii=True, allow_nan=False).encode("ascii")
    except (TypeError, ValueError) as exc:
        raise ValueError("Joint planning inputs must be finite ordinary JSON values") from exc


def content_sha256(value: object) -> str:
    """Digest canonical JSON, distinct from the native source file digest."""
    return hashlib.sha256(_json_bytes(value)).hexdigest()


def _hash(value: object, label: str) -> str:
    if not isinstance(value, str) or not _HASH.fullmatch(value):
        raise ValueError(f"Invalid {label} SHA-256")
    return value


def _time(value: object, label: str) -> pd.Timestamp:
    if not isinstance(value, str):
        raise ValueError(f"{label} requires an explicit timestamp string")
    try:
        result = pd.Timestamp(value)
    except (ValueError, TypeError) as exc:
        raise ValueError(f"Invalid {label} timestamp") from exc
    if pd.isna(result) or result.tzinfo is None:
        raise ValueError(f"{label} requires a timezone-aware timestamp")
    return result.tz_convert("UTC")


def _date(value: object) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        raise ValueError("Invalid action date")
    date.fromisoformat(value)
    return value


def _symbols(values: object) -> tuple[str, ...]:
    if not isinstance(values, (list, tuple)) or not values or len(values) > 100:
        raise ValueError("A bounded explicit frozen universe is required")
    if any(not isinstance(s, str) or not _SYMBOL.fullmatch(s) for s in values):
        raise ValueError("Invalid frozen symbol")
    if len(set(values)) != len(values):
        raise ValueError("Duplicate frozen symbol")
    return tuple(values)


def _number(value: object, label: str, *, positive: bool = False) -> float:
    if type(value) not in (int, float) or not math.isfinite(value) or value < 0 or (positive and value == 0):
        raise ValueError(f"Invalid nonnegative numeric {label}")
    return float(value)


def _package_content(package: Mapping) -> dict:
    return {key: value for key, value in package.items() if key != "package_sha256"}


def _forecast_json(row: Mapping) -> dict:
    result = {}
    for key, value in row.items():
        if isinstance(value, pd.Timestamp):
            value = value.isoformat()
        elif hasattr(value, "item") and not isinstance(value, (dict, list, tuple)):
            value = value.item()
        if key not in _FORECAST_REQUIRED and not isinstance(value, (dict, list, tuple)):
            try:
                if pd.isna(value):
                    value = None
            except (TypeError, ValueError):
                pass
        result[key] = value
    return result


def _validate_package(package: Mapping) -> None:
    if not isinstance(package, Mapping) or package.get("schema_version") != PACKAGE_SCHEMA:
        raise ValueError("Unsupported owner package schema")
    if package.get("holding_policy") != SIGNAL_DRIVEN_HOLDING_POLICY:
        raise ValueError("Joint composition requires the existing signal-driven holding policy")
    if not isinstance(package.get("owner_id"), str) or not _OWNER.fullmatch(package["owner_id"]):
        raise ValueError("Invalid package owner")
    run = package.get("run_id")
    if not isinstance(run, str) or not re.fullmatch(r"[A-Za-z0-9_.-]{1,160}", run):
        raise ValueError("Invalid source run identity")
    revision = package.get("source_revision")
    if revision is None:
        if package.get("source_revision_status") != "UNRECORDED":
            raise ValueError("Unknown source revisions must be explicitly unrecorded")
    elif (not isinstance(revision, str) or not re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", revision)
          or package.get("source_revision_status") != "RECORDED"):
        raise ValueError("Invalid frozen source revision")
    day = _date(package.get("action_date"))
    validate_fallback_policy(package.get("cross_horizon_fallback_policy"), day)
    symbols = _symbols(package.get("frozen_symbols"))
    created = _time(package.get("created_at"), "package created_at")
    hashes = package.get("source_hashes")
    if not isinstance(hashes, dict) or set(hashes) != _SOURCE_HASHES:
        raise ValueError("Required native source hashes are missing")
    for name, value in hashes.items():
        _hash(value, name)
    reference = package.get("source_reference")
    if reference is not None:
        if (not isinstance(reference, dict)
                or set(reference) != {"kind", "source_revision", "receipt_sha256", "reference_sha256"}
                or reference["kind"] not in {"SAVED_ARTIFACT_PROVENANCE", "CALLER_PINNED_SOURCE_REFERENCE"}
                or revision is None or reference["source_revision"] != revision
                or reference["receipt_sha256"] != hashes["receipt_sha256"]):
            raise ValueError("Source provenance reference differs from its frozen source")
        _hash(reference["reference_sha256"], "source reference")
    rows = package.get("forecasts")
    if not isinstance(rows, list) or len(rows) != 24 * len(symbols):
        raise ValueError("Every frozen symbol requires its 24 original forecasts")
    seen, windows, counts = set(), set(), Counter()
    for row in rows:
        if not isinstance(row, dict) or any(key in row for key in ("owner_id", "owner_run_id", "original_forecast_id")):
            raise ValueError("Forecast contains invalid or previously composed ownership fields")
        identifier = row.get("id")
        if not isinstance(identifier, str) or not identifier or len(identifier) > 512 or identifier in seen:
            raise ValueError("Duplicate or invalid forecast identity within owner")
        seen.add(identifier)
        symbol = row.get("symbol")
        if symbol not in symbols or row.get("action_date") != day:
            raise ValueError("Forecast differs from its frozen date or universe")
        counts[symbol] += 1
        horizon = row.get("model_group")
        if horizon not in {"1h", "4h", "1d", "1w"} or type(row.get("execution_eligible")) is not bool:
            raise ValueError("Invalid forecast horizon or eligibility")
        start = _time(row.get("target_window_start"), "forecast start")
        end = _time(row.get("target_window_end"), "forecast end")
        if end <= start:
            raise ValueError("Invalid forecast window")
        window = (symbol, horizon, start)
        if window in windows:
            raise ValueError("Duplicate forecast window claim")
        windows.add(window)
        if row["execution_eligible"] and start.tz_convert("America/Los_Angeles").date().isoformat() != day:
            raise ValueError("Execution forecast starts outside its frozen action session")
        probability = _number(row.get("calibrated_probability"), "forecast probability")
        if probability > 1 or not isinstance(row.get("direction"), str) or not isinstance(row.get("model_status"), str):
            raise ValueError("Invalid saved forecast probability or direction metadata")
    if any(counts[s] != 24 for s in symbols):
        raise ValueError("Forecast cardinality differs from the frozen universe")
    from ml.stock_trader.independent_signals import _validated_independent_forecasts
    normalized = _validated_independent_forecasts(pd.DataFrame(rows), action_date=day, symbols=symbols)
    from ml.stock_trader.gameplan_execution import _validated_instructions
    _validated_instructions(normalized)
    prior_session = normalized.loc[normalized["route"].eq("1h@gap"), "target_window_start"].iloc[0].tz_convert("America/Los_Angeles").date().isoformat()
    for row in rows:
        for name in ("decision_timestamp", "information_available_at", "frozen_at"):
            stamp = _time(row.get(name), name)
            if stamp > created:
                raise ValueError("Frozen forecast evidence postdates its package")
    prices = package.get("price_path")
    if not isinstance(prices, dict) or not isinstance(prices.get("points"), dict):
        raise ValueError("A verified planning price path is required")
    from ml.gameplan_price_bands import (PLANNING_PRICE_PATH_CONTRACT,
        COMPLETED_PLANNING_PRICE_PATH_CONTRACT, SPARSE_PLANNING_PRICE_PATH_CONTRACT)
    from ml.stock_target_prices import stock_price_dataset
    if prices.get("contract_version") not in {PLANNING_PRICE_PATH_CONTRACT,
            COMPLETED_PLANNING_PRICE_PATH_CONTRACT, SPARSE_PLANNING_PRICE_PATH_CONTRACT}:
        raise ValueError("Unsupported native planning price path contract")
    source_contract = str(normalized["target_price_source_contract"].iloc[0])
    if (prices.get("price_source_contract") != source_contract
            or prices.get("price_dataset") != stock_price_dataset(source_contract)):
        raise ValueError("Price path differs from the frozen forecast price source")
    if (type(prices.get("minimum_samples")) is not int or type(prices.get("lookback_sessions")) is not int
            or not 2 <= prices["minimum_samples"] <= prices["lookback_sessions"]):
        raise ValueError("Invalid planning price sample policy")
    width = _number(prices.get("working_half_width_bps"), "working range width", positive=True)
    if width >= 10000:
        raise ValueError("Planning working range width must be below 10000 basis points")
    if prices["contract_version"] != PLANNING_PRICE_PATH_CONTRACT and not isinstance(prices.get("reference_completion"), dict):
        raise ValueError("Completed-reference price paths require their bound completion evidence")
    price_observed = _time(prices.get("observed_at"), "price path observed_at")
    if price_observed > created:
        raise ValueError("Price evidence postdates its owner package")
    expected_points = {f"{s}|{day}|{h:02d}:00" for s in symbols for h in range(4, 18)}
    if set(prices["points"]) != expected_points:
        raise ValueError("Price path must exactly cover its frozen symbols and session")
    for key, point in prices["points"].items():
        if not isinstance(point, dict):
            raise ValueError("Invalid planning price point")
        expected_symbol, expected_day, expected_clock = key.split("|")
        if (point.get("symbol") != expected_symbol or point.get("action_date") != expected_day
                or point.get("clock_local") != expected_clock
                or _time(point.get("timestamp"), "price clock") != pd.Timestamp(f"{day} {expected_clock}", tz="America/Los_Angeles").tz_convert("UTC")):
            raise ValueError("Planning point differs from its frozen identity")
        samples = point.get("sample_count")
        if type(samples) is not int or not 0 <= samples <= prices["lookback_sessions"]:
            raise ValueError("Invalid observed planning sample count")
        status = point.get("status")
        if status not in {"AVAILABLE", "UNAVAILABLE_REFERENCE_PRICE", "UNAVAILABLE_MINIMUM_SAMPLES"}:
            raise ValueError("Unsupported planning price status")
        if status != "AVAILABLE" and any(point.get(name) is not None for name in ("planned_price_low", "planned_price_mid", "planned_price_high")):
            raise ValueError("Unavailable price points cannot carry planning prices")
        if status == "UNAVAILABLE_REFERENCE_PRICE" and point.get("reference_price") is not None:
            raise ValueError("Unavailable reference status disagrees with its anchor")
        if status == "UNAVAILABLE_MINIMUM_SAMPLES" and samples >= prices["minimum_samples"]:
            raise ValueError("Unavailable sample status disagrees with its sample count")
        if status != "UNAVAILABLE_REFERENCE_PRICE":
            _number(point.get("reference_price"), "reference price", positive=True)
            reference_time = _time(point.get("reference_observed_at"), "reference observed_at")
            reference_day = _date(point.get("reference_session"))
            if (reference_time > price_observed or reference_day != prior_session
                    or reference_time.tz_convert("America/Los_Angeles").date().isoformat() != reference_day):
                raise ValueError("Planning anchor time differs from its prior-session evidence")
        if status == "AVAILABLE":
            if samples < prices["minimum_samples"]:
                raise ValueError("Available planning points require the minimum observed samples")
            _number(point.get("ratio_median"), "historical median ratio", positive=True)
            for name in ("planned_price_low", "planned_price_mid", "planned_price_high"):
                _number(point.get(name), name, positive=True)
            center = Decimal(str(point["reference_price"])) * Decimal(str(point["ratio_median"]))
            allowance = Decimal(str(prices["working_half_width_bps"])) / Decimal(10000)
            expected_prices = ((center * (1 - allowance)).quantize(Decimal(".01"), rounding=ROUND_FLOOR),
                center.quantize(Decimal(".01"), rounding=ROUND_HALF_UP),
                (center * (1 + allowance)).quantize(Decimal(".01"), rounding=ROUND_CEILING))
            if any(Decimal(str(point.get(name))) != expected for name, expected in zip(
                    ("planned_price_low", "planned_price_mid", "planned_price_high"), expected_prices)):
                raise ValueError("Planning prices disagree with their observed anchor, median and fill allowance")
        for name in ("planned_price_low", "planned_price_mid", "planned_price_high"):
            if point.get(name) is not None:
                _number(point[name], name, positive=True)
    expected_content = {"forecasts": content_sha256(rows), "price_path": content_sha256(prices)}
    if package.get("content_sha256") != expected_content:
        raise ValueError("Owner package content hashes disagree")
    if _hash(package.get("package_sha256"), "package") != content_sha256(_package_content(package)):
        raise ValueError("Owner package digest disagrees with frozen contents")


def build_owner_package(*, owner_id: str, run_id: str, source_revision: str | None,
                        action_date: str, frozen_symbols: Sequence[str], created_at: str,
                        source_hashes: Mapping, forecasts: Sequence[Mapping], price_path: Mapping,
                        cross_horizon_fallback_policy: Mapping | None = None,
                        source_reference: Mapping | None = None) -> dict:
    """Seal already verified native inputs; this does not authenticate a producer."""
    package = deepcopy({"schema_version": PACKAGE_SCHEMA, "owner_id": owner_id,
        "run_id": run_id, "source_revision": source_revision, "action_date": action_date,
        "source_revision_status": "UNRECORDED" if source_revision is None else "RECORDED",
        "source_reference": source_reference,
        "holding_policy": SIGNAL_DRIVEN_HOLDING_POLICY, "cross_horizon_fallback_policy": cross_horizon_fallback_policy,
        "frozen_symbols": list(frozen_symbols), "created_at": created_at,
        "source_hashes": dict(source_hashes), "forecasts": [_forecast_json(row) for row in forecasts], "price_path": dict(price_path)})
    package["content_sha256"] = {"forecasts": content_sha256(package["forecasts"]),
                                 "price_path": content_sha256(package["price_path"])}
    package["package_sha256"] = content_sha256(package)
    _validate_package(package)
    return package


def _validate_snapshot(snapshot: Mapping, symbols: set[str], scope: str, now: pd.Timestamp, age: int, *, fallback: bool) -> None:
    _json_bytes(snapshot)
    if snapshot.get("account_scope_sha256") != scope:
        raise ValueError("Authoritative snapshot belongs to a different account scope")
    elapsed = (now - _time(snapshot.get("observed_at"), "snapshot observed_at")).total_seconds()
    if not 0 <= elapsed <= age:
        raise ValueError("Authoritative account snapshot is stale or future-dated")
    for name in ("account_equity", "available_cash", "reserved_cash", "gross_exposure"):
        _number(snapshot.get(name), name, positive=name == "account_equity")
    for name in ("held_shares", "symbol_exposure", "stock_market_value_by_symbol", "other_symbol_exposure"):
        values = snapshot.get(name)
        if not isinstance(values, dict) or set(values) != symbols:
            raise ValueError(f"Authoritative snapshot {name} must cover the exact joint universe")
        for value in values.values():
            _number(value, name)
    for name in ("pending_buy_shares", "pending_sell_shares"):
        values = snapshot.get(name)
        if not isinstance(values, dict):
            raise ValueError(f"Snapshot is missing {name}")
        for value in values.values():
            _number(value, name)
    for symbol, quantity in snapshot["pending_buy_shares"].items():
        if quantity:
            quote = snapshot.get("quotes", {}).get(symbol)
            if not isinstance(quote, dict):
                raise ValueError("Pending buys require their observed quote")
            _number(quote.get("ask"), "pending buy ask", positive=True)
    ownership = snapshot.get("ownership", {})
    allocations = ownership.get("active_allocations")
    if not isinstance(allocations, list) or not isinstance(ownership.get("blocked_symbols"), list):
        raise ValueError("Explicit reconciled ownership evidence is required")
    if not set(ownership["blocked_symbols"]).issubset(symbols):
        raise ValueError("Ownership block is outside the joint universe")
    seen, allocation_ids = set(), set()
    for allocation in allocations:
        if not isinstance(allocation, dict) or allocation.get("symbol") not in symbols:
            raise ValueError("Ownership allocation is outside the joint universe")
        key = (allocation["symbol"], allocation.get("horizon"))
        if key in seen:
            raise ValueError("Duplicate joint ownership allocation")
        seen.add(key)
        if fallback:
            allocation_id = _hash(allocation.get("allocation_id_sha256"), "allocation identity")
            if allocation_id in allocation_ids:
                raise ValueError("Duplicate source allocation identity")
            allocation_ids.add(allocation_id)
        for name in ("owned_shares", "reserved_sell_shares", "reserved_buy_shares"):
            _number(allocation.get(name, 0), name)


def compose_joint_plan(packages: Sequence[Mapping], snapshot: Mapping, *, action_date: str,
                       expected_universes: Mapping[str, Sequence[str]], expected_package_sha256: Mapping[str, str],
                       account_scope_sha256: str, as_of: str, executor_owner: str,
                       policy: StockTraderPolicy | None = None,
                       maximum_snapshot_age_seconds: int = 900,
                       maximum_package_age_seconds: int = 604800) -> dict:
    """Compose exactly two selected sources with one globally applied cash policy.

    The supplied trusted package digests must come from the caller's independent
    source verification. No source or broker state is discovered by this function.
    """
    day, scope, now = _date(action_date), _hash(account_scope_sha256, "account scope"), _time(as_of, "as_of")
    if type(maximum_snapshot_age_seconds) is not int or not 1 <= maximum_snapshot_age_seconds <= 3600:
        raise ValueError("Snapshot freshness bound must be within one hour")
    if type(maximum_package_age_seconds) is not int or not 1 <= maximum_package_age_seconds <= 604800:
        raise ValueError("Package freshness bound must be within seven days")
    if not isinstance(packages, (list, tuple)) or len(packages) != 2 or len(expected_universes) != 2:
        raise ValueError("Both expected owner packages are required")
    owners = set(expected_universes)
    if executor_owner not in owners:
        raise ValueError("One expected owner must be the explicitly selected sole executor")
    if set(expected_package_sha256) != owners:
        raise ValueError("Both trusted owner package bindings are required")
    selected = {}
    all_symbols, all_rows, points = set(), [], {}
    price_metadata = []
    for package in packages:
        _validate_package(package)
        owner = package["owner_id"]
        if owner not in owners or owner in selected:
            raise ValueError("Missing, unexpected, or duplicate owner package")
        if package["package_sha256"] != _hash(expected_package_sha256[owner], "trusted package"):
            raise ValueError("Package differs from its independently selected source binding")
        if package["action_date"] != day or set(package["frozen_symbols"]) != set(_symbols(expected_universes[owner])):
            raise ValueError("Package action date or frozen universe differs from the selected source")
        elapsed = (now - _time(package["created_at"], "package created_at")).total_seconds()
        if not 0 <= elapsed <= maximum_package_age_seconds:
            raise ValueError("Owner package is stale or future-dated")
        price_elapsed = (now - _time(package["price_path"]["observed_at"], "price observed_at")).total_seconds()
        if not 0 <= price_elapsed <= maximum_package_age_seconds:
            raise ValueError("Planning price evidence is stale or future-dated")
        symbols = set(package["frozen_symbols"])
        if all_symbols & symbols:
            raise ValueError("Overlapping owner symbol claims")
        all_symbols |= symbols
        selected[owner] = package
        price_metadata.append({k: v for k, v in package["price_path"].items() if k != "points"})
        points.update(deepcopy(package["price_path"]["points"]))
    # Price observations may differ; the interpretation and planning policy may not.
    comparable = [{k: v for k, v in item.items() if k not in {"observed_at", "reference_completion"}} for item in price_metadata]
    if comparable[0] != comparable[1]:
        raise ValueError("Owner planning price policies disagree")
    fallback_policies = [selected[owner].get("cross_horizon_fallback_policy") for owner in sorted(selected)]
    if fallback_policies[0] != fallback_policies[1]:
        raise ValueError("Owner cross-horizon fallback policies disagree")
    fallback_policy = fallback_policies[0]
    for owner in sorted(selected):
        package = selected[owner]
        for row in sorted(package["forecasts"], key=lambda r: r["id"]):
            identifier = "joint:" + content_sha256([owner, package["run_id"], row["id"]])
            all_rows.append({**deepcopy(row), "id": identifier, "original_forecast_id": row["id"],
                             "owner_id": owner, "owner_run_id": package["run_id"]})
    _validate_snapshot(snapshot, all_symbols, scope, now, maximum_snapshot_age_seconds, fallback=fallback_policy is not None)
    effective_policy = policy or StockTraderPolicy()
    effective_policy.validate()
    rows, ledger = project_direction_trades(pd.DataFrame(all_rows), deepcopy(snapshot),
        {**deepcopy(price_metadata[0]), "points": points}, policy=effective_policy, signal_driven=True,
        cross_horizon_fallback_policy=fallback_policy)
    # DataFrame missing optional fields become NaN; preserve exact source fields
    # and add only the projection fields produced by the ledger.
    projected = {str(row["id"]): row for row in rows.to_dict("records")}
    projection_fields = {"direction_based_trade_quantity", "direction_based_action", "direction_based_reason",
        "projected_cash_after_low", "projected_cash_after_base", "projected_cash_after_high",
        "projected_shares_after", "projected_available_shares_after"}
    projection_fields |= {name for name in rows.columns if name.startswith("fallback_")}
    outputs = [{**row, **{k: v for k, v in projected[row["id"]].items() if k in projection_fields}}
               for row in all_rows]
    for row in outputs:
        for key in projection_fields:
            if pd.isna(row.get(key)):
                row[key] = None
    event_owners = {row["id"]: row["owner_id"] for row in outputs}
    for event in ledger["events"]:
        event["owner_id"] = event_owners[event["forecast_id"]]
        event["executor_owner"] = executor_owner
        event["fill_basis"] = "CONDITIONAL_PLANNING_FILL"
    bindings = {owner: {key: deepcopy(selected[owner][key]) for key in (
        "run_id", "source_revision", "source_revision_status", "source_reference", "frozen_symbols", "created_at", "source_hashes", "content_sha256", "package_sha256")}
        for owner in sorted(selected)}
    plan = {"schema_version": PLAN_SCHEMA, "status": "COMPLETE", "authority": "INFORMATIONAL_ONLY",
        "action_date": day, "as_of": now.isoformat(), "account_scope_sha256": scope,
        "account_snapshot_sha256": content_sha256(snapshot), "account_snapshot_observed_at": snapshot["observed_at"],
        "input_bindings": bindings, "ranking_policy": RANKING_POLICY,
        "sole_executor_owner": executor_owner,
        "holding_policy": SIGNAL_DRIVEN_HOLDING_POLICY, "cross_horizon_fallback_policy": deepcopy(fallback_policy),
        "risk_policy_fingerprint": effective_policy.fingerprint,
        "cash_basis": "ONE_ACCOUNT_SNAPSHOT_CONDITIONAL_ON_PROJECTED_FILLS",
        "live_capital_reserved": False, "live_orders_authorized": False,
        "forecasts": outputs, "ledger": ledger,
        "limitations": ["Projected sales fund projected purchases only if fills occur and the broker makes cash available.",
            "Unknown, cancelled or unfilled orders do not release live cash reservations.",
            "A joint forecast does not serialize live submissions or reserve real account capital.",
            "Source hashes verify consistency; caller-selected trusted bindings establish source provenance."]}
    plan_id = "joint-capital:" + content_sha256(plan)
    plan["plan_id"] = plan_id
    plan["owner_views"] = {owner: {"plan_id": plan_id, "owner_id": owner,
        "sole_executor_owner": executor_owner, "role": "EXECUTOR_AND_FORECAST_PRODUCER" if owner == executor_owner else "FORECAST_PRODUCER_ONLY",
        "package_sha256": selected[owner]["package_sha256"], "frozen_symbols": deepcopy(selected[owner]["frozen_symbols"]),
        "forecast_ids": [row["id"] for row in outputs if row["owner_id"] == owner],
        "event_sequences": [event["sequence"] for event in ledger["events"] if event["symbol"] in selected[owner]["frozen_symbols"]]}
        for owner in sorted(selected)}
    plan["plan_sha256"] = content_sha256(plan)
    return plan


def _ordinary_path(path: Path, root: Path, *, missing_leaf: bool = False) -> Path:
    root, path = Path(os.path.abspath(root)), Path(os.path.abspath(path))
    if not path.is_relative_to(root):
        raise ValueError("Joint artifact path escapes its explicit root")
    for item in [*reversed(root.parents), root, *(root / relative for relative in reversed(path.relative_to(root).parents) if str(relative) != "."), path]:
        if missing_leaf and item == path and not item.exists():
            continue
        info = item.lstat()
        if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
            raise ValueError("Joint artifacts cannot use links or reparse points")
        if item == path and not missing_leaf and not stat.S_ISREG(info.st_mode):
            raise ValueError("Joint artifact must be an ordinary file")
        if item != path and not stat.S_ISDIR(info.st_mode):
            raise ValueError("Joint artifact root must be an ordinary directory")
    return path


def _validate_plan_digest(plan: Mapping, expected_sha256: str) -> None:
    if plan.get("schema_version") != PLAN_SCHEMA or plan.get("plan_sha256") != _hash(expected_sha256, "plan"):
        raise ValueError("Joint plan differs from its selected digest")
    if content_sha256({k: v for k, v in plan.items() if k != "plan_sha256"}) != expected_sha256:
        raise ValueError("Joint plan contents disagree with its digest")
    identity = "joint-capital:" + content_sha256({k: v for k, v in plan.items() if k not in {"plan_sha256", "plan_id", "owner_views"}})
    if plan.get("plan_id") != identity or any(view.get("plan_id") != identity for view in plan.get("owner_views", {}).values()):
        raise ValueError("Joint plan or owner view identity disagrees with its projection")


def publish_joint_plan(root: Path, plan: Mapping) -> Path:
    """Create one new private JSON artifact; existing artifacts are immutable."""
    _validate_plan_digest(plan, plan.get("plan_sha256"))
    data = _json_bytes(plan)
    if len(data) > MAXIMUM_JSON_BYTES:
        raise ValueError("Joint plan exceeds its JSON size bound")
    path = _ordinary_path(Path(root) / f"{plan['plan_sha256']}.json", root, missing_leaf=True)
    with path.open("xb") as output:
        output.write(data)
        output.flush()
        os.fsync(output.fileno())
    return path


def load_joint_plan(root: Path, path: Path, *, expected_sha256: str) -> dict:
    """Read only an existing bounded ordinary file beneath its explicit root."""
    selected = _ordinary_path(path, root)
    with selected.open("rb") as source:
        data = source.read(MAXIMUM_JSON_BYTES + 1)
    if len(data) > MAXIMUM_JSON_BYTES:
        raise ValueError("Joint plan exceeds its JSON size bound")
    plan = json.loads(data)
    if not isinstance(plan, dict):
        raise ValueError("Joint plan must be a JSON object")
    _validate_plan_digest(plan, expected_sha256)
    return plan


def publish_owner_package(root: Path, package: Mapping) -> Path:
    """Create a sealed forecast/price package without touching source artifacts."""
    _validate_package(package)
    data = _json_bytes(package)
    if len(data) > MAXIMUM_JSON_BYTES:
        raise ValueError("Owner package exceeds its JSON size bound")
    path = _ordinary_path(Path(root) / f"{package['package_sha256']}.json", root, missing_leaf=True)
    with path.open("xb") as output:
        output.write(data)
        output.flush()
        os.fsync(output.fileno())
    return path


def load_owner_package(root: Path, path: Path, *, expected_sha256: str) -> dict:
    """Load one caller-selected package; its self-declared digest is insufficient."""
    selected = _ordinary_path(path, root)
    with selected.open("rb") as source:
        data = source.read(MAXIMUM_JSON_BYTES + 1)
    if len(data) > MAXIMUM_JSON_BYTES:
        raise ValueError("Owner package exceeds its JSON size bound")
    package = json.loads(data)
    _validate_package(package)
    if package["package_sha256"] != _hash(expected_sha256, "selected package"):
        raise ValueError("Package differs from its independently selected source binding")
    return package
