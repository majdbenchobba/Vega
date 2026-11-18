"""Source-backed progress values. Reads never start or change project work."""
from __future__ import annotations

from contextlib import contextmanager
import hashlib
import json
import math
import os
from pathlib import Path
import re
import stat
import threading
import time

from companion import ConflictError
from memory_policy import contains_secret
from project_files import ProjectFiles, MAX_BYTES, file_identity, relative_path, text_from_bytes
from project_freshness import timestamp_seconds
from routing_targets import dispatch_target


ROOT = Path(__file__).resolve().parent


def install(db):
    db.execute("""
        CREATE TABLE IF NOT EXISTS project_progress_reports (
            owner INTEGER NOT NULL, task TEXT NOT NULL, thread_id TEXT NOT NULL,
            observed_at REAL NOT NULL, progress TEXT NOT NULL,
            PRIMARY KEY(owner,task)
        )
    """)


def _text(value, maximum=600):
    if (not isinstance(value, str) or not value.strip() or len(value) > maximum or contains_secret(value)
            or any(ord(char) < 32 and char not in "\n\t" for char in value)):
        raise ValueError("Progress needs concise non-secret source and scope descriptions.")
    return value.strip()


def _number(value):
    if type(value) not in (int, float) or not math.isfinite(value) or value < 0:
        raise ValueError("Progress values must be finite, non-negative numbers.")
    return float(value)


def normalize_progress(value, observed_at, *, now=None):
    if not isinstance(value, dict) or set(value) - {
        "kind", "percent", "completed", "total", "unit", "scope", "summary", "source",
        "session_id",
    }:
        raise ValueError("Use the structured progress fields.")
    stamp = timestamp_seconds(observed_at)
    now = time.time() if now is None else now
    if stamp is None or stamp > now + 30:
        raise ValueError("Progress needs its actual source or assessment time.")
    kind = value.get("kind")
    if kind not in {"measured", "estimate", "reported", "unknown"}:
        raise ValueError("Identify measured, estimated, reported or unknown progress.")
    scope = _text(value.get("scope"), 160)
    summary = _text(value.get("summary"))
    source = _text(value.get("source"), 240)
    session = value.get("session_id")
    if session is not None and (kind != "measured" or not isinstance(session, str)
                               or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9-]{0,99}", session)):
        raise ValueError("A measured source session must be a registered project-local identifier.")
    percent, completed, total = None, None, None
    if kind == "measured":
        completed, total = _number(value.get("completed")), _number(value.get("total"))
        if total == 0:
            raise ValueError("Measured progress needs a positive target.")
        percent = min(100.0, completed / total * 100)
        if value.get("percent") is not None and abs(_number(value["percent"]) - percent) > .05:
            raise ValueError("The measured percentage disagrees with the recorded amounts.")
        unit = _text(value.get("unit"), 80)
        basis = f"{completed:,.2f} / {total:,.0f} {unit}"
    elif kind in {"estimate", "reported"}:
        percent = _number(value.get("percent"))
        if percent > 100:
            raise ValueError("A progress percentage must be between zero and 100.")
        basis = summary
    else:
        if value.get("percent") is not None:
            raise ValueError("Unknown progress cannot contain an invented percentage.")
        basis = summary
    label = "Not yet estimated" if percent is None else ("≈" if kind == "estimate" else "") + f"{percent:g}%"
    return {
        "kind": kind, "percent": percent, "display_progress": label,
        "scope": scope, "basis_and_remaining": basis, "summary": summary,
        "assessed_at": stamp, "source": source, "source_error": None,
        "completed": completed, "total": total, "unit": value.get("unit"),
        "automatic": False,
        "session_id": session,
    }


class LiveProgress:
    def __init__(self, store, tasks, *, config=None, config_path=None, clock=time.time):
        self.store, self.tasks, self.clock = store, tasks, clock
        self.config = config
        self.config_path = Path(config_path) if config_path else ROOT / "project-progress-sources.json"
        self.config_root = ProjectFiles(self.config_path.parent).identity()
        self.cache = {}
        self.cache_lock = threading.RLock()

    def _principal(self, db, owner):
        data = {r["key"]: json.loads(r["value"]) for r in db.execute(
            "SELECT key,value FROM settings WHERE key IN ('owner_id','owner_chat_id')")}
        if type(owner) is not int or data.get("owner_id") != owner or data.get("owner_chat_id") != owner:
            raise PermissionError("The paired owner is required.")
        pref = db.execute("SELECT revision,payload FROM companion_preferences WHERE owner=?", (owner,)).fetchone()
        if pref and not json.loads(pref["payload"]).get("project_context_enabled", True):
            raise PermissionError("Project context is disabled.")
        return pref["revision"] if pref else 0

    def save(self, owner, task, value, observed_at):
        normalized = normalize_progress(value, observed_at, now=self.clock())
        target = dispatch_target(self.tasks(), task)
        with self.store.db() as db:
            self._principal(db, owner)
            db.execute(
                "INSERT INTO project_progress_reports VALUES(?,?,?,?,?) "
                "ON CONFLICT(owner,task) DO UPDATE SET thread_id=excluded.thread_id,"
                "observed_at=excluded.observed_at,progress=excluded.progress "
                "WHERE excluded.observed_at>=project_progress_reports.observed_at",
                (owner, task, target["thread_id"], normalized["assessed_at"], json.dumps(normalized)),
            )
        return normalized

    def _config(self):
        value = self.config
        if value is None:
            value = json.loads(ProjectFiles(self.config_path.parent, self.config_root).read(self.config_path.name)["text"])
        if (not isinstance(value, dict) or value.get("version") != 1 or not isinstance(value.get("sources"), list)
                or len(value["sources"]) > 32 or any(not isinstance(item, dict) for item in value["sources"])):
            raise ValueError("The registered progress source configuration is invalid.")
        return value

    @staticmethod
    def _snapshot(files, relative):
        """Share the producer's in-place snapshot writes and reject a torn read."""
        relative = relative_path(relative)
        if Path(relative).suffix != ".json":
            raise ValueError("Only the registered JSON snapshot can be read.")
        path = files.root / relative
        parent = Path(relative).parent.as_posix()
        with files.open("" if parent == "." else parent, directory=True):
            if os.name == "nt":
                from project_files import _windows_handle
                manager = _windows_handle(path, directory=False, appendable_read=True, replaceable_read=True)
            else:
                @contextmanager
                def open_posix():
                    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
                    with os.fdopen(descriptor, "rb") as stream:
                        yield stream
                manager = open_posix()
            with manager as stream:
                before = os.fstat(stream.fileno())
                data = stream.read(MAX_BYTES + 1)
                after = os.fstat(stream.fileno())
                current = path.lstat()
                if (not stat.S_ISREG(current.st_mode) or getattr(current, "st_file_attributes", 0) & 0x400
                        or current.st_nlink != 1 or after.st_nlink != 1
                        or file_identity(before) != file_identity(current)
                        or before.st_size != after.st_size or before.st_mtime_ns != after.st_mtime_ns):
                    raise ValueError("The progress source changed during its read.")
        text = text_from_bytes(data)
        value = json.loads(text, parse_constant=lambda _: (_ for _ in ()).throw(ValueError("Invalid progress number.")))
        return value, hashlib.sha256(data).hexdigest()

    def _source(self, entry, session_override=None):
        if entry.get("adapter") != "training_source_hours":
            raise ValueError("This progress adapter is not registered.")
        files = ProjectFiles(entry["root"], entry["root_identity"])
        # The integration owner supplies the current session through the
        # registered config or a source-dated sync. Do not poll the producer's
        # atomically replaced operation/control file while its job is running.
        session = session_override or entry.get("session_id")
        if not isinstance(session, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9-]{0,99}", session):
            raise ValueError("The current progress session is invalid.")
        total = _number(entry["target_hours"])
        path = relative_path(entry["session_base"]) + "/" + session + "/latest-progress.json"
        snapshot, snapshot_hash = self._snapshot(files, path)
        pipeline = snapshot.get("state", {}).get("pipeline-state.json")
        if not isinstance(pipeline, dict):
            raise ValueError("The current pipeline measurement is missing.")
        stamp = timestamp_seconds(pipeline.get("updated_at_utc"))
        checked = timestamp_seconds(snapshot.get("observed_at_utc"))
        if (stamp is None or checked is None or stamp > checked + 30 or checked > self.clock() + 30
                or stamp > self.clock() + 30):
            raise ValueError("The measurement timestamp is invalid.")
        hours = _number(pipeline.get("progress", {}).get("accepted_training_hours"))
        updates = pipeline.get("optimizer_updates")
        if type(updates) is not int or updates < 0:
            raise ValueError("The training counter is unavailable.")
        result = normalize_progress({
            "kind": "measured", "completed": hours, "total": total,
            "unit": "accepted source hours", "scope": "Training-source collection",
            "summary": f"Training: {updates:,} updates recorded.",
            "source": "Existing local training progress snapshot",
        }, stamp, now=self.clock())
        result.update(
            display_progress=f"{min(result['percent'], 99.9) if hours < total else 100:.1f}% data",
            automatic=True, checked_at=self.clock(), optimizer_updates=updates,
            stale=self.clock() - min(stamp, checked) > 15 * 60,
            source={"path": path, "sha256": snapshot_hash, "session": session,
                    "target_hours": total},
        )
        return result

    def current(self, owner, *, scope=None):
        tasks = json.loads(json.dumps(self.tasks()))
        with self.store.db() as db:
            revision = self._principal(db, owner)
            rows = [dict(row) for row in db.execute("SELECT * FROM project_progress_reports WHERE owner=?", (owner,))]
        result, errors = {}, {}
        for row in rows:
            alias = row["task"]
            if alias in tasks and (scope is None or scope == alias) and dispatch_target(tasks, alias)["thread_id"] == row["thread_id"]:
                result[alias] = json.loads(row["progress"])
        try:
            config = self._config()
            expected_owner = hashlib.sha256(json.dumps([owner, owner], separators=(",", ":")).encode()).hexdigest()
            for entry in config["sources"] if config.get("owner_binding") == expected_owner else []:
                alias = entry.get("task")
                if alias not in tasks or (scope is not None and scope != alias):
                    continue
                if dispatch_target(tasks, alias)["thread_id"] != entry.get("thread_id"):
                    errors[alias] = "The registered progress thread changed."
                    continue
                try:
                    supplied = result.get(alias, {})
                    session_override = supplied.get("session_id")
                    if session_override and (supplied.get("total") != entry["target_hours"] or supplied.get("kind") != "measured"):
                        raise ValueError("The reviewed measurement no longer matches this collection target.")
                    value = self._source(entry, session_override)
                    with self.cache_lock:
                        self.cache[(owner, alias, entry["thread_id"])] = dict(value)
                    if alias not in result or value["assessed_at"] >= result[alias]["assessed_at"]:
                        result[alias] = value
                except (ValueError, OSError, KeyError, TypeError):
                    errors[alias] = "The current progress source is unavailable."
                    with self.cache_lock:
                        known = self.cache.get((owner, alias, entry["thread_id"]))
                    if known:
                        result[alias] = {**known, "stale": True, "source_error": errors[alias]}
        except (ValueError, OSError, TypeError):
            errors["_configuration"] = "Automatic progress sources could not be checked."
            with self.cache_lock:
                for (prior_owner, alias, thread), known in self.cache.items():
                    if (prior_owner == owner and alias in tasks and (scope is None or scope == alias)
                            and dispatch_target(tasks, alias)["thread_id"] == thread):
                        result[alias] = {**known, "stale": True, "source_error": errors["_configuration"]}
                        errors[alias] = errors["_configuration"]
        with self.store.db() as db:
            if self._principal(db, owner) != revision or self.tasks() != tasks:
                raise ConflictError("The owner context or registered projects changed during the progress read.")
        return {"projects": result, "errors": errors}
