"""Strict, opt-in mainnet bridge; no live I/O occurs at import/construction.

Contracts verified against installed SDK and official docs, 2026-09-25:
https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api/info-endpoint
https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api/exchange-endpoint
https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api/tick-and-lot-size

Order acknowledgements are never execution evidence. Unknown transport outcomes
require reconciliation; this module never retries a signed exchange request.
V1 excludes subaccounts, vaults, portfolio margin and cross-account transfers.
"""
from decimal import Decimal, ROUND_FLOOR, ROUND_CEILING
import math
import os
import re
import time

from app.hyperliquid_accounts import valid_wallet
from app.services.hyperliquid import HyperliquidInfoClient
from app.services.hyperliquid_trading import HyperliquidTradingConfig, hyperliquid_price_tick
from ml.hyperliquid_paper_market import PublicPaperMarket, SPOT_BASES
from ml.hyperliquid_powder_lock import assert_ownership

ACCOUNTS = ("alex", "jeremy", "clearpond")
MAINNET = "https://api.hyperliquid.xyz"
ALIASES = {value: key for key, value in SPOT_BASES.items()}


class PreSubmitRejected(PermissionError):
    """Submission stopped before any signed request; safe to record REJECTED."""


def finite(value, name, *, positive=False, nonnegative=False):
    if isinstance(value, bool):
        raise ValueError(f"Invalid {name}")
    try:
        value = float(value)
    except (ValueError, TypeError, OverflowError):
        raise ValueError(f"Invalid {name}") from None
    if not math.isfinite(value) or positive and value <= 0 or nonnegative and value < 0:
        raise ValueError(f"Invalid {name}")
    return value


def mapping(value, name):
    if not isinstance(value, dict):
        raise ValueError(f"Missing or malformed {name}")
    return value


def rows(value, name):
    if not isinstance(value, list) or any(not isinstance(row, dict) for row in value):
        raise ValueError(f"Missing or malformed {name}")
    return value


def integer(value, name):
    if type(value) is not int or value < 0:
        raise ValueError(f"Invalid {name}")
    return value


def _signer(secret):
    from eth_account import Account
    try:
        return Account.from_key(secret).address.lower()
    except Exception:
        raise ValueError("Invalid local Hyperliquid signer") from None


class PowderExchange:
    network = MAINNET

    def __init__(self, symbols, max_order_notional=500, max_quote_age_seconds=45,
                 slippage_bps=2, ownership_token=None, *, client=None, market=None,
                 config_factory=HyperliquidTradingConfig, signer=_signer,
                 exchange_factory=None, clock=time.time):
        self.symbols = tuple(symbols)
        if (not self.symbols or len(set(self.symbols)) != len(self.symbols)
                or any(not isinstance(s, str) or not re.fullmatch(r"[A-Z0-9]{1,20}", s) for s in self.symbols)):
            raise ValueError("Use distinct supported uppercase symbols")
        self.max_order_notional = finite(max_order_notional, "order cap", positive=True)
        self.max_quote_age_seconds = finite(max_quote_age_seconds, "quote age", positive=True)
        self.slippage_bps = finite(slippage_bps, "slippage", nonnegative=True)
        if self.slippage_bps >= 10000 or self.max_quote_age_seconds > 45:
            raise ValueError("Unsupported slippage or quote age")
        self.ownership_token = ownership_token
        self.client = client or HyperliquidInfoClient(info_url=MAINNET + "/info", timeout_seconds=10)
        self.market = market or PublicPaperMarket(info_url=MAINNET + "/info", clock=clock)
        self.config_factory, self.signer = config_factory, signer
        self.exchange_factory, self.clock = exchange_factory, clock
        self.pre_submit_check = None
        self.identities = None
        self._bindings = None
        self._configs = {}
        self._markets = {}
        self._submitted = set()

    def _endpoint(self):
        configured = os.getenv("HYPERLIQUID_INFO_URL", MAINNET + "/info").rstrip("/")
        for endpoint in (configured, self.client.info_url, self.market.info_url):
            if endpoint.rstrip("/") != MAINNET + "/info":
                raise PermissionError("Powder read and signing endpoints must both be mainnet")

    def _info(self, kind, **payload):
        self._endpoint()
        return self.client.post_info({"type": kind, **payload})

    def preflight(self):
        self._endpoint()
        identities, bindings, configs = {}, {}, {}
        for account in ACCOUNTS:
            config = self.config_factory(account)
            finite(config.max_live_notional, "live environment order cap", positive=True)
            api = str(config.api_address).lower()
            if not valid_wallet(api) or self.signer(config.api_secret) != api:
                raise PermissionError(f"{account}: signer identity mismatch")
            role = mapping(self._info("userRole", user=api), "agent role")
            if role.get("role") != "agent":
                raise PermissionError(f"{account}: a verified API agent is required")
            owner = str(mapping(role.get("data"), "agent owner").get("user", "")).lower()
            if not valid_wallet(owner):
                raise PermissionError(f"{account}: invalid owner mapping")
            explicit = str(config.wallet_address or "").lower()
            if explicit and explicit != owner:
                raise PermissionError(f"{account}: configured owner differs from agent authority")
            owner_role = mapping(self._info("userRole", user=owner), "owner role")
            if owner_role.get("role") != "user":
                raise PermissionError("Subaccount and vault execution are unsupported")
            identities[account], bindings[account], configs[account] = owner, (owner, api), config
        if len(set(identities.values())) != len(ACCOUNTS):
            raise PermissionError("Powder account owners must be distinct")
        if self._bindings is not None and bindings != self._bindings:
            raise PermissionError("Account identity changed after preflight")
        self.identities, self._bindings, self._configs = identities, bindings, configs
        return dict(identities)

    def observe(self):
        identities = self.preflight()
        started = self.clock()
        raw, needed = {}, set(self.symbols)
        for account, owner in identities.items():
            perp = mapping(self._info("clearinghouseState", user=owner), "perpetual state")
            spot = mapping(self._info("spotClearinghouseState", user=owner), "spot state")
            mode = self._info("userAbstraction", user=owner)
            if mode not in {"unifiedAccount", "default", "standard", "disabled"}:
                raise PermissionError("Unsupported or unknown account abstraction")
            inventory = rows(perp.get("assetPositions"), "perpetual positions")
            balances = rows(spot.get("balances"), "spot balances")
            orders = rows(self._info("frontendOpenOrders", user=owner), "open orders")
            if orders:
                raise PermissionError("Open exchange orders require reconciliation before trading")
            for item in inventory:
                position = mapping(item.get("position"), "position")
                qty = finite(position.get("szi"), "perpetual quantity")
                coin = position.get("coin")
                if qty and (coin not in self.symbols or account == "clearpond"
                            or account == "alex" and qty > 0 or account == "jeremy" and qty < 0):
                    raise PermissionError("Held perpetual position violates configured account role/universe")
            for balance in balances:
                qty = finite(balance.get("total"), "spot total", nonnegative=True)
                coin = balance.get("coin")
                symbol = ALIASES.get(coin, coin)
                if qty and coin != "USDC":
                    if account == "clearpond" and symbol not in self.symbols:
                        raise PermissionError("Unexpected managed spot asset")
                    if not isinstance(symbol, str) or not re.fullmatch(r"[A-Z0-9]{1,20}", symbol):
                        raise PermissionError("Unsupported inherited spot asset")
                    needed.add(symbol)
            raw[account] = (perp, spot, mode)
        quote = mapping(self.market.snapshot(sorted(needed)), "market snapshot")
        markets = mapping(quote.get("markets"), "market marks")
        now = self.clock()
        if now - started > self.max_quote_age_seconds:
            raise ValueError("Account observation exceeded freshness budget")
        accounts = {}
        for account, (perp, spot, mode) in raw.items():
            positions = {}
            for symbol in self.symbols:
                kind = "spot" if account == "clearpond" else "perp"
                q = self._quote(markets, kind, symbol, now)
                positions[symbol] = {"quantity": 0.0, "mark": q["mark"], "entry_price": None, "kind": kind}
            gross, passive_positions = 0.0, {}
            for item in perp["assetPositions"]:
                p = item["position"]
                qty = finite(p["szi"], "quantity")
                if not qty:
                    continue
                target = positions[p["coin"]]
                if target["quantity"]:
                    raise ValueError("Duplicate perpetual position")
                target.update(quantity=qty, entry_price=finite(p.get("entryPx"), "entry price", positive=True))
                finite(p.get("unrealizedPnl"), "perpetual P/L")
                gross += abs(qty) * target["mark"]
            summary = mapping(perp.get("marginSummary"), "margin summary")
            perp_equity = finite(summary.get("accountValue"), "perpetual equity")
            available = finite(perp.get("withdrawable"), "withdrawable", nonnegative=True)
            # A valid complete balances array may omit zero-balance tokens.
            # Missing/malformed balances itself was rejected above.
            spot_equity, spot_cash = 0.0, 0.0
            maintenance = spot.get("tokenToAvailableAfterMaintenance")
            if mode == "unifiedAccount" and not isinstance(maintenance, list):
                raise ValueError("Missing unified maintenance availability")
            maintenance_by_token = {}
            for pair in maintenance or []:
                if not isinstance(pair, list) or len(pair) != 2 or pair[0] in maintenance_by_token:
                    raise ValueError("Malformed maintenance availability")
                maintenance_by_token[pair[0]] = finite(pair[1], "maintenance availability", nonnegative=True)
            seen, seen_symbols = set(), set()
            for balance in spot["balances"]:
                coin = balance["coin"]
                if coin in seen:
                    raise ValueError("Duplicate spot balance")
                seen.add(coin)
                qty = finite(balance["total"], "spot total", nonnegative=True)
                hold = finite(balance.get("hold"), "spot hold", nonnegative=True)
                if hold > qty:
                    raise ValueError("Spot hold exceeds balance")
                if coin == "USDC":
                    spot_equity += qty
                    spot_cash = qty - hold
                    if mode == "unifiedAccount":
                        token = balance.get("token")
                        if token not in maintenance_by_token:
                            raise ValueError("Missing USDC maintenance availability")
                        spot_cash = min(spot_cash, maintenance_by_token[token])
                elif qty:
                    symbol = ALIASES.get(coin, coin)
                    if symbol in seen_symbols:
                        raise ValueError("Duplicate spot asset alias")
                    seen_symbols.add(symbol)
                    q = self._quote(markets, "spot", symbol, now)
                    spot_equity += qty * q["mark"]
                    gross += qty * q["mark"]
                    if account == "clearpond":
                        free = qty - hold
                        if mode == "unifiedAccount":
                            token = balance.get("token")
                            if token not in maintenance_by_token:
                                raise ValueError("Missing managed token maintenance availability")
                            free = min(free, maintenance_by_token[token])
                        positions[symbol].update(quantity=qty, available_quantity=free)
                    else:
                        passive_positions[symbol] = {"quantity": qty, "mark": q["mark"]}
            equity = spot_equity if mode == "unifiedAccount" else spot_equity + perp_equity
            accounts[account] = {"equity": finite(equity, "marked account equity"),
                                 "available_cash": spot_cash if mode == "unifiedAccount" or account == "clearpond" else available,
                                 "gross": finite(gross, "gross exposure", nonnegative=True), "positions": positions, "passive_positions": passive_positions,
                                 "account_mode": mode}
        self._markets = markets
        return {"observed_at": started, "started_at": started, "completed_at": now,
                "identities": identities, "accounts": accounts, "open_orders": []}

    def _order_cap(self, account):
        config = self._configs.get(account)
        if config is None:
            raise PermissionError("Verified account configuration is required")
        return min(self.max_order_notional, finite(config.max_live_notional, "live environment order cap", positive=True))

    def _evidence(self, observation):
        """Pin quantities and free collateral, not fluctuating mark prices."""
        result = {"observed_at": finite(observation.get("observed_at"), "observation time"),
                  "identities": dict(mapping(observation.get("identities"), "observed identities")), "accounts": {}}
        for account in ACCOUNTS:
            state = observation["accounts"][account]
            result["accounts"][account] = {
                "available_cash": finite(state.get("available_cash"), "available cash", nonnegative=True),
                "account_mode": state.get("account_mode"),
                "positions": {s: finite(p.get("quantity"), "observed quantity") for s, p in state["positions"].items()},
                "available_quantities": {s: finite(p.get("available_quantity", abs(p["quantity"])), "available quantity", nonnegative=True)
                                         for s, p in state["positions"].items()},
                "passive_positions": {s: finite(p.get("quantity"), "passive quantity") for s, p in state.get("passive_positions", {}).items()},
            }
        return result

    def _quote(self, markets, kind, symbol, now):
        q = dict(mapping(markets.get(f"{kind}:{symbol}"), f"{kind}:{symbol} quote"))
        if q.get("coin") != symbol or q.get("kind") != kind:
            raise ValueError("Market identity mismatch")
        q["mark"] = finite(q.get("mark"), "mark", positive=True)
        from datetime import datetime
        try:
            stamp = datetime.fromisoformat(q["book_time_utc"].replace("Z", "+00:00"))
            if stamp.tzinfo is None or not -5 <= now - stamp.timestamp() <= self.max_quote_age_seconds:
                raise ValueError()
        except (KeyError, TypeError, ValueError):
            raise ValueError("Stale or missing executable quote") from None
        decimals = q.get("sz_decimals")
        if type(decimals) is not int or not 0 <= decimals <= 8:
            raise ValueError("Unknown market size precision")
        if not isinstance(q.get("wire_coin"), str) or not q["wire_coin"]:
            raise ValueError("Missing executable market route")
        return q

    def make_order(self, account, symbol, delta_notional, reduce_only, observation):
        if account not in ACCOUNTS or symbol not in self.symbols or type(reduce_only) is not bool:
            raise ValueError("Unknown order account/market")
        if observation.get("identities") != self.identities:
            raise PermissionError("Observation account binding differs")
        now = self.clock()
        if not 0 <= now - finite(observation.get("observed_at"), "observation time") <= self.max_quote_age_seconds:
            raise ValueError("Stale account observation")
        delta = finite(delta_notional, "notional change")
        if not delta:
            raise ValueError("No executable size")
        kind = "spot" if account == "clearpond" else "perp"
        q = self._quote(self._markets, kind, symbol, now)
        is_buy = delta > 0
        levels = q.get("asks" if is_buy else "bids")
        if not isinstance(levels, list) or not levels or not isinstance(levels[0], list) or len(levels[0]) != 2:
            raise ValueError("No executable depth")
        price = Decimal(str(finite(levels[0][0], "book price", positive=True)))
        price *= Decimal(1) + Decimal(str(self.slippage_bps / 10000)) * (1 if is_buy else -1)
        tick = hyperliquid_price_tick(price, size_decimals=q["sz_decimals"], spot=kind == "spot")
        price = (price / tick).to_integral_value(rounding=ROUND_FLOOR if is_buy else ROUND_CEILING) * tick
        if price <= 0:
            raise ValueError("Invalid rounded price")
        qty = min(Decimal(str(abs(delta))) / Decimal(str(q["mark"])), Decimal(str(self._order_cap(account))) / price)
        if not reduce_only and not (kind == "spot" and not is_buy):
            qty = min(qty, Decimal(str(abs(delta))) / price)
        current = observation["accounts"][account]["positions"][symbol]
        if reduce_only or kind == "spot" and not is_buy:
            existing = finite(current.get("quantity"), "held quantity")
            if not existing or existing * delta >= 0:
                raise ValueError("Requested reduction does not reduce a held position")
            available = abs(existing) if kind == "perp" else finite(current.get("available_quantity", existing), "available spot", nonnegative=True)
            qty = min(qty, Decimal(str(available)))
        qty = qty.quantize(Decimal(1).scaleb(-q["sz_decimals"]), rounding=ROUND_FLOOR)
        if qty <= 0 or qty * price < 10:
            raise ValueError("Order is below minimum size or $10 notional")
        return {"account": account, "symbol": symbol, "coin": q["wire_coin"], "is_buy": is_buy,
                "size": float(qty), "limit_price": float(price), "reduce_only": reduce_only and kind == "perp", "tif": "Ioc",
                "observation": self._evidence(observation)}

    def _request(self, intent, *, enforce_cap=False):
        request = mapping(intent.get("request"), "intent request")
        if not re.fullmatch(r"0x[0-9a-fA-F]{32}", str(intent.get("cloid", ""))):
            raise ValueError("Invalid durable client order identity")
        if request.get("account") not in ACCOUNTS or request.get("symbol") not in self.symbols:
            raise ValueError("Unknown intent account/market")
        if request.get("tif") != "Ioc" or type(request.get("is_buy")) is not bool or type(request.get("reduce_only")) is not bool:
            raise ValueError("Powder only submits explicit IOC requests")
        size = finite(request.get("size"), "size", positive=True)
        price = finite(request.get("limit_price"), "limit price", positive=True)
        if size * price < 10 or enforce_cap and size * price > self._order_cap(request["account"]) + 1e-8:
            raise PermissionError("Order notional is outside configured bounds")
        finite(intent.get("created_at"), "intent time", positive=True)
        return request

    def _submission_guard(self, intent):
        request = self._request(intent, enforce_cap=True)
        account = request["account"]
        assert_ownership(self.ownership_token, account)
        if any(os.getenv(key, "").lower() != "true" for key in ("HYPERLIQUID_ENABLE_POWDER", "HYPERLIQUID_ENABLE_LIVE_ORDERS")):
            raise PermissionError("Explicit session Powder and live-order gates are required")
        if not 0 <= self.clock() - intent["created_at"] <= self.max_quote_age_seconds:
            raise ValueError("Stale or future submission intent")
        evidence = mapping(request.get("observation"), "planning observation")
        if not 0 <= self.clock() - finite(evidence.get("observed_at"), "planning observation time") <= self.max_quote_age_seconds:
            raise ValueError("Stale planning account observation")
        if account == "clearpond" and request["is_buy"] or account != "clearpond" and not request["reduce_only"]:
            if self.clock() >= finite(request.get("forecast_valid_until"), "forecast deadline", positive=True):
                raise ValueError("Forecast expired before submission")
        kind = "spot" if account == "clearpond" else "perp"
        quote = self._quote(self._markets, kind, request["symbol"], self.clock())
        if request["coin"] != quote["wire_coin"]:
            raise PermissionError("Order route differs from verified market")
        size, price = Decimal(str(request["size"])), Decimal(str(request["limit_price"]))
        step = Decimal(1).scaleb(-quote["sz_decimals"])
        tick = hyperliquid_price_tick(price, size_decimals=quote["sz_decimals"], spot=kind == "spot")
        if size % step or price % tick:
            raise ValueError("Order violates exchange precision")
        if (account == "alex" and request["is_buy"] and not request["reduce_only"]
                or account == "jeremy" and not request["is_buy"] and not request["reduce_only"]
                or account == "clearpond" and request["reduce_only"]):
            raise PermissionError("Order violates account role")
        if intent["cloid"] in self._submitted:
            raise PermissionError("Submission already attempted; reconcile instead")
        return request

    def _prepare_submission(self, intent):
        request = self._request(intent)
        assert_ownership(self.ownership_token, request["account"])
        if any(os.getenv(key, "").lower() != "true" for key in ("HYPERLIQUID_ENABLE_POWDER", "HYPERLIQUID_ENABLE_LIVE_ORDERS")):
            raise PermissionError("Explicit session Powder and live-order gates are required")
        self.preflight()
        request = self._submission_guard(intent)
        account = request["account"]
        config = self._configs[account]
        if self.exchange_factory:
            exchange = self.exchange_factory(config, self.identities[account])
        else:
            from eth_account import Account
            from hyperliquid.exchange import Exchange
            exchange = Exchange(Account.from_key(config.api_secret), MAINNET,
                                account_address=self.identities[account], timeout=10)
        if getattr(exchange, "base_url", None) != MAINNET or getattr(exchange, "vault_address", None) is not None:
            raise PermissionError("Unexpected signing network or vault routing")
        # SDK construction itself fetches metadata. Re-read actual inventory and
        # collateral afterwards, then validate the full age budget at the boundary.
        actual = self.observe()
        current, planned = self._evidence(actual), request["observation"]
        if current["identities"] != planned.get("identities"):
            raise PermissionError("Planning account identity changed")
        for name in ACCOUNTS:
            before = mapping(planned.get("accounts", {}).get(name), "planning account")
            after = current["accounts"][name]
            if any(before.get(key) != after[key] for key in ("positions", "passive_positions", "account_mode")):
                raise PermissionError("Actual account inventory changed after planning")
        # Falling collateral is expected during a stop. The required policy
        # callback evaluates current cash; it must not block a valid reduction
        # merely because cash declined since planning.
        position = actual["accounts"][account]["positions"][request["symbol"]]
        if request["reduce_only"] or account == "clearpond" and not request["is_buy"]:
            quantity = finite(position.get("quantity"), "actual held quantity")
            if quantity * (1 if request["is_buy"] else -1) >= 0:
                raise PermissionError("Actual position does not support this reduction")
            available = abs(quantity) if account != "clearpond" else finite(position.get("available_quantity"), "actual available base", nonnegative=True)
            if request["size"] > available + 1e-10:
                raise PermissionError("Reduction exceeds actual available quantity")
        if not callable(self.pre_submit_check):
            raise PermissionError("Runtime policy validation callback is required")
        self.pre_submit_check(request, actual)
        self._submission_guard(intent)
        kind = "spot" if account == "clearpond" else "perp"
        quote = self._quote(self._markets, kind, request["symbol"], self.clock())
        levels = quote.get("asks" if request["is_buy"] else "bids")
        best = finite(levels[0][0], "fresh executable price", positive=True)
        tolerance = best * self.slippage_bps / 10000
        if (request["is_buy"] and request["limit_price"] > best + tolerance + 1e-8
                or not request["is_buy"] and request["limit_price"] < best - tolerance - 1e-8):
            raise PermissionError("IOC limit exceeds the fresh quote slippage bound")
        return exchange, request

    def submit(self, intent):
        try:
            exchange, request = self._prepare_submission(intent)
        except Exception as exc:
            raise PreSubmitRejected(f"Pre-submit validation: {type(exc).__name__}: {exc}") from None
        # No retry is safe after entering this boundary, including transport errors.
        self._submitted.add(intent["cloid"])
        from hyperliquid.utils.types import Cloid
        return exchange.order(request["coin"], request["is_buy"], request["size"],
                              request["limit_price"], {"limit": {"tif": "Ioc"}},
                              reduce_only=request["reduce_only"], cloid=Cloid.from_str(intent["cloid"]))

    def reconcile(self, intent):
        result = {"state": "UNKNOWN", "fills": [], "oid": None, "detail": "Unproven exchange outcome"}
        try:
            request = self._request(intent)
            self.preflight()
            account, cloid = request["account"], intent["cloid"]
            user = self.identities[account]
            status = mapping(self._info("orderStatus", user=user, oid=cloid), "order status")
            if status.get("status") != "order":
                return result
            wrapper = mapping(status.get("order"), "order status detail")
            order = mapping(wrapper.get("order"), "order identity")
            oid = integer(order.get("oid"), "order id")
            if (order.get("cloid") != cloid or order.get("coin") != request["coin"]
                    or order.get("side") != ("B" if request["is_buy"] else "A")
                    or not math.isclose(finite(order.get("origSz"), "original size"), request["size"], rel_tol=1e-8, abs_tol=1e-10)):
                raise ValueError("Exchange order identity does not match durable intent")
            result["oid"] = oid
            start = max(0, int((intent["created_at"] - 5) * 1000))
            end = int((self.clock() + 5) * 1000)
            history = rows(self._info("userFillsByTime", user=user, startTime=start,
                                      endTime=end, aggregateByTime=False), "fill history")
            # Time-range caps can hide rows even if no matching oid is visible.
            if len(history) >= 500:
                raise ValueError("Fill history may be truncated; operator reconciliation required")
            found = {}
            for fill in history:
                if fill.get("oid") != oid:
                    continue
                tid = integer(fill.get("tid"), "trade identity")
                when = integer(fill.get("time"), "fill timestamp")
                if not start <= when <= end or fill.get("coin") != request["coin"] or fill.get("side") != order["side"]:
                    raise ValueError("Fill identity/time mismatch")
                qty = finite(fill.get("sz"), "fill size", positive=True)
                price = finite(fill.get("px"), "fill price", positive=True)
                if request["is_buy"] and price > request["limit_price"] + 1e-8 or not request["is_buy"] and price < request["limit_price"] - 1e-8:
                    raise ValueError("Fill violates the IOC price limit")
                fee = finite(fill.get("fee"), "actual fill fee")
                fee_token = fill.get("feeToken")
                if not isinstance(fee_token, str) or not fee_token:
                    raise ValueError("Actual fee token is unavailable")
                allowed_fees = {"USDC"}
                if account == "clearpond":
                    allowed_fees.update((request["symbol"], SPOT_BASES.get(request["symbol"], request["symbol"])))
                if fee_token not in allowed_fees:
                    raise ValueError("Unsupported execution fee currency")
                normalized = {"account": account, "symbol": request["symbol"], "coin": fill["coin"],
                              "oid": oid, "tid": tid, "time": when,
                              "quantity": qty if request["is_buy"] else -qty,
                              "price": price, "fee": fee, "fee_token": fee_token, "raw": fill}
                if tid in found and normalized != found[tid]:
                    raise ValueError("Conflicting duplicate fill")
                found[tid] = normalized
            total = sum(abs(fill["quantity"]) for fill in found.values())
            if total > request["size"] + max(1e-10, request["size"] * 1e-8):
                raise ValueError("Actual fills exceed intended quantity")
            state = wrapper.get("status")
            if state == "filled":
                if not found or not math.isclose(total, request["size"], rel_tol=1e-8, abs_tol=1e-10):
                    raise ValueError("Filled status lacks complete actual fill evidence")
                mapped = "FILLED"
            elif state == "open":
                mapped = "OPEN"
            elif isinstance(state, str) and (state == "canceled" or state.endswith("Canceled") or state == "scheduledCancel"):
                remaining = finite(order.get("sz"), "canceled remainder", nonnegative=True)
                if not math.isclose(total + remaining, request["size"], rel_tol=1e-8, abs_tol=1e-10):
                    raise ValueError("Canceled status lacks complete fill/remainder evidence")
                mapped = "CANCELED"
            elif isinstance(state, str) and (state == "rejected" or state.endswith("Rejected")):
                if found:
                    raise ValueError("Rejected order unexpectedly has fills")
                mapped = "REJECTED"
            else:
                return result
            return {"state": mapped, "fills": list(found.values()), "oid": oid, "detail": state}
        except Exception as exc:
            result["detail"] = f"Reconciliation unavailable: {type(exc).__name__}"
            return result
