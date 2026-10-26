"""Offline order fixtures shared by the intraday execution tests."""

def raw_order(*, status="FILLED", filled=10):
    return {
        "orderId": "tracked-1", "status": status, "quantity": 10.0, "filledQuantity": float(filled),
        "remainingQuantity": float(10 - filled), "orderType": "LIMIT", "price": 100.0,
        "orderLegCollection": [{"legId": 1, "instruction": "BUY", "quantity": 10.0,
            "instrument": {"assetType": "EQUITY", "symbol": "COST"}}],
        "orderActivityCollection": ([{"activityType": "EXECUTION", "activityId": "activity-1",
            "executionLegs": [{"legId": 1, "executionId": "execution-1", "quantity": filled,
                               "price": 99.99, "time": "2026-09-08T11:00:20Z"}]}] if filled else []),
    }


def precision_order(reservation, raw_timestamp):
    """A complete exact-ID fixture, priced at its original submitted limit."""
    raw = raw_order()
    raw.update(orderId=reservation.broker_order_id, quantity=reservation.quantity,
               filledQuantity=reservation.quantity, remainingQuantity=0, price=reservation.limit_price)
    leg = raw["orderLegCollection"][0]
    leg.update(instruction=reservation.side, quantity=reservation.quantity)
    leg["instrument"]["symbol"] = reservation.symbol
    raw["orderActivityCollection"][0]["executionLegs"][0].update(
        quantity=reservation.quantity, price=reservation.limit_price, time=raw_timestamp)
    return raw

