"""Private reviewed host binding. Reading this file never enables a trader."""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
import re
from typing import Mapping

from ml.stock_trader.state import validated_symbols

VERSION = "sole-coordinator-account-gameplan-v1"
FIRST_USE_VERSION = "account-gameplan-first-use-v1"
DECLARATION_VERSION = "gameplan-first-use-declaration-v1"
CONFIG = Path("state/account-gameplan/config.json")


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"),
                                    allow_nan=False).encode()).hexdigest()


def _hash(value):
    if not isinstance(value, str) or not re.fullmatch("[a-f0-9]{64}", value):
        raise ValueError("Account configuration requires an exact SHA-256 identity")
    return value


def local_path(root, value, folder):
    if not isinstance(value, str) or "\\" in value or Path(value).is_absolute():
        raise ValueError("Account reference must be a relative immutable path")
    result = (Path(root) / value).resolve()
    if result.parent != (Path(root) / folder).resolve():
        raise ValueError("Account reference escapes its immutable run directory")
    return result


@dataclass(frozen=True)
class AccountConfig:
    machine_id: str
    coordinator_id: str
    participants: Mapping[str, tuple[str, ...]]
    account_fingerprint: str
    fingerprint: str
    activation: Mapping

    @property
    def symbols(self):
        return tuple(sorted(symbol for symbols in self.participants.values() for symbol in symbols))

    @property
    def role(self):
        return "coordinator" if self.machine_id == self.coordinator_id else "producer"


def load_account_config(root) -> AccountConfig | None:
    path = Path(root) / CONFIG
    if not path.exists():
        return None
    raw = json.loads(path.read_text(encoding="utf-8"))
    if (set(raw) != {"schema_version", "machine_id", "coordinator_id", "participants",
                    "account_fingerprint", "activation"}
            or raw["schema_version"] != VERSION or raw["coordinator_id"] != "pc-original"
            or raw["machine_id"] not in {"pc-original", "pc-new"}
            or set(raw["participants"]) != {"pc-original", "pc-new"}):
        raise ValueError("Unrecognized sole-coordinator account configuration")
    partitions = {key: tuple(sorted(validated_symbols(value))) for key, value in raw["participants"].items()}
    if set(partitions["pc-original"]) & set(partitions["pc-new"]):
        raise ValueError("Overlapping producer ownership")
    binding = {key: value for key, value in raw.items() if key != "activation"}
    fingerprint = digest(binding)
    activation = raw["activation"]
    if not isinstance(activation, dict) or activation.get("status") not in {"PREPARING", "ACTIVE"}:
        raise ValueError("Account activation status is missing")
    if activation.get("binding_sha256") != fingerprint:
        raise ValueError("Account activation is bound to another host or symbol registry")
    return AccountConfig(raw["machine_id"], raw["coordinator_id"], partitions,
                         _hash(raw["account_fingerprint"]), fingerprint, activation)


def assert_coordinator(config, sizing_policy):
    if config is None:
        return
    from ml.stock_trader.sizing_policy import GAMEPLAN_SIZING_POLICY
    if config.role != "coordinator":
        raise ValueError("FORECAST_PRODUCER_HAS_NO_ORDER_AUTHORITY")
    if sizing_policy != GAMEPLAN_SIZING_POLICY:
        raise ValueError("COMBINED_ACCOUNT_REQUIRES_SELECTED_GAMEPLAN_POLICY")
    if config.activation["status"] != "ACTIVE":
        raise ValueError("COMBINED_ACCOUNT_CUTOVER_NOT_ACTIVE")


def verify_cutover(root, config):
    """Require local reviewed receipts; no peer notice can activate this binding."""
    ref = config.activation
    path = local_path(root, ref.get("receipt_path"), "state/account-gameplan/cutovers")
    raw = path.read_bytes()
    if hashlib.sha256(raw).hexdigest() != _hash(ref.get("receipt_sha256")):
        raise ValueError("Account cutover receipt changed")
    saved = json.loads(raw)
    if saved.get("schema_version") == FIRST_USE_VERSION:
        required = {"schema_version", "status", "binding_sha256", "machine_id", "coordinator_id",
                    "account_fingerprint", "operation_id", "declaration_sha256", "manual_invocation_sha256",
                    "native_ledger_policy", "runtime_reconciliation_required", "orders_placed", "broker_calls"}
        if (set(saved) != required or saved["status"] != "VERIFIED"
                or saved["binding_sha256"] != config.fingerprint
                or saved["account_fingerprint"] != config.account_fingerprint
                or saved["machine_id"] != config.machine_id or saved["coordinator_id"] != config.coordinator_id
                or config.machine_id != "pc-original" or config.role != "coordinator"
                or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{7,95}", str(saved["operation_id"]))
                or saved["native_ledger_policy"] != "PRESERVE_EXISTING_ATLAS_ACCOUNT_WIDE_HISTORY"
                or saved["runtime_reconciliation_required"] is not True
                or saved["orders_placed"] != 0 or saved["broker_calls"] != 0):
            raise ValueError("Atlas sole-executor setup receipt is invalid")
        folder = Path(root) / "state/account-gameplan/first-use" / saved["operation_id"]
        declaration_raw = (folder / "declaration.json").read_bytes()
        invocation_raw = (folder / "manual-invocation.json").read_bytes()
        if (hashlib.sha256(declaration_raw).hexdigest() != _hash(saved["declaration_sha256"])
                or hashlib.sha256(invocation_raw).hexdigest() != _hash(saved["manual_invocation_sha256"])):
            raise ValueError("Atlas sole-executor setup evidence changed")
        validate_first_use_declaration(json.loads(declaration_raw), config)
        invocation = json.loads(invocation_raw)
        if (set(invocation) != {"operation_id", "trigger", "requested_at"}
                or invocation["operation_id"] != saved["operation_id"]
                or invocation["trigger"] != "EXPLICIT_MANUAL_START"
                or not isinstance(invocation["requested_at"], str) or not invocation["requested_at"]):
            raise ValueError("Atlas sole-executor manual invocation is invalid")
        if json.loads(declaration_raw)["operation_id"] != saved["operation_id"]:
            raise ValueError("Atlas sole-executor operation differs")
        return saved
    required = {"schema_version", "status", "binding_sha256", "machine_id", "coordinator_id",
                "peer_execution_fenced", "peer_fence_receipt_sha256", "migration_manifest_sha256",
                "fresh_union_reconciliation_sha256", "installed_source_commit", "orders_placed"}
    if (set(saved) != required or saved["schema_version"] != VERSION or saved["status"] != "VERIFIED"
            or saved["binding_sha256"] != config.fingerprint or saved["machine_id"] != config.machine_id
            or saved["coordinator_id"] != config.coordinator_id or saved["peer_execution_fenced"] is not True
            or saved["orders_placed"] != 0
            or not re.fullmatch("[a-f0-9]{40}", str(saved["installed_source_commit"]))):
        raise ValueError("Reviewed sole-host cutover evidence is incomplete")
    for key in ("peer_fence_receipt_sha256", "migration_manifest_sha256", "fresh_union_reconciliation_sha256"):
        _hash(saved[key])
    return saved


def validate_first_use_declaration(value, config):
    fields = {"schema_version", "operation_id", "account_binding_sha256", "account_fingerprint",
              "executor", "basis", "peer_history_expected", "manual_start_only"}
    if (not isinstance(value, dict) or set(value) != fields or value["schema_version"] != DECLARATION_VERSION
            or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{7,95}", str(value["operation_id"]))
            or value["account_binding_sha256"] != config.fingerprint
            or value["account_fingerprint"] != config.account_fingerprint
            or value["executor"] != "Atlas" or value["basis"] != "LOCAL_HUMAN_ATLAS_SOLE_EXECUTOR"
            or value["peer_history_expected"] is not False or value["manual_start_only"] is not True
            or config.machine_id != "pc-original" or config.role != "coordinator"):
        raise ValueError("ATLAS_SOLE_EXECUTOR_DECLARATION_INVALID")
