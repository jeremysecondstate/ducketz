from __future__ import annotations

import os
import threading
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation, ROUND_CEILING, ROUND_FLOOR
from typing import Any

from app.services.hyperliquid import HyperliquidInfoClient

HYPERLIQUID_MAX_PRICE_SIGNIFICANT_FIGURES = 5
HYPERLIQUID_MAX_SIZE_DECIMALS = 8

SPOT_EXECUTION_ALIASES = {
    "BTC": "UBTC/USDC",
    "UBTC": "UBTC/USDC",
    "ETH": "UETH/USDC",
    "UETH": "UETH/USDC",
    "ZEC": "UZEC/USDC",
    "UZEC": "UZEC/USDC",
}


@dataclass(frozen=True)
class HyperliquidLiveAccountProfile:
    key: str
    label: str
    wallet_address_env_keys: tuple[str, ...]
    api_address_env_keys: tuple[str, ...]
    api_secret_env_keys: tuple[str, ...]


HYPERLIQUID_LIVE_ACCOUNTS = {
    "jeremy": HyperliquidLiveAccountProfile(
        key="jeremy",
        label="Jeremy",
        wallet_address_env_keys=(
            "HYPE_WALLET_ADDRESS_JEREMY_SECONDSTATE",
            "HYPE_WALLET_ADDRESS_JEREMY",
        ),
        api_address_env_keys=("HYPE_API_ADDRESS_JEREMY",),
        api_secret_env_keys=("HYPE_API_SECRET_JEREMY",),
    ),
    "alex": HyperliquidLiveAccountProfile(
        key="alex",
        label="Alex",
        wallet_address_env_keys=(
            "HYPE_WALLET_ADDRESS_ALEX_SECONDSTATE",
            "HYPE_WALLET_ADDRESS_ALEX",
        ),
        api_address_env_keys=("HYPE_API_ADDRESS_ALEX",),
        api_secret_env_keys=("HYPE_API_SECRET_ALEX",),
    ),
}


@dataclass(frozen=True)
class HyperliquidOrderTicket:
    coin: str
    is_buy: bool
    size: float
    limit_price: float
    tif: str
    reduce_only: bool = False
    client_order_id: str | None = None

    @property
    def side_label(self) -> str:
        return "BUY" if self.is_buy else "SELL"

    @property
    def notional(self) -> float:
        return round(self.size * self.limit_price, 2)

    def order_type_payload(self) -> dict[str, Any]:
        return {"limit": {"tif": self.tif}}


class HyperliquidTradingConfig:
    def __init__(self, account_key: str) -> None:
        self.account = _live_account_profile(account_key)
        self.account_key = self.account.key
        self.account_label = self.account.label
        self.wallet_address = _first_env_value(self.account.wallet_address_env_keys)
        self.api_address = _first_env_value(self.account.api_address_env_keys)
        self.api_secret = _first_env_value(self.account.api_secret_env_keys)
        self.has_signing_secret = bool(self.api_secret)
        self.live_enabled = os.getenv("HYPERLIQUID_ENABLE_LIVE_ORDERS", "").strip().lower() == "true"
        self.max_live_notional = _float_env("HYPERLIQUID_MAX_LIVE_ORDER_DOLLARS", 500.0)

    def validate_for_live_action(self) -> None:
        if not self.wallet_address.startswith("0x") or len(self.wallet_address) != 42:
            raise ValueError(f"{self.account_label} wallet address is missing or invalid.")
        if not self.api_address.startswith("0x") or len(self.api_address) != 42:
            raise ValueError(f"{self.account_label} API wallet address is missing or invalid.")
        if not self.has_signing_secret:
            raise ValueError(f"{self.account_label} HYPE_API_SECRET is missing from local .env.")
        if not self.live_enabled:
            raise PermissionError("Set HYPERLIQUID_ENABLE_LIVE_ORDERS=true before live Hyperliquid actions.")

    def validate_for_live_order(self, ticket: HyperliquidOrderTicket) -> None:
        self.validate_for_live_action()

        if ticket.size <= 0:
            raise ValueError("Hyperliquid size must be positive.")
        if ticket.limit_price <= 0:
            raise ValueError("Hyperliquid limit price must be positive.")
        if not ticket.reduce_only and ticket.notional > self.max_live_notional:
            raise PermissionError(
                f"Estimated notional ${ticket.notional:,.2f} exceeds "
                f"HYPERLIQUID_MAX_LIVE_ORDER_DOLLARS=${self.max_live_notional:,.2f}."
            )

    def validation_lines(self) -> list[str]:
        return [
            _gate("wallet address", self.wallet_address.startswith("0x") and len(self.wallet_address) == 42),
            _gate("API wallet", self.api_address.startswith("0x") and len(self.api_address) == 42),
            _gate("API secret present", self.has_signing_secret),
            _gate("live enabled", self.live_enabled),
            _gate(f"max notional ${self.max_live_notional:,.2f}", True),
        ]


class HyperliquidExecutionAdapter:
    def __init__(self, account_key: str) -> None:
        self.account_key = account_key
        self._sdk_exchange: Any | None = None
        self._sdk_exchange_lock = threading.Lock()

    def config(self) -> HyperliquidTradingConfig:
        return HyperliquidTradingConfig(self.account_key)

    def open_orders(self) -> list[dict[str, Any]]:
        config = self.config()
        if not config.wallet_address.startswith("0x") or len(config.wallet_address) != 42:
            raise ValueError(f"{config.account_label} wallet address is missing or invalid.")

        payload = HyperliquidInfoClient().post_info(
            {
                "type": "openOrders",
                "user": config.wallet_address,
            }
        )

        if not isinstance(payload, list):
            raise RuntimeError("Hyperliquid openOrders returned an unexpected response.")

        return [
            {
                **order,
                "accountKey": config.account_key,
                "accountLabel": config.account_label,
                "accountAddress": config.wallet_address,
            }
            for order in payload
            if isinstance(order, dict)
        ]

    def modify_order(self, order_id: int, ticket: HyperliquidOrderTicket) -> Any:
        normalized_ticket = normalize_hyperliquid_ticket_for_wire(ticket)
        config = self.config()
        config.validate_for_live_order(normalized_ticket)

        if order_id <= 0:
            raise ValueError("Hyperliquid edit requires a positive order ID.")

        return self._local_signed_modify(order_id, normalized_ticket, config)

    def submit(self, ticket: HyperliquidOrderTicket) -> Any:
        normalized_ticket = normalize_hyperliquid_ticket_for_wire(ticket)
        config = self.config()
        config.validate_for_live_order(normalized_ticket)
        return self._local_signed_submit(normalized_ticket, config)

    def cancel(self, coin: str, order_id: int) -> Any:
        config = self.config()
        config.validate_for_live_action()

        normalized_coin = coin.strip()
        if not normalized_coin:
            raise ValueError("Hyperliquid cancel requires a coin / market.")
        if order_id <= 0:
            raise ValueError("Hyperliquid cancel requires a positive order ID.")

        return self._local_signed_cancel(normalized_coin, order_id, config)

    def _exchange(self, config: HyperliquidTradingConfig) -> Any:
        with self._sdk_exchange_lock:
            if self._sdk_exchange is None:
                from eth_account import Account
                from hyperliquid.exchange import Exchange
                from hyperliquid.utils import constants

                api_wallet = Account.from_key(config.api_secret)
                self._sdk_exchange = Exchange(
                    api_wallet,
                    constants.MAINNET_API_URL,
                    account_address=config.wallet_address,
                    timeout=10.0,
                )
            return self._sdk_exchange

    def _local_signed_submit(
        self,
        ticket: HyperliquidOrderTicket,
        config: HyperliquidTradingConfig,
    ) -> Any:
        exchange = self._exchange(config)
        normalized_ticket = normalize_hyperliquid_ticket_size_for_exchange(ticket, exchange)

        cloid = None
        if normalized_ticket.client_order_id:
            from hyperliquid.utils.types import Cloid

            cloid = Cloid.from_str(normalized_ticket.client_order_id)

        return exchange.order(
            normalized_ticket.coin,
            normalized_ticket.is_buy,
            normalized_ticket.size,
            normalized_ticket.limit_price,
            normalized_ticket.order_type_payload(),
            reduce_only=normalized_ticket.reduce_only,
            cloid=cloid,
        )

    def _local_signed_cancel(
        self,
        coin: str,
        order_id: int,
        config: HyperliquidTradingConfig,
    ) -> Any:
        exchange = self._exchange(config)

        return exchange.cancel(coin, order_id)

    def _local_signed_modify(
        self,
        order_id: int,
        ticket: HyperliquidOrderTicket,
        config: HyperliquidTradingConfig,
    ) -> Any:
        exchange = self._exchange(config)
        normalized_ticket = normalize_hyperliquid_ticket_size_for_exchange(ticket, exchange)

        cloid = None
        if normalized_ticket.client_order_id:
            from hyperliquid.utils.types import Cloid

            cloid = Cloid.from_str(normalized_ticket.client_order_id)

        return exchange.modify_order(
            order_id,
            normalized_ticket.coin,
            normalized_ticket.is_buy,
            normalized_ticket.size,
            normalized_ticket.limit_price,
            normalized_ticket.order_type_payload(),
            reduce_only=normalized_ticket.reduce_only,
            cloid=cloid,
        )


def normalize_hyperliquid_ticket_for_wire(ticket: HyperliquidOrderTicket) -> HyperliquidOrderTicket:
    normalized_price = normalize_hyperliquid_limit_price(ticket.limit_price, is_buy=ticket.is_buy)
    normalized_size = normalize_hyperliquid_size(ticket.size)

    return HyperliquidOrderTicket(
        coin=ticket.coin,
        is_buy=ticket.is_buy,
        size=normalized_size,
        limit_price=normalized_price,
        tif=ticket.tif,
        reduce_only=ticket.reduce_only,
        client_order_id=ticket.client_order_id,
    )


def normalize_hyperliquid_ticket_size_for_exchange(
    ticket: HyperliquidOrderTicket,
    exchange: Any,
) -> HyperliquidOrderTicket:
    decimals = _hyperliquid_size_decimals_for_exchange(ticket.coin, exchange)
    normalized_size = normalize_hyperliquid_size(ticket.size, max_decimals=decimals)

    return HyperliquidOrderTicket(
        coin=ticket.coin,
        is_buy=ticket.is_buy,
        size=normalized_size,
        limit_price=ticket.limit_price,
        tif=ticket.tif,
        reduce_only=ticket.reduce_only,
        client_order_id=ticket.client_order_id,
    )


def normalize_hyperliquid_size(
    size: float,
    max_decimals: int = HYPERLIQUID_MAX_SIZE_DECIMALS,
) -> float:
    try:
        decimal_size = Decimal(str(size))
    except InvalidOperation as exc:
        raise ValueError("Hyperliquid size must be a number.") from exc

    if decimal_size <= 0:
        raise ValueError("Hyperliquid size must be positive.")

    decimals = max(0, min(HYPERLIQUID_MAX_SIZE_DECIMALS, int(max_decimals)))
    quant = Decimal("1").scaleb(-decimals)
    normalized = decimal_size.quantize(quant, rounding=ROUND_FLOOR)

    if normalized <= 0:
        raise ValueError("Hyperliquid size is too small after exchange precision rounding.")

    return float(normalized)


def normalize_hyperliquid_limit_price(price: float, *, is_buy: bool) -> float:
    try:
        decimal_price = Decimal(str(price))
    except InvalidOperation as exc:
        raise ValueError("Hyperliquid limit price must be a number.") from exc

    if decimal_price <= 0:
        raise ValueError("Hyperliquid limit price must be positive.")

    tick = hyperliquid_price_tick(decimal_price)
    quotient = decimal_price / tick
    rounding = ROUND_FLOOR if is_buy else ROUND_CEILING
    return float(quotient.to_integral_value(rounding=rounding) * tick)


def format_hyperliquid_limit_price(price: float) -> str:
    text = format(Decimal(str(price)).normalize(), "f")
    return text.rstrip("0").rstrip(".") if "." in text else text


def normalize_hyperliquid_coin(symbol: str) -> str:
    clean = symbol.strip().upper()

    if "/" in clean:
        clean = clean.split("/", 1)[0]

    for suffix in ("-PERP-SHORT", "-PERP", "-SPOT"):
        if clean.endswith(suffix):
            clean = clean[: -len(suffix)]

    return clean


def normalize_hyperliquid_spot_market(symbol: str, quote_asset: str = "USDC") -> str:
    clean = normalize_hyperliquid_coin(symbol)
    quote = quote_asset.strip().upper() or "USDC"

    if quote not in {"USDC", "USDT"}:
        raise ValueError("Hyperliquid spot quote must be USDC or USDT.")

    alias = SPOT_EXECUTION_ALIASES.get(clean)
    if alias and alias.endswith(f"/{quote}"):
        return alias

    return f"{clean}/{quote}"


def _hyperliquid_size_decimals_for_exchange(coin: str, exchange: Any) -> int:
    info = exchange.info
    mapped_coin = info.name_to_coin.get(coin, coin)
    asset = info.coin_to_asset.get(mapped_coin)

    if asset is None:
        return HYPERLIQUID_MAX_SIZE_DECIMALS

    decimals = info.asset_to_sz_decimals.get(asset)
    return int(decimals) if decimals is not None else HYPERLIQUID_MAX_SIZE_DECIMALS


def hyperliquid_price_tick(
    price: Decimal,
    *,
    size_decimals: int | None = None,
    spot: bool = True,
) -> Decimal:
    if price <= 0:
        raise ValueError("Hyperliquid price tick requires a positive price.")
    if price == price.to_integral_value():
        return Decimal("1")
    adjusted = price.adjusted()
    exponent = adjusted - HYPERLIQUID_MAX_PRICE_SIGNIFICANT_FIGURES + 1
    significant_tick = Decimal("1").scaleb(exponent)
    if size_decimals is None:
        return significant_tick
    max_decimals = (8 if spot else 6) - max(0, int(size_decimals))
    decimal_limit_tick = Decimal("1").scaleb(-max(0, max_decimals))
    return max(significant_tick, decimal_limit_tick)


def _live_account_profile(account_key: str) -> HyperliquidLiveAccountProfile:
    normalized = account_key.strip().lower()
    try:
        return HYPERLIQUID_LIVE_ACCOUNTS[normalized]
    except KeyError as exc:
        choices = ", ".join(sorted(HYPERLIQUID_LIVE_ACCOUNTS))
        raise ValueError(f"Unknown Hyperliquid account '{account_key}'. Choices: {choices}") from exc


def _first_env_value(keys: tuple[str, ...]) -> str:
    for key in keys:
        value = os.getenv(key, "").strip().strip("'\"")
        if value and value.lower() != "key in here":
            return value
    return ""


def _float_env(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, "").strip())
    except ValueError:
        return default


def _gate(label: str, ok: bool) -> str:
    return f"{'✅' if ok else '❌'} {label}"
