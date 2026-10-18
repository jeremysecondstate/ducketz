"""Disposable exact-input cache for already-derived causal context vectors.

The durable provider receipts remain the source evidence. Plain mappings and
lazy cache misses verify full source content. Lazy hits verify the independent
derived artifact bound to original receipt hashes and the receipt file stat;
they do not reread source bytes. This cache never discovers inputs or substitutes
receipt clocks. One transaction per dataset avoids syncing every reference.
"""
from __future__ import annotations

from contextlib import AbstractContextManager
import hashlib
import json
import math
from pathlib import Path
import sqlite3
import zlib

from .market_context import (
    MarketContext, _hash, _json, _selectors, _build_resolved_context, instant,
    _resolved_contract, snapshot_digest,
)
from .prediction import ContractError

# Do not reuse vectors produced before explicit observation-marker/positive-
# volume admission. Original immutable provider receipts are retained.
VERSION = "intraday-exact-causal-context-cache-v6"
MAX_COMPRESSED_BYTES = 4 * 1024 * 1024
MAX_DECODED_BYTES = 16 * 1024 * 1024


def _implementation_digest():
    """Bind disposable vectors to exact derivation code, not a manual bump."""
    from . import market_context, market_state
    paths = (Path(__file__), Path(market_context.__file__), Path(market_state.__file__))
    return _hash({path.name: hashlib.sha256(path.read_bytes()).hexdigest() for path in paths})


class ContextCache(AbstractContextManager):
    """Bounded derived cache, safe to delete without losing observations."""

    def __init__(self, path):
        self.path = Path(path)
        self.connection = None
        self.hits = self.misses = self.invalid_entries = 0
        self._contracts = {}
        self._lazy_refs = {}
        self.implementation_sha256 = _implementation_digest()

    def _contract(self, source_ids, contract, selectors):
        # Keep the caller alive, so an id cannot be reused during this dataset.
        # Equality against the validated independent copy detects nested edits
        # without rebuilding and deep-copying thousands of feature names per row.
        selector_key = tuple(sorted(selectors.items()))
        key = (id(contract), selector_key)
        cached = self._contracts.get(key)
        if cached is not None:
            original, resolved, digest = cached
            if contract is not original or contract is not None and contract != resolved:
                raise ContractError("Market context contract changed during cache transaction")
            return resolved, digest
        resolved = _resolved_contract(source_ids, contract)
        digest = _hash(resolved)
        if len(self._contracts) >= 32:
            raise ContractError("Too many context contracts in one dataset transaction")
        self._contracts[key] = (contract, resolved, digest)
        return resolved, digest

    def __enter__(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(self.path, timeout=30)
        self.connection.execute("PRAGMA journal_mode=WAL")
        self.connection.execute("CREATE TABLE IF NOT EXISTS contexts "
            "(cache_key TEXT PRIMARY KEY, payload BLOB NOT NULL, payload_sha256 TEXT NOT NULL)")
        self.connection.commit()
        self.connection.execute("BEGIN")
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        try:
            if exc_type is None:
                if self.implementation_sha256 != _implementation_digest():
                    raise ContractError("Market context implementation changed during cache transaction")
                # One check before first use and again before the dataset is
                # released. Rechecking an unchanged receipt for every adjacent
                # historical decision adds no evidence and scales poorly.
                for snapshot in self._lazy_refs.values():
                    snapshot.verify_unchanged()
                self.connection.commit()
            else:
                self.connection.rollback()
        except BaseException:
            self.connection.rollback()
            raise
        finally:
            self.connection.close()
            self.connection = None

    def build(self, snapshots, *, symbol, cutoff, source_ids=None, contract=None):
        if self.connection is None:
            raise RuntimeError("ContextCache requires a dataset transaction")
        cutoff = instant(cutoff)
        selectors = _selectors(source_ids)
        selected = []
        # Preserve build_market_context's future-byte independence and scope.
        for snapshot in snapshots:
            if snapshot.get("symbol") not in {symbol, "*"}:
                continue
            kind = snapshot.get("source_kind")
            if kind in selectors and snapshot.get("source_id") not in selectors[kind]:
                continue
            if instant(snapshot["receipt_at"]) > cutoff or instant(snapshot["available_at"]) > cutoff:
                continue
            if callable(getattr(snapshot, "load_verified", None)):
                identity = id(snapshot)
                if identity not in self._lazy_refs:
                    snapshot.verify_unchanged()
                    self._lazy_refs[identity] = snapshot
            elif snapshot.get("snapshot_sha256") != snapshot_digest(snapshot):
                raise ContractError("Market snapshot content hash mismatch")
            selected.append(snapshot)
        contract, contract_sha256 = self._contract(source_ids, contract, selectors)
        key = _hash({"cache_version": VERSION, "implementation_sha256": self.implementation_sha256,
                     "contract_sha256": contract_sha256, "symbol": symbol,
                     "cutoff": cutoff.isoformat(), "receipts": sorted(
                         (s["source_id"], s["snapshot_id"], s["snapshot_sha256"]) for s in selected)})
        row = self.connection.execute("SELECT payload,payload_sha256 FROM contexts WHERE cache_key=?", (key,)).fetchone()
        if row is not None:
            try:
                payload, digest = row
                if len(payload) > MAX_COMPRESSED_BYTES or hashlib.sha256(payload).hexdigest() != digest:
                    raise ValueError("Invalid cached payload")
                decoder = zlib.decompressobj()
                decoded = decoder.decompress(payload, MAX_DECODED_BYTES + 1)
                if len(decoded) > MAX_DECODED_BYTES or decoder.unconsumed_tail or not decoder.eof:
                    raise ValueError("Cached payload exceeds bounded decoding")
                envelope = json.loads(decoded)
                if not isinstance(envelope, dict) or envelope.get("cache_key") != key:
                    raise ValueError("Cached context belongs to different receipt inputs")
                value = envelope["context"]
                result = MarketContext(value["symbol"], value["cutoff"], tuple(value["feature_names"]),
                    tuple(value["values"]), tuple(value["source_evidence"]), value["snapshot_sha256"],
                    tuple((k, tuple(v)) for k, v in value["source_ids"]), value["feature_version"])
                if (result.symbol != symbol or result.cutoff != cutoff.isoformat()
                        or list(result.feature_names) != contract["feature_names"]
                        or result.source_ids != tuple(sorted(selectors.items()))
                        or len(result.values) != len(result.feature_names)
                        or not all(math.isfinite(v) for v in result.values)
                        or result.feature_version != contract["version"]
                        or result.snapshot_sha256 != _hash(result.snapshot)):
                    raise ValueError("Cached context contract mismatch")
                self.hits += 1
                return result
            except (ValueError, TypeError, KeyError, zlib.error):
                self.invalid_entries += 1
        materialized = []
        for snapshot in selected:
            if callable(getattr(snapshot, "load_verified", None)):
                payload = snapshot.load_verified()
                fields = ("source_kind", "source_id", "snapshot_id", "snapshot_sha256", "symbol", "receipt_at", "available_at", "status")
                if any(payload.get(name) != snapshot.get(name) for name in fields):
                    raise ContractError("Lazy market snapshot differs from its indexed identity")
                materialized.append(payload)
            else:
                materialized.append(snapshot)
        result = _build_resolved_context(materialized, symbol=symbol, cutoff=cutoff, source_ids=source_ids, contract=contract)
        # JSON immediately serializes these owned immutable vectors. Recursive
        # dataclass conversion copies thousands of scalar coordinates per miss
        # without changing their bytes or providing an additional validation.
        value = {name:getattr(result,name) for name in result.__dataclass_fields__}
        payload = zlib.compress(_json({"cache_key": key, "context": value}).encode("utf-8"))
        if len(payload) <= MAX_COMPRESSED_BYTES:
            self.connection.execute("INSERT OR REPLACE INTO contexts VALUES (?,?,?)",
                (key, payload, hashlib.sha256(payload).hexdigest()))
        self.misses += 1
        return result
