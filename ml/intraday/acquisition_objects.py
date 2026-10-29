"""Canonical immutable acquisition payloads with lossless receipt references.

Attempt clocks and identity remain distinct. Equal native observations can share
one payload object across attempts, journals and dates. Existing inline journal
rows are read unchanged; this module never migrates or removes old evidence.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import tempfile


REFERENCE_VERSION = "intraday-acquisition-response-ref-v1"
OBJECT_VERSION = "intraday-acquisition-response-object-v1"
MAX_OBJECT_BYTES = 64 * 1024 * 1024
_PAYLOAD_FIELDS = frozenset(("rows", "native_response"))


def _encoded(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")


def object_root(journal_path):
    """Share the intraday source store between the old and System 1A journals."""
    parent = Path(journal_path).resolve().parent
    if parent.name == "system-1a" and parent.parent.name == "intraday":
        parent = parent.parent
    return parent / "source-objects" / "responses"


def market_response_root(market_input_root):
    # The store already anchors this root. Resolving it again while another
    # Windows process creates its missing directory can transiently introduce
    # an extended drive prefix; preserve the original binding here.
    return Path(market_input_root).absolute().parent / "source-objects" / "market-responses"


def resolve_market_payload(market_input_root, relative, digest):
    """Accept legacy dated bodies and the single new shared hash location only."""
    root = Path(market_input_root).resolve()
    if not isinstance(relative, str) or not re.fullmatch(r"[a-f0-9]{64}", str(digest)):
        raise ValueError("Invalid market payload identity")
    path = (root / relative).resolve()
    legacy = path.is_relative_to(root / "raw")
    shared = path == market_response_root(root) / (digest + ".json")
    if not (legacy or shared) or path.name != digest + ".json":
        raise ValueError("Market payload path escapes its immutable store")
    return path


class ResponseObjects:
    def __init__(self, root):
        self.root = Path(root).resolve()

    def _path(self, kind, identity):
        if kind not in {"payloads", "attempts"} or not re.fullmatch(r"[a-f0-9]{64}", str(identity)):
            raise ValueError("Invalid acquisition object identity")
        path = self.root / kind / (identity + ".json")
        if not path.resolve().is_relative_to(self.root):
            raise ValueError("Acquisition object escapes its store")
        return path

    def _read(self, kind, identity):
        path = self._path(kind, identity)
        if not 0 < path.stat().st_size <= MAX_OBJECT_BYTES:
            raise ValueError("Acquisition object exceeds its byte bound")
        raw = path.read_bytes()
        if len(raw) > MAX_OBJECT_BYTES or hashlib.sha256(raw).hexdigest() != identity:
            raise ValueError("Acquisition object checksum mismatch")
        return raw

    def _put(self, kind, raw):
        if not 0 < len(raw) <= MAX_OBJECT_BYTES:
            raise ValueError("Acquisition object exceeds its byte bound")
        identity = hashlib.sha256(raw).hexdigest()
        path = self._path(kind, identity)
        if path.exists():
            if self._read(kind, identity) != raw:
                raise ValueError("Immutable acquisition object differs")
            return identity
        path.parent.mkdir(parents=True, exist_ok=True)
        descriptor, name = tempfile.mkstemp(prefix=".pending-", dir=path.parent)
        temporary = Path(name)
        try:
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(raw)
                handle.flush()
                os.fsync(handle.fileno())
            try:
                os.link(temporary, path)
            except FileExistsError:
                if self._read(kind, identity) != raw:
                    raise ValueError("Immutable acquisition object differs")
        finally:
            temporary.unlink(missing_ok=True)
        return identity

    def preserve(self, raw):
        """Return a compact reference that reconstructs the exact original JSON."""
        if isinstance(raw, str):
            raw = raw.encode("utf-8")
        if not isinstance(raw, bytes) or not 0 < len(raw) <= MAX_OBJECT_BYTES:
            raise ValueError("Bounded original acquisition bytes required")
        record = json.loads(raw)
        if not isinstance(record, dict) or _encoded(record) != raw:
            raise ValueError("Canonical acquisition object required")
        payload = {key: record[key] for key in _PAYLOAD_FIELDS if key in record}
        metadata = {key: value for key, value in record.items() if key not in _PAYLOAD_FIELDS}
        payload_id = self._put("payloads", _encoded(payload))
        identity = self._put("attempts", _encoded({"schema_version": OBJECT_VERSION,
            "metadata": metadata, "payload_sha256": payload_id}))
        return {"schema_version": REFERENCE_VERSION, "object_sha256": identity,
                "response_sha256": hashlib.sha256(raw).hexdigest(), "response_bytes": len(raw)}

    def restore(self, reference):
        if (not isinstance(reference, dict)
                or set(reference) != {"schema_version", "object_sha256", "response_sha256", "response_bytes"}
                or reference["schema_version"] != REFERENCE_VERSION
                or not re.fullmatch(r"[a-f0-9]{64}", str(reference["response_sha256"]))
                or type(reference["response_bytes"]) is not int
                or not 0 < reference["response_bytes"] <= MAX_OBJECT_BYTES):
            raise ValueError("Invalid acquisition response reference")
        obj = json.loads(self._read("attempts", reference["object_sha256"]))
        if (not isinstance(obj, dict) or set(obj) != {"schema_version", "metadata", "payload_sha256"}
                or obj["schema_version"] != OBJECT_VERSION or not isinstance(obj["metadata"], dict)
                or _PAYLOAD_FIELDS.intersection(obj["metadata"])):
            raise ValueError("Invalid acquisition response object")
        payload = json.loads(self._read("payloads", obj["payload_sha256"]))
        if not isinstance(payload, dict) or not set(payload).issubset(_PAYLOAD_FIELDS):
            raise ValueError("Invalid acquisition observation payload")
        raw = _encoded({**obj["metadata"], **payload})
        if (len(raw) != reference["response_bytes"]
                or hashlib.sha256(raw).hexdigest() != reference["response_sha256"]):
            raise ValueError("Original acquisition response checksum mismatch")
        return raw


def journal_payload_bytes(journal_path, payload):
    """Resolve new references while returning legacy original bytes verbatim."""
    raw = payload.encode("utf-8") if isinstance(payload, str) else payload
    value = json.loads(raw)
    if isinstance(value, dict) and value.get("schema_version") == REFERENCE_VERSION:
        return ResponseObjects(object_root(journal_path)).restore(value)
    return raw


def read_journal_record(journal_path, payload):
    return json.loads(journal_payload_bytes(journal_path, payload))
