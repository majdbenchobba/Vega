"""Codex CLI fallback for the native Codex workspace.

The desktop app changed its app-tools pipe in October 2026. The supported CLI
keeps its own session files and can queue a message to an existing thread.
This module only reads local session JSONL and calls `codex queue`; it never
starts the desktop app or bypasses the thread's own model/permissions.
"""
from __future__ import annotations

from datetime import datetime
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import time

from codex_activity import public_text


class CodexCliUnavailable(RuntimeError):
    pass


IDENTITY = re.compile(r"^[a-f0-9-]{36}$")
MAX_TEXT = 12_000
ROOT = Path(__file__).resolve().parent
DEFAULT_CODEX = Path.home() / ".codex" / "plugins" / ".plugin-appserver" / "codex.exe"
SESSIONS = Path.home() / ".codex" / "sessions"
CONTEXT_TAGS = (
    "<environment_context>", "<external_codex_apps_open_page>",
    "<user_instructions>", "<developer_instructions>", "<app-context>",
    "<in-app-browser-context", "<permissions instructions>",
    "<collaboration_mode>", "# AGENTS.md instructions",
)


def _text_parts(value):
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return "\n".join(
            part.get("text", "") for part in value
            if isinstance(part, dict) and isinstance(part.get("text"), str)
        )
    return ""


def _is_real_user_text(text):
    stripped = text.strip()
    return bool(stripped) and not any(stripped.startswith(tag) for tag in CONTEXT_TAGS)


def _timestamp(value):
    if isinstance(value, (int, float)):
        return float(value)
    if not isinstance(value, str):
        return time.time()
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except (ValueError, OverflowError):
        return time.time()


class CodexCliBridge:
    """Same callable contract as the desktop bridge, backed by the Codex CLI."""

    def __init__(self, *, codex=None, sessions=None, runner=None):
        self.codex = Path(codex or os.environ.get("CODEX_CLI_PATH") or DEFAULT_CODEX)
        self.sessions = Path(sessions or SESSIONS)
        self.runner = runner or self._run
        self._cache = None
        self._cache_at = 0.0

    def _run(self, arguments, *, cwd, timeout):
        try:
            result = subprocess.run(
                [str(self.codex), *arguments], cwd=cwd, capture_output=True,
                text=True, encoding="utf-8", timeout=timeout,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except (OSError, subprocess.TimeoutExpired):
            raise CodexCliUnavailable("The Codex CLI is unavailable.") from None
        if result.returncode:
            raise CodexCliUnavailable((result.stderr or result.stdout or "The Codex CLI rejected the request.").strip()[:600])
        return result.stdout.strip()

    def _meta(self, path):
        try:
            with path.open("r", encoding="utf-8", errors="replace") as handle:
                first = handle.readline(128000)
            row = json.loads(first)
        except (OSError, json.JSONDecodeError):
            return None
        if row.get("type") != "session_meta" or not isinstance(row.get("payload"), dict):
            return None
        payload = row["payload"]
        identity = payload.get("id") or payload.get("session_id")
        cwd = payload.get("cwd")
        if not isinstance(identity, str) or not IDENTITY.fullmatch(identity):
            return None
        if not isinstance(cwd, str) or not cwd.strip():
            return None
        return {
            "id": identity,
            "cwd": cwd,
            "at": _timestamp(payload.get("timestamp")),
            "path": path,
        }

    def _title(self, path, identity):
        try:
            with path.open("r", encoding="utf-8", errors="replace") as handle:
                for _ in range(240):
                    line = handle.readline()
                    if not line:
                        break
                    try:
                        row = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    payload = row.get("payload") if isinstance(row, dict) else None
                    if not isinstance(payload, dict) or payload.get("type") != "message" or payload.get("role") != "user":
                        continue
                    text = _text_parts(payload.get("content"))
                    if _is_real_user_text(text):
                        safe = public_text(text).strip().replace("\n", " ")
                        return safe[:80] or "Codex conversation"
        except OSError:
            pass
        return f"Codex conversation {identity[:8]}"

    def _list(self):
        now = time.time()
        if self._cache and now - self._cache_at < 4:
            return self._cache
        rows = []
        if self.sessions.is_dir():
            for path in self.sessions.rglob("rollout-*.jsonl"):
                try:
                    modified = path.stat().st_mtime
                except OSError:
                    continue
                rows.append((modified, path))
        rows.sort(reverse=True)
        sessions, seen = [], set()
        for modified, path in rows[:80]:
            meta = self._meta(path)
            if not meta or meta["id"] in seen:
                continue
            seen.add(meta["id"])
            meta["modified"] = modified
            meta["title"] = self._title(path, meta["id"])
            sessions.append(meta)
            if len(sessions) >= 50:
                break
        self._cache = sessions
        self._cache_at = now
        return sessions

    def _find(self, identity):
        for session in self._list():
            if session["id"] == identity:
                return session
        return None

    def _read_tail(self, path, limit=8 * 1024 * 1024):
        try:
            size = path.stat().st_size
            with path.open("rb") as handle:
                if size > limit:
                    handle.seek(size - limit)
                    handle.readline()
                data = handle.read()
        except OSError:
            return []
        rows = []
        for line in data.decode("utf-8", errors="replace").splitlines():
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                continue
        return rows

    def _read(self, identity):
        session = self._find(identity)
        if not session:
            raise CodexCliUnavailable("This Codex conversation is no longer available in the local session store.")
        messages = []
        for row in self._read_tail(session["path"]):
            payload = row.get("payload") if isinstance(row, dict) else None
            if not isinstance(payload, dict) or payload.get("type") != "message":
                continue
            role = payload.get("role")
            if role == "user":
                text = _text_parts(payload.get("content"))
                if not _is_real_user_text(text):
                    continue
                item = {
                    "type": "userMessage",
                    "id": str(payload.get("id", len(messages))),
                    "content": [{"type": "text", "text": public_text(text)}],
                }
                message_role = "user"
            elif role == "assistant":
                text = _text_parts(payload.get("content"))
                if not text.strip():
                    continue
                phase = payload.get("phase")
                item = {
                    "type": "agentMessage", "phase": "final" if phase in (None, "final_answer", "final") else "commentary",
                    "text": public_text(text), "id": str(payload.get("id", len(messages))),
                }
                message_role = "assistant"
            else:
                continue
            messages.append((message_role, item, _timestamp(row.get("timestamp"))))
        turns = [{"items": [item], "completedAt": at} for _role, item, at in messages]
        turns.reverse()
        return {"thread": {"id": identity}, "turns": turns, "page": {"hasMore": False}}

    def _send(self, identity, text):
        if not isinstance(text, str) or not text.strip() or len(text) > MAX_TEXT:
            raise CodexCliUnavailable("Enter a prompt of up to 12,000 characters.")
        session = self._find(identity)
        if not session:
            raise CodexCliUnavailable("This Codex conversation is no longer available in the local session store.")
        output = self.runner(
            ["queue", "--thread", identity, "--message", text, "--cd", session["cwd"]],
            cwd=session["cwd"], timeout=30,
        )
        if "Queued message" not in output:
            raise CodexCliUnavailable("The Codex CLI did not confirm the queued message.")
        return {"threadId": identity, "status": "accepted"}

    def __call__(self, operation, **values):
        if operation == "snapshot":
            projects, chats, seen = [], [], set()
            for session in self._list():
                cwd = str(Path(session["cwd"]))
                if cwd not in seen:
                    seen.add(cwd)
                    projects.append({
                        "projectId": cwd, "label": Path(cwd).name or cwd,
                        "hostId": "local", "projectKind": "local",
                    })
                chats.append({
                    "id": session["id"], "title": session["title"],
                    "hostId": "local", "kind": "codex", "projectId": cwd,
                    "status": "idle", "running": False, "updatedAt": session["modified"],
                    "cwd": cwd, "isUnread": False,
                })
            return {"projects": {"projects": projects},
                    "chats": {"threads": chats, "pinnedThreads": []}}
        if operation == "read":
            return self._read(values.get("threadId"))
        if operation == "send":
            if values.get("attachment_ids"):
                raise CodexCliUnavailable("The Codex CLI queue path cannot attach files. Nothing was sent.")
            return self._send(values.get("threadId"), values.get("text"))
        raise CodexCliUnavailable("The Codex CLI does not support that operation.")
