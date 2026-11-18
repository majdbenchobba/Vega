"""Read the explicitly linked Codex session's public activity, locally.

The feed is observation only. It never resumes a turn, executes a tool, sends
a message, imports reasoning, or treats session text as action authority.
"""
from collections import deque
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import threading
import time
from xml.etree import ElementTree

from project_files import ProjectFiles, file_identity, relative_path, text_from_bytes
from project_freshness import timestamp_seconds

ROOT = Path(__file__).resolve().parent
TAIL_BYTES = 2 * 1024 * 1024
MAX_RECORD = 512 * 1024
STATE_LABELS = {"working": "Codex working", "completed": "Turn finished",
                "interrupted": "Turn interrupted", "error": "Session needs attention",
                "quiet": "No recent update", "unavailable": "Session unavailable",
                "disabled": "Project context is off", "waiting": "Waiting for activity"}


def public_text(body):
    # A scheduler envelope is display data, never an instruction to notify or
    # change the automation. Expose only its human-readable message.
    if body.strip() == "DONT_NOTIFY":
        body = "No update was published by this check."
    elif body.lstrip().startswith("<heartbeat>"):
        try:
            if len(body) > MAX_RECORD or "<!" in body:
                raise ValueError("Unsupported heartbeat content.")
            message = ElementTree.fromstring(body).find("message")
            body = "".join(message.itertext()).strip() if message is not None else ""
            body = body or "No update was published by this check."
        except (ValueError, ElementTree.ParseError):
            body = "The automated check did not provide a readable update."
    try:
        safe = text_from_bytes(body.encode("utf-8"))
        return safe[:2400] + ("… [shortened]" if len(safe) > 2400 else "")
    except (ValueError, UnicodeError):
        return "A public update was omitted because it contains excluded content."


def public_event(record, thread_id, now):
    """Allow only explicit public messages and fixed tool/lifecycle labels."""
    if not isinstance(record, dict) or record.get("type") != "event_msg":
        return None
    payload = record.get("payload")
    at = timestamp_seconds(record.get("timestamp"))
    if (not isinstance(payload, dict) or at is None or at > now + 30
            or payload.get("thread_id") not in (None, thread_id)):
        return None
    kind = payload.get("type")
    state, text, category = "working", "", "activity"
    if kind == "task_started":
        text, category = "Started a turn", "lifecycle"
    elif kind in ("task_complete", "turn_aborted", "error"):
        state = {"task_complete": "completed", "turn_aborted": "interrupted", "error": "error"}[kind]
        text, category = STATE_LABELS[state], "lifecycle"
        if kind == "task_complete" and isinstance(payload.get("last_agent_message"), str) and payload["last_agent_message"].strip():
            text, category = public_text(payload["last_agent_message"]), "result"
    elif kind == "item_completed":
        item = payload.get("item")
        if not isinstance(item, dict):
            return None
        item_type = item.get("type")
        if item_type == "AgentMessage" and item.get("phase") in ("commentary", "final"):
            content = item.get("content")
            if not isinstance(content, list) or not content or len(content) > 32:
                return None
            if any(not isinstance(c, dict) or c.get("type") != "Text" or not isinstance(c.get("text"), str) for c in content):
                return None
            body = "".join(c["text"] for c in content)
            text = public_text(body)
            category = "update" if item["phase"] == "commentary" else "result"
        elif item_type == "CommandExecution":
            code = item.get("exit_code")
            text = ("Command failed" if type(code) is int and code != 0 else
                    "Command finished" if code == 0 else "Command activity recorded")
        elif item_type == "FileChange":
            text = "File edit recorded" if item.get("status") == "completed" else "File edit activity recorded"
        elif item_type == "McpToolCall":
            text = "Tool call finished" if item.get("status") == "completed" else "Tool activity recorded"
        elif item_type == "WebSearch":
            text = "Searched reference material"
        elif item_type == "ImageView":
            text = "Inspected an image"
        else:
            # Reasoning, user messages, tool arguments and tool output are never exposed.
            return None
    else:
        return None
    turn = payload.get("turn_id")
    return {"at": at, "kind": category, "text": text.strip(), "state": state,
            "turn_id": turn if isinstance(turn, str) and len(turn) <= 100 else None}


class CodexActivity:
    def __init__(self, store, tasks, *, config=None, config_path=None, clock=time.time):
        self.store, self.tasks, self.clock = store, tasks, clock
        self.config = config
        self.config_path = Path(config_path) if config_path else ROOT / "codex-session-source.json"
        self.config_root = ProjectFiles(self.config_path.parent).identity()
        self.lock = threading.RLock()
        self.reset()

    def reset(self):
        self.binding = None
        self.position = None
        self.identity = None
        self.events = deque(maxlen=24)
        self.seen = deque(maxlen=128)
        self.state = "waiting"
        self.turn = None
        self.last_at = None
        self.checked_at = None
        self.error = None
        self.source = None
        self.latest_update = None

    def _config(self):
        config = self.config if self.config is not None else json.loads(
            ProjectFiles(self.config_path.parent, self.config_root).read(self.config_path.name)["text"])
        if not isinstance(config, dict) or config.get("version") != 1:
            raise ValueError("No linked Codex session.")
        task = self.tasks().get(config.get("project"))
        if not task or task.get("thread_id") != config.get("thread_id"):
            raise ValueError("The linked project chat changed.")
        relative = relative_path(config["session_file"])
        if not re.fullmatch(r"\d{4}/\d{2}/\d{2}/rollout-[0-9T-]+-" + re.escape(task["thread_id"]) + r"\.jsonl", relative):
            raise ValueError("The linked session path does not match its chat.")
        return json.loads(json.dumps(config))

    def _principal(self, config, owner=None):
        with self.store.db() as db:
            values = {r["key"]: json.loads(r["value"]) for r in db.execute(
                "SELECT key,value FROM settings WHERE key IN ('owner_id','owner_chat_id')")}
            actual = values.get("owner_id")
            binding = hashlib.sha256(json.dumps([actual, actual], separators=(",", ":")).encode()).hexdigest()
            if (type(actual) is not int or values.get("owner_chat_id") != actual
                    or owner not in (None, actual) or binding != config.get("owner_binding")):
                raise PermissionError("The paired owner is required.")
            pref = db.execute("SELECT revision,payload FROM companion_preferences WHERE owner=?", (actual,)).fetchone()
            if pref and json.loads(pref["payload"]).get("project_context_enabled", True) is not True:
                raise PermissionError("Project context is off.")
            return actual, pref["revision"] if pref else 0

    @contextmanager
    def _stream(self, config):
        files = ProjectFiles(config["session_root"], config["session_root_identity"])
        relative = relative_path(config["session_file"])
        parent = Path(relative).parent.as_posix()
        with files.open(parent, directory=True):
            path = files.root / relative
            if os.name == "nt":
                from project_files import _windows_handle
                with _windows_handle(path, directory=False, appendable_read=True) as stream:
                    yield stream
            else:
                descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
                with os.fdopen(descriptor, "rb") as stream:
                    info = os.fstat(stream.fileno())
                    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                        raise ValueError("Linked files are excluded.")
                    yield stream

    def poll(self):
        with self.lock:
            try:
                config = self._config()
                principal = self._principal(config)
                binding = json.dumps(config, sort_keys=True)
                if binding != self.binding:
                    self.reset()
                    self.binding = binding
                ProjectFiles(config["project_root"], config["project_root_identity"]).identity()
                with self._stream(config) as stream:
                    before = os.fstat(stream.fileno())
                    header = stream.readline(64 * 1024 + 1)
                    if not header.endswith(b"\n") or len(header) > 64 * 1024:
                        raise ValueError("The session header is unavailable.")
                    meta = json.loads(header)
                    payload = meta.get("payload") if isinstance(meta, dict) else None
                    if (not isinstance(payload, dict) or meta.get("type") != "session_meta" or payload.get("id") != config["thread_id"]
                            or os.path.normcase(str(Path(payload.get("cwd", "")))) != os.path.normcase(str(Path(config["project_root"])))):
                        raise ValueError("The session header differs from the linked project.")
                    identity = file_identity(before)
                    reset = self.position is None or self.identity != identity or before.st_size < self.position
                    start = max(len(header), before.st_size - TAIL_BYTES) if reset else self.position
                    # Catch up from a bounded recent tail if a producer outran this reader.
                    clipped = before.st_size - start > TAIL_BYTES
                    if clipped:
                        start = before.st_size - TAIL_BYTES
                    stream.seek(start)
                    data = stream.read(min(TAIL_BYTES, before.st_size - start))
                    after = os.fstat(stream.fileno())
                    if file_identity(after) != identity or after.st_size < before.st_size:
                        raise ValueError("The session was replaced during reading.")
                if self._principal(config) != principal or self._config() != config:
                    raise PermissionError("The linked scope changed.")
                if reset:
                    self.events.clear(); self.seen.clear()
                    self.state, self.turn, self.last_at = "waiting", None, None
                    self.latest_update = None
                offset = start
                lines = data.splitlines(keepends=True)
                for index, line in enumerate(lines):
                    if not line.endswith(b"\n"):
                        break
                    offset += len(line)
                    if index == 0 and (clipped or reset and start > len(header)):
                        continue
                    if len(line) > MAX_RECORD or b'"event_msg"' not in line[:200]:
                        continue
                    try:
                        event = public_event(json.loads(line), config["thread_id"], self.clock())
                    except (ValueError, TypeError, UnicodeError):
                        continue
                    if event is None:
                        continue
                    key = hashlib.sha256(line).hexdigest()
                    if key in self.seen or self.last_at is not None and event["at"] < self.last_at:
                        continue
                    if (event["state"] != "working"
                            and self.turn and event["turn_id"] and self.turn != event["turn_id"]):
                        continue
                    self.seen.append(key)
                    self.events.append({"id": key, **event})
                    if event["turn_id"] and event["turn_id"] != self.turn:
                        self.latest_update = None
                    if event["kind"] in ("update", "result"):
                        self.latest_update = {"id": key, **event}
                    self.state, self.last_at = event["state"], event["at"]
                    if event["turn_id"]:
                        self.turn = event["turn_id"]
                self.position, self.identity = offset, identity
                self.source = config
                self.checked_at, self.error = self.clock(), None
            except PermissionError:
                self.reset()
                self.state = "disabled"
            except (ValueError, OSError, KeyError, TypeError):
                self.error = "The linked session could not be checked. Retained updates may be old."

    def view(self, owner, project=None):
        with self.lock:
            try:
                config = self._config()
                self._principal(config, owner)
                if project is not None and project != config["project"]:
                    return None
                if self.binding != json.dumps(config, sort_keys=True):
                    return {"available": False, "state": "waiting", "label": STATE_LABELS["waiting"], "updates": []}
            except PermissionError:
                return {"available": False, "state": "disabled", "label": STATE_LABELS["disabled"], "updates": []}
            except (ValueError, OSError, KeyError, TypeError):
                return {"available": False, "state": "unavailable", "label": STATE_LABELS["unavailable"], "updates": []}
            now = self.clock()
            fresh = bool(self.checked_at and now - self.checked_at <= 20 and not self.error)
            state = self.state if fresh else "unavailable"
            if state == "working" and (self.last_at is None or now - self.last_at > 180):
                state = "quiet"
            updates = list(self.events)
            latest = self.latest_update
            return {"available": fresh, "state": state, "label": STATE_LABELS[state],
                    "project": config["project"], "thread_id": config["thread_id"],
                    "chat_title": config.get("label", "Linked Codex chat"),
                    "observed_at": self.last_at, "checked_at": self.checked_at,
                    "latest_update": latest, "updates": updates,
                    "source_error": self.error, "authority": False,
                    "note": "Observed from the linked local chat. Session activity is not a project completion percentage."}

    def context(self, owner):
        result = self.view(owner)
        return {key: result[key] for key in ("available", "state", "label", "chat_title",
                "observed_at", "checked_at", "latest_update", "source_error", "authority", "note") if key in result}
