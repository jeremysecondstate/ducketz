"""Disposable exact-input cache for already-derived causal context vectors.

The durable provider receipts remain the source evidence. Plain mappings and
lazy cache misses verify full source content. Lazy hits verify the independent
derived artifact bound to original receipt hashes and the receipt file stat;
they do not reread source bytes. This cache never discovers inputs or substitutes
receipt clocks. Reads do not hold a SQLite snapshot across dataset construction.
Bounded new vectors are published together only after final source validation;
cache contention can lose that optional optimization, never the derived result.
"""
from __future__ import annotations

from contextlib import AbstractContextManager
from collections import OrderedDict
from dataclasses import dataclass
import hashlib
import json
import math
from pathlib import Path
import sqlite3
import threading
import zlib

import numpy as np

from .market_context import (
    MarketContext, _hash, _json, _selectors, _build_resolved_context, instant,
    _resolved_contract, _owned_context_digest, snapshot_digest,
)
from .prediction import ContractError
from . import market_context as _context_module

# Do not reuse vectors produced before explicit observation-marker/positive-
# volume admission. Original immutable provider receipts are retained.
VERSION = "intraday-exact-causal-context-cache-v6"
MAX_COMPRESSED_BYTES = 4 * 1024 * 1024
MAX_DECODED_BYTES = 16 * 1024 * 1024
MAX_PENDING_BYTES = 16 * 1024 * 1024
MAX_PENDING_ENTRIES = 4096
# Ordered feature names are already frozen by the fully validated contract
# in each cache key. Repeating them in every vector dominated the staged-byte
# budget; restore them only from that exact contract and rehash the full context.
CONTEXT_FORMAT = "fixed-feature-contract-v1"

# This is a disposable process-local working set for the independent data
# service, not another source of market evidence. Store immutable packed bytes,
# not caller-owned arrays or MarketContext's nested mutable evidence mappings.
# Sparse historical context compresses particularly well. Both bytes and entry
# count are bounded; eviction only returns work to the durable verified cache.
MAX_REUSE_BYTES = 128 * 1024 * 1024
MAX_REUSE_ENTRIES = 65536


@dataclass(frozen=True, slots=True)
class ContextRow:
    values: np.ndarray
    snapshot_sha256: str
    has_source: bool


@dataclass(frozen=True, slots=True)
class _VerifiedRow:
    packed: bytes
    size: int
    snapshot_sha256: str
    has_source: bool

    def weight(self, key):
        # Include conservative per-entry Python/key/index overhead instead of
        # bounding compressed payload bytes alone.
        return len(self.packed) + 4 * (len(key[0]) + len(key[1])) + 1024

    @classmethod
    def from_context(cls, context):
        values = np.asarray(context.values, dtype="<f8")
        return cls(zlib.compress(values.tobytes()), values.nbytes,
                   context.snapshot_sha256,
                   any(e.get("records_used") for e in context.source_evidence))

    def row(self):
        # The bounded object originated in this process after the original
        # cache/derivation checks. No disk bytes or caller references enter it.
        # A bytes-backed array cannot be made writable by a consumer.
        raw = zlib.decompress(self.packed)
        if len(raw) != self.size:
            raise ContractError("Verified context working set changed")
        return ContextRow(np.frombuffer(raw, dtype="<f8"), self.snapshot_sha256,
                          self.has_source)


class _VerifiedRows:
    def __init__(self):
        self.entries = OrderedDict()
        self.bytes = 0
        self.lock = threading.Lock()

    def get(self, key):
        with self.lock:
            value = self.entries.get(key)
            if value is not None:
                self.entries.move_to_end(key)
            return value

    def publish(self, entries):
        with self.lock:
            for key, value in entries.items():
                weight = value.weight(key)
                if weight > MAX_REUSE_BYTES or MAX_REUSE_ENTRIES < 1:
                    continue
                previous = self.entries.pop(key, None)
                if previous is not None:
                    self.bytes -= previous.weight(key)
                self.entries[key] = value
                self.bytes += weight
                while self.bytes > MAX_REUSE_BYTES or len(self.entries) > MAX_REUSE_ENTRIES:
                    removed_key, removed = self.entries.popitem(last=False)
                    self.bytes -= removed.weight(removed_key)


_VERIFIED_ROWS = _VerifiedRows()


# The optimization applies only to this native canonical serializer. Overrides
# retain the original helper, including its exceptions and interpretation.
_NATIVE_CONTEXT_CLASS = MarketContext
_NATIVE_BUILD_CONTEXT = _build_resolved_context
_NATIVE_OWNED_DIGEST = _owned_context_digest
_NATIVE_CONTEXT_SNAPSHOT = _context_module._context_snapshot
_NATIVE_HASH, _NATIVE_JSON = _hash, _json
_NATIVE_DUMPS, _NATIVE_LOADS = json.dumps, json.loads
_NATIVE_ENCODER = json.JSONEncoder
_NATIVE_ENCODER_PARTS = (json.JSONEncoder.__init__, json.JSONEncoder.encode, json.JSONEncoder.iterencode,
    json.JSONEncoder.default, json.encoder.encode_basestring,
    json.encoder.encode_basestring_ascii, json.encoder.c_make_encoder,
    json.encoder._make_iterencode)


def _native_context_serializer():
    return (type(_context_module.FEATURE_VERSION) is str
            and type(_context_module.STATE_POLICY) is str
            and type(_context_module.FEATURE_POLICY) is str
            and MarketContext is _NATIVE_CONTEXT_CLASS
            and _context_module.MarketContext is _NATIVE_CONTEXT_CLASS
            and _owned_context_digest is _NATIVE_OWNED_DIGEST
            and _context_module._context_snapshot is _NATIVE_CONTEXT_SNAPSHOT
            and _hash is _NATIVE_HASH and _context_module._hash is _NATIVE_HASH
            and _context_module._json is _NATIVE_JSON and _json is _NATIVE_JSON
            and json.dumps is _NATIVE_DUMPS and json.loads is _NATIVE_LOADS
            and json.JSONEncoder is _NATIVE_ENCODER
            and all(current is original for current, original in zip(
                (json.JSONEncoder.__init__, json.JSONEncoder.encode, json.JSONEncoder.iterencode,
                 json.JSONEncoder.default, json.encoder.encode_basestring,
                 json.encoder.encode_basestring_ascii, json.encoder.c_make_encoder,
                 json.encoder._make_iterencode), _NATIVE_ENCODER_PARTS)))


@dataclass(frozen=True)
class _ContextDigestFragments:
    """Immutable JSON for the already-validated fixed fields of one contract.

    A decoded hit must still pass every cache validation and be freshly hashed.
    Reusing these strings avoids encoding thousands of identical feature names
    for every historical row. Values, source evidence and clocks are encoded on
    every call; no receipt, vector or final digest is cached by this helper.
    """
    names: tuple
    source_ids: tuple
    version: str
    policy: str
    middle: str
    source_ids_json: str

    @classmethod
    def from_contract(cls, contract, selectors):
        if (not _native_context_serializer() or type(contract) is not dict
                or type(selectors) is not dict):
            return None
        # Resolve only ordinary validated containers. Custom mappings, lists
        # and text retain the original path without any new callback.
        names = contract["feature_names"]
        version = contract["version"]
        if (type(names) is not list or type(version) is not str
                or any(type(name) is not str for name in names)
                or any(type(kind) is not str or type(ids) is not tuple
                       or any(type(source) is not str for source in ids)
                       for kind, ids in selectors.items())):
            return None
        names = tuple(names)
        source_ids = tuple(sorted(selectors.items()))
        policy = (_context_module.STATE_POLICY if version == _context_module.FEATURE_VERSION
                  else _context_module.FEATURE_POLICY)
        if type(policy) is not str:
            return None
        middle = (',"feature_names":' + _json(list(names))
                  + ',"feature_policy":' + _json(policy)
                  + ',"feature_version":' + _json(version) + ',"features":')
        return cls(names, source_ids, version, policy, middle,
                   _json({kind: list(ids) for kind, ids in source_ids}))

    def digest(self, context):
        if (not _native_context_serializer() or type(context) is not MarketContext
                or type(context.feature_version) is not str
                or type(context.values) is not tuple or type(context.source_evidence) is not tuple):
            return _owned_context_digest(context)
        policy = (_context_module.STATE_POLICY if context.feature_version == _context_module.FEATURE_VERSION
                  else _context_module.FEATURE_POLICY)
        if (type(context.cutoff) is not str or type(context.symbol) is not str
                or type(context.feature_names) is not tuple
                or any(type(name) is not str for name in context.feature_names)
                or context.feature_names != self.names
                or type(context.source_ids) is not tuple
                or any(type(pair) is not tuple or len(pair) != 2
                       or type(pair[0]) is not str or type(pair[1]) is not tuple
                       or any(type(source) is not str for source in pair[1])
                       for pair in context.source_ids)
                or context.source_ids != self.source_ids
                or context.feature_version != self.version
                or type(policy) is not str or policy != self.policy):
            return _owned_context_digest(context)
        # These are precisely _context_snapshot's sorted JSON keys. Static
        # names/selectors/version have just matched the validated contract;
        # mutable decoded values and nested evidence are always serialized now.
        canonical = ('{"cutoff":' + _json(context.cutoff) + self.middle
                     + _json(list(context.values))
                     + ',"missing_encoding":"zero_placeholder_plus_per_value_missing_indicator"'
                     + ',"source_ids":' + self.source_ids_json
                     + ',"sources":' + _json(list(context.source_evidence))
                     + ',"symbol":' + _json(context.symbol) + '}')
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _busy(error):
    code = getattr(error, "sqlite_errorcode", None)
    return isinstance(code, int) and (code & 255) in {sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED}


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
        self.write_skips = self.read_skips = 0
        self._active = False
        self._pending = {}
        self._pending_bytes = 0
        self._contracts = {}
        self._digest_fragments = {}
        self._lazy_refs = {}
        self._plain_refs = {}
        self._verified_pending = {}
        self._verified_pending_bytes = 0
        self.row_reuse_hits = 0
        self._reuse_namespace = str(self.path.resolve())
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
        self._digest_fragments[digest] = _ContextDigestFragments.from_contract(resolved, selectors)
        return resolved, digest

    def __enter__(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(self.path, timeout=0, isolation_level=None)
        try:
            self.connection.execute("PRAGMA journal_mode=WAL").close()
            self.connection.execute("CREATE TABLE IF NOT EXISTS contexts "
                "(cache_key TEXT PRIMARY KEY, payload BLOB NOT NULL, payload_sha256 TEXT NOT NULL)").close()
        except BaseException as exc:
            self.connection.close()
            self.connection = None
            if not isinstance(exc, sqlite3.OperationalError) or not _busy(exc):
                raise
        self._active = True
        return self

    def _flush(self):
        if not self._pending:
            return
        if self.connection is None:
            self.write_skips += len(self._pending)
            return
        try:
            # No model/dataset work is performed while owning the writer slot.
            # An older preparation process may still hold its long transaction;
            # skip this disposable batch immediately instead of delaying it.
            self.connection.execute("BEGIN IMMEDIATE").close()
            self.connection.executemany("INSERT OR REPLACE INTO contexts VALUES (?,?,?)",
                ((key, *value) for key, value in self._pending.items())).close()
            self.connection.commit()
        except sqlite3.OperationalError as exc:
            self.connection.rollback()
            if not _busy(exc):
                raise
            self.write_skips += len(self._pending)

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
                for snapshot, expected in self._plain_refs.values():
                    if snapshot.get("snapshot_sha256") != expected or snapshot_digest(snapshot) != expected:
                        raise ContractError("Immutable receipt changed during context preparation")
                self._flush()
                _VERIFIED_ROWS.publish(self._verified_pending)
        finally:
            if self.connection is not None:
                self.connection.close()
            self.connection = None
            self._active = False
            self._pending.clear()
            self._pending_bytes = 0
            self._verified_pending.clear()
            self._verified_pending_bytes = 0

    def _inputs(self, snapshots, *, symbol, cutoff, source_ids=None, contract=None):
        if not self._active:
            raise RuntimeError("ContextCache requires a dataset context")
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
            else:
                previous = self._plain_refs.get(id(snapshot))
                if previous is not None and previous[1] != snapshot["snapshot_sha256"]:
                    raise ContractError("Immutable receipt changed during context preparation")
                self._plain_refs[id(snapshot)] = (snapshot, snapshot["snapshot_sha256"])
            selected.append(snapshot)
        contract, contract_sha256 = self._contract(source_ids, contract, selectors)
        key = _hash({"cache_version": VERSION, "implementation_sha256": self.implementation_sha256,
                     "contract_sha256": contract_sha256, "symbol": symbol,
                     "cutoff": cutoff.isoformat(), "receipts": sorted(
                         (s["source_id"], s["snapshot_id"], s["snapshot_sha256"]) for s in selected)})
        return selected, contract, contract_sha256, selectors, cutoff, key

    def build(self, snapshots, *, symbol, cutoff, source_ids=None, contract=None):
        inputs = self._inputs(snapshots, symbol=symbol, cutoff=cutoff,
                              source_ids=source_ids, contract=contract)
        return self._build(inputs, symbol=symbol, source_ids=source_ids)

    def build_row(self, snapshots, *, symbol, cutoff, source_ids=None, contract=None):
        """Reuse an exact verified historical vector without decoding its JSON.

        This deliberately does not replace public ``build``: inference still
        receives the complete checked context and durable cache corruption is
        checked by that original path. Historical reuse is an independent
        in-memory derivation witness, released only after source checks at a
        successful dataset boundary. A recipe's context schema and source
        selectors are already part of the key; recipe-specific labels, row
        selection and support gates remain outside this reusable projection.
        """
        inputs = self._inputs(snapshots, symbol=symbol, cutoff=cutoff,
                              source_ids=source_ids, contract=contract)
        native = (_native_context_serializer()
                  and _build_resolved_context is _NATIVE_BUILD_CONTEXT
                  and _context_module._build_resolved_context is _NATIVE_BUILD_CONTEXT)
        key = (self._reuse_namespace, inputs[-1])
        saved = (self._verified_pending.get(key) or _VERIFIED_ROWS.get(key)) if native else None
        if saved is not None:
            self.hits += 1
            self.row_reuse_hits += 1
            return saved.row()
        context = self._build(inputs, symbol=symbol, source_ids=source_ids)
        value = _VerifiedRow.from_context(context)
        weight = value.weight(key)
        if (native and len(self._verified_pending) < MAX_REUSE_ENTRIES
                and self._verified_pending_bytes + weight <= MAX_REUSE_BYTES):
            self._verified_pending[key] = value
            self._verified_pending_bytes += weight
        return value.row()

    def _build(self, inputs, *, symbol, source_ids):
        selected, contract, contract_sha256, selectors, cutoff, key = inputs
        row = self._pending.get(key)
        if row is None and self.connection is not None:
            try:
                cursor = self.connection.execute("SELECT payload,payload_sha256 FROM contexts WHERE cache_key=?", (key,))
                try:
                    row = cursor.fetchone()
                finally:
                    cursor.close()
            except sqlite3.OperationalError as exc:
                if not _busy(exc):
                    raise
                self.read_skips += 1
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
                if "context_format" not in envelope:
                    # Retain exact order validation for older named envelopes.
                    feature_names = value["feature_names"]
                elif envelope["context_format"] == CONTEXT_FORMAT and "feature_names" not in value:
                    feature_names = contract["feature_names"]
                else:
                    raise ValueError("Unsupported cached context format")
                result = MarketContext(value["symbol"], value["cutoff"], tuple(feature_names),
                    tuple(value["values"]), tuple(value["source_evidence"]), value["snapshot_sha256"],
                    tuple((k, tuple(v)) for k, v in value["source_ids"]), value["feature_version"])
                if (result.symbol != symbol or result.cutoff != cutoff.isoformat()
                        or list(result.feature_names) != contract["feature_names"]
                        or result.source_ids != tuple(sorted(selectors.items()))
                        or len(result.values) != len(result.feature_names)
                        or not all(math.isfinite(v) for v in result.values)
                        or result.feature_version != contract["version"]
                        # json.loads above owns every nested evidence object;
                        # validate it immediately before returning any reference.
                        or result.snapshot_sha256 != (
                            self._digest_fragments[contract_sha256].digest(result)
                            if self._digest_fragments[contract_sha256] is not None
                            else _owned_context_digest(result))):
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
        value = {name:getattr(result,name) for name in result.__dataclass_fields__ if name != "feature_names"}
        payload = zlib.compress(_json({"cache_key": key, "context_format": CONTEXT_FORMAT,
                                      "context": value}).encode("utf-8"))
        if len(payload) <= MAX_COMPRESSED_BYTES:
            if (len(self._pending) < MAX_PENDING_ENTRIES
                    and self._pending_bytes + len(payload) <= MAX_PENDING_BYTES):
                self._pending[key] = (payload, hashlib.sha256(payload).hexdigest())
                self._pending_bytes += len(payload)
            else:
                self.write_skips += 1
        self.misses += 1
        return result
