"""Read-only, timestamped HYPER Paper versus Duckets account comparison.

The collector never constructs a ledger writer or an exchange signer.  Current
holdings are repriced at one public market snapshot.  An automatic performance
comparison is available only for a verified mirror with complete, empty public
non-funding ledger histories; nonzero or ambiguous flows require review.
"""
from __future__ import annotations

import argparse
from dataclasses import asdict, replace
from datetime import datetime, timezone
import hashlib
import json
import math
from pathlib import Path
import re
import sqlite3

from app.config import hyperliquid_accounts
from app.hyperliquid_accounts import HYPERLIQUID_ACCOUNT_PROFILES, resolve_portfolio_wallet
from app.services.hyperliquid import HyperliquidInfoClient, _sync_hyperliquid_portfolio_with_market
from app.services.hyperliquid_markets import spot_catalog
from ml.hyperliquid_paper_seed import ALIASES
from ml.hyperliquid_paper_forecast_evidence import collect_forecast_evidence


def _now():
    return datetime.now(timezone.utc).isoformat()


def _number(value):
    if isinstance(value, bool):
        raise ValueError("Boolean is not an account value.")
    result = float(value)
    if not math.isfinite(result):
        raise ValueError("Nonfinite account value.")
    return result


def _utc(value):
    result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if result.tzinfo is None:
        raise ValueError("Observation timestamps must have a timezone.")
    return result.astimezone(timezone.utc)


def _public_record(value):
    """Keep public source evidence without persisting owner wallet addresses."""
    if isinstance(value, dict):
        return {key: _public_record(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_public_record(item) for item in value]
    if isinstance(value, str) and re.fullmatch(r"0x[0-9a-fA-F]{40}", value):
        return "wallet-sha256:" + hashlib.sha256(value.lower().encode()).hexdigest()
    return value


class RecordingInfoClient:
    """Restrict every outbound request to explicitly public information types."""

    allowed = {"allMids", "spotMetaAndAssetCtxs", "userRole", "clearinghouseState",
               "spotClearinghouseState", "userAbstraction", "frontendOpenOrders",
               "userFills", "userNonFundingLedgerUpdates"}

    def __init__(self, client):
        self.client = client
        self.info_url = client.info_url
        self.records = []
        self.account = None

    def post_info(self, payload):
        if not isinstance(payload, dict) or payload.get("type") not in self.allowed:
            raise ValueError("Comparison accepts only public information requests.")
        record = {"account": self.account, "request": _public_record(payload),
                  "started_at_utc": _now()}
        try:
            response = self.client.post_info(payload)
            record["response"] = _public_record(response)
            return response
        except Exception as exc:
            record["error_type"] = type(exc).__name__
            raise
        finally:
            record["completed_at_utc"] = _now()
            self.records.append(record)


def read_paper_snapshot(data_root):
    root = Path(data_root)
    experiment_path = root / "_paper/experiment.json"
    experiment_bytes = experiment_path.read_bytes()
    experiment = json.loads(experiment_bytes)
    registry_path = root / "_operations/excluded-paper-runs.json"
    excluded = json.loads(registry_path.read_text(encoding="utf-8")) if registry_path.exists() else {}
    for row in excluded.get("excluded_runs", []):
        if (experiment.get("experiment_id") == row.get("experiment_id")
                or experiment.get("seed_at_utc") == row.get("seed_at_utc")):
            raise ValueError("Current experiment is excluded; its ledger was not opened.")
    if experiment.get("analysis_eligible") is not True:
        raise ValueError("Current experiment is not marked analysis eligible.")
    path = root / "_paper/ledger.sqlite3"
    with sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True) as connection:
        connection.row_factory = sqlite3.Row
        connection.execute("BEGIN")
        seed = json.loads(connection.execute("SELECT details_json FROM seed WHERE singleton=1").fetchone()[0])
        latest = json.loads(connection.execute(
            "SELECT result_json FROM cycles ORDER BY timestamp_utc DESC LIMIT 1").fetchone()[0])
        opening = json.loads(connection.execute(
            "SELECT result_json FROM cycles WHERE cycle_id='opening'").fetchone()[0])
        groups = {}
        for label, columns in (("by_coin", "coin"), ("by_reason", "reason"), ("by_account", "account")):
            groups[label] = [dict(row) for row in connection.execute(
                f"SELECT {columns}, COUNT(*) AS fills, SUM(notional) AS turnover, "
                f"SUM(fee) AS fees, SUM(realized_pnl) AS realized_pnl FROM fills GROUP BY {columns}")]
        counts = {name: connection.execute(f"SELECT COUNT(*) FROM {name}").fetchone()[0]
                  for name in ("cycles", "fills", "transfers", "funding")}
        decisions = [dict(row) for row in connection.execute(
            "SELECT d.decision_id,d.cycle_id,d.timestamp_utc,d.coin,d.forecast_id,d.model_id,"
            "d.details_json,c.timestamp_utc AS committed_at_utc FROM decisions d "
            "JOIN cycles c ON c.cycle_id=d.cycle_id "
            "WHERE d.timestamp_utc>=? AND d.timestamp_utc<=? ORDER BY d.timestamp_utc,d.decision_id",
            (seed["timestamp_utc"], latest["timestamp_utc"]))]
    if experiment_path.read_bytes() != experiment_bytes:
        raise ValueError("Experiment changed during comparison; retry after maintenance.")
    if seed["timestamp_utc"] != experiment.get("seed_at_utc"):
        raise ValueError("Experiment metadata and ledger opening disagree.")
    return {"experiment": experiment, "experiment_sha256": hashlib.sha256(experiment_bytes).hexdigest(),
            "seed": seed, "opening": opening, "latest": latest, "fill_groups": groups, "counts": counts,
            "forecast_evidence": collect_forecast_evidence(root, experiment, seed["timestamp_utc"],
                                                           latest["timestamp_utc"], decisions)}


def common_marks(all_mids, spot_meta):
    marks = {f"perp:{coin}": _number(value) for coin, value in all_mids.items()
             if re.fullmatch(r"[A-Z0-9]{1,20}", coin) and _number(value) > 0}
    for coin, route in spot_catalog(spot_meta).items():
        value = all_mids.get(route["coin"]) or route.get("mid")
        if value is not None and _number(value) > 0:
            marks[f"spot:{coin}"] = _number(value)
    return marks


def value_positions(cash, positions, marks):
    total = _number(cash)
    for position in positions:
        mark = _number(marks[f"{position['kind']}:{position['coin']}"])
        if mark <= 0:
            raise ValueError("Marks must be positive.")
        quantity = _number(position["quantity"])
        if position["kind"] == "spot":
            total += quantity * mark
        elif position["kind"] == "perp":
            total += quantity * (mark - _number(position["avg_entry"]))
        else:
            raise ValueError("Unknown position kind.")
    return total


def normalize_account(perp, spot, abstraction):
    """Preserve signed inventory and remove unified-account P/L once only."""
    if abstraction not in {"unifiedAccount", "portfolioMargin", "default", "standard",
                           "disabled", "dexAbstraction", None}:
        raise ValueError("Unsupported account abstraction.")
    if not isinstance(perp.get("assetPositions"), list) or not isinstance(spot.get("balances"), list):
        raise ValueError("Incomplete account inventory.")
    positions, reported_pnl, cash = [], 0.0, 0.0
    for row in perp["assetPositions"]:
        item = row["position"]
        quantity = _number(item["szi"])
        if quantity:
            positions.append({"coin": item["coin"], "kind": "perp", "quantity": quantity,
                              "avg_entry": _number(item["entryPx"])})
            reported_pnl += _number(item["unrealizedPnl"])
    for row in spot["balances"]:
        quantity = _number(row["total"])
        if row["coin"] in {"USDC", "USD"}:
            cash += quantity
        elif quantity:
            positions.append({"coin": ALIASES.get(row["coin"], row["coin"]), "kind": "spot",
                              "quantity": quantity, "avg_entry": 0.0})
    summary = perp.get("marginSummary") or perp.get("crossMarginSummary")
    if not isinstance(summary, dict) or "accountValue" not in summary:
        raise ValueError("Missing perpetual equity.")
    if abstraction not in {"unifiedAccount", "portfolioMargin"}:
        cash += _number(summary["accountValue"])
    return {"base_collateral": cash - reported_pnl, "positions": positions,
            "reported_perp_unrealized_pnl": reported_pnl, "account_mode": abstraction}


def assess_flows(rows, start_ms, end_ms):
    """Fail closed for truncated, malformed, or nonzero capital activity.

    The provider documents 500 elements for time-range responses.  A capped
    response is explicitly incomplete.  Any non-funding ledger event is retained
    for review rather than guessing its direction, token value, or pool boundary.
    """
    valid = isinstance(rows, list) and all(
        isinstance(row, dict) and type(row.get("time")) is int
        and start_ms <= row["time"] <= end_ms and isinstance(row.get("delta"), dict)
        for row in rows)
    complete = valid and len(rows) < 500
    return {"start_time_ms": start_ms, "end_time_ms": end_ms,
            "range_complete": complete, "event_count": len(rows) if isinstance(rows, list) else None,
            "zero_external_flows_verified": complete and not rows,
            "net_external_flow_usd": 0.0 if complete and not rows else None,
            "reason": "complete_empty_nonfunding_history" if complete and not rows else
                      "nonfunding_events_require_classification" if complete else
                      "malformed_or_capped_nonfunding_history"}


def read_actual_accounts(seed_at_utc, *, client=None, accounts=None):
    recorder = RecordingInfoClient(client or HyperliquidInfoClient(timeout_seconds=12))
    started = _now()
    mids = recorder.post_info({"type": "allMids"})
    mark_observed = recorder.records[-1]["completed_at_utc"]
    spot_meta = recorder.post_info({"type": "spotMetaAndAssetCtxs"})
    marks = common_marks(mids, spot_meta)
    start_ms = int(_utc(seed_at_utc).timestamp() * 1000)
    result = {}
    for account in (hyperliquid_accounts() if accounts is None else accounts):
        recorder.account = account.profile_key
        owner = account.wallet_address or resolve_portfolio_wallet(
            HYPERLIQUID_ACCOUNT_PROFILES[account.profile_key], recorder)
        configured = replace(account, wallet_address=owner)
        first = len(recorder.records)
        snapshot = _sync_hyperliquid_portfolio_with_market(
            configured, recorder, all_mids=mids, spot_meta_and_asset_ctxs=spot_meta,
            hype_market={}, chain_status={})
        account_observed = _now()
        reads = {row["request"]["type"]: row for row in recorder.records[first:]}
        normalized = normalize_account(reads["clearinghouseState"]["response"],
                                       reads["spotClearinghouseState"]["response"],
                                       reads["userAbstraction"]["response"])
        end_ms = int(_utc(account_observed).timestamp() * 1000)
        try:
            updates = recorder.post_info({"type": "userNonFundingLedgerUpdates", "user": owner,
                                          "startTime": start_ms, "endTime": end_ms})
            flows = assess_flows(updates, start_ms, end_ms)
        except Exception as exc:
            flows = {"range_complete": False, "zero_external_flows_verified": False,
                     "net_external_flow_usd": None, "reason": type(exc).__name__}
        result[account.profile_key] = {**normalized, "duckets_equity": snapshot.total_value,
            "observed_at_utc": account_observed, "flows": flows,
            "common_mark_equity": value_positions(normalized["base_collateral"], normalized["positions"], marks),
            "duckets_holdings": [asdict(row) for row in snapshot.holdings]}
    return {"started_at_utc": started, "completed_at_utc": _now(),
            "mark_observed_at_utc": mark_observed, "marks": marks,
            "accounts": result, "source_reads": recorder.records}


def reconcile_mirror_baseline(seed, opening, account_names):
    """Reconcile the saved source-to-mark bridge, not different-time totals.

    Unified-account cash already contains source perpetual P/L.  The seed
    subtracts that P/L from collateral, then adds the same signed positions'
    P/L at the opening marks.  The difference may legitimately exceed a dollar;
    only an unexplained residual invalidates the mirror.
    """
    def equal(actual, expected, label):
        if not math.isclose(_number(actual), _number(expected), rel_tol=1e-11, abs_tol=1e-7):
            raise ValueError(label)

    def indexed(positions):
        result = {}
        for position in positions:
            key = (position["account"], position["coin"], position["kind"])
            if key in result or key[0] not in account_names:
                raise ValueError("duplicate_or_unknown_opening_position")
            result[key] = position
        return result

    reconciled = {}
    try:
        sources = seed["metadata"]["accounts"]
        names = set(account_names)
        if (not names or seed["metadata"]["seed_mode"] != "mirror"
                or names != set(sources) or names != set(seed["cash"])
                or names != set(seed["baseline_equity"])):
            raise ValueError("mirror_account_membership_mismatch")
        saved, observed = indexed(seed["positions"]), indexed(opening["positions"])
        if saved.keys() != observed.keys():
            raise ValueError("opening_inventory_membership_mismatch")
        for key, position in saved.items():
            mark = _number(seed["marks"][f"{key[2]}:{key[1]}"])
            if mark <= 0 or key[2] not in {"perp", "spot"}:
                raise ValueError("invalid_opening_mark_or_position_kind")
            equal(position["mark_price"], mark, "seed_position_mark_mismatch")
            for field in ("quantity", "avg_entry", "mark_price"):
                equal(position[field], observed[key][field], "opening_position_" + field + "_mismatch")
        for account in names:
            source = sources[account]
            mode = source["source_account_mode"]
            if mode not in {"unifiedAccount", "portfolioMargin", "default", "standard",
                            "disabled", "dexAbstraction", None}:
                raise ValueError("unsupported_opening_account_mode")
            positions = [position for key, position in saved.items() if key[0] == account]
            source_pnl = sum(_number(p["source_unrealized_pnl"]) for p in positions if p["kind"] == "perp")
            equal(source_pnl, source["source_perp_unrealized_pnl"], "source_perpetual_pnl_sum_mismatch")
            spot_value = sum(_number(p["quantity"]) * _number(p["mark_price"])
                             for p in positions if p["kind"] == "spot")
            marked_pnl = sum(_number(p["quantity"]) * (_number(p["mark_price"]) - _number(p["avg_entry"]))
                             for p in positions if p["kind"] == "perp")
            perp_value = 0.0 if mode in {"unifiedAccount", "portfolioMargin"} else _number(source["source_perp_equity"])
            collateral = _number(source["source_spot_usdc"]) + perp_value - source_pnl
            source_equity = _number(source["source_spot_usdc"]) + perp_value + spot_value
            marked_equity = collateral + spot_value + marked_pnl
            equal(seed["cash"][account], collateral, "seed_collateral_mismatch")
            equal(source["base_collateral"], collateral, "source_collateral_mismatch")
            equal(source["source_equity"], source_equity, "source_equity_mismatch")
            equal(seed["baseline_equity"][account], marked_equity, "marked_baseline_mismatch")
            equal(marked_equity - source_equity, marked_pnl - source_pnl, "valuation_bridge_mismatch")
            reconciled[account] = {"source_equity": source_equity, "opening_equity": marked_equity,
                "source_perp_unrealized_pnl": source_pnl, "opening_mark_perp_unrealized_pnl": marked_pnl,
                "source_to_opening_valuation_difference": marked_equity - source_equity}
        equal(opening["cash"], sum(_number(value) for value in seed["cash"].values()), "pooled_opening_cash_mismatch")
        equal(opening["equity"], sum(_number(value) for value in seed["baseline_equity"].values()), "pooled_baseline_mismatch")
        return {"verified": True, "accounts": reconciled, "reason": "collateral_inventory_and_valuation_bridge_reconciled"}
    except (KeyError, TypeError, ValueError, OverflowError) as exc:
        return {"verified": False, "accounts": reconciled, "reason": str(exc)}


def build_comparison(paper, actual, *, max_skew_seconds=120):
    seed, latest = paper["seed"], paper["latest"]
    pooled = latest["state"]["pooled"]
    opening = paper["opening"]["state"]["pooled"]
    marks = actual["marks"]
    paper_equity = value_positions(pooled["cash"], pooled["positions"], marks)
    actual_equity = sum(row["common_mark_equity"] for row in actual["accounts"].values())
    duckets_equity = sum(row["duckets_equity"] for row in actual["accounts"].values())
    baseline = _number(opening["equity"])
    reconciliation = reconcile_mirror_baseline(seed, opening, set(actual["accounts"]))
    mirror_verified = reconciliation["verified"]
    flows_verified = all(row["flows"]["zero_external_flows_verified"] for row in actual["accounts"].values())
    skew = abs((_utc(actual["completed_at_utc"]) - _utc(latest["timestamp_utc"])).total_seconds())
    comparable = mirror_verified and flows_verified and skew <= max_skew_seconds and baseline > 0
    equity_edge = paper_equity - actual_equity
    by_coin = {}
    for row in paper["fill_groups"]["by_coin"]:
        coin = row["coin"]
        opening_pnl = sum(p["unrealized_pnl"] for p in opening["positions"] if p["coin"] == coin)
        current_pnl = sum(p["unrealized_pnl"] for p in pooled["positions"] if p["coin"] == coin)
        gross = row["realized_pnl"] + current_pnl - opening_pnl
        by_coin[coin] = {**row, "price_pnl_before_fees_funding": gross,
                         "pnl_after_fees_before_funding": gross - row["fees"]}
    hold_equity = value_positions(opening["cash"], opening["positions"], marks)
    return {"schema_version": 1, "created_at_utc": _now(),
        "experiment_id": paper["experiment"]["experiment_id"], "seed_at_utc": seed["timestamp_utc"],
        "experiment_sha256": paper["experiment_sha256"],
        "paper": {"observed_at_utc": latest["timestamp_utc"], "opening_equity": baseline,
                  "ledger_equity": pooled["equity"], "common_mark_equity": paper_equity,
                  "pnl_since_opening": pooled["total_pnl"], "fees": pooled["fees"],
                  "funding": pooled["funding"], "counts": paper["counts"],
                  "forecast_evidence": paper.get("forecast_evidence"),
                  "accounts": {name: {"opening_equity": seed["baseline_equity"][name],
                      "ledger_equity": row["equity"],
                      "common_mark_equity": value_positions(row["cash"], row["positions"], marks),
                      "pnl_since_opening": row["total_pnl"], "fees": row["fees"], "funding": row["funding"],
                      "net_internal_transfers": row["net_transfers"]}
                      for name, row in latest["state"].get("accounts", {}).items()},
                  "turnover": sum(row["turnover"] for row in paper["fill_groups"]["by_coin"]),
                  "by_coin": by_coin, "by_reason": paper["fill_groups"]["by_reason"]},
        "actual": {"duckets_equity": duckets_equity, "common_mark_equity": actual_equity,
                   "accounts": actual["accounts"], "started_at_utc": actual["started_at_utc"],
                   "completed_at_utc": actual["completed_at_utc"]},
        "comparison": {"raw_display_equity_edge": pooled["equity"] - duckets_equity,
                       "common_mark_equity_edge": equity_edge,
                       "performance_comparable": comparable, "mirror_baseline_verified": mirror_verified,
                       "mirror_baseline_reconciliation": reconciliation,
                       "zero_external_flows_verified": flows_verified, "observation_skew_seconds": skew,
                       "max_observation_skew_seconds": max_skew_seconds,
                       "paper_return_fraction": (paper_equity / baseline - 1) if comparable else None,
                       "actual_return_fraction": (actual_equity / baseline - 1) if comparable else None,
                       "excess_return_fraction": equity_edge / baseline if comparable else None,
                       "paper_beating_actual": equity_edge > 0 if comparable else None,
                       "unchanged_opening_inventory_equity_before_funding": hold_equity,
                       "paper_minus_unchanged_inventory_before_benchmark_funding": paper_equity - hold_equity},
        "common_marks": {"observed_at_utc": actual["mark_observed_at_utc"], "markets": marks},
        "source_reads": actual["source_reads"],
        "limitations": ["Account and quote reads are sequential, not an atomic exchange snapshot.",
                        "Common marks remove quote differences; inventories are observed at their recorded times.",
                        "Nonempty or capped non-funding history requires flow classification before ranking returns.",
                        "Paper funding is estimated; unchanged-inventory benchmark excludes funding and fees.",
                        "One short run and overlapping forecast outcomes do not establish future profitability."],
        "flow_api_documentation": "https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api/info-endpoint/perpetuals"}


def collect_comparison(data_root, *, client=None, accounts=None):
    paper = read_paper_snapshot(data_root)
    actual = read_actual_accounts(paper["seed"]["timestamp_utc"], client=client, accounts=accounts)
    current_hash = hashlib.sha256((Path(data_root) / "_paper/experiment.json").read_bytes()).hexdigest()
    if current_hash != paper["experiment_sha256"]:
        raise ValueError("Experiment changed while public accounts were being read.")
    return build_comparison(paper, actual)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-root", default="C:/DATASTORE/hyperliquid")
    parser.add_argument("--output", type=Path, required=True, help="New JSON artifact; existing files are never overwritten")
    args = parser.parse_args(argv)
    if args.output.exists():
        parser.error("--output must name a new file")
    result = collect_comparison(args.data_root)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("x", encoding="utf-8") as handle:
        json.dump(result, handle, indent=2, allow_nan=False)
        handle.write("\n")
    print(json.dumps({"output": str(args.output.resolve()), "experiment_id": result["experiment_id"],
                      "paper_equity": result["paper"]["ledger_equity"],
                      "actual_equity": result["actual"]["duckets_equity"],
                      "comparison": result["comparison"]}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
