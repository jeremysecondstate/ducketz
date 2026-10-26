"""One-join reuse of immutable receipt proofs with a final identity fence.

Callbacks retain the application's receipt and path validation. This helper has
no storage, provider, model, or cross-join cache and performs no automatic retry.
"""
from __future__ import annotations


class ReceiptJoinProofs:
    """Reuse successful proofs only within one caller-owned join snapshot."""

    def __init__(self, verify, identity, integrity_errors):
        self._verify = verify
        self._identity = identity
        self._integrity_errors = integrity_errors
        self._proofs = {}
        self._changed = False

    def verify(self, key, value, *, uses):
        # One or two uses cannot repay the initial/final identity fence. Errors
        # are never cached: a later ordinary row can observe restored evidence.
        if uses < 3:
            return self._verify(value)
        if key in self._proofs:
            return self._proofs[key][2]
        try:
            before = self._identity(value)
        except self._integrity_errors:
            before = None
        recorded = self._verify(value)
        try:
            after = self._identity(value)
        except self._integrity_errors:
            after = None
        if before is None or after is None or before != after:
            self._changed = True
        else:
            self._proofs[key] = (value, after, recorded)
        return recorded

    def fences_unchanged(self):
        """Return false on an expected change; the caller uses rowwise fallback.

        Unexpected callback exceptions propagate. No failed or changed proof is
        promoted, and this method does not loop or invoke the caller's fallback.
        """
        if self._changed:
            return False
        for value, expected, _ in self._proofs.values():
            try:
                if self._identity(value) != expected:
                    return False
                self._verify(value)
                if self._identity(value) != expected:
                    return False
            except self._integrity_errors:
                return False
        return True
