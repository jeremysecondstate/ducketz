"""Shared account ownership for Powder and the manual Hyperliquid workspace."""
from contextlib import contextmanager, ExitStack
from pathlib import Path
import os
import threading
from uuid import uuid4

from filelock import FileLock, Timeout

LOCK_ROOT = Path("C:/DATASTORE/hyperliquid/_powder/_ownership")
ACCOUNTS = ("alex", "jeremy", "clearpond")
_owned = {}
_mutex = threading.RLock()


def _lock(account):
    if account not in ACCOUNTS:
        raise ValueError("Unknown Hyperliquid account")
    LOCK_ROOT.mkdir(parents=True, exist_ok=True)
    return FileLock(str(LOCK_ROOT / f"{account}.lock"), timeout=0)


@contextmanager
def account_ownership(accounts=ACCOUNTS):
    """Hold every account lock for the full automated session, or acquire none."""
    accounts = tuple(sorted(set(accounts)))
    if not accounts:
        raise ValueError("Account ownership cannot be empty")
    token = uuid4().hex
    try:
        with ExitStack() as stack:
            for account in accounts:
                stack.enter_context(_lock(account))
            with _mutex:
                _owned[token] = (frozenset(accounts), os.getpid())
            try:
                yield token
            finally:
                with _mutex:
                    _owned.pop(token, None)
    except Timeout:
        raise PermissionError("Hyperliquid account is owned by another trading action or Powder session") from None


def assert_ownership(token, account):
    with _mutex:
        held, pid = _owned.get(token, ((), None))
    if account not in held or pid != os.getpid():
        raise PermissionError("Powder requires active account ownership in this process")


@contextmanager
def manual_action_guard(account):
    """Hold through the entire signed manual action, avoiding a precheck race."""
    try:
        with _lock(account.strip().lower()):
            yield
    except Timeout:
        raise PermissionError("Manual trading is unavailable while Powder owns this account") from None
