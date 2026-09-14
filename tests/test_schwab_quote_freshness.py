"""Live response provenance, using a synthetic HTTP response only."""
from datetime import datetime, timezone
from types import SimpleNamespace

import app.services.schwab as schwab
from ml.stock_trader.state import capture_portfolio_state
from test_stock_trader_cash_state import CashBroker


def test_schwab_adapter_preserves_realtime_metadata_and_times_each_http_response(monkeypatch):
    session = object.__new__(schwab.SchwabSession)
    session._headers = lambda: {}
    quote = {"bidPrice": 123.66, "askPrice": 124.75, "quoteTime": 1789390120096}
    calls = []
    def get(url, **kwargs):
        calls.append((url, kwargs))
        return SimpleNamespace(raise_for_status=lambda: None, json=lambda: {
            "TWST": {"realtime": True, "quoteType": "NBBO", "quote": quote}})
    monkeypatch.setattr(schwab.requests, "get", get)
    before = datetime.now(timezone.utc)
    first = session.get_equity_quotes(("TWST",))["TWST"]
    second = session.get_equity_quotes(("TWST",))["TWST"]
    after = datetime.now(timezone.utc)
    assert len(calls) == 2
    assert calls[0][1]["params"] == {"symbols": "TWST", "fields": "quote"}
    for result in (first, second):
        assert result["quoteTime"] == quote["quoteTime"]
        assert result["quote_realtime"] is True and result["quote_type"] == "NBBO"
        assert before <= datetime.fromisoformat(result["quote_received_at"]) <= after
    assert "quote_received_at" not in quote


def test_state_keeps_provider_update_time_and_current_response_time_distinct():
    broker = CashBroker()
    broker.get_equity_quotes = lambda symbols: {s: {
        "bid": 123.66, "ask": 124.75, "quoteTime": "2026-09-09T10:48:40Z",
        "quote_received_at": "2026-09-09T11:00:01Z", "quote_realtime": True, "quote_type": "NBBO"
    } for s in symbols}
    result = capture_portfolio_state(broker, observed_at="2026-09-09T11:00:00Z", parallel=False,
                                     use_actual_quote_timestamps=True)
    quote = result.quotes["AAPL"]
    assert quote.observed_at == "2026-09-09T10:48:40+00:00"
    assert quote.freshness_observed_at == "2026-09-09T11:00:01+00:00"
    assert result.observed_at == "2026-09-09T11:00:00+00:00"
