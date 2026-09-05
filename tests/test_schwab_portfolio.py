from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any
from urllib.parse import parse_qs, urlparse

import pytest
import requests
from filelock import Timeout as FileLockTimeout

import app.services.schwab as schwab_module
import app.services.schwab_token_store as schwab_token_store
from app.models.portfolio import Holding, PortfolioSnapshot
from app.services.aggregate import DucketBucketSnapshot
from app.services.schwab import SchwabSession
from app.services.schwab_retry import is_retryable_schwab_error
from ml.stock_trader.state import capture_portfolio_state


def test_default_token_cache_uses_project_data() -> None:
    assert schwab_token_store.DEFAULT_TOKEN_CACHE_PATH == (
        schwab_token_store.PROJECT_ROOT / "data" / "schwab_tokens.json"
    )


def test_schwab_authorization_url_uses_supported_code_flow_without_made_up_scope() -> None:
    session = object.__new__(SchwabSession)
    session.config = SimpleNamespace(
        client_id="client-id",
        redirect_uri="https://example.test/schwab/callback",
    )

    authorization_url, state = session.build_authorization_url()
    query = parse_qs(urlparse(authorization_url).query)

    assert query == {
        "response_type": ["code"],
        "client_id": ["client-id"],
        "redirect_uri": ["https://example.test/schwab/callback"],
        "state": [state],
    }
    assert state


def test_schwab_session_serializes_lazy_account_hash_discovery(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = object.__new__(SchwabSession)
    session.access_token = "access-token"
    session.access_token_expires_at = datetime.now(timezone.utc) + timedelta(hours=1)
    session.refresh_token = "refresh-token"
    session.account_hash = None
    session._access_token_lock = threading.RLock()
    session._account_hash_lock = threading.Lock()
    calls = 0

    class Response:
        @staticmethod
        def raise_for_status() -> None:
            return None

        @staticmethod
        def json() -> list[dict[str, str]]:
            return [{"hashValue": "account-hash"}]

    def get(*_args, **_kwargs) -> Response:
        nonlocal calls
        calls += 1
        time.sleep(0.02)
        return Response()

    monkeypatch.setattr(schwab_module.requests, "get", get)
    with ThreadPoolExecutor(max_workers=3) as pool:
        results = list(pool.map(lambda _index: session._get_account_hash(), range(3)))

    assert results == ["account-hash"] * 3
    assert calls == 1


def test_schwab_session_serializes_access_token_refresh() -> None:
    session = object.__new__(SchwabSession)
    session.access_token = None
    session.access_token_expires_at = None
    session.refresh_token = "refresh-token"
    session.account_hash = None
    session._access_token_lock = threading.RLock()
    session._account_hash_lock = threading.Lock()
    calls = 0

    def refresh() -> None:
        nonlocal calls
        calls += 1
        time.sleep(0.02)
        session.access_token = "new-access-token"
        session.access_token_expires_at = datetime.now(timezone.utc) + timedelta(
            hours=1
        )

    session.refresh_access_token = refresh
    with ThreadPoolExecutor(max_workers=3) as pool:
        list(pool.map(lambda _index: session.ensure_access_token(), range(3)))

    assert calls == 1


def test_parallel_snapshot_preflight_does_not_fan_out_token_refresh_failure(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        schwab_token_store, "TOKEN_CACHE_PATH", tmp_path / "schwab_tokens.json"
    )
    schwab_token_store.write_token_payload_atomic(
        {
            "access_token": "expired-access-token",
            "access_token_expires_at": "2020-01-01T00:00:00+00:00",
            "refresh_token": "refresh-token",
            "refresh_token_expires_at": "2099-01-01T00:00:00+00:00",
        }
    )
    session = object.__new__(SchwabSession)
    session.config = SimpleNamespace(client_id="client", client_secret="secret")
    session.access_token = None
    session.access_token_expires_at = None
    session.refresh_token = "refresh-token"
    session.account_hash = None
    session._access_token_lock = threading.RLock()
    session._account_hash_lock = threading.Lock()
    calls = 0

    def post(*_args, **_kwargs):
        nonlocal calls
        calls += 1
        raise requests.ReadTimeout("Schwab token refresh timed out")

    monkeypatch.setattr(schwab_module.requests, "post", post)
    with pytest.raises(requests.ReadTimeout) as failure:
        capture_portfolio_state(
            session,
            observed_at="2026-08-31T16:00:00+00:00",
            parallel=True,
        )

    assert calls == 1
    assert getattr(failure.value, "schwab_retry_safe") is False
    assert getattr(failure.value, "stock_trader_operation") == "authentication"


def test_token_refresh_is_single_flight_across_separate_sessions(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        schwab_token_store, "TOKEN_CACHE_PATH", tmp_path / "schwab_tokens.json"
    )
    schwab_token_store.write_token_payload_atomic(
        {
            "access_token": "expired-access-token",
            "access_token_expires_at": "2020-01-01T00:00:00+00:00",
            "refresh_token": "refresh-token-0",
            "refresh_token_expires_at": "2099-01-01T00:00:00+00:00",
        }
    )
    calls = 0

    class Response:
        @staticmethod
        def raise_for_status() -> None:
            return None

        @staticmethod
        def json() -> dict[str, object]:
            return {
                "access_token": "access-token-1",
                "refresh_token": "refresh-token-1",
                "expires_in": 1_800,
                "refresh_token_expires_in": 604_800,
            }

    def post(*_args, **_kwargs) -> Response:
        nonlocal calls
        calls += 1
        time.sleep(0.02)
        return Response()

    def build_session() -> SchwabSession:
        session = object.__new__(SchwabSession)
        session.config = SimpleNamespace(client_id="client", client_secret="secret")
        session.account_hash = None
        session._access_token_lock = threading.RLock()
        session._account_hash_lock = threading.Lock()
        session._hydrate_from_cache()
        return session

    monkeypatch.setattr(schwab_module.requests, "post", post)
    sessions = [build_session(), build_session()]
    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(lambda session: session.ensure_access_token(), sessions))

    assert calls == 1
    assert [session.access_token for session in sessions] == ["access-token-1"] * 2
    assert [session.refresh_token for session in sessions] == ["refresh-token-1"] * 2
    cached = schwab_token_store.load_token_payload(strict=True)
    assert cached is not None
    assert cached["access_token"] == "access-token-1"
    assert "refresh_attempt" not in cached


def test_uncertain_token_refresh_blocks_waiting_sessions_without_second_post(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        schwab_token_store, "TOKEN_CACHE_PATH", tmp_path / "schwab_tokens.json"
    )
    schwab_token_store.write_token_payload_atomic(
        {
            "access_token": "expired-access-token",
            "access_token_expires_at": "2020-01-01T00:00:00+00:00",
            "refresh_token": "refresh-token-0",
            "refresh_token_expires_at": "2099-01-01T00:00:00+00:00",
        }
    )
    calls = 0

    def post(*_args, **_kwargs):
        nonlocal calls
        calls += 1
        time.sleep(0.02)
        raise requests.ReadTimeout("ambiguous token refresh timeout")

    def build_session() -> SchwabSession:
        session = object.__new__(SchwabSession)
        session.config = SimpleNamespace(client_id="client", client_secret="secret")
        session.account_hash = None
        session._access_token_lock = threading.RLock()
        session._account_hash_lock = threading.Lock()
        session._hydrate_from_cache()
        return session

    monkeypatch.setattr(schwab_module.requests, "post", post)
    sessions = [build_session(), build_session()]
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(session.ensure_access_token) for session in sessions]
        failures = [future.exception() for future in futures]

    assert calls == 1
    assert all(failure is not None for failure in failures)
    assert all(
        getattr(failure, "schwab_retry_safe", None) is False
        for failure in failures
    )
    cached = schwab_token_store.load_token_payload(strict=True)
    assert cached is not None
    assert schwab_token_store.has_uncertain_refresh_attempt(cached)

    third_session = build_session()
    with pytest.raises(RuntimeError, match="uncertain prior outcome"):
        third_session.ensure_access_token()
    assert calls == 1


@pytest.mark.parametrize(
    "refresh_attempt",
    [
        {
            "id": "uncertain-attempt",
            "started_at": "2026-08-31T16:00:00+00:00",
            "status": "FAILED_UNCERTAIN",
            "error_type": "ReadTimeout",
        },
        {
            "id": "rejected-attempt",
            "started_at": "2026-09-03T20:32:36+00:00",
            "status": "FAILED_REAUTH_REQUIRED",
            "error_type": "HTTPError:400",
            "oauth_error_code": "invalid_grant",
        },
    ],
    ids=("uncertain-outcome", "reauthorization-required"),
)
def test_authorization_code_exchange_replaces_blocking_refresh_marker(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
    refresh_attempt: dict[str, str],
) -> None:
    monkeypatch.setattr(
        schwab_token_store, "TOKEN_CACHE_PATH", tmp_path / "schwab_tokens.json"
    )
    schwab_token_store.write_token_payload_atomic(
        {
            "access_token": "expired-access-token",
            "access_token_expires_at": "2020-01-01T00:00:00+00:00",
            "refresh_token": "uncertain-refresh-token",
            "refresh_attempt": refresh_attempt,
        }
    )

    class Response:
        @staticmethod
        def raise_for_status() -> None:
            return None

        @staticmethod
        def json() -> dict[str, object]:
            return {
                "access_token": "reauthorized-access-token",
                "refresh_token": "reauthorized-refresh-token",
                "expires_in": 1_800,
                "refresh_token_expires_in": 604_800,
            }

    monkeypatch.setattr(schwab_module.requests, "post", lambda *_a, **_k: Response())
    session = object.__new__(SchwabSession)
    session.config = SimpleNamespace(
        client_id="client",
        client_secret="secret",
        redirect_uri="https://example.test/callback",
    )
    session.account_hash = None
    session._access_token_lock = threading.RLock()
    session._account_hash_lock = threading.Lock()
    session._hydrate_from_cache()

    session.exchange_authorization_code("authorization-code")

    cached = schwab_token_store.load_token_payload(strict=True)
    assert cached is not None
    assert cached["access_token"] == "reauthorized-access-token"
    assert cached["refresh_token"] == "reauthorized-refresh-token"
    assert "refresh_attempt" not in cached


def test_authorization_code_read_timeout_leaves_uncertain_marker(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        schwab_token_store, "TOKEN_CACHE_PATH", tmp_path / "schwab_tokens.json"
    )
    schwab_token_store.write_token_payload_atomic(
        {
            "access_token": "prior-access-token",
            "access_token_expires_at": "2099-01-01T00:00:00+00:00",
            "refresh_token": "prior-refresh-token",
            "refresh_token_expires_at": "2099-01-01T00:00:00+00:00",
        }
    )
    monkeypatch.setattr(
        schwab_module.requests,
        "post",
        lambda *_a, **_k: (_ for _ in ()).throw(
            requests.ReadTimeout("ambiguous authorization exchange")
        ),
    )
    session = object.__new__(SchwabSession)
    session.config = SimpleNamespace(
        client_id="client",
        client_secret="secret",
        redirect_uri="https://example.test/callback",
    )
    session.account_hash = None
    session._access_token_lock = threading.RLock()
    session._account_hash_lock = threading.Lock()
    session._hydrate_from_cache()

    with pytest.raises(requests.ReadTimeout) as failure:
        session.exchange_authorization_code("authorization-code")

    assert getattr(failure.value, "schwab_retry_safe") is False
    cached = schwab_token_store.load_token_payload(strict=True)
    assert cached is not None
    assert schwab_token_store.has_uncertain_refresh_attempt(cached)


def test_authorization_response_without_refresh_token_stays_uncertain(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        schwab_token_store, "TOKEN_CACHE_PATH", tmp_path / "schwab_tokens.json"
    )

    class Response:
        @staticmethod
        def raise_for_status() -> None:
            return None

        @staticmethod
        def json() -> dict[str, object]:
            return {"access_token": "new-access-token", "expires_in": 1_800}

    monkeypatch.setattr(schwab_module.requests, "post", lambda *_a, **_k: Response())
    session = object.__new__(SchwabSession)
    session.config = SimpleNamespace(
        client_id="client",
        client_secret="secret",
        redirect_uri="https://example.test/callback",
    )
    session.access_token = None
    session.access_token_expires_at = None
    session.refresh_token = None
    session.account_hash = None
    session._access_token_lock = threading.RLock()
    session._account_hash_lock = threading.Lock()

    with pytest.raises(ValueError, match="did not include a refresh token"):
        session.exchange_authorization_code("authorization-code")

    cached = schwab_token_store.load_token_payload(strict=True)
    assert cached is not None
    assert schwab_token_store.has_uncertain_refresh_attempt(cached)
    assert "access_token" not in cached


def test_authorization_cache_read_failure_never_mutates_or_posts(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        schwab_token_store, "TOKEN_CACHE_PATH", tmp_path / "schwab_tokens.json"
    )
    schwab_token_store.write_token_payload_atomic(
        {
            "access_token": "valid-access-token",
            "access_token_expires_at": "2099-01-01T00:00:00+00:00",
            "refresh_token": "valid-refresh-token",
            "refresh_token_expires_at": "2099-01-01T00:00:00+00:00",
        }
    )
    original_bytes = schwab_token_store.TOKEN_CACHE_PATH.read_bytes()
    post_calls = 0

    def post(*_args, **_kwargs):
        nonlocal post_calls
        post_calls += 1
        raise AssertionError("OAuth POST must not run after a cache read failure")

    monkeypatch.setattr(
        schwab_module,
        "_load_token_payload_unlocked",
        lambda **_kwargs: (_ for _ in ()).throw(OSError("cache read failed")),
    )
    monkeypatch.setattr(schwab_module.requests, "post", post)
    session = object.__new__(SchwabSession)
    session.config = SimpleNamespace(
        client_id="client",
        client_secret="secret",
        redirect_uri="https://example.test/callback",
    )
    session._access_token_lock = threading.RLock()

    with pytest.raises(OSError, match="cache read failed"):
        session.exchange_authorization_code("authorization-code")

    assert post_calls == 0
    assert schwab_token_store.TOKEN_CACHE_PATH.read_bytes() == original_bytes


def test_token_cache_lock_timeout_is_retryable_without_oauth_post(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    post_calls = 0

    @contextmanager
    def unavailable_cache():
        raise FileLockTimeout("token-cache.lock")
        yield

    def post(*_args, **_kwargs):
        nonlocal post_calls
        post_calls += 1
        raise AssertionError("OAuth POST must not run without the cache lock")

    monkeypatch.setattr(schwab_module, "locked_token_cache", unavailable_cache)
    monkeypatch.setattr(schwab_module.requests, "post", post)
    session = object.__new__(SchwabSession)
    session.config = SimpleNamespace(
        client_id="client",
        client_secret="secret",
        redirect_uri="https://example.test/callback",
    )
    session._access_token_lock = threading.RLock()

    with pytest.raises(FileLockTimeout) as failure:
        session.exchange_authorization_code("authorization-code")

    assert post_calls == 0
    assert getattr(failure.value, "schwab_retry_safe") is True
    assert is_retryable_schwab_error(failure.value)


def test_marker_write_failure_happens_before_oauth_post(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        schwab_token_store, "TOKEN_CACHE_PATH", tmp_path / "schwab_tokens.json"
    )
    original = {
        "access_token": "valid-access-token",
        "access_token_expires_at": "2099-01-01T00:00:00+00:00",
        "refresh_token": "valid-refresh-token",
        "refresh_token_expires_at": "2099-01-01T00:00:00+00:00",
    }
    schwab_token_store.write_token_payload_atomic(original)
    original_bytes = schwab_token_store.TOKEN_CACHE_PATH.read_bytes()
    post_calls = 0
    original_replace = schwab_token_store._replace_file_durably
    replace_calls = 0

    def fail_first_replace(source, destination):
        nonlocal replace_calls
        replace_calls += 1
        if replace_calls == 1:
            raise OSError("marker replace failed")
        original_replace(source, destination)

    def post(*_args, **_kwargs):
        nonlocal post_calls
        post_calls += 1
        raise AssertionError("OAuth POST must not run before a durable marker")

    monkeypatch.setattr(
        schwab_token_store, "_replace_file_durably", fail_first_replace
    )
    monkeypatch.setattr(schwab_module.requests, "post", post)
    session = object.__new__(SchwabSession)
    session.config = SimpleNamespace(
        client_id="client",
        client_secret="secret",
        redirect_uri="https://example.test/callback",
    )
    session._access_token_lock = threading.RLock()

    with pytest.raises(OSError, match="marker replace failed") as failure:
        session.exchange_authorization_code("authorization-code")

    assert post_calls == 0
    assert getattr(failure.value, "schwab_retry_safe") is True
    assert is_retryable_schwab_error(failure.value)
    assert schwab_token_store.TOKEN_CACHE_PATH.read_bytes() == original_bytes


def test_post_replace_marker_failure_restores_clean_cache_without_oauth_post(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        schwab_token_store, "TOKEN_CACHE_PATH", tmp_path / "schwab_tokens.json"
    )
    original = {
        "access_token": "valid-access-token",
        "access_token_expires_at": "2099-01-01T00:00:00+00:00",
        "refresh_token": "valid-refresh-token",
        "refresh_token_expires_at": "2099-01-01T00:00:00+00:00",
    }
    schwab_token_store.write_token_payload_atomic(original)
    original_replace = schwab_token_store._replace_file_durably
    replace_calls = 0
    post_calls = 0

    def fail_after_first_replace(source, destination):
        nonlocal replace_calls
        replace_calls += 1
        if replace_calls == 1:
            os.replace(source, destination)
            raise OSError("marker durability failed after replace")
        original_replace(source, destination)

    def post(*_args, **_kwargs):
        nonlocal post_calls
        post_calls += 1
        raise AssertionError("OAuth POST must not run after marker failure")

    monkeypatch.setattr(
        schwab_token_store, "_replace_file_durably", fail_after_first_replace
    )
    monkeypatch.setattr(schwab_module.requests, "post", post)
    session = object.__new__(SchwabSession)
    session.config = SimpleNamespace(
        client_id="client",
        client_secret="secret",
        redirect_uri="https://example.test/callback",
    )
    session._access_token_lock = threading.RLock()

    with pytest.raises(OSError, match="marker durability failed") as failure:
        session.exchange_authorization_code("authorization-code")

    assert post_calls == 0
    assert getattr(failure.value, "schwab_retry_safe") is True
    assert is_retryable_schwab_error(failure.value)
    assert schwab_token_store.load_token_payload(strict=True) == original


def test_token_success_cache_recovers_after_post_replace_durability_error(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        schwab_token_store, "TOKEN_CACHE_PATH", tmp_path / "schwab_tokens.json"
    )
    schwab_token_store.write_token_payload_atomic(
        {
            "access_token": "old-access-token",
            "access_token_expires_at": "2099-01-01T00:00:00+00:00",
            "refresh_token": "old-refresh-token",
            "refresh_token_expires_at": "2099-01-01T00:00:00+00:00",
        }
    )

    class Response:
        @staticmethod
        def raise_for_status() -> None:
            return None

        @staticmethod
        def json() -> dict[str, object]:
            return {
                "access_token": "new-access-token",
                "refresh_token": "new-refresh-token",
                "expires_in": 1_800,
                "refresh_token_expires_in": 604_800,
            }

    original_replace = schwab_token_store._replace_file_durably
    replace_calls = 0

    def fail_after_second_replace(source, destination):
        nonlocal replace_calls
        replace_calls += 1
        if replace_calls == 2:
            os.replace(source, destination)
            raise OSError("directory durability failed after replace")
        original_replace(source, destination)

    monkeypatch.setattr(
        schwab_token_store, "_replace_file_durably", fail_after_second_replace
    )
    monkeypatch.setattr(schwab_module.requests, "post", lambda *_a, **_k: Response())
    session = object.__new__(SchwabSession)
    session.config = SimpleNamespace(
        client_id="client",
        client_secret="secret",
        redirect_uri="https://example.test/callback",
    )
    session.access_token = None
    session.access_token_expires_at = None
    session.refresh_token = None
    session.account_hash = None
    session._access_token_lock = threading.RLock()
    session._account_hash_lock = threading.Lock()

    session.exchange_authorization_code("authorization-code")

    cached = schwab_token_store.load_token_payload(strict=True)
    assert cached is not None
    assert cached["access_token"] == "new-access-token"
    assert cached["refresh_token"] == "new-refresh-token"
    assert "refresh_attempt" not in cached
    assert session.access_token == "new-access-token"


def test_token_success_cache_failure_preserves_candidate_with_uncertain_marker(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        schwab_token_store, "TOKEN_CACHE_PATH", tmp_path / "schwab_tokens.json"
    )
    schwab_token_store.write_token_payload_atomic(
        {
            "access_token": "old-access-token",
            "access_token_expires_at": "2099-01-01T00:00:00+00:00",
            "refresh_token": "old-refresh-token",
            "refresh_token_expires_at": "2099-01-01T00:00:00+00:00",
        }
    )

    class Response:
        @staticmethod
        def raise_for_status() -> None:
            return None

        @staticmethod
        def json() -> dict[str, object]:
            return {
                "access_token": "new-access-token",
                "refresh_token": "new-refresh-token",
                "expires_in": 1_800,
                "refresh_token_expires_in": 604_800,
            }

    original_replace = schwab_token_store._replace_file_durably
    replace_calls = 0

    def fail_primary_and_clean_recovery(source, destination):
        nonlocal replace_calls
        replace_calls += 1
        if replace_calls in {2, 3}:
            raise OSError("token cache replacement failed")
        original_replace(source, destination)

    monkeypatch.setattr(
        schwab_token_store,
        "_replace_file_durably",
        fail_primary_and_clean_recovery,
    )
    monkeypatch.setattr(schwab_module.requests, "post", lambda *_a, **_k: Response())
    session = object.__new__(SchwabSession)
    session.config = SimpleNamespace(
        client_id="client",
        client_secret="secret",
        redirect_uri="https://example.test/callback",
    )
    session.access_token = None
    session.access_token_expires_at = None
    session.refresh_token = None
    session.account_hash = None
    session._access_token_lock = threading.RLock()
    session._account_hash_lock = threading.Lock()

    with pytest.raises(OSError, match="token cache replacement failed"):
        session.exchange_authorization_code("authorization-code")

    cached = schwab_token_store.load_token_payload(strict=True)
    assert cached is not None
    assert cached["access_token"] == "new-access-token"
    assert cached["refresh_token"] == "new-refresh-token"
    assert schwab_token_store.has_uncertain_refresh_attempt(cached)


def test_token_refresh_single_flight_uses_process_file_lock(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    child = r"""
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import app.services.schwab as schwab_module
import app.services.schwab_token_store as token_store
from app.services.schwab import SchwabSession

token_store.TOKEN_CACHE_PATH = Path(sys.argv[1])
call_log = Path(sys.argv[2])
ready = Path(sys.argv[3])
start_gate = Path(sys.argv[4])

class Response:
    def raise_for_status(self):
        return None

    def json(self):
        return {
            "access_token": "shared-access-token",
            "refresh_token": "shared-refresh-token",
            "expires_in": 1800,
            "refresh_token_expires_in": 604800,
        }

def post(*_args, **_kwargs):
    with call_log.open("a", encoding="utf-8") as handle:
        handle.write("post\n")
        handle.flush()
    time.sleep(0.2)
    return Response()

schwab_module.requests.post = post
ready.write_text("ready", encoding="utf-8")
deadline = time.monotonic() + 10.0
while not start_gate.exists():
    if time.monotonic() >= deadline:
        raise TimeoutError("multiprocess start gate was never released")
    time.sleep(0.005)
session = object.__new__(SchwabSession)
session.config = SimpleNamespace(client_id="client", client_secret="secret")
session.account_hash = None
session._access_token_lock = threading.RLock()
session._account_hash_lock = threading.Lock()
session._hydrate_from_cache()
session.ensure_access_token()
assert session.access_token == "shared-access-token"
"""
    for round_index in range(5):
        round_root = tmp_path / f"round-{round_index}"
        token_path = round_root / "shared" / "schwab_tokens.json"
        call_log = round_root / "oauth-posts.log"
        start_gate = round_root / "start"
        ready_paths = [round_root / f"ready-{index}" for index in range(2)]
        monkeypatch.setattr(schwab_token_store, "TOKEN_CACHE_PATH", token_path)
        schwab_token_store.write_token_payload_atomic(
            {
                "access_token": "expired-access-token",
                "access_token_expires_at": "2020-01-01T00:00:00+00:00",
                "refresh_token": "refresh-token-0",
                "refresh_token_expires_at": "2099-01-01T00:00:00+00:00",
            }
        )
        processes = [
            subprocess.Popen(
                [
                    sys.executable,
                    "-c",
                    child,
                    str(token_path),
                    str(call_log),
                    str(ready_path),
                    str(start_gate),
                ],
                cwd=os.path.dirname(os.path.dirname(__file__)),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            for ready_path in ready_paths
        ]
        ready_deadline = time.monotonic() + 10.0
        while not all(path.is_file() for path in ready_paths):
            if time.monotonic() >= ready_deadline:
                pytest.fail("child processes did not reach the start barrier")
            time.sleep(0.005)
        start_gate.write_text("start", encoding="utf-8")
        completed = [process.communicate(timeout=15) for process in processes]

        assert [process.returncode for process in processes] == [0, 0], completed
        assert call_log.read_text(encoding="utf-8").splitlines() == ["post"]
        assert token_path.with_name(f"{token_path.name}.lock").is_file()


@pytest.mark.skipif(os.name != "nt", reason="Windows DACL behavior")
def test_token_cache_and_lock_have_owner_only_windows_dacls(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    powershell = shutil.which("pwsh") or shutil.which("powershell")
    if powershell is None:
        pytest.skip("PowerShell is unavailable")
    token_path = tmp_path / "schwab_tokens.json"
    monkeypatch.setattr(schwab_token_store, "TOKEN_CACHE_PATH", token_path)
    schwab_token_store.write_token_payload_atomic(
        {
            "access_token": "access-token",
            "access_token_expires_at": "2099-01-01T00:00:00+00:00",
            "refresh_token": "refresh-token",
        }
    )

    def sddl(path) -> str:
        environment = dict(os.environ)
        environment["DUCKET_ACL_TARGET"] = str(path)
        result = subprocess.run(
            [
                powershell,
                "-NoProfile",
                "-Command",
                "(Get-Acl -LiteralPath $env:DUCKET_ACL_TARGET).Sddl",
            ],
            check=True,
            capture_output=True,
            text=True,
            env=environment,
        )
        return result.stdout.strip()

    assert "D:P(A;;FA;;;OW)" in sddl(token_path)
    assert "D:P(A;;FA;;;OW)" in sddl(
        token_path.with_name(f"{token_path.name}.lock")
    )


def test_connect_timeout_restores_cache_and_allows_later_refresh(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        schwab_token_store, "TOKEN_CACHE_PATH", tmp_path / "schwab_tokens.json"
    )
    schwab_token_store.write_token_payload_atomic(
        {
            "access_token": "expired-access-token",
            "access_token_expires_at": "2020-01-01T00:00:00+00:00",
            "refresh_token": "refresh-token-0",
            "refresh_token_expires_at": "2099-01-01T00:00:00+00:00",
        }
    )
    calls = 0

    class Response:
        @staticmethod
        def raise_for_status() -> None:
            return None

        @staticmethod
        def json() -> dict[str, object]:
            return {
                "access_token": "access-token-1",
                "refresh_token": "refresh-token-1",
                "expires_in": 1_800,
                "refresh_token_expires_in": 604_800,
            }

    def post(*_args, **_kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise requests.ConnectTimeout("definite pre-connect timeout")
        return Response()

    def build_session() -> SchwabSession:
        session = object.__new__(SchwabSession)
        session.config = SimpleNamespace(client_id="client", client_secret="secret")
        session.account_hash = None
        session._access_token_lock = threading.RLock()
        session._account_hash_lock = threading.Lock()
        session._hydrate_from_cache()
        return session

    monkeypatch.setattr(schwab_module.requests, "post", post)
    with pytest.raises(requests.ConnectTimeout) as failure:
        build_session().ensure_access_token()
    assert getattr(failure.value, "schwab_retry_safe") is True
    cached = schwab_token_store.load_token_payload(strict=True)
    assert cached is not None
    assert "refresh_attempt" not in cached

    second_session = build_session()
    second_session.ensure_access_token()
    assert calls == 2
    assert second_session.access_token == "access-token-1"


def test_oauth_429_restores_cache_and_remains_retryable(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        schwab_token_store, "TOKEN_CACHE_PATH", tmp_path / "schwab_tokens.json"
    )
    original = {
        "access_token": "expired-access-token",
        "access_token_expires_at": "2020-01-01T00:00:00+00:00",
        "refresh_token": "refresh-token",
        "refresh_token_expires_at": "2099-01-01T00:00:00+00:00",
    }
    schwab_token_store.write_token_payload_atomic(original)

    class Response:
        status_code = 429

        def raise_for_status(self) -> None:
            raise requests.HTTPError("rate limited", response=self)

    monkeypatch.setattr(schwab_module.requests, "post", lambda *_a, **_k: Response())
    session = object.__new__(SchwabSession)
    session.config = SimpleNamespace(client_id="client", client_secret="secret")
    session.access_token = None
    session.access_token_expires_at = None
    session.refresh_token = "refresh-token"
    session.account_hash = None
    session._access_token_lock = threading.RLock()
    session._account_hash_lock = threading.Lock()
    session._hydrate_from_cache()

    with pytest.raises(requests.HTTPError) as failure:
        session.ensure_access_token()

    assert getattr(failure.value, "schwab_retry_safe") is True
    assert is_retryable_schwab_error(failure.value)
    assert schwab_token_store.load_token_payload(strict=True) == original


@pytest.mark.parametrize(
    ("status_code", "oauth_error_code"),
    [(400, "invalid_grant"), (401, "invalid_client")],
)
def test_rejected_refresh_requires_reauthorization_without_duplicate_post(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
    status_code: int,
    oauth_error_code: str,
) -> None:
    monkeypatch.setattr(
        schwab_token_store, "TOKEN_CACHE_PATH", tmp_path / "schwab_tokens.json"
    )
    schwab_token_store.write_token_payload_atomic(
        {
            "access_token": "expired-access-token",
            "access_token_expires_at": "2020-01-01T00:00:00+00:00",
            "refresh_token": "rejected-refresh-token",
            "refresh_token_expires_at": "2099-01-01T00:00:00+00:00",
        }
    )
    calls: list[tuple[str, dict[str, object]]] = []
    sensitive_description = "provider-description-must-not-leak"

    class Response:
        def __init__(self) -> None:
            self.status_code = status_code

        def json(self) -> dict[str, str]:
            return {
                "error": oauth_error_code,
                "error_description": sensitive_description,
            }

        def raise_for_status(self) -> None:
            raise requests.HTTPError(sensitive_description, response=self)

    def post(url: str, **kwargs: object) -> Response:
        calls.append((url, kwargs))
        return Response()

    def build_session() -> SchwabSession:
        session = object.__new__(SchwabSession)
        session.config = SimpleNamespace(client_id="client", client_secret="secret")
        session.account_hash = None
        session._access_token_lock = threading.RLock()
        session._account_hash_lock = threading.Lock()
        session._hydrate_from_cache()
        return session

    monkeypatch.setattr(schwab_module.requests, "post", post)
    with pytest.raises(requests.HTTPError) as first_failure:
        build_session().ensure_access_token()

    assert str(first_failure.value) == (
        f"Schwab OAuth token request failed (HTTP {status_code}; "
        f"error={oauth_error_code})"
    )
    assert sensitive_description not in str(first_failure.value)
    assert getattr(first_failure.value, "schwab_retry_safe") is False
    assert getattr(first_failure.value, "schwab_reauthorization_required") is True
    assert getattr(first_failure.value, "schwab_oauth_error_code") == oauth_error_code
    assert calls == [
        (
            schwab_module.TOKEN_URL,
            {
                "headers": {"Content-Type": "application/x-www-form-urlencoded"},
                "data": {
                    "grant_type": "refresh_token",
                    "refresh_token": "rejected-refresh-token",
                },
                "auth": ("client", "secret"),
                "timeout": 10,
            },
        )
    ]
    cached = schwab_token_store.load_token_payload(strict=True)
    assert cached is not None
    assert schwab_token_store.refresh_reauthorization_required(cached)
    assert cached["refresh_attempt"] == {
        "id": cached["refresh_attempt"]["id"],
        "started_at": cached["refresh_attempt"]["started_at"],
        "status": "FAILED_REAUTH_REQUIRED",
        "error_type": f"HTTPError:{status_code}",
        "oauth_error_code": oauth_error_code,
    }
    assert sensitive_description not in repr(cached)

    with pytest.raises(RuntimeError, match=oauth_error_code) as second_failure:
        build_session().ensure_access_token()
    assert getattr(second_failure.value, "schwab_retry_safe") is False
    assert getattr(second_failure.value, "schwab_reauthorization_required") is True
    assert len(calls) == 1


def test_unallowlisted_oauth_body_is_not_logged_but_http_rejection_is_persisted(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        schwab_token_store, "TOKEN_CACHE_PATH", tmp_path / "schwab_tokens.json"
    )
    original = {
        "access_token": "expired-access-token",
        "access_token_expires_at": "2020-01-01T00:00:00+00:00",
        "refresh_token": "refresh-token",
        "refresh_token_expires_at": "2099-01-01T00:00:00+00:00",
    }
    schwab_token_store.write_token_payload_atomic(original)
    sensitive_error = "unexpected-provider-secret"

    class Response:
        status_code = 400

        @staticmethod
        def json() -> dict[str, str]:
            return {"error": sensitive_error}

        def raise_for_status(self) -> None:
            raise requests.HTTPError(sensitive_error, response=self)

    monkeypatch.setattr(schwab_module.requests, "post", lambda *_a, **_k: Response())
    session = object.__new__(SchwabSession)
    session.config = SimpleNamespace(client_id="client", client_secret="secret")
    session.account_hash = None
    session._access_token_lock = threading.RLock()
    session._account_hash_lock = threading.Lock()
    session._hydrate_from_cache()

    with pytest.raises(requests.HTTPError) as failure:
        session.ensure_access_token()

    assert str(failure.value) == "Schwab OAuth token request failed (HTTP 400)"
    assert sensitive_error not in str(failure.value)
    assert getattr(failure.value, "schwab_reauthorization_required", False)
    cached = schwab_token_store.load_token_payload(strict=True)
    assert cached is not None
    assert schwab_token_store.refresh_reauthorization_required(cached)
    assert cached["refresh_attempt"]["oauth_error_code"] == "http_400"
    assert sensitive_error not in repr(cached)


def test_nested_schwab_oauth_error_is_classified_without_leaking_description() -> None:
    sensitive_description = "account detail must not leak"

    class Response:
        status_code = 400

        @staticmethod
        def json() -> dict[str, str]:
            return {
                "error": "unsupported_token_type",
                "error_description": json.dumps(
                    {
                        "error": "invalid_grant",
                        "error_description": sensitive_description,
                    }
                ),
            }

        def raise_for_status(self) -> None:
            raise requests.HTTPError(sensitive_description, response=self)

    with pytest.raises(requests.HTTPError) as failure:
        schwab_module._raise_for_oauth_status(Response())

    assert str(failure.value) == (
        "Schwab OAuth token request failed (HTTP 400; error=invalid_grant)"
    )
    assert sensitive_description not in str(failure.value)
    assert getattr(failure.value, "schwab_oauth_error_code") == "invalid_grant"


def test_schwab_oauth_error_propagates_safe_client_correlid() -> None:
    correlid = "977dbd7f-992e-44d2-a5fa-e213d29c8691"
    sensitive_description = "authorization-code-and-token-must-not-leak"

    class Response:
        status_code = 400
        headers = {"sChWaB-cLiEnT-cOrReLiD": correlid}

        @staticmethod
        def json() -> dict[str, str]:
            return {
                "error": "invalid_grant",
                "error_description": sensitive_description,
            }

        def raise_for_status(self) -> None:
            raise requests.HTTPError(sensitive_description, response=self)

    with pytest.raises(requests.HTTPError) as failure:
        schwab_module._raise_for_oauth_status(Response())

    assert str(failure.value) == (
        "Schwab OAuth token request failed "
        f"(HTTP 400; error=invalid_grant; correlid={correlid})"
    )
    assert getattr(failure.value, "schwab_client_correlid") == correlid
    assert sensitive_description not in str(failure.value)


def test_schwab_oauth_error_suppresses_unsafe_client_correlid() -> None:
    unsafe_correlid = "support-id; Authorization: Bearer secret-token"

    class Response:
        status_code = 400
        headers = {"Schwab-Client-Correlid": unsafe_correlid}

        @staticmethod
        def json() -> dict[str, str]:
            return {"error": "invalid_grant"}

        def raise_for_status(self) -> None:
            raise requests.HTTPError(unsafe_correlid, response=self)

    with pytest.raises(requests.HTTPError) as failure:
        schwab_module._raise_for_oauth_status(Response())

    assert str(failure.value) == (
        "Schwab OAuth token request failed (HTTP 400; error=invalid_grant)"
    )
    assert not hasattr(failure.value, "schwab_client_correlid")
    assert unsafe_correlid not in str(failure.value)
    assert "secret-token" not in str(failure.value)


def test_concurrent_duplicate_authorization_rejection_preserves_success(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        schwab_token_store, "TOKEN_CACHE_PATH", tmp_path / "schwab_tokens.json"
    )
    calls = 0

    class SuccessResponse:
        @staticmethod
        def raise_for_status() -> None:
            return None

        @staticmethod
        def json() -> dict[str, object]:
            return {
                "access_token": "new-access-token",
                "refresh_token": "new-refresh-token",
                "expires_in": 1_800,
                "refresh_token_expires_in": 604_800,
            }

    class RejectedResponse:
        status_code = 400

        def raise_for_status(self) -> None:
            raise requests.HTTPError("invalid_grant", response=self)

    def post(*_args, **_kwargs):
        nonlocal calls
        calls += 1
        return SuccessResponse() if calls == 1 else RejectedResponse()

    def build_session() -> SchwabSession:
        session = object.__new__(SchwabSession)
        session.config = SimpleNamespace(
            client_id="client",
            client_secret="secret",
            redirect_uri="https://example.test/callback",
        )
        session.access_token = None
        session.access_token_expires_at = None
        session.refresh_token = None
        session.account_hash = None
        session._access_token_lock = threading.RLock()
        session._account_hash_lock = threading.Lock()
        return session

    monkeypatch.setattr(schwab_module.requests, "post", post)
    sessions = [build_session(), build_session()]
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [
            pool.submit(session.exchange_authorization_code, "same-code")
            for session in sessions
        ]
        failures = [future.exception() for future in futures]

    assert sum(failure is None for failure in failures) == 1
    assert calls == 2
    cached = schwab_token_store.load_token_payload(strict=True)
    assert cached is not None
    assert cached["access_token"] == "new-access-token"
    assert "refresh_attempt" not in cached


def test_empty_authorization_code_does_not_mark_valid_cache(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        schwab_token_store, "TOKEN_CACHE_PATH", tmp_path / "schwab_tokens.json"
    )
    original = {
        "access_token": "valid-access-token",
        "access_token_expires_at": "2099-01-01T00:00:00+00:00",
        "refresh_token": "valid-refresh-token",
        "refresh_token_expires_at": "2099-01-01T00:00:00+00:00",
    }
    schwab_token_store.write_token_payload_atomic(original)
    session = object.__new__(SchwabSession)

    with pytest.raises(ValueError, match="authorization code is required"):
        session.exchange_authorization_code("   ")

    assert schwab_token_store.load_token_payload(strict=True) == original


def test_submit_order_uses_hash_and_header_from_new_token_generation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = object.__new__(SchwabSession)
    session.access_token = "old-access-token"
    session.access_token_expires_at = datetime.now(timezone.utc) + timedelta(hours=1)
    session.refresh_token = "old-refresh-token"
    session.account_hash = "old-account-hash"
    session._access_token_lock = threading.RLock()
    session._account_hash_lock = threading.Lock()
    captured: dict[str, object] = {}

    def ensure_access_token() -> None:
        session._adopt_cached_payload(
            {
                "access_token": "new-access-token",
                "access_token_expires_at": "2099-01-01T00:00:00+00:00",
                "refresh_token": "new-refresh-token",
                "refresh_token_expires_at": "2099-01-01T00:00:00+00:00",
            }
        )

    class AccountResponse:
        @staticmethod
        def raise_for_status() -> None:
            return None

        @staticmethod
        def json() -> list[dict[str, str]]:
            return [{"hashValue": "new-account-hash"}]

    class OrderResponse:
        headers = {"Location": "/orders/1"}

        @staticmethod
        def raise_for_status() -> None:
            return None

    def get(*_args, **kwargs):
        captured["identity_headers"] = kwargs["headers"]
        return AccountResponse()

    def post(url: str, **kwargs):
        captured.update(url=url, **kwargs)
        return OrderResponse()

    session.ensure_access_token = ensure_access_token
    monkeypatch.setattr(schwab_module.requests, "get", get)
    monkeypatch.setattr(schwab_module.requests, "post", post)

    assert session.submit_order({"orderType": "LIMIT"}) == "/orders/1"
    assert captured["url"].endswith("/accounts/new-account-hash/orders")
    assert captured["identity_headers"] == {
        "Authorization": "Bearer new-access-token",
        "Accept": "application/json",
    }
    assert captured["headers"] == {
        "Authorization": "Bearer new-access-token",
        "Accept": "application/json",
        "Content-Type": "application/json",
    }


def test_parallel_snapshot_preflight_does_not_fan_out_account_identity_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = object.__new__(SchwabSession)
    session.access_token = "access-token"
    session.access_token_expires_at = datetime.now(timezone.utc) + timedelta(hours=1)
    session.refresh_token = "refresh-token"
    session.account_hash = None
    session._access_token_lock = threading.RLock()
    session._account_hash_lock = threading.Lock()
    calls = 0

    def get(*_args, **_kwargs):
        nonlocal calls
        calls += 1
        raise requests.ReadTimeout("Schwab account identity read timed out")

    monkeypatch.setattr(schwab_module.requests, "get", get)
    with pytest.raises(requests.ReadTimeout) as failure:
        capture_portfolio_state(
            session,
            observed_at="2026-08-31T16:00:00+00:00",
            parallel=True,
        )

    assert calls == 1
    assert getattr(failure.value, "stock_trader_operation") == "account_identity"


def test_schwab_snapshot_identity_detects_account_or_oauth_generation_change() -> None:
    session = object.__new__(SchwabSession)
    session.access_token = "first-access-token"
    session.access_token_expires_at = datetime.now(timezone.utc) + timedelta(hours=1)
    session.refresh_token = "first-refresh-token"
    session.account_hash = "first-account-hash"
    session._access_token_lock = threading.RLock()
    session._account_hash_lock = threading.Lock()

    captured_identity = session.prepare_read_snapshot()
    session.access_token = "replacement-access-token"
    session.refresh_token = "replacement-refresh-token"
    session.account_hash = "replacement-account-hash"

    with pytest.raises(RuntimeError, match="identity or authorization changed") as failure:
        session.verify_read_snapshot(captured_identity)

    assert getattr(failure.value, "schwab_retry_safe") is True
    assert getattr(failure.value, "stock_trader_operation") == "account_identity"
    prepared = session.prepare_order_submission()
    assert prepared.identity_fingerprint != captured_identity


def test_schwab_cash_and_sweep_includes_short_sale_credit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    account_payload: dict[str, Any] = {
        "securitiesAccount": {
            "accountNumber": "12345678",
            "currentBalances": {
                "cashBalance": 55_668.78,
                "shortBalance": 74_799.98,
                "liquidationValue": 130_604.90,
            },
            "positions": [
                {
                    "instrument": {"assetType": "EQUITY", "symbol": "LONGS"},
                    "longQuantity": 1.0,
                    "shortQuantity": 0.0,
                    "marketValue": 73_548.13,
                },
                {
                    "instrument": {"assetType": "EQUITY", "symbol": "MU"},
                    "longQuantity": 0.0,
                    "shortQuantity": 80.0,
                    "marketValue": -73_411.99,
                },
            ],
        }
    }

    class StubSchwabSession:
        def get_account(self) -> dict[str, Any]:
            return account_payload

        def get_open_orders(self) -> list[object]:
            return []

    monkeypatch.setattr(schwab_module, "SchwabSession", StubSchwabSession)

    snapshot = schwab_module.sync_schwab_portfolio()

    assert snapshot.cash_value == pytest.approx(130_468.76)
    assert snapshot.cash[0].bucket == "Cash & sweep"
    assert snapshot.holdings_value == pytest.approx(136.14)
    assert {holding.bucket for holding in snapshot.holdings} == {"Stock"}
    assert snapshot.total_value == pytest.approx(130_604.90)
    assert snapshot.cash_value + snapshot.holdings_value == pytest.approx(
        snapshot.total_value
    )
    assert snapshot.account_facts["account_values"]["short_balance"] == pytest.approx(
        74_799.98
    )


def test_schwab_cash_without_short_credit_remains_unchanged(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class StubSchwabSession:
        def get_account(self) -> dict[str, Any]:
            return {
                "securitiesAccount": {
                    "currentBalances": {
                        "cashBalance": 20_000.0,
                        "liquidationValue": 20_000.0,
                    },
                    "positions": [],
                }
            }

        def get_open_orders(self) -> list[object]:
            return []

    monkeypatch.setattr(schwab_module, "SchwabSession", StubSchwabSession)

    snapshot = schwab_module.sync_schwab_portfolio()

    assert snapshot.cash_value == pytest.approx(20_000.0)
    assert snapshot.total_value == pytest.approx(20_000.0)


@pytest.mark.parametrize(
    ("row", "expected_day_pnl"),
    [
        (
            {
                "instrument": {
                    "assetType": "EQUITY",
                    "symbol": "NVDA",
                    "netChange": 18.91,
                },
                "longQuantity": 25.0,
                "shortQuantity": 0.0,
                "marketValue": 5_715.50,
                "currentDayProfitLoss": -41.25,
            },
            472.75,
        ),
        (
            {
                "instrument": {
                    "assetType": "EQUITY",
                    "symbol": "MU",
                    "netChange": -19.283625,
                },
                "longQuantity": 0.0,
                "shortQuantity": 80.0,
                "marketValue": -73_525.60,
                "currentDayProfitLoss": 1_249.20,
            },
            1_542.69,
        ),
        (
            {
                "instrument": {
                    "assetType": "OPTION",
                    "symbol": "AAPL  260828C00312500",
                    "netChange": -0.90,
                },
                "longQuantity": 1.0,
                "shortQuantity": 0.0,
                "marketValue": 238.50,
                "currentDayProfitLoss": 87.50,
            },
            87.50,
        ),
    ],
)
def test_schwab_day_pnl_matches_thinkorswim_semantics(
    row: dict[str, Any],
    expected_day_pnl: float,
) -> None:
    holding = schwab_module._holding_from_schwab(row)

    assert holding is not None
    assert holding.day_pnl == pytest.approx(expected_day_pnl)


def test_schwab_option_uses_option_bucket_and_per_share_quote_mark() -> None:
    holding = schwab_module._holding_from_schwab(
        {
            "instrument": {
                "assetType": "OPTION",
                "symbol": "AAPL  260828C00312500",
            },
            "longQuantity": 1.0,
            "shortQuantity": 0.0,
            "marketValue": 297.50,
        },
        option_quote={"mark": 2.975},
    )

    assert holding is not None
    assert holding.bucket == "Option"
    assert holding.price == pytest.approx(2.975)
    assert holding.value == pytest.approx(297.50)


def test_schwab_summary_separates_stock_etf_and_option_values() -> None:
    snapshot = PortfolioSnapshot(
        source="schwab",
        account_label="Schwab",
        holdings=[
            Holding("MU", -80, 934.66, -74_772.80, "schwab", "Stock"),
            Holding("VXUS", 30, 87.90, 2_637.00, "schwab", "ETF"),
            Holding(
                "MU    260831C00940000",
                1,
                16.425,
                1_642.50,
                "schwab",
                "Option",
            ),
        ],
    )
    bucket = DucketBucketSnapshot([snapshot])

    assert bucket.holdings_value_for("Stock", "ETF") == pytest.approx(-72_135.80)
    assert bucket.holdings_value_for("Option") == pytest.approx(1_642.50)
    assert bucket.holdings_value == pytest.approx(-70_493.30)
