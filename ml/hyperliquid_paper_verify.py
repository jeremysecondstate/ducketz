"""Independently verify Paper via read-only SQLite; write only a new labeled report.

Opening: python verify.py --stage opening --label opening
Trading: python verify.py --stage trading --label first-cycle --opening-report opening.json
No runtime/ledger constructors or external account requests are used.
"""
import argparse
import hashlib
import json
import math
import re
import sqlite3
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"),
                                     allow_nan=False).encode()).hexdigest()


def close(actual, expected, context):
    assert math.isfinite(actual) and math.isfinite(expected), context
    assert math.isclose(actual, expected, rel_tol=1e-11, abs_tol=1e-7), (context, actual, expected)


def write_new(path, record):
    # Refuse to overwrite prior evidence, even when an operator repeats a label.
    with path.open("x", encoding="utf-8") as stream:
        stream.write(json.dumps(record, indent=2, allow_nan=False) + "\n")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--stage", choices=("opening", "trading"), required=True)
    parser.add_argument("--label", required=True)
    parser.add_argument("--paper-dir", type=Path, default=Path("C:/DATASTORE/hyperliquid/_paper"))
    parser.add_argument("--opening-report", type=Path)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()
    assert re.fullmatch(r"[a-zA-Z0-9_-]+", args.label), "Use a simple unique report label"
    args.output_dir.mkdir(parents=True, exist_ok=True)
    output = args.output_dir.resolve() / (args.label + ".json")
    assert not output.exists(), f"Evidence already exists: {output}"
    if args.stage == "trading":
        assert args.opening_report, "Trading verification requires its immutable opening report"
        prior_path = args.opening_report
        if not prior_path.is_absolute():
            prior_path = args.output_dir.resolve() / prior_path
        prior = json.loads(prior_path.read_text(encoding="utf-8"))
        assert prior["stage"] == "opening" and prior["result"] == "passed"
    else:
        prior = None

    paper = args.paper_dir.resolve(strict=True)
    database = paper / "ledger.sqlite3"
    assert database.is_file(), "Missing ledger; this verifier never initializes it"
    conn = sqlite3.connect(database.as_uri() + "?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only=ON")
    conn.execute("BEGIN")
    assert conn.execute("PRAGMA quick_check").fetchone()[0] == "ok"
    seed = json.loads(conn.execute("SELECT details_json FROM seed WHERE singleton=1").fetchone()[0])
    assert seed["metadata"]["seed_mode"] == "mirror"
    assert seed["metadata"]["inherited_stop_reference"] == "opening_mark"
    assert not seed["metadata"]["live_updates_after_seed"]
    # Keep the separately excluded predecessor out of all analysis/recovery.
    assert not seed["timestamp_utc"].startswith("2026-09-26T10:35:59"), "Excluded predecessor"
    cycles = [dict(row) for row in conn.execute("SELECT * FROM cycles ORDER BY rowid")]
    assert cycles and cycles[0]["cycle_id"] == "opening"
    opening_result = json.loads(cycles[0]["result_json"])
    opening = opening_result["state"]
    assert opening_result["observation_kind"] == "opening_snapshot"
    assert not opening_result["fills"] and not opening_result["transfers"] and not opening_result["funding"]
    initial_rows = [dict(row) for row in conn.execute("SELECT * FROM initial_positions ORDER BY account,coin,kind")]
    snapshot_bytes = (paper / "opening_snapshot.json").read_bytes()
    snapshot = json.loads(snapshot_bytes)
    assert snapshot == seed, "Published opening must equal the immutable SQLite seed"
    hashes = {"seed_sha256": digest(seed), "opening_cycle_sha256": digest(opening_result),
              "initial_positions_sha256": digest(initial_rows),
              "opening_snapshot_sha256": hashlib.sha256(snapshot_bytes).hexdigest()}
    if prior:
        assert hashes == prior["immutable_opening_hashes"], "Opening changed after preparation"
        assert seed["timestamp_utc"] == prior["seed_at_utc"]

    tables = ("cycles", "equity", "initial_positions", "fills", "decisions", "transfers", "funding")
    counts = {table: conn.execute("SELECT COUNT(*) FROM " + table).fetchone()[0] for table in tables}
    assert counts["initial_positions"] == len(seed["positions"])
    assert counts["equity"] == counts["cycles"] * 4
    journals = {}
    for table in ("fills", "transfers", "funding", "equity"):
        grouped = defaultdict(list)
        for row in conn.execute("SELECT * FROM " + table + " ORDER BY rowid"):
            grouped[row["cycle_id"]].append(dict(row))
        assert set(grouped).issubset({cycle["cycle_id"] for cycle in cycles})
        journals[table] = grouped

    cash = dict(seed["cash"])
    assert set(cash) == {"alex", "jeremy", "clearpond"}
    fees = {account: 0.0 for account in cash}
    funding, transfers, realized_totals = dict(fees), dict(fees), dict(fees)
    inventory = {}
    source_reconciliation = {}
    initial = {(row["account"], row["coin"], row["kind"]): row for row in initial_rows}
    for position in seed["positions"]:
        key = position["account"], position["coin"], position["kind"]
        assert key not in inventory
        account, coin, kind = key
        quantity = position["quantity"]
        assert quantity != 0
        assert quantity < 0 if account == "alex" and kind == "perp" else quantity > 0
        assert kind != "perp" or account != "clearpond"
        mark = seed["marks"][kind + ":" + coin]
        close(position["mark_price"], mark, "seed mark")
        close(position["risk_reference_price"], mark, "fresh stop reference")
        assert position["risk_reference_source"] == "opening_mark"
        if kind == "perp":
            assert position["entry_source"] == "historical_exchange_entry"
            close(position["avg_entry"], position["average_entry"], "retained source entry")
            assert math.isfinite(position["source_unrealized_pnl"])
        else:
            assert position["entry_source"] == "opening_mark_not_historical_cost"
            close(position["avg_entry"], mark, "spot opening basis")
            assert position["passive"] == (account != "clearpond")
        retained = json.loads(initial[key]["details_json"])
        assert retained == position, "Initial source-derived inventory disagrees with seed"
        for field in ("quantity", "avg_entry", "mark_price"):
            close(initial[key][field], position[field], "initial row " + field)
        inventory[key] = {"quantity": quantity, "entry": position["avg_entry"],
                          "risk": position["risk_reference_price"], "risk_source": "opening_mark"}
    for account, source in seed["metadata"]["accounts"].items():
        unified = source["source_account_mode"] in {"unifiedAccount", "portfolioMargin"}
        expected_cash = source["source_spot_usdc"] - source["source_perp_unrealized_pnl"]
        if not unified:
            expected_cash += source["source_perp_equity"]
        close(cash[account], expected_cash, account + " source collateral")
        positions = [p for p in seed["positions"] if p["account"] == account]
        source_upnl = sum(p["source_unrealized_pnl"] for p in positions if p["kind"] == "perp")
        close(source_upnl, source["source_perp_unrealized_pnl"], account + " reported P/L sum")
        upnl = sum(p["quantity"] * (p["mark_price"] - p["avg_entry"]) for p in positions if p["kind"] == "perp")
        spot_value = sum(p["quantity"] * p["mark_price"] for p in positions if p["kind"] == "spot")
        source_equity = source["source_spot_usdc"] + spot_value + (0 if unified else source["source_perp_equity"])
        close(source["source_equity"], source_equity, account + " source equity")
        delta = seed["baseline_equity"][account] - source["source_equity"]
        close(delta, upnl - source_upnl, account + " non-atomic quote difference")
        for read in source["read_observations"].values():
            assert read["started_at_utc"] <= read["completed_at_utc"]
        source_reconciliation[account] = {**source, "opening_equity": seed["baseline_equity"][account],
                                          "source_to_opening_valuation_difference": delta}
    assert opening["pooled"]["total_pnl"] == opening["pooled"]["fees"] == opening["pooled"]["funding"] == 0
    fill_checks = []
    for cycle in cycles:
        cycle_id = cycle["cycle_id"]
        for row in journals["transfers"][cycle_id]:
            cash[row["from_account"]] -= row["amount"]
            cash[row["to_account"]] += row["amount"]
            transfers[row["from_account"]] -= row["amount"]
            transfers[row["to_account"]] += row["amount"]
        for row in journals["funding"][cycle_id]:
            cash[row["account"]] += row["amount"]
            funding[row["account"]] += row["amount"]
        for row in journals["fills"][cycle_id]:
            detail = json.loads(row["details_json"])
            policy_id = detail["policy_id"]
            policy = json.loads((paper / "policies" / (policy_id + ".json")).read_text())
            fee_rate = policy["spot_fee_rate" if row["kind"] == "spot" else "perp_fee_rate"]
            close(detail["fee_rate"], fee_rate, "versioned fee rate")
            close(detail["extra_slippage_bps"], policy["slippage_bps"], "versioned slippage")
            if policy["slippage_bps"] == 0:
                close(row["price"], detail["raw_book_vwap"], "executed book VWAP")
            if row["reason"] == "signal_rebalance" and policy["require_qualified_forecasts"]:
                assert detail["qualified"] is True
            key = row["account"], row["coin"], row["kind"]
            before = dict(inventory.get(key, {"quantity": 0.0, "entry": 0.0, "risk": 0.0}))
            old, quantity, price = before["quantity"], row["quantity"], row["price"]
            new = old + quantity
            if abs(new) <= 1e-9:
                new = 0.0
            assert new <= 0 if row["account"] == "alex" else new >= 0
            realized = 0.0
            if old == 0 or old * quantity > 0:
                entry = (abs(old) * before["entry"] + abs(quantity) * price) / abs(new)
                risk = (abs(old) * before["risk"] + abs(quantity) * price) / abs(new)
                risk_source = "weighted_entry" if old else "fill_price"
            else:
                realized = min(abs(old), abs(quantity)) * (price - before["entry"]) * math.copysign(1, old)
                entry, risk, risk_source = before["entry"], before["risk"], before["risk_source"]
            fee = abs(quantity) * price * fee_rate
            close(row["fee"], fee, "fill fee")
            close(row["notional"], abs(quantity) * price, "fill notional")
            close(row["realized_pnl"], realized, "realized historical P/L")
            cash_delta = (-quantity * price if row["kind"] == "spot" else realized) - fee
            cash[row["account"]] += cash_delta
            fees[row["account"]] += fee
            realized_totals[row["account"]] += realized
            if new:
                inventory[key] = {"quantity": new, "entry": entry, "risk": risk, "risk_source": risk_source}
            else:
                inventory.pop(key, None)
            close(detail["quantity_after"], new, "post-fill quantity")
            fill_checks.append({"fill_id": row["fill_id"], "timestamp_utc": row["timestamp_utc"],
                                "account": row["account"], "coin": row["coin"], "kind": row["kind"],
                                "reason": row["reason"], "policy_id": policy_id, "price": price, "fee": fee,
                                "quantity_before": old, "quantity_delta": quantity, "quantity_after": new,
                                "cash_delta": cash_delta, "realized_pnl": realized})
        state = json.loads(cycle["result_json"])["state"]
        for account, values in state["accounts"].items():
            expected_fields = {"cash": cash[account], "fees": fees[account], "funding": funding[account],
                               "net_transfers": transfers[account], "realized_pnl": realized_totals[account]}
            for field, expected in expected_fields.items():
                close(values[field], expected, account + " " + field + " at " + cycle_id)
            equity, unrealized, gross = cash[account], 0.0, 0.0
            current = {(p["account"], p["coin"], p["kind"]): p for p in values["positions"]}
            assert set(current) == {key for key in inventory if key[0] == account}
            for key, position in current.items():
                expected = inventory[key]
                for actual_key, expected_key in (("quantity", "quantity"), ("avg_entry", "entry"), ("risk_reference_price", "risk")):
                    close(position[actual_key], expected[expected_key], "retained position " + actual_key)
                assert position["risk_reference_source"] == expected["risk_source"]
                upnl = expected["quantity"] * (position["mark_price"] - expected["entry"])
                unrealized += upnl
                gross += abs(expected["quantity"]) * position["mark_price"]
                equity += expected["quantity"] * position["mark_price"] if key[2] == "spot" else upnl
            close(values["equity"], equity, account + " marked equity")
            close(values["unrealized_pnl"], unrealized, account + " unrealized P/L")
            close(values["gross_exposure"], gross, account + " gross exposure")
            close(values["total_pnl"], equity - seed["baseline_equity"][account] - transfers[account], account + " experiment P/L")
        for field in ("equity", "cash", "fees", "funding", "total_pnl", "net_transfers", "gross_exposure"):
            close(state["pooled"][field], sum(a[field] for a in state["accounts"].values()), "pool " + field)
        close(state["pooled"]["net_transfers"], 0, "transfers conserve pool")
        for row in journals["equity"][cycle_id]:
            expected = state["pooled"] if row["account"] == "pooled" else state["accounts"][row["account"]]
            for field in ("cash", "equity", "fees", "funding", "total_pnl", "gross_exposure"):
                close(row[field], expected[field], "equity journal " + field)
    for row in conn.execute("SELECT * FROM accounts"):
        for field in ("cash", "fees", "funding", "net_transfers", "realized_pnl", "initial_equity"):
            close(row[field], state["accounts"][row["account"]][field], "current account table " + field)
    actual_positions = {(row["account"], row["coin"], row["kind"]): dict(row) for row in conn.execute("SELECT * FROM positions")}
    assert set(actual_positions) == set(inventory)
    for key, row in actual_positions.items():
        for field, replay_field in (("quantity", "quantity"), ("avg_entry", "entry"), ("risk_reference_price", "risk")):
            close(row[field], inventory[key][replay_field], "current positions table " + field)
    conn.close()
    if args.stage == "opening":
        assert counts["cycles"] == 1 and counts["equity"] == 4
        assert all(counts[table] == 0 for table in ("fills", "decisions", "transfers", "funding"))
    else:
        assert counts["cycles"] > 1, "No post-opening committed cycle yet"
    record = {"verified_at_utc": datetime.now(timezone.utc).isoformat(), "stage": args.stage, "result": "passed",
              "paper_directory": str(paper), "seed_at_utc": seed["timestamp_utc"],
              "opening_equity": opening["pooled"]["equity"], "opening_pnl": 0.0, "opening_fees": 0.0,
              "immutable_opening_hashes": hashes, "counts": counts,
              "source_reconciliation": source_reconciliation, "opening_inventory": seed["positions"],
              "quote_observation": seed["metadata"]["quote_observation"],
              "latest_committed_at_utc": cycles[-1]["timestamp_utc"], "latest_portfolio": state,
              "fill_accounting_checks": fill_checks,
              "verification_limits": ["Account reads and opening quotes are sequential, not atomic.",
                  "Raw account response quantities are not retained separately from normalized seed inventory; quantity/basis checks compare all persisted opening representations.",
                  "Replay checks cash effects of recorded funding; it does not independently fetch settlement rates or oracle prices."]}
    write_new(output, record)
    print(json.dumps({"result": "passed", "report": str(output), "stage": args.stage,
                      "seed_at_utc": seed["timestamp_utc"], "opening_equity": record["opening_equity"],
                      "counts": counts, "latest_at_utc": record["latest_committed_at_utc"],
                      "latest_equity": state["pooled"]["equity"], "latest_pnl": state["pooled"]["total_pnl"]}, indent=2))


if __name__ == "__main__":
    main()
