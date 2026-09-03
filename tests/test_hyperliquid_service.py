from __future__ import annotations

import pytest

from app.services.hyperliquid import (
    HyperEvmRpcClient,
    _hype_candle_closes,
    _hype_market_facts,
    _hyperliquid_account_facts,
)


def _spot_meta_and_contexts() -> list[object]:
    return [
        {
            "tokens": [
                {"name": "USDC", "index": 0},
                {"name": "HYPE", "index": 150},
            ],
            "universe": [
                {"name": "@105", "tokens": [149, 0], "index": 105},
                {"name": "@107", "tokens": [150, 0], "index": 107},
            ],
        },
        [
            {"coin": "@107", "midPx": "81.87", "prevDayPx": "80.03", "dayNtlVlm": "642180000", "circulatingSupply": "333930000"},
            {"coin": "@105", "midPx": "0.08", "prevDayPx": "0.08"},
        ],
    ]


def test_hype_market_facts_match_context_by_coin_not_array_position() -> None:
    facts = _hype_market_facts({"@107": "81.86"}, _spot_meta_and_contexts())

    assert facts["status"] == "current"
    assert facts["coin"] == "@107"
    assert facts["price"] == 81.87
    assert facts["change_percent_24h"] == pytest.approx((81.87 - 80.03) / 80.03 * 100)
    assert facts["volume_24h"] == 642_180_000.0
    assert facts["circulating_supply"] == 333_930_000.0


def test_hype_candles_keep_only_finite_close_values() -> None:
    class FakeClient:
        def post_info(self, payload: dict[str, object]) -> list[object]:
            assert payload["type"] == "candleSnapshot"
            request = payload["req"]
            assert isinstance(request, dict)
            assert request["coin"] == "@107"
            assert request["interval"] == "15m"
            return [{"c": "80.0"}, {"c": "nan"}, {"bad": "row"}, {"c": "81.5"}]

    closes = _hype_candle_closes(FakeClient(), "@107", end_time_ms=1_000_000_000)  # type: ignore[arg-type]

    assert closes == [80.0, 81.5]


def test_account_facts_retain_margin_and_position_risk_fields() -> None:
    facts = _hyperliquid_account_facts(
        {
            "withdrawable": "319.52",
            "marginSummary": {"accountValue": "14327.25", "totalMarginUsed": "14007.73"},
            "assetPositions": [
                {
                    "position": {
                        "coin": "HYPE",
                        "szi": "-200",
                        "entryPx": "81.01",
                        "liquidationPx": "106.88",
                        "marginUsed": "1096.84",
                        "returnOnEquity": "-0.0107",
                        "leverage": {"type": "cross", "value": 5},
                    }
                }
            ],
        }
    )

    assert facts["perp_equity"] == 14_327.25
    assert facts["available"] == 319.52
    assert facts["margin_used"] == 14_007.73
    position = facts["positions"]["HYPE"]  # type: ignore[index]
    assert position["signed_size"] == -200.0
    assert position["entry_price"] == 81.01
    assert position["liquidation_price"] == 106.88
    assert position["margin_mode"] == "cross"


def test_hyperevm_status_parses_batch_quantities_by_request_id(monkeypatch) -> None:
    captured: dict[str, object] = {}

    class FakeResponse:
        def raise_for_status(self) -> None:
            pass

        def json(self) -> list[dict[str, object]]:
            return [
                {"jsonrpc": "2.0", "id": 3, "result": "0x5f5e100"},
                {"jsonrpc": "2.0", "id": 1, "result": "0x3e7"},
                {"jsonrpc": "2.0", "id": 2, "result": "0x66dc420"},
            ]

    def fake_post(url: str, **kwargs: object) -> FakeResponse:
        captured["url"] = url
        captured.update(kwargs)
        return FakeResponse()

    monkeypatch.setattr("app.services.hyperliquid.requests.post", fake_post)

    status = HyperEvmRpcClient("https://example.invalid/evm", timeout_seconds=4).chain_status()

    assert status == {
        "available": True,
        "chain_id": 999,
        "block_number": 107_856_928,
        "gas_price_wei": 100_000_000,
    }
    assert captured["url"] == "https://example.invalid/evm"
    assert captured["timeout"] == 4
    payload = captured["json"]
    assert isinstance(payload, list)
    assert {row["method"] for row in payload} == {
        "eth_chainId",
        "eth_blockNumber",
        "eth_gasPrice",
    }
