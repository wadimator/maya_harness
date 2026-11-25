"""Maya on a hidden Windows desktop, in a job object that kills it when the job handle closes.

* The hidden desktop keeps Maya's windows off screen: its splash screen takes the focus even
  from a minimized process, and the windows the code opens would cover the user's work.
* The job kills Maya, and everything Maya started, when its handle closes: on an error, on
  Ctrl+C and when the harness itself is killed. Not a `quit`: the settings of this Maya are thrown
  away with its directory, and a kill does not hang on a modal dialog. A Maya left running by
  `release` holds its own job: the job closes, and kills what Maya started, when Maya dies.
  Without it, a killed Maya leaves its two `AdskLicensingAgent.exe` for minutes, and they keep
  its hidden desktop.
* The job runs Maya below normal priority and, on more than 8 logical CPUs, off the first
  `SPARED_CPUS` of them: the PC stays usable while Maya works.
* Maya is created suspended and put in the job before its first instruction: nothing it starts
  can escape the job.
* Windows only: the Win32 API is in `win32`.
"""

from __future__ import annotations

import ctypes
import os
import subprocess
import threading
from ctypes import wintypes
from pathlib import Path

from . import win32

EXIT_TIMEOUT = 60  # seconds for a killed Maya to exit
SPARED_CPUS = 4  # the first logical CPUs, left to the user's own work while Maya runs
SPARE_FROM = 8  # logical CPUs; on fewer, Maya gets all of them

DESKTOP_PREFIX = "maya_harness_"
MAIN_TITLE = "Autodesk MAYA"  # in the title of Maya's main window: "untitled - Autodesk MAYA 2025.3.1: untitled"


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
        startup = win32.STARTUPINFOW(cb=ctypes.sizeof(win32.STARTUPINFOW))
        self.desktop = None
        if not visible:
            name = f"{DESKTOP_PREFIX}{os.getpid()}_{number}"
            self.desktop = win32.checked(win32.user32.CreateDesktopW(name, None, None, 0, win32.GENERIC_ALL, None))
            startup.lpDesktop = f"WinSta0\\{name}"
        self.job = win32.checked(win32.kernel32.CreateJobObjectW(None, None))
        self.closed = False
        self._lock = threading.Lock()
        affinity = affinity_mask(os.cpu_count() or 1)
        flags = win32.JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE | win32.JOB_OBJECT_LIMIT_PRIORITY_CLASS
        self._set_job_limits(flags | (win32.JOB_OBJECT_LIMIT_AFFINITY if affinity else 0), affinity)

        process = win32.PROCESS_INFORMATION()
        block = environment_block(env)
        environment = ctypes.create_unicode_buffer(block, len(block))
        command = ctypes.create_unicode_buffer(subprocess.list2cmdline([str(maya), "-hideConsole"]))
        flags = win32.CREATE_SUSPENDED | win32.CREATE_UNICODE_ENVIRONMENT
        win32.checked(
            win32.kernel32.CreateProcessW(
                None, command, None, None, False, flags, environment, None, ctypes.byref(startup), ctypes.byref(process)
            )
        )
        self.handle, self.pid = process.hProcess, int(process.dwProcessId)
        self.created = win32.creation_time(self.handle)
        win32.checked(win32.kernel32.AssignProcessToJobObject(self.job, self.handle))
        win32.kernel32.ResumeThread(process.hThread)
        win32.kernel32.CloseHandle(process.hThread)

    def _set_job_limits(self, flags: int, affinity: int = 0) -> None:
        limits = win32.JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
        limits.BasicLimitInformation.LimitFlags = flags
        limits.BasicLimitInformation.Affinity = affinity
        limits.BasicLimitInformation.PriorityClass = win32.BELOW_NORMAL_PRIORITY_CLASS
        win32.checked(
            win32.kernel32.SetInformationJobObject(
                self.job, win32.JOB_OBJECT_EXTENDED_LIMIT_INFORMATION_CLASS, ctypes.byref(limits), ctypes.sizeof(limits)
            )
        )

    def running(self) -> bool:
        return bool(win32.kernel32.WaitForSingleObject(self.handle, 0) != 0)

    def wait(self, timeout: float) -> bool:
        return bool(win32.kernel32.WaitForSingleObject(self.handle, int(timeout * 1000)) == 0)

    def window_titles(self) -> list[str]:
        """Maya's visible windows: on the hidden desktop a modal dialog is seen only this way."""
        return window_titles(
            self.pid, self.desktop or win32.user32.GetThreadDesktop(win32.kernel32.GetCurrentThreadId())
        )

    def close(self) -> None:
        """Kills whatever is still running in the job, then lets go of the desktop. Once."""
        with self._lock:
            if self.closed:
                return
            self.closed = True
            win32.kernel32.CloseHandle(self.job)
            self.wait(EXIT_TIMEOUT)
            self._close_handles()

    def release(self) -> None:
        """Lets Maya outlive the harness: Maya holds its own job, which closes with Maya and kills
        what Maya started; the desktop stays while Maya is on it. If Maya cannot take the job,
        closing ours kills Maya, and the error goes on."""
        with self._lock:
            self.closed = True
            try:
                win32.give(self.job, self.handle)
            finally:
                win32.kernel32.CloseHandle(self.job)
                self._close_handles()

    def _close_handles(self) -> None:
        win32.kernel32.CloseHandle(self.handle)
        if self.desktop:
            win32.user32.CloseDesktop(self.desktop)


def window_titles(pid: int, desktop: object = None) -> list[str]:
    """The visible windows of process `pid` that have a title (Maya has visible ones without),
    on `desktop`; without it, on every hidden desktop of maya_harness and on the current one:
    the Maya of another harness process is on a desktop this one did not make."""
    return [title for _, title in _windows(pid, desktop)]


def modal_dialog(pid: int, desktop: object = None) -> list[str]:
    """The titles of Maya's other windows when a modal dialog holds its main window, else [].

    Measured in Maya 2025.3.1 on the hidden desktop: the main window is the one with `MAIN_TITLE`
    in its title. «Untrusted Plugin Loading» and `cmds.confirmDialog` disable it. A call in
    `time.sleep`, an open Script Editor, `cmds.progressWindow`, the main progress bar and the
    opening of a 422 MB scene leave it enabled: a busy Maya is not taken for one on a dialog.
    `confirmDialog` disables it some 0.25 s before the dialog gets its title: a disabled window
    with no other titled one is [] too, and the next look finds the dialog with its title."""
    windows = _windows(pid, desktop)
    main = [window for window, title in windows if MAIN_TITLE in title]
    if not main or any(win32.user32.IsWindowEnabled(window) for window in main):
        return []
    return [title for window, title in windows if window not in main]


def _windows(pid: int, desktop: object) -> list[tuple[int, str]]:
    """The visible titled windows of `pid` and their titles; the desktops as in `window_titles`."""
    if desktop is not None:
        return _titles_on(desktop, pid)
    names: list[str] = []

    def collect(name: str, _: int) -> bool:
        if name.startswith(DESKTOP_PREFIX):
            names.append(name)
        return True

    win32.user32.EnumDesktopsW(win32.user32.GetProcessWindowStation(), win32.DESKENUMPROC(collect), 0)
    titles = _titles_on(win32.user32.GetThreadDesktop(win32.kernel32.GetCurrentThreadId()), pid)
    for name in names:
        handle = win32.user32.OpenDesktopW(name, 0, False, win32.DESKTOP_READOBJECTS | win32.DESKTOP_ENUMERATE)
        if handle:  # closed since it was listed
            titles += _titles_on(handle, pid)
            win32.user32.CloseDesktop(handle)
    return titles


def _titles_on(desktop: object, pid: int) -> list[tuple[int, str]]:
    titles: list[tuple[int, str]] = []

    def collect(window: int, _: int) -> bool:
        owner = wintypes.DWORD()
        win32.user32.GetWindowThreadProcessId(window, ctypes.byref(owner))
        if owner.value == pid and win32.user32.IsWindowVisible(window) and win32.user32.GetWindowTextLengthW(window):
            title = ctypes.create_unicode_buffer(512)
            win32.user32.GetWindowTextW(window, title, len(title))
            titles.append((window, title.value))
        return True

    win32.user32.EnumDesktopWindows(desktop, win32.WNDENUMPROC(collect), 0)
    return titles


def is_our_maya(image: str, created: int, recorded: int) -> bool:
    """Windows reuses pids: a pid names our Maya only together with its image and creation time."""
    return Path(image).name.lower() == "maya.exe" and created == recorded


def kill_maya(pid: int, recorded: int) -> bool:
    """Kills our Maya `pid`; True once it is gone. Our Maya always opens for us: a pid that does
    not, or that names another process now, means it is gone already."""
    handle = win32.kernel32.OpenProcess(
        win32.PROCESS_TERMINATE | win32.PROCESS_QUERY_LIMITED_INFORMATION | win32.SYNCHRONIZE, False, pid
    )
    if not handle:
        return True
    try:
        try:
            ours = is_our_maya(win32.image_name(handle), win32.creation_time(handle), recorded)
        except OSError:  # exited, though someone still holds a handle: no image name (WinError 31)
            return bool(win32.kernel32.WaitForSingleObject(handle, 0) == 0)
        if not ours:
            return True  # the pid names another process now
        win32.kernel32.TerminateProcess(handle, 1)  # unchecked: a process already exiting refuses
        return bool(win32.kernel32.WaitForSingleObject(handle, EXIT_TIMEOUT * 1000) == 0)
    finally:
        win32.kernel32.CloseHandle(handle)


def maya_alive(pid: int, recorded: int) -> bool:
    """Our Maya `pid` still runs. Windows reuses pids: a pid that names another process now is
    not our Maya."""
    handle = win32.kernel32.OpenProcess(win32.PROCESS_QUERY_LIMITED_INFORMATION | win32.SYNCHRONIZE, False, pid)
    if not handle:
        return False
    try:
        if win32.kernel32.WaitForSingleObject(handle, 0) != win32.WAIT_TIMEOUT:
            return False
        try:
            return is_our_maya(win32.image_name(handle), win32.creation_time(handle), recorded)
        except OSError:
            return False
    finally:
        win32.kernel32.CloseHandle(handle)


def process_alive(pid: int) -> bool:
    handle = win32.kernel32.OpenProcess(win32.SYNCHRONIZE, False, pid)
    if not handle:
        return False
    alive = win32.kernel32.WaitForSingleObject(handle, 0) == win32.WAIT_TIMEOUT
    win32.kernel32.CloseHandle(handle)
    return bool(alive)
