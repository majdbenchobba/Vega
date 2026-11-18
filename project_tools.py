"""Owner-reviewed local project changes, bounded reads and durable receipts."""
from __future__ import annotations

import difflib
import json
import os
from pathlib import Path
import re
import threading
import time
import uuid

from companion import ConflictError
from conversation import ConversationError
from memory_policy import contains_secret
from project_files import (
    MAX_BYTES, PROTECTED_NAMES, ProjectFiles, bytes_from_text, digest,
    file_identity, relative_path, text_from_bytes,
)
from project_requests import ProjectRequests
from routing_targets import dispatch_target


LIFETIME = 30 * 60
PENDING = {"preparing", "awaiting_approval"}


def _hash(value):
    return digest(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode())


class ProjectTools:
    def __init__(self, store, tasks, *, workspaces=None, clock=time.time):
        self.store, self.tasks, self.clock = store, tasks, clock
        self.lock = threading.RLock()
        self.jobs = {}
        with store.db() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS project_tool_workspaces (
                    owner INTEGER NOT NULL, project TEXT NOT NULL, thread_id TEXT NOT NULL,
                    root TEXT NOT NULL, root_identity TEXT NOT NULL,
                    revision INTEGER NOT NULL, can_write INTEGER NOT NULL,
                    PRIMARY KEY(owner,project)
                );
                CREATE TABLE IF NOT EXISTS project_tool_runs (
                    id TEXT PRIMARY KEY, owner INTEGER NOT NULL, project TEXT NOT NULL,
                    thread_id TEXT NOT NULL, operation TEXT NOT NULL, path TEXT NOT NULL,
                    state TEXT NOT NULL, detail TEXT NOT NULL, created_at REAL NOT NULL,
                    finished_at REAL
                );
                CREATE TABLE IF NOT EXISTS project_tool_proposals (
                    id TEXT PRIMARY KEY, owner INTEGER NOT NULL, client_key TEXT NOT NULL,
                    payload_hash TEXT NOT NULL, source TEXT NOT NULL, project TEXT NOT NULL,
                    thread_id TEXT NOT NULL, workspace_revision INTEGER NOT NULL,
                    preference_revision INTEGER NOT NULL, path TEXT NOT NULL,
                    original BLOB NOT NULL, proposed BLOB NOT NULL, file_identity TEXT NOT NULL,
                    before_sha256 TEXT NOT NULL, after_sha256 TEXT NOT NULL,
                    review_hash TEXT NOT NULL, summary TEXT NOT NULL, instruction TEXT NOT NULL,
                    state TEXT NOT NULL, revision INTEGER NOT NULL,
                    created_at REAL NOT NULL, expires_at REAL NOT NULL,
                    approved_at REAL, finished_at REAL, detail TEXT NOT NULL,
                    origin_id TEXT, provider_admitted_at REAL,
                    provider_active INTEGER NOT NULL DEFAULT 0, UNIQUE(owner,client_key)
                );
            """)
            columns = {
                column["name"] for column in db.execute("PRAGMA table_info(project_tool_proposals)")
            }
            if "provider_admitted_at" not in columns:
                db.execute("ALTER TABLE project_tool_proposals ADD COLUMN provider_admitted_at REAL")
            if "provider_active" not in columns:
                db.execute("ALTER TABLE project_tool_proposals ADD COLUMN provider_active INTEGER NOT NULL DEFAULT 0")
            for name in ("review_run","review_state","review_binding"):
                if name not in columns:
                    db.execute(f"ALTER TABLE project_tool_proposals ADD COLUMN {name} TEXT")
            # Recovery records uncertainty. A stopped write/model call is never
            # dispatched again by opening the host or refreshing the browser.
            db.execute("UPDATE project_tool_proposals SET state='uncertain',revision=revision+1,"
                       "detail='Vega stopped during the write. Inspect the current file; this action will not repeat.',"
                       "finished_at=? WHERE state='executing'", (clock(),))
            db.execute("UPDATE project_tool_proposals SET state='interrupted',revision=revision+1,"
                       "detail='Vega stopped while preparing this change. Nothing was applied; the model call will not repeat.',"
                       "finished_at=? WHERE state='preparing'", (clock(),))
            # The previous host's local jobs ended. Preserve cancellation and
            # uncertainty receipts without claiming a remote request succeeded.
            db.execute("UPDATE project_tool_proposals SET provider_active=0 WHERE provider_active<>0")
        # Bootstrap only the explicitly configured project, once for the
        # currently paired owner. Re-pairing does not inherit file permissions.
        if (workspaces and store.get("project_tools_bootstrap_owner") is None
                and type(store.get("owner_id")) is int
                and store.get("owner_id") == store.get("owner_chat_id")):
            owner = store.get("owner_id")
            with store.db() as db:
                db.execute("BEGIN IMMEDIATE")
                ProjectRequests._principal(db, owner)
                for project, value in workspaces.items():
                    if project not in tasks():
                        continue
                    files = ProjectFiles(value["root"])
                    identity = files.identity()
                    db.execute("INSERT OR IGNORE INTO project_tool_workspaces VALUES(?,?,?,?,?,1,?)",
                               (owner, project, dispatch_target(tasks(), project)["thread_id"],
                                str(files.root), identity, int(value.get("can_write") is True)))
                db.execute("INSERT OR REPLACE INTO settings(key,value) VALUES('project_tools_bootstrap_owner',?)",
                           (json.dumps(owner),))

    def _principal(self, db, owner):
        return ProjectRequests._principal(db, owner)

    def active_provider_calls(self):
        with self.store.db() as db:
            return db.execute("SELECT count(*) FROM project_tool_proposals WHERE provider_active=1").fetchone()[0]

    def _workspace(self, db, owner, binding, *, writing=False):
        self._principal(db, owner)
        project = binding.get("project")
        if not isinstance(project, str) or project not in self.tasks():
            raise ValueError("Choose an existing registered project.")
        current = dispatch_target(self.tasks(), project)
        row = db.execute("SELECT * FROM project_tool_workspaces WHERE owner=? AND project=?",
                         (owner, project)).fetchone()
        if not row:
            raise ValueError("This project has no registered local folder. Its coordinator route remains available.")
        if (row["thread_id"] != current["thread_id"] or binding.get("thread_id") != row["thread_id"]
                or type(binding.get("workspace_revision")) is not int
                or binding["workspace_revision"] != row["revision"]):
            raise ConflictError("The project destination or file permissions changed. Refresh before continuing.")
        if writing and not row["can_write"]:
            raise PermissionError("Reviewed writes are disabled for this project.")
        return row, ProjectFiles(row["root"], row["root_identity"])

    def projects(self, owner):
        with self.store.db() as db:
            self._principal(db, owner)
            workspaces = {row["project"]: row for row in db.execute(
                "SELECT * FROM project_tool_workspaces WHERE owner=?", (owner,))}
        projects = []
        for alias, info in self.tasks().items():
            target = dispatch_target(self.tasks(), alias)
            row = workspaces.get(alias)
            workspace = None
            if row and row["thread_id"] == target["thread_id"]:
                workspace = {"revision": row["revision"], "root": row["root"], "can_read": True,
                             "can_write": bool(row["can_write"]) and os.name == "nt"}
            projects.append({"alias": alias, "label": info["label"], "thread_id": target["thread_id"],
                             "workspace": workspace})
        return {"available": True, "projects": projects}

    def register_workspace(self, owner, payload):
        project = payload.get("project")
        if not isinstance(project, str) or project not in self.tasks():
            raise ValueError("Choose an existing registered project.")
        root = payload.get("root")
        if not isinstance(root, str) or not root.strip() or len(root) > 1024:
            raise ValueError("Enter an absolute local project folder.")
        files = ProjectFiles(root.strip())
        identity = files.identity()
        current = dispatch_target(self.tasks(), project)
        with self.store.db() as db:
            db.execute("BEGIN IMMEDIATE")
            self._principal(db, owner)
            existing = db.execute(
                "SELECT root,root_identity,revision FROM project_tool_workspaces WHERE owner=? AND project=?",
                (owner, project),
            ).fetchone()
            canonical_root = str(files.root)
            if (existing and existing["root"] == canonical_root
                    and existing["root_identity"] == identity):
                pass
            else:
                revision = (existing["revision"] + 1) if existing else 1
                db.execute(
                    "INSERT OR REPLACE INTO project_tool_workspaces "
                    "(owner,project,thread_id,root,root_identity,revision,can_write) VALUES(?,?,?,?,?,?,1)",
                    (owner, project, current["thread_id"], canonical_root, identity, revision),
                )
        return self.projects(owner)

    def _run(self, owner, binding, operation, path, result, detail):
        identity = str(uuid.uuid4())
        now = self.clock()
        with self.store.db() as db:
            db.execute("BEGIN IMMEDIATE")
            self._workspace(db, owner, binding)
            db.execute("INSERT INTO project_tool_runs VALUES(?,?,?,?,?,?,?,?,?,?)",
                       (identity, owner, binding["project"], binding["thread_id"],
                        operation, path, "completed", detail, now, now))
        return {**result, "receipt": {"id": identity, "operation": operation, "state": "completed",
                                     "detail": detail, "finished_at": now}}

    def inspect(self, owner, payload):
        path = relative_path(payload.get("path", ""), directory=True)
        with self.store.db() as db:
            _, files = self._workspace(db, owner, payload)
        result = files.inspect(path)
        return self._run(owner, payload, "inspect", path, result, f"Listed {len(result['entries'])} available entries.")

    def search(self, owner, payload):
        path = relative_path(payload.get("path", ""), directory=True)
        with self.store.db() as db:
            _, files = self._workspace(db, owner, payload)
        result = files.search(payload.get("query"), path)
        return self._run(owner, payload, "search", path, result,
                         f"Found {len(result['matches'])} matches in {result['files_scanned']} inspected text files.")

    @staticmethod
    def _writable(path, workspace):
        return (bool(workspace["can_write"]) and os.name == "nt"
                and Path(path).name.casefold() not in PROTECTED_NAMES)

    def read(self, owner, payload):
        path = relative_path(payload.get("path"))
        with self.store.db() as db:
            workspace, files = self._workspace(db, owner, payload)
        content = files.read(path)
        return self._run(owner, payload, "read", path, {"file": {
            "path": path, "text": content["text"], "sha256": content["sha256"],
            "bytes": len(content["data"]), "writable": self._writable(path, workspace),
        }}, f"Read {len(content['data'])} bytes from the current file.")

    @staticmethod
    def _client_key(value):
        if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_.:-]{1,160}", value):
            raise ValueError("A stable request identity is required.")
        return value

    def _row(self, db, owner, identity):
        if not isinstance(identity, str) or len(identity) > 100:
            raise ValueError("Choose an existing file-change proposal.")
        row = db.execute("SELECT * FROM project_tool_proposals WHERE owner=? AND id=?", (owner, identity)).fetchone()
        if not row:
            raise ValueError("This file-change proposal is unavailable.")
        return row

    def _public(self, row, *, delegation=None):
        state = row["state"]
        if state in PENDING and self.clock() > row["expires_at"]:
            state = "expired"
        before, after = text_from_bytes(row["original"]), text_from_bytes(row["proposed"])
        diff_lines = [] if state == "preparing" else list(difflib.unified_diff(
            before.splitlines(keepends=True), after.splitlines(keepends=True),
            fromfile="saved/" + row["path"], tofile="proposed/" + row["path"],
        ))
        diff = "".join(line if line.endswith("\n") else line + "\n\\ No newline at end of file\n"
                       for line in diff_lines)
        # Keep no-newline changes legible without inventing an added newline in
        # the bytes that were approved.
        if not diff and row["before_sha256"] != row["after_sha256"]:
            diff = "File encoding or line-ending change. Review the file bytes before applying."
        info = self.tasks().get(row["project"], {})
        review_run=row["review_run"] if "review_run" in row.keys() else None
        review_state=row["review_state"] if review_run else None
        return {
            "id": row["id"], "project": row["project"], "label": info.get("label", row["project"]),
            "path": row["path"], "state": state, "revision": row["revision"],
            "source": row["source"], "summary": row["summary"], "diff": diff,
            "before_sha256": row["before_sha256"], "after_sha256": row["after_sha256"],
            "review_hash": row["review_hash"], "created_at": row["created_at"], "expires_at": row["expires_at"],
            "provider_admitted_at": row["provider_admitted_at"],
            "receipt": {"id": row["id"], "operation": "file_change", "state": state,
                        "detail": "This proposal expired without applying." if state == "expired" else row["detail"],
                        "finished_at": row["finished_at"]},
            "can_apply": state == "awaiting_approval" and (
                not review_run or delegation==review_run and review_state=="accepted"),
            "review_run":review_run,"vega_review_state":review_state,
            "can_cancel": state in PENDING,
            "can_undo": state == "applied",
        }

    def get(self, owner, identity):
        with self.store.db() as db:
            self._principal(db, owner)
            return {"proposal": self._public(self._row(db, owner, identity))}

    def list(self, owner, project):
        if project not in self.tasks():
            raise ValueError("Choose an existing project.")
        with self.store.db() as db:
            self._principal(db, owner)
            rows = db.execute("SELECT * FROM project_tool_proposals WHERE owner=? AND project=? "
                              "ORDER BY created_at DESC LIMIT 30", (owner, project)).fetchall()
            return {"proposals": [self._public(row) for row in rows]}

    def _review_hash(self, row, proposed):
        value={key: row[key] for key in (
            "id", "owner", "project", "thread_id", "workspace_revision", "preference_revision",
            "path", "before_sha256", "file_identity",
        )} | {"after_sha256": digest(proposed)}
        if "review_run" in row.keys() and row["review_run"]:
            value["review_run"]=row["review_run"]
        return _hash(value)

    def _prepare(self, owner, payload, *, source, proposed_bytes=None, origin_id=None, review_run=None):
        key = self._client_key(payload.get("request_key"))
        path = relative_path(payload.get("path"))
        expected = payload.get("expected_sha256")
        if not isinstance(expected, str) or not re.fullmatch("[a-f0-9]{64}", expected):
            raise ValueError("Read the current file before preparing a change.")
        instruction = payload.get("instruction", "") if source == "vega_suggestion" else ""
        if source == "vega_suggestion" and (
            not isinstance(instruction, str) or not 1 <= len(instruction.strip()) <= 2000 or contains_secret(instruction)
        ):
            raise ValueError("Describe this file change in 1–2,000 characters without credentials.")
        if source == "owner_edit" and (not isinstance(payload.get("text"), str) or len(payload["text"]) > MAX_BYTES):
            raise ValueError("Enter the proposed file text.")
        fingerprint_data={
            "source": source, "project": payload.get("project"), "thread_id": payload.get("thread_id"),
            "workspace_revision": payload.get("workspace_revision"), "path": path, "expected": expected,
            "text": payload.get("text") if source == "owner_edit" else None, "instruction": instruction, "origin": origin_id,
        }
        if review_run is not None:
            if source!="vega_suggestion" or not isinstance(review_run,str) or not re.fullmatch(r"[a-f0-9-]{36}",review_run):
                raise ValueError("Invalid agent review binding.")
            fingerprint_data["review_run"]=review_run
        fingerprint=_hash(fingerprint_data)
        with self.lock, self.store.db() as db:
            db.execute("BEGIN IMMEDIATE")
            preference = self._principal(db, owner)
            prior = db.execute("SELECT * FROM project_tool_proposals WHERE owner=? AND client_key=?", (owner, key)).fetchone()
            if prior:
                if prior["payload_hash"] != fingerprint:
                    raise ConflictError("This request identity already belongs to another proposed change.")
                return prior["id"], False
            workspace, files = self._workspace(db, owner, payload, writing=True)
            if review_run is not None:
                run=db.execute("SELECT owner,project,thread_id,state FROM team_runs WHERE id=?",(review_run,)).fetchone()
                if (not run or run["owner"]!=owner or run["project"]!=payload["project"]
                        or run["thread_id"]!=workspace["thread_id"]
                        or run["state"] not in {"delegating","preparing"}):
                    raise ConflictError("The owning agent task is not preparing this change.")
            if not self._writable(path, workspace):
                raise ValueError("This control or permission file is read-only here. Use the project's existing review workflow.")
            current = files.read(path)
            if current["sha256"] != expected:
                raise ConflictError("The file changed. Read its current contents before preparing a new edit.")
            if source == "vega_suggestion":
                if len(current["text"]) > 12000:
                    raise ValueError("Ask Vega to prepare changes for files up to 12,000 characters; larger files support manual review.")
                if db.execute("SELECT 1 FROM project_tool_proposals WHERE state='preparing'").fetchone():
                    raise ConflictError("A file suggestion is already being prepared. Wait for its receipt or cancel it.")
                proposed = current["data"]
            else:
                proposed = proposed_bytes if proposed_bytes is not None else bytes_from_text(payload["text"], current["data"])
                text_from_bytes(proposed)
                if proposed == current["data"]:
                    raise ValueError("The proposed text has no changes.")
            identity, now = str(uuid.uuid4()), self.clock()
            state = "preparing" if source == "vega_suggestion" else "awaiting_approval"
            summary = "Preparing your requested file change." if state == "preparing" else (
                "Review this undo before applying." if source == "undo" else "Review your edit before applying.")
            detail = "Nothing has been applied."
            values = {
                "id": identity, "owner": owner, "client_key": key, "payload_hash": fingerprint,
                "source": source, "project": payload["project"], "thread_id": workspace["thread_id"],
                "workspace_revision": workspace["revision"], "preference_revision": preference,
                "path": path, "original": current["data"], "proposed": proposed, "file_identity": current["identity"],
                "before_sha256": current["sha256"], "after_sha256": digest(proposed), "review_hash": "",
                "summary": summary, "instruction": instruction, "state": state, "revision": 1,
                "created_at": now, "expires_at": now + LIFETIME, "approved_at": None, "finished_at": None,
                "detail": detail, "origin_id": origin_id,
                "review_run":review_run,"review_state":"pending" if review_run else None,
                "review_binding":None,
            }
            values["review_hash"] = self._review_hash(values, proposed)
            db.execute("INSERT INTO project_tool_proposals (" + ",".join(values) + ") VALUES(" +
                       ",".join("?" for _ in values) + ")", tuple(values.values()))
            return identity, True

    def propose(self, owner, payload):
        identity, _ = self._prepare(owner, payload, source="owner_edit")
        return self.get(owner, identity)

    def suggest(self, owner, payload, generator, *, review_run=None):
        identity, created = self._prepare(owner, payload, source="vega_suggestion",review_run=review_run)
        if created:
            worker = threading.Thread(target=self._generate, args=(owner, identity, generator),
                                      name="vega-project-suggestion", daemon=True)
            self.jobs[identity] = worker
            worker.start()
        return self.get(owner, identity)

    def record_agent_review(self,db,owner,identity,*,run_id,expected_revision,review_hash,binding,accepted):
        """Internal quality gate; no HTTP endpoint or model-supplied authority."""
        row=self._row(db,owner,identity)
        if (type(accepted) is not bool or row["review_run"]!=run_id or row["review_state"]!="pending"
                or row["state"]!="awaiting_approval" or row["revision"]!=expected_revision
                or row["review_hash"]!=review_hash or not isinstance(binding,str)
                or not re.fullmatch(r"[a-f0-9]{64}",binding)):
            raise ConflictError("The proposal changed before Vega's review settled.")
        self._frozen(db,owner,row)
        if self.clock()>row["expires_at"]:
            raise ConflictError("The proposal expired before Vega's review settled.")
        db.execute("UPDATE project_tool_proposals SET review_state=?,review_binding=?,state=?,"
                   "revision=revision+1,detail=? WHERE id=?",(
                       "accepted" if accepted else "rejected",binding,
                       "awaiting_approval" if accepted else "rejected",
                       "Vega accepted this draft. Review and apply it from Vega work." if accepted else
                       "Vega requested revisions. This draft cannot be applied.",
                       identity,
                   ))

    def _frozen(self, db, owner, row):
        preference = self._principal(db, owner)
        workspace, files = self._workspace(db, owner, {
            "project": row["project"], "thread_id": row["thread_id"], "workspace_revision": row["workspace_revision"],
        }, writing=True)
        if preference != row["preference_revision"] or not self._writable(row["path"], workspace):
            raise ConflictError("Preferences or file permissions changed. Prepare a fresh review.")
        return files

    def _finish(self, db, owner, identity, state, detail):
        db.execute("UPDATE project_tool_proposals SET state=?,detail=?,revision=revision+1,finished_at=? "
                   "WHERE owner=? AND id=?", (state, detail, self.clock(), owner, identity))

    def _admit_generation(self, owner, identity, reserve):
        """Commit authorization and budget admission atomically with cancellation."""
        refused = None
        with self.lock, self.store.db() as db:
            db.execute("BEGIN IMMEDIATE")
            self._principal(db, owner)
            row = self._row(db, owner, identity)
            if row["state"] != "preparing" or row["provider_admitted_at"] is not None:
                raise ConflictError("This suggestion is cancelled, finished or already admitted. No new model request was admitted.")
            if self.clock() > row["expires_at"]:
                refused = "The suggestion expired before model admission. No model request was sent."
                self._finish(db, owner, identity, "expired", refused)
            else:
                try:
                    files = self._frozen(db, owner, row)
                    current = files.read(row["path"])
                    if current["sha256"] != row["before_sha256"] or current["identity"] != row["file_identity"]:
                        raise ConflictError("The file changed before model admission.")
                except (ConflictError, PermissionError, ValueError, OSError):
                    refused = "The project, permissions or file changed before model admission. No model request was sent."
                    self._finish(db, owner, identity, "stale", refused)
                else:
                    admitted = reserve(db)
                    # Registry data can refresh without writing this database.
                    # Validate its frozen destination again before committing.
                    self._frozen(db, owner, row)
                    db.execute("UPDATE project_tool_proposals SET provider_admitted_at=?,provider_active=1,"
                               "detail='Model request admitted. Waiting for its result; nothing has been applied.' "
                               "WHERE owner=? AND id=?", (admitted, owner, identity))
        if refused:
            raise ConflictError(refused)
        return admitted

    def _generate(self, owner, identity, generator):
        try:
            with self.store.db() as db:
                row = self._row(db, owner, identity)
                if row["state"] != "preparing":
                    return
                files = self._frozen(db, owner, row)
                if self.clock() > row["expires_at"]:
                    raise ValueError("This request expired before preparation.")
                current = files.read(row["path"])
                if current["sha256"] != row["before_sha256"] or current["identity"] != row["file_identity"]:
                    raise ConflictError("The file changed before Vega could prepare an edit.")
            result = generator(
                row["instruction"], row["path"], current["text"],
                admission=lambda reserve: self._admit_generation(owner, identity, reserve),
            )
            if not isinstance(result, dict) or set(result) != {"summary", "edits"}:
                raise ValueError("Vega did not return a valid proposed change.")
            summary, edits = result["summary"], result["edits"]
            if (not isinstance(summary, str) or not 1 <= len(summary) <= 400 or contains_secret(summary)
                    or not isinstance(edits, list) or not 1 <= len(edits) <= 6):
                raise ValueError("Vega returned no bounded change to review.")
            text = current["text"]
            for edit in edits:
                if (not isinstance(edit, dict) or set(edit) != {"before", "after"}
                        or not isinstance(edit["before"], str) or not edit["before"]
                        or not isinstance(edit["after"], str)
                        or len(edit["before"]) > MAX_BYTES or len(edit["after"]) > MAX_BYTES
                        or text.find(edit["before"]) < 0
                        or text.find(edit["before"], text.find(edit["before"]) + 1) >= 0):
                    raise ValueError("The proposed edit did not match one exact location in the selected file.")
                text = text.replace(edit["before"], edit["after"], 1)
                if len(text) > MAX_BYTES:
                    raise ValueError("The proposed file exceeds the size limit.")
            proposed = bytes_from_text(text, row["original"])
            if proposed == row["original"]:
                raise ValueError("Vega returned no file changes.")
            with self.lock, self.store.db() as db:
                db.execute("BEGIN IMMEDIATE")
                row = self._row(db, owner, identity)
                if row["state"] != "preparing":
                    return
                if row["provider_admitted_at"] is None:
                    raise ConversationError("The generator did not use guarded model admission. Its result was discarded.")
                files = self._frozen(db, owner, row)
                if self.clock() > row["expires_at"]:
                    self._finish(db, owner, identity, "expired", "The suggestion expired. Nothing was applied.")
                    return
                latest = files.read(row["path"])
                if latest["sha256"] != row["before_sha256"] or latest["identity"] != row["file_identity"]:
                    self._finish(db, owner, identity, "stale", "The file changed during preparation. Nothing was applied.")
                    return
                db.execute("UPDATE project_tool_proposals SET proposed=?,after_sha256=?,review_hash=?,summary=?,"
                           "state='awaiting_approval',revision=revision+1,detail='Prepared for your review. Nothing has been applied.' "
                           "WHERE id=?", (proposed, digest(proposed), self._review_hash(row, proposed), summary, identity))
        except Exception as error:
            with self.store.db() as db:
                row = self._row(db, owner, identity)
                if row["state"] == "preparing":
                    # Do not surface arbitrary provider/source exception bodies.
                    detail = "The file suggestion could not be prepared. Nothing was applied; this request will not repeat."
                    if isinstance(error, ConversationError):
                        detail = str(error)[:600] + " Nothing was applied; this request will not repeat."
                    state = "stale" if isinstance(error, (ConflictError, PermissionError)) else "failed"
                    self._finish(db, owner, identity, state, detail)
        finally:
            try:
                with self.store.db() as db:
                    db.execute("UPDATE project_tool_proposals SET provider_active=0 "
                               "WHERE owner=? AND id=? AND provider_active=1", (owner, identity))
            finally:
                self.jobs.pop(identity, None)

    def cancel(self, owner, identity, payload):
        with self.lock, self.store.db() as db:
            db.execute("BEGIN IMMEDIATE")
            self._principal(db, owner)
            row = self._row(db, owner, identity)
            if row["state"] == "cancelled":
                return {"proposal": self._public(row)}
            if type(payload.get("expected_revision")) is not int or payload["expected_revision"] != row["revision"]:
                raise ConflictError("The proposal changed. Refresh its receipt.")
            if row["state"] not in PENDING:
                raise ConflictError("This action already has a final receipt.")
            detail = "Cancelled by the owner. Nothing was applied."
            if row["state"] == "preparing" and row["source"] == "vega_suggestion":
                detail = (
                    "Cancelled before model admission. No model request was sent; nothing was applied."
                    if row["provider_admitted_at"] is None else
                    "Cancelled after model admission. The admitted request may still complete; its result will be discarded. Nothing was applied."
                )
            self._finish(db, owner, identity, "cancelled", detail)
            return {"proposal": self._public(self._row(db, owner, identity))}

    def apply(self, owner, identity, payload, *, guard=None):
        with self.lock:
            with self.store.db() as db:
                db.execute("BEGIN IMMEDIATE")
                self._principal(db, owner)
                row = self._row(db, owner, identity)
                # Exact retries retrieve the receipt; they never write twice.
                if row["state"] not in PENDING:
                    return {"proposal": self._public(row)}
                if (row["state"] != "awaiting_approval" or type(payload.get("expected_revision")) is not int
                        or payload["expected_revision"] != row["revision"] or payload.get("review_hash") != row["review_hash"]):
                    raise ConflictError("Review this exact proposal before applying it.")
                if row["review_run"] and (
                    guard is None or row["review_state"]!="accepted"
                    or not isinstance(row["review_binding"],str)
                    or not re.fullmatch(r"[a-f0-9]{64}",row["review_binding"])
                ):
                    raise ConflictError("This agent draft requires Vega's accepted review and application through Vega work.")
                if self.clock() > row["expires_at"]:
                    self._finish(db, owner, identity, "expired", "The review expired. Nothing was applied.")
                    return {"proposal": self._public(self._row(db, owner, identity))}
                try:
                    files = self._frozen(db, owner, row)
                except (PermissionError, ConflictError, ValueError):
                    self._finish(db, owner, identity, "stale", "The project, owner settings or file access changed. Nothing was applied.")
                    return {"proposal": self._public(self._row(db, owner, identity))}
            with files.open(row["path"], writing=True) as stream:
                current = stream.read(MAX_BYTES + 1)
                info = os.fstat(stream.fileno())
                if digest(current) != row["before_sha256"] or file_identity(info) != row["file_identity"]:
                    with self.store.db() as db:
                        self._finish(db, owner, identity, "stale", "The file changed after review. Nothing was applied.")
                    return self.get(owner, identity)
                with self.store.db() as db:
                    db.execute("BEGIN IMMEDIATE")
                    self._frozen(db, owner, row)
                    if guard is not None:
                        guard(db, "admit")
                    # Commit the intent and backup before touching the file.
                    # The exclusive handle remains open through both commits.
                    db.execute("UPDATE project_tool_proposals SET state='executing',approved_at=?,"
                               "detail='The owner approved this exact change; write receipt pending.' WHERE id=?",
                               (self.clock(), identity))
                with self.store.db() as db:
                    db.execute("BEGIN IMMEDIATE")
                    try:
                        self._frozen(db, owner, row)
                        if guard is not None:
                            guard(db, "write")
                    except (PermissionError, ConflictError, ValueError):
                        self._finish(db, owner, identity, "stale", "The permissions changed before the write. Nothing was applied.")
                        return {"proposal": self._public(self._row(db, owner, identity))}
                    try:
                        files.write_checked(stream, row["proposed"])
                    except Exception:
                        try:
                            files.write_checked(stream, row["original"])
                            state, detail = "failed", "The write failed. The original file bytes were restored and verified."
                        except Exception:
                            state, detail = "uncertain", "The write could not be verified. Inspect this file; its saved original is retained and this action will not repeat."
                        self._finish(db, owner, identity, state, detail)
                    else:
                        self._finish(db, owner, identity, "applied", "Applied this reviewed change and verified the resulting file hash.")
            return self.get(owner, identity)

    def undo(self, owner, identity, payload):
        with self.store.db() as db:
            self._principal(db, owner)
            row = self._row(db, owner, identity)
            if (row["state"] != "applied" or type(payload.get("expected_revision")) is not int
                    or payload["expected_revision"] != row["revision"]):
                raise ConflictError("Choose an applied change with its current receipt.")
            binding = {"project": row["project"], "thread_id": row["thread_id"],
                       "workspace_revision": row["workspace_revision"], "path": row["path"],
                       "expected_sha256": row["after_sha256"], "request_key": payload.get("request_key")}
        new_id, _ = self._prepare(owner, binding, source="undo", proposed_bytes=row["original"], origin_id=identity)
        return self.get(owner, new_id)
