"""Windows worker containment for Scout's iteration controller.

Only a newly created child is controlled. No PID discovery, adoption, breakaway,
crash respawn, or unrelated-process termination is available here. The job is
attached by CreateProcess itself (Windows 10+) before any child code can run.
Its sole, noninherited owner handle kills the complete tree if the controller
dies. Failure to establish this guarantee fails startup closed.

API contracts: ``wait`` waits for the root process; ``job_empty`` additionally
accounts for every descendant. ``terminate`` and ``close`` require both to have
exited within a finite timeout, otherwise they raise and prohibit replacement.
"""
from __future__ import annotations

import ctypes
from ctypes import wintypes as w
import math
import os
from pathlib import Path
import subprocess
import time
import uuid


class ContainmentError(RuntimeError):
    """The controller cannot establish or prove sole worker containment."""


class _BasicLimits(ctypes.Structure):
    _fields_ = [("PerProcessUserTimeLimit", ctypes.c_longlong),
                ("PerJobUserTimeLimit", ctypes.c_longlong), ("LimitFlags", w.DWORD),
                ("MinimumWorkingSetSize", ctypes.c_size_t),
                ("MaximumWorkingSetSize", ctypes.c_size_t), ("ActiveProcessLimit", w.DWORD),
                ("Affinity", ctypes.c_size_t), ("PriorityClass", w.DWORD),
                ("SchedulingClass", w.DWORD)]


class _IOCounters(ctypes.Structure):
    _fields_ = [(name, ctypes.c_ulonglong) for name in (
        "ReadOperationCount", "WriteOperationCount", "OtherOperationCount",
        "ReadTransferCount", "WriteTransferCount", "OtherTransferCount")]


class _ExtendedLimits(ctypes.Structure):
    _fields_ = [("BasicLimitInformation", _BasicLimits), ("IoInfo", _IOCounters),
                ("ProcessMemoryLimit", ctypes.c_size_t), ("JobMemoryLimit", ctypes.c_size_t),
                ("PeakProcessMemoryUsed", ctypes.c_size_t), ("PeakJobMemoryUsed", ctypes.c_size_t)]


class _Accounting(ctypes.Structure):
    _fields_ = [("TotalUserTime", ctypes.c_longlong), ("TotalKernelTime", ctypes.c_longlong),
                ("ThisPeriodTotalUserTime", ctypes.c_longlong),
                ("ThisPeriodTotalKernelTime", ctypes.c_longlong),
                ("TotalPageFaultCount", w.DWORD), ("TotalProcesses", w.DWORD),
                ("ActiveProcesses", w.DWORD), ("TotalTerminatedProcesses", w.DWORD)]


class _StartupInfo(ctypes.Structure):
    _fields_ = [("cb", w.DWORD), ("lpReserved", w.LPWSTR), ("lpDesktop", w.LPWSTR),
                ("lpTitle", w.LPWSTR), ("dwX", w.DWORD), ("dwY", w.DWORD),
                ("dwXSize", w.DWORD), ("dwYSize", w.DWORD), ("dwXCountChars", w.DWORD),
                ("dwYCountChars", w.DWORD), ("dwFillAttribute", w.DWORD),
                ("dwFlags", w.DWORD), ("wShowWindow", w.WORD), ("cbReserved2", w.WORD),
                ("lpReserved2", ctypes.POINTER(w.BYTE)), ("hStdInput", w.HANDLE),
                ("hStdOutput", w.HANDLE), ("hStdError", w.HANDLE)]


class _StartupInfoEx(ctypes.Structure):
    _fields_ = [("StartupInfo", _StartupInfo), ("lpAttributeList", ctypes.c_void_p)]


class _ProcessInfo(ctypes.Structure):
    _fields_ = [("hProcess", w.HANDLE), ("hThread", w.HANDLE),
                ("dwProcessId", w.DWORD), ("dwThreadId", w.DWORD)]


def _api():
    if os.name != "nt":
        raise ContainmentError("Scout worker containment requires Windows 10 or newer")
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    prototypes = {
        "CreateJobObjectW": ([ctypes.c_void_p, w.LPCWSTR], w.HANDLE),
        "OpenJobObjectW": ([w.DWORD, w.BOOL, w.LPCWSTR], w.HANDLE),
        "OpenProcess": ([w.DWORD, w.BOOL, w.DWORD], w.HANDLE),
        "IsProcessInJob": ([w.HANDLE, w.HANDLE, ctypes.POINTER(w.BOOL)], w.BOOL),
        "SetInformationJobObject": ([w.HANDLE, ctypes.c_int, ctypes.c_void_p, w.DWORD], w.BOOL),
        "QueryInformationJobObject": ([w.HANDLE, ctypes.c_int, ctypes.c_void_p, w.DWORD,
                                       ctypes.POINTER(w.DWORD)], w.BOOL),
        "InitializeProcThreadAttributeList": ([ctypes.c_void_p, w.DWORD, w.DWORD,
                                                ctypes.POINTER(ctypes.c_size_t)], w.BOOL),
        "UpdateProcThreadAttribute": ([ctypes.c_void_p, w.DWORD, ctypes.c_size_t,
                                       ctypes.c_void_p, ctypes.c_size_t,
                                       ctypes.c_void_p, ctypes.c_void_p], w.BOOL),
        "DeleteProcThreadAttributeList": ([ctypes.c_void_p], None),
        "CreateProcessW": ([w.LPCWSTR, w.LPWSTR, ctypes.c_void_p, ctypes.c_void_p,
                            w.BOOL, w.DWORD, ctypes.c_void_p, w.LPCWSTR,
                            ctypes.POINTER(_StartupInfoEx), ctypes.POINTER(_ProcessInfo)], w.BOOL),
        "GetProcessTimes": ([w.HANDLE, ctypes.POINTER(w.FILETIME), ctypes.POINTER(w.FILETIME),
                             ctypes.POINTER(w.FILETIME), ctypes.POINTER(w.FILETIME)], w.BOOL),
        "ResumeThread": ([w.HANDLE], w.DWORD),
        "WaitForSingleObject": ([w.HANDLE, w.DWORD], w.DWORD),
        "GetExitCodeProcess": ([w.HANDLE, ctypes.POINTER(w.DWORD)], w.BOOL),
        "TerminateJobObject": ([w.HANDLE, w.UINT], w.BOOL),
        "CloseHandle": ([w.HANDLE], w.BOOL),
    }
    for name, (argtypes, restype) in prototypes.items():
        function = getattr(kernel, name)
        function.argtypes, function.restype = argtypes, restype
    return kernel


def _checked(success, operation):
    if not success:
        error = ctypes.WinError(ctypes.get_last_error())
        raise ContainmentError(f"{operation} failed: {error}") from error


def _timeout(value):
    value = float(value)
    if not math.isfinite(value) or not 0 <= value <= 300:
        raise ValueError("A finite process timeout between 0 and 300 seconds is required")
    return value


def inspect_job(job_name):
    """Return prior job's active process count, or None if no such job exists.

    A new controller must refuse activation while this returns a positive count.
    A killed controller's main PID disappearing alone is not sufficient evidence.
    Opening for QUERY neither stops workers nor adopts their trading authority.
    """
    if not isinstance(job_name, str) or not job_name.startswith("Local\\DucketzScoutIteration-"):
        raise ValueError("Explicit Scout generation job name required")
    kernel = _api()
    job = kernel.OpenJobObjectW(0x0004, False, job_name)  # JOB_OBJECT_QUERY
    if not job:
        if ctypes.get_last_error() == 2:  # ERROR_FILE_NOT_FOUND
            return None
        _checked(False, "Open previous worker job")
    try:
        accounting = _Accounting()
        _checked(kernel.QueryInformationJobObject(job, 1, ctypes.byref(accounting),
            ctypes.sizeof(accounting), None), "Query previous worker job")
        return int(accounting.ActiveProcesses)
    finally:
        kernel.CloseHandle(job)


def process_in_job(pid, job_name):
    """Verify native generation membership, including Python venv helpers.

    This checks a native job rather than trusting parent PID ancestry or a PID
    alone. A missing process or closed job returns False; other query failures
    raise. The caller separately fences its logical generation and controller
    identity, and must not retain this temporary job handle.
    """
    if not isinstance(job_name, str) or not job_name.startswith("Local\\DucketzScoutIteration-"):
        raise ValueError("Explicit Scout generation job name required")
    if isinstance(pid, bool) or not isinstance(pid, int) or pid <= 0:
        return False
    kernel = _api()
    job = kernel.OpenJobObjectW(0x0004, False, job_name)
    if not job:
        if ctypes.get_last_error() == 2:
            return False
        _checked(False, "Open generation job")
    process = None
    try:
        process = kernel.OpenProcess(0x1000, False, pid)  # QUERY_LIMITED_INFORMATION
        if not process:
            if ctypes.get_last_error() == 87:  # Missing PID: ERROR_INVALID_PARAMETER
                return False
            _checked(False, "Open generation process")
        member = w.BOOL()
        _checked(kernel.IsProcessInJob(process, job, ctypes.byref(member)), "Verify generation process membership")
        return bool(member.value)
    finally:
        if process:
            kernel.CloseHandle(process)
        kernel.CloseHandle(job)


class ManagedWorker:
    """One atomically contained, optionally suspended Windows process tree.

    ``command[0]`` must name an absolute executable, never a shell fragment.
    Standard input is NUL. Output goes to ``log_path`` (append), or NUL. Only
    these two explicit handles are inherited; the job handle is never inherited.
    Passing ``start_suspended=True`` allows generation fencing to be installed
    and stop authority to be checked immediately before ``resume()``.
    """

    def __init__(self, command, *, cwd, env=None, log_path=None, start_suspended=False):
        self._kernel = _api()
        self._job = self._process = self._thread = None
        self._closed_empty = False
        self._resumed = False
        self.returncode = None
        self.job_name = "Local\\DucketzScoutIteration-" + uuid.uuid4().hex
        self.command = tuple(os.fspath(part) for part in command)
        if not self.command or not Path(self.command[0]).is_absolute():
            raise ValueError("Managed worker requires an absolute executable path")
        if any("\0" in part for part in self.command):
            raise ValueError("NUL in worker command")
        working_directory = str(Path(cwd).resolve(strict=True))
        environment = dict(os.environ if env is None else env)
        if any(not isinstance(k, str) or not isinstance(v, str) or not k or "=" in k
               or "\0" in k or "\0" in v for k, v in environment.items()):
            raise ValueError("Invalid child environment")
        environment_buffer = ctypes.create_unicode_buffer(
            "\0".join(f"{k}={v}" for k, v in sorted(environment.items(), key=lambda x: x[0].upper())) + "\0\0")
        attribute_buffer = None
        attributes_initialized = False
        handles = []
        try:
            ctypes.set_last_error(0)
            job = self._kernel.CreateJobObjectW(None, self.job_name)
            if job and ctypes.get_last_error() == 183:  # Never adopt an existing job.
                self._kernel.CloseHandle(job)
                raise ContainmentError("Generation job name already exists")
            self._job = job
            _checked(self._job, "CreateJobObjectW")
            limits = _ExtendedLimits()
            limits.BasicLimitInformation.LimitFlags = 0x2000  # KILL_ON_JOB_CLOSE; no breakaway
            _checked(self._kernel.SetInformationJobObject(
                self._job, 9, ctypes.byref(limits), ctypes.sizeof(limits)), "SetInformationJobObject")
            size = ctypes.c_size_t()
            self._kernel.InitializeProcThreadAttributeList(None, 2, 0, ctypes.byref(size))
            if not size.value:
                raise ContainmentError("Process attribute list is unavailable")
            attribute_buffer = ctypes.create_string_buffer(size.value)
            _checked(self._kernel.InitializeProcThreadAttributeList(
                attribute_buffer, 2, 0, ctypes.byref(size)), "InitializeProcThreadAttributeList")
            attributes_initialized = True
            jobs = (w.HANDLE * 1)(self._job)
            # ProcThreadAttributeValue(JobList=13, thread=False, input=True, additive=False).
            _checked(self._kernel.UpdateProcThreadAttribute(attribute_buffer, 0, 0x0002000D,
                ctypes.byref(jobs), ctypes.sizeof(jobs), None, None), "Set atomic process job list")
            import msvcrt
            input_file = open(os.devnull, "rb")
            handles.append(input_file)
            output_file = open(log_path if log_path is not None else os.devnull, "ab", buffering=0)
            handles.append(output_file)
            for stream in handles:
                os.set_handle_inheritable(msvcrt.get_osfhandle(stream.fileno()), True)
            native_input, native_output = (msvcrt.get_osfhandle(stream.fileno()) for stream in handles)
            inherited = (w.HANDLE * 2)(native_input, native_output)
            _checked(self._kernel.UpdateProcThreadAttribute(attribute_buffer, 0, 0x00020002,
                ctypes.byref(inherited), ctypes.sizeof(inherited), None, None), "Set restricted process handle list")
            startup = _StartupInfoEx()
            startup.StartupInfo.cb = ctypes.sizeof(startup)
            startup.StartupInfo.dwFlags = 0x100  # STARTF_USESTDHANDLES
            startup.StartupInfo.hStdInput = native_input
            startup.StartupInfo.hStdOutput = startup.StartupInfo.hStdError = native_output
            startup.lpAttributeList = ctypes.addressof(attribute_buffer)
            process = _ProcessInfo()
            command_buffer = ctypes.create_unicode_buffer(subprocess.list2cmdline(self.command))
            # SUSPENDED | UNICODE_ENVIRONMENT | EXTENDED_STARTUPINFO_PRESENT | NO_WINDOW
            flags = 0x00000004 | 0x00000400 | 0x00080000 | 0x08000000
            _checked(self._kernel.CreateProcessW(self.command[0], command_buffer, None, None,
                True, flags, environment_buffer, working_directory, ctypes.byref(startup),
                ctypes.byref(process)), "Create contained process")
            self._process, self._thread, self.pid = process.hProcess, process.hThread, int(process.dwProcessId)
            created, exited, kernel_time, user_time = (w.FILETIME() for _ in range(4))
            _checked(self._kernel.GetProcessTimes(self._process, ctypes.byref(created),
                ctypes.byref(exited), ctypes.byref(kernel_time), ctypes.byref(user_time)), "GetProcessTimes")
            self.create_ticks = (created.dwHighDateTime << 32) + created.dwLowDateTime
            # The controller's existing PID/birth gates use psutil. Its native
            # FILETIME conversion rounds differently from Python arithmetic;
            # preserve that exact representation rather than an epsilon match.
            import psutil
            self.create_time = psutil.Process(self.pid).create_time()
            if not start_suspended:
                self.resume()
        except BaseException:
            # The child is contained before CreateProcess returns. Closing the
            # only job handle kills it even if a later setup operation fails.
            try:
                if self._process:
                    self.terminate(timeout=10)
            finally:
                self._release_handles()
            raise
        finally:
            for stream in handles:
                stream.close()
            if attributes_initialized:
                self._kernel.DeleteProcThreadAttributeList(attribute_buffer)

    def resume(self):
        if self._resumed or not self._thread or self.poll() is not None:
            raise ContainmentError("Worker can only be resumed once while suspended and alive")
        previous = self._kernel.ResumeThread(self._thread)
        if previous != 1:
            # Never permit a process with an unexpected thread-suspend count.
            self.terminate()
            raise ContainmentError(f"Unexpected worker suspend count: {previous}")
        self._resumed = True
        _checked(self._kernel.CloseHandle(self._thread), "Close primary thread handle")
        self._thread = None

    def poll(self):
        if self.returncode is not None:
            return self.returncode
        if not self._process:
            raise ContainmentError("Worker process handle is closed without exit evidence")
        result = self._kernel.WaitForSingleObject(self._process, 0)
        if result == 0x102:  # WAIT_TIMEOUT
            return None
        _checked(result == 0, "Poll worker process")
        code = w.DWORD()
        _checked(self._kernel.GetExitCodeProcess(self._process, ctypes.byref(code)), "GetExitCodeProcess")
        self.returncode = int(code.value)
        return self.returncode

    def wait(self, timeout=10):
        timeout = _timeout(timeout)
        if self.poll() is not None:
            return self.returncode
        result = self._kernel.WaitForSingleObject(self._process, int(timeout * 1000))
        if result == 0x102:
            raise subprocess.TimeoutExpired(self.command, timeout)
        _checked(result == 0, "Wait for worker process")
        return self.poll()

    def job_empty(self):
        if self._closed_empty:
            return True
        if not self._job:
            raise ContainmentError("Job is closed without proof that all owned workers exited")
        accounting = _Accounting()
        _checked(self._kernel.QueryInformationJobObject(self._job, 1, ctypes.byref(accounting),
            ctypes.sizeof(accounting), None), "Query process job")
        return accounting.ActiveProcesses == 0

    def terminate(self, timeout=10):
        """Stop only this job and prove its complete tree exited, or raise."""
        timeout = _timeout(timeout)
        deadline = time.monotonic() + timeout
        if self.job_empty():
            return self.wait(timeout=max(0, deadline - time.monotonic()))
        _checked(self._kernel.TerminateJobObject(self._job, 70), "Terminate owned worker job")
        while not self.job_empty():
            if time.monotonic() >= deadline:
                raise ContainmentError("Owned worker tree did not exit before containment timeout")
            time.sleep(min(0.01, max(0, deadline - time.monotonic())))
        return self.wait(timeout=max(0, deadline - time.monotonic()))

    def await_empty(self, timeout=10):
        """Boundedly await natural descendant exit; never kill or infer drain."""
        deadline = time.monotonic() + _timeout(timeout)
        while not self.job_empty():
            if time.monotonic() >= deadline:
                raise ContainmentError("Owned child workers remain after bounded drain")
            time.sleep(min(0.01, max(0, deadline - time.monotonic())))
        return True

    def close(self, timeout=10):
        """Release native handles only after bounded complete-tree shutdown."""
        if self._closed_empty:
            return
        try:
            self.terminate(timeout=timeout)
            self._closed_empty = True
        finally:
            self._release_handles()

    def _release_handles(self):
        # Job first: every abnormal cleanup path revokes the entire child tree.
        for name in ("_job", "_thread", "_process"):
            handle = getattr(self, name, None)
            if handle:
                self._kernel.CloseHandle(handle)
                setattr(self, name, None)

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()

    def __del__(self):
        self._release_handles()
