"""Cheap identity checks after complete local immutable-file validation."""
from pathlib import Path
import os


def stat_witness(info):
    return [info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns]


def file_integrity_witness(path):
    """Include Windows ChangeTime, not just its creation-time ``st_ctime``.

    Restoring LastWriteTime after a same-length edit changes native ChangeTime.
    Read it from the open file used for identity and fail closed if unavailable.
    This is a change witness, not a substitute for first-use content validation.
    """
    path = Path(path)
    with path.open("rb") as stream:
        info = os.fstat(stream.fileno())
        change = _windows_change_time(stream.fileno()) if os.name == "nt" else info.st_ctime_ns
        current = path.stat()
        if (info.st_dev, info.st_ino) != (current.st_dev, current.st_ino):
            raise ValueError("Retained immutable evidence was replaced during validation")
    # Path.stat preserves existing envelopes. Some Python/Windows versions
    # expose different ctime semantics for fstat; only compare its file ID.
    return {"schema": "scout-file-witness-v2", "metadata": stat_witness(current),
            "change_time": change}


def _windows_change_time(fd):
    import ctypes
    from ctypes import wintypes
    import msvcrt

    class FileBasicInfo(ctypes.Structure):
        _fields_ = [(name, ctypes.c_longlong) for name in
                    ("creation", "access", "write", "change")] + [("attributes", wintypes.DWORD)]

    query = ctypes.WinDLL("kernel32", use_last_error=True).GetFileInformationByHandleEx
    query.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
    query.restype = wintypes.BOOL
    value = FileBasicInfo()
    if not query(msvcrt.get_osfhandle(fd), 0, ctypes.byref(value), ctypes.sizeof(value)):
        raise ctypes.WinError(ctypes.get_last_error())
    return value.change
