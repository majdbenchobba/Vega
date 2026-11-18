"""Current desktop projects/chats and explicit paired-owner prompt delivery.

Uses the running desktop, not the legacy project routing queue. No background
consumer, model process, notification, archive mutation or permission override.
"""
from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import threading
import time

from companion import ConflictError
from codex_activity import public_text
from codex_cli import CodexCliBridge, CodexCliUnavailable
from desktop_attachments import DesktopAttachments

ROOT = Path(__file__).resolve().parent
IDENTITY = re.compile(r"^[a-f0-9-]{36}$")


class DesktopUnavailable(RuntimeError):
    pass


class DesktopBridge:
    def __call__(self, operation, **values):
        config = json.loads((ROOT / "codex-session-source.json").read_text(encoding="utf-8"))
        payload = {"operation": operation, "callerThreadId": config["thread_id"], **values}
        try:
            result = subprocess.run([
                str(ROOT / ".n8n-runtime/node-v24.21.0-win-x64/node.exe"),
                str(ROOT / "codex-desktop-bridge.mjs"),
            ], input=json.dumps(payload, ensure_ascii=False), capture_output=True,
                text=True, encoding="utf-8", timeout=20,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            data = json.loads(result.stdout)
            if result.returncode or "error" in data or not isinstance(data.get("result"), dict):
                raise DesktopUnavailable("Open Codex on your laptop to reconnect.")
            return data["result"]
        except (OSError, ValueError, subprocess.TimeoutExpired):
            raise DesktopUnavailable("The Codex connection is unavailable.") from None


def target_version(chat):
    return hashlib.sha256(json.dumps([
        chat["id"], chat.get("hostId"), chat.get("kind"), chat.get("projectId"), chat.get("cwd"),
    ], separators=(",", ":")).encode()).hexdigest()


class HybridCodexBridge:
    """Use the desktop pipe while it works, then the supported Codex CLI."""

    def __init__(self, desktop=None, cli=None):
        self.desktop = desktop or DesktopBridge()
        self.cli = cli or CodexCliBridge()

    def __call__(self, operation, **values):
        try:
            return self.desktop(operation, **values)
        except DesktopUnavailable:
            try:
                return self.cli(operation, **values)
            except CodexCliUnavailable as error:
                raise DesktopUnavailable(str(error)) from None


class CodexWorkspace:
    def __init__(self, store, *, bridge=None, clock=time.time):
        self.store, self.bridge, self.clock = store, bridge or HybridCodexBridge(), clock
        self.lock = threading.RLock()
        self.cached = None
        self.attachments = DesktopAttachments(store, clock=clock)
        with store.db() as db:
            db.execute("""CREATE TABLE IF NOT EXISTS desktop_chat_prompts(
                owner INTEGER NOT NULL, request_key TEXT NOT NULL, thread_id TEXT NOT NULL,
                target_version TEXT NOT NULL, title TEXT NOT NULL, text TEXT NOT NULL,
                state TEXT NOT NULL, created_at REAL NOT NULL, updated_at REAL NOT NULL,
                receipt TEXT, PRIMARY KEY(owner,request_key))""")
            if "attachments" not in {r["name"] for r in db.execute("PRAGMA table_info(desktop_chat_prompts)")}:
                db.execute("ALTER TABLE desktop_chat_prompts ADD COLUMN attachments TEXT NOT NULL DEFAULT '[]'")
            # A process stopping after desktop acceptance cannot safely retry.
            db.execute("UPDATE desktop_chat_prompts SET state='uncertain' WHERE state='sending'")

    def _principal(self, owner):
        with self.store.db() as db:
            settings = {r["key"]: json.loads(r["value"]) for r in db.execute(
                "SELECT key,value FROM settings WHERE key IN ('owner_id','owner_chat_id')")}
            if type(owner) is not int or settings.get("owner_id") != owner or settings.get("owner_chat_id") != owner:
                raise PermissionError("The paired owner is required.")
            pref = db.execute("SELECT revision,payload FROM companion_preferences WHERE owner=?", (owner,)).fetchone()
            if pref and json.loads(pref["payload"]).get("project_context_enabled", True) is not True:
                raise PermissionError("Project context is off.")
            return owner, pref["revision"] if pref else 0

    def snapshot(self, owner, *, fresh=False):
        principal = self._principal(owner)
        with self.lock:
            if (not fresh and self.cached and self.cached[0] == principal
                    and self.clock() - self.cached[1]["snapshot_at"] < 4):
                return deepcopy(self.cached[1])
            raw = self.bridge("snapshot")
            if self._principal(owner) != principal:
                raise PermissionError("The owner context changed.")
            project_source, chat_source = raw.get("projects", {}), raw.get("chats", {})
            if not isinstance(project_source.get("projects"), list) or not isinstance(chat_source.get("threads"), list):
                raise DesktopUnavailable("The desktop list could not be read.")
            projects = [{
                "id": p["projectId"], "label": p["label"],
                "host_id": p.get("hostId"), "kind": p.get("projectKind"),
            } for p in project_source["projects"]
                if isinstance(p, dict) and isinstance(p.get("projectId"), str) and isinstance(p.get("label"), str)]
            known_projects = {p["id"] for p in projects}
            chats, seen = [], set()
            for chat in chat_source.get("pinnedThreads", []) + chat_source["threads"]:
                if not isinstance(chat, dict) or not IDENTITY.fullmatch(str(chat.get("id", ""))):
                    continue
                identity = chat["id"]
                host, kind = chat.get("hostId"), chat.get("kind")
                key = (host, kind, identity)
                if key in seen:
                    continue
                seen.add(key)
                title = chat.get("title")
                if not isinstance(title, str):
                    continue
                status = chat.get("status")
                status = status if status in ("active", "idle", "notLoaded", "needsAttention") else "unknown"
                project = chat.get("projectId")
                chats.append({
                    "id": identity, "title": title, "project_id": project,
                    "project_missing": project is not None and project not in known_projects,
                    "host_id": host, "kind": kind, "status": status,
                    "running": status == "active", "updated_at": chat.get("updatedAt"),
                    "pinned": "pinnedIndex" in chat, "unread": chat.get("isUnread") is True,
                    "can_send": kind == "codex" and host == "local" and status != "unknown",
                    "target_version": target_version(chat),
                })
            value = {
                "projects": projects, "chats": chats, "snapshot_at": self.clock(),
                "limited": len(chat_source["threads"]) >= 50,
                "unavailable": bool(chat_source.get("unavailableHosts") or chat_source.get("unavailableSources")),
                "work_paused": self.store.get("work_paused", False),
            }
            self.cached = principal, value
            return deepcopy(value)

    def _chat(self, owner, identity, *, fresh=False):
        if not isinstance(identity, str) or not IDENTITY.fullmatch(identity):
            raise ValueError("Invalid chat.")
        candidates = [c for c in self.snapshot(owner, fresh=fresh)["chats"]
                      if c["id"] == identity and c["host_id"] == "local" and c["kind"] == "codex"]
        if len(candidates) != 1:
            raise ConflictError("This chat is no longer available. Refresh the list.")
        return candidates[0]

    def read(self, owner, identity):
        principal = self._principal(owner)
        chat = self._chat(owner, identity)
        raw = self.bridge("read", threadId=identity)
        if self._principal(owner) != principal:
            raise PermissionError("The owner context changed.")
        if raw.get("thread", {}).get("id") != identity:
            raise DesktopUnavailable("The returned chat did not match.")
        messages = []
        for turn in reversed(raw.get("turns", [])):
            if not isinstance(turn, dict):
                continue
            for item in turn.get("items", []):
                if not isinstance(item, dict):
                    continue
                if item.get("type") == "agentMessage" and item.get("phase") in ("commentary", "final"):
                    text, role = item.get("text"), "assistant"
                elif item.get("type") == "userMessage":
                    text = "\n".join(c["text"] for c in item.get("content", [])
                                     if isinstance(c, dict) and c.get("type") == "text" and isinstance(c.get("text"), str))
                    role = "user"
                else:
                    continue  # Never expose reasoning, commands, tool inputs or output.
                if isinstance(text, str) and text.strip():
                    messages.append({"id": str(item.get("id", "")), "role": role,
                                     "text": public_text(text), "phase": item.get("phase"),
                                     "at": turn.get("completedAt") or turn.get("startedAt")})
        return {"chat": chat, "messages": messages[-24:], "has_more": raw.get("page", {}).get("hasMore", False),
                "receipts": self.receipts(owner, identity), "snapshot_at": self.clock()}

    @staticmethod
    def _receipt(row):
        return {k: row[k] for k in ("request_key", "thread_id", "title", "text", "state", "created_at", "updated_at")} | {
            "attachments": json.loads(row["attachments"])}

    def stage_attachment(self, owner, payload):
        principal = self._principal(owner)
        if not isinstance(payload, dict) or set(payload) != {
                "thread_id", "target_version", "request_id", "name", "mime_type", "data_base64"}:
            raise ValueError("Invalid attachment fields.")
        with self.lock:
            chat = self._chat(owner, payload["thread_id"], fresh=True)
            if not chat["can_send"] or chat["target_version"] != payload["target_version"]:
                raise ConflictError("The chat destination changed. Refresh before attaching.")
            if self._principal(owner) != principal:
                raise PermissionError("The owner context changed.")
            def check_context():
                if self._principal(owner) != principal:
                    raise PermissionError("The owner context changed.")
            return self.attachments.stage(owner, payload, check_context=check_context)

    def remove_attachment(self, owner, identity, payload):
        self._principal(owner)
        if payload != {}:
            raise ValueError("Remove takes no additional fields.")
        with self.lock:
            return self.attachments.remove(owner, identity)

    def receipts(self, owner, identity=None):
        self._principal(owner)
        with self.store.db() as db:
            rows = db.execute("SELECT * FROM desktop_chat_prompts WHERE owner=?"
                              + (" AND thread_id=?" if identity else "") + " ORDER BY created_at DESC LIMIT 30",
                              (owner, identity) if identity else (owner,)).fetchall()
            return [self._receipt(row) for row in rows]

    def send(self, owner, payload):
        principal = self._principal(owner)
        if (not isinstance(payload, dict) or set(payload) - {"request_key", "thread_id", "target_version", "text", "attachment_ids"}
                or not {"request_key", "thread_id", "target_version", "text"} <= set(payload)):
            raise ValueError("Invalid chat prompt.")
        identities = payload.get("attachment_ids", [])
        if (not isinstance(identities, list) or len(identities) > 5
                or any(not isinstance(i, str) or not IDENTITY.fullmatch(i) for i in identities)
                or len(set(identities)) != len(identities)):
            raise ValueError("Attach up to five different files.")
        key, identity, version, text = (payload[k] for k in ("request_key", "thread_id", "target_version", "text"))
        if (not isinstance(key, str) or not re.fullmatch(r"[a-zA-Z0-9_.:-]{1,160}", key)
                or not isinstance(text, str) or not (text.strip() or identities) or len(text) > 12000
                or not isinstance(version, str) or not re.fullmatch(r"[a-f0-9]{64}", version)
                or not isinstance(identity, str) or not IDENTITY.fullmatch(identity)):
            raise ValueError("Enter a prompt of up to 12,000 characters.")
        # Serialize admissions. Never hold the database lock during desktop IO.
        with self.lock:
            with self.store.db() as db:
                existing = db.execute("SELECT * FROM desktop_chat_prompts WHERE owner=? AND request_key=?",
                                      (owner, key)).fetchone()
            if existing:
                if any(existing[k] != payload[k] for k in ("thread_id", "target_version", "text")):
                    raise ConflictError("This receipt belongs to a different prompt.")
                if [a["id"] for a in json.loads(existing["attachments"])] != identities:
                    raise ConflictError("This receipt belongs to different files.")
                return self._receipt(existing)
            chat = self._chat(owner, identity, fresh=True)
            if not chat["can_send"] or chat["target_version"] != version:
                raise ConflictError("The selected chat changed. Refresh before sending.")
            if self._principal(owner) != principal:
                raise PermissionError("The owner context changed.")
            at = self.clock()
            with self.store.db() as db:
                db.execute("BEGIN IMMEDIATE")
                if self._principal(owner) != principal:
                    raise PermissionError("The owner context changed.")
                attachments = self.attachments.bind(db, owner, identities, identity, version, key)
                db.execute("""INSERT INTO desktop_chat_prompts
                           (owner,request_key,thread_id,target_version,title,text,state,created_at,updated_at,receipt,attachments)
                           VALUES(?,?,?,?,?,?,'sending',?,?,NULL,?)""",
                           (owner, key, identity, version, chat["title"], text, at, at, json.dumps(attachments)))
        try:
            result = self.bridge("send", threadId=identity, text=self.attachments.prompt(text, attachments),
                                 attachment_ids=identities)
            # The desktop's success is acceptance, never completion of the work.
            state = "accepted"
            receipt = json.dumps({"accepted_by": "codex_desktop",
                                  "thread_id": identity, "response_keys": sorted(result)}, separators=(",", ":"))
        except Exception:
            # A lost connection can occur after acceptance. Never replay it.
            state, receipt = "uncertain", None
        with self.store.db() as db:
            db.execute("UPDATE desktop_chat_prompts SET state=?,receipt=?,updated_at=? WHERE owner=? AND request_key=?",
                       (state, receipt, self.clock(), owner, key))
            row = db.execute("SELECT * FROM desktop_chat_prompts WHERE owner=? AND request_key=?", (owner, key)).fetchone()
        self._principal(owner)
        self.cached = None
        return self._receipt(row)
