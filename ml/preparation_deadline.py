"""Explicit, source-bound operator exceptions for a late non-trading tail."""
from __future__ import annotations

import json
from pathlib import Path

import pandas as pd

from ml.artifacts import file_checksum

VERSION = 'operator-preparation-deadline-exception-v1'


def _aware(value):
    stamp = pd.Timestamp(value)
    if pd.isna(stamp) or stamp.tzinfo is None:
        raise ValueError('Preparation exception timestamps must be timezone aware')
    return stamp.tz_convert('UTC')


def preparation_deadline(root: Path, gameplan_run: Path, original_deadline,
                         observed_at, exception_path: Path | None = None):
    """Keep the original deadline unless an explicitly supplied record verifies.

    This does not grant execution authority, change a forecast or replay slots.
    Only callers in the planning/actuals tail support this exception.
    """
    original, now = _aware(original_deadline), _aware(observed_at)
    if exception_path is None:
        return original, None
    root, source = Path(root).resolve(), Path(gameplan_run).resolve()
    if source.parent != root/'ml/nightly-gameplan-runs':
        raise ValueError('Preparation exception source escapes the Gameplan archive')
    payload = json.loads(Path(exception_path).read_text(encoding='utf-8'))
    receipt = json.loads((source/'receipt.json').read_text(encoding='utf-8'))
    session = str(receipt['action_date'])
    opening = pd.Timestamp(session).tz_localize('America/Los_Angeles') + pd.Timedelta(hours=4)
    close = opening + pd.Timedelta(hours=13)
    approved, expires = _aware(payload.get('approved_at')), _aware(payload.get('expires_at'))
    if (payload.get('schema_version') != VERSION
            or payload.get('scope') != 'PINNED_STOCK_PLANNING_AND_ACTUALS_ONLY'
            or payload.get('operator_authorized') is not True
            or not str(payload.get('authorization_text', '')).strip()
            or not str(payload.get('authorization_source', '')).strip()
            or payload.get('orders_authorized') is not False
            or payload.get('gameplan_run') != source.relative_to(root).as_posix()
            or payload.get('gameplan_receipt_sha256') != file_checksum(source/'receipt.json')
            or payload.get('action_date') != session
            or _aware(payload.get('original_deadline_at')) != original
            or original != opening.tz_convert('UTC')
            or not original <= approved <= now < expires <= close.tz_convert('UTC')):
        raise ValueError('Preparation deadline exception is invalid, expired, or has a different source')
    return expires, {'path':str(Path(exception_path).resolve()),
                     'sha256':file_checksum(Path(exception_path)), 'authorization':payload}
