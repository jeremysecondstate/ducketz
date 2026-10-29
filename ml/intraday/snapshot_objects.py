"""Exact immutable snapshot bytes shared by explicitly configured local stores.

Objects retain their original SHA-256 identity and canonical JSON bytes. This
module knows no database, model, provider, execution state or mutable pointer.
"""
from __future__ import annotations

import hashlib
import os
from pathlib import Path
import re
import uuid

from .prediction import ContractError

MAX_SNAPSHOT_BYTES = 1024 * 1024 * 1024
_HASH = re.compile(r"[a-f0-9]{64}\Z")


class SnapshotObjects:
    def __init__(self, directory, *, max_bytes=MAX_SNAPSHOT_BYTES):
        if type(max_bytes) is not int or not 0 < max_bytes <= MAX_SNAPSHOT_BYTES:
            raise ContractError("Invalid immutable snapshot resource bound")
        self.root = Path(directory).resolve()
        self.max_bytes = max_bytes

    def _path(self, identity):
        if not isinstance(identity, str) or not _HASH.fullmatch(identity):
            raise ContractError("Invalid immutable snapshot identity")
        return self.root / identity[:2] / (identity + ".json")

    def read(self, identity, expected_bytes=None):
        path = self._path(identity)
        if expected_bytes is not None and (
                type(expected_bytes) is not int or not 0 <= expected_bytes <= self.max_bytes):
            raise ContractError("Invalid immutable snapshot byte count")
        with path.open("rb") as stream:
            raw = stream.read(self.max_bytes + 1)
        if (len(raw) > self.max_bytes
                or expected_bytes is not None and len(raw) != expected_bytes
                or hashlib.sha256(raw).hexdigest() != identity):
            raise ContractError("Incomplete or changed immutable snapshot object")
        return raw

    def write(self, raw):
        if not isinstance(raw, bytes):
            raise ContractError("Immutable snapshot requires original bytes")
        if len(raw) > self.max_bytes:
            raise ContractError("Immutable snapshot exceeds its resource bound")
        identity = hashlib.sha256(raw).hexdigest()
        path = self._path(identity)
        if path.exists():
            if self.read(identity, expected_bytes=len(raw)) != raw:
                raise ContractError("Immutable snapshot object collision")
            return identity
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name("." + uuid.uuid4().hex + ".partial")
        try:
            with temporary.open("xb") as stream:
                stream.write(raw)
                stream.flush()
                os.fsync(stream.fileno())
            # Link complete, flushed bytes without replacing the first writer.
            # A failed/interrupted temporary write never publishes an object.
            try:
                os.link(temporary, path)
            except FileExistsError:
                if self.read(identity, expected_bytes=len(raw)) != raw:
                    raise ContractError("Immutable snapshot object collision")
        finally:
            temporary.unlink(missing_ok=True)
        return identity
