"""Powder journal tests use temporary SQLite files and no exchange clients."""
import hashlib
import json
import re
import sqlite3

import pytest

from ml.hyperliquid_powder_ledger import PowderLedger, read_status


BINDING = {"mode": "powder", "policy_id": "test-policy", "accounts": {"alex": "public-owner"}}
BASELINE = {"observed_at": 1000, "accounts": {"alex": {"equity": 1000}}, "equity": 1000}
REQUEST = {"account": "alex", "coin": "BTC", "symbol": "BTC", "is_buy": False,
           "size": 2, "limit_price": 100, "reduce_only": False, "tif": "Ioc"}


@pytest.fixture
def ledger(tmp_path):
    with PowderLedger(tmp_path / "powder.sqlite3", clock=lambda: 1000.0) as instance:
        instance.activate(BINDING, BASELINE)
        yield instance


def prepare(ledger, **overrides):
    return ledger.prepare({**REQUEST, **overrides}, "signal_rebalance", {"forecast_id": "same-forecast"})


def fill(tid=10, quantity=-2, **overrides):
    return {"account": "alex", "symbol": "BTC", "coin": "BTC", "tid": tid, "oid": 77,
            "quantity": quantity, "price": 100, "fee": .04, "fee_token": "USDC", "time": 1000000,
            **overrides}


def test_activation_is_pinned_and_baseline_is_adopted_once(ledger):
    ledger.activate(BINDING, {"equity": 5000})
    assert ledger.metadata() == {"binding": BINDING, "baseline": BASELINE}
    with pytest.raises(ValueError, match="binding changed"):
        ledger.activate({**BINDING, "policy_id": "changed"}, BASELINE)
    assert ledger.metadata()["baseline"] == BASELINE


def test_client_ids_are_unique_durable_sequences_not_forecast_reuse(ledger):
    first = prepare(ledger)
    second = prepare(ledger)
    assert first["request"] == second["request"]
    assert first["cloid"] != second["cloid"]
    assert re.fullmatch("0x[0-9a-f]{32}", first["cloid"])
    assert first["created_at"] == 1000
    with PowderLedger(ledger.path) as reopened:
        assert reopened.get_intent(first["id"])["cloid"] == first["cloid"]
        third = prepare(reopened)
    assert third["cloid"] not in {first["cloid"], second["cloid"]}


def test_submit_boundary_survives_crash_and_unknown_cannot_resubmit(ledger):
    intent = prepare(ledger)
    ledger.mark_submitting(intent["id"])
    with PowderLedger(ledger.path) as reopened:
        assert reopened.pending()[0]["state"] == "SUBMITTING"
        reopened.mark_unknown(intent["id"], "timeout after request may have arrived")
        with pytest.raises(ValueError, match="must be reconciled"):
            reopened.mark_submitting(intent["id"])
        assert reopened.pending()[0]["state"] == "UNKNOWN"


def test_prepared_can_be_abandoned_but_never_resolved_as_filled(ledger):
    intent = prepare(ledger)
    with pytest.raises(ValueError, match="Invalid intent transition"):
        ledger.resolve(intent["id"], {"state": "FILLED", "fills": [fill()]})
    ledger.resolve(intent["id"], {"state": "REJECTED", "reason": "abandoned_without_submission"})
    assert not ledger.pending()
    assert not ledger.fills()


def test_acknowledgement_is_not_a_fill_and_failed_resolve_is_atomic(ledger):
    intent = prepare(ledger)
    ledger.mark_submitting(intent["id"])
    with pytest.raises(ValueError, match="explicit"):
        ledger.resolve(intent["id"], {"status": "ok", "oid": 77})
    with pytest.raises(ValueError, match="actual exchange fill evidence"):
        ledger.resolve(intent["id"], {"state": "FILLED", "oid": 77})
    assert ledger.get_intent(intent["id"])["state"] == "SUBMITTING"
    assert not ledger.fills()
    ledger.resolve(intent["id"], {"state": "OPEN", "oid": 77})
    assert ledger.pending()[0]["state"] == "OPEN"


def test_fill_evidence_deduplicates_by_account_and_tid(ledger):
    intent = prepare(ledger)
    ledger.mark_submitting(intent["id"])
    result = {"state": "FILLED", "fills": [fill()]}
    ledger.resolve(intent["id"], result)
    ledger.resolve(intent["id"], result)
    assert len(ledger.fills()) == 1
    assert ledger.fill_totals() == {"alex": {"BTC": -2}}
    assert ledger.fill_notionals() == {"alex": {"BTC": -200}}
    assert ledger.fills()[0]["fee"] == .04
    assert not ledger.pending()


def test_partial_ioc_is_canceled_with_actual_partial_quantity(ledger):
    intent = prepare(ledger)
    ledger.mark_submitting(intent["id"])
    ledger.resolve(intent["id"], {"state": "CANCELED", "fills": [fill(quantity=-.75)]})
    assert ledger.fill_totals()["alex"]["BTC"] == -.75
    assert ledger.get_intent(intent["id"])["request"]["size"] == 2
    assert not ledger.pending()


@pytest.mark.parametrize("bad_fill", [fill(quantity=2), fill(account="jeremy"),
    fill(symbol="ETH"), fill(quantity=-3), fill(tid=None), fill(fee=None)])
def test_mismatched_or_incomplete_fill_evidence_is_rejected_atomically(ledger, bad_fill):
    intent = prepare(ledger)
    ledger.mark_submitting(intent["id"])
    with pytest.raises(ValueError):
        ledger.resolve(intent["id"], {"state": "FILLED", "fills": [bad_fill]})
    assert ledger.get_intent(intent["id"])["state"] == "SUBMITTING"
    assert not ledger.fills()


def test_conflicting_duplicate_evidence_cannot_rewrite_accounting(ledger):
    intent = prepare(ledger)
    ledger.mark_submitting(intent["id"])
    ledger.resolve(intent["id"], {"state": "FILLED", "fills": [fill()]})
    with pytest.raises(ValueError, match="Conflicting exchange evidence"):
        ledger.resolve(intent["id"], {"state": "FILLED", "fills": [fill(price=105)]})
    assert ledger.fills()[0]["price"] == 100


def test_observation_and_monotonic_cooldown_survive_restart(ledger):
    observation = {"observed_at_utc": "2026-09-26T04:00:00Z", "equity": 999,
                   "_owned_fill_totals": {}, "positions": [{"symbol": "BTC", "quantity": -2}]}
    ledger.record_observation(observation)
    ledger.set_cooldown("alex", "BTC", 2000)
    ledger.set_cooldown("alex", "BTC", 1000)
    with PowderLedger(ledger.path) as reopened:
        assert reopened.latest_observation() == observation
        assert reopened.get_cooldown("alex", "BTC") == 2000
        assert reopened.get_cooldown("alex", "ETH") is None


def test_read_only_ui_returns_bounded_actual_evidence_without_constructor(ledger, monkeypatch):
    intent = prepare(ledger)
    ledger.mark_submitting(intent["id"])
    ledger.resolve(intent["id"], {"state": "FILLED", "fills": [fill()]})
    ledger.record_observation({"equity": 999})
    prepare(ledger)
    def forbidden(*args, **kwargs):
        raise AssertionError("UI must not construct writable PowderLedger")
    monkeypatch.setattr(PowderLedger, "__init__", forbidden)
    snapshot = read_status(ledger.path, limit=1)
    assert snapshot["status"] == "ready"
    assert snapshot["metadata"]["baseline"] == BASELINE
    assert snapshot["latest_observation"] == {"equity": 999}
    assert len(snapshot["intents"]) == len(snapshot["events"]) == len(snapshot["fills"]) == 1
    assert snapshot["fills"][0]["account"] == "alex"
    assert snapshot["counts"] == {"intents": 2, "fills": 1, "pending": 1}
    assert "total_pnl" not in snapshot and "fees" not in snapshot


def test_absent_ui_read_never_creates_directory(tmp_path):
    path = tmp_path / "missing" / "ledger.sqlite3"
    assert read_status(path)["status"] == "missing"
    assert not path.parent.exists()


def test_other_database_is_not_initialized_or_changed(tmp_path):
    path = tmp_path / "paper.sqlite3"
    with sqlite3.connect(path) as connection:
        connection.execute("CREATE TABLE accounts(account TEXT)")
    before = hashlib.sha256(path.read_bytes()).digest()
    with pytest.raises(ValueError, match="different ledger"):
        PowderLedger(path)
    assert hashlib.sha256(path.read_bytes()).digest() == before
    assert read_status(path)["status"] == "error"


def test_prepare_requires_activation(tmp_path):
    with PowderLedger(tmp_path / "powder.sqlite3") as ledger:
        with pytest.raises(ValueError, match="Activate"):
            prepare(ledger)
        assert read_status(ledger.path)["status"] == "not_activated"


@pytest.mark.parametrize("fee_token", ["BTC", "UBTC"])
@pytest.mark.parametrize("is_buy", [True, False])
def test_spot_inventory_accounts_for_base_token_fees_without_changing_trade_quantity(ledger, fee_token, is_buy):
    intent = prepare(ledger, account="clearpond", coin="@1", is_buy=is_buy)
    ledger.mark_submitting(intent["id"])
    quantity = 2 if is_buy else -2
    ledger.resolve(intent["id"], {"state": "FILLED", "fills": [fill(account="clearpond", coin="@1",
        quantity=quantity, fee=.001, fee_token=fee_token)]})
    assert ledger.fill_totals() == {"clearpond": {"BTC": quantity}}
    assert ledger.inventory_totals() == {"clearpond": {"BTC": pytest.approx(quantity-.001)}}
    assert ledger.fill_notionals() == {"clearpond": {"BTC": quantity*100}}


def test_spot_quote_currency_fee_does_not_change_inventory(ledger):
    intent = prepare(ledger, account="clearpond", is_buy=True)
    ledger.mark_submitting(intent["id"])
    ledger.resolve(intent["id"], {"state": "FILLED", "fills": [fill(account="clearpond", quantity=2)]})
    assert ledger.inventory_totals() == {"clearpond": {"BTC": 2}}


def test_unknown_spot_fee_currency_cannot_silently_reconcile_inventory(ledger):
    intent = prepare(ledger, account="clearpond", is_buy=True)
    ledger.mark_submitting(intent["id"])
    ledger.resolve(intent["id"], {"state": "FILLED", "fills": [fill(account="clearpond", quantity=2, fee_token="UNKNOWN")]})
    with pytest.raises(ValueError, match="Unknown spot fee currency"):
        ledger.inventory_totals()
