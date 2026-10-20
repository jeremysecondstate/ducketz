"""Offline credential-owner regression using temporary fixture files only."""
from __future__ import annotations

import ctypes
from contextlib import contextmanager
import json
import os
import shutil
import subprocess

import pytest

import app.services.schwab_token_store as token_store


pytestmark = pytest.mark.skipif(os.name != "nt", reason="Windows owner protection")


def _function(library, name, arguments, result=ctypes.c_int):
    function = getattr(library, name)
    function.argtypes, function.restype = arguments, result
    return function


def _security(path):
    powershell = shutil.which("pwsh") or shutil.which("powershell")
    if powershell is None:
        pytest.skip("PowerShell is unavailable")
    environment = dict(os.environ, DUCKET_ACL_TARGET=str(path))
    result = subprocess.run(
        [powershell, "-NoProfile", "-Command",
         "$acl = Get-Acl -LiteralPath $env:DUCKET_ACL_TARGET; "
         "[pscustomobject]@{sddl=$acl.Sddl; protected=$acl.AreAccessRulesProtected; "
         "owner=$acl.GetOwner([System.Security.Principal.SecurityIdentifier]).Value; "
         "user=[System.Security.Principal.WindowsIdentity]::GetCurrent().User.Value"
         "} | ConvertTo-Json -Compress"],
        check=True, capture_output=True, text=True, env=environment,
    )
    return json.loads(result.stdout)


def test_cache_and_lock_pin_owner_to_token_user_and_retain_owner_only_dacls(tmp_path, monkeypatch):
    path = tmp_path / "schwab_tokens.json"
    monkeypatch.setattr(token_store, "TOKEN_CACHE_PATH", path)
    token_store.write_token_payload_atomic({"access_token": "temporary-fixture"})

    for target in (path, path.with_name(path.name + ".lock")):
        metadata = _security(target)
        assert metadata["owner"] == metadata["user"]
        assert metadata["owner"] == token_store._windows_current_user_sid()
        assert metadata["protected"] is True
        assert metadata["sddl"].split("D:", 1)[1] == "P(A;;FA;;;OW)"


def test_owner_query_failure_preserves_existing_credential_bytes(tmp_path, monkeypatch):
    path = tmp_path / "schwab_tokens.json"
    monkeypatch.setattr(token_store, "TOKEN_CACHE_PATH", path)
    token_store.write_token_payload_atomic({"access_token": "original-fixture"})
    original = path.read_bytes()

    def denied():
        raise PermissionError("TokenUser query denied fixture")

    monkeypatch.setattr(token_store, "_windows_current_user_sid", denied)
    with pytest.raises(PermissionError, match="TokenUser query denied fixture"):
        token_store.write_token_payload_atomic({"access_token": "replacement-fixture"})
    assert path.read_bytes() == original
    assert not list(tmp_path.glob(".schwab_tokens.json.*.tmp"))


def _fixture_administrators_owner(path):
    """Reproduce an elevated writer's historical protected group owner."""
    advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    convert = _function(advapi32, "ConvertStringSecurityDescriptorToSecurityDescriptorW", [
        ctypes.c_wchar_p, ctypes.c_uint, ctypes.POINTER(ctypes.c_void_p), ctypes.c_void_p,
    ])
    apply = _function(advapi32, "SetFileSecurityW", [
        ctypes.c_wchar_p, ctypes.c_uint, ctypes.c_void_p,
    ])
    free = _function(kernel32, "LocalFree", [ctypes.c_void_p], ctypes.c_void_p)
    descriptor = ctypes.c_void_p()
    if not convert("O:BAD:P(A;;FA;;;OW)", 1, ctypes.byref(descriptor), None):
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        if not apply(str(path), 0x80000005, descriptor):
            raise ctypes.WinError(ctypes.get_last_error())
    finally:
        free(descriptor)


@contextmanager
def _without_administrator_rights():
    """Temporarily restrict this test thread; open handles without reading data."""
    class SidAndAttributes(ctypes.Structure):
        _fields_ = [("Sid", ctypes.c_void_p), ("Attributes", ctypes.c_uint)]

    advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    handle_pointer = ctypes.POINTER(ctypes.c_void_p)
    current = _function(kernel32, "GetCurrentProcess", [], ctypes.c_void_p)
    open_token = _function(advapi32, "OpenProcessToken", [ctypes.c_void_p, ctypes.c_uint, handle_pointer])
    convert = _function(advapi32, "ConvertStringSidToSidW", [ctypes.c_wchar_p, handle_pointer])
    restrict = _function(advapi32, "CreateRestrictedToken", [
        ctypes.c_void_p, ctypes.c_uint, ctypes.c_uint, ctypes.POINTER(SidAndAttributes),
        ctypes.c_uint, ctypes.c_void_p, ctypes.c_uint, ctypes.c_void_p, handle_pointer,
    ])
    duplicate = _function(advapi32, "DuplicateTokenEx", [
        ctypes.c_void_p, ctypes.c_uint, ctypes.c_void_p, ctypes.c_int, ctypes.c_int, handle_pointer,
    ])
    impersonate = _function(advapi32, "ImpersonateLoggedOnUser", [ctypes.c_void_p])
    revert = _function(advapi32, "RevertToSelf", [])
    open_file = _function(kernel32, "CreateFileW", [
        ctypes.c_wchar_p, ctypes.c_uint, ctypes.c_uint, ctypes.c_void_p,
        ctypes.c_uint, ctypes.c_uint, ctypes.c_void_p,
    ], ctypes.c_void_p)
    close = _function(kernel32, "CloseHandle", [ctypes.c_void_p])
    free = _function(kernel32, "LocalFree", [ctypes.c_void_p], ctypes.c_void_p)
    token, restricted, impersonation, sid = (ctypes.c_void_p() for _ in range(4))
    impersonating = False

    def access_error(path, rights):
        handle = open_file(str(path), rights, 7, None, 3, 0, None)
        if handle == ctypes.c_void_p(-1).value:
            return ctypes.get_last_error()
        close(handle)
        return 0

    try:
        if not open_token(current(), 0x000A, ctypes.byref(token)):
            raise ctypes.WinError(ctypes.get_last_error())
        if not convert("S-1-5-32-544", ctypes.byref(sid)):
            raise ctypes.WinError(ctypes.get_last_error())
        administrator = SidAndAttributes(sid, 0)
        if not restrict(token, 1, 1, ctypes.byref(administrator), 0, None, 0, None, ctypes.byref(restricted)):
            raise ctypes.WinError(ctypes.get_last_error())
        if not duplicate(restricted, 0x02000000, None, 2, 2, ctypes.byref(impersonation)):
            raise ctypes.WinError(ctypes.get_last_error())
        if not impersonate(impersonation):
            raise ctypes.WinError(ctypes.get_last_error())
        impersonating = True
        yield access_error
    finally:
        revert_error = None
        if impersonating and not revert():
            revert_error = ctypes.get_last_error()
        for handle in (impersonation, restricted, token):
            if handle.value:
                close(handle)
        if sid.value:
            free(sid)
        if revert_error is not None:
            raise ctypes.WinError(revert_error)


def test_group_owned_cache_recovers_same_user_access_without_admin_rights(tmp_path):
    shell32 = ctypes.WinDLL("shell32", use_last_error=True)
    is_admin = _function(shell32, "IsUserAnAdmin", [])
    if not is_admin():
        pytest.skip("An already elevated token is needed to create the historical group owner fixture")
    path = tmp_path / "fake-credential-cache.json"
    original = b'{"access_token":"temporary-offline-fixture"}'
    path.write_bytes(original)
    _fixture_administrators_owner(path)
    with _without_administrator_rights() as access_error:
        assert access_error(path, 0x80000000) == 5  # GENERIC_READ
        assert access_error(path, 0x00040000) == 5  # WRITE_DAC

    token_store._protect_token_path(path)

    with _without_administrator_rights() as access_error:
        assert access_error(path, 0x80000000) == 0
        assert access_error(path, 0x00040000) == 0
    assert path.read_bytes() == original
    metadata = _security(path)
    assert metadata["owner"] == metadata["user"]
    assert metadata["sddl"].split("D:", 1)[1] == "P(A;;FA;;;OW)"
