"""Separate present historical construction from evidence of past possession.

Unversioned frozen recipes retain their old receipt-gated interpretation. A
local recipe migration must explicitly select either new mode; installing
shared source never changes another PC's saved population.
"""
from .prediction import ContractError

VERSION = "intraday-training-timing-v1"
LEGACY = "legacy_receipt_gated_v1"
HISTORICAL_RESEARCH = "historical_research_v1"
OPERATIONAL_REPLAY = "exact_operational_replay_v1"


def validate_timing_mode(mode):
    if mode not in {LEGACY, HISTORICAL_RESEARCH, OPERATIONAL_REPLAY}:
        raise ContractError("Unknown training timing mode")
    return mode


def timing_contract(mode):
    return {"version": VERSION, "mode": validate_timing_mode(mode)}


def timing_mode_from_metadata(metadata):
    contract = (metadata or {}).get("training_timing_contract")
    if contract is None:
        return LEGACY
    if not isinstance(contract, dict) or contract != timing_contract(contract.get("mode")):
        raise ContractError("Incompatible training timing contract")
    return contract["mode"]
