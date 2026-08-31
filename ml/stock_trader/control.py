from __future__ import annotations

import os
from pathlib import Path

from ml.artifacts import file_checksum
from ml.stock_trader.contracts import ActivationIntent


OPERATOR_INTENT_RELATIVE_PATH = Path("controls/stock-trader/operator-intent.txt")
_KEY = "CONFIRM_ACTIVE_TRADING"


def operator_intent_path(datastore_root: Path) -> Path:
    return Path(datastore_root).resolve() / OPERATOR_INTENT_RELATIVE_PATH


def read_activation_intent(
    datastore_root: Path,
    *,
    path: Path | None = None,
) -> ActivationIntent:
    target = Path(path or operator_intent_path(datastore_root)).resolve()
    if not target.is_file():
        return ActivationIntent(
            active=False,
            status="INACTIVE",
            reason="OPERATOR_INTENT_MISSING",
            path=str(target),
            checksum_sha256=None,
        )
    try:
        lines = [line.strip() for line in target.read_text(encoding="utf-8").splitlines() if line.strip()]
    except OSError:
        return ActivationIntent(
            active=False,
            status="INACTIVE",
            reason="OPERATOR_INTENT_UNREADABLE",
            path=str(target),
            checksum_sha256=None,
        )
    if len(lines) != 1 or "=" not in lines[0]:
        return _invalid(target, "OPERATOR_INTENT_MALFORMED")
    key, raw_value = (part.strip() for part in lines[0].split("=", 1))
    if key != _KEY or raw_value not in {"TRUE", "FALSE"}:
        return _invalid(target, "OPERATOR_INTENT_MALFORMED")
    active = raw_value == "TRUE"
    return ActivationIntent(
        active=active,
        status="ACTIVE" if active else "INACTIVE",
        reason="OPERATOR_INTENT_TRUE" if active else "OPERATOR_INTENT_FALSE",
        path=str(target),
        checksum_sha256=file_checksum(target),
    )


def write_activation_intent(
    datastore_root: Path,
    *,
    active: bool,
    path: Path | None = None,
) -> Path:
    """Atomically write the human-facing persistent activation toggle."""

    if not isinstance(active, bool):
        raise TypeError("active must be an explicit boolean")
    target = Path(path or operator_intent_path(datastore_root)).resolve()
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_suffix(target.suffix + f".tmp-{os.getpid()}")
    try:
        temporary.write_text(
            f"{_KEY}={'TRUE' if active else 'FALSE'}\n",
            encoding="utf-8",
        )
        temporary.replace(target)
    finally:
        temporary.unlink(missing_ok=True)
    return target


def _invalid(path: Path, reason: str) -> ActivationIntent:
    return ActivationIntent(
        active=False,
        status="INACTIVE",
        reason=reason,
        path=str(path),
        checksum_sha256=file_checksum(path),
    )


__all__ = [
    "OPERATOR_INTENT_RELATIVE_PATH",
    "operator_intent_path",
    "read_activation_intent",
    "write_activation_intent",
]
