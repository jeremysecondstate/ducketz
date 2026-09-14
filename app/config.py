from __future__ import annotations

import os
from dataclasses import dataclass

from dotenv import load_dotenv
from app.hyperliquid_accounts import HYPERLIQUID_ACCOUNT_PROFILES, first_env_value


DEFAULT_HYPERLIQUID_INFO_URL = "https://api.hyperliquid.xyz/info"
DEFAULT_HYPEREVM_RPC_URL = "https://rpc.hyperliquid.xyz/evm"


load_dotenv()


@dataclass(frozen=True)
class HyperliquidAccountConfig:
    label: str
    wallet_address: str
    profile_key: str = ""


def hyperliquid_accounts() -> list[HyperliquidAccountConfig]:
    return [
        HyperliquidAccountConfig(
            label=profile.label,
            wallet_address=first_env_value(profile.wallet_address_env_keys),
            profile_key=profile.key,
        )
        for profile in HYPERLIQUID_ACCOUNT_PROFILES.values()
    ]


def hyperliquid_info_url() -> str:
    return os.getenv("HYPERLIQUID_INFO_URL", DEFAULT_HYPERLIQUID_INFO_URL).strip()


def hyperevm_rpc_url() -> str:
    return os.getenv("HYPEREVM_RPC_URL", DEFAULT_HYPEREVM_RPC_URL).strip()


def _required_env(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise RuntimeError(f"Missing required environment variable: {name}")
    return value


@dataclass(frozen=True)
class SchwabConfig:
    client_id: str
    client_secret: str
    redirect_uri: str


def schwab_config() -> SchwabConfig:
    return SchwabConfig(
        client_id=_required_env("SCHWAB_CLIENT_ID"),
        client_secret=_required_env("SCHWAB_CLIENT_SECRET"),
        redirect_uri=_required_env("SCHWAB_REDIRECT_URI"),
    )
