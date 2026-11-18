"""Reviewed milestone notifications; task-cycle events never confer admission."""
from __future__ import annotations

import hashlib
import json
import math
import os
from pathlib import Path
import re
import stat
import time

from companion import ConflictError
from project_checklists import sanitize_project_checklist
from routing_targets import dispatch_target
from notification_privacy import redact


POLICY = "milestones_only"
KINDS = {"release", "deployment", "owner_delivery", "major_capability", "project_completion"}
TERMINAL_EVENTS = {"sent", "suppressed", "muted", "uncertain", "cancelled", "failed"}
ROOT = Path(__file__).resolve().parent
VOICE_HOLD_CONFIG_PATH = ROOT / ".state" / "project-milestone-holds.local.json"
ROUTINE_REASON = "Owner policy allows verified milestones only; routine project notice suppressed without replay."
_ROUTINE = re.compile(
    r"\b(?:heartbeat|task_complete|step complete|step completed|tests? passed|test pass|"
    r"intermediate|routine update|profile change|profile update|administrator profile|"
    r"admin(?:istrative)? (?:update|acknowledg(?:e)?ment)|plan(?:ning)? (?:ready|prepared|complete))\b",
    re.I,
)


def _load_voice_hold_ids():
    try:
        value = json.loads(VOICE_HOLD_CONFIG_PATH.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return frozenset()
    if type(value) is not dict or set(value) != {"voice_hold_ids"}:
        raise ValueError("The local project-milestone hold configuration is invalid.")
    identities = value["voice_hold_ids"]
    if (type(identities) is not list or len(identities) > 100
            or any(not isinstance(identity, str) or not re.fullmatch(
                r"task:[A-Za-z0-9_.:-]{1,240}:task_complete", identity
            ) for identity in identities)
            or len(set(identities)) != len(identities)):
        raise ValueError("The local project-milestone hold configuration is invalid.")
    return frozenset(identities)


VOICE_HOLD_IDS = _load_voice_hold_ids()


class ProjectMilestones:
    def __init__(self, store, tasks, *, clock=time.time, evidence_root=None):
        self.store, self.tasks, self.clock = store, tasks, clock
        self.evidence_root = Path(evidence_root or Path.home() / "Desktop").resolve()
        with store.db() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS project_milestones (
                    owner INTEGER NOT NULL, task TEXT NOT NULL, milestone_id TEXT NOT NULL,
                    thread_id TEXT NOT NULL, evidence_hash TEXT NOT NULL, payload_hash TEXT NOT NULL,
                    payload TEXT NOT NULL, event_id TEXT UNIQUE NOT NULL,
                    created_at REAL NOT NULL, expires_at REAL NOT NULL,
                    PRIMARY KEY(owner,task,milestone_id),
                    UNIQUE(owner,task,evidence_hash)
                );
                CREATE TABLE IF NOT EXISTS requested_text_notices (
                    event_id TEXT PRIMARY KEY, owner INTEGER NOT NULL,
                    task TEXT NOT NULL, thread_id TEXT NOT NULL,
                    request_ref TEXT NOT NULL, created_at REAL NOT NULL
                );
            """)

    @staticmethod
    def _owner(db, owner):
        values = {row["key"]: json.loads(row["value"]) for row in db.execute(
            "SELECT key,value FROM settings WHERE key IN ('owner_id','owner_chat_id')")}
        if type(owner) is not int or values.get("owner_id") != owner or values.get("owner_chat_id") != owner:
            raise PermissionError("The currently paired private owner is required.")

    def _evidence(self, values):
        if not isinstance(values, list) or not 1 <= len(values) <= 8:
            raise ValueError("Provide one to eight current verification files.")
        checked = []
        for item in values:
            if not isinstance(item, dict) or set(item) != {"path", "sha256"}:
                raise ValueError("Evidence needs an exact local path and SHA-256.")
            if not isinstance(item["path"], str) or not isinstance(item["sha256"], str) or not re.fullmatch(r"[a-f0-9]{64}", item["sha256"]):
                raise ValueError("Invalid milestone evidence identity.")
            path = Path(item["path"])
            if not path.is_absolute():
                raise ValueError("Milestone evidence must have an absolute path.")
            # Reject outside/UNC/device paths lexically before any filesystem
            # lookup can follow them or contact another machine.
            path = Path(os.path.abspath(path))
            if not path.is_relative_to(self.evidence_root) or path.suffix.lower() not in {".json", ".md", ".txt"}:
                raise ValueError("Use a non-secret project verification record inside the evidence workspace.")
            if any(part.lower() in {".aws", ".ssh", ".git", "node_modules", "credentials", "secrets"} for part in path.parts):
                raise ValueError("Credential and tool-store paths cannot be milestone evidence.")
            current = self.evidence_root
            for part in path.relative_to(self.evidence_root).parts:
                current = current / part
                info = current.lstat()
                if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400):
                    raise ValueError("Milestone evidence cannot traverse a symlink, junction or reparse point.")
            if not path.is_file() or path.stat().st_size > 2 * 1024 * 1024:
                raise ValueError("Use a verification record of at most 2 MB.")
            with path.open("rb") as stream:
                content = stream.read(2 * 1024 * 1024 + 1)
            if not content or len(content) > 2 * 1024 * 1024:
                raise ValueError("Use a nonempty verification record of at most 2 MB.")
            actual = hashlib.sha256(content).hexdigest()
            if actual != item["sha256"]:
                raise ConflictError("Milestone evidence changed. Review the current result before announcing it.")
            checked.append({"path": str(path), "sha256": actual})
        return checked

    @staticmethod
    def _text(value, maximum, field):
        if not isinstance(value, str) or not value.strip() or len(value.encode("utf-16-le")) // 2 > maximum:
            raise ValueError(f"Use a concise {field} of at most {maximum} characters.")
        cleaned = sanitize_project_checklist({"done": [value]})["done"]
        if not cleaned:
            raise ValueError(f"The milestone {field} is empty or contains private material.")
        return cleaned[0]

    def _source_events(self, db, identities):
        if not isinstance(identities, list) or len(identities) > 20:
            raise ValueError("Provide the related notice identities, or an empty list.")
        for identity in identities:
            if not isinstance(identity, str) or not 1 <= len(identity) <= 250:
                raise ValueError("Invalid source notice identity.")
            if identity.startswith("task:"):
                raise ValueError("A task cycle cannot serve as milestone evidence or be repackaged.")
            row = db.execute("SELECT state FROM outbox WHERE event_id=?", (identity,)).fetchone()
            if row:
                raise ConflictError("An existing notice, queued or terminal, cannot be recycled as a milestone.")

    def _requested_source(self, db, owner, reference):
        window_row = db.execute("SELECT value FROM settings WHERE key=?",
                                ("requested_window:" + reference,)).fetchone()
        if window_row:
            window = json.loads(window_row["value"])
            start, end = window.get("start_at"), window.get("end_at")
            if (window.get("enabled") is not True or window.get("owner") != owner
                    or type(start) not in (int, float) or type(end) not in (int, float)
                    or not math.isfinite(start) or not math.isfinite(end)
                    or not start <= self.clock() < end or not 0 < end - start <= 7200):
                raise ConflictError("This requested notification window is closed.")
        if not reference.startswith("inbox:"):
            return  # Codex references require the coordinator's explicit owner-source review.
        row = db.execute("SELECT state,receipt,received_at FROM inbox WHERE owner_id=? AND update_id=?",
                         (owner, int(reference.split(":", 1)[1]))).fetchone()
        if not row or self.clock() - row["received_at"] > 86400 or row["state"] in {
                "cancelled", "interrupted", "expired", "awaiting_clarification"
        } or re.search(r"\b(?:denied|rejected|uncertain|policy|expired)\b", row["receipt"] or "", re.I):
            raise ConflictError("This owner request is stale, held or unavailable.")

    @staticmethod
    def _has_voice_hold(db, *, terminal_only=False):
        for identity in VOICE_HOLD_IDS:
            held = db.execute("SELECT state FROM outbox WHERE event_id=?", (identity,)).fetchone()
            if held and (not terminal_only or held["state"] in TERMINAL_EVENTS):
                return True
        return False

    def _voice_hold(self, db, alias, payload):
        held = self._has_voice_hold(db, terminal_only=True)
        context = " ".join((
            payload["milestone_id"], payload["title"], payload["outcome"], payload["review"]["note"],
            *(item["path"] for item in payload["evidence"]),
        )).replace("_", " ").replace("-", " ")
        vege_voice = re.search(r"\b(?:voice|speech|microphone|audio)\b", context, re.I) and (
            alias == "vega" or re.search(r"\bvega\b", context, re.I))
        readiness = re.search(r"\bvoice\b.{0,40}\b(?:ready|readiness)\b", context, re.I)
        if held and (vege_voice or readiness):
            raise ConflictError("The blocked voice-readiness contact cannot be repackaged as a milestone.")

    def _notice_text(self, info, payload):
        label = self._text(info["label"], 100, "project label")
        return (f"✓ {label}\n{payload['title']}\n{payload['outcome']}" if info.get("summary", False)
                else f"✓ {label}\nA verified project milestone was reached. Details are available in Projects.")

    @staticmethod
    def _public(db, row, *, duplicate=False):
        delivery = db.execute("SELECT state,message_id,error FROM outbox WHERE event_id=?", (row["event_id"],)).fetchone()
        return {
            "milestone_id": row["milestone_id"], "task": row["task"], "event_id": row["event_id"],
            "state": delivery["state"] if delivery else "unavailable",
            "message_id": delivery["message_id"] if delivery else None,
            "error": delivery["error"] if delivery else None, "duplicate": duplicate,
        }

    def announce(self, record):
        required = {"milestone_id", "task", "thread_id", "kind", "title", "outcome",
                    "achieved_at", "review", "evidence", "source_event_ids"}
        if not isinstance(record, dict) or set(record) != required:
            raise ValueError("Use the reviewed milestone schema; generic completion/heartbeat records are not accepted.")
        identity = record["milestone_id"]
        if not isinstance(identity, str) or not re.fullmatch(r"[a-z0-9][a-z0-9._-]{2,119}", identity) or re.fullmatch(r"[a-f0-9-]{36}", identity) or re.search(
                r"(?:^|[_.-])(?:turn|cycle|step|heartbeat|test|profile|admin|progress)(?:[_.-]|$)", identity):
            raise ValueError("Use a stable project milestone name, not a turn ID.")
        tasks = self.tasks()
        alias = record["task"]
        if not isinstance(alias, str) or alias not in tasks or not tasks[alias].get("notify", True):
            raise ValueError("Choose a currently registered notifying project.")
        if record["thread_id"] != dispatch_target(tasks, alias)["thread_id"]:
            raise ConflictError("The milestone's registered project owner changed.")
        if not isinstance(record["kind"], str) or record["kind"] not in KINDS:
            raise ValueError("Progress, tests, plans and administrative acknowledgments are not project milestones.")
        review = record["review"]
        if not isinstance(review, dict) or set(review) != {"by", "verified", "is_milestone", "note"} or \
                review["by"] != "coordinator" or review["verified"] is not True or review["is_milestone"] is not True:
            raise ValueError("A coordinator must review the actual outcome and confirm that it is a project milestone.")
        self._text(review["note"], 240, "review note")
        title = self._text(record["title"], 100, "title")
        outcome = self._text(record["outcome"], 240, "outcome")
        if _ROUTINE.search(" ".join((identity.replace("-", " ").replace("_", " "), title, outcome))):
            raise ValueError("Routine steps, tests, plans and profile acknowledgments are not milestone pushes.")
        now, achieved = self.clock(), record["achieved_at"]
        if type(achieved) not in (int, float) or not math.isfinite(achieved) or not now - 86400 <= achieved <= now + 30:
            raise ValueError("Only a current, newly achieved milestone can be announced.")
        evidence = self._evidence(record["evidence"])
        payload = {**record, "title": title, "outcome": outcome, "evidence": evidence}
        encoded = json.dumps(payload, sort_keys=True, ensure_ascii=False)
        payload_hash = hashlib.sha256(encoded.encode()).hexdigest()
        evidence_hash = hashlib.sha256(json.dumps(sorted(set(item["sha256"] for item in evidence))).encode()).hexdigest()
        owner = self.store.get("owner_id")
        with self.store.db() as db:
            db.execute("BEGIN IMMEDIATE")
            self._owner(db, owner)
            values = {row["key"]: json.loads(row["value"]) for row in db.execute(
                "SELECT key,value FROM settings WHERE key IN ('process_id','milestone_delivery_host_pid')")}
            if values.get("process_id") is None or values.get("milestone_delivery_host_pid") != values["process_id"]:
                raise ConflictError("Milestone transport is not live yet. No announcement was queued.")
            policy = db.execute("SELECT value FROM settings WHERE key='milestone_policy_started_at'").fetchone()
            policy_started = json.loads(policy[0]) if policy else now
            if achieved < policy_started:
                raise ConflictError("Historical outcomes are not replayed when the notification policy changes.")
            previous = db.execute("SELECT * FROM project_milestones WHERE owner=? AND task=? AND milestone_id=?",
                                  (owner, alias, identity)).fetchone()
            if previous:
                if previous["payload_hash"] != payload_hash:
                    raise ConflictError("This milestone identity already has a frozen receipt with different content.")
                return self._public(db, previous, duplicate=True)
            previous = db.execute("SELECT * FROM project_milestones WHERE owner=? AND task=? AND evidence_hash=?",
                                  (owner, alias, evidence_hash)).fetchone()
            if previous:
                return self._public(db, previous, duplicate=True)
            self._source_events(db, record["source_event_ids"])
            self._voice_hold(db, alias, payload)
            for item in evidence:
                earlier = db.execute(
                    "SELECT o.state FROM outbound_documents d JOIN outbox o ON o.event_id=d.event_id WHERE d.sha256=?",
                    (item["sha256"],),
                ).fetchone()
                if earlier and earlier["state"] in TERMINAL_EVENTS:
                    raise ConflictError("A delivered or uncertain document cannot generate another delivery notice.")
            event_id = "milestone:" + str(owner) + ":" + alias + ":" + identity
            if db.execute("SELECT 1 FROM outbox WHERE event_id=?", (event_id,)).fetchone():
                raise ConflictError("An existing notice receipt already owns this milestone identity.")
            text = self._notice_text(tasks[alias], payload)
            db.execute("INSERT INTO project_milestones VALUES(?,?,?,?,?,?,?,?,?,?)",
                       (owner, alias, identity, record["thread_id"], evidence_hash, payload_hash,
                        encoded, event_id, now, now + 86400))
            db.execute("INSERT INTO outbox(event_id,task,text,created_at,state) VALUES(?,?,?,?,'queued')",
                       (event_id, alias, text, now))
            return self._public(db, db.execute("SELECT * FROM project_milestones WHERE event_id=?", (event_id,)).fetchone())

    def delivery_reason(self, db, row):
        """Return a suppression reason, or None for an admitted current notice."""
        identity = row["event_id"]
        if identity.startswith(("task:", "handled:")):
            return ROUTINE_REASON
        if identity.startswith("milestone:"):
            milestone = db.execute("SELECT * FROM project_milestones WHERE event_id=?", (identity,)).fetchone()
            if not milestone:
                return "No reviewed milestone admission exists for this notice."
            try:
                self._owner(db, milestone["owner"])
                if self.clock() >= milestone["expires_at"]:
                    return "The milestone notice expired; it was not replayed."
                tasks = self.tasks()
                if milestone["task"] not in tasks or not tasks[milestone["task"]].get("notify", True) or \
                        dispatch_target(tasks, milestone["task"])["thread_id"] != milestone["thread_id"]:
                    return "The milestone's owner or target changed before delivery."
                payload = json.loads(milestone["payload"])
                if hashlib.sha256(milestone["payload"].encode()).hexdigest() != milestone["payload_hash"] or \
                        row["task"] != milestone["task"] or row["text"] != self._notice_text(tasks[milestone["task"]], payload):
                    return "The reviewed milestone text, privacy or identity changed before delivery."
                self._evidence(payload["evidence"])
                self._source_events(db, payload["source_event_ids"])
                self._voice_hold(db, milestone["task"], payload)
            except (PermissionError, ValueError, OSError):
                return "The milestone owner or verification evidence changed before delivery."
            if self.store.get("muted", False):
                return "Milestone notifications are muted by the owner; this event will not replay."
        elif identity.startswith("manual:") and not db.execute(
                "SELECT 1 FROM outbound_documents WHERE event_id=?", (identity,)).fetchone():
            requested = db.execute("SELECT * FROM requested_text_notices WHERE event_id=?", (identity,)).fetchone()
            if not requested:
                return "Unreviewed manual project push held by milestone-only policy."
            try:
                self._owner(db, requested["owner"])
                self._requested_source(db, requested["owner"], requested["request_ref"])
                if dispatch_target(self.tasks(), requested["task"])["thread_id"] != requested["thread_id"]:
                    return "The requested delivery target changed."
            except (PermissionError, ValueError, KeyError):
                return "The requested delivery owner or target changed."
        elif not identity.startswith((
                "ack:", "pair:", "conversation:", "media-received:",
                "voice-received:", "voice-failed:", "voice-output-failed:",
                "reminder:", "project-recovery-receipt:")) and not db.execute(
                    "SELECT 1 FROM outbound_documents WHERE event_id=?", (identity,)).fetchone():
            return "Unrecognized proactive notice source; only reviewed milestones or requested deliveries may push."
        return None

    def requested_text(self, alias, key, text, request_ref):
        """Internal CLI only. The caller must have a current explicit owner request."""
        if not isinstance(request_ref, str) or not re.fullmatch(
                r"(?:inbox:-?\d+|codex:[a-f0-9-]{36}:[a-f0-9-]{36})", request_ref):
            raise ValueError("A current explicit owner request reference is required for manual text delivery.")
        if not isinstance(key, str) or not re.fullmatch(r"[a-zA-Z0-9_.:-]{1,160}", key):
            raise ValueError("Use a stable requested-delivery identity.")
        tasks = self.tasks()
        destination = dispatch_target(tasks, alias)
        owner, event_id = self.store.get("owner_id"), "manual:" + key
        if not isinstance(text, str) or not text.strip() or len(text) > 3500:
            raise ValueError("Use a concise explicitly requested delivery.")
        text = redact(text)
        with self.store.db() as db:
            db.execute("BEGIN IMMEDIATE"); self._owner(db, owner)
            previous = db.execute("SELECT state FROM outbox WHERE event_id=?", (event_id,)).fetchone()
            if previous:
                return {"queued": False, "event_id": event_id, "state": previous["state"]}
            self._requested_source(db, owner, request_ref)
            held = self._has_voice_hold(db)
            if held and (re.search(r"\bvoice\b.{0,40}\b(?:ready|readiness)\b", text, re.I) or
                    ((alias == "vega" or re.search(r"\bvega\b", text, re.I)) and re.search(r"\b(?:voice|speech|microphone|audio)\b", text, re.I))):
                raise ConflictError("The held voice-readiness contact is not available for manual replay.")
            db.execute("INSERT INTO requested_text_notices VALUES(?,?,?,?,?,?)",
                       (event_id, owner, alias, destination["thread_id"], request_ref, self.clock()))
            db.execute("INSERT INTO outbox(event_id,task,text,created_at,state) VALUES(?,?,?,?,'queued')",
                       (event_id, alias, text, self.clock()))
            return {"queued": True, "event_id": event_id, "state": "queued"}
