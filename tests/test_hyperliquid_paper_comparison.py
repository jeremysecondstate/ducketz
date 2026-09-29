"""Accounting and fail-closed comparison checks using only local fixtures."""
from copy import deepcopy
import json

import pytest

from ml.hyperliquid_paper_comparison import (
    RecordingInfoClient, assess_flows, build_comparison, common_marks,
    normalize_account, read_paper_snapshot, reconcile_mirror_baseline, value_positions,
)


def test_unified_and_standard_valuation_do_not_double_count_perpetual_pnl():
    perp = {"assetPositions": [{"position": {"coin": "BTC", "szi": "-2",
            "entryPx": "100", "unrealizedPnl": "-20"}}],
            "marginSummary": {"accountValue": "180"}}
    spot = {"balances": [{"coin": "USDC", "total": "180"},
                         {"coin": "UBTC", "total": "0.5"}]}
    marks = {"perp:BTC": 110, "spot:BTC": 112}
    unified = normalize_account(perp, spot, "unifiedAccount")
    assert unified["base_collateral"] == 200
    assert unified["positions"][0]["quantity"] == -2
    assert value_positions(unified["base_collateral"], unified["positions"], marks) == 236
    standard = normalize_account(perp, spot, "default")
    assert value_positions(standard["base_collateral"], standard["positions"], marks) == 416
    with pytest.raises(KeyError):
        value_positions(200, unified["positions"], {"perp:BTC": 110})


def test_spot_pair_is_used_instead_of_naked_perp_symbol():
    meta = [{"tokens": [{"name": "USDC", "index": 0}, {"name": "UBTC", "index": 1}],
             "universe": [{"name": "@4", "tokens": [1, 0], "index": 4}]},
            [{"midPx": "110", "markPx": "111"}]]
    marks = common_marks({"BTC": "100", "@4": "112"}, meta)
    assert marks["perp:BTC"] == 100
    assert marks["spot:BTC"] == 112


def test_flow_history_never_calls_deposit_or_capped_response_zero_flow():
    assert assess_flows([], 100, 200)["zero_external_flows_verified"]
    deposit = {"time": 150, "delta": {"type": "deposit", "usdc": "200"}}
    populated = assess_flows([deposit], 100, 200)
    assert populated["range_complete"]
    assert not populated["zero_external_flows_verified"]
    assert populated["net_external_flow_usd"] is None
    assert not assess_flows([deposit] * 500, 100, 200)["range_complete"]
    assert not assess_flows([{"time": 250, "delta": {}}], 100, 200)["range_complete"]
    assert not assess_flows({}, 100, 200)["range_complete"]


def comparison_fixture():
    position = {"account": "alex", "coin": "BTC", "kind": "spot", "quantity": 1,
                "avg_entry": 100, "mark_price": 100, "unrealized_pnl": 0}
    opening = {"cash": 900, "positions": [position], "equity": 1000}
    # Paper sold its BTC, lost 1 in fees, and has no market risk at the new mark.
    latest = {"cash": 999, "positions": [], "equity": 999, "total_pnl": -1,
              "fees": 1, "funding": 0}
    paper = {"experiment": {"experiment_id": "test"}, "experiment_sha256": "hash",
             "seed": {"timestamp_utc": "2026-09-26T00:00:00+00:00",
                      "baseline_equity": {"alex": 1000},
                      "cash": {"alex": 900}, "positions": [deepcopy(position)], "marks": {"spot:BTC": 100},
                      "metadata": {"seed_mode": "mirror", "accounts": {"alex": {
                          "source_equity": 1000, "base_collateral": 900, "source_account_mode": "unifiedAccount",
                          "source_perp_unrealized_pnl": 0, "source_perp_equity": 0, "source_spot_usdc": 900}}}},
             "opening": {"state": {"pooled": opening}},
             "latest": {"timestamp_utc": "2026-09-28T00:00:00+00:00", "state": {"pooled": latest}},
             "counts": {"fills": 1}, "fill_groups": {
                 "by_coin": [{"coin": "BTC", "fills": 1, "realized_pnl": 0, "fees": 1, "turnover": 100}],
                 "by_reason": [{"reason": "signal_rebalance", "fills": 1, "fees": 1}]}}
    actual = {"accounts": {"alex": {"duckets_equity": 1020, "common_mark_equity": 1010,
                                   "flows": {"zero_external_flows_verified": True}}},
              "started_at_utc": "2026-09-28T00:00:05+00:00",
              "completed_at_utc": "2026-09-28T00:00:10+00:00",
              "mark_observed_at_utc": "2026-09-28T00:00:05+00:00",
              "marks": {"spot:BTC": 110}, "source_reads": []}
    return paper, actual


def test_common_marks_separate_display_gap_from_performance_and_hold_benchmark():
    paper, actual = comparison_fixture()
    result = build_comparison(paper, actual)
    c = result["comparison"]
    assert c["raw_display_equity_edge"] == -21
    assert c["common_mark_equity_edge"] == -11
    assert c["performance_comparable"]
    assert c["paper_beating_actual"] is False
    assert c["excess_return_fraction"] == pytest.approx(-0.011)
    assert c["unchanged_opening_inventory_equity_before_funding"] == 1010
    assert result["paper"]["by_coin"]["BTC"]["pnl_after_fees_before_funding"] == -1


def non_atomic_opening():
    # The exact legitimate valuation differences observed in the 2026-09-28 mirror.
    deltas = {"alex": 0.594665, "jeremy": -0.437885, "clearpond": 0.0}
    seed = {"metadata": {"seed_mode": "mirror", "accounts": {}}, "baseline_equity": {},
            "cash": {}, "positions": [], "marks": {"perp:BTC": 110, "spot:BTC": 112}}
    for account, delta in deltas.items():
        quantity = -2 if account == "alex" else 2 if account == "jeremy" else 0
        marked_pnl = quantity * 10
        reported_pnl = marked_pnl - delta
        source_cash = 1000 + reported_pnl
        seed["cash"][account] = 1000
        seed["baseline_equity"][account] = 1000 + marked_pnl
        seed["metadata"]["accounts"][account] = {
            "source_account_mode": "portfolioMargin" if account == "jeremy" else "unifiedAccount",
            "source_perp_equity": 123, "source_perp_unrealized_pnl": reported_pnl,
            "source_spot_usdc": source_cash, "base_collateral": 1000, "source_equity": source_cash}
        if quantity:
            seed["positions"].append({"account": account, "coin": "BTC", "kind": "perp",
                "quantity": quantity, "avg_entry": 100, "mark_price": 110,
                "source_unrealized_pnl": reported_pnl})
    opening = {"cash": 3000, "equity": 3000, "positions": deepcopy(seed["positions"])}
    return seed, opening, deltas


def test_realistic_non_atomic_quote_differences_reconcile_without_dollar_tolerance():
    seed, opening, deltas = non_atomic_opening()
    result = reconcile_mirror_baseline(seed, opening, set(deltas))
    assert result["verified"]
    for account, expected in deltas.items():
        assert result["accounts"][account]["source_to_opening_valuation_difference"] == pytest.approx(expected)


@pytest.mark.parametrize("field", ["cash", "baseline", "mark", "quantity", "source_pnl", "source_equity", "opening_quantity"])
def test_valuation_bridge_still_rejects_tampered_baseline_evidence(field):
    seed, opening, accounts = non_atomic_opening()
    if field == "cash":
        seed["cash"]["alex"] += 1
    elif field == "baseline":
        seed["baseline_equity"]["alex"] += 1
    elif field == "mark":
        seed["marks"]["perp:BTC"] += 1
    elif field == "quantity":
        seed["positions"][0]["quantity"] += 1
    elif field == "opening_quantity":
        opening["positions"][0]["quantity"] += 1
    else:
        key = "source_perp_unrealized_pnl" if field == "source_pnl" else "source_equity"
        seed["metadata"]["accounts"]["alex"][key] += 1
    assert not reconcile_mirror_baseline(seed, opening, set(accounts))["verified"]


@pytest.mark.parametrize("problem", ["flows", "stale", "baseline", "members"])
def test_no_return_ranking_when_comparison_is_ambiguous(problem):
    paper, actual = comparison_fixture()
    if problem == "flows":
        actual["accounts"]["alex"]["flows"]["zero_external_flows_verified"] = False
    elif problem == "stale":
        actual["completed_at_utc"] = "2026-09-28T00:10:00+00:00"
    elif problem == "baseline":
        paper["seed"]["metadata"]["accounts"]["alex"]["source_equity"] = 1200
    else:
        actual["accounts"]["jeremy"] = deepcopy(actual["accounts"]["alex"])
    c = build_comparison(paper, actual)["comparison"]
    assert not c["performance_comparable"]
    assert c["paper_beating_actual"] is None
    assert c["excess_return_fraction"] is None
    assert c["common_mark_equity_edge"] is not None


def test_excluded_run_is_rejected_before_any_ledger_access(tmp_path):
    (tmp_path / "_paper").mkdir()
    (tmp_path / "_operations").mkdir()
    experiment = {"experiment_id": "excluded", "seed_at_utc": "2026-01-01T00:00:00Z",
                  "analysis_eligible": True}
    (tmp_path / "_paper/experiment.json").write_text(json.dumps(experiment))
    (tmp_path / "_operations/excluded-paper-runs.json").write_text(
        json.dumps({"excluded_runs": [experiment]}))
    with pytest.raises(ValueError, match="ledger was not opened"):
        read_paper_snapshot(tmp_path)
    assert not (tmp_path / "_paper/ledger.sqlite3").exists()


def test_recorder_blocks_writes_redacts_wallets_and_preserves_source_timestamps():
    owner = "0x" + "1" * 40

    class Client:
        info_url = "https://example.invalid/info"

        def post_info(self, payload):
            return {"time": 1234567, "user": owner}

    client = RecordingInfoClient(Client())
    with pytest.raises(ValueError, match="public information"):
        client.post_info({"type": "order"})
    client.post_info({"type": "clearinghouseState", "user": owner})
    record = client.records[0]
    assert record["response"]["time"] == 1234567
    assert record["started_at_utc"] <= record["completed_at_utc"]
    assert owner not in json.dumps(record)
