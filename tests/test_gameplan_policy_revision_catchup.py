"""Policy corrections retain only their own explicit catch-up commitments."""
from types import SimpleNamespace

import pytest

from ml.stock_trader.catchup import catchup_signals


NOW = "2026-10-19T12:15:00Z"
ORIGINAL, PARENT, CURRENT, UNRELATED = (letter * 64 for letter in "abcd")


def _plan(*, buy=5, sell=1, ancestors=None):
    return {"action_date": "2026-10-19", "as_of": "2026-10-19T09:00:00Z", "plan_sha256": CURRENT,
        "policy_revision": {"schema_version": "gameplan-policy-revision-v1",
            "parent_plan_sha256": PARENT, "ancestor_plan_sha256": [PARENT] if ancestors is None else ancestors},
        "ledger": {"events": [
            {"forecast_id": "opening", "symbol": "AAPL", "horizon": "1h", "action": "BUY", "quantity": buy,
             "timestamp": "2026-10-19T11:00:00Z", "sequence": 1},
            {"forecast_id": "five", "symbol": "AAPL", "horizon": "1h", "action": "SELL", "quantity": sell,
             "timestamp": "2026-10-19T12:00:00Z", "sequence": 2}]}}


def _reservation(digest=PARENT, *, filled=0, remaining=0, status="FILLED", side="BUY",
                 symbol="AAPL", horizon="1h", identity="r1", **extra):
    return SimpleNamespace(forecast_id=f"catchup:{digest}:{symbol}:{horizon}:original-intention",
        side=side, filled_quantity=filled, reserved_quantity=remaining,
        status=status, reservation_id=identity, **extra)


@pytest.mark.parametrize("filled,remaining,status", [(4, 0, "FILLED"), (1, 3, "WORKING"), (0, 4, "SUBMITTING")])
def test_revised_plan_does_not_replay_parent_fills_or_reserved_shares(filled, remaining, status):
    reservations = [_reservation(filled=filled, remaining=remaining, status=status)]
    assert catchup_signals(_plan(), as_of=NOW, source_fingerprint="revised", reservations=reservations) == {}


def test_revised_sale_nets_parent_confirmed_sale():
    reservations = [_reservation(side="SELL", filled=4)]
    assert catchup_signals(_plan(buy=1, sell=5), as_of=NOW,
                          source_fingerprint="revised", reservations=reservations) == {}


def test_only_revised_residual_receives_the_current_plan_identity():
    source = _plan(buy=6)
    reservations = [_reservation(filled=4)]
    signal = catchup_signals(source, as_of=NOW, source_fingerprint="revised", reservations=reservations)["AAPL", "1h"]
    assert signal.planned_quantity == 1 and signal.calibrated_probability == 1
    assert signal.prediction_id.startswith(f"catchup:{CURRENT}:AAPL:1h:")
    assert signal.catchup_components == ("opening", "five")
    assert signal.source_fingerprint == "revised"


def test_multiple_revisions_keep_all_explicit_parent_commitments():
    reservations = [_reservation(ORIGINAL, filled=2), _reservation(PARENT, filled=2, identity="r2")]
    assert catchup_signals(_plan(ancestors=[ORIGINAL, PARENT]), as_of=NOW,
                          source_fingerprint="revised", reservations=reservations) == {}


def test_other_plan_symbol_and_horizon_reservations_are_not_netted():
    source = _plan()
    expected = catchup_signals(source, as_of=NOW, source_fingerprint="revised")
    reservations = [_reservation(UNRELATED, filled=20), _reservation(symbol="AMZN", filled=20, identity="r2"),
                    _reservation(horizon="4h", filled=20, identity="r3")]
    assert catchup_signals(source, as_of=NOW, source_fingerprint="revised", reservations=reservations) == expected


def test_confirmed_parent_rejection_remains_an_unresolved_failure():
    blocked = []
    reservations = [_reservation(status="REJECTED", broker_order_id="confirmed-order")]
    assert catchup_signals(_plan(), as_of=NOW, source_fingerprint="revised",
                          reservations=reservations, blocked=blocked) == {}
    assert blocked[0]["reason_code"] == "CONFIRMED_BROKER_REJECTION_REQUIRES_REVIEW"


@pytest.mark.parametrize("ancestors", [[], [CURRENT], [UNRELATED], [PARENT, PARENT], [PARENT, []], [PARENT, "invalid"]])
def test_malformed_or_unbound_revision_lineage_is_rejected(ancestors):
    with pytest.raises(ValueError, match="explicit original plan digests"):
        catchup_signals(_plan(ancestors=ancestors), as_of=NOW, source_fingerprint="revised")


def test_legacy_plan_identity_and_original_forecast_commitments_are_unchanged():
    source = _plan()
    del source["policy_revision"]
    signal = catchup_signals(source, as_of=NOW, source_fingerprint="revised")["AAPL", "1h"]
    reservation = _reservation(filled=5)
    reservation.forecast_id = "opening"
    residual = catchup_signals(source, as_of=NOW, source_fingerprint="revised", reservations=[reservation])["AAPL", "1h"]
    assert signal.prediction_id.startswith(f"catchup:{CURRENT}:AAPL:1h:")
    assert residual.planned_quantity == 1 and residual.calibrated_probability == 0
