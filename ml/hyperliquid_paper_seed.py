"""Read-only opening snapshot; never imports an order signer or writes on-chain."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import math
import os
import time

from dotenv import dotenv_values
import pandas as pd
import requests

from app.hyperliquid_accounts import HYPERLIQUID_ACCOUNT_PROFILES, valid_wallet
from datafetching.hyperliquid_candles import DEFAULT_INFO_URL

ALIASES = {"UBTC": "BTC", "UETH": "ETH", "UZEC": "ZEC"}
READ_TYPES = {"userRole", "clearinghouseState", "spotClearinghouseState",
              "userAbstraction", "frontendOpenOrders"}


class AccountReader:
    def post_info(self, payload):
        if not isinstance(payload, dict) or payload.get("type") not in READ_TYPES:
            raise ValueError("Only read-only account information requests are supported.")
        if set(payload) != {"type", "user"} or not isinstance(payload["user"], str) or not valid_wallet(payload["user"]):
            raise ValueError("Read-only account requests require only a type and valid public user address.")
        response = requests.post(DEFAULT_INFO_URL, json=payload, timeout=(5, 15))
        response.raise_for_status()
        return response.json()


def _number(value, *, positive=False, nonnegative=False):
    if isinstance(value, bool):
        raise ValueError("Account snapshot numbers cannot be booleans.")
    try:
        value = float(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError("Account snapshot has an invalid number.") from exc
    if not math.isfinite(value) or positive and value <= 0 or nonnegative and value < 0:
        raise ValueError("Account snapshot has an invalid finite numeric value.")
    return value


def mirror_accounts(market_provider, symbols, *, env_path=None, reader=None,
                    values=None, clock=time.time):
    """Return PaperLedger constructor arguments plus the exact opening quotes.

    Account owner/agent public addresses are used only for public info reads.
    No API signing key is used or passed to a client.
    """
    reader = reader or AccountReader()
    def observed_time():
        return pd.Timestamp(_number(clock(), nonnegative=True), unit="s", tz="UTC").isoformat()

    snapshot_started = observed_time()
    if values is None:
        path = Path(env_path or Path(__file__).resolve().parents[1] / ".env")
        loaded = dotenv_values(path) if path.exists() else {}
        address_keys = {key for p in HYPERLIQUID_ACCOUNT_PROFILES.values()
                        for key in (*p.wallet_address_env_keys, *p.api_address_env_keys)}
        values = {key: os.environ.get(key) or loaded.get(key, "") for key in address_keys}
        del loaded

    def fetch(profile):
        reads = {}
        def read(kind, address):
            started = observed_time()
            payload = reader.post_info({"type": kind, "user": address})
            reads[kind] = {"started_at_utc": started, "completed_at_utc": observed_time()}
            if isinstance(payload, dict) and type(payload.get("time")) is int:
                reads[kind]["exchange_time_ms"] = payload["time"]
            return payload

        def first(keys):
            candidates = (str(values.get(k) or "").strip().strip("'\"") for k in keys)
            return next((value for value in candidates if value and value.lower() != "key in here"), "")
        owner = first(profile.wallet_address_env_keys)
        if not owner:
            agent = first(profile.api_address_env_keys)
            if not valid_wallet(agent):
                raise ValueError(f"{profile.label}: a valid public owner/API address is required.")
            role = read("userRole", agent)
            if not isinstance(role, dict):
                raise ValueError(f"{profile.label}: invalid public role response.")
            if role.get("role") == "agent" and not isinstance(role.get("data"), dict):
                raise ValueError(f"{profile.label}: API agent has no verified public owner.")
            owner = (role.get("data", {}).get("user", "") if role.get("role") == "agent"
                     else agent if role.get("role") in {"user", "subAccount"} else "")
        if not isinstance(owner, str) or not valid_wallet(owner):
            raise ValueError(f"{profile.label}: could not resolve a public account owner.")
        result = {kind: read(kind, owner) for kind in
                  ("clearinghouseState", "spotClearinghouseState", "userAbstraction",
                   "frontendOpenOrders")}
        perp = result["clearinghouseState"]
        spot = result["spotClearinghouseState"]
        if (not isinstance(perp, dict) or not isinstance(perp.get("assetPositions"), list)
                or not isinstance(spot, dict) or not isinstance(spot.get("balances"), list)
                or not isinstance(result["frontendOpenOrders"], list)):
            raise ValueError(f"{profile.label}: incomplete public account snapshot.")
        summary = perp.get("marginSummary") or perp.get("crossMarginSummary")
        if not isinstance(summary, dict) or "accountValue" not in summary:
            raise ValueError(f"{profile.label}: missing perpetual account valuation.")
        result["read_observations"] = reads
        return profile.key, result

    with ThreadPoolExecutor(max_workers=3) as pool:
        raw = dict(pool.map(fetch, HYPERLIQUID_ACCOUNT_PROFILES.values()))
    needed = set(symbols)
    for account in raw.values():
        for row in account["clearinghouseState"].get("assetPositions", []):
            if _number(row["position"]["szi"]):
                needed.add(row["position"]["coin"])
        for row in account["spotClearinghouseState"].get("balances", []):
            if row["coin"] != "USDC" and _number(row["total"]):
                needed.add(ALIASES.get(row["coin"], row["coin"]))
    quotes_started = observed_time()
    quotes = market_provider.snapshot(sorted(needed))
    quotes_completed = observed_time()
    marks = {key: _number(row["mark"], positive=True) for key, row in quotes["markets"].items()}
    cash, positions, accounts = {}, [], {}
    for name, account in raw.items():
        mode = account["userAbstraction"]
        if mode not in {"unifiedAccount", "portfolioMargin", "default", "standard", "disabled",
                        "dexAbstraction", None}:
            raise ValueError(f"{name}: unsupported account abstraction.")
        perp_state, spot_state = account["clearinghouseState"], account["spotClearinghouseState"]
        reported_upnl = 0.0
        for row in perp_state.get("assetPositions", []):
            pos = row["position"]
            quantity = _number(pos["szi"])
            if not quantity:
                continue
            coin = pos["coin"]
            if (name == "clearpond" or (name == "alex" and quantity > 0)
                    or (name == "jeremy" and quantity < 0)):
                raise ValueError(f"{name}: actual {coin} position conflicts with the requested account role.")
            entry = _number(pos["entryPx"], positive=True)
            upnl = _number(pos["unrealizedPnl"])
            if f"perp:{coin}" not in marks:
                raise ValueError(f"Missing public perp valuation for {coin}.")
            reported_upnl = _number(reported_upnl + upnl)
            positions.append({"account": name, "coin": coin, "kind": "perp",
                              "quantity": quantity, "average_entry": entry,
                              "entry_source": "historical_exchange_entry",
                              "risk_reference_price": marks[f"perp:{coin}"],
                              "risk_reference_source": "opening_mark",
                              "source_unrealized_pnl": upnl,
                              "source_leverage": pos.get("leverage")})
        spot_cash, spot_value = 0.0, 0.0
        for row in spot_state.get("balances", []):
            quantity = _number(row["total"], nonnegative=True)
            if row["coin"] == "USDC":
                spot_cash = _number(spot_cash + quantity)
                continue
            if quantity == 0:
                continue
            coin = ALIASES.get(row["coin"], row["coin"])
            mark = marks.get(f"spot:{coin}")
            if mark is None:
                raise ValueError(f"Missing public spot valuation for inherited {coin}.")
            # Spot cost basis is not reliably provided; start performance at its
            # observed value, preserving exact inventory rather than inventing cost.
            positions.append({"account": name, "coin": coin, "kind": "spot",
                              "quantity": quantity, "average_entry": mark,
                              "risk_reference_price": mark,
                              "risk_reference_source": "opening_mark",
                              "entry_source": "opening_mark_not_historical_cost",
                              "passive_inherited": name != "clearpond"})
            spot_value = _number(spot_value + quantity * mark, nonnegative=True)
        summary = perp_state.get("marginSummary") or perp_state.get("crossMarginSummary") or {}
        perp_value = _number(summary.get("accountValue", 0))
        unified = mode in {"unifiedAccount", "portfolioMargin"}
        # Unified USDC already includes perp P/L. The local ledger tracks P/L
        # separately, so remove it from base collateral to avoid double counting.
        cash[name] = _number(spot_cash - reported_upnl if unified else spot_cash + perp_value - reported_upnl)
        source_equity = _number(spot_cash + spot_value if unified else spot_cash + spot_value + perp_value)
        accounts[name] = {"source_account_mode": mode, "source_spot_usdc": spot_cash,
                          "source_perp_equity": perp_value,
                          "source_perp_unrealized_pnl": reported_upnl,
                          "source_equity": source_equity,
                          "read_observations": account["read_observations"],
                          "open_orders_not_imported": len(account["frontendOpenOrders"]),
                          "base_collateral": cash[name]}
    now_seconds = _number(clock(), nonnegative=True)
    now = pd.Timestamp(now_seconds, unit="s", tz="UTC").isoformat()
    return {"initial_cash": cash, "initial_positions": positions, "initial_marks": marks,
            "now": now, "metadata": {"seed_mode": "mirror", "accounts": accounts,
                                      "snapshot_not_atomic_across_accounts": True,
                                      "snapshot_started_at_utc": snapshot_started,
                                      "snapshot_completed_at_utc": now,
                                      "quote_observation": {
                                          "started_at_utc": quotes_started,
                                          "completed_at_utc": quotes_completed,
                                          "observed_at_utc": quotes.get("observed_at_utc"),
                                          "markets": {key: {field: row[field] for field in
                                              ("mark", "book_time_utc", "observed_at_utc") if field in row}
                                              for key, row in quotes["markets"].items()},
                                      },
                                      "inherited_stop_reference": "opening_mark",
                                      "spot_cost_basis": "opening_mark",
                                      "live_updates_after_seed": False}, "quotes": quotes}
