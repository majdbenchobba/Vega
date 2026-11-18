"""The bounded Windows process API used by the Mini App tunnel owner.

Children enter a private kill-on-close job *during* CreateProcessW. The host
never joins that job, and its job handle is never inherited. Windows 10+ is
required for PROC_THREAD_ATTRIBUTE_JOB_LIST; there is no unowned fallback.

The supported options are exactly the tunnel's Popen options. An explicit env
mapping is required. arguments[0] is an executable path, including its extension;
PATH lookup and raw command strings are not supported. Call release() after exit,
and close stdout when its reader has finished; release() leaves buffered output
available to that reader. terminate() and kill() both use TerminateProcess, as
Popen does on Windows; release() closes the job to kill remaining descendants.

Native definitions and contracts:
https://learn.microsoft.com/windows/win32/api/processthreadsapi/nf-processthreadsapi-updateprocthreadattribute
https://learn.microsoft.com/windows/win32/api/processthreadsapi/nf-processthreadsapi-createprocessw
https://learn.microsoft.com/windows/win32/api/winnt/ns-winnt-jobobject_extended_limit_information
https://github.com/microsoft/win32metadata/blob/main/generation/WinSDK/RecompiledIdlHeaders/um/WinBase.h
"""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from contextlib import ExitStack
from functools import cmp_to_key
import io
import math
import os
import subprocess
import threading
import time


_CREATE_NO_WINDOW = 0x08000000
_CREATE_UNICODE_ENVIRONMENT = 0x00000400
_EXTENDED_STARTUPINFO_PRESENT = 0x00080000
_PROC_THREAD_ATTRIBUTE_HANDLE_LIST = 0x00020002
_PROC_THREAD_ATTRIBUTE_JOB_LIST = 0x0002000D
_JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000
_JOB_OBJECT_EXTENDED_LIMIT_INFORMATION = 9
_WAIT_OBJECT_0 = 0
_WAIT_TIMEOUT = 258
_INFINITE = 0xFFFFFFFF
_STARTUP_CLEANUP_TIMEOUT_MS = 5000


if os.name == "nt":
    import ctypes
    from ctypes import wintypes
    import msvcrt

    class _SECURITY_ATTRIBUTES(ctypes.Structure):
        _fields_ = [
            ("nLength", wintypes.DWORD), ("lpSecurityDescriptor", wintypes.LPVOID),
            ("bInheritHandle", wintypes.BOOL),
        ]

    class _STARTUPINFOW(ctypes.Structure):
        _fields_ = [
            ("cb", wintypes.DWORD), ("lpReserved", wintypes.LPWSTR),
            ("lpDesktop", wintypes.LPWSTR), ("lpTitle", wintypes.LPWSTR),
            ("dwX", wintypes.DWORD), ("dwY", wintypes.DWORD),
            ("dwXSize", wintypes.DWORD), ("dwYSize", wintypes.DWORD),
            ("dwXCountChars", wintypes.DWORD), ("dwYCountChars", wintypes.DWORD),
            ("dwFillAttribute", wintypes.DWORD), ("dwFlags", wintypes.DWORD),
            ("wShowWindow", wintypes.WORD), ("cbReserved2", wintypes.WORD),
            ("lpReserved2", ctypes.POINTER(wintypes.BYTE)),
            ("hStdInput", wintypes.HANDLE), ("hStdOutput", wintypes.HANDLE),
            ("hStdError", wintypes.HANDLE),
        ]

    class _STARTUPINFOEXW(ctypes.Structure):
        _fields_ = [("StartupInfo", _STARTUPINFOW), ("lpAttributeList", wintypes.LPVOID)]

    class _PROCESS_INFORMATION(ctypes.Structure):
        _fields_ = [
            ("hProcess", wintypes.HANDLE), ("hThread", wintypes.HANDLE),
            ("dwProcessId", wintypes.DWORD), ("dwThreadId", wintypes.DWORD),
        ]

    class _JOBOBJECT_BASIC_LIMIT_INFORMATION(ctypes.Structure):
        _fields_ = [
            ("PerProcessUserTimeLimit", ctypes.c_longlong),
            ("PerJobUserTimeLimit", ctypes.c_longlong),
            ("LimitFlags", wintypes.DWORD),
            ("MinimumWorkingSetSize", ctypes.c_size_t),
            ("MaximumWorkingSetSize", ctypes.c_size_t),
            ("ActiveProcessLimit", wintypes.DWORD),
            ("Affinity", ctypes.c_size_t),
            ("PriorityClass", wintypes.DWORD), ("SchedulingClass", wintypes.DWORD),
        ]

    class _IO_COUNTERS(ctypes.Structure):
        _fields_ = [(name, ctypes.c_ulonglong) for name in (
            "ReadOperationCount", "WriteOperationCount", "OtherOperationCount",
            "ReadTransferCount", "WriteTransferCount", "OtherTransferCount",
        )]

    class _JOBOBJECT_EXTENDED_LIMITS(ctypes.Structure):
        _fields_ = [
            ("BasicLimitInformation", _JOBOBJECT_BASIC_LIMIT_INFORMATION),
            ("IoInfo", _IO_COUNTERS),
            ("ProcessMemoryLimit", ctypes.c_size_t), ("JobMemoryLimit", ctypes.c_size_t),
            ("PeakProcessMemoryUsed", ctypes.c_size_t), ("PeakJobMemoryUsed", ctypes.c_size_t),
        ]

    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

    def _bind(name, result, *arguments):
        function = getattr(_kernel32, name)
        function.restype = result
        function.argtypes = list(arguments)

    _PVOID = wintypes.LPVOID
    _PHANDLE = ctypes.POINTER(wintypes.HANDLE)
    _PSIZE = ctypes.POINTER(ctypes.c_size_t)
    _PSA = ctypes.POINTER(_SECURITY_ATTRIBUTES)
    _bind("CreateJobObjectW", wintypes.HANDLE, _PSA, wintypes.LPCWSTR)
    _bind("SetInformationJobObject", wintypes.BOOL, wintypes.HANDLE, ctypes.c_int, _PVOID, wintypes.DWORD)
    _bind("CreatePipe", wintypes.BOOL, _PHANDLE, _PHANDLE, _PSA, wintypes.DWORD)
    _bind("SetHandleInformation", wintypes.BOOL, wintypes.HANDLE, wintypes.DWORD, wintypes.DWORD)
    _bind("CreateFileW", wintypes.HANDLE, wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
          _PSA, wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE)
    _bind("InitializeProcThreadAttributeList", wintypes.BOOL, _PVOID, wintypes.DWORD, wintypes.DWORD, _PSIZE)
    _bind("UpdateProcThreadAttribute", wintypes.BOOL, _PVOID, wintypes.DWORD, ctypes.c_size_t,
          _PVOID, ctypes.c_size_t, _PVOID, _PSIZE)
    _bind("DeleteProcThreadAttributeList", None, _PVOID)
    _bind("CreateProcessW", wintypes.BOOL, wintypes.LPCWSTR, wintypes.LPWSTR, _PSA, _PSA,
          wintypes.BOOL, wintypes.DWORD, _PVOID, wintypes.LPCWSTR,
          ctypes.POINTER(_STARTUPINFOEXW), ctypes.POINTER(_PROCESS_INFORMATION))
    _bind("CloseHandle", wintypes.BOOL, wintypes.HANDLE)
    _bind("WaitForSingleObject", wintypes.DWORD, wintypes.HANDLE, wintypes.DWORD)
    _bind("GetExitCodeProcess", wintypes.BOOL, wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD))
    _bind("TerminateProcess", wintypes.BOOL, wintypes.HANDLE, wintypes.UINT)
    _bind("GetCurrentProcess", wintypes.HANDLE)
    _bind("DuplicateHandle", wintypes.BOOL, wintypes.HANDLE, wintypes.HANDLE,
          wintypes.HANDLE, _PHANDLE, wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
    _bind("CompareStringOrdinal", ctypes.c_int, wintypes.LPCWSTR, ctypes.c_int,
          wintypes.LPCWSTR, ctypes.c_int, wintypes.BOOL)


def _check(result):
    if not result:
        raise ctypes.WinError(ctypes.get_last_error())
    return result


class _Handle:
    """One native handle, shared by startup cleanup and the returned owner."""
    def __init__(self):
        self.raw = wintypes.HANDLE()

    @property
    def value(self):
        return self.raw.value

    def close(self):
        if self.value is not None:
            _check(_kernel32.CloseHandle(self.value))
            self.raw.value = None


def _owned_handle(stack):
    handle = _Handle()
    stack.callback(handle.close)
    return handle


def _close_thread(info):
    if info.hThread:
        _check(_kernel32.CloseHandle(info.hThread))
        info.hThread = None


def _close_process_handles(info):
    try:
        _close_thread(info)
    finally:
        if info.hProcess:
            _check(_kernel32.CloseHandle(info.hProcess))
            info.hProcess = None


def _wait_status(handle, milliseconds):
    status = _kernel32.WaitForSingleObject(handle, milliseconds)
    if status == _INFINITE:  # WAIT_FAILED
        raise ctypes.WinError(ctypes.get_last_error())
    if status not in (_WAIT_OBJECT_0, _WAIT_TIMEOUT):
        raise OSError(f"Unexpected process wait status: {status}")
    return status


def _exit_code(handle):
    code = wintypes.DWORD()
    _check(_kernel32.GetExitCodeProcess(handle, ctypes.byref(code)))
    return code.value


class _OwnedProcess:
    def __init__(self, arguments, stdout, info, job):
        self.args, self.stdout, self._info, self._job = arguments, stdout, info, job
        self.pid = int(info.dwProcessId)
        self._returncode = None
        self._lock = threading.RLock()

    @property
    def returncode(self):
        return self._returncode

    def poll(self):
        with self._lock:
            if self._returncode is None and _wait_status(self._info.hProcess, 0) == _WAIT_OBJECT_0:
                # A real exit code of 259 (STILL_ACTIVE) is legal. Test the
                # process object's signaled state before reading its exit code.
                self._returncode = _exit_code(self._info.hProcess)
            return self._returncode

    def terminate(self):
        with self._lock:
            if self.poll() is not None:
                return
            if not _kernel32.TerminateProcess(self._info.hProcess, 1):
                error = ctypes.get_last_error()
                if self.poll() is None:  # Allow the ordinary exit/terminate race.
                    raise ctypes.WinError(error)

    kill = terminate

    def wait(self, timeout=None):
        duration = None if timeout is None else float(timeout)
        if duration is not None and not math.isfinite(duration):
            raise ValueError("timeout must be finite, or None")
        deadline = None if duration is None else time.monotonic() + max(0, duration)
        with ExitStack() as waiting:
            with self._lock:
                code = self.poll()
                if code is not None:
                    return code
                # A private duplicate makes concurrent wait/release safe. Do not
                # hold the lock during a wait: another thread must be able to kill.
                duplicate = _owned_handle(waiting)
                current = _kernel32.GetCurrentProcess()
                _check(_kernel32.DuplicateHandle(
                    current, self._info.hProcess, current, ctypes.byref(duplicate.raw),
                    0, False, 2,  # DUPLICATE_SAME_ACCESS
                ))
            while True:
                remaining = None if deadline is None else max(0, deadline - time.monotonic())
                milliseconds = _INFINITE if remaining is None else min(
                    _INFINITE - 1, math.ceil(min(remaining, (_INFINITE - 1) / 1000) * 1000))
                if _wait_status(duplicate.value, milliseconds) == _WAIT_OBJECT_0:
                    code = _exit_code(duplicate.value)
                    with self._lock:
                        self._returncode = code
                    return code
                if deadline is not None and time.monotonic() >= deadline:
                    raise subprocess.TimeoutExpired(self.args, timeout)

    def release(self):
        """Close process/job handles after confirmed exit; keep stdout readable."""
        with self._lock:
            if self.poll() is None:
                raise RuntimeError("Cannot release a running owned process.")
            self._job.close()  # Also kills any descendants that outlived the child.
            _close_process_handles(self._info)

    def __del__(self):
        # Last-resort ownership cleanup is nonblocking, including at interpreter
        # shutdown. Normal callers retain this object until wait/poll + release.
        try:
            if hasattr(self, "_job"):
                self._job.close()
        except Exception:
            pass
        try:
            if hasattr(self, "_info"):
                _close_process_handles(self._info)
        except Exception:
            pass


def _environment_block(environment):
    if not isinstance(environment, Mapping):
        raise TypeError("env must be an explicit string mapping")
    environment = dict(environment)
    for key, value in environment.items():
        if not isinstance(key, str) or not isinstance(value, str):
            raise TypeError("env keys and values must be strings")
        if not key or "=" in key or "\0" in key or "\0" in value:
            raise ValueError("Invalid environment entry")

    def compare(left, right):
        # Windows specifies Unicode ordinal, case-insensitive environment order.
        return _check(_kernel32.CompareStringOrdinal(left, -1, right, -1, True)) - 2

    keys = sorted(environment, key=cmp_to_key(compare))
    if any(compare(left, right) == 0 for left, right in zip(keys, keys[1:])):
        raise ValueError("env contains duplicate case-insensitive keys")
    # create_unicode_buffer adds the final NUL, including for an empty mapping.
    return ctypes.create_unicode_buffer("\0".join(f"{key}={environment[key]}" for key in keys) + "\0")


def _open_output(handle):
    descriptor = msvcrt.open_osfhandle(handle.value, os.O_RDONLY | os.O_BINARY)
    handle.raw.value = None  # The CRT now owns this handle.
    try:
        return io.open(descriptor, "rb")
    except BaseException:
        os.close(descriptor)
        raise


def spawn_owned_process(arguments, **kwargs):
    """Launch the tunnel's restricted Popen configuration, atomically job-owned."""
    if os.name != "nt":
        raise NotImplementedError("Owned child processes require Windows 10 or later.")
    supported = {
        "shell": False, "stdin": subprocess.DEVNULL, "stdout": subprocess.PIPE,
        "stderr": subprocess.STDOUT, "text": True, "encoding": "utf-8",
        "errors": "replace", "creationflags": _CREATE_NO_WINDOW,
    }
    unknown = kwargs.keys() - supported.keys() - {"env"}
    if unknown:
        raise TypeError(f"Unsupported process options: {', '.join(sorted(unknown))}")
    for key, value in supported.items():
        if kwargs.get(key, value) != value:
            raise ValueError(f"Unsupported value for {key}")
    if "env" not in kwargs:
        raise TypeError("An explicit env mapping is required")
    if not isinstance(arguments, Sequence) or isinstance(arguments, (str, bytes)) or not arguments:
        raise TypeError("arguments must be a nonempty sequence of strings or paths")
    arguments = [os.fspath(argument) for argument in arguments]
    if any(not isinstance(argument, str) or "\0" in argument for argument in arguments) or not arguments[0]:
        raise ValueError("arguments must contain Unicode strings without NUL characters")
    environment = _environment_block(kwargs["env"])
    command = ctypes.create_unicode_buffer(subprocess.list2cmdline(arguments))
    info = _PROCESS_INFORMATION()

    with ExitStack() as owned, ExitStack() as temporary:
        job = _owned_handle(owned)
        job.raw.value = _check(_kernel32.CreateJobObjectW(None, None))
        limits = _JOBOBJECT_EXTENDED_LIMITS()
        limits.BasicLimitInformation.LimitFlags = _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        _check(_kernel32.SetInformationJobObject(
            job.value, _JOB_OBJECT_EXTENDED_LIMIT_INFORMATION, ctypes.byref(limits), ctypes.sizeof(limits)))

        read, write, null = (_owned_handle(temporary) for _ in range(3))
        security = _SECURITY_ATTRIBUTES(ctypes.sizeof(_SECURITY_ATTRIBUTES), None, True)
        _check(_kernel32.CreatePipe(ctypes.byref(read.raw), ctypes.byref(write.raw), ctypes.byref(security), 0))
        _check(_kernel32.SetHandleInformation(read.value, 1, 0))  # HANDLE_FLAG_INHERIT
        null_value = _kernel32.CreateFileW(
            "NUL", 0x80000000, 3, ctypes.byref(security), 3, 0x80, None,
            # GENERIC_READ, FILE_SHARE_READ | FILE_SHARE_WRITE, OPEN_EXISTING,
            # FILE_ATTRIBUTE_NORMAL.
        )
        if null_value == wintypes.HANDLE(-1).value:
            raise ctypes.WinError(ctypes.get_last_error())
        null.raw.value = _check(null_value)
        binary = _open_output(read)
        owned.callback(binary.close)
        stdout = io.TextIOWrapper(binary, encoding="utf-8", errors="replace")
        owned.callback(stdout.close)

        size = ctypes.c_size_t()
        ctypes.set_last_error(0)
        initialized = _kernel32.InitializeProcThreadAttributeList(None, 2, 0, ctypes.byref(size))
        if initialized or ctypes.get_last_error() != 122 or not size.value:  # ERROR_INSUFFICIENT_BUFFER
            raise ctypes.WinError(ctypes.get_last_error())
        storage = ctypes.create_string_buffer(size.value)
        attributes = ctypes.cast(storage, wintypes.LPVOID)
        _check(_kernel32.InitializeProcThreadAttributeList(attributes, 2, 0, ctypes.byref(size)))
        temporary.callback(_kernel32.DeleteProcThreadAttributeList, attributes)
        jobs = (wintypes.HANDLE * 1)(job.value)
        inherited = (wintypes.HANDLE * 2)(null.value, write.value)
        _check(_kernel32.UpdateProcThreadAttribute(
            attributes, 0, _PROC_THREAD_ATTRIBUTE_JOB_LIST, jobs, ctypes.sizeof(jobs), None, None))
        _check(_kernel32.UpdateProcThreadAttribute(
            attributes, 0, _PROC_THREAD_ATTRIBUTE_HANDLE_LIST, inherited, ctypes.sizeof(inherited), None, None))
        startup = _STARTUPINFOEXW()
        startup.StartupInfo.cb = ctypes.sizeof(startup)
        startup.StartupInfo.dwFlags = 0x00000101  # STARTF_USESTDHANDLES | STARTF_USESHOWWINDOW
        startup.StartupInfo.wShowWindow = 0  # SW_HIDE
        startup.StartupInfo.hStdInput = null.value
        startup.StartupInfo.hStdOutput = startup.StartupInfo.hStdError = write.value
        startup.lpAttributeList = attributes
        # Register cleanup before the native call fills in either output handle.
        owned.callback(_close_process_handles, info)
        try:
            _check(_kernel32.CreateProcessW(
                arguments[0], command, None, None, True,
                _CREATE_NO_WINDOW | _CREATE_UNICODE_ENVIRONMENT | _EXTENDED_STARTUPINFO_PRESENT,
                environment, None, ctypes.byref(startup), ctypes.byref(info),
            ))
            _close_thread(info)
            temporary.close()
            process = _OwnedProcess(arguments, stdout, info, job)
            owned.pop_all()
            return process
        except BaseException as error:
            # Closing the only job handle kills even a child that started before
            # a later Python operation failed. Keep its process handle until the
            # bounded cleanup wait finishes; never recover a process by PID.
            try:
                job.close()
                if info.hProcess and _wait_status(info.hProcess, _STARTUP_CLEANUP_TIMEOUT_MS) != _WAIT_OBJECT_0:
                    error.add_note("The owned job was closed; startup cleanup's exit wait timed out.")
            except OSError as cleanup_error:
                error.add_note(f"Owned process startup cleanup: {cleanup_error}")
            raise
