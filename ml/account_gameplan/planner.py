"""Combine frozen producers through one existing cash ledger; never submit orders."""
from __future__ import annotations

from dataclasses import asdict, dataclass
from decimal import Decimal
from datetime import datetime, timezone
import hashlib
import io
import json
from pathlib import Path, PurePosixPath
from typing import Mapping, Sequence

import pandas as pd

from ml.account_gameplan.sources import SourceBundle, read_source_bundle
from ml.account_gameplan.joint_bridge import account_forecast_id, owner_package, joint_snapshot, assert_same_source
from ml.gameplan_cash_ledger import UnavailablePlanningPricePath
from ml import joint_capital_plan
from ml.stock_trader.contracts import StockTraderPolicy
from ml.stock_trader.fixed_horizon_budget import FIXED_HORIZON_WEIGHTS

VERSION = "combined-account-gameplan-v1"
_POLICIES = ("probability_target_contract", "target_price_source_contract", "target_price_dataset",
             "cross_horizon_fallback_policy", "holding_policy", "direction_policy_version",
             "direction_up_threshold", "direction_down_threshold")


def encoded(value) -> bytes:
    return (json.dumps(value, sort_keys=True, indent=2, allow_nan=False) + "\n").encode("utf-8")


def sha(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def aware(value) -> pd.Timestamp:
    result = pd.Timestamp(value)
    if pd.isna(result) or result.tzinfo is None:
        raise ValueError("Explicit timezone-aware clock required")
    return result.tz_convert("UTC")


def _money(value) -> Decimal:
    result = Decimal(str(value))
    if not result.is_finite() or result < 0:
        raise ValueError("Invalid account money or inventory")
    return result


def combine_inputs(sources: Sequence[SourceBundle]):
    """Pure union. Publication below reopens all bundles with their explicit pins."""
    if len(sources) != 2 or len({s.metadata["producer_id"] for s in sources}) != 2:
        raise ValueError("Exactly two distinct pinned producers are required")
    first = sources[0].metadata
    from ml.stock_direction_policy import (STOCK_DIRECTION_POLICY_VERSION,
                                           BULLISH_PROBABILITY, BEARISH_PROBABILITY)
    if (first.get("holding_policy") != "accumulate_bullish_sell_on_bearish_no_scheduled_expiry_v1"
            or first.get("direction_policy_version") != STOCK_DIRECTION_POLICY_VERSION
            or first.get("direction_up_threshold") != BULLISH_PROBABILITY
            or first.get("direction_down_threshold") != BEARISH_PROBABILITY):
        raise ValueError("Combined planning requires the selected current manual holding and direction policies")
    frames, points, membership, evidence = [], {}, {}, []
    for source in sorted(sources, key=lambda s: s.metadata["producer_id"]):
        meta = source.metadata
        if meta["action_date"] != first["action_date"]:
            raise ValueError("Producer action sessions differ")
        if any(meta.get(key) != first.get(key) for key in _POLICIES):
            raise ValueError("Producer source or operating policies differ")
        symbols = tuple(meta["symbols"])
        if not symbols or len(set(symbols)) != len(symbols):
            raise ValueError("Invalid producer symbol registry")
        if any(symbol in membership for symbol in symbols):
            raise ValueError("Overlapping symbol authority is not permitted")
        membership.update({symbol: meta["producer_id"] for symbol in symbols})
        frame = source.forecasts.copy(deep=True)
        if (set(frame.symbol) != set(symbols) or len(frame) != 24 * len(symbols)
                or not frame.groupby("symbol").size().eq(24).all()
                or frame.id.isna().any() or frame.id.duplicated().any()
                or not frame.action_date.astype(str).eq(meta["action_date"]).all()):
            raise ValueError("Incomplete or conflicting producer forecast coverage")
        frame["original_forecast_id"] = frame.id.astype(str)
        frame["producer_id"] = meta["producer_id"]
        frame["source_receipt_sha256"] = meta["source_receipt_sha256"]
        frame["id"] = [account_forecast_id(meta["producer_id"], PurePosixPath(meta["source_gameplan_run"]).name, v)
                       for v in frame.original_forecast_id]
        frames.append(frame)
        expected_points = {f"{symbol}|{meta['action_date']}|{hour:02d}:00"
                           for symbol in symbols for hour in range(4, 18)}
        if set(source.price_path["points"]) != expected_points or set(points) & expected_points:
            raise ValueError("Producer hourly price coverage differs from its universe")
        points.update(source.price_path["points"])
        evidence.append({"producer_id": meta["producer_id"], "symbols": list(symbols),
                         "source_receipt_sha256": meta["source_receipt_sha256"],
                         "bundle_manifest_sha256": source.manifest_sha256,
                         "source_gameplan_run": meta["source_gameplan_run"],
                         "deployment_status": meta.get("deployment_status", "NO_REGISTERED_HANDOFF"),
                         "source_published_at": meta["source_published_at"]})
    forecasts = pd.concat(frames, ignore_index=True)
    if forecasts.id.duplicated().any():
        raise ValueError("Combined forecast identity collision")
    return forecasts, {"points": points, "producer_price_paths": evidence}, membership, evidence


def _capacity_rows(forecasts, snapshot, prices, policy):
    """Independent opportunity capacity; never add capacities to shared cash."""
    rows = forecasts.copy(deep=True)
    rows["projected_trade_quantity"] = 0
    rows["trade_price_low"] = None
    rows["trade_price_mid"] = None
    rows["trade_price_high"] = None
    equity = _money(snapshot["account_equity"])
    cash = _money(snapshot["available_cash"]) * _money(policy.maximum_cash_utilization_fraction)
    gross_remaining = max(Decimal(0), equity * _money(policy.maximum_gross_equity_fraction)
                          - _money(snapshot["gross_exposure"]) - _money(snapshot["reserved_cash"]))
    for index, row in rows.iterrows():
        if not row.execution_eligible:
            continue
        start = aware(row.target_window_start).tz_convert("America/Los_Angeles")
        point = prices["points"][f"{row.symbol}|{row.action_date}|{start:%H:%M}"]
        if point.get("status") != "AVAILABLE":
            rows.at[index, "projected_trade_quantity"] = None
            continue
        low, mid, high = [_money(point[f"planned_price_{field}"]) for field in ("low", "mid", "high")]
        if not 0 < low <= mid <= high:
            raise ValueError("Invalid conditional price range")
        ceiling = equity * min(_money(policy.maximum_symbol_equity_fraction)
                               * FIXED_HORIZON_WEIGHTS[row.model_group] / 10,
                               _money(policy.maximum_single_order_equity_fraction))
        pending = _money(snapshot.get("pending_buy_shares", {}).get(row.symbol, 0))
        pending_value = pending * _money(snapshot["quotes"][row.symbol]["ask"]) if pending else Decimal(0)
        symbol_remaining = max(Decimal(0), equity * _money(policy.maximum_symbol_equity_fraction)
                               - _money(snapshot["symbol_exposure"][row.symbol]) - pending_value)
        quantity = int(min(ceiling, cash, gross_remaining, symbol_remaining) / high)
        if quantity * high < _money(policy.minimum_order_notional):
            quantity = 0
        rows.at[index, "projected_trade_quantity"] = quantity
        for field, value in zip(("low", "mid", "high"), (low, mid, high)):
            rows.at[index, f"trade_price_{field}"] = float(value)
    return rows


def project_account_plan(sources, snapshot, *, observed_at, policy=None):
    """Read-only projection of pinned inputs through Scout's one account ledger."""
    policy = policy or StockTraderPolicy()
    policy.validate()
    now = aware(observed_at)
    forecasts, prices, membership, evidence = combine_inputs(sources)
    day = str(sources[0].metadata["action_date"])
    deadline = pd.Timestamp(day, tz="America/Los_Angeles") + pd.Timedelta(hours=4)
    if now >= deadline:
        raise ValueError("Original action-session preparation deadline has passed")
    if any(aware(source.metadata[field]) > now for source in sources
           for field in ("source_published_at", "trade_plan_completed_at", "planning_observed_at")):
        raise ValueError("Producer source evidence is future-dated")
    snapshot_at = aware(snapshot["observed_at"])
    if not 0 <= (now - snapshot_at).total_seconds() <= 60:
        raise ValueError("Combined account snapshot is stale or future-dated")
    if (snapshot.get("status") != "OBSERVED" or snapshot.get("cash_status") != "CASH_ONLY_BOUNDED"
            or snapshot.get("ownership", {}).get("safe_for_planning") is not True):
        raise ValueError("Complete coherent account and ownership evidence required")
    if set(snapshot["held_shares"]) != set(membership):
        raise ValueError("Account snapshot must cover the exact combined universe")
    fallback = sources[0].metadata.get("cross_horizon_fallback_policy")
    packages = [owner_package(source) for source in sources]
    if {package["owner_id"] for package in packages} != {"pc-original", "pc-new"}:
        raise ValueError("Combined planning requires the configured Atlas and Scout producers")
    rows = _capacity_rows(forecasts, snapshot, prices, policy)
    joint = None
    try:
        joint = joint_capital_plan.compose_joint_plan(packages, joint_snapshot(snapshot), action_date=day,
            expected_universes={p["owner_id"]: p["frozen_symbols"] for p in packages},
            expected_package_sha256={p["owner_id"]: p["package_sha256"] for p in packages},
            account_scope_sha256=snapshot["account_fingerprint"], as_of=now.isoformat(),
            executor_owner="pc-original", policy=policy, maximum_snapshot_age_seconds=60)
        projected = pd.DataFrame(joint["forecasts"]).set_index("id")
        if set(projected.index) != set(rows.id):
            raise ValueError("Canonical composition changed source forecast identities")
        # Keep native source fields and independent capacity display. Only Scout
        # supplies the sequential cash/share projection and its unchanged IDs.
        projection_fields = {"direction_based_trade_quantity", "direction_based_action", "direction_based_reason",
            "projected_cash_after_low", "projected_cash_after_base", "projected_cash_after_high",
            "projected_shares_after", "projected_available_shares_after", "owner_id", "owner_run_id"}
        projection_fields |= {name for name in projected.columns if name.startswith("fallback_")}
        for field in projection_fields:
            if field in projected:
                rows[field] = rows.id.map(projected[field])
        ledger = joint["ledger"]
        status = "COMPLETE"
    except UnavailablePlanningPricePath as exc:
        status = "UNAVAILABLE_PRICE_REFERENCES"
        ledger = {"status": status, "reason": "Missing observed price references for the combined account.",
                  "unavailable_points": exc.points, "events": [], "hourly": [],
                  "ending_positions": {}, "summary": {}, "orders_placed": 0, "broker_orders_enabled": False}
        for field in ("direction_based_trade_quantity", "projected_cash_after_low",
                      "projected_cash_after_base", "projected_cash_after_high", "projected_shares_after",
                      "projected_available_shares_after"):
            rows[field] = None
        rows["direction_based_action"] = None
        rows["direction_based_reason"] = None
    report = {"schema_version": VERSION, "action_date": day, "observed_at": now.isoformat(),
              "deadline_at": deadline.isoformat(), "status": "COMPLETE",
              "direction_projection_status": status, "forecast_rows": len(rows),
              "hourly_price_points": len(prices["points"]), "symbols": sorted(membership),
              "symbol_producers": membership, "sources": evidence, "sizing_policy": asdict(policy),
              "account_fingerprint": snapshot.get("account_fingerprint"),
              "joint_composition": joint,
              "joint_package_pins": {p["owner_id"]: p["package_sha256"] for p in packages},
              **{key: sources[0].metadata.get(key) for key in _POLICIES},
              "cross_horizon_fallback_policy": fallback, "direction_based_projection": ledger,
              "orders_placed": 0, "broker_orders_enabled": False,
              "execution_authority": "INFORMATIONAL_ACCOUNT_PLAN",
              "limitations": ["Projected fills and sale proceeds are conditional, not actual broker balances.",
                              "Live execution requires the separately activated sole coordinator and current broker evidence."]}
    return rows, report, prices


@dataclass(frozen=True)
class AccountPlan:
    path: Path
    manifest_sha256: str
    rows: pd.DataFrame
    report: Mapping
    ledger: Mapping

    def view(self, producer_id):
        if producer_id not in {s["producer_id"] for s in self.report["sources"]}:
            raise ValueError("Unknown producer view")
        return self.rows.loc[self.rows.producer_id.eq(producer_id)].copy(), self.ledger


def render_account_plan(rows, report, producer_id=None):
    visible = rows if producer_id is None else rows.loc[rows.producer_id.eq(producer_id)]
    lines = [f"# Combined account Gameplan — {report['action_date']}", "",
             f"Scope: {producer_id or 'all producers'} · {len(visible)} forecasts", "",
             "Cash balances apply to the entire account after all symbols at each clock.",
             "Planned sale proceeds and fills are conditional. Live cash follows broker evidence.", "",
             "| Company | Horizon | Window starts | Projected Trade Quantity | Direction Based Trade Qty | Trade Price range | Cash available after (range) | Shares remaining | Plan action |",
             "|---|---|---|---:|---:|---|---|---:|---|"]
    def number(v):
        return "—" if v is None or pd.isna(v) else f"{v:,.2f}"
    for row in visible.sort_values(["target_window_start", "symbol", "model_group"]).to_dict("records"):
        entry = row["execution_eligible"]
        capacity = number(row.get("projected_trade_quantity")) if entry else "—"
        direction = number(row.get("direction_based_trade_quantity")) if entry else "—"
        cash = f"{number(row.get('projected_cash_after_low'))}–{number(row.get('projected_cash_after_high'))}"
        price = f"{number(row.get('trade_price_low'))}–{number(row.get('trade_price_high'))}"
        lines.append(f"| {row['symbol']} | {row['model_group']} | {aware(row['target_window_start']).tz_convert('America/Los_Angeles'):%m-%d %H:%M} | {capacity} | {direction} | {price} | {cash} | {number(row.get('projected_shares_after'))} | {row.get('direction_based_action', 'CONTEXT')} |")
    ledger = report["direction_based_projection"]
    lines += ["", "## Entire account by hour", "",
              "| Pacific clock | Cash low | Cash base | Cash high | Shares across all symbols |",
              "|---|---:|---:|---:|---|"]
    for hour in ledger.get("hourly", []):
        holdings = ", ".join(f"{s}: {q:g}" for s, q in sorted(hour["held_shares"].items()))
        lines.append(f"| {aware(hour['timestamp']).tz_convert('America/Los_Angeles'):%H:%M} | {number(hour['cash_low'])} | {number(hour['cash_base'])} | {number(hour['cash_high'])} | {holdings} |")
    lines += ["", "## Ordered account events", "", "All sale proceeds and dependent buys below assume the planned fills.", "",
              "| # | Pacific clock | Company | Horizon | Action | Shares | Cash before (low/base/high) | Cash change (low/base/high) | Cash after (low/base/high) |",
              "|---:|---|---|---|---|---:|---|---|---|"]
    for event in ledger.get("events", []):
        before = " / ".join(number(event[f"cash_before_{suffix}"]) for suffix in ("low", "base", "high"))
        change = " / ".join(number(event[f"cash_change_{suffix}"]) for suffix in ("low", "base", "high"))
        after = " / ".join(number(event[f"cash_{suffix}"]) for suffix in ("low", "base", "high"))
        lines.append(f"| {event['sequence']} | {aware(event['timestamp']).tz_convert('America/Los_Angeles'):%H:%M} | {event['symbol']} | {event['horizon']} | {event['action']} | {event['quantity']} | {before} | {change} | {after} |")
    if ledger.get("summary"):
        summary = ledger["summary"]
        lines += ["", "Ending cash (low/base/high): " + " / ".join(number(summary[f"ending_cash_{s}"]) for s in ("low", "base", "high")) + ".",
                  "Ending shares: " + ", ".join(f"{s}: {q:g}" for s, q in sorted(ledger["ending_positions"].items())) + ".",
                  f"No-fill cash baseline: {number(summary['starting_cash'])}; starting shares remain held without fills.",
                  "Estimates are before fees and taxes. Holdings persist until a qualifying sale instruction."]
    else:
        lines += ["", ledger.get("reason", "Price references unavailable; no cash or holdings projection.")]
    lines += ["", "Per-producer views filter one account calculation; the account tables retain both producers.", ""]
    return "\n".join(lines)


def publish_account_plan(destination, sources, snapshot, *, observed_at, clock=None):
    now = aware(observed_at)
    verified = [read_source_bundle(s.path, expected_manifest_sha256=s.manifest_sha256,
                    expected_producer=s.metadata["producer_id"], expected_symbols=s.metadata["symbols"],
                    expected_action_date=s.metadata["action_date"]) for s in sources]
    for original, pinned in zip(sources, verified):
        assert_same_source(original, pinned)
    rows, report, prices = project_account_plan(verified, snapshot, observed_at=now)
    destination = Path(destination).resolve()
    destination.mkdir(parents=True, exist_ok=False)
    rows.to_parquet(destination / "trade-plan.parquet", index=False)
    outputs = {"report.json": report, "direction-ledger.json": report["direction_based_projection"],
               "account-snapshot.json": snapshot, "planning-price-path.json": prices}
    for name, payload in outputs.items():
        (destination / name).write_bytes(encoded(payload))
    (destination / "Gameplan.md").write_text(render_account_plan(rows, report), encoding="utf-8")
    for source in report["sources"]:
        (destination / f"Gameplan-{source['producer_id']}.md").write_text(
            render_account_plan(rows, report, source["producer_id"]), encoding="utf-8")
    for source in verified:
        read_source_bundle(source.path, expected_manifest_sha256=source.manifest_sha256,
                           expected_producer=source.metadata["producer_id"], expected_symbols=source.metadata["symbols"],
                           expected_action_date=source.metadata["action_date"])
    complete_at = aware((clock or (lambda: datetime.now(timezone.utc)))())
    if complete_at < now or complete_at >= aware(report["deadline_at"]):
        raise ValueError("Combined account publication deadline passed")
    if not 0 <= (complete_at - aware(snapshot["observed_at"])).total_seconds() <= 60:
        raise ValueError("Combined account snapshot became stale before publication completed")
    manifest = {"schema_version": VERSION, "action_date": report["action_date"], "sources": report["sources"],
                "output_files": {p.name: sha(p.read_bytes()) for p in destination.iterdir() if p.is_file()}}
    (destination / "manifest.json").write_bytes(encoded(manifest))
    manifest_hash = sha((destination / "manifest.json").read_bytes())
    (destination / "receipt.json").write_bytes(encoded({"schema_version": VERSION, "status": "COMPLETE",
                "action_date": report["action_date"], "manifest_sha256": manifest_hash,
                "completed_at": complete_at.isoformat(), "orders_placed": 0, "broker_orders_enabled": False}))
    plan = read_account_plan(destination, expected_manifest_sha256=manifest_hash)
    finished = aware((clock or (lambda: datetime.now(timezone.utc)))())
    if (finished < complete_at or finished >= aware(report["deadline_at"])
            or not 0 <= (finished - aware(snapshot["observed_at"])).total_seconds() <= 60):
        (destination / "receipt.json").write_bytes(encoded({"schema_version": VERSION, "status": "FAILED",
            "action_date": report["action_date"], "manifest_sha256": manifest_hash,
            "completed_at": finished.isoformat(), "failure": "FINAL_DEADLINE_OR_SNAPSHOT_FRESHNESS_GUARD",
            "orders_placed": 0, "broker_orders_enabled": False}))
        raise ValueError("Combined account publication deadline or snapshot freshness crossed during final writes")
    return plan


def read_account_plan(path, *, expected_manifest_sha256):
    path = Path(path).resolve()
    raw = (path / "manifest.json").read_bytes()
    if sha(raw) != expected_manifest_sha256:
        raise ValueError("Combined plan manifest pin mismatch")
    manifest = json.loads(raw)
    receipt = json.loads((path / "receipt.json").read_bytes())
    if (manifest.get("schema_version") != VERSION or receipt.get("status") != "COMPLETE"
            or receipt.get("manifest_sha256") != expected_manifest_sha256
            or receipt.get("action_date") != manifest.get("action_date")
            or receipt.get("orders_placed") != 0 or receipt.get("broker_orders_enabled") is not False):
        raise ValueError("Combined account receipt or authority mismatch")
    required = {"trade-plan.parquet", "report.json", "direction-ledger.json", "account-snapshot.json", "planning-price-path.json", "Gameplan.md"}
    outputs = manifest["output_files"]
    producers = [source["producer_id"] for source in manifest["sources"]]
    if len(producers) != 2 or len(set(producers)) != 2:
        raise ValueError("Two source producers are required")
    required |= {f"Gameplan-{producer}.md" for producer in producers}
    if required != set(outputs):
        raise ValueError("Incomplete account publication")
    verified_bytes = {}
    for name, checksum in outputs.items():
        target = path / name
        if ("/" in name or "\\" in name or Path(name).is_absolute() or target.parent != path
                or target.is_symlink()):
            raise ValueError("Combined plan output checksum mismatch")
        payload = target.read_bytes()
        if sha(payload) != checksum:
            raise ValueError("Combined plan output checksum mismatch")
        verified_bytes[name] = payload
    report = json.loads(verified_bytes["report.json"])
    ledger = json.loads(verified_bytes["direction-ledger.json"])
    snapshot = json.loads(verified_bytes["account-snapshot.json"])
    prices = json.loads(verified_bytes["planning-price-path.json"])
    rows = pd.read_parquet(io.BytesIO(verified_bytes["trade-plan.parquet"]))
    expected_membership = {}
    for source in report["sources"]:
        if set(source["symbols"]) & set(expected_membership):
            raise ValueError("Overlapping account source universes")
        expected_membership.update({symbol: source["producer_id"] for symbol in source["symbols"]})
        partition = rows.loc[rows.producer_id.eq(source["producer_id"])]
        if (set(partition.symbol) != set(source["symbols"])
                or not partition.source_receipt_sha256.eq(source["source_receipt_sha256"]).all()
                or not partition.id.eq([account_forecast_id(source["producer_id"], PurePosixPath(source["source_gameplan_run"]).name, original)
                                       for original in partition.original_forecast_id]).all()):
            raise ValueError("Combined row lost its original source identity")
    deadline = pd.Timestamp(report["action_date"], tz="America/Los_Angeles") + pd.Timedelta(hours=4)
    if (report.get("schema_version") != VERSION or report.get("status") != "COMPLETE"
            or report.get("orders_placed") != 0 or report.get("broker_orders_enabled") is not False
            or report["sources"] != manifest["sources"] or report["action_date"] != manifest["action_date"]
            or set(rows.symbol) != set(expected_membership) or report["symbol_producers"] != expected_membership
            or report["symbols"] != sorted(expected_membership) or not rows.action_date.astype(str).eq(report["action_date"]).all()
            or ledger != report["direction_based_projection"] or rows.id.duplicated().any()
            or len(rows) != 24 * len(report["symbols"]) or not rows.groupby("symbol").size().eq(24).all()
            or aware(report["deadline_at"]) != deadline or aware(receipt["completed_at"]) < aware(report["observed_at"])
            or report.get("account_fingerprint") != snapshot.get("account_fingerprint")
            or not 0 <= (aware(receipt["completed_at"]) - aware(snapshot["observed_at"])).total_seconds() <= 60
            or aware(receipt["completed_at"]) >= deadline):
        raise ValueError("Combined plan coverage or source mismatch")
    expected_points = {f"{symbol}|{report['action_date']}|{hour:02d}:00" for symbol in expected_membership for hour in range(4, 18)}
    if set(prices["points"]) != expected_points or report["hourly_price_points"] != len(expected_points):
        raise ValueError("Combined price path coverage differs from the frozen universe")
    joint = report.get("joint_composition")
    if report["direction_projection_status"] == "COMPLETE":
        if not isinstance(joint, dict):
            raise ValueError("Canonical joint composition proof is missing")
        joint_capital_plan._validate_plan_digest(joint, joint.get("plan_sha256"))
        bindings = joint["input_bindings"]
        if (joint["ledger"] != ledger or joint["action_date"] != report["action_date"]
                or joint["sole_executor_owner"] != "pc-original"
                or joint["holding_policy"] != report["holding_policy"]
                or joint["cross_horizon_fallback_policy"] != report["cross_horizon_fallback_policy"]
                or aware(joint["as_of"]) != aware(report["observed_at"])
                or joint["account_scope_sha256"] != report["account_fingerprint"]
                or aware(joint["account_snapshot_observed_at"]) != aware(snapshot["observed_at"])
                or joint["account_snapshot_sha256"] != joint_capital_plan.content_sha256(joint_snapshot(snapshot))
                or joint["risk_policy_fingerprint"] != StockTraderPolicy(**report["sizing_policy"]).fingerprint
                or set(bindings) != set(producers)
                or {owner: value["package_sha256"] for owner, value in bindings.items()} != report["joint_package_pins"]):
            raise ValueError("Canonical joint composition differs from account evidence")
        for source in report["sources"]:
            binding = bindings[source["producer_id"]]
            if (binding["run_id"] != PurePosixPath(source["source_gameplan_run"]).name
                    or binding["source_hashes"]["receipt_sha256"] != source["source_receipt_sha256"]
                    or set(binding["frozen_symbols"]) != set(source["symbols"])):
                raise ValueError("Canonical joint source binding differs from native source")
        by_id = rows.set_index("id").to_dict("index")
        if {row["id"] for row in joint["forecasts"]} != set(by_id):
            raise ValueError("Canonical joint forecast coverage mismatch")
        for frozen in joint["forecasts"]:
            projected = by_id[frozen["id"]]
            for field, value in frozen.items():
                if field == "id":
                    continue
                actual = projected.get(field)
                if not isinstance(actual, (dict, list)) and pd.isna(actual):
                    actual = None
                if isinstance(actual, pd.Timestamp):
                    actual = actual.isoformat()
                if actual != value:
                    raise ValueError("Account row differs from canonical joint composition")
    else:
        unavailable_fields = {"direction_based_trade_quantity", "direction_based_action", "direction_based_reason",
            "projected_cash_after_low", "projected_cash_after_base", "projected_cash_after_high",
            "projected_shares_after", "projected_available_shares_after"}
        if (report["direction_projection_status"] != "UNAVAILABLE_PRICE_REFERENCES"
                or ledger.get("status") != "UNAVAILABLE_PRICE_REFERENCES" or joint is not None
                or any(ledger.get(field) for field in ("events", "hourly", "summary", "ending_positions"))
                or not unavailable_fields.issubset(rows.columns)
                or not rows[list(unavailable_fields)].isna().all().all()):
            raise ValueError("Unavailable joint projection cannot contain invented account outcomes")
    return AccountPlan(path, expected_manifest_sha256, rows, report, ledger)
