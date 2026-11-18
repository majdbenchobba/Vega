"""Vega-owned local n8n service and exact-review, inactive workflow publishing."""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import socket
import subprocess
import threading
import time
import urllib.error
import urllib.request
import uuid

from companion import ConflictError
from n8n_workflows import TEMPLATES, TRIGGERS, build_workflow, canonical, digest, validate_workflow, workflow_spec
from process_lifetime import spawn_owned_process
from project_requests import ProjectRequests
from relay import protect
from routing_targets import dispatch_target


ROOT = Path(__file__).resolve().parent
BASE_URL = "http://127.0.0.1:5678"


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *_args, **_kwargs):
        return None


class N8nError(ValueError):
    def __init__(self, message, *, status=None):
        super().__init__(message)
        self.status = status


class N8nService:
    def __init__(self, store, tasks, stop, *, root=ROOT, protect_fn=protect, request_fn=None, clock=time.time):
        self.store, self.tasks, self.stop = store, tasks, stop
        self.root, self.protect, self.clock = Path(root), protect_fn, clock
        self.request_fn = request_fn or self._http
        self.process = None
        self.lock = threading.RLock()
        self.state, self.error = "not_installed", None
        self.ready_at = None
        self.last_checked = None
        self.cache, self.cache_owner = None, None
        self.cache_at = 0
        self.runtime = self.root / ".n8n-runtime"
        self.data = store.directory / "n8n"
        with store.db() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS n8n_connections (
                    owner INTEGER PRIMARY KEY, credential BLOB NOT NULL, revision INTEGER NOT NULL,
                    verified_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS n8n_drafts (
                    id TEXT PRIMARY KEY, owner INTEGER NOT NULL, project TEXT NOT NULL, thread_id TEXT NOT NULL,
                    source TEXT NOT NULL, source_run TEXT UNIQUE, review_binding TEXT,
                    spec TEXT NOT NULL, workflow TEXT NOT NULL, review_hash TEXT NOT NULL,
                    state TEXT NOT NULL, remote_id TEXT, revision INTEGER NOT NULL DEFAULT 0,
                    created_at REAL NOT NULL, updated_at REAL NOT NULL, error TEXT
                );
            """)
            db.execute("UPDATE n8n_drafts SET state='uncertain',error=? WHERE state='publishing'",
                       ("Vega stopped before the n8n receipt was saved. Check the receipt before doing anything else.",))
        if (self.runtime / "installation-receipt.json").is_file():
            self.state = "starting"

    def _http(self, method, path, key=None, body=None):
        if not re.fullmatch(r"/(?:healthz(?:/readiness)?|api/v1/(?:workflows|executions)(?:\?[A-Za-z0-9=&%_-]+)?)", path):
            raise N8nError("Unsupported local n8n operation.")
        headers = {"Accept": "application/json"}
        if key:
            headers["X-N8N-API-KEY"] = key
        if body is not None:
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request(BASE_URL + path, headers=headers, method=method,
                                         data=None if body is None else canonical(body).encode())
        try:
            opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
            with opener.open(request, timeout=12) as response:
                if response.status not in (200, 201):
                    raise N8nError("n8n did not return a successful receipt.")
                data = response.read(1024 * 1024 + 1)
            if len(data) > 1024 * 1024:
                raise N8nError("The local n8n response exceeded the supported size.")
            result = json.loads(data)
            if not isinstance(result, dict):
                raise N8nError("n8n returned an unexpected response.")
            return result
        except urllib.error.HTTPError as error:
            if error.code in (401, 403):
                raise N8nError("n8n rejected the API key. Review its access and expiry in the local n8n settings.", status=error.code) from None
            raise N8nError(f"n8n returned HTTP {error.code}; no success receipt was recorded.", status=error.code) from None
        except (OSError, ValueError) as error:
            if isinstance(error, N8nError):
                raise
            raise N8nError("The local n8n service could not be reached or returned an unreadable response.") from None

    def _principal(self, db, owner):
        return ProjectRequests._principal(db, owner)

    def _credential(self, db, owner):
        self._principal(db, owner)
        row = db.execute("SELECT * FROM n8n_connections WHERE owner=?", (owner,)).fetchone()
        if not row:
            raise N8nError("Connect the local n8n API in Preferences first.")
        try:
            return self.protect(bytes(row["credential"]), decrypt=True).decode("utf-8"), row["revision"]
        except Exception:
            raise N8nError("Windows could not unlock the local n8n key. Reconnect it in Preferences.") from None

    def connect(self, owner, payload):
        if (not isinstance(payload, dict) or set(payload) != {"api_key"}
                or not isinstance(payload["api_key"], str) or not 20 <= len(payload["api_key"]) <= 4096
                or not re.fullmatch(r"[\x21-\x7e]+", payload["api_key"])):
            raise N8nError("Enter the API key from your local n8n settings.")
        with self.store.db() as db:
            principal = self._principal(db, owner)
        value = self.request_fn("GET", "/api/v1/workflows?limit=1", payload["api_key"])
        if not isinstance(value.get("data"), list):
            raise N8nError("n8n did not confirm workflow API access.")
        encrypted = self.protect(payload["api_key"].encode())
        with self.store.db() as db:
            db.execute("BEGIN IMMEDIATE")
            if self._principal(db, owner) != principal:
                raise ConflictError("The owner scope changed while connecting n8n.")
            db.execute("INSERT INTO n8n_connections VALUES(?,?,1,?) ON CONFLICT(owner) DO UPDATE SET "
                       "credential=excluded.credential,revision=n8n_connections.revision+1,verified_at=excluded.verified_at",
                       (owner, encrypted, self.clock()))
        self.cache = None
        return {"connected": True, "verified_at": self.clock(), "key_storage": "windows_dpapi"}

    def disconnect(self, owner):
        with self.store.db() as db:
            db.execute("BEGIN IMMEDIATE")
            self._principal(db, owner)
            db.execute("DELETE FROM n8n_connections WHERE owner=?", (owner,))
        self.cache = None
        return {"connected": False}

    def runtime_status(self):
        with self.lock:
            result = {"state": self.state, "error": self.error, "url": BASE_URL,
                      "managed": self.process is not None, "ready_at": self.ready_at,
                      "checked_at": self.last_checked, "loopback_only": True}
        try:
            receipt = json.loads((self.runtime / "installation-receipt.json").read_text("utf-8"))
            result.update(installed=True, version=receipt["n8n_version"], node_version=receipt["node_version"])
        except (OSError, ValueError, KeyError):
            result.update(installed=False, version=None)
        return result

    def _draft_scope(self, db, owner, row):
        self._principal(db, owner)
        if row["project"] not in self.tasks() or dispatch_target(self.tasks(), row["project"])["thread_id"] != row["thread_id"]:
            raise ConflictError("This workflow's registered project changed; create a new draft.")
        if row["source_run"]:
            run = db.execute("SELECT state,binding FROM team_runs WHERE owner=? AND id=?", (owner, row["source_run"])).fetchone()
            review = db.execute("SELECT review,review_binding FROM team_delegations WHERE run_id=?", (row["source_run"],)).fetchone()
            if (not run or run["state"] != "completed" or not review or review["review_binding"] != row["review_binding"]
                    or json.loads(review["review"] or "{}").get("status") != "accepted"):
                raise ConflictError("The source agent work no longer has Vega's accepted review.")
        workflow, spec = json.loads(row["workflow"]), json.loads(row["spec"])
        validate_workflow(row["project"], spec, workflow)
        if digest({"workflow": workflow, "project": row["project"], "thread_id": row["thread_id"],
                   "review_binding": row["review_binding"]}) != row["review_hash"]:
            raise ConflictError("The reviewed workflow changed.")
        return workflow

    def _public_draft(self, db, owner, row):
        valid = True
        try:
            self._draft_scope(db, owner, row)
        except (ValueError, PermissionError):
            valid = False
        connected = db.execute("SELECT 1 FROM n8n_connections WHERE owner=?", (owner,)).fetchone() is not None
        return {key: row[key] for key in ("id", "project", "source", "source_run", "review_binding", "review_hash",
                                         "state", "remote_id", "revision", "created_at", "updated_at", "error")} | {
            "spec": json.loads(row["spec"]), "workflow": json.loads(row["workflow"]),
            "scope_valid": valid, "can_publish": valid and connected and row["state"] == "draft",
            "activation": "inactive", "vega_reviewed": row["source"] == "agent_review"}

    def record_draft(self, db, owner, project, spec, *, source_run=None, review_binding=None):
        self._principal(db, owner)
        if project not in self.tasks():
            raise ValueError("Choose a registered project.")
        spec = workflow_spec(spec)
        if source_run:
            existing = db.execute("SELECT id FROM n8n_drafts WHERE owner=? AND source_run=?", (owner, source_run)).fetchone()
            if existing:
                return existing["id"]
        identity, now = str(uuid.uuid4()), self.clock()
        spec = {**spec, "name": spec["name"][:80] + " · Vega " + identity[:8]}
        workflow = build_workflow(project, spec)
        validate_workflow(project, spec, workflow)
        target = dispatch_target(self.tasks(), project)
        binding = digest({"workflow": workflow, "project": project, "thread_id": target["thread_id"],
                          "review_binding": review_binding})
        db.execute("INSERT INTO n8n_drafts(id,owner,project,thread_id,source,source_run,review_binding,spec,workflow,"
                   "review_hash,state,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,'draft',?,?)",
                   (identity, owner, project, target["thread_id"], "agent_review" if source_run else "owner_template",
                    source_run, review_binding, canonical(spec), canonical(workflow), binding, now, now))
        return identity

    def prepare(self, owner, payload):
        if not isinstance(payload, dict) or set(payload) != {"project", "spec"}:
            raise ValueError("Choose a project and workflow template.")
        with self.store.db() as db:
            db.execute("BEGIN IMMEDIATE")
            identity = self.record_draft(db, owner, payload["project"], payload["spec"])
            row = db.execute("SELECT * FROM n8n_drafts WHERE id=?", (identity,)).fetchone()
            return {"draft": self._public_draft(db, owner, row)}

    @staticmethod
    def _remote_workflow(row):
        return json.loads(row["workflow"])

    def publish(self, owner, identity, payload):
        if (not isinstance(payload, dict) or set(payload) != {"approved", "expected_revision", "review_hash"}
                or payload["approved"] is not True or type(payload["expected_revision"]) is not int):
            raise ValueError("Review the exact workflow and approve saving it inactive.")
        with self.store.db() as db:
            db.execute("BEGIN IMMEDIATE")
            self._principal(db, owner)
            row = db.execute("SELECT * FROM n8n_drafts WHERE owner=? AND id=?", (owner, identity)).fetchone()
            if not row:
                raise ValueError("This workflow draft is unavailable.")
            self._draft_scope(db, owner, row)
            if row["state"] in ("published_inactive", "uncertain"):
                return {"draft": self._public_draft(db, owner, row)}
            if row["state"] != "draft" or (row["revision"], row["review_hash"]) != (payload["expected_revision"], payload["review_hash"]):
                raise ConflictError("This workflow changed; refresh its review.")
            key, _ = self._credential(db, owner)
            db.execute("UPDATE n8n_drafts SET state='publishing',updated_at=? WHERE id=?", (self.clock(), identity))
            remote = self._remote_workflow(row)
        # One POST only. A timeout may have created the workflow; never replay it.
        try:
            result = self.request_fn("POST", "/api/v1/workflows", key, remote)
            if (not isinstance(result.get("id"), str) or not result["id"]
                    or result.get("active") is not False or result.get("name") != remote["name"]):
                raise N8nError("n8n did not confirm an inactive workflow; check the saved receipt.")
            with self.store.db() as db:
                db.execute("BEGIN IMMEDIATE")
                self._principal(db, owner)
                db.execute("UPDATE n8n_drafts SET state='published_inactive',remote_id=?,revision=revision+1,"
                           "updated_at=?,error=NULL WHERE owner=? AND id=? AND state='publishing'",
                           (result["id"], self.clock(), owner, identity))
        except Exception as error:
            refused = isinstance(error, N8nError) and error.status in (400, 401, 403, 404, 422)
            with self.store.db() as db:
                db.execute("UPDATE n8n_drafts SET state=?,revision=revision+1,updated_at=?,error=? "
                           "WHERE owner=? AND id=? AND state='publishing'",
                           ("draft" if refused else "uncertain", self.clock(),
                            f"n8n refused the save (HTTP {error.status}). Review the connection and approve again."
                            if refused else "Saving may have reached n8n. Check its receipt; Vega will not submit it again.",
                            owner, identity))
        self.cache = None
        with self.store.db() as db:
            self._principal(db, owner)
            row = db.execute("SELECT * FROM n8n_drafts WHERE owner=? AND id=?", (owner, identity)).fetchone()
            return {"draft": self._public_draft(db, owner, row)}

    def reconcile(self, owner, identity):
        with self.store.db() as db:
            self._principal(db, owner)
            row = db.execute("SELECT * FROM n8n_drafts WHERE owner=? AND id=?", (owner, identity)).fetchone()
            if not row or row["state"] != "uncertain":
                raise ValueError("Only an uncertain workflow receipt can be checked.")
            key, _ = self._credential(db, owner)
            expected = self._remote_workflow(row)
        response = self.request_fn("GET", "/api/v1/workflows?limit=100", key)
        matches = [item for item in response.get("data", []) if isinstance(item, dict)
                   and item.get("name") == expected["name"] and item.get("active") is False
                   and item.get("nodes") == expected["nodes"] and item.get("connections") == expected["connections"]]
        with self.store.db() as db:
            self._principal(db, owner)
            if len(matches) == 1 and isinstance(matches[0].get("id"), str):
                db.execute("UPDATE n8n_drafts SET state='published_inactive',remote_id=?,revision=revision+1,"
                           "updated_at=?,error=NULL WHERE owner=? AND id=? AND state='uncertain'",
                           (matches[0]["id"], self.clock(), owner, identity))
            row = db.execute("SELECT * FROM n8n_drafts WHERE owner=? AND id=?", (owner, identity)).fetchone()
            return {"draft": self._public_draft(db, owner, row)}

    def catalog(self, owner, *, refresh=False):
        with self.store.db() as db:
            self._principal(db, owner)
            connection = db.execute("SELECT revision,verified_at FROM n8n_connections WHERE owner=?", (owner,)).fetchone()
            rows = db.execute("SELECT * FROM n8n_drafts WHERE owner=? ORDER BY created_at DESC LIMIT 60", (owner,)).fetchall()
            drafts = [self._public_draft(db, owner, row) for row in rows]
            key, revision = self._credential(db, owner) if connection else (None, None)
        if key and (refresh or self.cache is None or self.cache_owner != (owner, revision) or self.clock() - self.cache_at > 20):
            cache = {"workflows": [], "executions": [], "error": None, "checked_at": self.clock()}
            try:
                workflows = self.request_fn("GET", "/api/v1/workflows?limit=100", key)
                cache["workflows"] = [{k: item.get(k) for k in ("id", "name", "active", "updatedAt")}
                                      for item in workflows["data"] if isinstance(item, dict)]
                cache["more_workflows"] = bool(workflows.get("nextCursor"))
                executions = self.request_fn("GET", "/api/v1/executions?limit=25&includeData=false", key)
                cache["executions"] = [{k: item.get(k) for k in ("id", "workflowId", "status", "startedAt", "stoppedAt")}
                                      for item in executions["data"] if isinstance(item, dict)]
            except (N8nError, KeyError, TypeError):
                cache["error"] = "n8n activity could not be refreshed. Check the local connection and API permissions."
            self.cache, self.cache_owner, self.cache_at = cache, (owner, revision), self.clock()
        with self.store.db() as db:
            self._principal(db, owner)
            latest = db.execute("SELECT revision FROM n8n_connections WHERE owner=?", (owner,)).fetchone()
            if connection and (not latest or latest["revision"] != revision):
                raise ConflictError("The n8n connection changed while refreshing.")
        return {"runtime": self.runtime_status(), "connected": connection is not None,
                "connection_verified_at": connection["verified_at"] if connection else None,
                "activity": self.cache if connection and self.cache_owner == (owner, revision) else None,
                "drafts": drafts, "templates": TEMPLATES, "triggers": list(TRIGGERS),
                "work_paused": bool(self.store.get("work_paused", False)), "key_storage": "windows_dpapi",
                "activation_supported": False}

    def local_overview(self, owner, *, project=None):
        """Do not make the map wait for n8n networking or send it workflow bodies."""
        with self.store.db() as db:
            self._principal(db, owner)
            connection = db.execute("SELECT revision FROM n8n_connections WHERE owner=?", (owner,)).fetchone()
            condition, values = ("owner=? AND project=?", (owner, project)) if project else ("owner=?", (owner,))
            drafts = [dict(row) for row in db.execute(
                "SELECT id,project,state,remote_id,source_run,updated_at FROM n8n_drafts WHERE "
                + condition + " ORDER BY created_at DESC LIMIT 60", values)]
        cache = self.cache if connection and self.cache_owner == (owner, connection["revision"]) else None
        if cache and project:
            identities = {draft["remote_id"] for draft in drafts if draft["remote_id"]}
            cache = {**cache, "workflows": [flow for flow in cache["workflows"] if flow["id"] in identities],
                     "executions": [execution for execution in cache["executions"] if execution["workflowId"] in identities]}
        return {"runtime": self.runtime_status(), "connected": connection is not None,
                "drafts": drafts, "activity": cache}

    def _environment(self, node):
        permitted = {"SYSTEMROOT", "WINDIR", "SYSTEMDRIVE", "COMSPEC", "TEMP", "TMP", "USERPROFILE",
                     "APPDATA", "LOCALAPPDATA", "PROCESSOR_ARCHITECTURE", "NUMBER_OF_PROCESSORS"}
        env = {key: value for key, value in os.environ.items() if key.upper() in permitted}
        env.update(PATH=str(node.parent) + os.pathsep + str(Path(os.environ.get("SYSTEMROOT", r"C:\Windows")) / "System32"),
                   NODE_OPTIONS="", N8N_USER_FOLDER=str(self.data), N8N_HOST="127.0.0.1",
                   N8N_LISTEN_ADDRESS="127.0.0.1", N8N_PORT="5678", N8N_PROTOCOL="http",
                   N8N_EDITOR_BASE_URL=BASE_URL, N8N_WEBHOOK_URL=BASE_URL + "/", N8N_SECURE_COOKIE="false",
                   N8N_DIAGNOSTICS_ENABLED="false", N8N_VERSION_NOTIFICATIONS_ENABLED="false",
                   N8N_TEMPLATES_ENABLED="false", N8N_COMMUNITY_PACKAGES_ENABLED="false",
                   N8N_PYTHON_ENABLED="false", N8N_RUNNERS_GRANT_TOKEN_TTL="300", N8N_RUNNERS_TASK_TIMEOUT="60",
                   N8N_BLOCK_ENV_ACCESS_IN_NODE="true", GENERIC_TIMEZONE="Africa/Tunis",
                   EXECUTIONS_DATA_MAX_AGE="168", EXECUTIONS_DATA_PRUNE="true")
        return env

    def _drain(self, process):
        # Keep a bounded local diagnostic log; never return process output in an API.
        log = self.data / "runtime.log"
        try:
            with log.open("w", encoding="utf-8") as stream:
                for line in process.stdout:
                    if stream.tell() > 256 * 1024:
                        stream.seek(0); stream.truncate()
                    stream.write(line); stream.flush()
        finally:
            process.stdout.close()

    def run(self):
        receipt = self.runtime / "installation-receipt.json"
        if not receipt.is_file():
            self.state = "not_installed"
            self.stop.wait()
            return
        manifest = json.loads((self.root / "n8n-runtime-manifest.json").read_text("utf-8"))
        node = self.runtime / manifest["node_archive_root"] / "node.exe"
        script = self.runtime / "app/node_modules/n8n/bin/n8n"
        self.data.mkdir(parents=True, exist_ok=True)
        try:
            with socket.socket() as check:
                if check.connect_ex(("127.0.0.1", 5678)) == 0:
                    self.state, self.error = "port_in_use", "Another process is using n8n's local port. Vega has not taken it over."
                    self.stop.wait()
                    return
            process = spawn_owned_process([str(node), str(script), "start"], env=self._environment(node))
            with self.lock:
                self.process, self.state, self.error = process, "starting", None
            reader = threading.Thread(target=self._drain, args=(process,), name="vega-n8n-output", daemon=True)
            reader.start()
            while not self.stop.is_set():
                if process.poll() is not None:
                    self.state, self.error = "stopped", "The local n8n process stopped. Its local log has the startup details."
                    break
                try:
                    health = self.request_fn("GET", "/healthz/readiness")
                    if health.get("status") == "ok":
                        self.state, self.error = "ready", None
                        self.ready_at = self.ready_at or self.clock()
                except N8nError:
                    if self.ready_at:
                        self.state, self.error = "unavailable", "n8n is running but its readiness check did not pass."
                self.last_checked = self.clock()
                if self.state == "ready":
                    owner = self.store.get("owner_id")
                    if owner:
                        try:
                            self.catalog(owner)
                        except (ValueError, PermissionError):
                            pass
                self.stop.wait(5)
            self.close()
            reader.join(timeout=2)
            self.stop.wait()
        except (OSError, ValueError, RuntimeError) as error:
            self.state, self.error = "unavailable", f"Local n8n startup needs attention ({type(error).__name__})."
            self.close()
            self.stop.wait()

    def close(self):
        with self.lock:
            process = self.process
            if not process:
                return
            if process.poll() is None:
                process.terminate()
            try:
                process.wait(timeout=5)
                process.release()
                self.process = None
            except subprocess.TimeoutExpired:
                self.error = "n8n is still stopping; the owned process will close with Vega."
