from app.services.hyperliquid_paper_view import PaperViewSnapshot, SourceState
from app.services.hyperliquid_powder_view import project_powder


def test_empty_powder_never_uses_paper_balances():
    paper = PaperViewSnapshot(pooled={"equity": 10000}, accounts={"alex": {"equity": 10000}},
        fills=[{"fee": 100}], forecasts=[{"coin": "BTC"}],
        sources={"ledger": SourceState("Paper", "fresh"), "models": SourceState("Models", "fresh")})
    powder = project_powder({}, paper=paper, now=1000)
    assert powder.pooled == powder.accounts == {}
    assert powder.fills == []
    assert powder.forecasts == paper.forecasts
    assert "ledger" not in powder.sources
    assert powder.runtime["connected"] is False


def test_actual_equity_is_not_reported_as_profit_and_fee_tokens_preserved():
    report = {"status": "ready", "latest_observation": {"observed_at": 995,
        "accounts": {"alex": {"equity": 125, "available_cash": 50, "gross": 75,
            "positions": {"BTC": {"quantity": -.001, "mark": 75000, "entry_price": 80000, "kind": "perp"}}},
            "clearpond": {"equity": 300, "available_cash": 100, "gross": 200,
            "positions": {"HYPE": {"quantity": 5, "mark": 40, "entry_price": 40, "kind": "spot"}}}}},
        "metadata": {"baseline": {"equity": 10}}, "counts": {"pending": 1},
        "fills": [{"account": "clearpond", "symbol": "HYPE", "tid": 1, "sz": "1", "px": "40",
                   "side": "B", "fee": "0.001", "feeToken": "HYPE", "time": 999000}]}
    view = project_powder(report, now=1000)
    assert view.pooled == {"equity": 425, "gross_exposure": 275}
    assert view.performance == {}
    assert view.positions[0]["unrealized_pnl"] == 5
    assert view.positions[1]["avg_entry"] is None
    assert view.positions[1]["unrealized_pnl"] is None
    assert view.fills[0]["feeToken"] == "HYPE"
    assert view.fills[0]["quantity"] == 1
    assert view.warnings
    assert view.sources["powder_ledger"].state == "fresh"


def test_stale_and_missing_evidence_are_visible():
    assert project_powder({}, now=1000).sources["powder_ledger"].state == "missing"
    snapshot = project_powder({"latest_observation": {"observed_at": 1}}, now=1000)
    assert snapshot.sources["powder_ledger"].state == "stale"
    assert snapshot.warnings


def test_broker_normalized_sell_keeps_signed_quantity_and_fee_currency():
    snapshot = project_powder({"fills": [{"account": "clearpond", "symbol": "HYPE", "tid": "44",
        "quantity": -2, "price": 40, "fee": .07, "fee_token": "USDC", "time": 1000000,
        "raw": {"sz": "2", "side": "A", "feeToken": "USDC"}}]}, now=1001)
    assert snapshot.fills[0]["quantity"] == -2
    assert snapshot.fills[0]["feeToken"] == "USDC"


def test_workspace_reader_creates_no_missing_powder_files(tmp_path):
    from app.services.hyperliquid_powder_view import HyperliquidWorkspaceViewService
    snapshot = HyperliquidWorkspaceViewService(tmp_path).load_snapshot()
    assert not (tmp_path / "_powder").exists()
    assert snapshot.powder.runtime["connected"] is False
