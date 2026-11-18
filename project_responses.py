"""Owner-requested report content and delivery, separate from routing receipts."""
from __future__ import annotations

import hashlib
import json
import re
import time
import uuid

from companion import ConflictError
from notification_privacy import redact
from routing_targets import dispatch_target
import text_delivery


def install(db):
    db.executescript("""
        CREATE TABLE IF NOT EXISTS project_report_responses (
            id TEXT PRIMARY KEY, owner INTEGER NOT NULL, origin_update_id INTEGER NOT NULL,
            request_id TEXT, report_id TEXT NOT NULL, task TEXT NOT NULL, thread_id TEXT NOT NULL,
            event_id TEXT UNIQUE NOT NULL, body_hash TEXT NOT NULL, source TEXT NOT NULL,
            preference_revision INTEGER NOT NULL, created_at REAL NOT NULL,
            UNIQUE(owner,origin_update_id,report_id)
        );
        CREATE TABLE IF NOT EXISTS project_report_confirmations (
            response_id TEXT PRIMARY KEY, owner INTEGER NOT NULL,
            confirmed_at REAL NOT NULL, source_ref TEXT NOT NULL
        );
    """)


class ProjectResponses:
    def __init__(self, store, tasks, *, clock=time.time):
        self.store, self.tasks, self.clock = store, tasks, clock

    @staticmethod
    def _principal(db, owner):
        values = {row["key"]: json.loads(row["value"]) for row in db.execute(
            "SELECT key,value FROM settings WHERE key IN ('owner_id','owner_chat_id')")}
        if type(owner) is not int or values.get("owner_id") != owner or values.get("owner_chat_id") != owner:
            raise PermissionError("Only the paired private owner may retrieve this report.")
        preference = db.execute("SELECT revision,payload FROM companion_preferences WHERE owner=?", (owner,)).fetchone()
        if preference and not json.loads(preference["payload"]).get("project_context_enabled", True):
            raise PermissionError("Project context is disabled in your preferences.")
        return preference["revision"] if preference else 0

    @staticmethod
    def _origin(db, owner, update_id):
        row = db.execute("SELECT * FROM inbox WHERE owner_id=? AND update_id=?", (owner, update_id)).fetchone()
        if (not row or row["message_id"] <= 0
                or row["state"] in {"cancelled", "interrupted", "expired", "awaiting_clarification"}):
            raise ConflictError("The original Telegram report request is held or unavailable.")
        context = db.execute("SELECT payload FROM inbox_context WHERE update_id=?", (update_id,)).fetchone()
        origin = json.loads(context["payload"]).get("origin") if context else None
        if origin not in {"telegram_text", "manual_review", "speaker_matched"}:
            raise PermissionError("A confirmed owner request is required for report delivery.")
        # Voice matching never authorizes a historical source with revoked
        # provenance. The host performs the current profile check before queue.
        return row

    def queue(self, owner, update_id, report, *, expected_preference_revision):
        """Internal only: the host has resolved the request through ProjectReports."""
        body = report.get("text")
        report_id = report.get("report_id")
        task = report.get("task")
        if not isinstance(body, str) or not body.strip() or len(body.encode("utf-8")) > text_delivery.MAX_TEXT_BYTES:
            raise ValueError("The requested report has no bounded readable content.")
        if not isinstance(report_id, str) or not re.fullmatch(r"[A-Za-z0-9_.:-]{1,160}", report_id):
            raise ValueError("The report needs a registered identity.")
        if task not in self.tasks():
            raise ValueError("The report's registered project is unavailable.")
        body = redact(body)
        body_hash = hashlib.sha256(body.encode("utf-8")).hexdigest()
        source = json.dumps({key: report.get(key) for key in (
            "report_id", "kind", "source", "sha256", "assessed_at", "retrieved_at", "truncated",
        )}, ensure_ascii=False)
        with self.store.db() as db:
            db.execute("BEGIN IMMEDIATE")
            revision = self._principal(db, owner)
            if revision != expected_preference_revision:
                raise ConflictError("Your preferences changed before report delivery. Nothing was queued.")
            self._origin(db, owner, update_id)
            prior = db.execute(
                "SELECT * FROM project_report_responses WHERE owner=? AND origin_update_id=? ORDER BY created_at LIMIT 1",
                (owner, update_id),
            ).fetchone()
            if prior:
                # An update to the source file is not a new request to resend.
                return self._public(db, prior, duplicate=True)
            linked = db.execute(
                "SELECT request_id FROM project_request_origins WHERE owner=? AND update_id=?", (owner, update_id),
            ).fetchone()
            identity = str(uuid.uuid4())
            event_id = f"report-response:{identity}"
            target = dispatch_target(self.tasks(), task)
            db.execute("INSERT INTO project_report_responses VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                       (identity, owner, update_id, linked["request_id"] if linked else None, report_id,
                        task, target["thread_id"], event_id, body_hash, source, revision, self.clock()))
            db.execute("INSERT INTO outbox(event_id,task,text,created_at,state) VALUES(?,?,?,?,'queued')",
                       (event_id, task, body, self.clock()))
            return self._public(db, db.execute("SELECT * FROM project_report_responses WHERE id=?", (identity,)).fetchone())

    @staticmethod
    def _public(db, row, *, duplicate=False):
        result = {
            "id": row["id"], "origin_update_id": str(row["origin_update_id"]),
            "request_id": row["request_id"], "report_id": row["report_id"], "event_id": row["event_id"],
            "source": json.loads(row["source"]), "created_at": row["created_at"], "duplicate": duplicate,
            **text_delivery.receipt(db, row["event_id"]),
        }
        confirmed = db.execute("SELECT confirmed_at,source_ref FROM project_report_confirmations "
                               "WHERE response_id=? AND owner=?", (row["id"], row["owner"])).fetchone()
        if confirmed:
            result.update(owner_read_confirmed=True, owner_confirmation=dict(confirmed))
        return result

    def confirm_read(self, owner, identity, source_ref):
        """Internal owner-feedback recording; never an action-approval endpoint."""
        if not isinstance(source_ref, str) or not source_ref.startswith("codex-owner-feedback:") or len(source_ref) > 220:
            raise ValueError("An explicit owner feedback source is required.")
        with self.store.db() as db:
            self._principal(db, owner)
            row = db.execute("SELECT * FROM project_report_responses WHERE id=? AND owner=?", (identity, owner)).fetchone()
            if not row or not text_delivery.receipt(db, row["event_id"])["delivered"]:
                raise ValueError("The delivered report response is unavailable.")
            db.execute("INSERT OR IGNORE INTO project_report_confirmations VALUES(?,?,?,?)",
                       (identity, owner, self.clock(), source_ref))
            return self._public(db, row)

    def list(self, owner, *, request_id=None, update_id=None, limit=12):
        with self.store.db() as db:
            self._principal(db, owner)
            clauses, values = ["owner=?"], [owner]
            if request_id is not None:
                clauses.append("request_id=?"); values.append(request_id)
            if update_id is not None:
                clauses.append("origin_update_id=?"); values.append(update_id)
            rows = db.execute(
                "SELECT * FROM project_report_responses WHERE " + " AND ".join(clauses) +
                " ORDER BY created_at DESC LIMIT ?", (*values, min(30, max(1, limit))),
            ).fetchall()
            return [self._public(db, row) for row in rows]

    def delivery_reason(self, db, row):
        saved = db.execute("SELECT * FROM project_report_responses WHERE event_id=?", (row["event_id"],)).fetchone()
        if not saved:
            return "No explicit owner report response is registered for this message."
        try:
            revision = self._principal(db, saved["owner"])
            self._origin(db, saved["owner"], saved["origin_update_id"])
            if revision != saved["preference_revision"]:
                return "Owner preferences changed; pending report content is retained for review."
            if dispatch_target(self.tasks(), saved["task"])["thread_id"] != saved["thread_id"]:
                return "The registered project changed; pending report content is retained."
            if row["task"] != saved["task"] or hashlib.sha256(row["text"].encode("utf-8")).hexdigest() != saved["body_hash"]:
                return "The saved report content changed before delivery."
            if saved["request_id"]:
                routes = db.execute(
                    "SELECT i.state FROM project_request_routes r JOIN inbox i ON i.update_id=r.update_id WHERE r.request_id=?",
                    (saved["request_id"],),
                ).fetchall()
                if any(route["state"] in {"cancelled", "awaiting_clarification", "expired"} for route in routes):
                    return "The source project request is cancelled or held; response content is retained."
        except (PermissionError, ValueError, KeyError):
            return "The original owner, project or request is no longer eligible for this response."
        return None

    def retry_failed(self, owner, identity):
        """Explicit internal retry of a definite rejection, never an uncertain send."""
        with self.store.db() as db:
            db.execute("BEGIN IMMEDIATE")
            self._principal(db, owner)
            saved = db.execute("SELECT * FROM project_report_responses WHERE id=? AND owner=?", (identity, owner)).fetchone()
            if not saved:
                raise ValueError("This report response is unavailable.")
            row = db.execute("SELECT * FROM outbox WHERE event_id=?", (saved["event_id"],)).fetchone()
            if row["state"] != "failed":
                raise ConflictError("Only a definitely failed report may be retried. Uncertain or delivered parts are never replayed.")
            reason = self.delivery_reason(db, row)
            if reason:
                raise ConflictError(reason)
            if db.execute("SELECT 1 FROM outbound_text_parts WHERE event_id=? AND state IN ('sending','uncertain')",
                          (saved["event_id"],)).fetchone():
                raise ConflictError("A part has no confirmed outcome. Reconcile it before any retry.")
            db.execute("UPDATE outbound_text_parts SET state='queued',error=NULL WHERE event_id=? AND state='failed'",
                       (saved["event_id"],))
            db.execute("UPDATE outbox SET state='queued',error=NULL WHERE event_id=?", (saved["event_id"],))
            return self._public(db, saved)
