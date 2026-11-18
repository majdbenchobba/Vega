"""Owner-bound project-prompt requests, required input and routing receipts."""
from __future__ import annotations

import json
import re
import time
import uuid

from companion import ConflictError
from routing_targets import dispatch_target, require_dispatchable


class ProjectRequests:
    def __init__(self, store, tasks, *, clock=time.time):
        self.store, self.tasks, self.clock = store, tasks, clock
        with store.db() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS project_requests (
                    id TEXT PRIMARY KEY, owner INTEGER NOT NULL, client_key TEXT NOT NULL,
                    source TEXT NOT NULL, state TEXT NOT NULL, revision INTEGER NOT NULL,
                    text TEXT NOT NULL, targets TEXT NOT NULL, preference_revision INTEGER NOT NULL,
                    created_at REAL NOT NULL, updated_at REAL NOT NULL,
                    UNIQUE(owner,client_key)
                );
                CREATE TABLE IF NOT EXISTS project_request_routes (
                    request_id TEXT NOT NULL, update_id INTEGER UNIQUE NOT NULL,
                    task TEXT NOT NULL, thread_id TEXT NOT NULL, label TEXT NOT NULL,
                    PRIMARY KEY(request_id,task)
                );
                CREATE TABLE IF NOT EXISTS project_request_origins (
                    owner INTEGER NOT NULL, update_id INTEGER NOT NULL, request_id TEXT NOT NULL,
                    prior_state TEXT NOT NULL, prior_receipt TEXT,
                    PRIMARY KEY(owner,update_id)
                );
                CREATE TABLE IF NOT EXISTS project_request_followups (
                    child_id TEXT PRIMARY KEY, parent_id TEXT NOT NULL,
                    owner INTEGER NOT NULL, origin_update_id INTEGER NOT NULL,
                    UNIQUE(owner,origin_update_id)
                );
                CREATE TABLE IF NOT EXISTS project_request_steers (
                    child_id TEXT PRIMARY KEY, parent_id TEXT NOT NULL,
                    owner INTEGER NOT NULL, parent_revision INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS inbox_context (
                    update_id INTEGER PRIMARY KEY, payload TEXT NOT NULL
                );
            """)

    @staticmethod
    def _body(value):
        if not isinstance(value, str) or len(value) > 12000:
            raise ValueError("A project prompt must contain at most 12,000 characters.")
        try:
            value.encode("utf-8")
        except UnicodeError:
            raise ValueError("Use valid text for the project prompt.") from None
        return value

    def _targets(self, values):
        if not isinstance(values, list) or len(values) > 32:
            raise ValueError("Choose up to 32 registered project targets.")
        tasks, result, seen = self.tasks(), [], set()
        for item in values:
            if not isinstance(item, dict) or set(item) - {"alias", "thread_id", "label"}:
                raise ValueError("Choose a registered project from the current list.")
            alias = item.get("alias")
            if not isinstance(alias, str) or alias not in tasks:
                raise ValueError("Choose an existing registered project.")
            expected = item.get("thread_id")
            destination = dispatch_target(tasks, alias)
            if not isinstance(expected, str) or expected != destination["thread_id"]:
                raise ConflictError("This project's destination changed. Refresh the project list before sending.")
            require_dispatchable(tasks, alias)
            if alias not in seen:
                result.append({"alias": alias, "thread_id": expected, "label": tasks[alias]["label"]})
                seen.add(alias)
        return result

    @staticmethod
    def _principal(db, owner):
        rows = {row["key"]: json.loads(row["value"]) for row in db.execute(
            "SELECT key,value FROM settings WHERE key IN ('owner_id','owner_chat_id')")}
        if type(owner) is not int or rows.get("owner_id") != owner or rows.get("owner_chat_id") != owner:
            raise PermissionError("The paired owner is required.")
        preference = db.execute("SELECT revision FROM companion_preferences WHERE owner=?", (owner,)).fetchone()
        return preference["revision"] if preference else 0

    def _row(self, db, identity, owner):
        if not isinstance(identity, str) or len(identity) > 100:
            raise ValueError("Choose an existing project request.")
        row = db.execute("SELECT * FROM project_requests WHERE id=? AND owner=?", (identity, owner)).fetchone()
        if not row:
            raise ValueError("This project request is unavailable.")
        return row

    def _public(self, db, row):
        routes = [dict(item) for item in db.execute(
            "SELECT r.update_id,r.task,r.thread_id,r.label,i.state,i.receipt,i.received_at,i.handled_at,"
            "c.first_claimed_at AS claimed_at,c.last_claimed_at AS last_attempt_at "
            "FROM project_request_routes r JOIN inbox i ON i.update_id=r.update_id "
            "LEFT JOIN inbox_claims c ON c.update_id=r.update_id "
            "WHERE r.request_id=? ORDER BY i.received_at,r.task", (row["id"],))]
        tasks = self.tasks()
        targets_current = True
        for route in routes:
            route["update_id"] = str(route["update_id"])
            route["age_seconds"] = max(0, int(self.clock() - route["received_at"]))
            route["receipt_recorded_at"] = route["handled_at"] if route["state"] == "handled" else None
            route["destination_acceptance"] = "unverified"
            route["blocker"] = {
                "queued": "Waiting for coordinator; no acceptance receipt.",
                "routing": "Routing started; no final receipt yet.",
                "awaiting_clarification": "The destination needs clarification.",
                "interrupted": "Routing was interrupted; it was not replayed.",
            }.get(route["state"])
            try:
                current = require_dispatchable(tasks, route["task"])
                same_target = current["thread_id"] == route["thread_id"]
            except (ValueError, KeyError):
                same_target = False
            targets_current = targets_current and same_target
            route["connection_changed"] = route["state"] == "queued" and not same_target
            if route["connection_changed"]:
                route["blocker"] = "Saved for an older chat. Nothing was sent to the new chat."
        states = {route["state"] for route in routes}
        state = row["state"]
        if routes:
            if states <= {"cancelled"}:
                state = "cancelled"
            elif any(route["connection_changed"] for route in routes):
                state = "stale_target"
            elif "cancelled" in states:
                state = "partially_cancelled"
            elif "awaiting_clarification" in states:
                state = "stale_target"
            elif states & {"interrupted", "failed", "uncertain", "expired"}:
                state = "needs_attention"
            elif states <= {"handled"}:
                state = "handled"
            elif "routing" in states or "handled" in states:
                state = "routing"
            else:
                state = "queued"
        question = None
        if state == "awaiting_input":
            fields = []
            if not json.loads(row["targets"]): fields.append({"name": "targets", "label": "Which project should receive the prompt?", "required": True})
            if not row["text"].strip(): fields.append({"name": "text", "label": "What should the project work on?", "required": True})
            question = {"fields": fields}
        from project_responses import ProjectResponses
        responses = [ProjectResponses._public(db, item) for item in db.execute(
            "SELECT * FROM project_report_responses WHERE owner=? AND request_id=? ORDER BY created_at",
            (row["owner"], row["id"]),
        )]
        parent = db.execute("SELECT parent_id FROM project_request_followups WHERE child_id=? AND owner=?",
                            (row["id"], row["owner"])).fetchone()
        steer = db.execute("SELECT parent_id FROM project_request_steers WHERE child_id=? AND owner=?",
                           (row["id"], row["owner"])).fetchone()
        return {
            "id": row["id"], "state": state, "revision": row["revision"], "text": row["text"],
            "request_key": row["client_key"],
            "targets": json.loads(row["targets"]), "source": row["source"],
            "created_at": row["created_at"], "updated_at": row["updated_at"],
            "question": question, "routes": routes, "responses": responses,
            "parent_request_id": steer["parent_id"] if steer else parent["parent_id"] if parent else None,
            "follow_up_kind": "steer" if steer else "follow_up" if parent else None,
            "can_steer": bool(routes) and targets_current and state in ("queued", "routing", "handled", "needs_attention"),
            "can_cancel": state == "awaiting_input" or any(item["state"] == "queued" for item in routes),
            "authority_note": "Routing a prompt does not approve spending, publication, permissions or execution outside the target's existing scope.",
        }

    def _queue(self, db, row, targets):
        # Resolve the registry again at admission. Serialized target objects do
        # not retain authority when their registered destination changes.
        targets = self._targets(targets)
        steer = db.execute("SELECT parent_id FROM project_request_steers WHERE child_id=?",
                           (row["id"],)).fetchone()
        instruction = (f"Follow-up to request {steer['parent_id']}.\nCurrent owner instruction:\n{row['text']}"
                       if steer else row["text"])
        created = self.clock()
        for target in targets:
            identity = -time.time_ns()
            while db.execute("SELECT 1 FROM inbox WHERE update_id=?", (identity,)).fetchone():
                identity -= 1
            db.execute(
                "INSERT INTO inbox(update_id,message_id,owner_id,task,text,received_at,state,receipt) "
                "VALUES(?,0,?,?,?,?, 'queued',?)",
                (identity, row["owner"], target["alias"], instruction, created,
                 "Queued for the coordinator. No project implementation is claimed."),
            )
            db.execute("INSERT INTO project_request_routes VALUES(?,?,?,?,?)",
                       (row["id"], identity, target["alias"], target["thread_id"], target["label"]))
            db.execute("INSERT INTO inbox_context VALUES(?,?)", (identity, json.dumps({
                "routing_target": {"task": target["alias"], "thread_id": target["thread_id"]},
                "project_request_id": row["id"], "owner_source": row["source"],
                **({"parent_request_id": steer["parent_id"], "request_action": "steer"} if steer else {}),
            })))
        db.execute("UPDATE project_requests SET state='queued',updated_at=? WHERE id=?", (created, row["id"]))

    def create(self, owner, source, client_key, text="", targets=None, *, expected_preference_revision=None, origin_ids=None, parent_id=None):
        if source not in ("local_owner_ui", "telegram_miniapp", "owner_conversation", "codex_owner_request"):
            raise ValueError("This request source is not supported.")
        if not isinstance(client_key, str) or not re.fullmatch(r"[A-Za-z0-9_.:-]{1,160}", client_key):
            raise ValueError("A stable request identity is required.")
        text = self._body(text)
        supplied = targets if targets is not None else []
        if not isinstance(supplied, list) or len(supplied) > 32 or any(not isinstance(item, dict) for item in supplied):
            raise ValueError("Choose up to 32 registered project targets.")
        # Repeat identities return their stored outcome, including after a
        # destination changes, without creating another queued instruction.
        with self.store.db() as db:
            db.execute("BEGIN IMMEDIATE")
            preference_revision = self._principal(db, owner)
            if expected_preference_revision is not None and preference_revision != expected_preference_revision:
                raise ConflictError("Your preferences changed before routing. No instruction was queued.")
            existing = db.execute("SELECT * FROM project_requests WHERE owner=? AND client_key=?", (owner, client_key)).fetchone()
            if existing:
                prior_targets = json.loads(existing["targets"])
                prior_identity = sorted((item["alias"], item["thread_id"]) for item in prior_targets)
                new_identity = sorted((item.get("alias"), item.get("thread_id")) for item in supplied if isinstance(item, dict))
                if (existing["source"], existing["text"], prior_identity) != (source, text, new_identity):
                    raise ConflictError("This request identity already belongs to different content.")
                return self._public(db, existing)
            origins = []
            for update_id in origin_ids or []:
                if type(update_id) is not int or not -(2**63) < update_id < 2**63:
                    raise ValueError("The source request identity is invalid.")
                prior = db.execute("SELECT state,receipt FROM inbox WHERE owner_id=? AND update_id=?", (owner, update_id)).fetchone()
                if not prior or prior["state"] not in ("conversation_done", "conversation_failed", "responding", "acting") or re.search(
                    r"\b(?:expired|denied|rejected|uncertain|policy)\b", prior["receipt"] or "", re.I
                ):
                    raise ConflictError("The source request is already routed, held, expired or unavailable.")
                if db.execute("SELECT 1 FROM project_request_origins WHERE owner=? AND update_id=?", (owner, update_id)).fetchone():
                    raise ConflictError("A source request already has a routing receipt. Reconcile it before proceeding.")
                origins.append((update_id, prior["state"], prior["receipt"]))
            resolved = self._targets(supplied)
            if parent_id is not None:
                parent = self._row(db, parent_id, owner)
                if (source != "owner_conversation" or len(origins) != 1
                        or sorted((x["alias"], x["thread_id"]) for x in json.loads(parent["targets"]))
                        != sorted((x["alias"], x["thread_id"]) for x in resolved)):
                    raise ConflictError("The follow-up must keep its original destination.")
            identity = str(uuid.uuid4())
            now = self.clock()
            db.execute("INSERT INTO project_requests VALUES(?,?,?,?,?,1,?,?,?,?,?)",
                       (identity, owner, client_key, source, "awaiting_input", text,
                        json.dumps(resolved), preference_revision, now, now))
            if parent_id is not None:
                db.execute("INSERT INTO project_request_followups VALUES(?,?,?,?)",
                           (identity, parent_id, owner, origins[0][0]))
            row = self._row(db, identity, owner)
            if text.strip() and resolved:
                self._queue(db, row, resolved)
            for update_id, prior_state, prior_receipt in origins:
                db.execute("INSERT INTO project_request_origins VALUES(?,?,?,?,?)",
                           (owner, update_id, identity, prior_state, prior_receipt))
            return self._public(db, self._row(db, identity, owner))

    def follow_up(self, owner, identity, origin):
        """A new owner instruction linked to the immutable original, never a replay."""
        with self.store.db() as db:
            revision = self._principal(db, owner)
            parent = dict(self._row(db, identity, owner))
            row = db.execute("SELECT * FROM inbox WHERE update_id=? AND owner_id=?",
                             (origin["update_id"], owner)).fetchone()
            context = db.execute("SELECT payload FROM inbox_context WHERE update_id=?",
                                 (origin["update_id"],)).fetchone()
            payload = json.loads(context["payload"]) if context else {}
            if (not row or row["text"] != origin["text"] or payload.get("origin") not in {"typed", "telegram_text"}
                    or payload.get("forwarded_data") or not 0 <= self.clock() - row["received_at"] <= 1800
                    or not re.match(r"^(?:please\s+)?follow[\s-]+up\b", row["text"], re.I)):
                raise PermissionError("A fresh typed follow-up from the paired owner is required.")
        return self.create(owner, "owner_conversation", f"followup:{origin['update_id']}",
                           f"Follow-up to request {identity}.\nCurrent owner instruction:\n{origin['text']}",
                           json.loads(parent["targets"]), expected_preference_revision=revision,
                           origin_ids=[origin["update_id"]], parent_id=identity)

    def steer(self, owner, identity, payload, *, source="telegram_miniapp"):
        """Queue one new instruction for the original targets, without rewriting or interrupting them."""
        if source not in ("telegram_miniapp", "local_owner_ui"):
            raise ValueError("This steering source is not supported.")
        if not isinstance(payload, dict) or set(payload) != {"request_key", "text", "expected_revision"}:
            raise ValueError("Provide a steering prompt, request key and original revision.")
        key, revision = payload["request_key"], payload["expected_revision"]
        if not isinstance(key, str) or not re.fullmatch(r"[A-Za-z0-9_.:-]{1,160}", key):
            raise ValueError("A stable request identity is required.")
        text = self._body(payload["text"])
        if not text.strip() or type(revision) is not int or revision < 1:
            raise ValueError("Enter a steering prompt and use the current request revision.")
        with self.store.db() as db:
            db.execute("BEGIN IMMEDIATE")
            preference = self._principal(db, owner)
            parent = self._row(db, identity, owner)
            existing = db.execute("SELECT * FROM project_requests WHERE owner=? AND client_key=?",
                                  (owner, key)).fetchone()
            if existing:
                link = db.execute("SELECT * FROM project_request_steers WHERE child_id=? AND owner=?",
                                  (existing["id"], owner)).fetchone()
                if (not link or link["parent_id"] != identity or link["parent_revision"] != revision
                        or existing["source"] != source or existing["text"] != text):
                    raise ConflictError("This request key already belongs to another instruction.")
                # A lost response remains reconcilable after cancellation or a registry change.
                return self._public(db, existing)
            if revision != parent["revision"] or not self._public(db, parent)["can_steer"]:
                raise ConflictError("The original request changed. Refresh its receipt before steering.")
            targets = self._targets(json.loads(parent["targets"]))
            child, now = str(uuid.uuid4()), self.clock()
            db.execute("INSERT INTO project_requests VALUES(?,?,?,?,?,1,?,?,?,?,?)",
                       (child, owner, key, source, "awaiting_input", text, json.dumps(targets),
                        preference, now, now))
            db.execute("INSERT INTO project_request_steers VALUES(?,?,?,?)", (child, identity, owner, revision))
            self._queue(db, self._row(db, child, owner), targets)
            return self._public(db, self._row(db, child, owner))

    def answer(self, owner, identity, text, targets, expected_revision):
        text = self._body(text)
        with self.store.db() as db:
            db.execute("BEGIN IMMEDIATE")
            current_preference = self._principal(db, owner)
            row = self._row(db, identity, owner)
            if type(expected_revision) is not int or row["revision"] != expected_revision or row["state"] != "awaiting_input":
                raise ConflictError("The request changed or was already submitted. Refresh its receipt.")
            if current_preference != row["preference_revision"]:
                raise ConflictError("Your preferences changed. Start a fresh request with the current settings.")
            resolved = self._targets(targets)
            if not text.strip() or not resolved:
                raise ValueError("Choose a project and enter the prompt before sending.")
            db.execute("UPDATE project_requests SET text=?,targets=?,revision=revision+1,updated_at=? WHERE id=?",
                       (text, json.dumps(resolved), self.clock(), identity))
            self._queue(db, self._row(db, identity, owner), resolved)
            return self._public(db, self._row(db, identity, owner))

    def get(self, owner, identity):
        with self.store.db() as db:
            self._principal(db, owner)
            return self._public(db, self._row(db, identity, owner))

    def for_origin(self, owner, update_id):
        try:
            update_id = int(update_id)
        except (TypeError, ValueError):
            return None
        with self.store.db() as db:
            self._principal(db, owner)
            origin = db.execute("SELECT request_id FROM project_request_origins WHERE owner=? AND update_id=?",
                                (owner, update_id)).fetchone()
            return self._public(db, self._row(db, origin["request_id"], owner)) if origin else None

    def link_origins(self, owner, identity, update_ids):
        """Internal reconciliation only; this operation has no public API."""
        with self.store.db() as db:
            db.execute("BEGIN IMMEDIATE")
            self._principal(db, owner); self._row(db, identity, owner)
            for update_id in update_ids:
                row = db.execute("SELECT state,receipt FROM inbox WHERE update_id=? AND owner_id=?", (update_id, owner)).fetchone()
                if not row or row["state"] not in ("conversation_done", "conversation_failed", "responding", "acting"):
                    raise ConflictError("The source request is already routed, held, cancelled or unavailable.")
                existing = db.execute("SELECT request_id FROM project_request_origins WHERE owner=? AND update_id=?", (owner, update_id)).fetchone()
                if existing and existing["request_id"] != identity:
                    raise ConflictError("This source request was already reconciled.")
                db.execute("INSERT OR IGNORE INTO project_request_origins VALUES(?,?,?,?,?)",
                           (owner, update_id, identity, row["state"], row["receipt"]))

    def list(self, owner, project=None, limit=30):
        if project is not None and project not in self.tasks():
            raise ValueError("Choose a registered project.")
        with self.store.db() as db:
            self._principal(db, owner)
            condition="owner=?"
            parameters=[owner]
            if project is not None:
                condition+=" AND EXISTS (SELECT 1 FROM json_each(project_requests.targets) target WHERE json_extract(target.value,'$.alias')=?)"
                parameters.append(project)
            parameters.append(min(100,max(1,limit)))
            rows=db.execute("SELECT * FROM project_requests WHERE "+condition+
                            " ORDER BY created_at DESC LIMIT ?",parameters).fetchall()
            return [self._public(db,row) for row in rows]

    def cancel(self, owner, identity, expected_revision):
        with self.store.db() as db:
            db.execute("BEGIN IMMEDIATE")
            self._principal(db, owner)
            row = self._row(db, identity, owner)
            if row["state"] == "cancelled":
                return self._public(db, row)
            if type(expected_revision) is not int or row["revision"] != expected_revision:
                raise ConflictError("The request changed. Refresh before cancelling.")
            if row["state"] == "awaiting_input":
                db.execute("UPDATE project_requests SET state='cancelled',revision=revision+1,updated_at=? WHERE id=?",
                           (self.clock(), identity))
            else:
                changed = db.execute(
                    "UPDATE inbox SET state='cancelled',receipt='Cancelled by the owner before routing.',handled_at=? "
                    "WHERE owner_id=? AND state='queued' AND update_id IN "
                    "(SELECT update_id FROM project_request_routes WHERE request_id=?)",
                    (self.clock(), owner, identity),
                ).rowcount
                if not changed:
                    raise ConflictError("Routing has already started. Its actual receipt remains available.")
                db.execute("UPDATE project_requests SET revision=revision+1,updated_at=? WHERE id=?",
                           (self.clock(), identity))
            return self._public(db, self._row(db, identity, owner))

    @staticmethod
    def routing_receipt(request):
        if not request["routes"]:
            return "No instruction is queued. Complete the project and prompt fields."
        lines = []
        for route in request["routes"]:
            lines.append(f"{route['label']}: {route['state']}. "
                         + (route.get("blocker") or "Routing receipt recorded; completion is not confirmed."))
        lines.append(f"Request {request['id']}")
        return "\n".join(lines)
