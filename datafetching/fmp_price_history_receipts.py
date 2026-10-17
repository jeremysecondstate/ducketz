"""Immutable native FMP five-minute observations; no import-time acquisition.

The explicit acquisition API uses existing authentication. A provider response
is evidence of that source's observations, never consolidated market coverage.
Unadjusted and extended flags are recorded as requests, not response guarantees.
"""
from __future__ import annotations

from datetime import date, datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
from typing import Mapping
from uuid import uuid4

import numpy as np
import pandas as pd

VERSION = 'fmp-native-five-minute-receipt-v1'
SOURCE = 'fmp:historical-chart:5min'
DATASET = 'FMP.HISTORICAL_CHART_5MIN'
ENDPOINT = 'historical-chart/5min'
OBSERVATION_COLUMNS = ('symbol', 'timestamp', 'open', 'high', 'low', 'close', 'volume',
    'observation_source', 'observation_dataset', 'observation_interval_seconds',
    'observation_input_sha256', 'observation_recorded_at')
MARKERS = ('synthetic', 'is_synthetic', 'no_trade', 'is_no_trade', 'source_empty', 'is_source_empty')


def _encoded(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False).encode('utf-8')


def _symbol(value):
    value = str(value).strip().upper()
    if not re.fullmatch(r'[A-Z][A-Z0-9.\-]{0,14}', value):
        raise ValueError('Invalid equity symbol')
    return value


def _clock(value):
    stamp = pd.Timestamp(value)
    if pd.isna(stamp) or stamp.tzinfo is None:
        raise ValueError('Receipt clocks must be timezone aware')
    return stamp.tz_convert('UTC')


def _request(symbol, start_date, end_date):
    start, end = date.fromisoformat(str(start_date)), date.fromisoformat(str(end_date))
    if not 0 <= (end - start).days <= 6:
        raise ValueError('Native FMP capture requires one through seven inclusive calendar days')
    return {'symbol': _symbol(symbol), 'from': start.isoformat(), 'to': end.isoformat(),
            'nonadjusted': 'true', 'extended': 'true'}


def _validate_payload(payload):
    if not isinstance(payload, list) or len(payload) > 7 * 288:
        raise ValueError('Native FMP payload is not a bounded chart array')
    for row in payload:
        if not isinstance(row, dict) or not {'date', 'open', 'high', 'low', 'close', 'volume'}.issubset(row):
            raise ValueError('Native FMP row lacks OHLCV/date identity')
        if any(re.search(r'api.?key|token|authorization|password|secret', str(k), re.I) for k in row):
            raise ValueError('Credential-shaped provider payload rejected')
        if any(isinstance(value, (dict, list)) for value in row.values()):
            raise ValueError('Unexpected nested chart payload rejected')


def _retain(root, symbol, params, payload, *, started_at, received_at):
    _validate_payload(payload)
    started, received = _clock(started_at), _clock(received_at)
    if started > received:
        raise ValueError('Request clock follows receipt')
    envelope = {'version': VERSION, 'provider': 'fmp', 'dataset': DATASET,
        'endpoint': ENDPOINT, 'symbol': symbol, 'request': params,
        'request_started_at': started.isoformat(), 'received_at': received.isoformat(),
        'interval_seconds': 300, 'timestamp_convention': 'interval_open',
        'provider_timestamp_timezone': 'America/New_York',
        'requested_corporate_action_adjustment': 'none', 'requested_extended_session': True,
        'response_adjustment_and_session_flags_attested': False,
        'payload_sha256': hashlib.sha256(_encoded(payload)).hexdigest(), 'payload': payload}
    encoded = _encoded(envelope)
    digest = hashlib.sha256(encoded).hexdigest()
    folder = Path(root) / 'stocks' / symbol / 'bars/5m/fmp/raw-history'
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / (digest + '.json')
    temporary = path.with_name(path.name + '.pending-' + uuid4().hex)
    try:
        with temporary.open('xb') as output:
            output.write(encoded)
            output.flush()
            os.fsync(output.fileno())
        # Publish only complete bytes. Readers ignore the unique pending file;
        # link is atomic and cannot replace a concurrent immutable receipt.
        os.link(temporary, path)
    except FileExistsError:
        if path.read_bytes() != encoded:
            raise RuntimeError('Immutable FMP receipt conflicts with existing bytes')
    finally:
        temporary.unlink(missing_ok=True)
    return {'symbol': symbol, 'status': 'ok' if payload else 'empty', 'rows': len(payload),
        'received_at': received.isoformat(), 'path': str(path), 'sha256': digest,
        'payload_sha256': envelope['payload_sha256'], 'request': params}


def capture_native_five_minute_history(datastore_root, symbols, *, start_date, end_date,
                                     provider=None, reused_receipts: Mapping | None = None):
    """Capture a bounded inclusive NY-date window independently per symbol.

    Reused receipts require the exact original request, payload and actual
    request/receipt clocks. They make no additional provider request. Failures
    remain explicit, with exception types only to avoid secret-bearing URLs.
    """
    if isinstance(symbols, str):
        raise ValueError('Pass an explicit sequence of equity symbols')
    symbols = tuple(dict.fromkeys(_symbol(s) for s in symbols))
    if not symbols or len(symbols) > 64:
        raise ValueError('Capture requires 1..64 explicitly configured equities')
    requests = {s: _request(s, start_date, end_date) for s in symbols}
    if provider is None and set(symbols) - set(reused_receipts or {}):
        from app.services.fmp_corporate_data import FmpCorporateDataProvider
        provider = FmpCorporateDataProvider(timeout_seconds=15)
    results = []
    for symbol, params in requests.items():
        started = datetime.now(timezone.utc).isoformat()
        reused = (reused_receipts or {}).get(symbol)
        try:
            if reused is not None:
                if reused['params'] != params or reused['endpoint'] != ENDPOINT:
                    raise ValueError('Reused request differs from requested native chart identity')
                payload, started, received = reused['payload'], reused['request_started_at'], reused['received_at']
                if reused.get('payload_sha256') != hashlib.sha256(_encoded(payload)).hexdigest():
                    raise ValueError('Reused payload hash differs')
            else:
                payload = provider._get_json(ENDPOINT, params)
                received = datetime.now(timezone.utc).isoformat()
                secret = getattr(provider, 'api_key', '')
                if secret and secret.encode() in _encoded(payload):
                    raise ValueError('Credential-bearing response rejected')
            result = _retain(datastore_root, symbol, params, payload,
                             started_at=started, received_at=received)
            result['reused_original_receipt'] = reused is not None
        except Exception as exc:
            result = {'symbol': symbol, 'status': 'error', 'error_type': type(exc).__name__,
                'request_started_at': started, 'attempt_completed_at': datetime.now(timezone.utc).isoformat(),
                'request': params, 'reused_original_receipt': reused is not None}
        results.append(result)
    return {'version': VERSION, 'source': SOURCE, 'receipts': results,
            'actual_provider_requests': sum(not r['reused_original_receipt'] for r in results),
            'interpretation': 'Per-source requests and receipts, not proof of complete or market-wide observations'}


def load_native_five_minute_history(datastore_root, *, symbol):
    """Verify immutable receipts and project genuine completed native candles.

    Dated revisions remain separate until each consumer applies its own evidence
    cutoff. Missing/invalid observations are excluded, never replaced or filled.
    """
    symbol = _symbol(symbol)
    folder = Path(datastore_root) / 'stocks' / symbol / 'bars/5m/fmp/raw-history'
    frames, paths, inventory = [], [], []
    for path in sorted(folder.glob('*.json')):
        encoded = path.read_bytes()
        digest = hashlib.sha256(encoded).hexdigest()
        if digest != path.stem:
            raise RuntimeError('Native FMP receipt checksum mismatch')
        row = json.loads(encoded)
        params = row['request']
        expected = _request(symbol, params['from'], params['to'])
        if (row.get('version') != VERSION or row.get('provider') != 'fmp'
                or row.get('dataset') != DATASET or row.get('endpoint') != ENDPOINT
                or row.get('symbol') != symbol or params != expected
                or row.get('interval_seconds') != 300 or row.get('timestamp_convention') != 'interval_open'
                or row.get('provider_timestamp_timezone') != 'America/New_York'
                or row.get('requested_corporate_action_adjustment') != 'none'
                or row.get('requested_extended_session') is not True
                or row.get('response_adjustment_and_session_flags_attested') is not False):
            raise ValueError('Native FMP receipt source identity mismatch')
        payload = row['payload']
        _validate_payload(payload)
        if hashlib.sha256(_encoded(payload)).hexdigest() != row['payload_sha256']:
            raise RuntimeError('Native FMP payload checksum mismatch')
        received = _clock(row['received_at'])
        if _clock(row['request_started_at']) > received:
            raise ValueError('Invalid native FMP acquisition clocks')
        frame = pd.DataFrame(payload)
        valid = pd.Series(True, index=frame.index)
        if len(frame):
            naive = frame.date.astype('string').str.fullmatch(r'\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}', na=False)
            clocks = pd.to_datetime(frame.date.where(naive), format='%Y-%m-%d %H:%M:%S', errors='coerce')
            dates = clocks.dt.strftime('%Y-%m-%d')
            frame['timestamp'] = clocks.dt.tz_localize('America/New_York', ambiguous='NaT', nonexistent='NaT').dt.tz_convert('UTC')
            for column in ('open', 'high', 'low', 'close', 'volume'):
                valid &= ~frame[column].map(lambda value: isinstance(value, (bool, np.bool_)))
                frame[column] = pd.to_numeric(frame[column], errors='coerce')
            price = frame[['open', 'high', 'low', 'close']]
            valid &= (frame.timestamp.notna() & dates.between(params['from'], params['to'])
                & frame.timestamp.dt.minute.mod(5).eq(0) & frame.timestamp.dt.second.eq(0)
                & frame.timestamp.dt.microsecond.eq(0)
                & frame.timestamp.add(pd.Timedelta(minutes=5)).le(received)
                & np.isfinite(frame[[*price, 'volume']]).all(axis=1)
                & price.gt(0).all(axis=1) & frame.volume.gt(0)
                & frame.high.ge(price.max(axis=1)) & frame.low.le(price.min(axis=1)))
            for marker in MARKERS:
                if marker in frame:
                    valid &= frame[marker].map(lambda value: isinstance(value, (bool, np.bool_)) and not bool(value))
            projected = frame.loc[valid, ['timestamp', 'open', 'high', 'low', 'close', 'volume']].copy()
            projected = projected.assign(symbol=symbol, observation_source=SOURCE, observation_dataset=DATASET,
                observation_interval_seconds=300, observation_input_sha256=digest, observation_recorded_at=received)
            frames.append(projected)
        paths.append(path)
        inventory.append({'path': str(path), 'sha256': digest, 'payload_sha256': row['payload_sha256'],
            'source': SOURCE, 'rows': int(valid.sum()), 'raw_rows': len(payload),
            'invalid_or_zero_volume_rows_excluded': int((~valid).sum()),
            'received_at': received.isoformat(), 'request': params,
            'response_adjustment_and_session_flags_attested': False})
    result = pd.concat(frames, ignore_index=True) if frames else pd.DataFrame(columns=OBSERVATION_COLUMNS)
    return result, tuple(paths), inventory
