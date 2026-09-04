from __future__ import annotations

import hashlib
import json
import re
import secrets
import threading
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Iterable
from urllib.parse import urlencode

import requests
from filelock import Timeout as FileLockTimeout

from app.models.portfolio import CashBalance, Holding, PortfolioSnapshot
from app.config import SchwabConfig, schwab_config
from app.services.schwab_policy_inputs import (
    SCHWAB_TERMINAL_ORDER_STATUSES,
    normalize_schwab_policy_inputs,
)
from app.services.schwab_option_management import enrich_option_position_quotes
from app.services.schwab_token_store import (
    OAUTH_REAUTHORIZATION_ERROR_CODES,
    access_token_is_fresh,
    cached_access_token_expires_at,
    has_uncertain_refresh_attempt,
    _load_token_payload_unlocked,
    load_token_payload,
    locked_token_cache,
    refresh_reauthorization_error_code,
    refresh_reauthorization_required,
    refresh_token_is_available,
    save_token_payload,
    write_refresh_attempt,
    write_token_payload_atomic,
)

TRADER_BASE_URL = "https://api.schwabapi.com/trader/v1"
MARKETDATA_BASE_URL = "https://api.schwabapi.com/marketdata/v1"
AUTH_URL = "https://api.schwabapi.com/v1/oauth/authorize"
TOKEN_URL = "https://api.schwabapi.com/v1/oauth/token"

SCHWAB_GTC_ORDER_LOOKBACK_DAYS = 180
SCHWAB_ORDER_QUERY_WINDOW_DAYS = 60
SCHWAB_ORDER_QUERY_MAX_RESULTS = 3_000
_SAFE_OAUTH_ERROR_CODES = frozenset(
    {
        "invalid_client",
        "invalid_grant",
        "invalid_request",
        "invalid_scope",
        "unauthorized_client",
        "unsupported_grant_type",
        "unsupported_token_type",
    }
)
_SAFE_SCHWAB_CLIENT_CORRELID = re.compile(
    r"\A[A-Za-z0-9][A-Za-z0-9._-]{0,127}\Z"
)


@dataclass(frozen=True)
class SchwabOrderSubmissionContext:
    """Account identity and credential generation frozen before a final gate."""

    account_hash: str
    access_token: str = field(repr=False)
    identity_fingerprint: str


class SchwabSession:
    def __init__(self, config: SchwabConfig | None = None) -> None:
        self.config = config or schwab_config()
        self.access_token: str | None = None
        self.access_token_expires_at: datetime | None = None
        self.refresh_token: str | None = None
        self.account_hash: str | None = None
        self._access_token_lock = threading.RLock()
        self._account_hash_lock = threading.Lock()
        self._hydrate_from_cache()

    def build_authorization_url(self) -> tuple[str, str]:
        # Schwab's official flow returns state with the authorization code.  A
        # high-entropy value binds the callback to this exact login attempt.
        state = secrets.token_urlsafe(24)
        params = {
            "response_type": "code",
            "client_id": self.config.client_id,
            "redirect_uri": self.config.redirect_uri,
            "state": state,
        }
        return f"{AUTH_URL}?{urlencode(params)}", state

    def exchange_authorization_code(self, authorization_code: str) -> None:
        cleaned_authorization_code = str(authorization_code).strip()
        if not cleaned_authorization_code:
            raise ValueError("Schwab authorization code is required.")

        retry_safe = False
        try:
            with self._session_lock("_access_token_lock"):
                with locked_token_cache():
                    cached_payload = _load_token_payload_unlocked(strict=True) or {}
                    marked_payload = _publish_token_mutation_marker(cached_payload)
                    attempt_id = str(marked_payload["refresh_attempt"]["id"])
                    try:
                        response = requests.post(
                            TOKEN_URL,
                            headers={
                                "Content-Type": "application/x-www-form-urlencoded"
                            },
                            data={
                                "grant_type": "authorization_code",
                                "code": cleaned_authorization_code,
                                "redirect_uri": self.config.redirect_uri,
                            },
                            auth=(self.config.client_id, self.config.client_secret),
                            timeout=10,
                        )
                        _raise_for_oauth_status(response)
                        payload = response.json()
                        if not isinstance(payload, dict) or not str(
                            payload.get("refresh_token") or ""
                        ).strip():
                            raise ValueError(
                                "Schwab authorization response did not include a "
                                "refresh token"
                            )
                        self._store_token_payload(
                            payload,
                            previous_refresh_token=None,
                        )
                    except Exception as exchange_error:
                        retry_safe = _token_mutation_retry_safe(exchange_error)
                        recovered_payload = _record_token_mutation_failure(
                            cached_payload=cached_payload,
                            marked_payload=marked_payload,
                            attempt_id=attempt_id,
                            error=exchange_error,
                        )
                        if recovered_payload is not None:
                            self._adopt_cached_payload(recovered_payload)
                            self._token_cache_bound = True
                            return
                        raise
        except Exception as exc:
            # Authorization codes are one-time credentials. Only a definite
            # pre-connect timeout is safe for an outer caller to repeat.
            setattr(
                exc,
                "schwab_retry_safe",
                retry_safe
                or isinstance(exc, FileLockTimeout)
                or getattr(exc, "schwab_retry_safe", None) is True,
            )
            setattr(exc, "stock_trader_operation", "authentication")
            raise

    def ensure_access_token(self) -> None:
        with self._session_lock("_access_token_lock"):
            # Hand-built test/fake sessions are not backed by the shared cache.
            # Real sessions always consult it so an uncertain OAuth mutation
            # cannot be ignored just because an in-memory token appears fresh.
            if (
                not getattr(self, "_token_cache_bound", False)
                and self._access_token_is_current()
            ):
                return
            self.refresh_access_token()

    def refresh_access_token(self) -> None:
        retry_safe = False
        with self._session_lock("_access_token_lock"):
            try:
                with locked_token_cache():
                    cached_payload = _load_token_payload_unlocked(strict=True)
                    if has_uncertain_refresh_attempt(cached_payload):
                        raise RuntimeError(
                            "Schwab token refresh has an uncertain prior outcome; "
                            "authorize Schwab again before making broker requests."
                        )
                    if refresh_reauthorization_required(cached_payload):
                        oauth_error_code = refresh_reauthorization_error_code(
                            cached_payload
                        )
                        error = RuntimeError(
                            _reauthorization_required_message(oauth_error_code)
                        )
                        setattr(error, "schwab_oauth_error_code", oauth_error_code)
                        setattr(error, "schwab_reauthorization_required", True)
                        raise error
                    self._adopt_cached_payload(cached_payload)
                    if self._access_token_is_current():
                        return
                    if not refresh_token_is_available(cached_payload):
                        raise RuntimeError("Schwab refresh token is not available.")

                    refresh_token = str(cached_payload["refresh_token"])
                    marked_payload = _publish_token_mutation_marker(cached_payload)
                    attempt = marked_payload["refresh_attempt"]
                    attempt_id = str(attempt["id"])
                    try:
                        response = requests.post(
                            TOKEN_URL,
                            headers={
                                "Content-Type": "application/x-www-form-urlencoded"
                            },
                            data={
                                "grant_type": "refresh_token",
                                "refresh_token": refresh_token,
                            },
                            auth=(self.config.client_id, self.config.client_secret),
                            timeout=10,
                        )
                        _raise_for_oauth_status(response)
                        payload = response.json()
                        self._store_token_payload(
                            payload,
                            previous_refresh_token=refresh_token,
                            previous_payload=cached_payload,
                        )
                    except Exception as refresh_error:
                        retry_safe = _token_mutation_retry_safe(refresh_error)
                        recovered_payload = _record_token_mutation_failure(
                            cached_payload=cached_payload,
                            marked_payload=marked_payload,
                            attempt_id=attempt_id,
                            error=refresh_error,
                            persist_reauthorization_rejection=True,
                        )
                        if recovered_payload is not None:
                            self._adopt_cached_payload(recovered_payload)
                            self._token_cache_bound = True
                            return
                        raise
            except Exception as exc:
                # A failed or timed-out token refresh may have rotated broker
                # state, so consumers must not blindly repeat it.
                setattr(
                    exc,
                    "schwab_retry_safe",
                    retry_safe
                    or isinstance(exc, FileLockTimeout)
                    or getattr(exc, "schwab_retry_safe", None) is True,
                )
                setattr(exc, "stock_trader_operation", "authentication")
                raise

    def get_account(self) -> Any:
        account_hash, headers = self._account_request_context()
        response = requests.get(
            f"{TRADER_BASE_URL}/accounts/{account_hash}",
            headers=headers,
            params={"fields": "positions"},
            timeout=10,
        )
        response.raise_for_status()
        return response.json()

    def _get_account_hash(self) -> str:
        with self._session_lock("_access_token_lock"):
            self.ensure_access_token()
            return self._get_account_hash_for_current_token()

    def _get_account_hash_for_current_token(self) -> str:
        if self.account_hash:
            return self.account_hash

        with self._session_lock("_account_hash_lock"):
            if self.account_hash:
                return self.account_hash

            response = requests.get(
                f"{TRADER_BASE_URL}/accounts/accountNumbers",
                headers=self._headers_for_current_token(),
                timeout=10,
            )
            response.raise_for_status()

            accounts = response.json()
            if not isinstance(accounts, list) or not accounts:
                raise RuntimeError("No Schwab accounts returned.")

            account_hash = accounts[0].get("hashValue")
            if not account_hash:
                raise RuntimeError("Schwab account hashValue was missing.")

            self.account_hash = str(account_hash)
            return self.account_hash

    def _headers(self) -> dict[str, str]:
        with self._session_lock("_access_token_lock"):
            self.ensure_access_token()
            return self._headers_for_current_token()

    def _headers_for_current_token(self) -> dict[str, str]:
        if not self.access_token:
            raise RuntimeError("Schwab access token is not available.")
        return {
            "Authorization": f"Bearer {self.access_token}",
            "Accept": "application/json",
        }

    def _account_request_context(self) -> tuple[str, dict[str, str]]:
        """Capture an account hash and header from one token generation."""

        with self._session_lock("_access_token_lock"):
            self.ensure_access_token()
            account_hash = self._get_account_hash_for_current_token()
            return account_hash, self._headers_for_current_token()

    def _identity_fingerprint_for_current_token(self, account_hash: str) -> str:
        """Return a non-secret binding for one account and OAuth grant."""

        credential_generation = self.refresh_token or self.access_token
        if not credential_generation:
            raise RuntimeError("Schwab credential generation is unavailable.")
        material = f"{account_hash}\0{credential_generation}".encode("utf-8")
        return hashlib.sha256(material).hexdigest()

    def _session_lock(self, attribute: str) -> Any:
        lock = getattr(self, attribute, None)
        if lock is None:
            lock = (
                threading.RLock()
                if attribute == "_access_token_lock"
                else threading.Lock()
            )
            setattr(self, attribute, lock)
        return lock

    def _hydrate_from_cache(self) -> None:
        self._adopt_cached_payload(load_token_payload())
        self._token_cache_bound = True

    def _adopt_cached_payload(self, cached_payload: dict[str, Any] | None) -> None:
        previous_credentials = (
            getattr(self, "access_token", None),
            getattr(self, "refresh_token", None),
        )
        self.access_token = None
        self.access_token_expires_at = None
        self.refresh_token = None
        if cached_payload:
            if access_token_is_fresh(cached_payload):
                self.access_token = str(cached_payload["access_token"])
                self.access_token_expires_at = cached_access_token_expires_at(
                    cached_payload
                )

            if refresh_token_is_available(cached_payload):
                self.refresh_token = str(cached_payload["refresh_token"])

        if previous_credentials != (self.access_token, self.refresh_token):
            self.account_hash = None

    def _access_token_is_current(self) -> bool:
        if not self.access_token:
            return False

        if self.access_token_expires_at is None:
            return self.refresh_token is None

        return self.access_token_expires_at > datetime.now(timezone.utc)

    def _store_token_payload(
        self,
        payload: dict[str, Any],
        previous_refresh_token: str | None,
        *,
        previous_payload: dict[str, Any] | None = None,
    ) -> None:
        cached_payload = save_token_payload(
            payload,
            previous_refresh_token,
            previous_payload=previous_payload,
        )
        self._adopt_cached_payload(cached_payload)
        self._token_cache_bound = True

    def get_open_orders(self) -> Any:
        now = datetime.now(timezone.utc)
        horizon_start = now - timedelta(days=SCHWAB_GTC_ORDER_LOOKBACK_DAYS)
        orders_by_key: dict[tuple[str, str], Any] = {}
        window_start = horizon_start

        while window_start < now:
            window_end = min(
                window_start + timedelta(days=SCHWAB_ORDER_QUERY_WINDOW_DAYS),
                now,
            )
            window_orders = self.get_orders(
                from_entered_time=window_start,
                to_entered_time=window_end,
                max_results=SCHWAB_ORDER_QUERY_MAX_RESULTS,
            )
            if not isinstance(window_orders, list):
                raise RuntimeError(
                    "Schwab order-history window returned a non-list payload; "
                    "current working orders are unavailable."
                )
            if len(window_orders) >= SCHWAB_ORDER_QUERY_MAX_RESULTS:
                raise RuntimeError(
                    "Schwab order-history window reached the "
                    f"{SCHWAB_ORDER_QUERY_MAX_RESULTS}-row maxResults cap for "
                    f"{window_start.isoformat()} through {window_end.isoformat()}; "
                    "the response may be truncated, so current working orders are unavailable."
                )

            for order in window_orders:
                orders_by_key[_schwab_order_dedup_key(order)] = order
            window_start = window_end

        current_orders: list[Any] = []
        for order in orders_by_key.values():
            if isinstance(order, dict):
                status = str(order.get("status") or "").strip().upper()
                if status in SCHWAB_TERMINAL_ORDER_STATUSES:
                    continue
            current_orders.append(order)
        return current_orders

    def get_recent_orders(self) -> Any:
        now = datetime.now(timezone.utc)
        return self.get_orders(
            from_entered_time=now - timedelta(days=14),
            to_entered_time=now,
        )

    def get_orders(
        self,
        *,
        from_entered_time: datetime,
        to_entered_time: datetime,
        status: str | None = None,
        max_results: int | None = None,
    ) -> Any:
        account_hash, headers = self._account_request_context()
        params = {
            "fromEnteredTime": from_entered_time.astimezone(timezone.utc).isoformat(timespec="seconds"),
            "toEnteredTime": to_entered_time.astimezone(timezone.utc).isoformat(timespec="seconds"),
        }
        if status:
            params["status"] = status
        if max_results is not None:
            params["maxResults"] = max_results

        response = requests.get(
            f"{TRADER_BASE_URL}/accounts/{account_hash}/orders",
            headers=headers,
            params=params,
            timeout=10,
        )
        response.raise_for_status()
        return response.json()

    def prepare_read_snapshot(self) -> str:
        """Initialize shared auth/account state before concurrent broker reads."""

        with self._session_lock("_access_token_lock"):
            self.ensure_access_token()
            try:
                account_hash = self._get_account_hash_for_current_token()
                return self._identity_fingerprint_for_current_token(account_hash)
            except Exception as exc:
                if not getattr(exc, "stock_trader_operation", None):
                    setattr(exc, "stock_trader_operation", "account_identity")
                raise

    def verify_read_snapshot(self, expected_identity_fingerprint: str) -> None:
        """Reject a snapshot assembled across an account/OAuth generation change."""

        with self._session_lock("_access_token_lock"):
            self.ensure_access_token()
            account_hash = self._get_account_hash_for_current_token()
            observed = self._identity_fingerprint_for_current_token(account_hash)
        if not secrets.compare_digest(observed, expected_identity_fingerprint):
            error = RuntimeError(
                "Schwab account identity or authorization changed during "
                "broker-state capture."
            )
            error.schwab_retry_safe = True
            error.stock_trader_operation = "account_identity"
            raise error

    def get_transactions(
        self,
        *,
        start_date: datetime,
        end_date: datetime,
        transaction_types: str = "TRADE",
        symbol: str | None = None,
    ) -> Any:
        """Return one bounded, read-only Schwab transaction-history window."""

        account_hash, headers = self._account_request_context()
        params = {
            "startDate": start_date.astimezone(timezone.utc).isoformat(timespec="seconds"),
            "endDate": end_date.astimezone(timezone.utc).isoformat(timespec="seconds"),
            "types": transaction_types,
        }
        if symbol:
            params["symbol"] = symbol.strip().upper()
        response = requests.get(
            f"{TRADER_BASE_URL}/accounts/{account_hash}/transactions",
            headers=headers,
            params=params,
            timeout=10,
        )
        response.raise_for_status()
        return response.json()

    def get_option_chain(self, symbol: str, strikes: int) -> Any:
        cleaned_symbol = symbol.strip().upper()
        if not cleaned_symbol:
            raise ValueError("Symbol is required for option chain.")

        response = requests.get(
            f"{MARKETDATA_BASE_URL}/chains",
            headers=self._headers(),
            params={
                "symbol": cleaned_symbol,
                "contractType": "ALL",
                "strikeCount": strikes,
                "includeUnderlyingQuote": "true",
                "strategy": "SINGLE",
            },
            timeout=10,
        )
        response.raise_for_status()
        return response.json()

    def get_equity_quotes(self, symbols: Iterable[str]) -> dict[str, dict[str, Any]]:
        cleaned_symbols = tuple(
            dict.fromkeys(
                str(symbol).strip().upper()
                for symbol in symbols
                if str(symbol).strip()
            )
        )
        if not cleaned_symbols:
            raise ValueError("At least one stock / ETF symbol is required for quotes.")
        response = requests.get(
            f"{MARKETDATA_BASE_URL}/quotes",
            headers=self._headers(),
            params={
                "symbols": ",".join(cleaned_symbols),
                "fields": "quote",
            },
            timeout=10,
        )
        response.raise_for_status()

        payload = response.json()
        if not isinstance(payload, dict):
            raise RuntimeError("Unexpected Schwab quote response.")

        rows_by_symbol = {
            str(key).strip().upper(): row
            for key, row in payload.items()
            if isinstance(row, dict)
        }
        quotes: dict[str, dict[str, Any]] = {}
        for symbol in cleaned_symbols:
            row = rows_by_symbol.get(symbol)
            if not isinstance(row, dict):
                continue
            quote = row.get("quote")
            quotes[symbol] = quote if isinstance(quote, dict) else row
        return quotes

    def get_equity_quote(self, symbol: str) -> dict[str, Any]:
        cleaned_symbol = symbol.strip().upper()
        quotes = self.get_equity_quotes((cleaned_symbol,))
        quote = quotes.get(cleaned_symbol)
        if quote is None:
            raise RuntimeError(f"No quote returned for {cleaned_symbol}.")
        return quote

    def get_equity_mid(self, symbol: str) -> float:
        cleaned_symbol = symbol.strip().upper()
        quote = self.get_equity_quote(cleaned_symbol)

        bid = _first_number(quote, ("bidPrice", "bid"))
        ask = _first_number(quote, ("askPrice", "ask"))
        mark = _first_number(quote, ("mark", "markPrice"))
        last = _first_number(quote, ("lastPrice", "last"))

        if bid is not None and ask is not None and bid > 0 and ask > 0:
            return round((bid + ask) / 2, 2)

        if mark is not None and mark > 0:
            return round(mark, 2)

        if last is not None and last > 0:
            return round(last, 2)

        raise RuntimeError(
            f"Quote for {cleaned_symbol} did not include a usable bid/ask, mark, or last price."
        )

    def cancel_order(self, order_id: str) -> object:
        cleaned_order_id = str(order_id).strip()
        if not cleaned_order_id:
            raise ValueError("Order ID is required for cancel.")

        account_hash, headers = self._account_request_context()
        response = requests.delete(
            f"{TRADER_BASE_URL}/accounts/{account_hash}/orders/{cleaned_order_id}",
            headers=headers,
            timeout=10,
        )
        response.raise_for_status()

        if not response.text:
            return None

        try:
            return response.json()
        except ValueError:
            return response.text

    def prepare_order_submission(self) -> SchwabOrderSubmissionContext:
        with self._session_lock("_access_token_lock"):
            self.ensure_access_token()
            account_hash = self._get_account_hash_for_current_token()
            if not self.access_token:
                raise RuntimeError("Schwab access token is not available.")
            return SchwabOrderSubmissionContext(
                account_hash=account_hash,
                access_token=self.access_token,
                identity_fingerprint=self._identity_fingerprint_for_current_token(
                    account_hash
                ),
            )

    def submit_order(self, order_payload: dict[str, Any]) -> str | None:
        context = self.prepare_order_submission()
        return self.submit_prepared_order(order_payload, context)

    def submit_prepared_order(
        self,
        order_payload: dict[str, Any],
        context: SchwabOrderSubmissionContext,
        *,
        before_post: Callable[[], None] | None = None,
    ) -> str | None:
        if not isinstance(context, SchwabOrderSubmissionContext):
            raise TypeError("A Schwab order-submission context is required.")
        if before_post is not None:
            before_post()
        response = requests.post(
            f"{TRADER_BASE_URL}/accounts/{context.account_hash}/orders",
            headers={
                "Authorization": f"Bearer {context.access_token}",
                "Accept": "application/json",
                "Content-Type": "application/json",
            },
            json=order_payload,
            timeout=10,
        )
        response.raise_for_status()
        return response.headers.get("Location")

    def replace_order(self, order_id: str, order_payload: dict[str, Any]) -> str | None:
        cleaned_order_id = str(order_id).strip()
        if not cleaned_order_id:
            raise ValueError("Order ID is required for replacement.")

        account_hash, headers = self._account_request_context()
        response = requests.put(
            f"{TRADER_BASE_URL}/accounts/{account_hash}/orders/{cleaned_order_id}",
            headers={**headers, "Content-Type": "application/json"},
            json=order_payload,
            timeout=10,
        )
        response.raise_for_status()
        return response.headers.get("Location")


def _record_token_mutation_failure(
    *,
    cached_payload: dict[str, Any],
    marked_payload: dict[str, Any],
    attempt_id: str,
    error: Exception,
    persist_reauthorization_rejection: bool = False,
) -> dict[str, Any] | None:
    """Restore definite failures; fail closed after an ambiguous OAuth outcome."""

    candidate_payload = getattr(error, "schwab_candidate_token_payload", None)
    if isinstance(candidate_payload, dict):
        try:
            write_token_payload_atomic(candidate_payload)
        except Exception as recovery_error:
            try:
                write_refresh_attempt(
                    candidate_payload,
                    attempt_id=attempt_id,
                    status="FAILED_UNCERTAIN",
                    error_type=type(recovery_error).__name__,
                )
            except Exception:
                pass
            return None
        return candidate_payload

    try:
        reauthorization_error = (
            _refresh_reauthorization_error_code(error)
            if persist_reauthorization_rejection
            else None
        )
        if reauthorization_error is not None:
            write_refresh_attempt(
                cached_payload,
                attempt_id=attempt_id,
                status="FAILED_REAUTH_REQUIRED",
                error_type=_token_mutation_error_type(error),
                oauth_error_code=reauthorization_error,
            )
            setattr(error, "schwab_reauthorization_required", True)
        elif _token_mutation_definitely_failed(error):
            write_token_payload_atomic(cached_payload)
        else:
            write_refresh_attempt(
                marked_payload,
                attempt_id=attempt_id,
                status="FAILED_UNCERTAIN",
                error_type=type(error).__name__,
            )
    except Exception:
        # The durable IN_PROGRESS marker is already fail-closed if this
        # best-effort transition itself cannot be persisted.
        pass
    return None


def _publish_token_mutation_marker(
    cached_payload: dict[str, Any],
) -> dict[str, Any]:
    """Durably mark a planned OAuth POST or restore clean pre-POST state."""

    try:
        return write_refresh_attempt(cached_payload, status="IN_PROGRESS")
    except Exception as marker_error:
        try:
            write_token_payload_atomic(cached_payload)
        except Exception:
            setattr(marker_error, "schwab_retry_safe", False)
        else:
            # No request reached Schwab and the prior cache is durable again.
            setattr(marker_error, "schwab_retry_safe", True)
        raise


def _token_mutation_definitely_failed(error: Exception) -> bool:
    if isinstance(error, requests.ConnectTimeout):
        return True
    if isinstance(error, requests.HTTPError):
        response = getattr(error, "response", None)
        status = getattr(response, "status_code", None)
        return status in {400, 401, 403, 404, 405, 415, 422, 429}
    return False


def _token_mutation_retry_safe(error: Exception) -> bool:
    if isinstance(error, requests.ConnectTimeout):
        return True
    if isinstance(error, requests.HTTPError):
        response = getattr(error, "response", None)
        return getattr(response, "status_code", None) == 429
    return False


def _token_mutation_error_type(error: Exception) -> str:
    """Return a non-secret diagnostic label for a failed OAuth mutation."""

    if isinstance(error, requests.HTTPError):
        response = getattr(error, "response", None)
        status = getattr(response, "status_code", None)
        if status is not None:
            return f"HTTPError:{status}"
    return type(error).__name__


def _raise_for_oauth_status(response: Any) -> None:
    """Raise an HTTP error containing only allowlisted OAuth diagnostics."""

    try:
        response.raise_for_status()
    except requests.HTTPError as error:
        status = getattr(response, "status_code", None)
        oauth_error_code = _sanitized_oauth_error_code(response)
        client_correlid = _sanitized_schwab_client_correlid(response)
        message = f"Schwab OAuth token request failed (HTTP {status or 'unknown'}"
        if oauth_error_code is not None:
            message += f"; error={oauth_error_code}"
            setattr(error, "schwab_oauth_error_code", oauth_error_code)
        if client_correlid is not None:
            message += f"; correlid={client_correlid}"
            setattr(error, "schwab_client_correlid", client_correlid)
        error.args = (f"{message})",)
        raise


def _sanitized_oauth_error_code(response: Any) -> str | None:
    try:
        payload = response.json()
    except Exception:
        return None
    if not isinstance(payload, dict):
        return None
    value = payload.get("error")
    if not isinstance(value, str):
        return None
    normalized = value.strip().lower()
    if normalized == "unsupported_token_type":
        nested = _nested_oauth_error_code(payload.get("error_description"))
        if nested is not None:
            return nested
    return normalized if normalized in _SAFE_OAUTH_ERROR_CODES else None


def _sanitized_schwab_client_correlid(response: Any) -> str | None:
    """Return only a short identifier-safe Schwab support correlation value."""

    headers = getattr(response, "headers", None)
    try:
        items = headers.items()
    except (AttributeError, TypeError):
        return None
    for name, value in items:
        if str(name).lower() != "schwab-client-correlid":
            continue
        if isinstance(value, str) and _SAFE_SCHWAB_CLIENT_CORRELID.fullmatch(value):
            return value
        return None
    return None


def _nested_oauth_error_code(value: Any) -> str | None:
    """Extract only an allowlisted code from Schwab's nested error wrapper."""

    if not isinstance(value, str):
        return None
    nested_payload: Any = None
    try:
        nested_payload = json.loads(value)
    except (TypeError, ValueError):
        pass
    if isinstance(nested_payload, dict):
        candidate = nested_payload.get("error")
        if isinstance(candidate, str):
            normalized = candidate.strip().lower()
            if normalized in _SAFE_OAUTH_ERROR_CODES:
                return normalized
    match = re.search(r'["\']error["\']\s*:\s*["\']([a-z_]+)["\']', value, re.I)
    if match is None:
        return None
    normalized = match.group(1).lower()
    return normalized if normalized in _SAFE_OAUTH_ERROR_CODES else None


def _refresh_reauthorization_error_code(error: Exception) -> str | None:
    if not isinstance(error, requests.HTTPError):
        return None
    response = getattr(error, "response", None)
    if response is None or getattr(response, "status_code", None) not in {400, 401}:
        return None
    oauth_error_code = getattr(error, "schwab_oauth_error_code", None)
    if oauth_error_code in OAUTH_REAUTHORIZATION_ERROR_CODES:
        return oauth_error_code
    # Schwab sometimes returns an empty/non-JSON body for a definite OAuth
    # rejection.  The refresh request shape is fixed and a repeated 400/401
    # cannot heal by fanning the same credential out across symbols/processes.
    # Persist only the status class (never the response body) and require one
    # fresh authorization instead.
    return f"http_{response.status_code}"


def _reauthorization_required_message(oauth_error_code: str | None) -> str:
    if oauth_error_code == "invalid_client":
        return (
            "Schwab OAuth refresh was rejected (invalid_client); verify the Schwab "
            "app credentials, then authorize Schwab again before making broker requests."
        )
    reason = {
        "invalid_grant": "invalid_grant",
        "http_400": "HTTP 400",
        "http_401": "HTTP 401",
    }.get(oauth_error_code, "credential rejection")
    return (
        f"Schwab OAuth refresh was rejected ({reason}); authorize Schwab again "
        "before making broker requests."
    )


def _schwab_order_dedup_key(order: Any) -> tuple[str, str]:
    if isinstance(order, dict):
        order_id = str(order.get("orderId") or order.get("order_id") or "").strip()
        if order_id:
            return "order_id", order_id
    return "payload", json.dumps(order, sort_keys=True, separators=(",", ":"), default=str)


def sync_schwab_portfolio() -> PortfolioSnapshot:
    session = SchwabSession()
    account_payload = session.get_account()
    orders_payload: Any = None
    orders_error: str | None = None
    try:
        orders_payload = session.get_open_orders()
    except Exception as exc:
        orders_error = f"{type(exc).__name__}: {exc}"

    synced_at = datetime.now(timezone.utc)
    account_facts = normalize_schwab_policy_inputs(
        account_payload,
        orders_payload,
        observed_at=synced_at,
        orders_error=orders_error,
    )
    option_symbols = _normalized_option_symbols(account_facts)
    option_quotes: dict[str, dict[str, Any]] = {}
    if option_symbols:
        try:
            option_quotes = session.get_equity_quotes(option_symbols)
        except Exception as exc:
            normalized_positions = account_facts.get("positions")
            if isinstance(normalized_positions, dict):
                normalized_positions["option_quote_status"] = "UNAVAILABLE"
                normalized_positions["option_quote_unavailable_reasons"] = [
                    f"Schwab option quote refresh failed: {type(exc).__name__}: {exc}"
                ]
        else:
            enrich_option_position_quotes(
                account_facts,
                option_quotes,
                observed_at=synced_at,
            )
    account = _securities_account(account_payload)
    account_values = account_facts["account_values"]
    if not isinstance(account_values, dict):
        raise RuntimeError("Normalized Schwab account values were unavailable.")

    holdings: list[Holding] = []
    for row in _position_rows(account):
        instrument = row.get("instrument") if isinstance(row.get("instrument"), dict) else {}
        symbol = str(instrument.get("symbol") or row.get("symbol") or "").strip().upper()
        holding = _holding_from_schwab(
            row,
            option_quote=option_quotes.get(symbol),
        )
        if holding is not None:
            holdings.append(holding)
    holdings.sort(key=lambda holding: holding.bucket == "Option")

    liquidation_value = _to_float(account_values.get("liquidation_value"))
    cash_balance = _to_float(account_values.get("cash_balance"))
    short_balance = _to_float(account_values.get("short_balance"))
    cash: list[CashBalance] = []
    if cash_balance is not None:
        cash_and_sweep = cash_balance + (short_balance or 0.0)
        cash.append(
            CashBalance(
                symbol="USD",
                amount=round(cash_and_sweep, 2),
                value=round(cash_and_sweep, 2),
                source="schwab",
                bucket="Cash & sweep",
            )
        )

    order_status = ""
    working_orders = account_facts.get("working_orders")
    if isinstance(working_orders, dict) and working_orders.get("status") != "CURRENT":
        order_status = f"; working orders {str(working_orders.get('status', 'UNAVAILABLE')).lower()}"

    return PortfolioSnapshot(
        source="schwab",
        account_label="Schwab",
        cash=cash,
        holdings=holdings,
        synced_at=synced_at,
        status=f"Schwab synced {_account_label(account)}{order_status}",
        reported_total_value=liquidation_value,
        account_facts=account_facts,
    )


def _securities_account(payload: Any) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise RuntimeError("Unexpected Schwab account response.")

    account = payload.get("securitiesAccount") or payload
    if not isinstance(account, dict):
        raise RuntimeError("Unexpected Schwab account response; missing securitiesAccount.")

    return account


def _normalized_option_symbols(account_facts: dict[str, object]) -> tuple[str, ...]:
    positions = account_facts.get("positions")
    if not isinstance(positions, dict):
        return ()
    items = positions.get("items")
    if not isinstance(items, list):
        return ()
    return tuple(
        dict.fromkeys(
            str(row.get("symbol") or "").strip().upper()
            for row in items
            if isinstance(row, dict)
            and "OPTION" in str(row.get("asset_type") or "").upper()
            and str(row.get("symbol") or "").strip()
        )
    )


def _position_rows(account: dict[str, Any]) -> list[dict[str, Any]]:
    positions = account.get("positions") or []
    return [row for row in positions if isinstance(row, dict)] if isinstance(positions, list) else []


def _holding_from_schwab(
    row: dict[str, Any],
    *,
    option_quote: dict[str, Any] | None = None,
) -> Holding | None:
    instrument = row.get("instrument") if isinstance(row.get("instrument"), dict) else {}

    symbol = str(instrument.get("symbol") or row.get("symbol") or "").strip().upper()
    asset_type = str(instrument.get("assetType") or row.get("assetType") or "").strip().upper()
    quantity = _net_quantity(row)

    if not symbol or abs(quantity) <= 0.00000001:
        return None

    market_value = _to_float(row.get("marketValue"))
    price = _first_number(row, ("marketPrice", "lastPrice", "currentPrice", "markPrice"))

    if "OPTION" in asset_type and option_quote is not None:
        price = _first_number(option_quote, ("mark", "markPrice"))
        if price is None:
            bid = _first_number(option_quote, ("bidPrice", "bid"))
            ask = _first_number(option_quote, ("askPrice", "ask"))
            if bid is not None and ask is not None:
                price = (bid + ask) / 2.0

    if price is None and market_value is not None and abs(quantity) > 0.00000001:
        price = abs(market_value / quantity)

    if market_value is None:
        market_value = quantity * (price or 0.0)

    return Holding(
        symbol=symbol,
        quantity=round(quantity, 8),
        price=round(price or 0.0, 8),
        value=round(market_value, 2),
        source="schwab",
        bucket=_schwab_holding_bucket(asset_type),
        unrealized_pnl=_schwab_unrealized_pnl(row),
        day_pnl=_schwab_day_pnl(row, quantity),
    )


def _schwab_holding_bucket(asset_type: str) -> str:
    if "OPTION" in asset_type:
        return "Option"
    if asset_type == "COLLECTIVE_INVESTMENT":
        return "ETF"
    if asset_type in {"EQUITY", "STOCK"}:
        return "Stock"
    return asset_type.title() or "Other"


def _net_quantity(row: dict[str, Any]) -> float:
    long_quantity = _to_float(row.get("longQuantity"))
    short_quantity = _to_float(row.get("shortQuantity"))

    if long_quantity is not None or short_quantity is not None:
        return (long_quantity or 0.0) - (short_quantity or 0.0)

    for key in ("quantity", "settledLongQuantity", "agedQuantity"):
        value = _to_float(row.get(key))
        if value is not None:
            return value

    return 0.0


def _schwab_unrealized_pnl(row: dict[str, Any]) -> float | None:
    long_pnl = _to_float(row.get("longOpenProfitLoss"))
    short_pnl = _to_float(row.get("shortOpenProfitLoss"))

    if long_pnl is not None or short_pnl is not None:
        return round((long_pnl or 0.0) + (short_pnl or 0.0), 2)

    value = _first_number(row, ("openProfitLoss", "unrealizedProfitLoss", "unrealizedPnl"))
    return round(value, 2) if value is not None else None


def _schwab_day_pnl(row: dict[str, Any], quantity: float) -> float | None:
    instrument = row.get("instrument") if isinstance(row.get("instrument"), dict) else {}
    asset_type = str(instrument.get("assetType") or row.get("assetType") or "").strip().upper()
    net_change = _to_float(instrument.get("netChange"))

    # thinkorswim's P/L Day marks stock and ETF exposure from the prior close.
    # Schwab's position-level currentDayProfitLoss can instead switch to a
    # cost-basis convention after exercises, assignments, or other same-day
    # position changes.  The instrument net change preserves TOS semantics for
    # both long and short shares.  Options retain the position-level field so
    # contracts opened today are measured from their actual fill price.
    if asset_type in {"COLLECTIVE_INVESTMENT", "EQUITY", "STOCK"} and net_change is not None:
        return round(net_change * quantity, 2)

    value = _first_number(row, ("currentDayProfitLoss", "dayProfitLoss"))

    if value is not None:
        return round(value, 2)

    market_value = _to_float(row.get("marketValue"))
    day_pnl_percent = _first_number(row, ("currentDayProfitLossPercentage", "dayProfitLossPercentage"))

    if market_value is None or day_pnl_percent is None:
        return None

    return round(market_value * (day_pnl_percent / 100.0), 2)


def _first_number(row: dict[str, Any], keys: tuple[str, ...]) -> float | None:
    for key in keys:
        value = _to_float(row.get(key))
        if value is not None:
            return value

    return None


def _to_float(value: Any) -> float | None:
    if value is None or value == "":
        return None

    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _account_label(account: dict[str, Any]) -> str:
    account_number = str(account.get("accountNumber") or "").strip()
    if account_number:
        return "••••" + account_number[-4:]

    return "account"
