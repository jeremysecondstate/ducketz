"""Persistent per-symbol historical boundaries for explicitly onboarded stocks."""
from datetime import date, datetime, timezone
import hashlib
import json
from pathlib import Path


def policy_digest(payload: dict) -> str:
    return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def read_history_policy(root: Path, symbol: str) -> dict | None:
    path = Path(root)/'state/symbol-history-policy'/f'{symbol}.json'
    if not path.exists(): return None
    payload = json.loads(path.read_text(encoding='utf-8'))
    data = {k:v for k,v in payload.items() if k != 'sha256'}
    if payload.get('sha256') != policy_digest(data) or data.get('symbol') != symbol:
        raise ValueError('Historical scope policy checksum or symbol is invalid')
    if date.fromisoformat(data['history_floor']) < date(2018,1,1):
        raise ValueError('Research history floor cannot precede 2018')
    date.fromisoformat(data['listing_date'])
    return data


def filter_dated_payload(payload, floor: str):
    if isinstance(payload, list):
        return [row for row in payload if not isinstance(row,dict) or not row.get('date') or str(row['date'])[:10] >= floor]
    if isinstance(payload, dict) and isinstance(payload.get('historical'), list):
        return {**payload, 'historical': filter_dated_payload(payload['historical'],floor)}
    return payload


def price_floor(policy: dict) -> datetime:
    return datetime.fromisoformat(max(policy['history_floor'], policy['listing_date'])).replace(tzinfo=timezone.utc)
