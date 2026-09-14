"""Shared Hyperliquid account identities; API signers are not portfolio wallets."""

from __future__ import annotations

import os
import re
import threading
import time
from dataclasses import dataclass


@dataclass(frozen=True)
class HyperliquidAccountProfile:
    key: str
    label: str
    wallet_address_env_keys: tuple[str, ...]
    api_address_env_keys: tuple[str, ...]
    api_secret_env_keys: tuple[str, ...]


HYPERLIQUID_ACCOUNT_PROFILES = {
    "jeremy": HyperliquidAccountProfile(
        "jeremy", "Jeremy",
        ("HYPE_WALLET_ADDRESS_JEREMY_SECONDSTATE", "HYPE_WALLET_ADDRESS_JEREMY"),
        ("HYPE_API_ADDRESS_JEREMY",), ("HYPE_API_SECRET_JEREMY",),
    ),
    "alex": HyperliquidAccountProfile(
        "alex", "Alex",
        ("HYPE_WALLET_ADDRESS_ALEX_SECONDSTATE", "HYPE_WALLET_ADDRESS_ALEX"),
        ("HYPE_API_ADDRESS_ALEX",), ("HYPE_API_SECRET_ALEX",),
    ),
    "clearpond": HyperliquidAccountProfile(
        "clearpond", "Clearpond",
        ("HYPE_WALLET_ADDRESS_CLEARPOND",),
        ("HYPE_API_CLEARPOND_WALLET",), ("HYPE_API_CLEARPOND_PRIVATE",),
    ),
}


def first_env_value(keys: tuple[str, ...]) -> str:
    for key in keys:
        value = os.getenv(key, "").strip().strip("'\"")
        if value and value.lower() != "key in here":
            return value
    return ""


def valid_wallet(value: str) -> bool:
    return bool(re.fullmatch(r"0x[0-9a-fA-F]{40}", value))


_wallet_cache: dict[tuple[str, str], tuple[float, str]] = {}
_wallet_lock = threading.Lock()


def resolve_portfolio_wallet(profile: HyperliquidAccountProfile, client=None) -> str:
    """Use an explicit owner or resolve an API agent via the public userRole API.

    Cache only successful public mappings for five minutes, scoped to endpoint
    and signer. Never infer an owner from balances, or persist a private key.
    """
    owner = first_env_value(profile.wallet_address_env_keys)
    if owner:
        if not valid_wallet(owner):
            raise ValueError(f"{profile.label} portfolio wallet is invalid.")
        return owner
    agent = first_env_value(profile.api_address_env_keys)
    if not valid_wallet(agent):
        raise ValueError(f"{profile.label} portfolio/API wallet is missing or invalid.")
    if client is None:
        from app.services.hyperliquid import HyperliquidInfoClient
        client = HyperliquidInfoClient(timeout_seconds=12)
    cache_key = (client.info_url, agent.lower())
    with _wallet_lock:
        cached = _wallet_cache.get(cache_key)
        if cached and cached[0] > time.monotonic():
            return cached[1]
    role = client.post_info({"type": "userRole", "user": agent})
    if not isinstance(role, dict):
        raise ValueError(f"{profile.label} account owner could not be verified.")
    if role.get("role") == "agent":
        data = role.get("data")
        owner = data.get("user", "") if isinstance(data, dict) else ""
    elif role.get("role") in {"user", "subAccount"}:
        owner = agent
    else:
        owner = ""
    if not isinstance(owner, str) or not valid_wallet(owner):
        raise ValueError(f"{profile.label} account owner could not be verified. Configure its portfolio wallet.")
    with _wallet_lock:
        _wallet_cache[cache_key] = (time.monotonic() + 300, owner)
    return owner
