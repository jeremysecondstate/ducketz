"""Every recovery checks exact originals without repeatedly decoding model trees."""
from dataclasses import asdict, replace

import pytest

from test_intraday_account_execution import env, native, account, advance
from test_intraday_recovery_latency import terminal_orders
from test_intraday_unknown_commitment_containment import uncertain
from ml.intraday.prediction import ContractError, digest, encoded
from ml.stock_direction_policy import stock_direction_for_policy


def decoding_spy(env, monkeypatch):
    decoded = []
    validator = env.store._forecast_value
    def tracked(row):
        decoded.append(row[0])
        return validator(row)
    monkeypatch.setattr(env.store, "_forecast_value", tracked)
    return decoded


def test_repeated_recovery_validates_each_exact_original_once_then_reuses_header(env, monkeypatch):
    originals = terminal_orders(env, 2)
    env.store._forecast_header_cache.clear()
    decoded = decoding_spy(env, monkeypatch)
    count = len(env.broker.submissions)
    advance(env)
    first = env.executor.recover()
    assert len(decoded) == len(originals), "Cold recovery redundantly decoded original model payloads"
    assert set(decoded) == {f["forecast_id"] for f in originals}
    decoded.clear()
    advance(env)
    assert env.executor.recover() == first
    assert decoded == [], "Warm recovery decoded unchanged historical model payloads"
    assert len(env.broker.submissions) == count and not env.broker.cancellations


@pytest.mark.parametrize("damage", ["payload", "indexed_actor", "indexed_symbol", "indexed_cycle", "indexed_reference", "indexed_issue", "missing"])
def test_warm_recovery_rechecks_all_raw_bytes_and_indexed_identities(env, damage):
    original = terminal_orders(env, 1)[0]
    env.executor.recover()
    with env.store.connect() as db:
        if damage == "payload":
            value = dict(original, model_diagnostics={"changed": True})
            db.execute("UPDATE forecasts SET payload=? WHERE id=?", (encoded(value), original["forecast_id"]))
        elif damage == "missing":
            db.execute("DELETE FROM forecasts WHERE id=?", (original["forecast_id"],))
        else:
            column = {"indexed_actor":"pc", "indexed_symbol":"symbol", "indexed_cycle":"cycle_id",
                      "indexed_reference":"reference_at", "indexed_issue":"issued_at"}[damage]
            db.execute("UPDATE forecasts SET " + column + "='changed' WHERE id=?", (original["forecast_id"],))
    count = len(env.broker.submissions)
    advance(env)
    with pytest.raises(ContractError):
        env.executor.recover()
    assert len(env.broker.submissions) == count and not env.broker.cancellations


@pytest.mark.parametrize("damage", ["missing_sizing", "sizing_payload", "order_forecast", "order_symbol", "order_side", "order_quantity"])
def test_saved_recovery_never_trusts_caller_identity_or_economics(env, damage):
    original = terminal_orders(env, 1)[0]
    order = native(env).snapshot().reservations[0]
    if damage.startswith("order_"):
        field, value = {"order_forecast":("forecast_id", "wrong"), "order_symbol":("symbol", "AMZN"),
                        "order_side":("side", "SELL"), "order_quantity":("quantity", env.executor.intents.get(original["forecast_id"])["desired_quantity"] + 1)}[damage]
        order = replace(order, **{field:value})
    else:
        with env.store.connect() as db:
            if damage == "missing_sizing":
                db.execute("DELETE FROM intraday_intentions WHERE forecast_id=?", (original["forecast_id"],))
            else:
                db.execute("UPDATE intraday_intentions SET payload=json_set(payload,'$.desired_quantity',999) WHERE forecast_id=?", (original["forecast_id"],))
    count = len(env.broker.submissions)
    with pytest.raises(ContractError):
        env.executor.intents.record_recovery(original["forecast_id"], actor="Atlas", event_key="recovery-check",
            recorded_at=env.clock(), disposition="order_status_reconciled", evidence={"test":True},
            kind="execution_disposition", order=order)
    assert len(env.broker.submissions) == count and not env.broker.cancellations


def test_recovery_rejects_wrong_actor_and_arbitrary_caller_dictionary(env):
    original = terminal_orders(env, 1)[0]
    order = native(env).snapshot().reservations[0]
    arguments = dict(event_key="wrong", recorded_at=env.clock(), disposition="order_status_reconciled",
                     evidence={"test":True}, kind="execution_disposition", order=order)
    with pytest.raises(ContractError):
        env.executor.intents.record_recovery(original["forecast_id"], actor="Scout", **arguments)
    with pytest.raises((ContractError, TypeError)):
        env.executor.intents.record_recovery(original, actor="Atlas", **arguments)


def test_warm_recovery_preserves_unknown_and_known_exact_fill_evidence(env, monkeypatch):
    env.cash = 350.
    known_forecast = env.forecast(symbol="AMZN")
    env.executor.step(known_forecast)
    known = native(env).snapshot().reservations[0]
    _, unknown = uncertain(env)
    before_native = asdict(unknown)
    before_authority = account(env).read_state()["reservations"][1]
    env.orders[known.broker_order_id].update(status="FILLED", filled=known.quantity, fill_at=env.clock())
    env.held["AMZN"] = known.quantity
    env.cash = 150.
    first = env.executor.recover()
    decoded = decoding_spy(env, monkeypatch)
    advance(env)
    assert env.executor.recover() == first
    assert decoded == []
    assert asdict(native(env).lookup_reservation(unknown.reservation_id)) == before_native
    assert account(env).read_state()["reservations"][1] == before_authority
    by_id = {row["evidence"]["native_reservation_id"]:row for row in first}
    assert by_id[unknown.reservation_id]["disposition"] == "unresolved_submission_retained"
    assert by_id[unknown.reservation_id]["evidence"]["commitment_retained"] is True
    assert by_id[known.reservation_id]["filled_quantity"] == known.quantity
    assert by_id[known.reservation_id]["broker_order_id"] == known.broker_order_id
    assert len(env.broker.submissions) == 2 and not env.broker.cancellations


@pytest.mark.parametrize("policy", ["stock-direction-50-01-v3", "stock-direction-5001-4999-v3"])
@pytest.mark.parametrize("probability,direction", [(0.4999,"BEARISH"), (0.5,"NO_EDGE"), (0.5001,"BULLISH")])
def test_current_policy_dependencies_retain_both_inclusive_exact_identities(policy, probability, direction):
    assert stock_direction_for_policy(probability, policy) == direction


def test_recovery_preserves_original_historical_sized_side_under_current_neutral_band(env):
    original = terminal_orders(env, 1)[0]
    order = native(env).snapshot().reservations[0]
    # A frozen pre-policy publication, not a reinterpretation of a new issue.
    legacy = dict(original, cycle_id="legacy-cycle", probability_not_down=0.50005,
                  direction="BULLISH", direction_policy_version="stock-direction-50-v2",
                  direction_up_threshold=0.5, direction_down_threshold=0.5)
    legacy.pop("forecast_id")
    legacy["forecast_id"] = digest(legacy)
    original_sizing = env.executor.intents.get(original["forecast_id"])
    sizing_details = dict(original_sizing["sizing"], forecast_id=legacy["forecast_id"])
    sizing = dict(original_sizing, forecast_id=legacy["forecast_id"],
                  sizing=sizing_details, sizing_id=digest(sizing_details))
    with env.store.connect() as db:
        db.execute("INSERT INTO forecasts VALUES(?,?,?,?,?,?,?)", (
            *(legacy[k] for k in ("forecast_id","pc","symbol","cycle_id","reference_at","issued_at")), encoded(legacy)))
        db.execute("INSERT INTO intraday_intentions VALUES(?,?,?)", (legacy["forecast_id"],digest(sizing),encoded(sizing)))
    assert stock_direction_for_policy(legacy["probability_not_down"], "stock-direction-50-01-v3") == "NO_EDGE"
    projected = env.executor.intents.record_recovery(legacy["forecast_id"], actor="Atlas", event_key="legacy-recovery",
        recorded_at=env.clock(), disposition="order_status_reconciled", evidence={"test":True},
        kind="execution_disposition", order=replace(order, forecast_id=legacy["forecast_id"]))
    assert projected["side"] == sizing["side"] == "BUY"
    assert env.store.forecast(legacy["cycle_id"],legacy["symbol"]) == legacy
