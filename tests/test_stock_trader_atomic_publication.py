import ctypes
import json
import os
from pathlib import Path
import threading

import pytest

from ml.stock_trader import publication


@pytest.mark.parametrize("winerror", [5, 32, 33])
def test_atomic_json_retries_windows_reader_conflicts(tmp_path, monkeypatch, winerror):
    target = tmp_path / "session-status.json"
    target.write_text('{"status": "OLD"}')
    replace = Path.replace
    attempts = []
    sleeps = []

    def temporarily_locked(source, destination):
        attempts.append(source)
        if len(attempts) < 3:
            assert json.loads(target.read_text()) == {"status": "OLD"}
            assert json.loads(source.read_text()) == {"status": "RUNNING"}
            error = PermissionError("reader holds destination")
            error.winerror = winerror
            raise error
        return replace(source, destination)

    monkeypatch.setattr(Path, "replace", temporarily_locked)
    monkeypatch.setattr(publication.time, "sleep", sleeps.append)
    publication._write_json_atomic(target, {"status": "RUNNING"})
    assert json.loads(target.read_text()) == {"status": "RUNNING"}
    assert len(attempts) == 3
    assert sleeps == [0.05, 0.1]
    assert not target.with_suffix(".json.tmp").exists()


@pytest.mark.parametrize("winerror,expected_attempts", [(5, 6), (None, 1)])
def test_atomic_json_preserves_old_file_when_permission_error_persists(
    tmp_path, monkeypatch, winerror, expected_attempts
):
    target = tmp_path / "session-status.json"
    target.write_text('{"status": "OLD"}')
    attempts = []
    sleeps = []
    error = PermissionError("replacement denied")
    error.winerror = winerror

    def denied(source, destination):
        attempts.append(source)
        raise error

    monkeypatch.setattr(Path, "replace", denied)
    monkeypatch.setattr(publication.time, "sleep", sleeps.append)
    with pytest.raises(PermissionError) as raised:
        publication._write_json_atomic(target, {"status": "RUNNING"})
    assert raised.value is error
    assert len(attempts) == expected_attempts
    assert sum(sleeps) <= 1.55
    assert json.loads(target.read_text()) == {"status": "OLD"}
    assert json.loads(target.with_suffix(".json.tmp").read_text()) == {"status": "RUNNING"}


@pytest.mark.skipif(os.name != "nt", reason="Windows file sharing behavior")
def test_atomic_json_recovers_from_real_windows_reader_handle(tmp_path):
    from ctypes import wintypes

    target = tmp_path / "session-status.json"
    target.write_text('{"status": "OLD"}')
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                                   ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
    kernel32.CreateFileW.restype = wintypes.HANDLE
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL
    handle = kernel32.CreateFileW(str(target), 0x80000000, 1, None, 3, 0x80, None)
    assert handle != wintypes.HANDLE(-1).value
    release = threading.Timer(0.15, kernel32.CloseHandle, args=(handle,))
    release.start()
    try:
        publication._write_json_atomic(target, {"status": "RUNNING"})
    finally:
        release.join()
    assert json.loads(target.read_text()) == {"status": "RUNNING"}
