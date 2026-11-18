"""Owner-scoped Copilot SDK sessions for explicitly registered Vega projects."""
from __future__ import annotations

import difflib
import json
import os
from pathlib import Path
import queue
import re
import sqlite3
import subprocess
import threading
import time
import uuid

from companion import ConflictError
from memory_policy import contains_secret
from openrouter_conversation import ConversationError
from project_files import MAX_BYTES, PROTECTED_NAMES, digest, is_text_path, relative_path


MAX_PROMPT_CHARS = 24_000
MAX_MODEL_CHARS = 200
MAX_PERMISSION_BYTES = 768 * 1024
MAX_PENDING_APPROVALS = 20
APPROVAL_LIFETIME = 10 * 60
SESSION_TIMEOUT = 630
SESSION_ID = re.compile(r"^[a-f0-9-]{36}$")
MODEL_ID = re.compile(r"^[A-Za-z0-9._:/-]{1,200}$")


class CopilotSessions:
    def __init__(self, store, project_tools, tasks, brain, root):
        self.store = store
        self.project_tools = project_tools
        self.tasks = tasks
        self.brain = brain
        self.root = Path(root)
        self.runner = self.root / "copilot_sdk_runner.mjs"
        self.lock = threading.RLock()
        self.session_locks = {}
        self.project_locks = {}
        with store.db() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS copilot_sessions (
                    id TEXT PRIMARY KEY, owner INTEGER NOT NULL, project TEXT NOT NULL,
                    thread_id TEXT NOT NULL, workspace_revision INTEGER NOT NULL,
                    root TEXT NOT NULL, root_identity TEXT NOT NULL, model TEXT NOT NULL,
                    title TEXT NOT NULL, created_at REAL NOT NULL, updated_at REAL NOT NULL,
                    state TEXT NOT NULL DEFAULT 'ready', last_error TEXT
                );
                CREATE TABLE IF NOT EXISTS copilot_messages (
                    id INTEGER PRIMARY KEY, session_id TEXT NOT NULL,
                    role TEXT NOT NULL, content TEXT NOT NULL, created_at REAL NOT NULL,
                    FOREIGN KEY(session_id) REFERENCES copilot_sessions(id)
                );
                CREATE TABLE IF NOT EXISTS copilot_approvals (
                    id TEXT PRIMARY KEY, owner INTEGER NOT NULL, session_id TEXT NOT NULL,
                    project TEXT NOT NULL, thread_id TEXT NOT NULL,
                    workspace_revision INTEGER NOT NULL, root TEXT NOT NULL,
                    root_identity TEXT NOT NULL, request_id TEXT NOT NULL,
                    request_hash TEXT NOT NULL, preview_json TEXT NOT NULL,
                    state TEXT NOT NULL, revision INTEGER NOT NULL,
                    created_at REAL NOT NULL, expires_at REAL NOT NULL,
                    decided_at REAL, detail TEXT NOT NULL,
                    UNIQUE(session_id,request_id),
                    FOREIGN KEY(session_id) REFERENCES copilot_sessions(id)
                );
            """)
            columns = {row["name"] for row in db.execute("PRAGMA table_info(copilot_sessions)")}
            if "state" not in columns:
                db.execute("ALTER TABLE copilot_sessions ADD COLUMN state TEXT NOT NULL DEFAULT 'ready'")
            if "last_error" not in columns:
                db.execute("ALTER TABLE copilot_sessions ADD COLUMN last_error TEXT")
            db.execute(
                "UPDATE copilot_sessions SET state='interrupted',last_error=? WHERE state='running'",
                ("Vega stopped during this turn. The prompt was not replayed; inspect the transcript before continuing.",),
            )
            db.execute(
                "UPDATE copilot_approvals SET state='interrupted',revision=revision+1,"
                "preview_json='{}',decided_at=?,detail=? WHERE state IN ('pending','approved')",
                (time.time(), "Vega stopped before this permission decision was confirmed by the session."),
            )

    def projects(self, owner):
        return self.project_tools.projects(owner)

    def register_workspace(self, owner, payload):
        project = payload.get("project")
        if not isinstance(project, str):
            raise ValueError("Choose an existing registered project.")
        project_lock = self._project_lock(project)
        if not project_lock.acquire(blocking=False):
            raise ConflictError("This project has an active agent turn. Try registering its folder again when it finishes.")
        try:
            with self.store.db() as db:
                self.project_tools._principal(db, owner)
            return self.project_tools.register_workspace(owner, payload)
        finally:
            project_lock.release()

    def _workspace(self, owner, project, thread_id=None, revision=None, *, writing=True):
        if not isinstance(project, str) or project not in self.tasks():
            raise ValueError("Choose an existing registered project.")
        current = self.project_tools.projects(owner)
        selected = next((item for item in current["projects"] if item["alias"] == project), None)
        workspace = selected.get("workspace") if selected else None
        if not workspace:
            raise ValueError("Register a local folder for this project before starting an agent session.")
        binding = {"project": project, "thread_id": selected["thread_id"],
                   "workspace_revision": workspace["revision"]}
        if thread_id is not None and (
            thread_id != binding["thread_id"] or revision != binding["workspace_revision"]
        ):
            raise ConflictError("The project folder or access changed. Refresh before continuing.")
        with self.store.db() as db:
            row, files = self.project_tools._workspace(db, owner, binding, writing=writing)
        return selected, row, files

    def _run(self, owner, request):
        if not self.runner.is_file():
            raise ConversationError("The Copilot SDK runner is unavailable.")
        key_loader = getattr(self.brain, "copilot_api_key", None) or self.brain._load_api_key
        key = key_loader()
        base_url = getattr(self.brain, "copilot_base_url", None)
        if not isinstance(base_url, str) or not base_url:
            raise ConversationError("The Copilot model provider endpoint is not configured.")
        environment = {
            name: os.environ[name] for name in (
                "PATH", "SYSTEMROOT", "WINDIR", "TEMP", "TMP", "USERPROFILE",
                "APPDATA", "LOCALAPPDATA", "HOMEDRIVE", "HOMEPATH", "PROGRAMDATA",
            ) if os.environ.get(name)
        }
        if key:
            environment["COPILOT_API_KEY"] = key
        request = {**request, "baseUrl": base_url}
        process = None
        responses = queue.Queue()

        def drain(stream, name):
            try:
                for line in stream:
                    responses.put((name, line))
            finally:
                responses.put((name, None))

        try:
            process = subprocess.Popen(
                ["node", str(self.runner)], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=subprocess.PIPE, text=True, encoding="utf-8", errors="replace",
                bufsize=1, cwd=self.root, env=environment,
            )
            threading.Thread(target=drain, args=(process.stdout, "stdout"), daemon=True).start()
            threading.Thread(target=drain, args=(process.stderr, "stderr"), daemon=True).start()
            process.stdin.write(json.dumps(request, ensure_ascii=False) + "\n")
            process.stdin.flush()
        except OSError:
            if process and process.poll() is None:
                process.kill()
                process.wait()
            raise ConversationError("The Copilot SDK runtime could not be started.") from None

        active = {}
        response = None
        stdout_closed = False
        deadline = time.monotonic() + SESSION_TIMEOUT
        try:
            while time.monotonic() < deadline:
                for request_id, approval_id in list(active.items()):
                    try:
                        state = self._approval_state(owner, approval_id)
                    except sqlite3.Error:
                        raise ConversationError("Vega could not read the owner approval decision.") from None
                    if state == "pending":
                        continue
                    process.stdin.write(json.dumps({
                        "type": "permission_decision", "requestId": request_id,
                        "decision": "approve" if state == "approved" else "reject",
                    }) + "\n")
                    process.stdin.flush()
                    active.pop(request_id)

                try:
                    name, line = responses.get(timeout=0.1)
                except queue.Empty:
                    if process.poll() is not None and stdout_closed:
                        break
                    continue
                if name == "stderr":
                    continue
                if line is None:
                    stdout_closed = True
                    continue
                if len(line) > MAX_PERMISSION_BYTES + 16 * 1024:
                    raise ConversationError("The Copilot SDK returned an oversized event.")
                try:
                    message = json.loads(line)
                except json.JSONDecodeError:
                    raise ConversationError("The Copilot SDK returned an invalid event.") from None
                if not isinstance(message, dict):
                    raise ConversationError("The Copilot SDK returned an invalid event.")
                if message.get("type") == "permission_request":
                    if message.get("sessionId") != request.get("sessionId"):
                        raise ConversationError("The Copilot SDK requested approval for a different session.")
                    try:
                        approval = self.enqueue_approval(owner, message)
                    except (ValueError, ConflictError, PermissionError, OSError):
                        process.stdin.write(json.dumps({
                            "type": "permission_decision", "requestId": message.get("requestId"),
                            "decision": "reject",
                        }) + "\n")
                        process.stdin.flush()
                    except sqlite3.Error:
                        raise ConversationError(
                            "Vega could not save this approval request. No file write was approved."
                        ) from None
                    else:
                        active[message["requestId"]] = approval["approval"]["id"]
                else:
                    response = message
                    break

            if response is None:
                expired = time.monotonic() >= deadline
                self._settle_approvals(
                    request.get("sessionId"), "expired" if expired else "interrupted",
                    "The approval or agent turn expired. No prompt was replayed." if expired
                    else "The Copilot session stopped before returning a result.",
                )
                raise ConversationError(
                    "The Copilot session or its approval request expired after 10 minutes."
                    if expired else "The Copilot SDK returned no result."
                )
        except (BrokenPipeError, OSError):
            self._settle_approvals(request.get("sessionId"), "interrupted",
                                   "The Copilot session stopped before completing this permission request.")
            raise ConversationError("The Copilot SDK session stopped before completing its request.") from None
        finally:
            if process.stdin:
                try:
                    process.stdin.close()
                except OSError:
                    pass
            if process.poll() is None:
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
            process.wait()
            process.stdout.close()
            process.stderr.close()

        self._settle_approvals(request.get("sessionId"), "interrupted",
                               "The session ended before this pending permission request was decided.")
        if response is None:
            raise ConversationError("The Copilot SDK returned no result.")
        if process.returncode != 0 or not response.get("ok"):
            raise ConversationError(str(response.get("error") or "The Copilot SDK request failed.")[:1200])
        if response.get("sessionId") != request.get("sessionId") or not isinstance(response.get("content"), str):
            raise ConversationError("The Copilot SDK returned a mismatched session response.")
        return response

    @staticmethod
    def _relative_preview_path(value, root):
        if not isinstance(value, str) or not Path(value).is_absolute():
            raise ValueError("The file preview did not contain an absolute project path.")
        absolute = os.path.abspath(value)
        try:
            common = os.path.commonpath((str(root), absolute))
        except ValueError:
            raise ValueError("The file preview points outside the registered project.") from None
        if os.path.normcase(common) != os.path.normcase(str(root)):
            raise ValueError("The file preview points outside the registered project.")
        relative = os.path.relpath(absolute, str(root))
        if relative in (".", os.pardir) or relative.startswith(os.pardir + os.sep):
            raise ValueError("The file preview points outside the registered project.")
        relative = relative_path(relative)
        if not is_text_path(relative) or Path(relative).name.casefold() in PROTECTED_NAMES:
            raise ValueError("This protected or unsupported file cannot enter the approval queue.")
        return relative

    def _normalize_permission(self, permission, root):
        if (not isinstance(permission, dict) or permission.get("kind") != "write"
                or permission.get("managedApprovalRequired") or permission.get("requestSandboxBypass")):
            raise ValueError("Only file-write requests can enter this approval queue.")
        intention = permission.get("intention")
        diff = permission.get("diff")
        edits = permission.get("fileEdits")
        if (not isinstance(intention, str) or not intention.strip() or len(intention) > 4000
                or not isinstance(diff, str) or not diff.strip() or len(diff.encode("utf-8")) > MAX_PERMISSION_BYTES
                or not isinstance(edits, list) or not 1 <= len(edits) <= 20):
            raise ValueError("The file change did not include a complete review preview.")
        normalized = []
        targets = set()
        for edit in edits:
            if not isinstance(edit, dict):
                raise ValueError("The file change preview is incomplete.")
            before, after = edit.get("before"), edit.get("after")
            if not isinstance(after, dict) or not isinstance(after.get("content"), str):
                raise ValueError("File deletions cannot enter the approval queue.")
            after_path = self._relative_preview_path(after.get("path"), root)
            before_text = None
            if before is not None:
                if not isinstance(before, dict) or not isinstance(before.get("content"), str):
                    raise ValueError("The file change preview is incomplete.")
                before_path = self._relative_preview_path(before.get("path"), root)
                if before_path != after_path:
                    raise ValueError("File moves cannot enter the approval queue.")
                before_text = before["content"].replace("\r\n", "\n")
            after_text = after["content"].replace("\r\n", "\n")
            if (len(after_text.encode("utf-8")) > MAX_BYTES
                    or (before_text is not None and len(before_text.encode("utf-8")) > MAX_BYTES)
                    or contains_secret(after_text) or (before_text is not None and contains_secret(before_text))):
                raise ValueError("This file preview is too large or contains credential-like content.")
            if before_text == after_text:
                raise ValueError("This file change does not contain an edit to review.")
            if after_path in targets:
                raise ValueError("The file change preview contains a duplicate path.")
            targets.add(after_path)
            normalized.append({"path": after_path, "before": before_text, "after": after_text})
        file_name = self._relative_preview_path(permission.get("fileName"), root)
        if file_name not in targets or contains_secret(intention):
            raise ValueError("The file change preview failed Vega's review checks.")
        rendered_diff = []
        for item in normalized:
            before_path = "/dev/null" if item["before"] is None else "saved/" + item["path"]
            after_path = "proposed/" + item["path"]
            lines = difflib.unified_diff(
                (item["before"] or "").splitlines(keepends=True),
                item["after"].splitlines(keepends=True),
                fromfile=before_path, tofile=after_path,
            )
            rendered_diff.extend(
                line if line.endswith("\n") else line + "\n\\ No newline at end of file\n"
                for line in lines
            )
        diff = "".join(rendered_diff)
        if not diff or len(diff.encode("utf-8")) > MAX_PERMISSION_BYTES or contains_secret(diff):
            raise ValueError("The file change preview is empty, too large or contains credential-like content.")
        preview = {
            "intention": intention.strip(),
            "files": normalized,
            "diff": diff,
            "tool_call_id": str(permission.get("toolCallId") or "")[:200],
        }
        if len(json.dumps(preview, ensure_ascii=False).encode("utf-8")) > MAX_PERMISSION_BYTES:
            raise ValueError("This file change is too large for Vega's review queue.")
        return preview

    @staticmethod
    def _preview_is_current(files, preview, *, bind=False):
        for item in preview["files"]:
            if item["before"] is None:
                if files.exists(item["path"]):
                    return False
                continue
            try:
                current = files.read(item["path"])
            except (OSError, ValueError, ConflictError):
                return False
            if current["text"] != item["before"]:
                return False
            if bind:
                item["before_sha256"] = current["sha256"]
                item["before_identity"] = current["identity"]
            elif (current["sha256"] != item.get("before_sha256")
                    or current["identity"] != item.get("before_identity")):
                return False
        return True

    @staticmethod
    def _approval_public(row):
        preview = json.loads(row["preview_json"])
        return {
            "id": row["id"], "session_id": row["session_id"], "project": row["project"],
            "title": row["title"], "intention": preview.get("intention", ""),
            "files": [{key: item[key] for key in ("path", "before", "after")}
                      for item in preview.get("files", [])],
            "diff": preview.get("diff", ""),
            "state": row["state"], "revision": row["revision"],
            "request_hash": row["request_hash"], "created_at": row["created_at"],
            "expires_at": row["expires_at"],
        }

    def enqueue_approval(self, owner, message):
        request_id = message.get("requestId") if isinstance(message, dict) else None
        session_id = message.get("sessionId") if isinstance(message, dict) else None
        if (not isinstance(request_id, str) or not SESSION_ID.fullmatch(request_id)
                or not isinstance(session_id, str) or not SESSION_ID.fullmatch(session_id)):
            raise ValueError("The Copilot SDK returned an invalid permission request.")
        now = time.time()
        with self.store.db() as db:
            db.execute("BEGIN IMMEDIATE")
            self.project_tools._principal(db, owner)
            session = db.execute(
                "SELECT * FROM copilot_sessions WHERE id=? AND owner=?", (session_id, owner)
            ).fetchone()
            if not session or session["state"] != "running":
                raise ConflictError("This agent session is no longer waiting for a file decision.")
            binding = {
                "project": session["project"], "thread_id": session["thread_id"],
                "workspace_revision": session["workspace_revision"],
            }
            workspace, files = self.project_tools._workspace(db, owner, binding, writing=True)
            if (str(files.root) != session["root"] or files.identity() != session["root_identity"]
                    or workspace["root"] != session["root"]):
                raise ConflictError("The registered project folder changed before review.")
            preview = self._normalize_permission(message.get("permission"), files.root)
            if not self._preview_is_current(files, preview, bind=True):
                raise ConflictError("The file changed before Vega could queue its review.")
            count = db.execute(
                "SELECT count(*) FROM copilot_approvals WHERE owner=? AND state='pending'", (owner,)
            ).fetchone()[0]
            if count >= MAX_PENDING_APPROVALS:
                raise ConflictError("The owner approval queue is full. Review or reject an existing request.")
            identity = str(uuid.uuid4())
            fingerprint = digest(json.dumps({
                "owner": owner, "session_id": session_id, "project": session["project"],
                "thread_id": session["thread_id"], "workspace_revision": session["workspace_revision"],
                "root_identity": session["root_identity"], "request_id": request_id, "preview": preview,
            }, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8"))
            db.execute(
                "INSERT INTO copilot_approvals "
                "(id,owner,session_id,project,thread_id,workspace_revision,root,root_identity,"
                "request_id,request_hash,preview_json,state,revision,created_at,expires_at,detail) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,'pending',1,?,?,?)",
                (identity, owner, session_id, session["project"], session["thread_id"],
                 session["workspace_revision"], session["root"], session["root_identity"],
                 request_id, fingerprint, json.dumps(preview, ensure_ascii=False),
                 now, now + APPROVAL_LIFETIME, "Waiting for the owner to review this file change."),
            )
            row = db.execute(
                "SELECT a.*,s.title FROM copilot_approvals a JOIN copilot_sessions s ON s.id=a.session_id "
                "WHERE a.id=?", (identity,),
            ).fetchone()
        return {"approval": self._approval_public(row)}

    def list_approvals(self, owner):
        now = time.time()
        with self.store.db() as db:
            db.execute("BEGIN IMMEDIATE")
            self.project_tools._principal(db, owner)
            db.execute(
                "UPDATE copilot_approvals SET state='expired',revision=revision+1,preview_json='{}',"
                "decided_at=?,detail='This approval expired before a decision was made.' "
                "WHERE owner=? AND state='pending' AND expires_at<=?",
                (now, owner, now),
            )
            rows = db.execute(
                "SELECT a.*,s.title FROM copilot_approvals a JOIN copilot_sessions s ON s.id=a.session_id "
                "WHERE a.owner=? AND a.state='pending' ORDER BY a.created_at LIMIT 50", (owner,),
            ).fetchall()
        return {"approvals": [self._approval_public(row) for row in rows]}

    def _approval_state(self, owner, identity):
        now = time.time()
        with self.store.db() as db:
            self.project_tools._principal(db, owner)
            row = db.execute(
                "SELECT state,expires_at FROM copilot_approvals WHERE id=? AND owner=?", (identity, owner)
            ).fetchone()
            if not row:
                return "interrupted"
            if row["state"] == "pending" and row["expires_at"] <= now:
                db.execute(
                    "UPDATE copilot_approvals SET state='expired',revision=revision+1,preview_json='{}',"
                    "decided_at=?,detail='This approval expired before a decision was made.' WHERE id=?",
                    (now, identity),
                )
                return "expired"
            return row["state"]

    def decide_approval(self, owner, identity, action, payload):
        if not isinstance(identity, str) or not SESSION_ID.fullmatch(identity):
            raise ValueError("Choose a valid Copilot approval.")
        if action not in ("approve", "reject"):
            raise ValueError("Choose approve or reject.")
        expected_revision = payload.get("expected_revision")
        request_hash = payload.get("request_hash")
        if (type(expected_revision) is not int or expected_revision < 1
                or not isinstance(request_hash, str) or not re.fullmatch(r"[a-f0-9]{64}", request_hash)):
            raise ValueError("Refresh the approval before deciding.")
        now = time.time()
        with self.store.db() as db:
            db.execute("BEGIN IMMEDIATE")
            self.project_tools._principal(db, owner)
            row = db.execute(
                "SELECT a.*,s.title,s.state AS session_state FROM copilot_approvals a "
                "JOIN copilot_sessions s ON s.id=a.session_id WHERE a.id=? AND a.owner=?",
                (identity, owner),
            ).fetchone()
            if not row:
                raise ValueError("The Copilot approval was not found for this owner.")
            if (row["state"] != "pending" or row["revision"] != expected_revision
                    or row["request_hash"] != request_hash):
                raise ConflictError("This approval changed. Refresh the queue before deciding.")
            if row["expires_at"] <= now:
                db.execute(
                    "UPDATE copilot_approvals SET state='expired',revision=revision+1,preview_json='{}',"
                    "decided_at=?,detail='This approval expired before a decision was made.' WHERE id=?",
                    (now, identity),
                )
            elif action == "reject":
                db.execute(
                    "UPDATE copilot_approvals SET state='rejected',revision=revision+1,preview_json='{}',"
                    "decided_at=?,detail='The owner rejected this file change.' WHERE id=?", (now, identity),
                )
            elif row["session_state"] != "running":
                db.execute(
                    "UPDATE copilot_approvals SET state='interrupted',revision=revision+1,preview_json='{}',"
                    "decided_at=?,detail='This agent session stopped before the file change was approved.' "
                    "WHERE id=?", (now, identity),
                )
            else:
                preview = json.loads(row["preview_json"])
                try:
                    binding = {
                        "project": row["project"], "thread_id": row["thread_id"],
                        "workspace_revision": row["workspace_revision"],
                    }
                    workspace, files = self.project_tools._workspace(db, owner, binding, writing=True)
                    current = (
                        str(files.root) == row["root"]
                        and files.identity() == row["root_identity"]
                        and workspace["root"] == row["root"]
                        and self._preview_is_current(files, preview)
                    )
                except (ConflictError, OSError, PermissionError, ValueError):
                    current = False
                if not current:
                    db.execute(
                        "UPDATE copilot_approvals SET state='stale',revision=revision+1,preview_json='{}',"
                        "decided_at=?,detail='The project folder or one of its files changed. No write was approved.' "
                        "WHERE id=?", (now, identity),
                    )
                else:
                    db.execute(
                        "UPDATE copilot_approvals SET state='approved',revision=revision+1,preview_json='{}',"
                        "decided_at=?,detail='The owner approved this one-time file write.' WHERE id=?",
                        (now, identity),
                    )
            decided = db.execute(
                "SELECT a.*,s.title FROM copilot_approvals a JOIN copilot_sessions s ON s.id=a.session_id "
                "WHERE a.id=?", (identity,),
            ).fetchone()
        return {"approval": self._approval_public(decided)}

    def _settle_approvals(self, session_id, state, detail):
        if not isinstance(session_id, str) or not SESSION_ID.fullmatch(session_id):
            return
        with self.store.db() as db:
            db.execute(
                "UPDATE copilot_approvals SET state=?,revision=revision+1,preview_json='{}',"
                "decided_at=?,detail=? WHERE session_id=? AND state='pending'",
                (state, time.time(), detail, session_id),
            )

    def create(self, owner, payload):
        project = payload.get("project")
        model = payload.get("model")
        if model is None:
            model = getattr(self.brain, "copilot_model", self.brain.model)
        if not isinstance(model, str) or len(model) > MAX_MODEL_CHARS or not MODEL_ID.fullmatch(model):
            raise ValueError("Use a valid OpenRouter model ID.")
        title = payload.get("title", "New agent session")
        if not isinstance(title, str) or not title.strip() or len(title) > 120:
            raise ValueError("Enter a session title of at most 120 characters.")
        prompt = payload.get("prompt", "")
        if not isinstance(prompt, str) or not prompt.strip() or len(prompt) > MAX_PROMPT_CHARS:
            raise ValueError("Enter a first prompt of at most 24,000 characters.")
        project_lock = self._project_lock(project)
        if not project_lock.acquire(blocking=False):
            raise ConflictError("Another agent turn is active for this project. Try again when it finishes.")
        try:
            selected, workspace, files = self._workspace(owner, project, writing=True)
            identity = str(uuid.uuid4())
            now = time.time()
            with self.store.db() as db:
                db.execute(
                    "INSERT INTO copilot_sessions "
                    "(id,owner,project,thread_id,workspace_revision,root,root_identity,model,title,created_at,updated_at,state) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?,?,'running')",
                    (identity, owner, project, selected["thread_id"], workspace["revision"],
                     str(files.root), files.identity(), model, title.strip(), now, now),
                )
                db.execute(
                    "INSERT INTO copilot_messages(session_id,role,content,created_at) VALUES(?,?,?,?)",
                    (identity, "user", prompt.strip(), now),
                )
            try:
                result = self._run(owner, {
                    "operation": "create", "sessionId": identity, "project": project,
                    "root": str(files.root), "model": model, "prompt": prompt.strip(),
                })
            except (ConversationError, ValueError, OSError) as error:
                with self.store.db() as db:
                    db.execute(
                        "UPDATE copilot_sessions SET state='uncertain',last_error=?,updated_at=? WHERE id=?",
                        (str(error)[:1200], time.time(), identity),
                    )
                raise
            now = time.time()
            with self.store.db() as db:
                db.execute(
                    "INSERT INTO copilot_messages(session_id,role,content,created_at) VALUES(?,?,?,?)",
                    (identity, "assistant", result["content"], now),
                )
                db.execute(
                    "UPDATE copilot_sessions SET state='ready',last_error=NULL,updated_at=? WHERE id=?",
                    (now, identity),
                )
            return self.get(owner, identity)
        finally:
            project_lock.release()

    def list(self, owner, project=None):
        with self.store.db() as db:
            self.project_tools._principal(db, owner)
            query = (
                "SELECT s.id,s.project,s.model,s.title,s.created_at,s.updated_at,s.state,s.last_error,"
                "(SELECT content FROM copilot_messages m WHERE m.session_id=s.id "
                "ORDER BY m.id DESC LIMIT 1) AS last_message "
                "FROM copilot_sessions s WHERE s.owner=?"
            )
            values = [owner]
            if project:
                query += " AND s.project=?"
                values.append(project)
            query += " ORDER BY s.updated_at DESC LIMIT 100"
            rows = db.execute(query, values).fetchall()
        return {"sessions": [dict(row) for row in rows]}

    def get(self, owner, session_id):
        if not isinstance(session_id, str) or not SESSION_ID.fullmatch(session_id):
            raise ValueError("Choose a valid agent session.")
        with self.store.db() as db:
            self.project_tools._principal(db, owner)
            session = db.execute(
                "SELECT * FROM copilot_sessions WHERE id=? AND owner=?", (session_id, owner)
            ).fetchone()
            if not session:
                raise ValueError("The agent session was not found for this owner.")
            messages = db.execute(
                "SELECT role,content,created_at FROM copilot_messages "
                "WHERE session_id=? ORDER BY id", (session_id,),
            ).fetchall()
        return {"session": dict(session), "messages": [dict(row) for row in messages]}

    def _session_lock(self, session_id):
        with self.lock:
            return self.session_locks.setdefault(session_id, threading.Lock())

    def _project_lock(self, project):
        with self.lock:
            return self.project_locks.setdefault(project, threading.RLock())

    def send(self, owner, session_id, payload):
        current = self.get(owner, session_id)["session"]
        prompt = payload.get("text")
        if not isinstance(prompt, str) or not prompt.strip() or len(prompt) > MAX_PROMPT_CHARS:
            raise ValueError("Enter a prompt of at most 24,000 characters.")
        session_lock = self._session_lock(session_id)
        if not session_lock.acquire(blocking=False):
            raise ConflictError("This agent session is already handling a prompt.")
        project_lock = self._project_lock(current["project"])
        if not project_lock.acquire(blocking=False):
            session_lock.release()
            raise ConflictError("Another agent turn is active for this project. Try again when it finishes.")
        try:
            selected, workspace, files = self._workspace(
                owner, current["project"], current["thread_id"], current["workspace_revision"], writing=True,
            )
            if (str(files.root) != current["root"] or files.identity() != current["root_identity"]):
                raise ConflictError("The registered project folder changed. Create a new session for the current folder.")
            now = time.time()
            with self.store.db() as db:
                db.execute(
                    "INSERT INTO copilot_messages(session_id,role,content,created_at) VALUES(?,?,?,?)",
                    (session_id, "user", prompt.strip(), now),
                )
                db.execute(
                    "UPDATE copilot_sessions SET state='running',last_error=NULL,updated_at=? "
                    "WHERE id=? AND owner=?",
                    (now, session_id, owner),
                )
            try:
                result = self._run(owner, {
                    "operation": "send", "sessionId": session_id, "project": current["project"],
                    "root": str(files.root), "model": current["model"], "prompt": prompt.strip(),
                })
            except (ConversationError, ValueError, OSError) as error:
                with self.store.db() as db:
                    db.execute(
                        "UPDATE copilot_sessions SET state='uncertain',last_error=?,updated_at=? "
                        "WHERE id=? AND owner=?",
                        (str(error)[:1200], time.time(), session_id, owner),
                    )
                raise
            now = time.time()
            with self.store.db() as db:
                db.execute(
                    "INSERT INTO copilot_messages(session_id,role,content,created_at) VALUES(?,?,?,?)",
                    (session_id, "assistant", result["content"], now),
                )
                db.execute(
                    "UPDATE copilot_sessions SET state='ready',last_error=NULL,updated_at=? "
                    "WHERE id=? AND owner=?",
                    (now, session_id, owner),
                )
            return self.get(owner, session_id)
        finally:
            project_lock.release()
            session_lock.release()
