from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[2]
TOKEN_CACHE_PATH = PROJECT_ROOT / "data" / "schwab_tokens.json"
ACCESS_TOKEN_EXPIRY_SAFETY_SECONDS = 60


def load_token_payload() -> dict[str, Any] | None:
    if not TOKEN_CACHE_PATH.exists():
        return None

    try:
        with TOKEN_CACHE_PATH.open("r", encoding="utf-8") as handle:
            payload = json.load(handle)
    except (OSError, json.JSONDecodeError):
        return None

    return payload if isinstance(payload, dict) else None


def save_token_payload(payload: dict[str, Any], previous_refresh_token: str | None = None) -> dict[str, Any]:
    TOKEN_CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)

    now = datetime.now(timezone.utc)
    expires_in = _int_value(payload.get("expires_in"), default=1800)
    access_token_expires_at = now + timedelta(seconds=max(expires_in - ACCESS_TOKEN_EXPIRY_SAFETY_SECONDS, 1))

    cached_payload = {
        "access_token": payload.get("access_token"),
        "refresh_token": str(payload.get("refresh_token") or previous_refresh_token or "").strip(),
        "token_type": payload.get("token_type"),
        "scope": payload.get("scope"),
        "access_token_expires_at": access_token_expires_at.isoformat(),
        "saved_at": now.isoformat(),
    }

    refresh_token_expires_in = _optional_int_value(payload.get("refresh_token_expires_in"))
    if refresh_token_expires_in is not None:
        cached_payload["refresh_token_expires_at"] = (now + timedelta(seconds=refresh_token_expires_in)).isoformat()

    with TOKEN_CACHE_PATH.open("w", encoding="utf-8") as handle:
        json.dump(cached_payload, handle, indent=2)

    return cached_payload


def access_token_is_fresh(payload: dict[str, Any] | None) -> bool:
    if not payload:
        return False

    access_token = payload.get("access_token")
    expires_at = _parse_datetime(payload.get("access_token_expires_at"))

    return bool(access_token and expires_at and expires_at > datetime.now(timezone.utc))


def refresh_token_is_available(payload: dict[str, Any] | None) -> bool:
    if not payload or not payload.get("refresh_token"):
        return False

    expires_at = _parse_datetime(payload.get("refresh_token_expires_at"))
    return expires_at is None or expires_at > datetime.now(timezone.utc)


def cached_access_token_expires_at(payload: dict[str, Any] | None) -> datetime | None:
    if not payload:
        return None

    return _parse_datetime(payload.get("access_token_expires_at"))


def _parse_datetime(value: Any) -> datetime | None:
    if not value:
        return None

    try:
        parsed = datetime.fromisoformat(str(value))
    except ValueError:
        return None

    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)

    return parsed.astimezone(timezone.utc)


def _int_value(value: Any, default: int) -> int:
    parsed = _optional_int_value(value)
    return parsed if parsed is not None else default


def _optional_int_value(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None