from __future__ import annotations

import pandas as pd
import pytest
import requests

from datafetching import hyperliquid_candles as md


_START = 1_800_000
_STEP = md.INTERVAL_MS["15m"]


def _row(opened=_START, **overrides):
    return {
        "t": opened, "T": opened + _STEP - 1, "s": "BTC", "i": "15m",
        "o": "100", "h": "103", "l": "99", "c": "102", "v": "2.5", "n": 20,
        **overrides,
    }


def _normalize(rows, as_of=_START + 10 * _STEP):
    return md.normalize_candles(rows, coin="BTC", interval="15m", as_of_ms=as_of)


def test_forming_candle_excluded_until_exclusive_boundary():
    rows = [_row(), _row(_START + _STEP)]
    before = _normalize(rows, _START + _STEP - 1)
    at_boundary = _normalize(rows, _START + _STEP)
    assert before.empty
    assert len(at_boundary) == 1
    assert at_boundary.iloc[0]["close_time"] == pd.Timestamp(_START + _STEP, unit="ms", tz="UTC")
    assert str(at_boundary["timestamp"].dtype) == "datetime64[ns, UTC]"
    assert str(at_boundary["trade_count"].dtype) == "int64"


def test_duplicate_order_and_refresh_keep_newest_values_and_old_history():
    old = _normalize([_row(), _row(_START + _STEP)])
    incoming = _normalize([
        _row(_START + 2 * _STEP), _row(_START + _STEP, c="101"),
        _row(_START + _STEP, c="100"),
    ])
    merged = md.merge_candles(old, incoming)
    assert merged["close"].tolist() == [102.0, 100.0, 102.0]
    assert merged["timestamp"].is_monotonic_increasing
    assert merged["timestamp"].is_unique
    pd.testing.assert_frame_equal(md.merge_candles(merged, incoming), merged)


@pytest.mark.parametrize("change,match", [
    ({"s": "ETH"}, "identity"), ({"i": "5m"}, "identity"),
    ({"t": _START + 1}, "aligned"), ({"T": _START + 3}, "boundary"),
    ({"o": "nan"}, "finite"), ({"h": "101"}, "envelope"),
    ({"l": "103"}, "envelope"), ({"c": "0"}, "positive"),
    ({"v": "-1"}, "nonnegative"), ({"n": 1.5}, "integer"),
    ({"n": True}, "integer"), ({"t": "not-a-time"}, "integer"),
])
def test_malformed_rows_fail_with_specific_reason(change, match):
    with pytest.raises(md.CandleDataError, match=match):
        _normalize([_row(**change)])


def test_missing_field_has_context_and_empty_table_keeps_schema():
    row = _row()
    del row["c"]
    with pytest.raises(md.CandleDataError, match="row 0 is missing field 'c'"):
        _normalize([row])
    empty = _normalize([])
    assert empty.columns.tolist() == list(md.CANDLE_COLUMNS)
    assert str(empty["close"].dtype) == "float64"


def test_gap_is_reported_without_synthetic_rows():
    frame = _normalize([_row(), _row(_START + 3 * _STEP)])
    summary = md.summarize_candles(frame, "15m")
    assert summary["rows"] == 2
    assert summary["gap_count"] == 1
    assert summary["missing_candles"] == 2
    assert summary["duplicate_timestamps"] == 0


def test_merge_rejects_different_market():
    btc = _normalize([_row()])
    eth = btc.assign(symbol="ETH")
    with pytest.raises(md.CandleDataError, match="different symbols"):
        md.merge_candles(btc, eth)


class _Response:
    def __init__(self, status=200, body=None, headers=None):
        self.status_code = status
        self.body = [_row()] if body is None else body
        self.headers = headers or {}
        self.closed = False

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(str(self.status_code))

    def json(self):
        return self.body

    def close(self):
        self.closed = True


class _Session:
    def __init__(self, responses):
        self.responses = iter(responses)
        self.calls = []
        self.closed = False

    def post(self, url, **kwargs):
        self.calls.append((url, kwargs))
        result = next(self.responses)
        if isinstance(result, Exception):
            raise result
        return result

    def close(self):
        self.closed = True


def _fetch(session):
    return md.fetch_candles(start_ms=_START, end_ms=_START + _STEP, session=session)


def test_public_payload_and_bounded_retries(monkeypatch):
    pauses = []
    monkeypatch.setattr(md.time, "sleep", pauses.append)
    limited = _Response(429, headers={"Retry-After": "999"})
    success = _Response()
    session = _Session([requests.ConnectionError("offline"), limited, _Response(503), success])
    assert _fetch(session) == [_row()]
    assert pauses == [1.0, 10.0, 4.0]
    assert len(session.calls) == 4
    url, args = session.calls[-1]
    assert url == md.DEFAULT_INFO_URL
    assert args == {"json": {"type": "candleSnapshot", "req": {
        "coin": "BTC", "interval": "15m", "startTime": _START, "endTime": _START + _STEP,
    }}, "timeout": (5, 30)}
    assert limited.closed and success.closed
    assert not session.closed


def test_http_client_error_is_not_retried():
    session = _Session([_Response(400)])
    with pytest.raises(md.CandleFetchError, match="HTTP 400"):
        _fetch(session)
    assert len(session.calls) == 1


def test_timeout_retry_budget_is_finite(monkeypatch):
    monkeypatch.setattr(md.time, "sleep", lambda delay: None)
    session = _Session([requests.Timeout()] * 4)
    with pytest.raises(md.CandleFetchError, match="after 4 attempts"):
        _fetch(session)
    assert len(session.calls) == 4


def test_exchange_error_object_is_not_treated_as_empty_history():
    with pytest.raises(md.CandleDataError, match="list of candle objects"):
        _fetch(_Session([_Response(body={"error": "bad request"})]))
