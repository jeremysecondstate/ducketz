"""Private resource choices for a human-started local 15-minute session."""
from __future__ import annotations

import json
from pathlib import Path

from .prediction import ContractError

VERSION = "intraday-manual-resource-policy-v1"
POLICY_PATH = Path("intraday/manual-resource-policy.json")


def _workers(value):
    if type(value) is not int or value < 1:
        raise ContractError("Explicit positive symbol worker count required")
    return value


def manual_symbol_workers(datastore, *, actor, explicit=None):
    """An explicit CLI choice wins; absent local policy preserves one worker.

    This reads only an actor-scoped local file. It neither starts a session nor
    changes model selection, existing frozen cycles, or another PC's defaults.
    """
    if actor not in {"Atlas", "Scout"}:
        raise ContractError("Explicit local 15m actor required")
    if explicit is not None:
        return _workers(explicit)
    path = Path(datastore) / POLICY_PATH
    if not path.is_file():
        return 1
    raw = path.read_bytes()
    if len(raw) > 4096:
        raise ContractError("Local manual resource policy exceeds 4 KiB")
    try:
        policy = json.loads(raw.decode("utf-8-sig"))
    except (ValueError, UnicodeError) as exc:
        raise ContractError("Invalid local manual resource policy") from exc
    if (not isinstance(policy, dict) or policy.get("schema_version") != VERSION
            or policy.get("actor") != actor):
        raise ContractError("Local manual resource policy must match this 15m actor")
    return _workers(policy.get("symbol_workers"))
