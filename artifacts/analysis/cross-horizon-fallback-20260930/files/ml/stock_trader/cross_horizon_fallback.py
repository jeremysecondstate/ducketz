"""Frozen, prospective ownership policy for bounded bearish fallback sales.

This module is pure except for the explicit immutable-publication reader. It
does not grant order authority, assign inventory, or read a broker account.
"""
from __future__ import annotations

from copy import deepcopy
from datetime import date
import json
from pathlib import Path
from typing import Mapping

import pandas as pd


POLICY_VERSION = "hierarchical-bearish-fallback-v1"
EFFECTIVE_ACTION_DATE = "2026-10-01"
POLICY_FIELD = "cross_horizon_fallback_policy"
_HORIZONS = ("1h", "4h", "1d", "1w")
_WEIGHTS = {"1h": 1, "4h": 2, "1d": 3}
_SLOTS = tuple((hour, horizon) for hour in range(4, 17)
               for horizon in ("1h", "4h", "1d")
               if horizon == "1h" or (horizon == "4h" and hour in (4, 8, 12, 16))
               or (horizon == "1d" and hour == 4))
_POLICY = {
    "policy_version": POLICY_VERSION,
    "effective_action_date": EFFECTIVE_ACTION_DATE,
    "daily_cap_numerator": 1,
    "daily_cap_denominator": 2,
    "donor_daily_cap_numerator": 1,
    "donor_daily_cap_denominator": 2,
    "baseline": "first-ready-account-snapshot-owned-longer-horizon-shares-per-action-date",
    "budget_scope": "shared-symbol-and-individual-donor-allocation",
    "pacing": "fixed-18-weighted-slots-incremental-floor-no-rollover",
    "slots_per_symbol": 18,
    "slot_horizon_weights": dict(_WEIGHTS),
    "total_slot_weight": 24,
    "donor_order": "nearest-longer-horizon-first-one-donor-per-forecast",
    "hierarchy": {h: list(_HORIZONS[i + 1:]) for i, h in enumerate(_HORIZONS)},
    "own_horizon_sales": "unchanged-before-fallback",
    "same_clock_bullish_donors": "excluded",
    "pending_or_blocked_own_inventory": "ineligible-for-fallback",
    "budget_usage": "confirmed-fills-plus-outstanding-and-unknown-reservations",
    "daily_reset": "new-action-date-actual-remaining-holdings",
    "lifetime_cap": None,
}


def _date(value: object) -> str:
    result = date.fromisoformat(str(value)).isoformat()
    if result != str(value):
        raise ValueError("Action date must be an exact ISO date")
    return result


def policy_for_action_date(action_date: object) -> dict | None:
    """Policy for new publications only; never infer a saved source's policy."""
    return deepcopy(_POLICY) if _date(action_date) >= EFFECTIVE_ACTION_DATE else None


def validate_fallback_policy(value: object, action_date: object) -> dict | None:
    expected = policy_for_action_date(action_date)
    if value is None:
        return None
    # Exact JSON types also reject bool-as-int and undocumented parameter drift.
    if (not isinstance(value, Mapping) or expected is None
            or json.dumps(dict(value), sort_keys=True, allow_nan=False)
            != json.dumps(expected, sort_keys=True, allow_nan=False)):
        raise ValueError("Unsupported or premature cross-horizon fallback policy")
    return deepcopy(expected)


def donor_horizons(horizon: str) -> tuple[str, ...]:
    if horizon not in _HORIZONS:
        raise ValueError("Unknown stock horizon")
    return _HORIZONS[_HORIZONS.index(horizon) + 1:]


def slot_quota(budget: int, horizon: str, target_start: object, action_date: object) -> int:
    """Disjoint integer quotas across all 18 scheduled fallback opportunities.

    Bullish, neutral, skipped, blocked and unused opportunities do not donate
    their quota to another forecast. Retrying a slot never creates a new quota.
    """
    if type(budget) is not int or budget < 0:
        raise ValueError("Fallback budget must be nonnegative whole shares")
    if horizon == "1w":
        return 0
    index = slot_index(horizon, target_start, action_date)
    before = sum(_WEIGHTS[h] for _, h in _SLOTS[:index])
    after = before + _WEIGHTS[horizon]
    return after * budget // 24 - before * budget // 24


def slot_index(horizon: str, target_start: object, action_date: object) -> int:
    day = _date(action_date)
    stamp = pd.Timestamp(target_start)
    if stamp.tzinfo is None:
        raise ValueError("Fallback target start must include a timezone")
    local = stamp.tz_convert("America/Los_Angeles")
    key = (local.hour, horizon)
    if (local.date().isoformat() != day or local.minute or local.second
            or local.microsecond or local.nanosecond or key not in _SLOTS):
        raise ValueError("Fallback signal is outside the frozen entry grid")
    return _SLOTS.index(key)


def verify_policy_metadata(run: Path, manifest: Mapping, receipt: Mapping | None = None) -> dict | None:
    """Verify policy identity against the already checksum-verified manifest."""
    config = manifest.get("configuration", {})
    payload = json.loads((Path(run) / "gameplan.json").read_text(encoding="utf-8"))
    policy = config.get(POLICY_FIELD)
    if payload.get(POLICY_FIELD) != policy or (receipt is not None and receipt.get(POLICY_FIELD) != policy):
        raise ValueError("Gameplan fallback policy bindings disagree")
    if policy is not None:
        if "gameplan.json" not in manifest.get("output_files", {}):
            raise ValueError("Fallback policy is not manifest-bound")
        if payload.get("action_date") != config.get("action_date"):
            raise ValueError("Fallback policy action date bindings disagree")
        if receipt is not None and receipt.get("action_date") != config.get("action_date"):
            raise ValueError("Fallback receipt action date differs")
        return validate_fallback_policy(policy, config.get("action_date"))
    return None


def read_gameplan_fallback_policy(run: Path, action_date: object) -> dict | None:
    """Read only the exact verified source; old publications remain unchanged."""
    day = _date(action_date)
    if day < EFFECTIVE_ACTION_DATE:
        return None
    from ml.artifacts import file_checksum, verify_manifest
    run = Path(run)
    manifest = verify_manifest(run)
    receipt = json.loads((run / "receipt.json").read_text(encoding="utf-8"))
    if (manifest.get("configuration", {}).get("action_date") != day
            or receipt.get("action_date") != day
            or receipt.get("manifest_checksum_sha256") != file_checksum(run / "manifest.json")):
        raise ValueError("Fallback source receipt or action date differs")
    return verify_policy_metadata(run, manifest, receipt)
