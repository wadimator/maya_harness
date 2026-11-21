"""Maya on a hidden Windows desktop, in a job object that kills it when the job handle closes.

* The hidden desktop keeps Maya's windows off screen: its splash screen takes the focus even
  from a minimized process, and the windows the code opens would cover the user's work.
* The job kills Maya, and everything Maya started, when its handle closes: on an error, on
  Ctrl+C and when the harness itself is killed. Not a `quit`: the settings of this Maya are thrown
  away with its directory, and a kill does not hang on a modal dialog. A Maya left running by
  `release` is out of the job; it is closed by a deferred `quit` instead, and a Maya that does
  not take it `stop` kills, once its image and creation time show the pid is still that Maya.
* The job runs Maya below normal priority and, on more than 8 logical CPUs, off the first
  `SPARED_CPUS` of them: the PC stays usable while Maya works.
* Maya is created suspended and put in the job before its first instruction: nothing it starts
  can escape the job.
* Windows only, through `ctypes`: every handle has its `argtypes` and `restype`, or ctypes
  passes it as a 32-bit int and cuts it on x64.
"""

from __future__ import annotations

import ctypes
import os
import subprocess
import threading
from ctypes import wintypes
from pathlib import Path

EXIT_TIMEOUT = 60  # seconds for a killed Maya to exit
SPARED_CPUS = 4  # the first logical CPUs, left to the user's own work while Maya runs
SPARE_FROM = 8  # logical CPUs; on fewer, Maya gets all of them

user32 = ctypes.WinDLL("user32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

CREATE_SUSPENDED = 0x4
CREATE_UNICODE_ENVIRONMENT = 0x400
GENERIC_ALL = 0x10000000
PROCESS_TERMINATE = 0x1
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
SYNCHRONIZE = 0x100000
WAIT_TIMEOUT = 0x102
JOB_OBJECT_LIMIT_AFFINITY = 0x10
JOB_OBJECT_LIMIT_PRIORITY_CLASS = 0x20
JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x2000
BELOW_NORMAL_PRIORITY_CLASS = 0x4000
JOB_OBJECT_EXTENDED_LIMIT_INFORMATION_CLASS = 9


class STARTUPINFOW(ctypes.Structure):
    _fields_ = [
        ("cb", wintypes.DWORD),
        ("lpReserved", wintypes.LPWSTR),
        ("lpDesktop", wintypes.LPWSTR),
        ("lpTitle", wintypes.LPWSTR),
        ("dwX", wintypes.DWORD),
        ("dwY", wintypes.DWORD),
        ("dwXSize", wintypes.DWORD),
        ("dwYSize", wintypes.DWORD),
        ("dwXCountChars", wintypes.DWORD),
        ("dwYCountChars", wintypes.DWORD),
        ("dwFillAttribute", wintypes.DWORD),
        ("dwFlags", wintypes.DWORD),
        ("wShowWindow", wintypes.WORD),
        ("cbReserved2", wintypes.WORD),
        ("lpReserved2", ctypes.c_void_p),
        ("hStdInput", wintypes.HANDLE),
        ("hStdOutput", wintypes.HANDLE),
        ("hStdError", wintypes.HANDLE),
    ]


class PROCESS_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("hProcess", wintypes.HANDLE),
        ("hThread", wintypes.HANDLE),
        ("dwProcessId", wintypes.DWORD),
        ("dwThreadId", wintypes.DWORD),
    ]


class IO_COUNTERS(ctypes.Structure):
    _fields_ = [(name, ctypes.c_ulonglong) for name in ("Read", "Write", "Other")] + [
        (f"{name}TransferCount", ctypes.c_ulonglong) for name in ("Read", "Write", "Other")
    ]


class JOBOBJECT_BASIC_LIMIT_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("PerProcessUserTimeLimit", ctypes.c_longlong),
        ("PerJobUserTimeLimit", ctypes.c_longlong),
        ("LimitFlags", wintypes.DWORD),
        ("MinimumWorkingSetSize", ctypes.c_size_t),
        ("MaximumWorkingSetSize", ctypes.c_size_t),
        ("ActiveProcessLimit", wintypes.DWORD),
        ("Affinity", ctypes.c_size_t),
        ("PriorityClass", wintypes.DWORD),
        ("SchedulingClass", wintypes.DWORD),
    ]


class JOBOBJECT_EXTENDED_LIMIT_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("BasicLimitInformation", JOBOBJECT_BASIC_LIMIT_INFORMATION),
        ("IoInfo", IO_COUNTERS),
        ("ProcessMemoryLimit", ctypes.c_size_t),
        ("JobMemoryLimit", ctypes.c_size_t),
        ("PeakProcessMemoryUsed", ctypes.c_size_t),
        ("PeakJobMemoryUsed", ctypes.c_size_t),
    ]


user32.CreateDesktopW.restype = wintypes.HANDLE
user32.CreateDesktopW.argtypes = [
    wintypes.LPCWSTR,
    wintypes.LPCWSTR,
    ctypes.c_void_p,
    wintypes.DWORD,
    wintypes.DWORD,
    ctypes.c_void_p,
]
user32.CloseDesktop.argtypes = [wintypes.HANDLE]
user32.GetThreadDesktop.restype = wintypes.HANDLE
user32.GetThreadDesktop.argtypes = [wintypes.DWORD]
kernel32.CreateJobObjectW.restype = wintypes.HANDLE
kernel32.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
kernel32.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
kernel32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
kernel32.CreateProcessW.argtypes = [
    wintypes.LPCWSTR,
    wintypes.LPWSTR,
    ctypes.c_void_p,
    ctypes.c_void_p,
    wintypes.BOOL,
    wintypes.DWORD,
    ctypes.c_void_p,
    wintypes.LPCWSTR,
    ctypes.POINTER(STARTUPINFOW),
    ctypes.POINTER(PROCESS_INFORMATION),
]
WNDENUMPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
user32.EnumDesktopWindows.argtypes = [wintypes.HANDLE, WNDENUMPROC, wintypes.LPARAM]
user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
user32.IsWindowVisible.argtypes = [wintypes.HWND]
user32.GetWindowTextLengthW.argtypes = [wintypes.HWND]
user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
kernel32.OpenProcess.restype = wintypes.HANDLE
kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
kernel32.ResumeThread.argtypes = [wintypes.HANDLE]
kernel32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
kernel32.GetProcessTimes.argtypes = [wintypes.HANDLE, *[ctypes.POINTER(wintypes.FILETIME)] * 4]
kernel32.QueryFullProcessImageNameW.argtypes = [
    wintypes.HANDLE,
    wintypes.DWORD,
    wintypes.LPWSTR,
    ctypes.POINTER(wintypes.DWORD),
]
kernel32.TerminateProcess.argtypes = [wintypes.HANDLE, wintypes.UINT]


def _checked(result: object) -> object:
    if not result:
        raise ctypes.WinError(ctypes.get_last_error())
    return result


def affinity_mask(cpus: int, spared: int = SPARED_CPUS) -> int:
    """The logical CPUs Maya may run on, as a mask; 0 for no limit. Above `SPARE_FROM` CPUs the
    first `spared` are left to the user."""
    if cpus <= SPARE_FROM:
        return 0
    return ((1 << cpus) - 1) & ~((1 << spared) - 1)


def environment_block(env: dict[str, str]) -> str:
    """The environment for `CreateProcessW`: `NAME=value` entries ending in NUL, then one more NUL.
    Sorted by name, case-insensitively, as Windows expects the block to be."""
    items = sorted(env.items(), key=lambda item: item[0].upper())
    return "".join(f"{name}={value}\0" for name, value in items) + "\0"


class MayaProcess:
    """Maya at `maya` on a hidden desktop of its own, or on the user's with `visible`, in a job
    object that kills it when the job handle closes.

    `number` tells apart the Mayas one harness process starts: each has a desktop of its own,
    named `maya_harness_<pid>_<number>`. `handle` is the process handle of Maya, open until
    `close` or `release`."""

    def __init__(self, maya: Path, env: dict[str, str], visible: bool = False, number: int = 1) -> None:
        startup = STARTUPINFOW(cb=ctypes.sizeof(STARTUPINFOW))
        self.desktop = None
        if not visible:
            name = f"maya_harness_{os.getpid()}_{number}"
            self.desktop = _checked(user32.CreateDesktopW(name, None, None, 0, GENERIC_ALL, None))
            startup.lpDesktop = f"WinSta0\\{name}"
        self.job = _checked(kernel32.CreateJobObjectW(None, None))
        self.closed = False
        self._lock = threading.Lock()
        affinity = affinity_mask(os.cpu_count() or 1)
        flags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE | JOB_OBJECT_LIMIT_PRIORITY_CLASS
        self._set_job_limits(flags | (JOB_OBJECT_LIMIT_AFFINITY if affinity else 0), affinity)

        process = PROCESS_INFORMATION()
        block = environment_block(env)
        environment = ctypes.create_unicode_buffer(block, len(block))
        command = ctypes.create_unicode_buffer(subprocess.list2cmdline([str(maya), "-hideConsole"]))
        flags = CREATE_SUSPENDED | CREATE_UNICODE_ENVIRONMENT
        _checked(
            kernel32.CreateProcessW(
                None, command, None, None, False, flags, environment, None, ctypes.byref(startup), ctypes.byref(process)
            )
        )
        self.handle, self.pid = process.hProcess, int(process.dwProcessId)
        self.created = creation_time(self.handle)
        _checked(kernel32.AssignProcessToJobObject(self.job, self.handle))
        kernel32.ResumeThread(process.hThread)
        kernel32.CloseHandle(process.hThread)

    def _set_job_limits(self, flags: int, affinity: int = 0) -> None:
        limits = JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
        limits.BasicLimitInformation.LimitFlags = flags
        limits.BasicLimitInformation.Affinity = affinity
        limits.BasicLimitInformation.PriorityClass = BELOW_NORMAL_PRIORITY_CLASS
        _checked(
            kernel32.SetInformationJobObject(
                self.job, JOB_OBJECT_EXTENDED_LIMIT_INFORMATION_CLASS, ctypes.byref(limits), ctypes.sizeof(limits)
            )
        )

    def running(self) -> bool:
        return bool(kernel32.WaitForSingleObject(self.handle, 0) != 0)

    def wait(self, timeout: float) -> bool:
        return bool(kernel32.WaitForSingleObject(self.handle, int(timeout * 1000)) == 0)

    def window_titles(self) -> list[str]:
        """Maya's visible windows: on the hidden desktop a modal dialog is seen only this way."""
        titles: list[str] = []

        def collect(window: int, _: int) -> bool:
            owner = wintypes.DWORD()
            user32.GetWindowThreadProcessId(window, ctypes.byref(owner))
            if owner.value == self.pid and user32.IsWindowVisible(window) and user32.GetWindowTextLengthW(window):
                title = ctypes.create_unicode_buffer(512)
                user32.GetWindowTextW(window, title, len(title))
                titles.append(title.value)
            return True

        desktop = self.desktop or user32.GetThreadDesktop(kernel32.GetCurrentThreadId())
        user32.EnumDesktopWindows(desktop, WNDENUMPROC(collect), 0)
        return titles

    def close(self) -> None:
        """Kills whatever is still running in the job, then lets go of the desktop. Once."""
        with self._lock:
            if self.closed:
                return
            self.closed = True
            kernel32.CloseHandle(self.job)
            self.wait(EXIT_TIMEOUT)
            self._close_handles()

    def release(self) -> None:
        """Lets Maya outlive the harness: the job no longer kills it, and the desktop stays while
        Maya is on it."""
        with self._lock:
            self.closed = True
            self._set_job_limits(0)
            kernel32.CloseHandle(self.job)
            self._close_handles()

    def _close_handles(self) -> None:
        kernel32.CloseHandle(self.handle)
        if self.desktop:
            user32.CloseDesktop(self.desktop)


def creation_time(handle: object) -> int:
    """When the process was created, in 100 ns FILETIME units: with the pid, it names the process."""
    created, exited, kernel, user = (wintypes.FILETIME() for _ in range(4))
    _checked(
        kernel32.GetProcessTimes(
            handle, ctypes.byref(created), ctypes.byref(exited), ctypes.byref(kernel), ctypes.byref(user)
        )
    )
    return (created.dwHighDateTime << 32) | created.dwLowDateTime


def image_name(handle: object) -> str:
    size = wintypes.DWORD(32768)
    name = ctypes.create_unicode_buffer(size.value)
    _checked(kernel32.QueryFullProcessImageNameW(handle, 0, name, ctypes.byref(size)))
    return name.value


def is_our_maya(image: str, created: int, recorded: int) -> bool:
    """Windows reuses pids: a pid names our Maya only together with its image and creation time."""
    return Path(image).name.lower() == "maya.exe" and created == recorded


def kill_maya(pid: int, recorded: int) -> bool:
    """Kills our Maya `pid`; True once it is gone. Our Maya always opens for us: a pid that does
    not, or that names another process now, means it is gone already."""
    handle = kernel32.OpenProcess(PROCESS_TERMINATE | PROCESS_QUERY_LIMITED_INFORMATION | SYNCHRONIZE, False, pid)
    if not handle:
        return True
    try:
        try:
            ours = is_our_maya(image_name(handle), creation_time(handle), recorded)
        except OSError:  # exited, though someone still holds a handle: no image name (WinError 31)
            return bool(kernel32.WaitForSingleObject(handle, 0) == 0)
        if not recorded:  # a state.json from before the creation time: cannot tell
            return False
        if not ours:
            return True  # the pid names another process now
        kernel32.TerminateProcess(handle, 1)  # unchecked: a process already exiting refuses
        return bool(kernel32.WaitForSingleObject(handle, EXIT_TIMEOUT * 1000) == 0)
    finally:
        kernel32.CloseHandle(handle)


def maya_alive(pid: int, recorded: int) -> bool:
    """Our Maya `pid` still runs. Windows reuses pids: with a recorded creation time, a pid that
    names another process now is not our Maya; without one (an old state.json), the pid decides."""
    handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION | SYNCHRONIZE, False, pid)
    if not handle:
        return False
    try:
        if kernel32.WaitForSingleObject(handle, 0) != WAIT_TIMEOUT:
            return False
        if not recorded:
            return True
        try:
            return is_our_maya(image_name(handle), creation_time(handle), recorded)
        except OSError:
            return False
    finally:
        kernel32.CloseHandle(handle)


def process_alive(pid: int) -> bool:
    handle = kernel32.OpenProcess(SYNCHRONIZE, False, pid)
    if not handle:
        return False
    alive = kernel32.WaitForSingleObject(handle, 0) == WAIT_TIMEOUT
    kernel32.CloseHandle(handle)
    return bool(alive)
