"""Native Antigravity conversations through the local CDP endpoint and transcript logs."""
from __future__ import annotations

from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import threading
import time

from companion import ConflictError
from memory_policy import scrub_secrets


IDENTITY = re.compile(r"^[a-f0-9-]{36}$")
MAX_PROMPT_CHARS = 12_000
ROOT = Path(__file__).resolve().parent
TRANSCRIPTS = Path.home() / ".gemini" / "antigravity" / "brain"


class AntigravityUnavailable(RuntimeError):
    pass


class AntigravityBridge:
    def __init__(self, node=None, root=ROOT):
        self.node = node or shutil.which("node") or r"C:\Program Files\nodejs\node.exe"
        self.root = Path(root)

    def __call__(self, operation, **values):
        payload = {"operation": operation, **values}
        try:
            result = subprocess.run(
                [self.node, str(self.root / "antigravity-cdp.mjs")],
                input=json.dumps(payload, ensure_ascii=False),
                capture_output=True, text=True, encoding="utf-8", timeout=20,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except (OSError, subprocess.TimeoutExpired):
            raise AntigravityUnavailable("Antigravity did not answer. Open its window and retry.") from None
        lines = [line for line in result.stdout.splitlines() if line.strip()]
        if not lines:
            raise AntigravityUnavailable("Antigravity returned no result.")
        try:
            data = json.loads(lines[-1])
        except json.JSONDecodeError:
            raise AntigravityUnavailable("Antigravity returned an unreadable result.") from None
        if result.returncode or not isinstance(data, dict) or data.get("error") or "result" not in data:
            message = data.get("error") if isinstance(data, dict) else None
            raise AntigravityUnavailable(str(message or "Antigravity is unavailable.")[:500])
        return data["result"]

    def snapshot(self):
        return self("snapshot")

    def read(self, identity):
        if not IDENTITY.fullmatch(identity):
            raise AntigravityUnavailable("Invalid Antigravity conversation.")
        base = TRANSCRIPTS / identity / ".system_generated" / "logs"
        path = base / "transcript.jsonl"
        if not path.is_file():
            path = base / "transcript_full.jsonl"
        try:
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            raise AntigravityUnavailable("The conversation transcript is not available yet.") from None
        messages = []
        for line in lines:
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            source, kind = row.get("source"), row.get("type")
            content = row.get("content")
            if not isinstance(content, str) or not content.strip():
                continue
            if source == "USER_EXPLICIT" and kind == "USER_INPUT":
                match = re.search(r"<USER_REQUEST>\s*(.*?)\s*</USER_REQUEST>", content, re.S)
                text = match.group(1) if match else content
                role = "user"
            elif source == "MODEL" and kind == "PLANNER_RESPONSE":
                text = content
                role = "assistant"
            else:
                continue
            text = scrub_secrets(text[:6000]).strip()
            if not text:
                continue
            messages.append({
                "id": str(row.get("step_index", len(messages))),
                "role": role,
                "text": text,
                "phase": "final",
                "at": _timestamp(row.get("created_at")),
            })
        return {"messages": messages[-24:], "has_more": len(messages) > 24}

    def send(self, identity, text):
        return self("send", chatId=identity, text=text)


def _timestamp(value):
    if not isinstance(value, str):
        return time.time()
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except (ValueError, OverflowError):
        return time.time()


class AntigravityWorkspace:
    """Owner-scoped Antigravity workspace, mirroring the native Codex workspace."""

    def __init__(self, store, *, bridge=None, clock=time.time):
        self.store = store
        self.bridge = bridge or AntigravityBridge()
        self.clock = clock
        self.lock = threading.RLock()
        self.cached = None
        with store.db() as db:
            db.execute("""CREATE TABLE IF NOT EXISTS antigravity_prompts(
                owner INTEGER NOT NULL, request_key TEXT NOT NULL, thread_id TEXT NOT NULL,
                target_version TEXT NOT NULL, title TEXT NOT NULL, text TEXT NOT NULL,
                state TEXT NOT NULL, created_at REAL NOT NULL, updated_at REAL NOT NULL,
                receipt TEXT, attachments TEXT NOT NULL DEFAULT '[]', PRIMARY KEY(owner,request_key))""")
            if "attachments" not in {r["name"] for r in db.execute("PRAGMA table_info(antigravity_prompts)")}:
                db.execute("ALTER TABLE antigravity_prompts ADD COLUMN attachments TEXT NOT NULL DEFAULT '[]'")
            db.execute("UPDATE antigravity_prompts SET state='uncertain' WHERE state='sending'")

    def _principal(self, owner):
        with self.store.db() as db:
            settings = {r["key"]: json.loads(r["value"]) for r in db.execute(
                "SELECT key,value FROM settings WHERE key IN ('owner_id','owner_chat_id')")}
            if type(owner) is not int or settings.get("owner_id") != owner or settings.get("owner_chat_id") != owner:
                raise PermissionError("The paired owner is required.")
            return owner, 0

    @staticmethod
    def _version(chat):
        return hashlib.sha256(json.dumps(
            [chat["id"], chat["title"], chat.get("project")], separators=(",", ":"),
        ).encode()).hexdigest()

    def snapshot(self, owner, *, fresh=False):
        principal = self._principal(owner)
        with self.lock:
            if not fresh and self.cached and self.cached[0] == principal and self.clock() - self.cached[1]["snapshot_at"] < 4:
                return self.cached[1]
            raw = self.bridge.snapshot()
            if self._principal(owner) != principal:
                raise PermissionError("The owner context changed.")
            projects, chats, seen = [], [], set()
            for project in raw.get("projects") or []:
                if isinstance(project, dict) and isinstance(project.get("title"), str) and project["title"].strip():
                    projects.append({"id": project["title"], "title": project["title"]})
            for chat in raw.get("chats") or []:
                if not isinstance(chat, dict) or not IDENTITY.fullmatch(str(chat.get("id", ""))):
                    continue
                title = chat.get("title")
                if not isinstance(title, str) or not title.strip() or chat["id"] in seen:
                    continue
                seen.add(chat["id"])
                item = {
                    "id": chat["id"], "title": title.strip(), "host_id": "local", "kind": "antigravity",
                    "status": "idle", "project": chat.get("project"),
                    "project_id": chat.get("project"),
                    "running": False, "updated_at": self.clock(), "pinned": bool(chat.get("pinned")),
                    "unread": False, "can_send": True,
                }
                item["target_version"] = self._version(item)
                chats.append(item)
            value = {
                "projects": projects,
                "chats": chats,
                "snapshot_at": self.clock(),
                "unavailable": False,
                "work_paused": bool(self.store.get("work_paused", False)),
            }
            self.cached = principal, value
            return value

    def _chat(self, owner, identity, *, fresh=False):
        if not isinstance(identity, str) or not IDENTITY.fullmatch(identity):
            raise ValueError("Invalid chat.")
        candidates = [c for c in self.snapshot(owner, fresh=fresh)["chats"]
                      if c["id"] == identity and c["kind"] == "antigravity"]
        if len(candidates) != 1:
            raise ConflictError("This Antigravity conversation is no longer available. Refresh the list.")
        return candidates[0]

    def read(self, owner, identity):
        principal = self._principal(owner)
        chat = self._chat(owner, identity)
        raw = self.bridge.read(identity)
        if self._principal(owner) != principal:
            raise PermissionError("The owner context changed.")
        return {
            "chat": chat,
            "messages": raw.get("messages", []),
            "has_more": raw.get("has_more", False),
            "receipts": [],
            "snapshot_at": self.clock(),
        }

    @staticmethod
    def _receipt(row):
        return {k: row[k] for k in ("request_key", "thread_id", "title", "text", "state", "created_at", "updated_at")} | {
            "attachments": json.loads(row["attachments"]) if row["attachments"] else []}

    def send(self, owner, payload):
        principal = self._principal(owner)
        if not isinstance(payload, dict) or not {"request_key", "thread_id", "target_version", "text"} <= set(payload):
            raise ValueError("Invalid chat prompt.")
        key, identity, version, text = (payload[k] for k in ("request_key", "thread_id", "target_version", "text"))
        if not isinstance(text, str) or not text.strip() or len(text) > MAX_PROMPT_CHARS:
            raise ValueError("Enter a prompt of up to 12,000 characters.")
        if not isinstance(version, str) or not re.fullmatch(r"[a-f0-9]{64}", version):
            raise ValueError("Invalid target version.")
        if not isinstance(identity, str) or not IDENTITY.fullmatch(identity):
            raise ValueError("Invalid chat.")

        with self.lock:
            with self.store.db() as db:
                existing = db.execute("SELECT * FROM antigravity_prompts WHERE owner=? AND request_key=?",
                                      (owner, key)).fetchone()
            if existing:
                if any(existing[k] != payload[k] for k in ("thread_id", "target_version", "text")):
                    raise ConflictError("This receipt belongs to a different prompt.")
                return self._receipt(existing)
            chat = self._chat(owner, identity, fresh=True)
            if not chat["can_send"] or chat["target_version"] != version:
                raise ConflictError("The selected conversation changed. Refresh before sending.")
            if self._principal(owner) != principal:
                raise PermissionError("The owner context changed.")
            at = self.clock()
            with self.store.db() as db:
                db.execute("""INSERT INTO antigravity_prompts
                           (owner,request_key,thread_id,target_version,title,text,state,created_at,updated_at,receipt,attachments)
                           VALUES(?,?,?,?,?,?,'sending',?,?,NULL,'[]')""",
                           (owner, key, identity, version, chat["title"], text, at, at))

        try:
            result = self.bridge.send(identity, text)
            state = "accepted"
            receipt = json.dumps({"accepted_by": "antigravity", "thread_id": identity,
                                  "response_keys": sorted(result)}, separators=(",", ":"))
        except Exception:
            state, receipt = "uncertain", None

        with self.store.db() as db:
            db.execute("UPDATE antigravity_prompts SET state=?,receipt=?,updated_at=? WHERE owner=? AND request_key=?",
                       (state, receipt, self.clock(), owner, key))
            row = db.execute("SELECT * FROM antigravity_prompts WHERE owner=? AND request_key=?", (owner, key)).fetchone()
        self._principal(owner)
        self.cached = None
        return self._receipt(row)

    def receipts(self, owner, identity=None):
        self._principal(owner)
        with self.store.db() as db:
            rows = db.execute("SELECT * FROM antigravity_prompts WHERE owner=?"
                               + (" AND thread_id=?" if identity else "") + " ORDER BY created_at DESC LIMIT 30",
                               (owner, identity) if identity else (owner,)).fetchall()
            return [self._receipt(row) for row in rows]
