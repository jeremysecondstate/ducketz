"""Native Windows ACL checks use dummy files and never create a broker client."""
from contextlib import contextmanager
import ctypes
from ctypes import wintypes
import os
import socket

import pytest

from app.services import schwab_token_store as tokens


pytestmark = pytest.mark.skipif(os.name != "nt", reason="Native Windows security API regression")


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("Network forbidden in token ACL regression")
    monkeypatch.setattr(socket, "socket", forbidden)


def _apis():
    advapi = ctypes.WinDLL("advapi32", use_last_error=True)
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.GetCurrentProcess.restype = wintypes.HANDLE
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel.CloseHandle.restype = wintypes.BOOL
    kernel.LocalFree.argtypes = [ctypes.c_void_p]
    kernel.LocalFree.restype = ctypes.c_void_p
    advapi.OpenProcessToken.argtypes = [wintypes.HANDLE, wintypes.DWORD, ctypes.POINTER(wintypes.HANDLE)]
    advapi.OpenProcessToken.restype = wintypes.BOOL
    advapi.GetTokenInformation.argtypes = [wintypes.HANDLE, wintypes.DWORD, ctypes.c_void_p,
                                         wintypes.DWORD, ctypes.POINTER(wintypes.DWORD)]
    advapi.GetTokenInformation.restype = wintypes.BOOL
    advapi.DuplicateToken.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.POINTER(wintypes.HANDLE)]
    advapi.DuplicateToken.restype = wintypes.BOOL
    advapi.GetFileSecurityW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, ctypes.c_void_p,
                                      wintypes.DWORD, ctypes.POINTER(wintypes.DWORD)]
    advapi.GetFileSecurityW.restype = wintypes.BOOL
    advapi.SetFileSecurityW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, ctypes.c_void_p]
    advapi.SetFileSecurityW.restype = wintypes.BOOL
    advapi.ConvertStringSecurityDescriptorToSecurityDescriptorW.argtypes = [
        wintypes.LPCWSTR, wintypes.DWORD, ctypes.POINTER(ctypes.c_void_p), ctypes.POINTER(wintypes.DWORD)]
    advapi.ConvertStringSecurityDescriptorToSecurityDescriptorW.restype = wintypes.BOOL
    advapi.ConvertSecurityDescriptorToStringSecurityDescriptorW.argtypes = [
        ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD,
        ctypes.POINTER(wintypes.LPWSTR), ctypes.POINTER(wintypes.DWORD)]
    advapi.ConvertSecurityDescriptorToStringSecurityDescriptorW.restype = wintypes.BOOL
    return advapi, kernel


def _require(ok):
    if not ok:
        raise ctypes.WinError(ctypes.get_last_error())


def _descriptor(path):
    advapi, _ = _apis()
    size = wintypes.DWORD()
    assert not advapi.GetFileSecurityW(str(path), 7, None, 0, ctypes.byref(size))
    assert ctypes.get_last_error() == 122
    value = ctypes.create_string_buffer(size.value)
    _require(advapi.GetFileSecurityW(str(path), 7, value, len(value), ctypes.byref(size)))
    return value


def _sddl(path):
    advapi, kernel = _apis()
    value = wintypes.LPWSTR()
    _require(advapi.ConvertSecurityDescriptorToStringSecurityDescriptorW(
        _descriptor(path), 1, 5, ctypes.byref(value), None))
    try:
        return value.value
    finally:
        kernel.LocalFree(value)


def _set_sddl(path, value):
    advapi, kernel = _apis()
    descriptor = ctypes.c_void_p()
    _require(advapi.ConvertStringSecurityDescriptorToSecurityDescriptorW(
        value, 1, ctypes.byref(descriptor), None))
    try:
        _require(advapi.SetFileSecurityW(str(path), 0x80000005, descriptor))
    finally:
        kernel.LocalFree(descriptor)


@contextmanager
def _filtered_token():
    """UAC's real linked token; identification level suffices for AccessCheck."""
    advapi, kernel = _apis()
    current, linked, duplicate = (wintypes.HANDLE() for _ in range(3))
    try:
        _require(advapi.OpenProcessToken(kernel.GetCurrentProcess(), 10, ctypes.byref(current)))
        size, elevation = wintypes.DWORD(), wintypes.DWORD()
        _require(advapi.GetTokenInformation(current, 18, ctypes.byref(elevation),
                                           ctypes.sizeof(elevation), ctypes.byref(size)))
        if elevation.value != 2:
            pytest.skip("Native elevated/filtered UAC pair required")
        _require(advapi.GetTokenInformation(current, 19, ctypes.byref(linked),
                                           ctypes.sizeof(linked), ctypes.byref(size)))
        _require(advapi.DuplicateToken(linked, 1, ctypes.byref(duplicate)))
        yield duplicate
    finally:
        for handle in (duplicate, linked, current):
            if handle.value:
                kernel.CloseHandle(handle)


def _access(path, token, desired):
    class Mapping(ctypes.Structure):
        _fields_ = [(name, wintypes.DWORD) for name in ("read", "write", "execute", "all")]
    advapi, _ = _apis()
    advapi.AccessCheck.argtypes = [ctypes.c_void_p, wintypes.HANDLE, wintypes.DWORD,
        ctypes.POINTER(Mapping), ctypes.c_void_p, ctypes.POINTER(wintypes.DWORD),
        ctypes.POINTER(wintypes.DWORD), ctypes.POINTER(wintypes.BOOL)]
    advapi.AccessCheck.restype = wintypes.BOOL
    mapping = Mapping(0x120089, 0x120116, 0x1200a0, 0x1f01ff)
    privileges = ctypes.create_string_buffer(4096)
    size, granted, allowed = wintypes.DWORD(len(privileges)), wintypes.DWORD(), wintypes.BOOL()
    _require(advapi.AccessCheck(_descriptor(path), token, desired, ctypes.byref(mapping),
        privileges, ctypes.byref(size), ctypes.byref(granted), ctypes.byref(allowed)))
    return bool(allowed.value)


def test_admin_owned_owner_rights_cache_recovers_for_filtered_user(tmp_path):
    path = tmp_path / "dummy-token.json"
    path.write_bytes(b'{"fixture":true}')
    with _filtered_token() as filtered:
        # Exact prior ACL: elevated creation owns it as Administrators; OW-only
        # protects that group, which is deny-only in the normal UAC token.
        _set_sddl(path, "O:BAD:P(A;;FA;;;OW)")
        before = path.read_bytes()
        assert not _access(path, filtered, 1)  # FILE_READ_DATA
        assert not _access(path, filtered, 0x40000)  # WRITE_DAC
        tokens._protect_token_path(path)
        assert _access(path, filtered, 1 | 2 | 0x40000 | 0x80000)
        assert path.read_bytes() == before
        assert ";;;OW)" not in _sddl(path) and "O:BA" not in _sddl(path)


@pytest.mark.parametrize("directory", [False, True])
def test_private_owner_and_acl_are_stable_across_reprotection(tmp_path, directory):
    path = tmp_path / "private"
    path.mkdir() if directory else path.write_bytes(b"dummy")
    tokens._protect_token_path(path, directory=directory)
    first = _sddl(path)
    sid = tokens._windows_process_user_sid()
    assert first == f"O:{sid}D:P(A;{'OICI' if directory else ''};FA;;;{sid})"
    tokens._protect_token_path(path, directory=directory)
    assert _sddl(path) == first


def test_atomic_cache_replacement_preserves_filtered_access_and_lock_identity(tmp_path, monkeypatch):
    path = tmp_path / "dummy-token.json"
    monkeypatch.setattr(tokens, "TOKEN_CACHE_PATH", path)
    with _filtered_token() as filtered:
        tokens.write_token_payload_atomic({"fixture_generation": 1})
        lock = path.with_name(path.name + ".lock")
        lock_id = lock.stat().st_ino
        tokens.write_token_payload_atomic({"fixture_generation": 2})
        assert tokens.load_token_payload(strict=True) == {"fixture_generation": 2}
        assert lock.stat().st_ino == lock_id
        for protected in (path, lock):
            assert _access(protected, filtered, 1 | 2 | 0x40000)
            assert ";;;OW)" not in _sddl(protected)
        assert not list(tmp_path.glob("*.tmp"))


def test_failed_private_protection_never_publishes_candidate(tmp_path, monkeypatch):
    path = tmp_path / "dummy-token.json"
    monkeypatch.setattr(tokens, "TOKEN_CACHE_PATH", path)
    tokens.write_token_payload_atomic({"fixture_generation": 1})
    before = path.read_bytes()
    protect = tokens._protect_token_path
    failure = PermissionError("fixture denied ACL update")
    def deny_candidate(candidate, **kwargs):
        if candidate.name.endswith(".tmp"):
            raise failure
        return protect(candidate, **kwargs)
    monkeypatch.setattr(tokens, "_protect_token_path", deny_candidate)
    with pytest.raises(PermissionError) as caught:
        tokens.write_token_payload_atomic({"fixture_generation": 2})
    assert caught.value is failure
    assert path.read_bytes() == before
    assert not list(tmp_path.glob("*.tmp"))
