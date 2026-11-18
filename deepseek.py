"""DeepSeek Harness headless adapter for explicit project tasks.

The installed desktop app uses an account grant that its headless profile cannot
use. Headless therefore requires a DeepSeek API key. Runs are one-shot and are
never replayed automatically.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import threading
import time
import uuid

from companion import ConflictError
from memory_policy import scrub_secrets
from relay import protect


MAX_PROMPT_CHARS = 24_000
ROOT = Path(__file__).resolve().parent
DEFAULT_CLI = Path(os.environ.get("LOCALAPPDATA", "")) / "Programs" / "DeepSeek Harness" / "resources" / "runtime" / "cli" / "bin" / "dsh.cmd"
KEY_PATH = ROOT / ".state" / "deepseek-api-key.dpapi"


class DeepSeekUnavailable(RuntimeError):
    pass


class DeepSeekHarnessBridge:
    def __init__(self, *, cli=None, key_path=None, runner=None):
        self.cli = Path(cli or DEFAULT_CLI)
        self.key_path = Path(key_path or KEY_PATH)
        self.runner = runner or self._run

    def _key(self):
        value = os.environ.get("DEEPSEEK_API_KEY")
        if value:
            return value.strip()
        try:
            encrypted = self.key_path.read_bytes()
        except (FileNotFoundError, OSError):
            return None
        try:
            return protect(encrypted, decrypt=True).decode("ascii").strip()
        except (RuntimeError, UnicodeError):
            return None

    def available(self):
        return self.cli.is_file() and bool(self._key())

    def _run(self, root, prompt, key):
        environment = {name: os.environ[name] for name in (
            "PATH", "SYSTEMROOT", "WINDIR", "TEMP", "TMP", "USERPROFILE",
            "APPDATA", "LOCALAPPDATA", "HOMEDRIVE", "HOMEPATH", "PROGRAMDATA",
        ) if os.environ.get(name)}
        environment["DEEPSEEK_API_KEY"] = key
        try:
            result = subprocess.run(
                f'"{self.cli}" headless -', shell=True, cwd=str(root),
                input=prompt, capture_output=True, text=True, encoding="utf-8",
                timeout=300, env=environment,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except (OSError, subprocess.TimeoutExpired):
            raise DeepSeekUnavailable("The DeepSeek Harness did not finish within its task limit.") from None
        if result.returncode:
            message = (result.stderr or result.stdout or "DeepSeek Harness rejected the task.").strip()
            raise DeepSeekUnavailable(message[:800])
        output = result.stdout.strip()
        if not output:
            raise DeepSeekUnavailable("The DeepSeek Harness returned no final message.")
        return scrub_secrets(output[:12000])

    def run(self, root, prompt):
        if not isinstance(prompt, str) or not prompt.strip() or len(prompt) > MAX_PROMPT_CHARS:
            raise DeepSeekUnavailable("Enter a task of up to 24,000 characters.")
        if not self.cli.is_file():
            raise DeepSeekUnavailable("The DeepSeek Harness CLI is not installed.")
        key = self._key()
        if not key:
            raise DeepSeekUnavailable(
                "DeepSeek Harness needs DEEPSEEK_API_KEY. Run configure_deepseek_key.py "
                "or set the environment variable."
            )
        return self.runner(Path(root), prompt, key)


def _version(alias, root, revision):
    return hashlib.sha256(json.dumps(
        [alias, str(root), int(revision)], separators=(",", ":"),
    ).encode()).hexdigest()


def _chat_id(alias):
    return str(uuid.uuid5(uuid.NAMESPACE_URL, "vega-deepseek:" + alias))


class DeepSeekWorkspace:
    """Owner-scoped one-shot headless tasks in explicitly registered project folders."""

    def __init__(self, store, project_tools, *, bridge=None, clock=time.time):
        self.store = store
        self.project_tools = project_tools
        self.bridge = bridge or DeepSeekHarnessBridge()
        self.clock = clock
        self.lock = threading.RLock()
        self.threads = {}
        with store.db() as db:
            db.execute("""CREATE TABLE IF NOT EXISTS deepseek_runs(
                owner INTEGER NOT NULL, id TEXT NOT NULL, project TEXT NOT NULL,
                root TEXT NOT NULL, prompt TEXT NOT NULL, state TEXT NOT NULL,
                output TEXT, error TEXT, created_at REAL NOT NULL, updated_at REAL NOT NULL,
                request_key TEXT NOT NULL, target_version TEXT NOT NULL, title TEXT NOT NULL,
                PRIMARY KEY(owner,id,request_key))""")
            db.execute("UPDATE deepseek_runs SET state='uncertain',error='Vega stopped during this task; it was not replayed.' "
                       "WHERE state='running'")

    def _principal(self, owner):
        with self.store.db() as db:
            settings = {r["key"]: json.loads(r["value"]) for r in db.execute(
                "SELECT key,value FROM settings WHERE key IN ('owner_id','owner_chat_id')")}
            if type(owner) is not int or settings.get("owner_id") != owner or settings.get("owner_chat_id") != owner:
                raise PermissionError("The paired owner is required.")
            return owner, 0

    def _projects(self, owner):
        result = self.project_tools.projects(owner)
        projects = []
        for item in result.get("projects", []):
            workspace = item.get("workspace")
            if not isinstance(workspace, dict) or not workspace.get("root"):
                continue
            projects.append({
                "alias": item["alias"], "label": item["label"],
                "root": workspace["root"], "revision": workspace.get("revision", 0),
                "can_write": workspace.get("can_write", False),
            })
        return projects

    def snapshot(self, owner):
        principal = self._principal(owner)
        available = self.bridge.available()
        projects, chats = [], []
        for item in self._projects(owner):
            projects.append({"id": item["alias"], "title": item["label"]})
            version = _version(item["alias"], item["root"], item["revision"])
            with self.store.db() as db:
                latest = db.execute("SELECT state,updated_at FROM deepseek_runs WHERE owner=? AND project=? "
                                    "ORDER BY created_at DESC LIMIT 1", (owner, item["alias"])).fetchone()
            chats.append({
                "id": _chat_id(item["alias"]), "title": item["label"],
                "host_id": "local", "kind": "deepseek", "status": latest["state"] if latest else "idle",
                "project_id": item["alias"], "project": item["label"],
                "running": bool(latest and latest["state"] == "running"),
                "updated_at": latest["updated_at"] if latest else self.clock(),
                "pinned": False, "unread": False,
                "can_send": bool(available and item["can_write"]),
                "target_version": version,
                "alias": item["alias"], "root": item["root"],
            })
        if self._principal(owner) != principal:
            raise PermissionError("The owner context changed.")
        return {"projects": projects, "chats": chats, "snapshot_at": self.clock(),
                "unavailable": not available, "work_paused": bool(self.store.get("work_paused", False))}

    def _chat(self, owner, identity):
        chats = [c for c in self.snapshot(owner)["chats"] if c["id"] == identity]
        if len(chats) != 1:
            raise ConflictError("This DeepSeek project is no longer available. Refresh the list.")
        return chats[0]

    @staticmethod
    def _receipt(row):
        return {k: row[k] for k in ("request_key", "id", "project", "title", "prompt",
                                    "state", "output", "error", "created_at", "updated_at")}

    def read(self, owner, identity):
        self._principal(owner)
        chat = self._chat(owner, identity)
        with self.store.db() as db:
            row = db.execute("SELECT * FROM deepseek_runs WHERE owner=? AND project=? "
                             "ORDER BY created_at DESC LIMIT 1", (owner, chat["alias"])).fetchone()
        messages = []
        if row:
            messages.append({"id": row["request_key"] + "-user", "role": "user",
                             "text": row["prompt"], "phase": "final", "at": row["created_at"]})
            text = row["output"] if row["output"] else (
                row["error"] if row["error"] else "The DeepSeek task was accepted and is still running.")
            messages.append({"id": row["request_key"] + "-result", "role": "assistant",
                             "text": scrub_secrets(text), "phase": "final", "at": row["updated_at"]})
        return {"chat": chat, "messages": messages[-24:], "has_more": False,
                "receipts": [self._receipt(row)] if row else [], "snapshot_at": self.clock()}

    def _execute(self, owner, identity, root, prompt):
        try:
            output = self.bridge.run(root, prompt)
            state, error = "completed", None
        except DeepSeekUnavailable as failure:
            output, state, error = None, "failed", str(failure)[:800]
        with self.store.db() as db:
            db.execute("UPDATE deepseek_runs SET state=?,output=?,error=?,updated_at=? "
                       "WHERE owner=? AND id=?", (state, output, error, self.clock(), owner, identity))
        self.threads.pop((owner, identity), None)

    def send(self, owner, payload):
        principal = self._principal(owner)
        if not isinstance(payload, dict) or not {"request_key", "thread_id", "target_version", "text"} <= set(payload):
            raise ValueError("Invalid DeepSeek task.")
        key, identity, version, text = (payload[k] for k in ("request_key", "thread_id", "target_version", "text"))
        if (not isinstance(key, str) or not re.fullmatch(r"[a-zA-Z0-9_.:-]{1,160}", key)
                or not isinstance(text, str) or not text.strip() or len(text) > MAX_PROMPT_CHARS
                or not isinstance(version, str) or not re.fullmatch(r"[a-f0-9]{64}", version)):
            raise ValueError("Enter a task of up to 24,000 characters.")
        with self.lock:
            with self.store.db() as db:
                existing = db.execute("SELECT * FROM deepseek_runs WHERE owner=? AND request_key=?",
                                      (owner, key)).fetchone()
            if existing:
                if existing["prompt"] != text or existing["target_version"] != version:
                    raise ConflictError("This receipt belongs to a different task.")
                return self._receipt(existing)
            chat = self._chat(owner, identity)
            if not self.bridge.available():
                raise DeepSeekUnavailable(
                    "DeepSeek Harness needs DEEPSEEK_API_KEY. Run configure_deepseek_key.py "
                    "or set the environment variable."
                )
            if not chat["can_send"] or chat["target_version"] != version:
                raise ConflictError("The DeepSeek project or folder changed. Refresh before sending.")
            if self._principal(owner) != principal:
                raise PermissionError("The owner context changed.")
            at = self.clock()
            with self.store.db() as db:
                db.execute("""INSERT INTO deepseek_runs
                           (owner,id,project,root,prompt,state,output,error,created_at,updated_at,request_key,target_version,title)
                           VALUES(?,?,?,?,?,'accepted',NULL,NULL,?,?,?,?,?)""",
                           (owner, identity, chat["alias"], chat["root"], text, at, at, key, version, chat["title"]))
                row = db.execute("SELECT * FROM deepseek_runs WHERE owner=? AND request_key=?", (owner, key)).fetchone()
            thread = threading.Thread(target=self._execute, args=(owner, identity, chat["root"], text),
                                      name="vega-deepseek-task", daemon=True)
            self.threads[(owner, identity)] = thread
        thread.start()
        return self._receipt(row)

    def receipts(self, owner, identity=None):
        self._principal(owner)
        with self.store.db() as db:
            rows = db.execute("SELECT * FROM deepseek_runs WHERE owner=?"
                               + (" AND id=?" if identity else "") + " ORDER BY created_at DESC LIMIT 30",
                               (owner, identity) if identity else (owner,)).fetchall()
            return [self._receipt(row) for row in rows]
