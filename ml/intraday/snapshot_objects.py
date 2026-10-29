"""Exact immutable snapshot bytes shared by explicitly configured local stores.

Objects retain their original SHA-256 identity and canonical JSON bytes. Old
raw objects are never rewritten. A prospective, explicitly enabled writer can
share original canonical field/row bytes through bounded immutable chunks;
every reader reconstructs and verifies the complete original bytes. This module
knows no database, model, provider, execution state or mutable pointer.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import uuid

from .prediction import ContractError, encoded

MAX_SNAPSHOT_BYTES = 1024 * 1024 * 1024
MANIFEST_VERSION = "intraday-canonical-chunks-v1"
CHUNK_ROWS = 512
CHUNK_THRESHOLD = 64 * 1024
MAX_CHUNK_BYTES = 4 * 1024 * 1024
MAX_MANIFEST_BYTES = 4 * 1024 * 1024
MAX_CHUNKS = 32768
_HASH = re.compile(r"[a-f0-9]{64}\Z")


class SnapshotObjects:
    def __init__(self, directory, *, max_bytes=MAX_SNAPSHOT_BYTES, chunked=False):
        if type(max_bytes) is not int or not 0 < max_bytes <= MAX_SNAPSHOT_BYTES:
            raise ContractError("Invalid immutable snapshot resource bound")
        if type(chunked) is not bool:
            raise ContractError("Invalid immutable snapshot writer selection")
        self.root = Path(directory).resolve()
        self.max_bytes = max_bytes
        self.chunked = chunked

    def _path(self, identity):
        if not isinstance(identity, str) or not _HASH.fullmatch(identity):
            raise ContractError("Invalid immutable snapshot identity")
        return self.root / identity[:2] / (identity + ".json")

    def _manifest_path(self, identity):
        self._path(identity)
        return self.root / "manifests" / identity[:2] / (identity + ".json")

    def _chunk_path(self, identity):
        self._path(identity)
        return self.root / "chunks" / identity[:2] / (identity + ".bin")

    @staticmethod
    def _bounded_read(path, bound):
        with path.open("rb") as stream:
            raw = stream.read(bound + 1)
        if len(raw) > bound:
            raise ContractError("Incomplete or changed immutable snapshot object: resource bound exceeded")
        return raw

    def read(self, identity, expected_bytes=None):
        path = self._path(identity)
        if expected_bytes is not None and (
                type(expected_bytes) is not int or not 0 <= expected_bytes <= self.max_bytes):
            raise ContractError("Invalid immutable snapshot byte count")
        if path.exists():
            # A corrupt retained raw object must never be hidden by a sidecar.
            raw = self._bounded_read(path, self.max_bytes)
        else:
            try:
                manifest = json.loads(self._bounded_read(self._manifest_path(identity), MAX_MANIFEST_BYTES))
            except (json.JSONDecodeError, UnicodeDecodeError) as exc:
                raise ContractError("Invalid immutable snapshot chunk manifest") from exc
            if (not isinstance(manifest, dict)
                    or set(manifest) != {"schema", "sha256", "bytes", "chunks"}
                    or manifest["schema"] != MANIFEST_VERSION or manifest["sha256"] != identity
                    or type(manifest["bytes"]) is not int
                    or not 0 <= manifest["bytes"] <= self.max_bytes
                    or expected_bytes is not None and manifest["bytes"] != expected_bytes
                    or not isinstance(manifest["chunks"], list)
                    or not 0 < len(manifest["chunks"]) <= MAX_CHUNKS):
                raise ContractError("Invalid immutable snapshot chunk manifest")
            total = 0
            for item in manifest["chunks"]:
                if (not isinstance(item, dict) or set(item) != {"sha256", "bytes"}
                        or type(item["bytes"]) is not int or not 0 < item["bytes"] <= MAX_CHUNK_BYTES):
                    raise ContractError("Invalid immutable snapshot chunk descriptor")
                self._chunk_path(item["sha256"])
                total += item["bytes"]
                if total > manifest["bytes"]:
                    raise ContractError("Invalid immutable snapshot chunk total")
            if total != manifest["bytes"]:
                raise ContractError("Invalid immutable snapshot chunk total")
            reconstructed = bytearray()
            for item in manifest["chunks"]:
                chunk = self._bounded_read(self._chunk_path(item["sha256"]), item["bytes"])
                if len(chunk) != item["bytes"] or hashlib.sha256(chunk).hexdigest() != item["sha256"]:
                    raise ContractError("Incomplete or changed immutable snapshot chunk")
                reconstructed.extend(chunk)
            raw = bytes(reconstructed)
        if (len(raw) > self.max_bytes
                or expected_bytes is not None and len(raw) != expected_bytes
                or hashlib.sha256(raw).hexdigest() != identity):
            raise ContractError("Incomplete or changed immutable snapshot object")
        return raw

    @staticmethod
    def _pieces(payload):
        """Encode original canonical syntax; never parse or reinterpret raw bytes."""
        if not isinstance(payload, dict) or any(not isinstance(key, str) for key in payload):
            return
        yield b"{"
        for index, key in enumerate(sorted(payload)):
            yield ("," if index else "").encode() + encoded(key).encode() + b":"
            value = payload[key]
            if isinstance(value, list):
                yield b"["
                for start in range(0, len(value), CHUNK_ROWS):
                    yield (b"," if start else b"") + encoded(value[start:start + CHUNK_ROWS]).encode()[1:-1]
                yield b"]"
            else:
                yield encoded(value).encode()
        yield b"}"

    def _plan(self, raw, payload):
        # The supplied in-memory payload is merely an optimization hint. Verify
        # every original byte before any publication; mismatches use raw storage.
        pieces, offset = [], 0
        try:
            for piece in self._pieces(payload):
                for start in range(0, len(piece), MAX_CHUNK_BYTES):
                    chunk = piece[start:start + MAX_CHUNK_BYTES]
                    if raw[offset:offset + len(chunk)] != chunk or len(pieces) >= MAX_CHUNKS:
                        return None
                    pieces.append(chunk)
                    offset += len(chunk)
        except (ValueError, TypeError, OverflowError):
            return None
        return pieces if pieces and offset == len(raw) else None

    @staticmethod
    def _publish(path, raw, bound):
        if len(raw) > bound:
            raise ContractError("Immutable snapshot exceeds its resource bound")
        if path.exists():
            if SnapshotObjects._bounded_read(path, bound) != raw:
                raise ContractError("Incomplete or changed immutable snapshot object collision")
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name("." + uuid.uuid4().hex + ".partial")
        try:
            with temporary.open("xb") as stream:
                stream.write(raw)
                stream.flush()
                os.fsync(stream.fileno())
            try:
                os.link(temporary, path)
            except FileExistsError:
                if SnapshotObjects._bounded_read(path, bound) != raw:
                    raise ContractError("Incomplete or changed immutable snapshot object collision")
        finally:
            temporary.unlink(missing_ok=True)

    def write(self, raw, *, payload=None):
        if not isinstance(raw, bytes):
            raise ContractError("Immutable snapshot requires original bytes")
        if len(raw) > self.max_bytes:
            raise ContractError("Immutable snapshot exceeds its resource bound")
        identity = hashlib.sha256(raw).hexdigest()
        path = self._path(identity)
        if path.exists() or self._manifest_path(identity).exists():
            if self.read(identity, expected_bytes=len(raw)) != raw:
                raise ContractError("Immutable snapshot object collision")
            return identity
        pieces = self._plan(raw, payload) if self.chunked and len(raw) >= CHUNK_THRESHOLD else None
        if pieces is None:
            self._publish(path, raw, self.max_bytes)
        else:
            descriptors = [{"sha256": hashlib.sha256(piece).hexdigest(), "bytes": len(piece)} for piece in pieces]
            manifest = encoded({"schema": MANIFEST_VERSION, "sha256": identity,
                                "bytes": len(raw), "chunks": descriptors}).encode()
            if len(manifest) > MAX_MANIFEST_BYTES:
                self._publish(path, raw, self.max_bytes)
                return identity
            for piece, descriptor in zip(pieces, descriptors):
                self._publish(self._chunk_path(descriptor["sha256"]), piece, MAX_CHUNK_BYTES)
            # Only a complete, flushed chunk set can acquire this immutable
            # manifest name. Interrupted work remains unadvertised and reusable.
            try:
                self._publish(self._manifest_path(identity), manifest, MAX_MANIFEST_BYTES)
            except ContractError:
                # Hints can encode the same original bytes with different
                # segment boundaries (for example tuple versus list hints).
                # The first complete manifest wins; its decoded original bytes
                # are the identity, rather than this optimization's boundaries.
                if self.read(identity, expected_bytes=len(raw)) != raw:
                    raise ContractError("Immutable snapshot object collision")
        return identity
