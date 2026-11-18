"""Read-only VS Code Agent Host Protocol integration."""
from __future__ import annotations

from datetime import datetime
import ctypes
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import threading
import time
from uuid import NAMESPACE_URL, uuid5

from companion import ConflictError


IDENTITY = re.compile(r"^[a-f0-9-]{36}$")
TOKEN = re.compile(r"^[A-Za-z0-9_-]{16,512}={0,2}$")
PIPE_PREFIX = r"\\.\pipe\vscode-agent-host-"
ROOT = Path(__file__).resolve().parent
MAX_ENDPOINTS = 8
MAX_ENTRY_BYTES = 64 * 1024
MAX_LEGACY_BYTES = 1024 * 1024


class VscodeAgentsUnavailable(RuntimeError):
    pass


def _pid_is_alive(pid: int) -> bool:
    if os.name == "nt":
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.OpenProcess.argtypes = (ctypes.c_ulong, ctypes.c_int, ctypes.c_ulong)
        kernel.OpenProcess.restype = ctypes.c_void_p
        kernel.GetExitCodeProcess.argtypes = (ctypes.c_void_p, ctypes.POINTER(ctypes.c_ulong))
        kernel.CloseHandle.argtypes = (ctypes.c_void_p,)
        handle = kernel.OpenProcess(0x1000, False, pid)
        if not handle:
            return ctypes.get_last_error() != 87
        try:
            exit_code = ctypes.c_ulong()
            return bool(kernel.GetExitCodeProcess(handle, ctypes.byref(exit_code))) and exit_code.value == 259
        finally:
            kernel.CloseHandle(handle)
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def _entry_identity(entry: dict) -> tuple[str, int, str]:
    return entry["type"], entry["pid"], entry["instanceId"]


def _validate_entry(value: object) -> dict | None:
    if not isinstance(value, dict) or value.get("schemaVersion") != 2 or value.get("type") != "editor":
        return None
    pid, instance_id = value.get("pid"), value.get("instanceId")
    endpoint = value.get("endpoint")
    token = value.get("connectionToken")
    if type(pid) is not int or pid < 1 or not isinstance(instance_id, str):
        return None
    if not re.fullmatch(r"[A-Za-z0-9_-]{8,160}", instance_id):
        return None
    if not isinstance(token, str) or not TOKEN.fullmatch(token):
        return None
    if not isinstance(endpoint, dict) or endpoint.get("type") != "socket":
        return None
    path = endpoint.get("path")
    if not isinstance(path, str) or len(path) > 512 or "\0" in path or not path.lower().startswith(PIPE_PREFIX):
        return None
    if not _pid_is_alive(pid):
        return None
    identity = _entry_identity(value)
    entry_id = hashlib.sha256("\0".join(map(str, identity)).encode("utf-8")).hexdigest()
    return {
        "entry_id": entry_id,
        "pid": pid,
        "instance_id": instance_id,
        "path": path,
        "token": token,
    }


class VscodeAgentsBridge:
    """Discover local editor endpoints and use only read-only AHP requests."""

    def __init__(self, node=None, root=ROOT, user_data_dir=None, runner=subprocess.run):
        self.node = node or shutil.which("node") or r"C:\Program Files\nodejs\node.exe"
        self.root = Path(root)
        self.user_data_dir = Path(user_data_dir) if user_data_dir else None
        self.runner = runner

    def _user_data_path(self) -> Path:
        configured = self.user_data_dir or os.environ.get("VEGA_VSCODE_USER_DATA_DIR")
        if configured:
            return Path(configured)
        appdata = os.environ.get("APPDATA")
        if appdata:
            return Path(appdata) / "Code"
        return Path.home() / "AppData" / "Roaming" / "Code"

    @staticmethod
    def _read_json(path: Path, limit: int) -> object:
        try:
            if path.stat().st_size > limit:
                return None
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            return None

    def _discover_endpoints(self) -> tuple[list[dict], bool]:
        directory = self._user_data_path() / "agent-host" / "local-endpoint"
        entries_dir = directory / "entries"
        if not entries_dir.is_dir() and not (directory / "metadata.json").is_file():
            raise VscodeAgentsUnavailable(
                "No VS Code Agent Host registry found. Open VS Code with a chat, then refresh."
            )

        candidates: list[object] = []
        if entries_dir.is_dir():
            for path in sorted(entries_dir.glob("*.json")):
                value = self._read_json(path, MAX_ENTRY_BYTES)
                if value is not None:
                    candidates.append(value)
        legacy = self._read_json(directory / "metadata.json", MAX_LEGACY_BYTES)
        if isinstance(legacy, list):
            candidates.extend(legacy)

        endpoints: dict[tuple[str, int, str], dict] = {}
        for value in candidates:
            entry = _validate_entry(value)
            if entry is None:
                continue
            key = ("editor", entry["pid"], entry["instance_id"])
            endpoints.setdefault(key, entry)
        if not endpoints:
            raise VscodeAgentsUnavailable(
                "No live VS Code editor endpoint found. Open a VS Code chat, then refresh."
            )
        values = list(endpoints.values())
        truncated = len(values) > MAX_ENDPOINTS
        return values[:MAX_ENDPOINTS], truncated

    def _invoke(self, operation: str, *, endpoint: dict, **values) -> dict:
        if operation not in {"list", "read"}:
            raise ValueError("Unsupported VS Code operation.")
        payload = {
            "operation": operation,
            "endpoint": {"path": endpoint["path"], "token": endpoint["token"]},
            **values,
        }
        try:
            result = self.runner(
                [self.node, str(self.root / "vscode_ahp_readonly.mjs")],
                input=json.dumps(payload, ensure_ascii=False),
                capture_output=True,
                text=True,
                encoding="utf-8",
                timeout=20,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except (OSError, subprocess.TimeoutExpired):
            raise VscodeAgentsUnavailable(
                "VS Code Agent Host did not respond. Open VS Code and refresh."
            ) from None
        lines = [line for line in result.stdout.splitlines() if line.strip()]
        try:
            data = json.loads(lines[-1]) if lines else {}
        except json.JSONDecodeError:
            data = {}
        if result.returncode or not isinstance(data, dict) or data.get("error"):
            message = data.get("error") if isinstance(data, dict) else None
            allowed = {
                "The VS Code Agent Host did not respond.",
                "The VS Code Agent Host rejected the request.",
                "The VS Code Agent Host returned invalid data.",
            }
            raise VscodeAgentsUnavailable(message if isinstance(message, str) and message in allowed else
                                          "VS Code Agent Host did not respond. Open VS Code and refresh.")
        value = data.get("result")
        if not isinstance(value, dict):
            raise VscodeAgentsUnavailable("VS Code Agent Host returned invalid data.")
        return value

    def snapshot(self) -> dict:
        endpoints, truncated = self._discover_endpoints()
        chats, source_failures, partial_sources = [], 0, 0
        for endpoint in endpoints:
            try:
                result = self._invoke("list", endpoint=endpoint)
            except VscodeAgentsUnavailable:
                source_failures += 1
                continue
            if result.get("partial"):
                partial_sources += 1
            remote_chats = result.get("chats")
            if not isinstance(remote_chats, list):
                source_failures += 1
                continue
            for item in remote_chats:
                if not isinstance(item, dict):
                    continue
                chat_uri, session_uri = item.get("chat_uri"), item.get("session_uri")
                title = item.get("title")
                if not all(isinstance(value, str) and value for value in (chat_uri, session_uri, title)):
                    continue
                identity = str(uuid5(NAMESPACE_URL, f"ahp:{endpoint['entry_id']}:{chat_uri}"))
                status = item.get("status")
                if type(status) is int:
                    status_text = "active" if status & 8 else "idle"
                else:
                    status_text = "available"
                updated_at = _timestamp(item.get("modified_at"), time.time())
                version = hashlib.sha256(
                    f"{endpoint['entry_id']}\0{chat_uri}\0{session_uri}\0{title}\0{updated_at}".encode("utf-8")
                ).hexdigest()
                chats.append({
                    "id": identity,
                    "title": title[:240],
                    "host_id": "local",
                    "kind": "vscode_agents",
                    "status": status_text,
                    "project_id": None,
                    "running": status_text == "active",
                    "updated_at": updated_at,
                    "pinned": False,
                    "unread": False,
                    "can_send": False,
                    "target_version": version,
                    "_source": endpoint,
                    "_session_uri": session_uri,
                    "_chat_uri": chat_uri,
                })
        if not chats and source_failures == len(endpoints):
            raise VscodeAgentsUnavailable(
                "No VS Code Agent Host responded. Open an active VS Code chat and refresh."
            )
        unavailable_count = source_failures + partial_sources + int(truncated)
        return {
            "projects": {"projects": []},
            "chats": {"threads": chats, "pinnedThreads": []},
            "unavailable": bool(unavailable_count),
            "unavailable_count": unavailable_count,
            "work_paused": False,
        }

    def read(self, source: dict, session_uri: str, chat_uri: str) -> dict:
        return self._invoke(
            "read",
            endpoint=source,
            session_uri=session_uri,
            chat_uri=chat_uri,
        )


class VscodeAgentsWorkspace:
    """Owner-scoped, read-only VS Code workspace."""

    def __init__(self, store, *, bridge=None, clock=time.time):
        self.store = store
        self.bridge = bridge or VscodeAgentsBridge()
        self.clock = clock
        self.lock = threading.RLock()
        self.cached = None
        self.sources: dict[str, tuple[dict, str, str]] = {}

    def _principal(self, owner):
        with self.store.db() as db:
            settings = {row["key"]: json.loads(row["value"]) for row in db.execute(
                "SELECT key,value FROM settings WHERE key IN ('owner_id','owner_chat_id')")}
            if type(owner) is not int or settings.get("owner_id") != owner or settings.get("owner_chat_id") != owner:
                raise PermissionError("The paired owner is required.")
            return owner, 0

    def snapshot(self, owner, *, fresh=False):
        principal = self._principal(owner)
        with self.lock:
            if not fresh and self.cached and self.cached[0] == principal and self.clock() - self.cached[1]["snapshot_at"] < 4:
                return self.cached[1]
            raw = self.bridge.snapshot()
            if self._principal(owner) != principal:
                raise PermissionError("The owner context changed.")
            chats, sources = [], {}
            for item in raw["chats"]["threads"]:
                source = item.pop("_source")
                session_uri = item.pop("_session_uri")
                chat_uri = item.pop("_chat_uri")
                sources[item["id"]] = source, session_uri, chat_uri
                chats.append(item)
            value = {
                "projects": raw["projects"]["projects"],
                "chats": chats,
                "snapshot_at": self.clock(),
                "unavailable": raw.get("unavailable", False),
                "unavailable_count": raw.get("unavailable_count", 0),
                "work_paused": bool(self.store.get("work_paused", False)),
            }
            self.sources = sources
            self.cached = principal, value
            return value

    def _chat(self, owner, identity, *, fresh=False):
        if not isinstance(identity, str) or not IDENTITY.fullmatch(identity):
            raise ValueError("Invalid chat.")
        candidates = [chat for chat in self.snapshot(owner, fresh=fresh)["chats"]
                      if chat["id"] == identity and chat["host_id"] == "local"
                      and chat["kind"] == "vscode_agents"]
        if len(candidates) != 1:
            raise ConflictError("This VS Code chat is no longer available. Refresh the list.")
        return candidates[0]

    def read(self, owner, identity):
        principal = self._principal(owner)
        chat = self._chat(owner, identity)
        with self.lock:
            source = self.sources.get(identity)
        if source is None:
            raise ConflictError("This VS Code chat is no longer available. Refresh the list.")
        raw = self.bridge.read(*source)
        if self._principal(owner) != principal:
            raise PermissionError("The owner context changed.")
        return {
            "chat": chat,
            "messages": raw.get("messages", []),
            "has_more": raw.get("has_more", False),
            "receipts": [],
            "snapshot_at": self.clock(),
        }

    def send(self, owner, payload):
        self._principal(owner)
        raise PermissionError("VS Code Agent Host access is read-only in Vega.")

    def receipts(self, owner, identity=None):
        self._principal(owner)
        if identity is not None and not IDENTITY.fullmatch(str(identity)):
            raise ValueError("Invalid chat.")
        return []


def _timestamp(value, fallback: float) -> float:
    if not isinstance(value, str):
        return fallback
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except (ValueError, OverflowError):
        return fallback
