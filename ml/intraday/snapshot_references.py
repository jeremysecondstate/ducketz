"""Decode immutable input evidence without opening or initializing a database."""
from __future__ import annotations

import json
from pathlib import Path
import re

from .prediction import ContractError, digest


def canonical_snapshot_directory(path):
    """Read binding for established intraday layouts; never enables writes."""
    path = Path(path).resolve()
    if path.parent.name == "intraday" and path.name == "shadow.sqlite3":
        root = path.parent
    elif path.parent.parent.name == "intraday" and (
            path.parent.name == "system-1a"
            and re.fullmatch(r"preparation(?:-[A-Z][A-Z0-9.-]{0,15})?\.sqlite3", path.name)
            or path.parent.name == "system-1b"
            and re.fullmatch(r"models(?:-[0-9]+)?\.sqlite3", path.name)):
        root = path.parent.parent
    else:
        return None
    return root / "system-1a/ready/snapshots"


def read_reference_bytes(key, value, *, snapshot_objects):
    """Validate a descriptor and verify its complete original bytes."""
    if (not isinstance(value, dict) or set(value) != {"snapshot_storage", "sha256", "bytes"}
            or value["snapshot_storage"] != "intraday-input-reference-v1"
            or value["sha256"] != key
            or type(value["bytes"]) is not int or value["bytes"] < 0
            or snapshot_objects is None):
        raise ContractError("Persisted snapshot reference is invalid or unbound")
    return snapshot_objects.read(key, expected_bytes=value["bytes"])


def read_input_value(key, raw, *, snapshot_objects=None):
    value = json.loads(raw)
    # Legacy inline snapshots may contain any scientific dictionary keys.
    # Their original semantic identity takes precedence over the codec tag.
    if isinstance(value, dict) and digest(value) == key:
        return value
    if isinstance(value, dict) and "snapshot_storage" in value:
        raw = read_reference_bytes(key, value, snapshot_objects=snapshot_objects)
        value = json.loads(raw)
        if isinstance(value, dict) and digest(value) == key:
            return value
    raise ContractError("Persisted snapshot evidence hash changed")
