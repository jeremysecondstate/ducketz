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


def _token_number(token, kind):
    advapi, _ = _apis()
    value, size = wintypes.DWORD(), wintypes.DWORD()
    _require(advapi.GetTokenInformation(token, kind, ctypes.byref(value),
                                      ctypes.sizeof(value), ctypes.byref(size)))
    return value.value


def _token_user(token):
    class SidAndAttributes(ctypes.Structure):
        _fields_ = [("sid", ctypes.c_void_p), ("attributes", wintypes.DWORD)]
    advapi, kernel = _apis()
    size = wintypes.DWORD()
    assert not advapi.GetTokenInformation(token, 1, None, 0, ctypes.byref(size))
    assert ctypes.get_last_error() == 122
    value = ctypes.create_string_buffer(size.value)
    _require(advapi.GetTokenInformation(token, 1, value, len(value), ctypes.byref(size)))
    advapi.ConvertSidToStringSidW.argtypes = [ctypes.c_void_p, ctypes.POINTER(wintypes.LPWSTR)]
    advapi.ConvertSidToStringSidW.restype = wintypes.BOOL
    text = wintypes.LPWSTR()
    _require(advapi.ConvertSidToStringSidW(
        ctypes.cast(value, ctypes.POINTER(SidAndAttributes)).contents.sid, ctypes.byref(text)))
    try:
        return text.value
    finally:
        kernel.LocalFree(text)


@contextmanager
def _normal_shell_token():
    """Get the same account's actual limited token, with impersonation rights."""
    advapi, kernel = _apis()
    user = ctypes.WinDLL("user32", use_last_error=True)
    user.GetShellWindow.restype = wintypes.HWND
    user.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
    user.GetWindowThreadProcessId.restype = wintypes.DWORD
    kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel.OpenProcess.restype = wintypes.HANDLE
    current, shell_token, duplicate = (wintypes.HANDLE() for _ in range(3))
    try:
        _require(advapi.OpenProcessToken(kernel.GetCurrentProcess(), 10, ctypes.byref(current)))
        if _token_number(current, 18) != 2:
            pytest.skip("Native elevated/filtered UAC pair required")
        shell = user.GetShellWindow()
        if not shell:
            pytest.skip("Same-account normal desktop shell required")
        pid = wintypes.DWORD()
        _require(user.GetWindowThreadProcessId(shell, ctypes.byref(pid)))
        process = kernel.OpenProcess(0x1000, False, pid.value)
        _require(process)
        try:
            _require(advapi.OpenProcessToken(process, 10, ctypes.byref(shell_token)))
        finally:
            kernel.CloseHandle(process)
        if _token_user(shell_token) != _token_user(current) or _token_number(shell_token, 18) != 3:
            pytest.skip("Desktop shell must be the same account's limited UAC token")
        assert _token_number(shell_token, 8) == 1  # TokenPrimary
        # A linked UAC handle can be identification-only. Use the real shell
        # token at SecurityImpersonation, and verify actual thread activation.
        _require(advapi.DuplicateToken(shell_token, 2, ctypes.byref(duplicate)))
        assert _token_number(duplicate, 18) == 3
        assert _token_number(duplicate, 9) == 2
        yield duplicate
    finally:
        for handle in (duplicate, shell_token, current):
            if handle.value:
                kernel.CloseHandle(handle)


@contextmanager
def _impersonate(token):
    advapi, kernel = _apis()
    advapi.ImpersonateLoggedOnUser.argtypes = [wintypes.HANDLE]
    advapi.ImpersonateLoggedOnUser.restype = wintypes.BOOL
    advapi.RevertToSelf.argtypes = []
    advapi.RevertToSelf.restype = wintypes.BOOL
    kernel.GetCurrentThread.restype = wintypes.HANDLE
    advapi.OpenThreadToken.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.BOOL,
                                      ctypes.POINTER(wintypes.HANDLE)]
    advapi.OpenThreadToken.restype = wintypes.BOOL
    thread = wintypes.HANDLE()
    _require(advapi.ImpersonateLoggedOnUser(token))
    try:
        _require(advapi.OpenThreadToken(kernel.GetCurrentThread(), 8, True, ctypes.byref(thread)))
        assert _token_number(thread, 18) == 3
        assert _token_number(thread, 9) == 2
        yield
    finally:
        if thread.value:
            kernel.CloseHandle(thread)
        _require(advapi.RevertToSelf())


def _inherited_modify_directory(tmp_path):
    path = tmp_path / "inherited-modify"
    path.mkdir()
    sid = tokens._windows_process_user_sid()
    # Ordinary Modify access: children do not inherit WRITE_DAC/WRITE_OWNER.
    # Full control on this parent would conceal the real temporary-file bug.
    _set_sddl(path, f"O:{sid}D:P(A;OICI;0x1301bf;;;{sid})")
    return path


@pytest.mark.parametrize("directory", [False, True])
def test_normal_user_protects_new_inherited_modify_path(tmp_path, directory):
    parent = _inherited_modify_directory(tmp_path)
    path = parent / "new-private"
    with _normal_shell_token() as normal, _impersonate(normal):
        path.mkdir() if directory else path.write_bytes(b"dummy")
        assert _access(path, normal, 0x40000)  # Implicit owner WRITE_DAC
        assert not _access(path, normal, 0x80000)  # No WRITE_OWNER yet
        tokens._protect_token_path(path, directory=directory)
        sid = tokens._windows_process_user_sid()
        assert _sddl(path) == f"O:{sid}D:P(A;{'OICI' if directory else ''};FA;;;{sid})"
        assert _access(path, normal, 0xc0003)
        if not directory:
            assert path.read_bytes() == b"dummy"


def test_normal_refresh_attempt_and_rotation_keep_lock_identity(tmp_path, monkeypatch):
    parent = tmp_path
    path = parent / "dummy-token.json"
    monkeypatch.setattr(tokens, "TOKEN_CACHE_PATH", path)
    with _normal_shell_token() as normal:
        tokens.write_token_payload_atomic({"access_token": "dummy-old", "refresh_token": "dummy-refresh"})
        # Existing protected cache/lock survive the parent ACL change. Every
        # subsequent candidate must begin with the real inherited Modify ACL.
        sid = tokens._windows_process_user_sid()
        _set_sddl(parent, f"O:{sid}D:P(A;OICI;0x1301bf;;;{sid})")
        lock = path.with_name(path.name + ".lock")
        lock_id = lock.stat().st_ino
        original_protect = tokens._protect_token_path
        unprivileged_candidates = []
        def check_candidate(candidate, **kwargs):
            if candidate.name.endswith(".tmp"):
                assert _access(candidate, normal, 0x40000)
                assert not _access(candidate, normal, 0x80000)
                assert candidate.read_bytes() == b""
                unprivileged_candidates.append(candidate)
            return original_protect(candidate, **kwargs)
        with _impersonate(normal), monkeypatch.context() as patch:
            patch.setattr(tokens, "_protect_token_path", check_candidate)
            with tokens.locked_token_cache():
                cached = tokens.load_token_payload(strict=True)
                marked = tokens.write_refresh_attempt(cached, attempt_id="fixture", status="IN_PROGRESS")
                assert tokens.load_token_payload(strict=True) == marked
                saved = tokens.save_token_payload({"access_token": "dummy-new", "refresh_token": "dummy-rotated"})
                assert tokens.load_token_payload(strict=True) == saved
                assert "refresh_attempt" not in saved
            assert len(unprivileged_candidates) == 2
        # Check both directions across elevated and real normal writes.
        tokens.write_token_payload_atomic({"fixture_generation": 3})
        with _impersonate(normal):
            assert tokens.load_token_payload(strict=True) == {"fixture_generation": 3}
            tokens.write_token_payload_atomic({"fixture_generation": 4})
        assert tokens.load_token_payload(strict=True) == {"fixture_generation": 4}
        assert lock.stat().st_ino == lock_id
        sid = tokens._windows_process_user_sid()
        for protected in (path, lock):
            assert _sddl(protected) == f"O:{sid}D:P(A;;FA;;;{sid})"
        assert not list(parent.glob("*.tmp"))


def test_failed_owner_update_never_publishes_candidate(tmp_path, monkeypatch):
    path = tmp_path / "dummy-token.json"
    monkeypatch.setattr(tokens, "TOKEN_CACHE_PATH", path)
    tokens.write_token_payload_atomic({"fixture_generation": 1})
    before = path.read_bytes()
    original_dll = ctypes.WinDLL
    owner_failures = []
    class DeniedOwnerUpdate:
        def __init__(self, native):
            self.native = native
        def __call__(self, candidate, flags, descriptor):
            if str(candidate).endswith(".tmp") and flags == 1:
                candidate = type(path)(candidate)
                assert candidate.read_bytes() == b""
                sid = tokens._windows_process_user_sid()
                assert f"D:P(A;;FA;;;{sid})" in _sddl(candidate)
                owner_failures.append(candidate)
                ctypes.set_last_error(5)
                return 0
            return self.native(candidate, flags, descriptor)
    class AdvapiProxy:
        def __init__(self, native):
            self.native = native
            self.SetFileSecurityW = DeniedOwnerUpdate(native.SetFileSecurityW)
        def __getattr__(self, name):
            return getattr(self.native, name)
    def injected_dll(name, **kwargs):
        native = original_dll(name, **kwargs)
        return AdvapiProxy(native) if name == "advapi32" else native
    monkeypatch.setattr(ctypes, "WinDLL", injected_dll)
    with pytest.raises(PermissionError) as caught:
        tokens.save_token_payload({"access_token": "dummy-candidate"})
    assert caught.value.winerror == 5
    assert caught.value.schwab_candidate_token_payload["access_token"] == "dummy-candidate"
    assert len(owner_failures) == 1
    assert path.read_bytes() == before
    assert not list(tmp_path.glob("*.tmp"))


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
