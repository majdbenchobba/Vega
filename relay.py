"""Owner-paired Telegram relay for local Codex tasks. Standard library only."""

from __future__ import annotations

import argparse
import contextlib
import ctypes
from datetime import datetime, timezone
import hashlib
import hmac
import html
import http.client
import json
import os
from pathlib import Path
import re
import secrets
import sqlite3
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import urllib.error
import urllib.request
from urllib.parse import parse_qs
from transport_diagnostics import MalformedResponseError, classify_transport_failure
from project_checklists import extract_project_checklist, sanitize_project_checklist
from routing_targets import dispatch_target, require_dispatchable
from project_freshness import timestamp_seconds
from notification_privacy import TOKEN_PATTERN, redact
from project_milestones import POLICY, ROUTINE_REASON, ProjectMilestones
from project_responses import ProjectResponses
import project_responses
import text_delivery
from project_progress import LiveProgress, install as install_progress, normalize_progress
from photo_batches import PhotoBatches, ACK_PREFIX as PHOTO_ACK_PREFIX

ROOT = Path(__file__).resolve().parent
STOP = threading.Event()
_DATABASE_LOCKS = {}


def protect(data: bytes, *, decrypt: bool = False) -> bytes:
    """Use current-user Windows DPAPI; never fall back to clear-text storage."""
    if os.name != "nt":
        raise RuntimeError("This installation requires Windows DPAPI.")

    class Blob(ctypes.Structure):
        _fields_ = [("length", ctypes.c_ulong), ("data", ctypes.c_void_p)]

    buffer = ctypes.create_string_buffer(data)
    source = Blob(len(data), ctypes.cast(buffer, ctypes.c_void_p))
    result = Blob()
    crypt = ctypes.WinDLL("crypt32", use_last_error=True)
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.LocalFree.argtypes = [ctypes.c_void_p]
    kernel.LocalFree.restype = ctypes.c_void_p
    function = crypt.CryptUnprotectData if decrypt else crypt.CryptProtectData
    function.argtypes = [
        ctypes.POINTER(Blob), ctypes.c_void_p, ctypes.c_void_p,
        ctypes.c_void_p, ctypes.c_void_p, ctypes.c_ulong, ctypes.POINTER(Blob),
    ]
    function.restype = ctypes.c_int
    if not function(ctypes.byref(source), None, None, None, None, 1, ctypes.byref(result)):
        raise RuntimeError("Windows could not protect or unlock the bot credential.")
    try:
        return ctypes.string_at(result.data, result.length)
    finally:
        kernel.LocalFree(result.data)


class Store:
    def __init__(self, directory: Path):
        self.directory = directory
        directory.mkdir(parents=True, exist_ok=True)
        self.path = directory / "relay.sqlite3"
        # One process must not make its own readers/writers race through dozens
        # of separate SQLite connections. Reentrant reads inside a transaction
        # still work; other processes retain SQLite's native locking.
        self._db_lock = _DATABASE_LOCKS.setdefault(str(self.path.resolve()), threading.RLock())
        with self.db() as db:
            db.executescript("""
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS inbox (
                    update_id INTEGER PRIMARY KEY, message_id INTEGER NOT NULL,
                    owner_id INTEGER NOT NULL, task TEXT NOT NULL, text TEXT NOT NULL,
                    received_at REAL NOT NULL, state TEXT NOT NULL DEFAULT 'queued',
                    receipt TEXT, handled_at REAL
                );
                CREATE TABLE IF NOT EXISTS inbox_claims (
                    update_id INTEGER PRIMARY KEY, first_claimed_at REAL NOT NULL,
                    last_claimed_at REAL NOT NULL, claim_count INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS outbox (
                    event_id TEXT PRIMARY KEY, task TEXT NOT NULL, text TEXT NOT NULL,
                    created_at REAL NOT NULL, state TEXT NOT NULL DEFAULT 'queued',
                    message_id INTEGER, sent_at REAL, error TEXT
                );
                CREATE TABLE IF NOT EXISTS task_states (
                    task TEXT PRIMARY KEY, status TEXT NOT NULL, observed_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS cursors (path TEXT PRIMARY KEY, position INTEGER NOT NULL);
                CREATE TABLE IF NOT EXISTS outbound_documents (
                    event_id TEXT PRIMARY KEY, owner INTEGER NOT NULL, path TEXT NOT NULL,
                    sha256 TEXT NOT NULL, size INTEGER NOT NULL, filename TEXT NOT NULL,
                    telegram_file_id TEXT
                );
                CREATE TABLE IF NOT EXISTS delivery_diagnostics (
                    event_id TEXT PRIMARY KEY, details TEXT NOT NULL, created_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS project_checklist_reports (
                    owner INTEGER NOT NULL, task TEXT NOT NULL, thread_id TEXT NOT NULL,
                    status TEXT NOT NULL, checklist TEXT NOT NULL, observed_at REAL NOT NULL,
                    source TEXT NOT NULL, evidence TEXT NOT NULL,
                    PRIMARY KEY(owner,task)
                );
                CREATE TABLE IF NOT EXISTS project_source_checks (
                    owner INTEGER NOT NULL, task TEXT NOT NULL, thread_id TEXT NOT NULL,
                    checked_at REAL NOT NULL, evidence_at REAL, source TEXT NOT NULL,
                    error TEXT, PRIMARY KEY(owner,task)
                );
            """)
            text_delivery.install(db)
            project_responses.install(db)
            install_progress(db)

    @contextlib.contextmanager
    def db(self):
        with self._db_lock:
            connection = sqlite3.connect(self.path, timeout=15)
            connection.row_factory = sqlite3.Row
            try:
                with connection:
                    yield connection
            finally:
                connection.close()

    def get(self, key, default=None):
        with self.db() as db:
            row = db.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
        return json.loads(row[0]) if row else default

    def get_many(self, keys):
        """Read a bounded set together; do not cache authority or settings."""
        keys = tuple(dict.fromkeys(keys))
        if not keys:
            return {}
        if len(keys) > 100:
            raise ValueError("Too many settings requested.")
        with self.db() as db:
            rows = db.execute(
                "SELECT key,value FROM settings WHERE key IN (" + ",".join("?" for _ in keys) + ")",
                keys,
            ).fetchall()
        return {row["key"]: json.loads(row["value"]) for row in rows}

    def set(self, key, value):
        encoded = json.dumps(value, ensure_ascii=False)
        if key == "work_paused":
            # Each new pause revokes earlier one-run starts, even when the
            # background queue was already paused. Keep both values atomic.
            with self.db() as db:
                db.execute("BEGIN IMMEDIATE")
                previous=db.execute("SELECT value FROM settings WHERE key=?",(key,)).fetchone()
                if value is True or not previous or previous[0]!=encoded:
                    revision=db.execute("SELECT value FROM settings WHERE key='work_pause_revision'").fetchone()
                    db.execute("INSERT OR REPLACE INTO settings VALUES('work_pause_revision',?)",
                               (json.dumps((json.loads(revision[0]) if revision else 0)+1),))
                    db.execute("INSERT OR REPLACE INTO settings VALUES(?,?)",(key,encoded))
            return
        with self.db() as db:
            previous = db.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
        if previous and previous[0] == encoded:
            return
        with self.db() as db:
            db.execute(
                "INSERT OR REPLACE INTO settings VALUES (?,?)",
                (key, encoded),
            )

    def claim(self, table, ready_state, active_state):
        allowed = {"inbox": ("update_id", "received_at"), "voice_inbox": ("update_id", "received_at"),
                   "voice_replies": ("update_id", "created_at")}
        if table not in allowed:
            raise ValueError("This queue cannot be claimed here.")
        identity, ordering = allowed[table]
        # Idle workers must remain readers. Acquiring a RESERVED write lock on
        # every empty poll can starve short writes from the coordinator.
        with self.db() as db:
            row = db.execute(f"SELECT * FROM {table} WHERE state=? ORDER BY {ordering} LIMIT 1", (ready_state,)).fetchone()
        if not row:
            return None
        with self.db() as db:
            updated = db.execute(
                f"UPDATE {table} SET state=? WHERE {identity}=? AND state=?",
                (active_state, row[identity], ready_state),
            ).rowcount
        return {**dict(row), "state": active_state} if updated == 1 else None

    def enqueue(self, event_id, task, text, *, state="queued"):
        if state not in ("queued", "local"):
            raise ValueError("Invalid initial delivery state.")
        if not isinstance(text, str) or not text.strip() or len(text.encode("utf-8")) > text_delivery.MAX_TEXT_BYTES:
            raise ValueError("Use a nonempty text reply of at most 256 KB.")
        text = redact(text)
        with self.db() as db:
            routine = event_id.startswith(("task:", "handled:")) and state == "queued"
            inserted = db.execute(
                "INSERT OR IGNORE INTO outbox(event_id,task,text,created_at,state,error) VALUES(?,?,?,?,?,?)",
                (event_id, task, text, time.time(), "suppressed" if routine else state,
                 ROUTINE_REASON if routine else None),
            ).rowcount == 1
            owner = db.execute("SELECT value FROM settings WHERE key='owner_chat_id'").fetchone()
            if inserted and owner and type(json.loads(owner["value"])) is int:
                db.execute("INSERT INTO outbound_text VALUES(?,?,?,?)",
                           (event_id, json.loads(owner["value"]), hashlib.sha256(text.encode("utf-8")).hexdigest(), time.time()))
            return inserted

    def task_state(self, task, status, observed_at=None):
        observed = timestamp_seconds(observed_at) if observed_at is not None else time.time()
        if observed is None or observed > time.time() + 30:
            raise ValueError("A project activity observation needs a valid source timestamp.")
        with self.db() as db:
            db.execute(
                "INSERT INTO task_states VALUES(?,?,?) ON CONFLICT(task) DO UPDATE "
                "SET status=excluded.status,observed_at=excluded.observed_at "
                "WHERE excluded.observed_at >= task_states.observed_at",
                (task, status, observed),
            )

    def inbox(self, claim=False, tasks=None):
        with self.db() as db:
            if claim:
                db.execute("BEGIN IMMEDIATE")
            rows = db.execute(
                "SELECT * FROM inbox WHERE state='queued' ORDER BY received_at,update_id LIMIT 25"
            ).fetchall()
            contexts = {}
            if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='inbox_context'").fetchone():
                for item in rows:
                    context = db.execute("SELECT payload FROM inbox_context WHERE update_id=?", (item["update_id"],)).fetchone()
                    if context:
                        contexts[item["update_id"]] = json.loads(context["payload"])
            if tasks is not None:
                paired = {row["key"]: json.loads(row["value"]) for row in db.execute(
                    "SELECT key,value FROM settings WHERE key IN ('owner_id','owner_chat_id')")}
                eligible = []
                for row in rows:
                    batches = getattr(self, "photo_batches", None)
                    if batches is not None and batches.routing_reason(db, row):
                        continue
                    target = contexts.get(row["update_id"], {}).get("routing_target")
                    owner_changed = row["owner_id"] != paired.get("owner_id") or paired.get("owner_chat_id") != row["owner_id"]
                    unavailable = False
                    try:
                        destination = require_dispatchable(tasks, row["task"])
                    except (ValueError, KeyError):
                        destination = {}
                        unavailable = True
                    target_changed = target and target.get("thread_id") != destination.get("thread_id")
                    if owner_changed or target_changed or unavailable:
                        if claim:
                            db.execute("UPDATE inbox SET state='awaiting_clarification',receipt=? WHERE update_id=? AND state='queued'",
                                       ("The project chat is unavailable. Reconnect it before routing. Nothing was dispatched."
                                        if unavailable else "The owner or registered target changed before routing. Nothing was dispatched.",
                                        row["update_id"]))
                        continue
                    eligible.append(row)
                rows = eligible
            if claim:
                claimed_at = time.time()
                db.executemany(
                    "UPDATE inbox SET state='routing' WHERE update_id=? AND state='queued'",
                    [(row["update_id"],) for row in rows],
                )
                # This records the queue consumer's claim, not acceptance by
                # the destination chat. Historical rows are never backfilled.
                db.executemany(
                    "INSERT INTO inbox_claims VALUES(?,?,?,1) ON CONFLICT(update_id) DO UPDATE SET "
                    "last_claimed_at=excluded.last_claimed_at,claim_count=inbox_claims.claim_count+1",
                    [(row["update_id"], claimed_at, claimed_at) for row in rows],
                )
        return [
            {**dict(row), "state": "routing" if claim else row["state"],
             **({"notification_context": contexts[row["update_id"]]} if row["update_id"] in contexts else {})}
            for row in rows
        ]

    def save_project_checklist(self, task, thread_id, status, checklist, observed_at=None, *, source="task_report", evidence=None):
        owner = self.get("owner_id")
        if type(owner) is not int:
            return
        observed = timestamp_seconds(observed_at) if observed_at is not None else None
        if observed is None or observed > time.time() + 30:
            raise ValueError("A project checklist needs its original source timestamp.")
        data = sanitize_project_checklist(checklist)
        safe_evidence = sanitize_project_checklist({"done": evidence or []})["done"]
        with self.db() as db:
            previous = db.execute("SELECT thread_id,checklist FROM project_checklist_reports WHERE owner=? AND task=?",
                                  (owner, task)).fetchone()
            if source == "task_report" and not any(data.values()) and previous and previous["thread_id"] == thread_id:
                return
            changed = db.execute(
                "INSERT INTO project_checklist_reports VALUES(?,?,?,?,?,?,?,?) "
                "ON CONFLICT(owner,task) DO UPDATE SET thread_id=excluded.thread_id,status=excluded.status,"
                "checklist=excluded.checklist,observed_at=excluded.observed_at,source=excluded.source,evidence=excluded.evidence "
                "WHERE excluded.observed_at>=project_checklist_reports.observed_at",
                (owner, task, thread_id, status, json.dumps(data, ensure_ascii=False), observed,
                 source, json.dumps(safe_evidence, ensure_ascii=False)),
            ).rowcount
            if changed:
                db.execute(
                    "INSERT INTO project_source_checks VALUES(?,?,?,?,?,?,NULL) "
                    "ON CONFLICT(owner,task) DO UPDATE SET thread_id=excluded.thread_id,checked_at=excluded.checked_at,"
                    "evidence_at=excluded.evidence_at,source=excluded.source,error=NULL",
                    (owner, task, thread_id, time.time(), observed, source),
                )


class TelegramError(Exception):
    def __init__(self, message, *, uncertain=False, diagnostic=None):
        super().__init__(redact(message))
        self.uncertain = uncertain
        self.diagnostic = diagnostic


class Telegram:
    def __init__(self, token):
        if not TOKEN_PATTERN.fullmatch(token):
            raise TelegramError("The bot credential format is invalid.")
        self.token = token

    def call(self, method, **payload):
        request = urllib.request.Request(
            "https://api.telegram.org/bot" + self.token + "/" + method,
            data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        started, stage = time.monotonic(), "request"
        try:
            with urllib.request.urlopen(request, timeout=20) as response:
                stage = "read_response"
                result = json.load(response)
            stage = "validate_response"
            return self._validated_result(result, method)
        except (OSError, TimeoutError, ValueError, TypeError, KeyError, http.client.HTTPException) as error:
            diagnostic = classify_transport_failure(error, method=method, stage=stage,
                                                     elapsed_ms=(time.monotonic() - started) * 1000,
                                                     byte_count=len(request.data))
            raise TelegramError(diagnostic["summary"], uncertain=method == "sendMessage" and diagnostic["uncertain"],
                                diagnostic=diagnostic) from None

    @staticmethod
    def _validated_result(envelope, method):
        if not isinstance(envelope, dict) or type(envelope.get("ok")) is not bool:
            raise MalformedResponseError()
        if envelope["ok"] is False:
            code = envelope.get("error_code")
            if type(code) is int and 400 <= code <= 599:
                # A fixed URL is enough to classify status; the credential URL
                # and the server's description are never retained.
                raise urllib.error.HTTPError("https://api.telegram.org/", code, "", None, None)
            raise MalformedResponseError()
        if "result" not in envelope:
            raise MalformedResponseError()
        result = envelope["result"]
        if method in ("sendMessage", "sendVoice", "sendDocument", "sendPhoto"):
            if not isinstance(result, dict) or type(result.get("message_id")) is not int or result["message_id"] <= 0:
                raise MalformedResponseError()
        elif method == "getUpdates":
            if not isinstance(result, list):
                raise MalformedResponseError()
        elif method in ("getMe", "getWebhookInfo", "getFile", "getChatMenuButton"):
            if not isinstance(result, dict):
                raise MalformedResponseError()
        elif method in ("sendChatAction", "setChatMenuButton") and result is not True:
            raise MalformedResponseError()
        return result

    def download_voice(self, file_id, *, maximum=12 * 1024 * 1024):
        return self.download_file(file_id, maximum=maximum)

    def download_file(self, file_id, *, maximum=20 * 1024 * 1024):
        file = self.call("getFile", file_id=file_id)
        path = file.get("file_path", "")
        if (
            not isinstance(path, str) or not re.fullmatch(r"[A-Za-z0-9_./-]{1,300}", path)
            or path.startswith("/") or ".." in path.split("/")
            or file.get("file_size", 0) > maximum
        ):
            raise TelegramError("This media item exceeds the download limit or has an invalid file reference.")
        request = urllib.request.Request("https://api.telegram.org/file/bot" + self.token + "/" + path)
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                data = response.read(maximum + 1)
        except (OSError, TimeoutError):
            raise TelegramError("The media item could not be downloaded from Telegram.") from None
        if not 4 <= len(data) <= maximum:
            raise TelegramError("This media item exceeds the download limit.")
        return data

    def send_voice(self, chat_id, audio, *, caption=""):
        return self._upload(
            "sendVoice", "voice", chat_id, audio, filename="vega.mp3",
            mime="audio/mpeg", caption=caption, maximum=8 * 1024 * 1024, timeout=45,
        )

    def send_document(self, chat_id, document, *, filename, caption="", mime="application/zip"):
        if not isinstance(filename, str) or len(filename) > 180 or any(value in filename for value in ('"', "\r", "\n", "\0")):
            raise TelegramError("The document filename is invalid.")
        if {"application/zip": ".zip", "audio/mp4": ".m4a"}.get(mime) != Path(filename).suffix.lower():
            raise TelegramError("This document format is not supported by the reviewed delivery path.")
        return self._upload(
            "sendDocument", "document", chat_id, document, filename=filename,
            mime=mime, caption=caption, maximum=50_000_000, timeout=120,
        )

    def send_artifact(self, chat_id, content, *, filename, mime, kind, caption=""):
        """Transport for separately admitted project artifacts; legacy ZIP rules stay separate."""
        if (not isinstance(filename, str) or not 1 <= len(filename) <= 180
                or any(character in filename for character in ('"', "\r", "\n", "\0", "/", "\\"))):
            raise TelegramError("The artifact filename is invalid.")
        allowed = {"photo": {"image/png", "image/jpeg"},
                   "document": {"application/pdf", "text/plain", "image/png", "image/jpeg", "image/webp"}}
        if kind not in allowed or mime not in allowed[kind]:
            raise TelegramError("This artifact format is unsupported.")
        return self._upload(
            "sendPhoto" if kind == "photo" else "sendDocument",
            "photo" if kind == "photo" else "document", chat_id, content,
            filename=filename, mime=mime, caption=caption, maximum=10_000_000, timeout=120,
        )

    def _upload(self, method, field, chat_id, data, *, filename, mime, caption, maximum, timeout):
        if not isinstance(data, bytes) or not 0 < len(data) <= maximum:
            raise TelegramError("The media exceeds the upload limit.")
        boundary = "Vega" + secrets.token_hex(16)
        parts = []
        fields = [("chat_id", str(chat_id)), ("caption", caption[:1000])]
        if method == "sendDocument" and mime == "audio/mp4":
            fields.append(("disable_content_type_detection", "true"))
        for key, value in fields:
            parts.append(
                f'--{boundary}\r\nContent-Disposition: form-data; name="{key}"\r\n\r\n{value}\r\n'.encode("utf-8")
            )
        parts.append(
            f'--{boundary}\r\nContent-Disposition: form-data; name="{field}"; filename="{filename}"\r\n'
            f'Content-Type: {mime}\r\n\r\n'.encode("utf-8")
        )
        parts.extend((data, f"\r\n--{boundary}--\r\n".encode("utf-8")))
        request = urllib.request.Request(
            "https://api.telegram.org/bot" + self.token + "/" + method,
            data=b"".join(parts), headers={"Content-Type": "multipart/form-data; boundary=" + boundary}, method="POST",
        )
        started, stage = time.monotonic(), "request"
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                stage = "read_response"
                result = json.load(response)
            stage = "validate_response"
            return self._validated_result(result, method)
        except (OSError, TimeoutError, ValueError, TypeError, KeyError, http.client.HTTPException) as error:
            diagnostic = classify_transport_failure(error, method=method, stage=stage,
                                                     elapsed_ms=(time.monotonic() - started) * 1000,
                                                     byte_count=len(request.data))
            raise TelegramError(diagnostic["summary"], uncertain=diagnostic["uncertain"], diagnostic=diagnostic) from None


class Relay:
    def __init__(self, store: Store, tasks: dict, *, telegram_factory=Telegram):
        self.store = store
        self.tasks = tasks
        self.telegram_factory = telegram_factory
        self.api = None
        self.api_lock = threading.RLock()
        self.configure_lock = threading.Lock()
        self.registry_lock = threading.RLock()
        self.milestones = ProjectMilestones(store, lambda: self.tasks)
        self.project_responses = ProjectResponses(store, lambda: self.tasks)
        self.photo_batches = PhotoBatches(store, lambda: self.tasks)
        self.store.photo_batches = self.photo_batches
        if store.get("milestone_policy_started_at") is None:
            transition = store.get("milestone_policy_transition", {})
            store.set("milestone_policy_started_at", transition.get("started_at", time.time()))
        store.set("project_notification_policy", POLICY)
        self.token_file = store.directory / "telegram-token.dpapi"
        if self.token_file.exists():
            self.api = telegram_factory(protect(self.token_file.read_bytes(), decrypt=True).decode())

    def configure(self, token: str):
        with self.configure_lock:
            if self.store.get("owner_id") or self.token_file.exists():
                raise ValueError("A bot is already configured. Existing credentials were preserved.")
            api = self.telegram_factory(token.strip())
            identity = api.call("getMe")
            if not identity.get("is_bot") or not identity.get("username"):
                raise ValueError("Telegram did not return a bot identity.")
            if api.call("getWebhookInfo").get("url"):
                raise ValueError("This bot already has a webhook. Use a dedicated bot.")
            encrypted = protect(token.strip().encode())
            temporary = self.token_file.with_suffix(".tmp")
            temporary.write_bytes(encrypted)
            temporary.replace(self.token_file)
            self.store.set("bot_username", identity["username"])
            self.store.set("pair_code", secrets.token_urlsafe(24))
            self.store.set("pair_expires", time.time() + 1800)
            self.store.set("poll_error", None)
            with self.api_lock:
                self.api = api
            return identity["username"]

    def status(self):
        saved = self.store.get_many((
            "last_poll_at", "last_scan_at", "poll_error", "watcher_error", "owner_id",
            "bot_username", "work_paused", "muted", "milestone_evidence_guard_host_pid",
            "process_id", "milestone_delivery_host_pid",
        ))
        with self.store.db() as db:
            queued = db.execute("SELECT count(*) FROM inbox WHERE state='queued'").fetchone()[0]
            routing = db.execute("SELECT count(*) FROM inbox WHERE state='routing'").fetchone()[0]
            deliveries = dict(db.execute("SELECT state,count(*) FROM outbox GROUP BY state").fetchall())
        last_poll = saved.get("last_poll_at")
        last_scan = saved.get("last_scan_at")
        poll_error = saved.get("poll_error")
        watcher_error = saved.get("watcher_error")
        return {
            "configured": self.api is not None or self.token_file.exists(),
            "paired": bool(saved.get("owner_id")),
            "bot_username": saved.get("bot_username"),
            "last_poll_at": last_poll,
            "last_scan_at": last_scan,
            "poll_error": poll_error,
            "watcher_error": watcher_error,
            "online": bool(last_poll and time.time() - last_poll < 45 and not poll_error),
            "watching": bool(last_scan and time.time() - last_scan < 45 and not watcher_error),
            "work_paused": saved.get("work_paused", False),
            "muted": saved.get("muted", False),
            "project_notifications": {
                "policy": POLICY,
                "routine_cycles_suppressed": True,
                "local_regular_evidence_only": saved.get("milestone_evidence_guard_host_pid") == saved.get("process_id")
                    and saved.get("process_id") is not None,
                "milestones_muted": saved.get("muted", False),
                "delivery_gate_live": saved.get("milestone_delivery_host_pid") == saved.get("process_id")
                    and saved.get("process_id") is not None,
            },
            "inbox_queued": queued,
            "inbox_routing": routing,
            "deliveries": deliveries,
            "process_id": saved.get("process_id"),
        }

    def label(self, task):
        return self.tasks.get(task, {}).get("label", task)

    def inbox_state_for(self, task, original_text):
        return "queued"

    def acknowledge(self, update, task, text):
        return self.store.enqueue(f"ack:{update['update_id']}", task, text)

    def report_status(self, task=None):
        with self.store.db() as db:
            states = {r["task"]: dict(r) for r in db.execute("SELECT * FROM task_states")}
        selected = {task: self.tasks[task]} if task in self.tasks else self.tasks
        lines = ["Project status"]
        if self.store.get("work_paused", False):
            lines.append("Automatic continuation is paused. /resume_work requests resumption.")
        for alias, info in selected.items():
            if alias == "coordinator":
                continue
            state = states.get(alias)
            value = state["status"] if state else info.get("initial_status", "not checked")
            lines.append(f"• {info['label']}: {value}")
        lines.append("\nLatest locally observed state. The coordinator checks every 10 minutes.")
        return "\n".join(lines)

    def handle_update(self, update):
        message = update.get("message") or {}
        sender = message.get("from") or {}
        chat = message.get("chat") or {}
        text = message.get("text")
        update_id = update.get("update_id")
        if (
            not isinstance(update_id, int)
            or chat.get("type") != "private"
            or sender.get("is_bot", False)
            or not isinstance(sender.get("id"), int)
            or sender["id"] != chat.get("id")
        ):
            return False
        owner = self.store.get("owner_id")
        forwarded = any(k in message for k in ("forward_origin", "forward_date", "via_bot"))
        if not owner:
            pair = self.store.get("pair_code", "")
            if (
                forwarded or not pair or not isinstance(text, str)
                or time.time() > self.store.get("pair_expires", 0)
                or not hmac.compare_digest(text, "/start " + pair)
            ):
                return False
            self.store.set("owner_id", sender["id"])
            self.store.set("owner_chat_id", chat["id"])
            self.store.set("pair_code", None)
            self.store.enqueue(
                f"pair:{update_id}", "coordinator",
                "Connected to your private project relay.\n"
                "Only this Telegram account can give instructions.\n"
                "Use /status, /projects, or reply to an update. "
                "Text instructions are picked up on the next available coordinator check, "
                "scheduled every 10 minutes.",
            )
            return True
        if sender["id"] != owner or chat["id"] != self.store.get("owner_chat_id"):
            return False
        ack = f"ack:{update_id}"
        with self.store.db() as db:
            seen = db.execute(
                "SELECT 1 FROM inbox WHERE update_id=? UNION ALL "
                "SELECT 1 FROM outbox WHERE event_id IN (?,?) LIMIT 1",
                (update_id, ack, f"pair:{update_id}"),
            ).fetchone()
        if seen:
            return False
        if forwarded:
            self.acknowledge(update, "coordinator", "Please type your instruction directly. Forwarded messages are not accepted as owner commands.")
            return False
        if not isinstance(text, str) or not text.strip():
            self.acknowledge(update, "coordinator", "Text replies are supported. Please type the instruction; voice notes and attachments are not routed yet.")
            return False
        original_text = text
        text = text.strip()
        command = text.split(maxsplit=1)[0].split("@")[0].lower()
        if command in ("/start", "/help"):
            answer = (
                "Vega project relay\n\n/status — latest project status\n"
                "/projects — project names for routing\n"
                "/tell project your instruction — message one project\n"
                "Reply to an update — message that project\n"
                "Ordinary text — message the coordinating Main\n"
                "/mute and /unmute — milestone notifications\n"
                "/pause_work — ask the coordinator to pause continuation\n\n"
                "/resume_work — ask the coordinator to resume continuation\n\n"
                "Replies are saved immediately and routed on the next available coordinator check "
                "(scheduled every 10 minutes while Codex is running). Payments, publishing and other "
                "consequential actions retain their existing review requirements."
            )
        elif command == "/projects":
            answer = "Use /tell followed by one of these names:\n\n" + "\n".join(
                f"{alias} — {info['label']}" for alias, info in self.tasks.items()
            )
        elif command == "/status":
            parts = text.split(maxsplit=1)
            alias = parts[1].strip().lower() if len(parts) > 1 else None
            answer = self.report_status(alias) if alias is None or alias in self.tasks else "Unknown project. Use /projects to see the names."
        elif command in ("/mute", "/unmute"):
            muted = command == "/mute"
            self.store.set("muted", muted)
            answer = "Milestone notifications muted. Replies still work." if muted else "Milestone notifications enabled. Routine project steps stay quiet."
        else:
            task = "coordinator"
            body = original_text
            if command == "/tell":
                parts = original_text.lstrip().split(maxsplit=2)
                if len(parts) != 3 or parts[1].lower() not in self.tasks:
                    self.acknowledge(update, task, "Use /tell project-name your instruction. /projects lists the names.")
                    return False
                task, body = parts[1].lower(), parts[2]
            elif command in ("/pause_work", "/resume_work"):
                paused = command == "/pause_work"
                self.store.set("work_paused", paused)
                body = (
                    "Pause automated continuation of all my project work. Ask active tasks to stop "
                    "at a safe checkpoint and preserve unfinished work. Keep Telegram reply handling "
                    "available so I can resume remotely."
                    if paused else
                    "Resume useful authorized project work from the latest checkpoints. Keep all "
                    "existing project-specific decisions, budget limits and approval requirements."
                )
            elif text.startswith("/"):
                self.acknowledge(update, task, "Unknown command. Use /help, or type your instruction without a slash.")
                return False
            else:
                reply_id = (message.get("reply_to_message") or {}).get("message_id")
                if reply_id:
                    with self.store.db() as db:
                        row = db.execute(
                            "SELECT task FROM outbox WHERE message_id=? AND state='sent' UNION ALL "
                            "SELECT o.task FROM outbox o JOIN outbound_text_parts p ON p.event_id=o.event_id "
                            "WHERE p.message_id=? AND p.state='sent' LIMIT 1", (reply_id, reply_id),
                        ).fetchone()
                    if row:
                        task = row["task"]
            with self.store.db() as db:
                target_state = self.inbox_state_for(task, text)
                route_error = None
                if target_state == "queued":
                    try:
                        require_dispatchable(self.tasks, task)
                    except (ValueError, KeyError):
                        target_state = "awaiting_clarification"
                        route_error = "The project chat is unavailable. Your instruction is saved, but nothing was queued for dispatch. Reconnect the project before routing."
                db.execute(
                    "INSERT OR IGNORE INTO inbox(update_id,message_id,owner_id,task,text,received_at,state) VALUES(?,?,?,?,?,?,?)",
                    (update_id, message.get("message_id", 0), owner, task, body[:16000], time.time(), target_state),
                )
                if route_error:
                    db.execute("UPDATE inbox SET receipt=? WHERE update_id=? AND state='awaiting_clarification'",
                               (route_error, update_id))
            if route_error:
                self.acknowledge(update, task, route_error)
            if target_state != "queued":
                return True
            answer = (
                f"Received for {self.label(task)}. Your instruction is saved; "
                "the coordinator will route it on its next available check, scheduled every "
                "10 minutes while Codex is running."
            )
            self.acknowledge(update, task, answer)
            return True
        self.acknowledge(update, "coordinator", answer)
        return True

    def flush_one(self, *, documents=False):
        owner = self.store.get("owner_chat_id")
        if not owner or self.api is None:
            return False
        with self.store.db() as db:
            candidate = db.execute(
                "SELECT o.* FROM outbox o LEFT JOIN outbound_documents d ON d.event_id=o.event_id "
                "WHERE o.state=? AND " + ("d.event_id IS NOT NULL" if documents else "d.event_id IS NULL") +
                " ORDER BY o.created_at LIMIT 1",
                ("document_queued" if documents else "queued",),
            ).fetchone()
        if candidate is None:
            return False
        with self.store.db() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT * FROM outbox WHERE event_id=? AND state=?",
                (candidate["event_id"], "document_queued" if documents else "queued"),
            ).fetchone()
            if not row:
                return False
            reason = ((self.artifacts.delivery_reason(db,row) if getattr(self,"artifacts",None)
                       else "The artifact service is unavailable.")
                      if row["event_id"].startswith("artifact:")
                      else (self.team.delivery_reason(db,row) if getattr(self,"team",None) else "The team service is unavailable.")
                      if row["event_id"].startswith("team-review:")
                      else self.photo_batches.delivery_reason(db, row) if row["event_id"].startswith(PHOTO_ACK_PREFIX)
                      else self.project_responses.delivery_reason(db, row) if row["event_id"].startswith("report-response:")
                      else self.milestones.delivery_reason(db, row))
            if reason:
                db.execute("UPDATE outbox SET state='suppressed',error=? WHERE event_id=? AND state IN ('queued','document_queued')",
                           (reason, row["event_id"]))
                return True
            document = db.execute("SELECT * FROM outbound_documents WHERE event_id=?", (row["event_id"],)).fetchone() if documents else None
            part = None
            if not document:
                from telegram_formatting import format_telegram
                try:
                    text_delivery.prepare(db, row, owner, format_telegram)
                    part = text_delivery.claim_part(db, row["event_id"])
                    if part is None:
                        raise ValueError("No pending text part has a deliverable receipt.")
                except (ValueError, TypeError) as error:
                    db.execute("UPDATE outbox SET state='failed',error=? WHERE event_id=?",
                               (redact(str(error)), row["event_id"]))
                    return True
            db.execute("UPDATE outbox SET state='sending' WHERE event_id=?", (row["event_id"],))
        attempted_upload = False
        try:
            if document:
                if row["event_id"].startswith("artifact:"):
                    data, mime, kind = self.artifacts.verified(document)
                    attempted_upload = True
                    result = self.api.send_artifact(document["owner"], data, filename=document["filename"],
                                                    caption=row["text"], mime=mime, kind=kind)
                    self.artifacts.verify_response(document, result, kind)
                else:
                    from document_delivery import OwnerDocuments
                    documents_service = OwnerDocuments(self.store)
                    data = documents_service.verified_bytes(document)
                    requested = documents_service.is_requested(document)
                    options = {"filename": document["filename"], "caption": row["text"]}
                    if requested:
                        options["mime"] = documents_service.mime_type(document)
                    attempted_upload = True
                    result = self.api.send_document(document["owner"], data, **options)
                    if requested:
                        documents_service.record_requested_sent(event_id=row["event_id"], result=result)
                        return True
            else:
                payload = {"chat_id": owner, "text": part["text"], "link_preview_options": {"is_disabled": True}}
                if part["part_index"] > 0:
                    payload["disable_notification"] = True
                if part["parse_mode"]:
                    payload["parse_mode"] = part["parse_mode"]
                if row["event_id"].startswith("report-response:") and part["part_index"] == 0:
                    with self.store.db() as db:
                        origin = db.execute(
                            "SELECT i.message_id FROM project_report_responses r JOIN inbox i "
                            "ON i.update_id=r.origin_update_id WHERE r.event_id=?", (row["event_id"],),
                        ).fetchone()
                    if origin and origin["message_id"] > 0:
                        payload["reply_parameters"] = {"message_id": origin["message_id"], "allow_sending_without_reply": True}
                result = self.api.call(
                    "sendMessage", **payload,
                )
            with self.store.db() as db:
                if document:
                    db.execute(
                        "UPDATE outbox SET state='sent',message_id=?,sent_at=?,error=NULL WHERE event_id=?",
                        (result["message_id"], time.time(), row["event_id"]),
                    )
                    db.execute(
                        "UPDATE outbound_documents SET telegram_file_id=? WHERE event_id=?",
                        ((result.get("document") or (result.get("photo") or [{}])[-1]).get("file_id"), row["event_id"]),
                    )
                else:
                    text_delivery.record_sent(db, row["event_id"], part["part_index"], result["message_id"], result=result)
        except (ValueError, OSError) as error:
            with self.store.db() as db:
                # A local error after sendMessage may follow a successful send;
                # only an explicit Telegram rejection proves non-delivery.
                text_delivery.record_failure(db, row["event_id"], part["part_index"] if part else None,
                                             "failed" if document and not attempted_upload else "uncertain", redact(str(error)))
        except TelegramError as error:
            with self.store.db() as db:
                text_delivery.record_failure(db, row["event_id"], part["part_index"] if part else None,
                                             "uncertain" if error.uncertain else "failed", str(error))
                if error.diagnostic:
                    db.execute("INSERT OR REPLACE INTO delivery_diagnostics VALUES(?,?,?)",
                               (row["event_id"], json.dumps(error.diagnostic), time.time()))
        except Exception:
            with self.store.db() as db:
                text_delivery.record_failure(db, row["event_id"], part["part_index"] if part else None,
                                             "uncertain", "Delivery result could not be recorded")
        return True

    def poll_loop(self):
        while not STOP.is_set():
            if self.api is None:
                STOP.wait(1)
                continue
            try:
                updates = self.api.call(
                    "getUpdates", offset=self.store.get("telegram_offset", 0),
                    timeout=5, limit=50, allowed_updates=["message"],
                )
                for update in updates:
                    self.handle_update(update)
                    if isinstance(update.get("update_id"), int):
                        self.store.set("telegram_offset", update["update_id"] + 1)
                self.store.set("last_poll_at", time.time())
                self.store.set("poll_error", None)
                if not getattr(self, "separate_delivery", False):
                    for _ in range(10):
                        if not self.flush_one():
                            break
                        STOP.wait(1.05)
            except TelegramError as error:
                self.store.set("poll_error", str(error))
                STOP.wait(5)
            except Exception:
                self.store.set("poll_error", "Local polling failed; inspect the relay state.")
                STOP.wait(5)

    def handle_event(self, task, record, expected_thread_id=None):
        with self.registry_lock:
            if expected_thread_id is not None and self.tasks.get(task, {}).get("thread_id") != expected_thread_id:
                return
            return self._handle_event(task, record)

    def _handle_event(self, task, record):
        payload = record.get("payload") or {}
        if record.get("type") != "event_msg":
            return
        activity = getattr(self, "project_activity", None)
        if activity is not None:
            activity.observe(task, self.tasks.get(task, {}).get("thread_id"), record)
        kind = payload.get("type")
        try:
            observed = datetime.fromisoformat(record["timestamp"].replace("Z", "+00:00")).timestamp()
        except (ValueError, KeyError, AttributeError):
            observed = time.time()
        if kind == "task_started":
            self.store.task_state(task, "working", observed)
            return
        if kind not in ("task_complete", "turn_aborted", "error"):
            return
        status = {"task_complete": "idle", "turn_aborted": "interrupted", "error": "error"}[kind]
        self.store.task_state(task, status, observed)
        info = self.tasks.get(task, {})
        reports = getattr(self, "project_reports", None)
        if reports is not None and kind == "task_complete":
            reports.record_reply(task, info.get("thread_id", ""), payload.get("last_agent_message"),
                                 observed, f"task_complete:{payload.get('turn_id') or 'unknown'}")
        if not info.get("notify", True) or task == "coordinator":
            return
        message = str(payload.get("last_agent_message") or "")
        if "<heartbeat>" in message:
            if "<decision>NOTIFY</decision>" not in message:
                return
            match = re.search(r"<message>([\s\S]*?)</message>", message)
            message = match[1] if match else ""
        if "DONT_NOTIFY" in message:
            return
        checklist = extract_project_checklist(message, status)
        self.store.save_project_checklist(task, info.get("thread_id", ""), status, checklist, observed,
                                          evidence=[f"Reported by task cycle {payload.get('turn_id') or 'unknown'}; cycle status alone is not implementation proof."])
        # Cycle completion, tests, heartbeats and cached checklists only update
        # on-demand state. A separately reviewed milestone record is required
        # to admit any proactive project push.

    def scan_loop(self, session_root: Path):
        paths = {}
        first_discovery = True
        discovered_at = 0
        while not STOP.is_set():
            try:
                if time.time() - discovered_at >= 30:
                    for path in session_root.rglob("*.jsonl"):
                        alias = next(
                            (key for key, info in self.tasks.items() if path.name.endswith(info["thread_id"] + ".jsonl")),
                            None,
                        )
                        if alias is None:
                            continue
                        if path not in paths:
                            with self.store.db() as db:
                                existing = db.execute("SELECT 1 FROM cursors WHERE path=?", (str(path),)).fetchone()
                            if not existing:
                                with self.store.db() as db:
                                    # Do not flood the newly paired chat with historical completions.
                                    db.execute(
                                        "INSERT OR IGNORE INTO cursors VALUES(?,?)",
                                        (str(path), path.stat().st_size if first_discovery else 0),
                                    )
                        paths[path] = alias
                    discovered_at = time.time()
                    first_discovery = False
                for path, alias in list(paths.items()):
                    expected_thread = self.tasks.get(alias, {}).get("thread_id")
                    if not expected_thread or not path.name.endswith(expected_thread + ".jsonl"):
                        paths.pop(path, None)
                        continue
                    if not path.exists():
                        paths.pop(path, None)
                        continue
                    with self.store.db() as db:
                        row = db.execute("SELECT position FROM cursors WHERE path=?", (str(path),)).fetchone()
                    position = row[0] if row else 0
                    original_position = position
                    if path.stat().st_size < position:
                        position = 0
                    with path.open("rb") as stream:
                        project_activity = getattr(self, "project_activity", None)
                        if project_activity is not None:
                            project_activity.prepare(alias, expected_thread, stream)
                        stream.seek(position)
                        # Bound each scan; skip large non-event payloads without retaining them.
                        for _ in range(400):
                            start = stream.tell()
                            line = stream.readline(2 * 1024 * 1024)
                            if not line:
                                break
                            if not line.endswith(b"\n"):
                                if len(line) == 2 * 1024 * 1024:
                                    complete = False
                                    while line:
                                        if line.endswith(b"\n"):
                                            complete = True
                                            break
                                        line = stream.readline(2 * 1024 * 1024)
                                    if complete:
                                        position = stream.tell()
                                        continue
                                position = start
                                break
                            position = stream.tell()
                            if b'"event_msg"' not in line[:200]:
                                continue
                            try:
                                record = json.loads(line)
                            except (ValueError, UnicodeDecodeError):
                                continue
                            self.handle_event(alias, record, expected_thread_id=expected_thread)
                        if project_activity is not None:
                            project_activity.checked(alias, expected_thread,
                                                     caught_up=position >= path.stat().st_size)
                    if position != original_position:
                        with self.store.db() as db:
                            db.execute("UPDATE cursors SET position=? WHERE path=?", (position, str(path)))
                self.store.set("last_scan_at", time.time())
                self.store.set("watcher_error", None)
                activity = getattr(self, "codex_activity", None)
                if activity is not None:
                    activity.poll()
            except Exception as error:
                self.store.set(
                    "watcher_error",
                    f"Task history scan needs attention ({type(error).__name__}); "
                    "coordinator status checks remain available.",
                )
            STOP.wait(3)


def setup_page(relay, csrf):
    status = relay.status()
    safe = lambda value: html.escape(str(value), quote=True)
    if status["paired"]:
        badge = "Connected" if status["online"] and status["watching"] else "Checking connection"
        content = (
            f'<div class="badge">{badge}</div><h1>Your projects, on Telegram.</h1>'
            f"<p>@{safe(status['bot_username'])} is paired with your private chat.</p>"
            "<p>Completion updates arrive here. Reply to an update to give that project "
            "instructions, or use <code>/status</code>.</p>"
            "<p class=\"note\">Keep this computer awake and Codex running. The coordinator "
            "checks replies every ten minutes.</p>"
        )
    elif status["configured"]:
        command = "/start " + str(relay.store.get("pair_code", ""))
        content = (
            '<div class="badge">One step left</div><h1>Pair your Telegram account.</h1>'
            f"<p>Open <strong>@{safe(status['bot_username'])}</strong> in Telegram and send:</p>"
            f'<pre id="pair-command">{safe(command)}</pre>'
            "<p>Only the account that sends this one-time code can control the relay.</p>"
            '<p><a href="/">Check connection</a></p>'
        )
    else:
        content = (
            '<div class="badge">Private local setup</div><h1>Connect Telegram.</h1>'
            "<p>Use the token issued by Telegram's official BotFather for your dedicated bot. "
            "It will be encrypted for your Windows account.</p>"
            '<form method="post" action="/configure">'
            f'<input type="hidden" name="csrf" value="{safe(csrf)}">'
            '<label for="token">Bot token</label>'
            '<input id="token" type="password" name="token" autocomplete="off" required>'
            '<button type="submit">Connect bot</button></form>'
        )
    return (
        '<!doctype html><html lang="en"><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        '<title>Vega · Telegram connection</title><style>'
        '*{box-sizing:border-box}body{margin:0;background:#f5f5f2;color:#172927;'
        'font:17px/1.55 system-ui,sans-serif}main{max-width:660px;margin:9vh auto;padding:36px}'
        '.brand{font-weight:750;letter-spacing:.13em;margin-bottom:42px}'
        '.badge{font-size:13px;color:#326956;font-weight:650}h1{font-size:38px;line-height:1.15;'
        'letter-spacing:-.04em;margin:14px 0 24px}p{color:#52615e}'
        'label{display:block;margin:28px 0 8px}input[type=password]{width:100%;padding:13px;'
        'border:1px solid #a9b9b2;border-radius:8px;background:white;font:inherit}'
        'button{margin-top:16px;padding:13px 22px;border:0;border-radius:8px;background:#285f4c;'
        'color:white;font:inherit;cursor:pointer}pre{padding:20px;background:white;'
        'border:1px solid #d9dfda;overflow-wrap:anywhere;white-space:pre-wrap}'
        '.note{font-size:14px}a{color:#285f4c}</style>'
        '<main><div class="brand">VEGA</div>' + content + "</main></html>"
    )


def handler_for(relay, port):
    csrf = secrets.token_urlsafe(32)
    authority = f"127.0.0.1:{port}"
    origin = "http://" + authority

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass  # Never put credentials or request bodies in server logs.

        def respond(self, code, body, content_type="text/html; charset=utf-8"):
            self.send_response(code)
            self.send_header("Content-Type", content_type)
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("X-Frame-Options", "DENY")
            self.send_header(
                "Content-Security-Policy",
                "default-src 'none'; style-src 'unsafe-inline'; form-action 'self'; frame-ancestors 'none'; base-uri 'none'",
            )
            self.end_headers()
            self.wfile.write(body.encode())

        def do_GET(self):
            if self.headers.get("Host") != authority:
                self.respond(403, "Unexpected host.")
            elif self.path == "/":
                self.respond(200, setup_page(relay, csrf))
            elif self.path == "/api/status":
                self.respond(200, json.dumps(relay.status()), "application/json")
            else:
                self.respond(404, "Not found.")

        def do_POST(self):
            if self.headers.get("Host") != authority or self.headers.get("Origin") != origin:
                self.respond(403, "Only the local setup page may connect this bot.")
                return
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if not 0 < length <= 4096:
                    raise ValueError("Invalid setup request.")
                form = parse_qs(self.rfile.read(length).decode(), strict_parsing=True)
                supplied = form.get("csrf", [""])[0]
                if not hmac.compare_digest(supplied, csrf):
                    self.respond(403, "This setup form expired. Reload it.")
                    return
                if self.path != "/configure":
                    self.respond(404, "Not found.")
                    return
                relay.configure(form.get("token", [""])[0])
                self.send_response(303)
                self.send_header("Location", "/")
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
            except (ValueError, RuntimeError, TelegramError) as error:
                self.respond(400, html.escape(redact(str(error))) + '<p><a href="/">Return to setup</a></p>')
            except Exception:
                self.respond(500, "Setup could not complete. No credential is displayed. Check relay status.")

    return Handler


def load_tasks(path=ROOT / "tasks.json"):
    result = json.loads(path.read_text(encoding="utf-8"))
    for alias, info in result.items():
        if not re.fullmatch(r"[a-z0-9-]+", alias) or not re.fullmatch(r"[a-f0-9-]{36}", info["thread_id"]):
            raise ValueError("Invalid project routing registry.")
        dispatch_target(result, alias)
    return result


def main():
    if sys.stdout is not None and hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state-dir", type=Path, default=ROOT / ".state")
    sub = parser.add_subparsers(dest="command", required=True)
    server_command = sub.add_parser("serve")
    server_command.add_argument("--port", type=int, default=8769)
    sub.add_parser("status")
    inbox_command = sub.add_parser("inbox")
    inbox_command.add_argument("--claim", action="store_true")
    ack = sub.add_parser("ack")
    ack.add_argument("--id", type=int, required=True)
    ack.add_argument("--receipt", required=True)
    notify = sub.add_parser("notify")
    notify.add_argument("--task", default="coordinator")
    notify.add_argument("--key", required=True)
    notify.add_argument("--text-file", type=Path, required=True)
    notify.add_argument("--requested", action="store_true", help="Only an explicitly requested owner delivery.")
    notify.add_argument("--request-ref", help="inbox:<update-id> or codex:<thread-id>:<owner-turn-id>")
    milestone = sub.add_parser("milestone", help="Queue one newly verified project milestone from its reviewed record.")
    milestone.add_argument("--file", type=Path, required=True)
    sync = sub.add_parser("sync")
    sync.add_argument("--file", type=Path, required=True)
    read_report = sub.add_parser("read-report", help="Read content through the registered owner report/reply scope.")
    read_report.add_argument("--query", required=True)
    response = sub.add_parser("report-response", help="Return a registered report to its original Telegram request.")
    response.add_argument("--origin-id", type=int)
    response.add_argument("--retry-failed", help="Explicitly retry only definitely rejected parts of this saved response.")
    response.add_argument("--receipts", action="store_true")
    sub.add_parser("receipts")
    media = sub.add_parser("media")
    media.add_argument("--download", type=int)
    media.add_argument("--limit", type=int, default=30)
    document = sub.add_parser("queue-document")
    document.add_argument("--task", required=True)
    document.add_argument("--key", required=True)
    document.add_argument("--file", type=Path, required=True)
    document.add_argument("--sha256", required=True)
    document.add_argument("--size", type=int, required=True)
    requested_file = sub.add_parser("queue-requested-file", help="Queue the single reviewed owner-requested Tuturu M4A.")
    requested_file.add_argument("--reviewed-owner", type=int, required=True)
    sub.add_parser("enable-photo-batches", help="Enable only the verified owner request for future photo batches.")
    photo_read = sub.add_parser("photo-batch", help="Read retained items and actual receipts for one owner photo batch.")
    photo_read.add_argument("--id", required=True)
    args = parser.parse_args()
    if args.command == "serve":
        raise SystemExit("Use the 'Vega Independent Companion' Windows task. This CLI cannot start a second Telegram receiver.")
    if args.command == "photo-batch":
        class PhotoReadStore:
            get = Store.get
            @contextlib.contextmanager
            def db(self):
                path = (args.state_dir / "relay.sqlite3").resolve()
                connection = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=10)
                connection.row_factory = sqlite3.Row
                try:
                    yield connection
                finally:
                    connection.close()
        readonly = PhotoReadStore()
        result = PhotoBatches(readonly, load_tasks, install=False).get(readonly.get("owner_id"), args.id)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return
    store = Store(args.state_dir)
    if args.command == "enable-photo-batches":
        from photo_batches import load_photo_batch_source_review
        with urllib.request.urlopen("http://127.0.0.1:8769/api/status", timeout=8) as response:
            capability = json.load(response)
        if capability.get("photo_batches_version") != 1:
            raise ValueError("Photo batching is not loaded in the running Vega host; the grant was not enabled.")
        result = PhotoBatches(store, load_tasks).authorize(source_record=load_photo_batch_source_review())
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return
    if args.command == "queue-requested-file":
        from document_delivery import OwnerDocuments, load_requested_source_record
        try:
            with urllib.request.urlopen("http://127.0.0.1:8769/api/status", timeout=8) as response:
                capability = json.load(response)
        except (OSError, ValueError):
            raise ValueError("The running Vega file-delivery capability could not be verified; nothing was queued.") from None
        if capability.get("requested_files_version") != 1:
            raise ValueError("The reviewed M4A delivery path is not loaded in the running Vega host; nothing was queued.")
        result = OwnerDocuments(store).queue_requested(
            source_record=load_requested_source_record(), reviewed_owner=args.reviewed_owner)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return
    relay = Relay(store, load_tasks())
    if args.command == "status":
        print(json.dumps(relay.status(), ensure_ascii=False, indent=2))
    elif args.command == "inbox":
        items = store.inbox(claim=args.claim, tasks=relay.tasks)
        for item in items:
            item["source"] = "local_owner_ui" if item["message_id"] == 0 else "paired_owner_direct_telegram"
            destination = dispatch_target(relay.tasks, item["task"])
            item["target_thread_id"] = destination["thread_id"]
            item["target_label"] = destination["label"]
            item["watch_thread_id"] = relay.tasks[item["task"]]["thread_id"]
            item["route_via_coordinator"] = relay.tasks[item["task"]].get("route_via_coordinator", False)
            if item.get("notification_context", {}).get("owner_source"):
                item["source"] = item["notification_context"]["owner_source"]
        print(json.dumps(items, ensure_ascii=False, indent=2))
    elif args.command == "ack":
        with store.db() as db:
            row = db.execute("SELECT * FROM inbox WHERE update_id=?", (args.id,)).fetchone()
            if row is None:
                raise ValueError("Unknown inbox receipt.")
            structured = db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='project_request_routes'").fetchone()
            if structured and db.execute("SELECT 1 FROM project_request_routes WHERE update_id=?", (args.id,)).fetchone():
                if row["state"] not in ("routing", "handled"):
                    raise ValueError("This project request is cancelled, held or not yet claimed; its receipt cannot be overwritten.")
            if row["state"] != "handled":
                db.execute(
                    "UPDATE inbox SET state='handled',receipt=?,handled_at=? WHERE update_id=?",
                    (args.receipt[:2000], time.time(), args.id),
                )
        # Routing progress remains available in the request view. The original
        # owner command already received its direct acceptance reply.
        print(json.dumps({"handled": args.id, "receipt_saved": True, "notification_queued": False}))
    elif args.command == "notify":
        if args.task not in relay.tasks:
            raise ValueError("Unknown project.")
        text = args.text_file.read_text(encoding="utf-8")
        if not args.requested:
            raise ValueError("Routine project pushes are disabled. Use a reviewed milestone record, or identify a current explicit owner delivery.")
        print(json.dumps(relay.milestones.requested_text(args.task, args.key, text, args.request_ref)))
    elif args.command == "milestone":
        print(json.dumps(relay.milestones.announce(json.loads(args.file.read_text(encoding="utf-8"))), ensure_ascii=False, indent=2))
    elif args.command == "sync":
        values = json.loads(args.file.read_text(encoding="utf-8"))
        if not isinstance(values, dict) or not values:
            raise ValueError("Use a nonempty project status snapshot.")
        for alias, value in values.items():
            if alias not in relay.tasks or not isinstance(value, dict) or not isinstance(value.get("status"), str):
                raise ValueError("Unknown or invalid project in status snapshot.")
            observed = timestamp_seconds(value.get("observed_at"))
            if observed is None or observed > time.time() + 30:
                raise ValueError("Each project update needs its original source timestamp; import time is not progress.")
            if "progress" in value:
                normalize_progress(value["progress"], value["observed_at"])
        progress_service = LiveProgress(store, lambda: relay.tasks) if any("progress" in v for v in values.values()) else None
        for alias, value in values.items():
            if alias not in relay.tasks:
                raise ValueError("Unknown project in status snapshot.")
            store.task_state(alias, value["status"], value.get("observed_at"))
            if "checklist" in value:
                store.save_project_checklist(alias, relay.tasks[alias]["thread_id"], value["status"], value["checklist"],
                                             value.get("observed_at"), source="coordinator_report", evidence=value.get("evidence"))
            if "progress" in value:
                progress_service.save(store.get("owner_id"), alias, value["progress"], value["observed_at"])
        print(json.dumps({"updated": len(values)}))
    elif args.command in ("read-report", "report-response"):
        from project_reports import ProjectReports
        reports = ProjectReports(store, lambda: relay.tasks)
        owner = store.get("owner_id")
        if args.command == "read-report":
            query = reports.parse_query(args.query)
            if not query:
                raise ValueError("Ask for a registered report or a visible project reply, such as /report.")
            print(json.dumps(reports.read(owner, query), ensure_ascii=False, indent=2))
        elif args.receipts:
            print(json.dumps(relay.project_responses.list(owner, update_id=args.origin_id), ensure_ascii=False, indent=2))
        elif args.retry_failed:
            print(json.dumps(relay.project_responses.retry_failed(owner, args.retry_failed), ensure_ascii=False, indent=2))
        else:
            if type(args.origin_id) is not int:
                raise ValueError("Identify the original owner Telegram report request.")
            with store.db() as db:
                revision = relay.project_responses._principal(db, owner)
                original = relay.project_responses._origin(db, owner, args.origin_id)
            # Re-read the confirmed owner's actual request. The CLI cannot
            # substitute an unrelated report or an arbitrary filesystem path.
            query = reports.parse_query(original["text"])
            if not query:
                raise ValueError("The original message does not explicitly request registered project information.")
            report = reports.read(owner, query)
            if not report.get("available"):
                print(json.dumps({"queued": False, "state": "source_unavailable", "error": report["text"]}, ensure_ascii=False))
            else:
                print(json.dumps(relay.project_responses.queue(owner, args.origin_id, report,
                                                               expected_preference_revision=revision),
                                 ensure_ascii=False, indent=2))
    elif args.command == "receipts":
        with store.db() as db:
            rows = db.execute(
                "SELECT o.event_id,o.task,o.state,o.message_id,o.sent_at,o.error,d.details AS diagnostic "
                "FROM outbox o LEFT JOIN delivery_diagnostics d ON d.event_id=o.event_id ORDER BY o.created_at DESC LIMIT 30"
            ).fetchall()
            receipts = [{**dict(row), "diagnostic": json.loads(row["diagnostic"]) if row["diagnostic"] else None,
                         "text_delivery": text_delivery.receipt(db, row["event_id"])} for row in rows]
        print(json.dumps(receipts, ensure_ascii=False, indent=2))
    elif args.command == "media":
        from media_store import OwnerMedia
        media = OwnerMedia(store)
        if args.download is not None:
            if not relay.api:
                raise ValueError("The existing Telegram connection is unavailable.")
            result = media.download(args.download, relay.api)
        else:
            result = media.list(args.limit)
        print(json.dumps(result, ensure_ascii=False, indent=2))
    elif args.command == "queue-document":
        from document_delivery import OwnerDocuments
        if args.task not in relay.tasks or args.task == "coordinator":
            raise ValueError("Choose the existing project that owns this reviewed ZIP.")
        result = OwnerDocuments(store).queue(
            source=args.file, sha256=args.sha256, size=args.size, key=args.key,
            task=args.task, label=relay.label(args.task),
        )
        print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    try:
        main()
    except (ValueError, RuntimeError, TelegramError, OSError) as error:
        # Avoid accidental traceback/URL disclosure at the service boundary.
        print(redact(str(error)))
        raise SystemExit(1)
