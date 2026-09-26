import json

import pandas as pd
import pytest

from ml.hyperliquid_paper_ledger import PaperLedger


NOW = "2026-09-25T12:00:00+00:00"
MARKS = {"perp:BTC": 100.0, "spot:BTC": 100.0, "spot:HYPE": 20.0}


def order(account="jeremy", quantity=10, price=100, fee_rate=0, **extra):
    return {"account": account, "coin": "BTC", "kind": "spot" if account == "clearpond" else "perp",
            "quantity": quantity, "price": price, "fee_rate": fee_rate,
            "forecast_id": "forecast-1", "model_id": "model-1", "reason": "paper signal", **extra}


@pytest.fixture
def ledger(tmp_path):
    with PaperLedger(tmp_path / "paper.sqlite") as value:
        yield value


def cycle(ledger, identity, orders=(), **kwargs):
    return ledger.execute_cycle(identity, NOW, kwargs.pop("marks", MARKS), orders, **kwargs)


def test_long_add_partial_reduce_and_fee_accounting(ledger):
    cycle(ledger, "one", [order(quantity=10, fee_rate=0.001)])
    cycle(ledger, "two", [order(quantity=10, price=120)], marks={**MARKS, "perp:BTC": 120})
    state = ledger.state({**MARKS, "perp:BTC": 120})["accounts"]["jeremy"]
    assert state["positions"][0]["avg_entry"] == 110
    assert state["cash"] == 9999
    reduced = cycle(ledger, "three", [order(quantity=-5, price=130, fee_rate=0.001)], marks={**MARKS, "perp:BTC": 130})
    account = reduced["state"]["accounts"]["jeremy"]
    assert account["cash"] == pytest.approx(10098.35)
    assert account["realized_pnl"] == 100
    assert account["positions"][0]["quantity"] == 15
    assert account["positions"][0]["avg_entry"] == 110
    assert account["total_pnl"] == pytest.approx(398.35)


def test_short_reduce_realizes_profit_and_never_credits_sale_proceeds(ledger):
    cycle(ledger, "open", [order(account="alex", quantity=-10)])
    assert ledger.state(MARKS)["accounts"]["alex"]["cash"] == 10000
    closed = cycle(ledger, "close", [order(account="alex", quantity=10, price=80)], marks={**MARKS, "perp:BTC": 80})
    assert closed["state"]["accounts"]["alex"]["cash"] == 10200
    assert closed["state"]["accounts"]["alex"]["positions"] == []


def test_spot_cash_and_realized_profit_are_not_double_counted(ledger):
    cycle(ledger, "buy", [order(account="clearpond", quantity=10, fee_rate=0.001)])
    account = ledger.state(MARKS)["accounts"]["clearpond"]
    assert account["cash"] == 8999
    assert account["equity"] == 9999
    sold = cycle(ledger, "sell", [order(account="clearpond", quantity=-10, price=120, fee_rate=0.001)], marks={**MARKS, "spot:BTC": 120})
    account = sold["state"]["accounts"]["clearpond"]
    assert account["cash"] == pytest.approx(10197.8)
    assert account["realized_pnl"] == 200
    assert account["total_pnl"] == pytest.approx(197.8)


@pytest.mark.parametrize("bad_order", [order(account="alex", quantity=1), order(account="jeremy", quantity=-1),
                                         order(account="clearpond", quantity=-1), order(kind="spot"),
                                         order(account="clearpond", kind="perp")])
def test_role_restrictions_and_no_short_spot(ledger, bad_order):
    with pytest.raises(ValueError):
        cycle(ledger, "bad", [bad_order])
    assert ledger.history("fills") == []


def test_transaction_rolls_back_fills_transfers_and_decisions(ledger):
    with pytest.raises(ValueError):
        cycle(ledger, "bad", [order(), order(account="alex", quantity=1)],
              transfers=[{"from_account": "clearpond", "to_account": "jeremy", "amount": 100}],
              decisions=[{"action": "skip", "reason": "example"}])
    assert ledger.state(MARKS)["pooled"]["cash"] == 30000
    for table in ("fills", "transfers", "decisions", "cycles", "events", "equity"):
        assert ledger.history(table) == []


def test_persistent_cycle_identity_prevents_duplicate_execution(tmp_path):
    path = tmp_path / "paper.sqlite"
    with PaperLedger(path) as ledger:
        original = cycle(ledger, "same", [order(fee_rate=0.001)])
    with PaperLedger(path) as ledger:
        result = cycle(ledger, "same", [order(quantity=50)])
        assert result == {**original, "duplicate": True}
        assert len(ledger.history("fills")) == 1
        assert ledger.state(MARKS)["accounts"]["jeremy"]["positions"][0]["quantity"] == 10


def test_virtual_transfer_is_zero_sum_and_does_not_change_performance(ledger):
    result = cycle(ledger, "transfer", transfers=[{"from_account": "clearpond", "to_account": "alex", "amount": 500}])
    assert result["state"]["pooled"]["cash"] == 30000
    assert result["state"]["pooled"]["total_pnl"] == 0
    assert result["state"]["accounts"]["alex"]["cash"] == 10500
    assert result["state"]["accounts"]["alex"]["total_pnl"] == 0
    assert result["state"]["accounts"]["clearpond"]["total_pnl"] == 0


def test_cannot_transfer_committed_collateral_or_unrealized_profit(ledger):
    cycle(ledger, "open", [order(quantity=90)])
    with pytest.raises(ValueError, match="free cash"):
        cycle(ledger, "transfer", transfers=[{"from_account": "jeremy", "to_account": "alex", "amount": 1001}])
    profitable = {**MARKS, "perp:BTC": 200}
    assert ledger.state(profitable)["accounts"]["jeremy"]["free_cash"] == 1000


def test_new_position_must_obey_gross_cap_and_spot_cannot_borrow(ledger):
    with pytest.raises(ValueError, match="collateral"):
        cycle(ledger, "leveraged", [order(quantity=101)])
    with pytest.raises(ValueError, match="borrow"):
        cycle(ledger, "borrow", [order(account="clearpond", quantity=101)])


def test_funding_signed_cash_and_persistent_identity(ledger):
    cycle(ledger, "open", [order(quantity=10), order(account="alex", quantity=-10)])
    result = cycle(ledger, "fund", funding=[{"account": "jeremy", "coin": "BTC", "rate": 0.001},
                                             {"account": "alex", "coin": "BTC", "rate": 0.001}])
    assert result["state"]["accounts"]["jeremy"]["cash"] == 9999
    assert result["state"]["accounts"]["alex"]["cash"] == 10001
    cycle(ledger, "retry-new-cycle", funding=[{"account": "jeremy", "coin": "BTC", "rate": 0.002}])
    assert ledger.state(MARKS)["accounts"]["jeremy"]["cash"] == 9999
    assert len(ledger.history("funding")) == 2


def test_late_known_funding_can_settle_after_position_was_closed(ledger):
    cycle(ledger, "open", [order()])
    cycle(ledger, "close", [order(quantity=-10)])
    result = cycle(ledger, "late", funding=[{"funding_id": "historical-hour", "account": "jeremy", "coin": "BTC", "amount": -3}])
    assert result["state"]["accounts"]["jeremy"]["cash"] == 9997
    assert result["state"]["accounts"]["jeremy"]["positions"] == []


def test_mirrored_seed_baselines_current_equity_preserving_original_entry(tmp_path):
    cash = {"alex": 1000, "jeremy": 1000, "clearpond": 1000}
    positions = [{"account": "jeremy", "coin": "BTC", "kind": "perp", "quantity": 20, "average_entry": 90},
                 {"account": "clearpond", "coin": "BTC", "kind": "spot", "quantity": 2, "average_entry": 80},
                 {"account": "alex", "coin": "HYPE", "kind": "spot", "quantity": 0.001, "average_entry": 20}]
    with PaperLedger(tmp_path / "mirror.sqlite", cash, initial_positions=positions, initial_marks=MARKS, now=NOW, metadata={"mode": "mirror"}) as ledger:
        state = ledger.state(MARKS)
        assert state["accounts"]["jeremy"]["initial_equity"] == 1200
        assert state["accounts"]["jeremy"]["unrealized_pnl"] == 200
        assert state["pooled"]["total_pnl"] == 0
        assert state["accounts"]["alex"]["positions"][0]["passive"] is True
        assert len(ledger.history("initial_positions")) == 3
        assert ledger.history("fills") == []
        assert ledger.seed()["metadata"] == {"mode": "mirror"}
        reduced = cycle(ledger, "cut", [order(quantity=-1)])
        assert reduced["state"]["accounts"]["jeremy"]["gross_exposure"] == 1900
        assert reduced["state"]["accounts"]["jeremy"]["equity"] == 1200
        assert reduced["state"]["accounts"]["jeremy"]["total_pnl"] == 0
        with pytest.raises(ValueError, match="collateral"):
            cycle(ledger, "worsen", [order(quantity=1)])
        with pytest.raises(ValueError, match="role"):
            cycle(ledger, "dust-trade", [order(account="alex", coin="HYPE", kind="spot", quantity=-0.001, price=20)])


def test_price_moves_may_breach_limits_but_reducing_orders_still_work(tmp_path):
    with PaperLedger(tmp_path / "short.sqlite", {"alex": 1000, "jeremy": 1000, "clearpond": 1000}) as ledger:
        cycle(ledger, "open", [order(account="alex", quantity=-9)])
        higher = {**MARKS, "perp:BTC": 150}
        cycle(ledger, "mark", marks=higher)
        reduced = cycle(ledger, "reduce", [order(account="alex", quantity=1, price=150)], marks=higher)
        assert reduced["state"]["accounts"]["alex"]["gross_exposure"] == 1200
        assert reduced["state"]["accounts"]["alex"]["equity"] == 550


def test_exports_are_inspectable_and_include_skip_decisions(ledger, tmp_path):
    cycle(ledger, "one", [order()], decisions=[{"account": "alex", "coin": "BTC", "action": "skip", "reason": "no short signal"}])
    paths = ledger.export(tmp_path / "exports")
    assert len(pd.read_parquet(paths["fills"])) == 1
    assert len(pd.read_parquet(paths["equity"])) == 4
    assert pd.read_parquet(paths["decisions"])["reason"].tolist() == ["no short signal"]
    assert json.loads(ledger.history("fills")[0]["details_json"])["forecast_id"] == "forecast-1"


@pytest.mark.parametrize("value", [float("nan"), float("inf"), -float("inf"), True])
def test_all_numeric_inputs_must_be_finite(ledger, value):
    with pytest.raises(ValueError):
        cycle(ledger, "bad", [order(quantity=value)])
    assert ledger.history("cycles") == []


def test_missing_held_mark_rolls_back_and_timestamp_requires_timezone(ledger):
    with pytest.raises(ValueError, match="Missing mark"):
        cycle(ledger, "bad", [order()], marks={})
    with pytest.raises(ValueError, match="timezone"):
        ledger.execute_cycle("time", "2026-09-25T12:00:00", MARKS, [])
    assert ledger.history("fills") == []


def test_reopen_never_resets_seed_or_positions(tmp_path):
    path = tmp_path / "paper.sqlite"
    with PaperLedger(path) as ledger:
        cycle(ledger, "open", [order()])
        original_seed = ledger.seed()
    with PaperLedger(path) as ledger:
        assert ledger.seed() == original_seed
        assert len(ledger.state(MARKS)["pooled"]["positions"]) == 1
    with pytest.raises(ValueError, match="different initial cash"):
        PaperLedger(path, {"alex": 5, "jeremy": 5, "clearpond": 5})


def test_failed_opening_seed_cannot_turn_into_default_balances_on_resume(tmp_path):
    path = tmp_path / "incomplete.sqlite"
    with pytest.raises(KeyError):
        PaperLedger(path, initial_positions=[{"account": "jeremy", "coin": "BTC", "kind": "perp", "quantity": 1, "average_entry": 100}])
    assert path.exists()
    with pytest.raises(ValueError, match="no completed opening seed"):
        PaperLedger(path, open_existing=True)


def test_open_existing_requires_persisted_seed_and_preserves_it(tmp_path):
    path = tmp_path / "complete.sqlite"
    with PaperLedger(path) as ledger:
        seed = ledger.seed()
    with PaperLedger(path, open_existing=True) as ledger:
        assert ledger.seed() == seed


def test_inventory_includes_new_markets_until_the_position_is_closed(ledger):
    marks = {**MARKS, "perp:ETH": 20}
    assert ledger.inventory() == []
    cycle(ledger, "open-new", [order(coin="ETH", quantity=3, price=20)], marks=marks)
    assert ledger.inventory() == [{"account": "jeremy", "coin": "ETH", "kind": "perp", "quantity": 3.0, "avg_entry": 20.0}]
    cycle(ledger, "reduce-new", [order(coin="ETH", quantity=-1, price=20)], marks=marks)
    assert ledger.inventory()[0]["quantity"] == 2
    cycle(ledger, "close-new", [order(coin="ETH", quantity=-2, price=20)], marks=marks)
    assert ledger.inventory() == []


def test_has_cycle_uses_persistent_identity_and_excludes_rolled_back_cycles(tmp_path):
    path = tmp_path / "cycles.sqlite"
    with PaperLedger(path) as ledger:
        assert not ledger.has_cycle("completed")
        cycle(ledger, "completed")
        assert ledger.has_cycle("completed")
        with pytest.raises(ValueError):
            cycle(ledger, "rolled-back", [order(account="alex", quantity=1)])
        assert not ledger.has_cycle("rolled-back")
    with PaperLedger(path, open_existing=True) as ledger:
        assert ledger.has_cycle("completed")
        assert not ledger.has_cycle("rolled-back")
        with pytest.raises(ValueError):
            ledger.has_cycle("")
