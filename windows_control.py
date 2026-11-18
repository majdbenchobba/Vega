"""Small, explicit Windows controls; no arbitrary command execution."""
from __future__ import annotations

import ctypes
from ctypes import wintypes
import hashlib
import hmac
import os
from pathlib import Path
import re
import secrets
import subprocess
import time


def inspect_codex(settings):
    if os.name != "nt":
        return {"state": "unknown", "pids": [], "reason": "Windows is required"}

    class Entry(ctypes.Structure):
        _fields_ = [
            ("dwSize", wintypes.DWORD), ("cntUsage", wintypes.DWORD),
            ("th32ProcessID", wintypes.DWORD), ("th32DefaultHeapID", ctypes.c_size_t),
            ("th32ModuleID", wintypes.DWORD), ("cntThreads", wintypes.DWORD),
            ("th32ParentProcessID", wintypes.DWORD), ("pcPriClassBase", wintypes.LONG),
            ("dwFlags", wintypes.DWORD), ("szExeFile", wintypes.WCHAR * 260),
        ]

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
    kernel.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    kernel.Process32FirstW.argtypes = [wintypes.HANDLE, ctypes.POINTER(Entry)]
    kernel.Process32NextW.argtypes = [wintypes.HANDLE, ctypes.POINTER(Entry)]
    kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.QueryFullProcessImageNameW.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD)]
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    snapshot = kernel.CreateToolhelp32Snapshot(2, 0)
    if snapshot == ctypes.c_void_p(-1).value:
        return {"state": "unknown", "pids": [], "reason": "Process inventory unavailable"}
    matching, inaccessible = [], False
    entry = Entry()
    entry.dwSize = ctypes.sizeof(entry)
    try:
        exists = kernel.Process32FirstW(snapshot, ctypes.byref(entry))
        while exists:
            if entry.szExeFile.lower() == settings["codex_process_name"].lower():
                handle = kernel.OpenProcess(0x1000, False, entry.th32ProcessID)
                if not handle:
                    inaccessible = True
                else:
                    try:
                        buffer = ctypes.create_unicode_buffer(32768)
                        length = wintypes.DWORD(len(buffer))
                        if kernel.QueryFullProcessImageNameW(handle, 0, buffer, ctypes.byref(length)):
                            if settings["codex_package_path_marker"].lower() in buffer.value.lower():
                                matching.append(entry.th32ProcessID)
                        else:
                            inaccessible = True
                    finally:
                        kernel.CloseHandle(handle)
            exists = kernel.Process32NextW(snapshot, ctypes.byref(entry))
    finally:
        kernel.CloseHandle(snapshot)
    return {"state": "running" if matching else "unknown" if inaccessible else "absent", "pids": matching}


class WindowsControl:
    def __init__(self, store, settings, *, inspector=inspect_codex, launcher=None, runner=None, clock=time.time):
        self.store, self.settings = store, settings
        self.inspector, self.clock = inspector, clock
        self.launcher = launcher or self._launch
        self.runner = runner or self._run
        system = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32"
        apps = {
            "codex": {"label": "Codex / ChatGPT desktop", "app_id": settings["codex_app_id"]},
            "vega": {"label": "Vega", "url": "http://127.0.0.1:8769/"},
            "notepad": {"label": "Notepad", "path": str(system / "notepad.exe")},
            "calculator": {"label": "Calculator", "path": str(system / "calc.exe")},
        }
        additional_apps = settings.get("additional_apps", {})
        if not isinstance(additional_apps, dict):
            raise ValueError("The additional Windows app configuration is invalid.")
        for alias, app in additional_apps.items():
            if (not isinstance(alias, str) or re.fullmatch(r"[a-z][a-z0-9_-]{0,39}", alias) is None
                    or alias in apps or not isinstance(app, dict) or set(app) != {"label", "app_id"}
                    or not isinstance(app["label"], str) or not 1 <= len(app["label"]) <= 80
                    or not isinstance(app["app_id"], str)
                    or re.fullmatch(r"[A-Za-z0-9_.!-]{1,200}", app["app_id"]) is None):
                raise ValueError("An additional Windows app entry is invalid.")
            apps[alias] = {"label": app["label"], "app_id": app["app_id"]}
        self.apps = apps
        self.shutdown = str(system / "shutdown.exe")
        if os.name == "nt":
            kernel = ctypes.WinDLL("kernel32", use_last_error=True)
            kernel.GetTickCount64.restype = ctypes.c_ulonglong
            boot_time = time.time() - kernel.GetTickCount64() / 1000
            pending = self.store.get("reboot_pending")
            if pending and pending["scheduled_at"] < boot_time:
                self.store.set("reboot_pending", None)
                self.store.set("reboot_challenge", None)

    @staticmethod
    def _launch(spec):
        if os.name != "nt":
            raise RuntimeError("Windows application launch is unavailable.")
        if "app_id" in spec:
            os.startfile("shell:AppsFolder\\" + spec["app_id"])
        elif spec.get("url") == "http://127.0.0.1:8769/":
            os.startfile(spec["url"])
        else:
            path = Path(spec["path"])
            if not path.is_file() or path.suffix.lower() != ".exe":
                raise RuntimeError("The configured application was not found.")
            subprocess.Popen([str(path)], shell=False, creationflags=subprocess.CREATE_NEW_PROCESS_GROUP)

    @staticmethod
    def _run(arguments):
        result = subprocess.run(
            arguments, shell=False, capture_output=True,
            creationflags=subprocess.CREATE_NO_WINDOW, timeout=10,
        )
        if result.returncode:
            raise RuntimeError("Windows rejected the requested system action.")

    def status(self):
        observed = self.inspector(self.settings)
        saved = self.store.get_many(("watchdog_enabled", "codex_last_recovery", "reboot_pending"))
        return {
            "codex": observed,
            "watchdog_enabled": saved.get("watchdog_enabled", self.settings["codex_watchdog_enabled"]),
            "last_recovery_at": saved.get("codex_last_recovery"),
            "reboot_pending": bool(saved.get("reboot_pending")),
            "apps": {key: value["label"] for key, value in self.apps.items()},
        }

    def open_app(self, alias):
        if alias not in self.apps:
            raise ValueError("App not configured. Available: " + ", ".join(self.apps))
        if alias == "codex":
            state = self.inspector(self.settings)["state"]
            if state == "running":
                return "Codex is already running."
            if state == "unknown":
                raise RuntimeError("I cannot verify Codex's state, so I will not launch a duplicate.")
        self.launcher(self.apps[alias])
        return f"Requested Windows to open {self.apps[alias]['label']}."

    def request_reboot(self, owner):
        code = f"{secrets.randbelow(1000000):06d}"
        self.store.set("reboot_challenge", {
            "owner": owner, "hash": hashlib.sha256(code.encode()).hexdigest(),
            "expires": self.clock() + 120,
        })
        return (
            "A reboot will close your applications. Save your work first.\n"
            f"To schedule a reboot in 60 seconds, type /confirm_reboot {code} within two minutes.\n"
            "Nothing has been scheduled yet."
        )

    def confirm_reboot(self, owner, code):
        challenge = self.store.get("reboot_challenge")
        if (
            not challenge or challenge["owner"] != owner
            or self.clock() > challenge["expires"]
            or not hmac.compare_digest(challenge["hash"], hashlib.sha256(code.encode()).hexdigest())
        ):
            raise ValueError("The reboot confirmation is invalid or expired. Nothing was scheduled.")
        self.store.set("reboot_challenge", None)
        self.runner([self.shutdown, "/r", "/t", "60", "/c", "Vega: reboot explicitly confirmed by the owner"])
        self.store.set("reboot_pending", {"owner": owner, "scheduled_at": self.clock()})
        return "Windows accepted a reboot in 60 seconds. Use /cancel_reboot to cancel before it begins."

    def cancel_reboot(self, owner):
        pending = self.store.get("reboot_pending")
        self.store.set("reboot_challenge", None)
        if not pending:
            return "No Vega reboot is scheduled; any pending confirmation was cleared."
        if pending["owner"] != owner:
            raise ValueError("This reboot belongs to a different owner.")
        self.runner([self.shutdown, "/a"])
        self.store.set("reboot_pending", None)
        return "Windows accepted cancellation of the pending reboot."

    def watchdog_tick(self):
        now = self.clock()
        if not self.store.get("watchdog_enabled", self.settings["codex_watchdog_enabled"]):
            self.store.set("codex_missing_since", None)
            return "disabled"
        if self.store.get("reboot_pending"):
            return "reboot-pending"
        observed = self.inspector(self.settings)
        if observed["state"] != "absent":
            self.store.set("codex_missing_since", None)
            return observed["state"]
        missing = self.store.get("codex_missing_since")
        if missing is None:
            self.store.set("codex_missing_since", now)
            return "grace"
        if now - missing < self.settings["codex_missing_grace_seconds"]:
            return "grace"
        last = self.store.get("codex_last_recovery", 0)
        if now - last < self.settings["codex_restart_cooldown_seconds"]:
            return "cooldown"
        recent = [value for value in self.store.get("codex_recovery_times", []) if now - value < 3600]
        if len(recent) >= self.settings["codex_max_restarts_per_hour"]:
            self.store.enqueue(
                "manual:watchdog-limit:" + str(int(now // 3600)), "coordinator",
                "Vega reached the Codex recovery retry limit. I have stopped relaunching it; inspect the app before another attempt.",
            )
            return "limit"
        # Persist admission before launch; a Vega crash cannot produce an immediate launch loop.
        self.store.set("codex_last_recovery", now)
        self.store.set("codex_recovery_times", recent + [now])
        result = self.open_app("codex")
        self.store.enqueue(
            "manual:codex-recovery:" + str(int(now)), "coordinator",
            "Vega detected that the Codex desktop app was absent. " + result
            + " Existing task files were preserved; task execution has not been assumed resumed.",
        )
        return "launch-requested"
