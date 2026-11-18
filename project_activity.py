"""Current public chat activity from the existing registered-session scanner.

This in-memory projection never replays work, changes report dates, or sends a
notification. The scanner alone supplies streams for already watched projects.
"""
from copy import deepcopy
import json
import os
import threading
import time

from codex_activity import MAX_RECORD, TAIL_BYTES, public_event
from project_files import file_identity


class ProjectActivity:
    def __init__(self, store, tasks, *, clock=time.time):
        self.store, self.tasks, self.clock = store, tasks, clock
        self.lock = threading.RLock()
        self.rows = {}
        self.owner = None

    def enabled(self, owner=None):
        with self.store.db() as db:
            values = {r["key"]: json.loads(r["value"]) for r in db.execute(
                "SELECT key,value FROM settings WHERE key IN ('owner_id','owner_chat_id')")}
            actual = values.get("owner_id")
            if type(actual) is not int or values.get("owner_chat_id") != actual or owner not in (None, actual):
                return None
            pref = db.execute("SELECT payload FROM companion_preferences WHERE owner=?", (actual,)).fetchone()
            return actual if not pref or json.loads(pref["payload"]).get("project_context_enabled", True) is True else None

    def bound(self, alias, thread):
        return self.tasks().get(alias, {}).get("thread_id") == thread

    def observe(self, alias, thread, record):
        with self.lock:
            if not self.bound(alias, thread):
                return
            event = public_event(record, thread, self.clock())
            if not event:
                return
            row = self.rows.get(alias)
            if not row or row["thread_id"] != thread:
                return  # A matching session header must first be checked.
            previous = row.get("event")
            if previous and (event["at"] < previous["at"]
                             or event["state"] != "working" and previous["turn_id"]
                             and event["turn_id"] and event["turn_id"] != previous["turn_id"]):
                return
            if previous and event["turn_id"] and event["turn_id"] != previous["turn_id"]:
                row["latest_update"] = None
            row["event"] = event
            if event["kind"] in ("update", "result"):
                row["latest_update"] = event

    def prepare(self, alias, thread, stream):
        """Validate the source and seed a bounded tail without replaying it."""
        with self.lock:
            owner = self.enabled()
            if owner is None:
                self.rows.clear()
                return
            if self.owner != owner:
                self.rows.clear()
                self.owner = owner
            if not self.bound(alias, thread):
                return
            info = os.fstat(stream.fileno())
            identity = file_identity(info)
            stream.seek(0)
            line = stream.readline(64 * 1024 + 1)
            try:
                header = json.loads(line)
                valid = (line.endswith(b"\n") and len(line) <= 64 * 1024
                         and header.get("type") == "session_meta" and header["payload"]["id"] == thread)
            except (ValueError, TypeError, KeyError, AttributeError):
                valid = False
            if not valid:
                self.rows.pop(alias, None)
                return
            old = self.rows.get(alias)
            if old and old["thread_id"] == thread and old["identity"] == identity and info.st_size >= old["size"]:
                old["size"] = info.st_size
                return
            self.rows[alias] = {"thread_id": thread, "identity": identity, "size": info.st_size,
                                "event": None, "latest_update": None, "checked_at": None, "error": None}
            start = max(len(line), info.st_size - TAIL_BYTES)
            stream.seek(start)
            for index, record in enumerate(stream.read(TAIL_BYTES).splitlines(keepends=True)):
                if index == 0 and start > len(line) or not record.endswith(b"\n") or len(record) > MAX_RECORD:
                    continue
                if b'"event_msg"' not in record[:200]:
                    continue
                try:
                    self.observe(alias, thread, json.loads(record))
                except (ValueError, TypeError, UnicodeError):
                    continue

    def checked(self, alias, thread, *, caught_up=True):
        with self.lock:
            row = self.rows.get(alias)
            if row and row["thread_id"] == thread and self.bound(alias, thread):
                row["checked_at"] = self.clock()
                row["error"] = None if caught_up else "Catching up with this chat."

    def views(self, owner):
        with self.lock:
            if self.enabled(owner) != self.owner or self.owner is None:
                self.rows.clear()
                return {}
            now, result = self.clock(), {}
            for alias, row in self.rows.items():
                if not self.bound(alias, row["thread_id"]):
                    continue
                event = row.get("event")
                if not event:
                    continue
                available = bool(row["checked_at"] and now - row["checked_at"] <= 20 and not row["error"])
                state = event["state"] if available else "unavailable"
                if state == "working" and now - event["at"] > 180:
                    state = "quiet"
                label = {"working": "Chat working", "completed": "Turn finished",
                         "interrupted": "Turn interrupted", "error": "Chat needs attention",
                         "quiet": "Waiting for chat update", "unavailable": "Chat update unavailable"}[state]
                result[alias] = {"thread_id": row["thread_id"], "state": state, "label": label,
                                 "available": available, "observed_at": event["at"],
                                 "checked_at": row["checked_at"], "latest_update": row["latest_update"],
                                 "source_error": row["error"], "authority": False}
            return deepcopy(result)
