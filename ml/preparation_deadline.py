"""Explicit, source-bound operator exceptions for a late non-trading tail."""
from __future__ import annotations

import json
from pathlib import Path

import pandas as pd

from ml.artifacts import file_checksum

VERSION = 'operator-preparation-deadline-exception-v1'
RECOVERY_VERSION = 'operator-recovery-tail-continuation-v1'


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
    from ml.nightly_recovery import VERSION as PREPARATION_RECOVERY_VERSION, verify_recovery
    if payload.get('schema_version') == PREPARATION_RECOVERY_VERSION:
        evidence = verify_recovery(root, exception_path, now, action_date=session)
        from ml.artifacts import verify_manifest
        manifest = verify_manifest(source)
        if (manifest.get('configuration', {}).get('late_preparation') != evidence
                or _aware(payload['original_deadline_at']) != original
                or receipt.get('manifest_checksum_sha256') != file_checksum(source/'manifest.json')):
            raise ValueError('Late preparation exception differs from its frozen Gameplan')
        return _aware(payload['expires_at']), evidence
    opening = pd.Timestamp(session).tz_localize('America/Los_Angeles') + pd.Timedelta(hours=4)
    close = opening + pd.Timedelta(hours=13)
    approved, expires = _aware(payload.get('approved_at')), _aware(payload.get('expires_at'))
    recovery = payload.get('schema_version') == RECOVERY_VERSION
    if recovery:
        # A continuation is additional evidence, never a rewrite of the missed
        # 04:00 deadline or the failed recovery's fixed deadline.
        if (_aware(payload.get('original_session_deadline_at')) != opening.tz_convert('UTC')
                or not opening.tz_convert('UTC') < original < close.tz_convert('UTC')):
            raise ValueError('Recovery continuation must preserve both original deadlines')
    if (payload.get('schema_version') not in {VERSION, RECOVERY_VERSION}
            or payload.get('scope') != 'PINNED_STOCK_PLANNING_AND_ACTUALS_ONLY'
            or payload.get('operator_authorized') is not True
            or not str(payload.get('authorization_text', '')).strip()
            or not str(payload.get('authorization_source', '')).strip()
            or payload.get('orders_authorized') is not False
            or payload.get('gameplan_run') != source.relative_to(root).as_posix()
            or payload.get('gameplan_receipt_sha256') != file_checksum(source/'receipt.json')
            or payload.get('action_date') != session
            or _aware(payload.get('original_deadline_at')) != original
            or (not recovery and original != opening.tz_convert('UTC'))
            or not original <= approved <= now < expires <= close.tz_convert('UTC')):
        raise ValueError('Preparation deadline exception is invalid, expired, or has a different source')
    return expires, {'path':str(Path(exception_path).resolve()),
                     'sha256':file_checksum(Path(exception_path)), 'authorization':payload}
