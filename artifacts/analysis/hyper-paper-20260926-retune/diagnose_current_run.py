"""Read-only matched-mark accounting for one explicitly selected Paper ledger.

No historical/archived experiments are discovered or evaluated. A consistent
SQLite read transaction pins the ledger while the report is collected.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from collections import defaultdict
from pathlib import Path
import sqlite3


def diagnose(paper_root: Path, *, as_of: str | None = None) -> dict:
    experiment = json.loads((paper_root / "experiment.json").read_text())
    if experiment.get("analysis_eligible") is not True:
        raise ValueError("The explicitly selected experiment is not analysis eligible.")
    connection = sqlite3.connect(
        f"file:{(paper_root / 'ledger.sqlite3').as_posix()}?mode=ro", uri=True,
    )
    connection.row_factory = sqlite3.Row
    try:
        connection.execute("BEGIN")
        if as_of:
            row = connection.execute(
                "SELECT timestamp_utc,result_json,rowid FROM cycles WHERE timestamp_utc=? ORDER BY rowid DESC LIMIT 1",
                (as_of,),
            ).fetchone()
        else:
            row = connection.execute(
                "SELECT timestamp_utc,result_json,rowid FROM cycles ORDER BY rowid DESC LIMIT 1",
            ).fetchone()
        if row is None:
            raise ValueError("No matching committed cycle.")
        latest = json.loads(row["result_json"])
        seed = json.loads(connection.execute("SELECT details_json FROM seed").fetchone()[0])
        pooled = latest["state"]["pooled"]
        marks = {p["market"]: p["mark_price"] for p in pooled["positions"]}
        required_markets = {f"{p['kind']}:{p['coin']}" for p in seed["positions"]}
        missing = required_markets - marks.keys()
        if missing:
            raise ValueError(f"Latest cycle lacks opening-position marks: {sorted(missing)}")
        fills = [dict(r) for r in connection.execute(
            "SELECT fills.* FROM fills JOIN cycles USING(cycle_id) WHERE cycles.rowid<=? ORDER BY fills.rowid",
            (row["rowid"],),
        )]
    finally:
        connection.close()

    held = defaultdict(float)
    for position in seed["positions"]:
        market = f"{position['kind']}:{position['coin']}"
        held[market] += position["quantity"] * (marks[market] - position["mark_price"])
    groups = {}
    for fill in fills:
        market = f"{fill['kind']}:{fill['coin']}"
        if market not in marks:
            raise ValueError(f"Latest cycle lacks traded-market mark: {market}")
        group = groups.setdefault(market, dict(fills=0, turnover=0.0, fees=0.0, trade_contribution_to_hold=0.0))
        group["fills"] += 1
        group["turnover"] += fill["notional"]
        group["fees"] += fill["fee"]
        group["trade_contribution_to_hold"] += fill["quantity"] * (marks[market] - fill["price"])
        details = json.loads(fill.pop("details_json"))
        fill.update({name: details.get(name) for name in (
            "p_not_down", "current_notional", "target_notional", "quantity_after", "policy_id",
        )})
    held_pnl = sum(held.values())
    residual = pooled["total_pnl"] - pooled["funding"] - held_pnl
    attribution = sum(g["trade_contribution_to_hold"] - g["fees"] for g in groups.values())
    if abs(residual - attribution) >= 1e-7:
        raise ValueError(f"Trade attribution fails accounting identity: {residual-attribution}")
    return dict(
        experiment_id=experiment["experiment_id"],
        as_of_utc=row["timestamp_utc"],
        opening_equity=pooled["initial_equity"],
        paper_equity=pooled["equity"],
        paper_pnl=pooled["total_pnl"],
        paper_fees=pooled["fees"],
        paper_funding=pooled["funding"],
        paper_pnl_before_fees_funding=pooled["total_pnl"] + pooled["fees"] - pooled["funding"],
        held_opening_pnl_excluding_funding=held_pnl,
        held_opening_equity_excluding_funding=pooled["initial_equity"] + held_pnl,
        paper_vs_hold_excluding_funding=residual,
        held_pnl_by_market=dict(held),
        marks=marks,
        fills_by_market=groups,
        fills=fills,
        source_cycle=latest,
        source_seed=seed,
        cycle_json_sha256=hashlib.sha256(row["result_json"].encode()).hexdigest(),
        limitations=[
            "Opening-hold counterfactual, not later actual-account equity or actual trades.",
            "Both return comparisons exclude funding; no hypothetical holding funding is invented.",
            "Marks share one committed Paper observation, not an atomic exchange snapshot.",
            "Three-hour sample and prior policy phase changes do not establish model quality or future profitability.",
        ],
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--paper-root", type=Path, required=True)
    parser.add_argument("--as-of")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    result = diagnose(args.paper_root.resolve(), as_of=args.as_of)
    args.output.write_text(json.dumps(result, indent=2), encoding="utf-8")
    print(json.dumps({k: v for k, v in result.items() if k not in {
        "fills", "source_cycle", "source_seed", "fills_by_market", "marks",
    }}, indent=2))
