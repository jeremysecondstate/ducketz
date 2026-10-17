"""Retain exact native price-history responses before mutable snapshots advance."""
from __future__ import annotations

from datetime import datetime, timezone
from hashlib import sha256
import json
from pathlib import Path


def preserve_price_history_raw(path: Path, *, history_dir: Path | None = None) -> Path | None:
    """Store immutable bytes plus actual capture time; never backdate a receipt.

    The provider's original fetched_at remains inside the retained Parquet.
    An older raw file preserved today is explicitly an existing-file capture,
    not a claim that this new receipt was created at the original request time.
    Call under the existing datastore writer lock.
    """
    path = Path(path)
    if not path.is_file():
        return None
    payload = path.read_bytes()
    digest = sha256(payload).hexdigest()
    folder = Path(history_dir) if history_dir is not None else path.parent.parent / 'raw-history'
    folder.mkdir(parents=True, exist_ok=True)
    target = folder / f'{digest}.parquet'
    try:
        with target.open('xb') as stream:
            stream.write(payload)
    except FileExistsError:
        if target.read_bytes() != payload:
            raise RuntimeError('Immutable Schwab price-history object differs from its hash')
    receipt = target.with_suffix('.json')
    evidence = {'version':'schwab-price-history-raw-receipt-v1','sha256':digest,
                'bytes':len(payload),'raw_file':target.name,'captured_at':datetime.now(timezone.utc).isoformat(),
                'capture_basis':'exact-existing-raw-file-at-capture','source_path':str(path.resolve())}
    try:
        with receipt.open('x', encoding='utf-8') as stream:
            json.dump(evidence,stream,sort_keys=True)
            stream.write('\n')
    except FileExistsError:
        existing = json.loads(receipt.read_text(encoding='utf-8'))
        if existing.get('sha256') != digest or existing.get('bytes') != len(payload):
            raise RuntimeError('Immutable Schwab price-history receipt differs from its object')
    return target
