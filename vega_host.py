"""Standalone Vega host: Telegram, direct conversation and bounded Windows control."""
from __future__ import annotations

import argparse
import base64
import ctypes
import hashlib
import hmac
import json
import os
import mimetypes
from pathlib import Path
import re
import secrets
import threading
import time
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit, parse_qs, quote, unquote

from openrouter_conversation import (
    OpenRouterConversation, OmniRouteConversation, ConversationError,
)
from companion import Companion, ConflictError
from media_store import OwnerMedia
from memory_import import MemoryImports, FORMATS as MEMORY_IMPORT_FORMATS, MAX_UPLOAD as MEMORY_UPLOAD_LIMIT
from memory_policy import current_action_permissions, unquoted_text
from project_requests import ProjectRequests
from project_reports import ProjectReports
from codex_activity import CodexActivity
from codex_workspace import CodexWorkspace, DesktopUnavailable
from vscode_agents import VscodeAgentsWorkspace, VscodeAgentsUnavailable
from antigravity import AntigravityWorkspace, AntigravityUnavailable
from deepseek import DeepSeekWorkspace, DeepSeekUnavailable
from infrastructure import InfrastructureMap
from project_activity import ProjectActivity
from work_references import preview_reference
from photo_batches import classify_owner_text
from project_freshness import project_freshness
from project_tools import ProjectTools
from copilot_sessions import CopilotSessions
from relay import Relay, Store, STOP, TelegramError, load_tasks, redact
from speech_service import SpeechService, SpeechError, is_short_greeting
from speech_reply_policy import select_spoken_reply, explicit_read_aloud
from owner_voice import OwnerVoice
from windows_control import WindowsControl
from worker_supervisor import WorkerSupervisor
from telegram_miniapp import MiniAppManager, MINIAPP_VISUAL_ASSETS, MINIAPP_VISUAL_FILES
from routing_targets import dispatch_target, project_connection
from http_runtime import ExclusiveLoopbackServer, ReadTimings
import text_delivery
from team_service import TeamService
from team_commands import team_command
from routing_references import resolve_reference
from team_skills import SKILLS as TEAM_SKILLS
from work_activity import conversation_activity
from automatic_context import infer_context, work_request, activity_question
from owner_files import OwnerFiles
from local_attachments import LocalAttachments
from n8n_service import N8nService
from work_overview import overview as work_overview
from agent_skills import AGENTS as TEAM_AGENTS
from away_monitor import AwayMonitor
from artifact_delivery import ArtifactDelivery
from project_operations import ProjectOperations

ROOT = Path(__file__).resolve().parent
READ_TIMING_ROUTES = {
    "/": "http.page", "/api/status": "http.status", "/api/companion": "http.companion",
    "/api/messages": "http.messages", "/api/work-overview": "http.work",
    "/api/voice/config": "http.voice_config", "/api/voice/owner": "http.voice_owner",
    "/api/n8n": "http.n8n",
    "/api/team": "http.team",
}
STATUS_TIMINGS = (
    "status.registry", "status.relay", "status.settings", "status.pending", "status.receipts",
    "speech.route", "speech.owner", "status.voice_pending", "status.memory", "status.workers",
    "status.team", "status.project_tools", "status.miniapp", "status.n8n", "status.windows",
)
WORK_BUILD_FILES = (
    "infrastructure.py", "infrastructure_probe.py", "infrastructure_catalog.py", "companion.py", "public/local-projects.js",
    "public/infrastructure.js", "public/infrastructure.css", "public/infrastructure.html",
    "public/infrastructure-bootstrap.js", "public/infrastructure-model.js", "public/infrastructure-camera.js", "public/map-motion.js",
    *MINIAPP_VISUAL_FILES,
    "artifact_delivery.py", "project_operations.py", "media_store.py", "memory_policy.py", "owner_files.py",
    "work_references.py", "away_monitor.py", "project_milestones.py", "public/voice-local-buffer.js", "public/voice-session.js",
    "codex_activity.py", "codex_workspace.py", "desktop_attachments.py", "codex-desktop-bridge.mjs",
    "public/composer-attachments.js",
    "public/miniapp/workspace.js", "project_activity.py", "conversation.py", "openrouter_conversation.py",
    "routing_targets.py", "project_requests.py", "public/miniapp/app.js",
    "public/request-receipts.js", "public/miniapp/style.css",
    "relay.py", "telegram_miniapp.py", "windows_control.py", "speech_service.py", "http_runtime.py", "local_attachments.py",
    "public/vega-refresh.css", "public/miniapp/vega-icon.svg", "public/memory-panel.js",
    "agent_delegation.py","agent_skills.py","team_service.py","team_model.py","team_skills.py","project_checks.py",
    "project_tools.py","public/project-tools.js","public/project-tools.css",
    "copilot_sessions.py","copilot_sdk_runner.mjs","public/copilot-sessions.js","public/copilot-sessions.css",
    "public/native-ide-sessions.js",
    "vscode_agents.py","vscode_ahp_readonly.mjs",
    "antigravity.py", "antigravity-cdp.mjs", "codex_cli.py", "deepseek.py",
    "work_overview.py","n8n_service.py","n8n_workflows.py","vega_host.py",
    "public/work-overview.js","public/work-overview.css","public/n8n-panel.js","public/n8n-panel.css",
    "public/dashboard.js","public/dashboard.bundle.js","public/dashboard-loader.js","public/index.html","public/team-panel.js","public/team-panel.css",
    "public/work-console.html","public/work-bootstrap.js",
    "voice_diagnostics.py","inspect_owner_check.py","owner_voice.py","greeting_profile.py","speaker_worker.py","speaker-models.json",
    "speech-worker.mjs","port/src/main/vega/awsSpeech.ts",
    "public/speaker-profile.js","public/speaker-profile.css",
    "public/voice-microphone.js","public/microphone-preference.js",
    "public/speech-detector.js","public/speech-detector-worker.js","public/silero-stream.js",
    "public/voice-interruption.js",
    "public/vendor/silero/silero.onnx","public/vendor/onnxruntime/ort.wasm.min.mjs",
    "public/vendor/onnxruntime/ort-wasm-simd-threaded.mjs","public/vendor/onnxruntime/ort-wasm-simd-threaded.wasm",
)


def current_work_build():
    return hashlib.sha256(b"".join((ROOT/name).read_bytes() for name in WORK_BUILD_FILES)).hexdigest()


def make_brain(store, settings):
    """Select one explicit provider. No silent cross-provider fallback is added here."""
    provider = settings.get("provider")
    if provider == "openrouter":
        return OpenRouterConversation(store, settings)
    if provider == "omniroute":
        return OmniRouteConversation(store, settings)
    raise ConversationError("The configured conversation provider is not supported.")


def get_vega_workspace_root(settings):
    configured = settings.get("vega_workspace_root")
    if configured is None:
        local_app_data = os.environ.get("LOCALAPPDATA")
        base = Path(local_app_data) if local_app_data else Path.home() / ".local" / "share"
        return (base / "Vega" / "workspace").resolve()
    if not isinstance(configured, str) or not configured.strip():
        raise ValueError("vega_workspace_root must be an absolute path.")
    path = Path(configured).expanduser()
    if not path.is_absolute():
        raise ValueError("vega_workspace_root must be an absolute path.")
    return path.resolve()


class VegaRelay(Relay):
    def __init__(self, store, tasks, settings, *, brain=None, controls=None, speech=None, owner_voice=None):
        super().__init__(store, tasks)
        self.settings = settings
        self.work_build = current_work_build()
        self.read_timings = ReadTimings((*READ_TIMING_ROUTES.values(), "http.assets", *STATUS_TIMINGS))
        self.memory_release = bool(settings.get("selective_memory_release", False))
        self.separate_delivery = True
        self.admission_lock = threading.RLock()
        self.registry_mtime = 0
        self.supervisor = None
        self.away_monitor = AwayMonitor(
            store, self.milestones, reports_root=settings.get("away_reports_root", AwayMonitor.DEFAULT_REPORTS_ROOT)
        )
        self.brain = brain or make_brain(store, settings)
        self.controls = controls or WindowsControl(store, settings)
        self.companion = Companion(store, tasks)
        self.memory_imports = MemoryImports(self.companion)
        self.speech = speech or SpeechService(store)
        self.owner_voice = owner_voice if owner_voice is not None else OwnerVoice(store)
        self.speech.owner_voice = self.owner_voice
        self.media = OwnerMedia(store)
        with store.db() as db:
            db.execute(
                "CREATE TABLE IF NOT EXISTS chat_history "
                "(id INTEGER PRIMARY KEY,update_id INTEGER,source TEXT,role TEXT,text TEXT,created_at REAL)"
            )
            history_columns = {row["name"] for row in db.execute("PRAGMA table_info(chat_history)")}
            for name, declaration in (
                ("origin", "TEXT NOT NULL DEFAULT 'legacy_unverified'"),
                ("owner_id", "INTEGER"),
                ("spoken_text", "TEXT"),
                ("speak_requested", "INTEGER NOT NULL DEFAULT 0"),
                ("speaker_revision", "INTEGER"),
                ("parent_update_id", "INTEGER"),
            ):
                if name not in history_columns:
                    db.execute(f"ALTER TABLE chat_history ADD COLUMN {name} {declaration}")
            db.execute(
                "CREATE TABLE IF NOT EXISTS local_requests "
                "(request_id TEXT PRIMARY KEY,update_id INTEGER UNIQUE NOT NULL,text_hash TEXT NOT NULL)"
            )
            db.executescript("""
                CREATE TABLE IF NOT EXISTS voice_replies (
                    update_id INTEGER PRIMARY KEY, text TEXT NOT NULL, state TEXT NOT NULL,
                    created_at REAL NOT NULL, message_id INTEGER, error TEXT
                );
                CREATE TABLE IF NOT EXISTS inbox_context (
                    update_id INTEGER PRIMARY KEY, payload TEXT NOT NULL
                );
            """)
            db.execute("UPDATE voice_replies SET state='uncertain',error='Host stopped before a voice delivery receipt' WHERE state IN ('synthesizing','sending')")
            # A crash must not silently replay a possible app/system action.
            db.execute(
                "UPDATE inbox SET state='interrupted',receipt='Vega stopped during a live reply; not replayed' "
                "WHERE state IN ('responding','acting','photo_boundary')"
            )
        self.project_requests = ProjectRequests(store, lambda: self.tasks)
        self.project_reports = ProjectReports(store, lambda: self.tasks)
        self.codex_activity = CodexActivity(store, lambda: self.tasks)
        self.desktop_workspace = CodexWorkspace(store)
        self.vscode_agents_workspace = VscodeAgentsWorkspace(store)
        self.antigravity_workspace = AntigravityWorkspace(store)
        self.infrastructure = InfrastructureMap(self)
        self.project_activity = ProjectActivity(store, lambda: self.tasks)
        self.project_tools = ProjectTools(store, lambda: self.tasks,
                                         workspaces=settings.get("project_tool_workspaces"))
        self.deepseek_workspace = DeepSeekWorkspace(store, self.project_tools)
        self.copilot_sessions = CopilotSessions(store, self.project_tools, lambda: self.tasks, self.brain, ROOT)
        self.owner_files = OwnerFiles(store,lambda:self.tasks)
        self.attachments = LocalAttachments(store)
        self.artifacts = ArtifactDelivery(store, lambda: self.tasks, self.owner_files)
        self.operations = ProjectOperations(self)
        self.n8n = N8nService(store, lambda: self.tasks, STOP)
        self.team = TeamService(store,lambda:self.tasks,self.brain,self.project_tools,self.team_snapshot,
                                responses=self.project_responses,settings=settings,
                                files=self.owner_files,publish=self.publish_agent_result,
                                project_requests=self.project_requests,
                                automations=self.n8n,
                                voice_guard=lambda context,owner:self._current_voice_origin("speaker_matched",context,owner))
        self.team.codex_activity = self.codex_activity
        self.team.project_activity = self.project_activity
        self.miniapp = MiniAppManager(self, STOP)

    def team_snapshot(self,owner,project):
        view=self.project_views(owner,project)
        value=view["project"]
        result={key:value.get(key) for key in ("alias","label","status","checklist","freshness","progress_assessment")}
        if value.get("latest_reply"):
            result["latest_reply"]={**value["latest_reply"],"text":value["latest_reply"].get("text","")[:5000]}
        result["requests"]=[{"id":r["id"],"state":r["state"],"responses":r.get("responses",[])}
                            for r in view["requests"][:5]]
        result["model_policy"]={key:self.settings.get(key) for key in
                               ("model","provider","region","reasoning_effort","max_output_tokens","max_conversations_per_hour")}
        result["implemented_team_skills"]=TEAM_SKILLS
        return result

    def publish_agent_result(self,current,run):
        """Append each saved outcome once to the ordinary owner conversation."""
        state=current["state"]
        result=current.get("result") or {}
        if state=="awaiting_approval":
            lead="My specialists prepared" if current.get("agents") else "I prepared"
            body=(f"{lead} a change to {current['path']}.\n\n"
                  f"{result.get('summary','The proposed change is ready for review.')}\n\n"
                  "Review the exact diff in Vega work before applying it.")
            if (current.get("vega_review") or {}).get("status")=="accepted":
                body+="\n\nI reviewed both specialists' results and accepted this draft."
            spoken="The proposed change is ready for your review."
        elif state=="completed":
            body=result.get("summary") or "The requested review finished."
            if result.get("findings"):
                body+="\n\n"+"\n".join("• "+line for line in result["findings"])
            if result.get("implementation")=="applied_verified":
                body+="\n\nThe approved change was applied and its file hash verified."
                spoken="The approved change was applied and verified."
            elif result.get("implementation")=="workflow_draft_ready":
                body+="\n\nThe complete workflow is saved in Automations for your review. It has not been published or activated."
                spoken="The workflow draft is ready for your review."
            else:
                spoken="The review is ready. The findings are in our conversation."
        elif state in {"failed","cancelled","interrupted","expired","needs_revision","review_blocked"}:
            body=current.get("error") or "This work stopped before completion."
            review=current.get("vega_review") or {}
            if review.get("required_changes"):
                body+="\n\n"+"\n".join("• "+change for change in review["required_changes"])
            spoken="This task needs attention. I’ve shown the details in our conversation."
        else:
            return
        if current.get("agents"):
            body+="\n\nEach specialist's output and my review are saved under Vega work → Agents & skills."
        identity=-int.from_bytes(hashlib.sha256(
            f"vega-work:{run['id']}:{state}".encode()).digest()[:7],"big")
        with self.store.db() as db:
            db.execute("BEGIN IMMEDIATE")
            ProjectRequests._principal(db,run["owner"])
            origin=db.execute("SELECT state,message_id FROM inbox WHERE update_id=? AND owner_id=?",
                              (run["origin_update_id"],run["owner"])).fetchone()
            if not origin or origin["state"] in {"cancelled","interrupted","expired"}:
                return
            source="local" if origin["message_id"]==0 else "telegram"
            db.execute(
                "INSERT INTO chat_history(update_id,source,role,text,created_at,origin,owner_id,spoken_text,speak_requested,parent_update_id) "
                "SELECT ?,?,'assistant',?,?,'vega_result',?,?,0,? WHERE NOT EXISTS "
                "(SELECT 1 FROM chat_history WHERE update_id=? AND role='assistant')",
                (identity,source,body,time.time(),run["owner"],spoken,run["origin_update_id"],identity))

    def inbox_state_for(self, task, original_text):
        # Replying to a project notification supplies context, not an instruction to dispatch.
        if classify_owner_text(original_text) in ("owner_complete", "owner_wait", "revoked"):
            return "photo_boundary"
        return "conversation" if not original_text.startswith("/") else "queued"

    def acknowledge(self, update, task, text):
        message = update.get("message") or {}
        source = "local" if message.get("message_id") == 0 else "telegram"
        admitted = self.store.enqueue(
            f"ack:{update['update_id']}", task, text,
            state="local" if source == "local" else "queued",
        )
        if admitted and self._verified_owner(update):
            origin = self._input_context(update["update_id"]).get(
                "origin", "typed" if source == "local" else "telegram_text")
            self.save_history(update["update_id"], source, "user", message.get("text") or "[Attachment]", origin=origin)
            self.save_history(update["update_id"], source, "assistant", text)
        return admitted

    def _input_context(self, update_id):
        with self.store.db() as db:
            row = db.execute("SELECT payload FROM inbox_context WHERE update_id=?", (update_id,)).fetchone()
        return json.loads(row["payload"]) if row else {}

    def _current_voice_origin(self, origin, context, owner):
        if owner != self.store.get("owner_id"):
            return False
        if origin != "speaker_matched":
            return True
        service = getattr(self, "owner_voice", None)
        return bool(service and service.automatic_allowed()
                    and service.revision() == context.get("speaker_revision"))

    def close(self):
        try:
            self.owner_voice.close()
        finally:
            self.speech.worker.close()

    def speech_status(self):
        return {**self.read_timings.call("speech.route", self.speech.status),
                "owner_voice": self.read_timings.call("speech.owner", self.owner_voice.status),
                "owner_voice_required": True}

    @staticmethod
    def _voice_pcm(payload):
        encoded = payload.get("audio")
        if not isinstance(encoded, str) or len(encoded) > 30 * 16000 * 2 * 4 // 3 + 8:
            raise ValueError("Use a local voice clip of at most 30 seconds.")
        try:
            pcm = base64.b64decode(encoded, validate=True)
        except ValueError:
            raise ValueError("The recorded audio is invalid.") from None
        if (payload.get("sample_rate") != 16000 or type(payload.get("sample_rate")) is not int
                or not pcm or len(pcm) % 2 or len(pcm) > 30 * 16000 * 2):
            raise ValueError("Use mono 16-bit audio at 16 kHz.")
        return pcm

    def owner_voice_action(self, action, payload):
        voice = self.owner_voice
        expected = payload.get("expected_revision")
        if action.startswith("diagnostic/"):
            owner=self.store.get("owner_id")
            if action=="diagnostic/arm":
                diagnostic=voice.diagnostics.arm(owner,payload)
            elif action=="diagnostic/clear":
                diagnostic=voice.diagnostics.clear(owner,payload.get("id"))
            elif action=="diagnostic/status":
                diagnostic=voice.diagnostics.status(owner)
            else:raise ValueError("Unknown diagnostic operation.")
            return {"status":voice.status(),"diagnostic":diagnostic}
        if action == "check":
            return voice.verify_utterance(self._voice_pcm(payload), expected_revision=expected)
        if action == "enroll/sample":
            return voice.enroll_sample(payload.get("enrollment_id"), payload.get("sample_key"),
                                       self._voice_pcm(payload), payload.get("sample_rate"))
        if action == "greeting/sample":
            return voice.greetings.sample(payload.get("enrollment_id"), payload.get("sample_key"),
                                          self._voice_pcm(payload), payload.get("sample_rate"))
        if action == "greeting/probe":
            return voice.greetings.probe(payload.get("enrollment_id"), payload.get("sample_key"),
                                         self._voice_pcm(payload), payload.get("sample_rate"))
        if action == "test/finish":
            return voice.test_finish(payload.get("test_id"), payload.get("sample_key"),
                                     self._voice_pcm(payload), payload.get("sample_rate"))
        if action == "playback-sample":
            with self.store.db() as db:
                self.project_requests._principal(db, self.store.get("owner_id"))
            if payload.get("consent_to_cloud_speech") is not True:
                raise ValueError("Generating the sample uses the configured AWS speech service and its usual charges. Explicit consent is required.")
            if type(expected) is not int or expected != voice.revision():
                raise ConflictError("Your voice setup changed. Refresh before generating a sample.")
            audio = self.speech.synthesize(
                "This is Vega speaking. Stay quiet while this sample plays, so the local voice check can test whether my playback is ignored. "
                "Voice matching is a convenience and does not replace your action confirmations.",
                source="owner-requested-playback-check",
            )
            return {"status": voice.status(), "audio": base64.b64encode(audio).decode(), "mime": "audio/mpeg", "autoplay": False}
        with self.admission_lock:
            if action == "greeting/start":
                return voice.greetings.start(payload.get("consent"), expected, payload.get("consent_version"))
            if action == "greeting/use-for-voice":
                return voice.greetings.use_for_voice(payload.get("consent"), expected,
                                                     payload.get("consent_version"))
            if action == "greeting/commit":
                return voice.greetings.commit(payload.get("enrollment_id"), expected)
            if action == "greeting/cancel":
                return voice.greetings.cancel(payload.get("enrollment_id"))
            if action == "enroll/start":
                return voice.enroll_start(payload.get("consent"), expected)
            if action == "enroll/commit":
                return voice.enroll_commit(payload.get("enrollment_id"), expected)
            if action == "enroll/cancel":
                return voice.enroll_cancel(payload.get("enrollment_id"))
            if action == "test/start":
                return voice.test_start(payload.get("case"), expected, payload.get("input_mode", "standard"))
            if action == "test/cancel":
                return voice.test_cancel(payload.get("test_id"))
            if action == "enable":
                return voice.enable(expected, payload.get("consent"),
                                    defer_other_check=payload.get("defer_other_check", False))
            if action == "disable":
                return voice.disable(expected)
            if action == "delete":
                return voice.delete(expected, payload.get("confirm"))
        raise ValueError("Unknown owner voice operation.")

    def refresh_registry(self):
        with self.registry_lock:
            return self._refresh_registry()

    def _refresh_registry(self):
        if not self.settings.get("reload_project_registry", False):
            return
        path = ROOT / "tasks.json"
        stamp = path.stat().st_mtime_ns
        if stamp != self.registry_mtime:
            try:
                candidate = load_tasks(path)
            except (OSError, ValueError, KeyError, TypeError):
                self.store.set("registry_error", "The project registry update is invalid; the last verified routing map is still in use.")
                return
            changed = [alias for alias, info in candidate.items() if alias in self.tasks and info["thread_id"] != self.tasks[alias]["thread_id"]]
            if changed:
                with self.store.db() as db:
                    db.executemany("DELETE FROM task_states WHERE task=?", [(alias,) for alias in changed])
            self.tasks = candidate
            self.companion.tasks = self.tasks
            self.registry_mtime = stamp
            self.store.set("registry_error", None)

    def report_status(self, task=None):
        self.refresh_registry()
        return super().report_status(task)

    def history(self, limit=80, *, before_update=None):
        with self.store.db() as db:
            if before_update is None:
                rows = db.execute("SELECT * FROM chat_history ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
            else:
                rows = db.execute(
                    "WITH prior AS (SELECT update_id FROM chat_history WHERE role='user' AND id < "
                    "(SELECT min(id) FROM chat_history WHERE update_id=? AND role='user')) "
                    "SELECT * FROM chat_history WHERE update_id IN prior OR parent_update_id IN prior "
                    "ORDER BY id DESC LIMIT ?",
                    (before_update, limit),
                ).fetchall()
        return [dict(row) for row in reversed(rows)]

    def save_history(self, update_id, source, role, text, *, origin="legacy_unverified",
                     spoken_text=None, speak_requested=False, speaker_revision=None):
        if not isinstance(text, str) or len(text.encode("utf-8")) > text_delivery.MAX_TEXT_BYTES:
            raise ValueError("This reply exceeds the bounded conversation size; it was not silently shortened.")
        if role == "assistant" and spoken_text is None:
            spoken_text = select_spoken_reply(text)
        with self.store.db() as db:
            db.execute(
                "INSERT INTO chat_history(update_id,source,role,text,created_at,origin,owner_id,spoken_text,speak_requested,speaker_revision) "
                "SELECT ?,?,?,?,?,?,?,?,?,? WHERE NOT EXISTS "
                "(SELECT 1 FROM chat_history WHERE update_id=? AND role=?)",
                (update_id, source, role, text, time.time(), origin,
                 self.store.get("owner_id"), spoken_text, int(speak_requested), speaker_revision, update_id, role),
            )

    def history_messages(self):
        # One snapshot keeps pairing, history and attachment metadata consistent
        # and avoids reopening the database for every user message on each poll.
        with self.store.db() as db:
            db.execute("BEGIN")
            rows=db.execute("SELECT * FROM chat_history ORDER BY id DESC LIMIT 80").fetchall()
            if not rows:return []
            owner_row=db.execute("SELECT value FROM settings WHERE key='owner_id'").fetchone()
            owner=json.loads(owner_row["value"]) if owner_row else None
            attachments=self.attachments.for_messages(
                db,owner,[row["update_id"] for row in rows if row["role"]=="user"])
        messages=[]
        for row in reversed(rows):
            item=dict(row)
            files=attachments.get(item["update_id"],[]) if item["role"]=="user" else []
            messages.append({**item,"update_id":str(item["update_id"]),"attachments":files,
                             "text":"" if files and item["text"]=="[Attachments]" else item["text"]})
        return messages

    def routing_receipts(self, limit=8):
        with self.store.db() as db:
            rows = db.execute(
                "SELECT update_id,task,state,receipt,received_at,handled_at FROM inbox "
                "WHERE state IN ('queued','routing','handled') ORDER BY received_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [
            {**dict(row), "update_id": str(row["update_id"]), "label": self.label(row["task"])}
            for row in rows
        ]

    def project_views(self, owner, alias=None):
        self.refresh_registry()
        if alias is not None and alias not in self.tasks:
            raise ValueError("This registered project is unavailable.")
        with self.store.db() as db:
            self.project_requests._principal(db, owner)
            states = {row["task"]: dict(row) for row in db.execute("SELECT * FROM task_states")}
            reports = {row["task"]: dict(row) for row in db.execute(
                "SELECT * FROM project_checklist_reports WHERE owner=?", (owner,))}
            checks = {row["task"]: dict(row) for row in db.execute(
                "SELECT * FROM project_source_checks WHERE owner=?", (owner,))}
        try:
            assessment = self.project_reports.progress_assessments(owner)
        except (PermissionError, ValueError, OSError, RuntimeError):
            assessment = {"projects": {}, "source_error": "The registered progress assessment is unavailable in this scope."}
        try:
            replies = self.project_reports.cached_replies(owner)
        except (PermissionError, ValueError, OSError, RuntimeError):
            replies = {}
        source_error = self.store.get("watcher_error") or self.store.get("registry_error")
        now = time.time()
        codex = self.codex_activity.view(owner)
        chat_activity = self.project_activity.views(owner)
        projects = []
        for key, info in self.tasks.items():
            if alias is not None and key != alias:
                continue
            observed = states.get(key)
            report = reports.get(key)
            if report and report["thread_id"] != info["thread_id"]:
                report = None
            checked = checks.get(key)
            if checked and (not report or checked["thread_id"] != report["thread_id"]
                            or checked["evidence_at"] != report["observed_at"]):
                checked = None
            projects.append({
                "alias": key, "thread_id": dispatch_target(self.tasks, key)["thread_id"], "label": info["label"],
                "watch_thread_id": info["thread_id"], "routing_owner": dispatch_target(self.tasks, key)["label"],
                "connection": project_connection(self.tasks, key, now=now),
                "codex_activity": codex if codex and codex.get("project") == key else None,
                "chat_activity": chat_activity.get(key),
                "status": observed["status"] if observed else report["status"] if report else info.get("initial_status", "not observed"),
                "observed_at": observed["observed_at"] if observed else report["observed_at"] if report else None,
                "checklist": json.loads(report["checklist"]) if report else
                    {section: [] for section in ("done", "working", "next", "blockers", "suggestions")},
                "checklist_observed_at": report["observed_at"] if report else None,
                "checklist_source": report["source"] if report else "not_reported",
                "evidence": json.loads(report["evidence"]) if report else [],
                "freshness": project_freshness(
                    report["observed_at"] if report else None,
                    checked_at=checked["checked_at"] if checked else None,
                    error=source_error or (checked["error"] if checked else None), now=now,
                ),
                "progress_assessment": assessment["projects"].get(key),
                "progress_assessment_error": assessment.get("source_error") if key not in assessment["projects"] else None,
                "progress_source_error": assessment.get("source_errors", {}).get(key),
                "latest_reply": replies.get(key) if alias is not None else
                    ({field: value for field, value in replies[key].items() if field != "text"} if key in replies else None),
            })
        if alias is not None:
            return {"project": projects[0], "requests": self.project_requests.list(owner, alias),
                    "snapshot_at": now, "freshness_version": 1}
        return {
            "projects": projects, "requests": self.project_requests.list(owner),
            "work_paused": bool(self.store.get("work_paused", False)), "coordinator_interval_minutes": 10,
            "snapshot_at": now, "freshness_version": 1,
            "progress_assessment_error": assessment.get("source_error"),
        }

    def project_read_capabilities(self, owner):
        try:
            return self.project_reports.capabilities(owner)
        except (PermissionError, ValueError, RuntimeError, OSError):
            return {"version": 1, "available": False, "reports": [], "reply_projects": [],
                    "source_error": "The registered project read scope is currently unavailable.",
                    "authority": False}

    def status(self):
        measure = self.read_timings.call
        measure("status.registry", self.refresh_registry)
        status = measure("status.relay", super().status)
        saved = measure("status.settings", self.store.get_many, (
            "conversation_error", "delivery_error", "report_response_error", "photo_batch_error",
            "team_error", "document_error", "last_delivery_tick", "last_conversation_tick",
            "last_model_reply_at", "last_model_latency_seconds", "memory_error",
        ))
        with self.read_timings.measure("status.pending"), self.store.db() as db:
            status["live_replies_pending"] = db.execute(
                "SELECT count(*) FROM inbox WHERE state IN ('conversation','responding','acting')"
            ).fetchone()[0]
        status.update({
            "host": "standalone-vega",
            "conversation_model": self.settings["model"],
            **{key: saved.get(key) for key in (
                "conversation_error", "delivery_error", "report_response_error", "photo_batch_error",
                "team_error", "document_error", "last_delivery_tick", "last_conversation_tick",
                "last_model_reply_at", "last_model_latency_seconds",
            )},
            "routing_receipts": measure("status.receipts", self.routing_receipts),
            "speech": self.speech_status(),
            "voice_notes_pending": measure("status.voice_pending", self.voice_notes_pending),
            "delivery_health": "needs-attention" if (
                saved.get("delivery_error")
                or time.time() - saved.get("last_delivery_tick", 0) > 45
            ) else "ready",
            "workers": measure("status.workers", self.supervisor.snapshot) if self.supervisor else {},
            "selective_memory_available": self.memory_release,
            "memory_error": saved.get("memory_error"),
            "memory_revision": measure("status.memory", self.companion.memory.version) if self.memory_release else None,
            "project_requests_available": True,
            "project_report_reads_version": 2,
            "telegram_project_operations_version": 1,
            "conversation_context_version": 2,
            "project_claim_evidence_version": 1,
            "telegram_photo_inputs_version": 1,
            "requested_artifact_delivery_version": 1,
            "miniapp_project_steering_version": 1,
            "miniapp_reference_previews_version": 1,
            "miniapp_desktop_workspace_version": 1,
            "project_progress_version": 1,
            "requested_files_version": 1,
            "photo_batches_version": 1,
            "team_version": 3,
            "automatic_context_version": 2,
            "conversation_agents_version": 1,
            "work_activity_version": 1,
            "agent_delegation_version": 1,
            "work_overview_version": 1,
            "infrastructure_version": 1,
            "infrastructure_probe_active": self.infrastructure.active,
            "n8n_integration_version": 1,
            "work_build": self.work_build,
            "local_files_version": 1,
            "local_attachments_version": 1,
            "speaker_decoding_version": 3,
            "voice_diagnostics_version": 2,
            "voice_feedback_version": 2,
            "voice_echo_review_version": 1,
            "team_active_jobs": measure("status.team", self.team.active),
            "telegram_text_delivery_version": 1,
            "project_data_freshness_version": 1,
            "development_routing_version": 2,
            "owner_voice_version": 1,
            "separate_spoken_replies": True,
            "project_tools_available": True,
            "copilot_sessions_version": 1,
            "project_tool_guards": {
                "version": 2,
                "atomic_model_admission": True,
                "directory_handles": "read_access_and_handle_identity",
            },
            "project_tool_model_jobs": measure("status.project_tools", self.project_tools.active_provider_calls),
            "miniapp": measure("status.miniapp", self.miniapp.status),
            "n8n": measure("status.n8n", self.n8n.runtime_status),
            "windows": measure("status.windows", self.controls.status),
        })
        return status

    def recover_worker(self, name):
        with self.store.db() as db:
            if name == "team":
                db.execute("UPDATE team_runs SET state='interrupted',error=?,updated_at=? WHERE state IN "
                           "('planning','reading','delegating','preparing','reviewing','verifying')",
                           ("The team worker stopped. Its work was not replayed.",time.time()))
                db.execute("UPDATE team_nodes SET state='interrupted',finished_at=? WHERE state='running'",(time.time(),))
                db.execute("UPDATE team_agent_tasks SET state='interrupted',finished_at=? WHERE state IN ('queued','running')",(time.time(),))
            elif name == "conversation":
                db.execute(
                    "UPDATE inbox SET state='interrupted',receipt='The conversation worker stopped before its result was recorded; not replayed',handled_at=? "
                    "WHERE state IN ('responding','acting')", (time.time(),),
                )
            elif name == "delivery":
                text_delivery.recover_interrupted(db, "Delivery worker stopped before a text receipt was recorded")
                db.execute("UPDATE outbox SET state='uncertain',error='Delivery worker stopped before a receipt was recorded' WHERE state='sending' AND event_id NOT IN (SELECT event_id FROM outbound_documents)")
            elif name == "documents":
                db.execute("UPDATE outbox SET state='uncertain',error='Document worker stopped before a receipt was recorded' WHERE state='sending' AND event_id IN (SELECT event_id FROM outbound_documents)")
            elif name == "voice-input":
                db.execute("UPDATE voice_inbox SET state='interrupted',error='Voice worker stopped; transcription was not replayed' WHERE state='transcribing'")
            elif name == "voice-output":
                db.execute("UPDATE voice_replies SET state='uncertain',error='Speech worker stopped before a receipt was recorded' WHERE state IN ('synthesizing','sending')")

    def voice_notes_pending(self):
        with self.store.db() as db:
            return db.execute("SELECT count(*) FROM voice_inbox WHERE state IN ('received','transcribing')").fetchone()[0]

    def _verified_owner(self, update, *, allow_forwarded_data=False):
        message = update.get("message") or {}
        sender, chat = message.get("from") or {}, message.get("chat") or {}
        return (
            isinstance(update.get("update_id"), int)
            and self.store.get("owner_id") is not None
            and sender.get("id") == self.store.get("owner_id")
            and chat.get("id") == sender.get("id")
            and chat.get("type") == "private" and not sender.get("is_bot")
            and (allow_forwarded_data or not any(k in message for k in ("forward_origin", "forward_date", "via_bot")))
        )

    def execute_host_command(self, text, owner):
        parts = text.strip().split(maxsplit=1)
        command = parts[0].split("@")[0].lower() if parts else ""
        argument = parts[1].strip() if len(parts) > 1 else ""
        if command == "/apps":
            return "Available apps:\n" + "\n".join(f"{key} — {value['label']}" for key, value in self.controls.apps.items())
        if command == "/open":
            return self.controls.open_app(argument.lower())
        if command == "/reboot":
            return self.controls.request_reboot(owner)
        if command == "/confirm_reboot":
            return self.controls.confirm_reboot(owner, argument)
        if command == "/cancel_reboot":
            return self.controls.cancel_reboot(owner)
        if command == "/watchdog":
            if argument.lower() in ("on", "off"):
                self.store.set("watchdog_enabled", argument.lower() == "on")
            elif argument.lower() not in ("", "status"):
                return "Use /watchdog on, /watchdog off, or /watchdog status."
            status = self.controls.status()
            return f"Codex recovery is {'enabled' if status['watchdog_enabled'] else 'paused'}. Codex is {status['codex']['state']}."
        if command == "/host":
            status = self.controls.status()
            return (
                f"Vega is running independently on this laptop.\nCodex: {status['codex']['state']}.\n"
                f"Codex recovery: {'enabled' if status['watchdog_enabled'] else 'paused'}.\n"
                f"Conversation model: {self.settings['model']}.\n"
                f"Voice: {'configured' if self.speech.status()['configured'] else 'needs setup'}.\n"
                "Vega Main owns this companion and its feature migration."
            )
        if command in ("/help", "/start"):
            return (
                "Write normally for a direct Vega conversation.\n"
                "/status — projects\n/projects — routing names\n"
                "/tell vega your instruction — explicitly queue Vega development\n"
                "You can also ask naturally to send work to a named project.\n"
                "Send a voice note for a transcribed conversation and spoken reply.\n"
                "Telegram bots cannot start private calls.\n"
                "/apps and /open codex — application control\n"
                "/watchdog on|off|status — Codex recovery\n"
                "/reboot — request a reboot confirmation\n/cancel_reboot — cancel it\n"
                "/pause_work and /resume_work — project continuation\n"
                "/mute and /unmute — verified milestone notices\n\n"
                "Conversation and local controls work without Codex. Delegated project work "
                "still depends on its coordinator being available. Vega development belongs "
                "to Vega Main. Photos and videos are retained for explicit project handoff; "
                "a saved reference is not a delivery receipt."
            )
        return None

    def handle_update(self, update):
        self.refresh_registry()
        message = update.get("message") or {}
        if any(message.get(key) for key in ("photo", "video", "document")) and self._verified_owner(update, allow_forwarded_data=True):
            try:
                accepted, kind = self.media.accept(update)
            except ValueError:
                return False
            if accepted:
                managed = False
                if kind == "photo":
                    try:
                        batch = self.photo_batches.accept(self.store.get("owner_id"), update["update_id"])
                        managed = batch["managed"]
                        if batch.get("reason"):
                            self.store.set("photo_batch_error", batch["reason"])
                        elif batch.get("needs_followup"):
                            self.store.set("photo_batch_error", "A late photo is retained in its original batch; the coordinator should read the current batch items.")
                    except (ValueError, PermissionError, OSError):
                        managed = True
                        self.store.set("photo_batch_error", "Photo grouping needs attention; the original photo is retained.")
                if not managed:
                    if kind == "photo":
                        caption = message.get("caption") if isinstance(message.get("caption"), str) else ""
                        with self.store.db() as db:
                            db.execute("INSERT OR IGNORE INTO inbox(update_id,message_id,owner_id,task,text,received_at,state) "
                                       "VALUES(?,?,?,'vega',?,?,'conversation')",
                                       (update["update_id"], message["message_id"], self.store.get("owner_id"),
                                        caption[:4000] or "[Photo received]", time.time()))
                            db.execute("INSERT OR IGNORE INTO inbox_context VALUES(?,?)", (update["update_id"], json.dumps({
                                "origin": "telegram_media_caption", "telegram_photo_update_id": update["update_id"],
                                "forwarded_data": any(key in message for key in ("forward_origin", "forward_date", "via_bot")),
                            })))
                    else:
                        self.store.enqueue(f"media-received:{update['update_id']}", "vega",
                                           f"{kind.capitalize()} reference saved. Its contents have not been read.")
                if managed or kind != "photo":
                    self.save_history(update["update_id"], "telegram_media", "user", f"[{kind.capitalize()} received]" + (": " + message["caption"][:4000] if isinstance(message.get("caption"), str) else ""))
            return accepted
        if message.get("voice") and self._verified_owner(update):
            return self.accept_voice_note(update)
        text = message.get("text", "")
        command = text.strip().split(maxsplit=1)[0].split("@")[0].lower() if isinstance(text, str) and text.strip() else ""
        if command in ("/report","/team") and self._verified_owner(update):
            source = "local" if message.get("message_id") == 0 else "telegram"
            context = self.notification_context(message)
            context["origin"] = "typed" if source == "local" else "telegram_text"
            with self.store.db() as db:
                db.execute("BEGIN IMMEDIATE")
                inserted = db.execute(
                    "INSERT OR IGNORE INTO inbox(update_id,message_id,owner_id,task,text,received_at,state) "
                    "VALUES(?,?,?,'coordinator',?,?,'conversation')",
                    (update["update_id"], message.get("message_id", 0), self.store.get("owner_id"), text, time.time()),
                ).rowcount
                if inserted:
                    db.execute("INSERT OR IGNORE INTO inbox_context VALUES(?,?)", (update["update_id"], json.dumps(context)))
            return inserted == 1
        host_commands = {"/apps", "/open", "/reboot", "/confirm_reboot", "/cancel_reboot", "/watchdog", "/host", "/help", "/start"}
        if command not in host_commands or not self._verified_owner(update):
            if self._verified_owner(update) and isinstance(text, str) and text.strip():
                context = self.notification_context(message)
                context["origin"] = "typed" if message.get("message_id") == 0 else "telegram_text"
                reply_id = (message.get("reply_to_message") or {}).get("message_id")
                if type(reply_id) is int:
                    context["owner_media_reply_message_id"] = reply_id
                with self.store.db() as db:
                    db.execute("INSERT OR IGNORE INTO inbox_context VALUES(?,?)", (update["update_id"], json.dumps(context)))
            admitted = super().handle_update(update)
            if admitted and self._verified_owner(update) and isinstance(text, str):
                source = "local" if message.get("message_id") == 0 else "telegram"
                origin = self._input_context(update["update_id"]).get("origin", "legacy_unverified")
                if origin != "speaker_matched":
                    self.save_history(update["update_id"], source, "user", text, origin=origin)
                if source == "telegram" and origin == "telegram_text":
                    try:
                        closure = self.photo_batches.close_from_owner_text(self.store.get("owner_id"), update["update_id"])
                        if closure.get("state") == "owner_complete" and closure.get("batch_ids"):
                            self.photo_batches.flush_ready()
                            with self.store.db() as db:
                                db.execute("UPDATE inbox SET state='conversation_done',receipt=?,handled_at=? "
                                           "WHERE update_id=? AND state='photo_boundary'",
                                           ("Photo batch closed; its grouped receipt is queued.", time.time(), update["update_id"]))
                        elif closure.get("state") == "revoked":
                            with self.store.db() as db:
                                db.execute("UPDATE inbox SET state='conversation_done',receipt=?,handled_at=? "
                                           "WHERE update_id=? AND state='photo_boundary'",
                                           ("Automatic photo batching and routing stopped.", time.time(), update["update_id"]))
                            self.acknowledge(update, "coordinator", "Automatic photo batching and routing stopped.")
                        elif closure.get("state") == "owner_wait" and closure.get("batch_ids"):
                            with self.store.db() as db:
                                db.execute("UPDATE inbox SET state='conversation_done',receipt=?,handled_at=? "
                                           "WHERE update_id=? AND state='photo_boundary'",
                                           ("Waiting for explicit completion of the photo batch.", time.time(), update["update_id"]))
                            self.acknowledge(update, "coordinator", "I’ll wait for your “done” before routing this photo batch.")
                    except (ValueError, PermissionError, OSError):
                        pass
                    finally:
                        with self.store.db() as db:
                            db.execute("UPDATE inbox SET state='conversation' WHERE update_id=? AND state='photo_boundary'",
                                       (update["update_id"],))
                else:
                    with self.store.db() as db:
                        db.execute("UPDATE inbox SET state='conversation' WHERE update_id=? AND state='photo_boundary'",
                                   (update["update_id"],))
            return admitted
        update_id = update["update_id"]
        event_id = f"ack:{update_id}"
        with self.store.db() as db:
            db.execute("BEGIN IMMEDIATE")
            if db.execute("SELECT 1 FROM outbox WHERE event_id=?", (event_id,)).fetchone():
                return False
            # Admission receipt precedes an action; a replay cannot run it twice.
            db.execute(
                "INSERT INTO outbox(event_id,task,text,created_at,state) VALUES(?,?,?,?,?)",
                (event_id, "coordinator", "Host command accepted; result pending.", time.time(), "executing"),
            )
        source = "local" if message.get("message_id") == 0 else "telegram"
        self.save_history(update_id, source, "user", text,
                          origin="typed" if source == "local" else "telegram_text")
        try:
            result = self.execute_host_command(text, self.store.get("owner_id"))
        except (ValueError, RuntimeError, OSError) as error:
            result = redact(str(error))
        self.save_history(update_id, source, "assistant", result)
        with self.store.db() as db:
            db.execute(
                "UPDATE outbox SET text=?,state=? WHERE event_id=?",
                (redact(result), "local" if source == "local" else "queued", event_id),
            )
        return True

    def notification_context(self, message):
        reply_id = (message.get("reply_to_message") or {}).get("message_id")
        with self.store.db() as db:
            reply = db.execute(
                "SELECT task,text,message_id,sent_at FROM outbox WHERE message_id=? AND state='sent' UNION ALL "
                "SELECT o.task,o.text,p.message_id,p.sent_at FROM outbox o JOIN outbound_text_parts p ON p.event_id=o.event_id "
                "WHERE p.message_id=? AND p.state='sent' LIMIT 1",
                (reply_id, reply_id),
            ).fetchone() if type(reply_id) is int else None
            recent = db.execute(
                "SELECT task,text,message_id,sent_at FROM outbox WHERE state='sent' AND event_id LIKE 'task:%' ORDER BY sent_at DESC LIMIT 4"
            ).fetchall()
        return {
            "reply_to_notification": dict(reply) if reply else None,
            "recent_notifications": [dict(row) for row in recent],
        }

    def accept_voice_note(self, update):
        message, owner = update["message"], self.store.get("owner_id")
        voice = message["voice"]
        if not isinstance(voice, dict):
            return False
        duration, size, file_id = voice.get("duration"), voice.get("file_size", 0), voice.get("file_id")
        if (
            type(duration) is not int or not 0 < duration <= 120
            or type(size) is not int or not 0 <= size <= 12 * 1024 * 1024
            or not isinstance(file_id, str) or not 0 < len(file_id) <= 512
        ):
            self.acknowledge(update, "vega", "Please send a voice note up to two minutes and 12 MB, or type your message.")
            return False
        with self.store.db() as db:
            db.execute("BEGIN IMMEDIATE")
            if db.execute("SELECT 1 FROM voice_inbox WHERE update_id=?", (update["update_id"],)).fetchone():
                return False
            db.execute(
                "INSERT INTO voice_inbox VALUES(?,?,?,?,?,'received',?,NULL)",
                (update["update_id"], message["message_id"], owner, file_id, duration, time.time()),
            )
            db.execute("INSERT OR IGNORE INTO inbox_context VALUES(?,?)", (update["update_id"], json.dumps(self.notification_context(message))))
        self.store.enqueue(f"voice-received:{update['update_id']}", "vega", "Voice note received. I’ll check the speaker before transcription.")
        return True

    def process_voice_note(self):
        row = self.store.claim("voice_inbox", "received", "transcribing")
        if not row:
            return False
        try:
            if row["owner"] != self.store.get("owner_id") or not self.api:
                raise SpeechError("The paired owner or Telegram connection changed.")
            if not self.owner_voice.automatic_allowed():
                with self.store.db() as db:
                    db.execute("UPDATE voice_inbox SET state='ignored',error='Owner voice setup is required.' WHERE update_id=?", (row["update_id"],))
                self.store.enqueue(f"voice-failed:{row['update_id']}", "vega",
                                   "Set up and validate your voice in Vega before automatic voice messages. You can still type here.")
                return True
            speaker_revision = self.owner_voice.revision()
            audio = self.api.download_voice(row["file_id"])
            def current_note(db):
                current = db.execute("SELECT owner,state FROM voice_inbox WHERE update_id=?", (row["update_id"],)).fetchone()
                if not current or current["owner"] != row["owner"] or current["state"] != "transcribing":
                    raise SpeechError("The voice note changed before transcription.")
            result = self.speech.transcribe_owner_note(
                audio, expected_revision=speaker_revision, admission_check=current_note,
            )
            audio = None
            if result.get("ignored"):
                with self.store.db() as db:
                    db.execute("UPDATE voice_inbox SET state='ignored',error=? WHERE update_id=?",
                               (result["reason"], row["update_id"]))
                self.store.enqueue(f"voice-failed:{row['update_id']}", "vega", result["reason"] + "\nText conversation still works.")
                return True
            transcript = result["text"]
            if not self._current_voice_origin("speaker_matched", result, row["owner"]):
                with self.store.db() as db:
                    db.execute("UPDATE voice_inbox SET state='ignored',error='Voice profile changed.' WHERE update_id=?", (row["update_id"],))
                return True
            with self.store.db() as db:
                db.execute("BEGIN IMMEDIATE")
                saved = db.execute("SELECT payload FROM inbox_context WHERE update_id=?", (row["update_id"],)).fetchone()
                context = json.loads(saved["payload"]) if saved else {}
                context.update({"origin": "speaker_matched", "speaker_revision": speaker_revision})
                db.execute("INSERT OR REPLACE INTO inbox_context VALUES(?,?)", (row["update_id"], json.dumps(context)))
                db.execute(
                    "INSERT OR IGNORE INTO inbox(update_id,message_id,owner_id,task,text,received_at,state) VALUES(?,?,?,?,?,?,'conversation')",
                    (row["update_id"], row["message_id"], row["owner"], "coordinator", transcript, row["received_at"]),
                )
                db.execute("UPDATE voice_inbox SET state='transcribed' WHERE update_id=?", (row["update_id"],))
        except (SpeechError, TelegramError, OSError, ValueError) as error:
            safe = redact(str(error))
            with self.store.db() as db:
                db.execute("UPDATE voice_inbox SET state='failed',error=? WHERE update_id=?", (safe, row["update_id"]))
            self.store.enqueue(f"voice-failed:{row['update_id']}", "vega", safe + "\nText conversation still works.")
        return True

    def process_voice_reply(self):
        row = self.store.claim("voice_replies", "queued", "synthesizing")
        if not row:
            return False
        try:
            preference = self.companion.preferences()
            with self.store.db() as db:
                explicit = db.execute("SELECT speak_requested FROM chat_history WHERE update_id=? AND role='assistant'",
                                      (row["update_id"],)).fetchone()
            if not preference["values"]["auto_speak"] and not (explicit and explicit["speak_requested"]):
                with self.store.db() as db:
                    db.execute("UPDATE voice_replies SET state='cancelled' WHERE update_id=?", (row["update_id"],))
                return True
            if not self.api:
                raise SpeechError("Telegram is unavailable for the spoken reply.")
            audio = self.speech.synthesize(row["text"][:16000], source="telegram")
            if preference["revision"] != self.companion.preferences()["revision"]:
                with self.store.db() as db:
                    db.execute("UPDATE voice_replies SET state='cancelled' WHERE update_id=?", (row["update_id"],))
                return True
            with self.store.db() as db:
                db.execute("UPDATE voice_replies SET state='sending' WHERE update_id=?", (row["update_id"],))
            result = self.api.send_voice(self.store.get("owner_chat_id"), audio, caption="Vega · spoken reply")
            with self.store.db() as db:
                db.execute("UPDATE voice_replies SET state='sent',message_id=? WHERE update_id=?", (result["message_id"], row["update_id"]))
        except (SpeechError, TelegramError, OSError, ValueError) as error:
            state = "uncertain" if isinstance(error, TelegramError) and error.uncertain else "failed"
            with self.store.db() as db:
                db.execute("UPDATE voice_replies SET state=?,error=? WHERE update_id=?", (state, redact(str(error)), row["update_id"]))
            if state == "failed":
                self.store.enqueue(f"voice-output-failed:{row['update_id']}", "vega", "The spoken reply could not be delivered. Your text reply is still available.\n" + redact(str(error)))
        return True

    def submit_local(self, text, request_id=None, *, voice_receipt=None, attachment_ids=None):
        owner = self.store.get("owner_id")
        if owner is None:
            raise ValueError("The owner account has not been paired.")
        identities=attachment_ids if attachment_ids is not None else []
        if (not isinstance(identities,list) or len(identities)>5
                or any(not isinstance(value,str) for value in identities)):
            raise ValueError("Attach up to five files.")
        if not isinstance(text, str) or len(text) > 12000 or not text.strip() and not identities:
            raise ValueError("Enter a message of at most 12,000 characters.")
        if identities and voice_receipt:
            raise ValueError("Send selected files from the text composer.")
        if request_id is not None and (
            not isinstance(request_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{16,80}", request_id)
        ):
            raise ValueError("The message identity is invalid.")
        digest = hashlib.sha256((json.dumps([text,identities]) if identities else text).encode("utf-8")).hexdigest()
        submitted_text=text if text.strip() else "[Attachments]"
        with self.admission_lock:
            frozen_context = self.companion.context()
            origin, speaker_revision = "typed", None
            greeting_only = False
            update_id = -time.time_ns()
            if request_id:
                with self.store.db() as db:
                    db.execute("BEGIN IMMEDIATE")
                    existing = db.execute("SELECT * FROM local_requests WHERE request_id=?", (request_id,)).fetchone()
                    if existing:
                        if existing["text_hash"] != digest:
                            raise ValueError("That message identity was already used for different text.")
                        update_id = existing["update_id"]
                        delivered = db.execute(
                            "SELECT 1 FROM inbox WHERE update_id=? UNION ALL SELECT 1 FROM outbox WHERE event_id=? LIMIT 1",
                            (update_id, f"ack:{update_id}"),
                        ).fetchone()
                        if delivered:
                            return update_id
                    else:
                        db.execute("INSERT INTO local_requests VALUES(?,?,?)", (request_id, update_id, digest))
            if voice_receipt:
                voice_context = self.speech.accept_transcript(
                    voice_receipt, text, self.companion.preferences()["revision"], with_provenance=True,
                )
                frozen_context = voice_context["context"]
                origin = voice_context["origin"]
                speaker_revision = voice_context.get("speaker_revision")
                greeting_only = voice_context.get("greeting_only") is True
                if greeting_only and not is_short_greeting(text):
                    raise SpeechError("This short recording can only be used as a greeting.")
                if origin not in ("speaker_matched", "manual_review"):
                    raise SpeechError("This audio was not confirmed as an owner instruction. Please type or review your message.")
                if origin == "speaker_matched" and text.lstrip().startswith("/"):
                    raise SpeechError("Use typed input for command confirmations; voice matching is not authentication.")
            with self.store.db() as db:
                db.execute("BEGIN IMMEDIATE")
                self.attachments.bind(db,owner,identities,update_id)
                db.execute(
                    "INSERT OR IGNORE INTO inbox_context VALUES(?,?)",
                    (update_id, json.dumps({"frozen_context": frozen_context, "origin": origin,
                                           "speaker_revision": speaker_revision,"local_attachment_ids":identities,
                                           "greeting_only": greeting_only})),
                )
            self.handle_update({
                "update_id": update_id,
                "message": {
                    "message_id": 0, "from": {"id": owner, "is_bot": False},
                    "chat": {"id": owner, "type": "private"}, "text": submitted_text,
                },
            })
            return update_id

    def cancel_local(self, update_id):
        try:
            identity = int(update_id)
        except (TypeError, ValueError):
            raise ValueError("Choose an active local reply.") from None
        with self.admission_lock:
            with self.store.db() as db:
                cancelled = db.execute(
                    "UPDATE inbox SET state='cancelled',receipt='Stopped by the owner',handled_at=? "
                    "WHERE update_id=? AND message_id=0 AND state IN ('conversation','responding')",
                    (time.time(), identity),
                ).rowcount
        return {"cancelled": bool(cancelled)}

    def model_action(self, update_id, action):
        with self.admission_lock:
            with self.store.db() as db:
                admitted = db.execute("UPDATE inbox SET state='acting' WHERE update_id=? AND state='responding'", (update_id,)).rowcount
            if not admitted:
                return "This reply was stopped before the action began."
            return action()

    def queue_project_request(self, row, source, permissions, targets, preference_revision):
        with self.admission_lock:
            with self.store.db() as db:
                claimed = db.execute("UPDATE inbox SET state='acting' WHERE update_id=? AND state='responding'",
                                     (row["update_id"],)).rowcount
            if not claimed:
                return None
            reference_ids=permissions.get("routing_reference_source_ids",[])
            for reference in reference_ids:
                recovered=self.project_requests.for_origin(row["owner_id"],reference)
                if recovered:
                    self.project_requests.link_origins(row["owner_id"],recovered["id"],[row["update_id"]])
                    return recovered
            origin = permissions.get("routing_origin_update_id") or str(row["update_id"])
            recovered = self.project_requests.for_origin(row["owner_id"], origin)
            if recovered:
                return recovered
            if str(origin) != str(row["update_id"]):
                with self.store.db() as db:
                    prior = db.execute("SELECT state,receipt FROM inbox WHERE update_id=? AND owner_id=?",
                                       (origin, row["owner_id"])).fetchone()
                if not prior or prior["state"] not in ("conversation_done", "conversation_failed") or re.search(
                    r"\b(?:expired|denied|rejected|uncertain|policy)\b", prior["receipt"] or "", re.I
                ):
                    raise ConflictError("That earlier request is held or already handled. It was not replayed.")
            instruction = permissions.get("routing_instruction") or row["text"]
            expected_threads = permissions.get("target_threads", {})
            resolved = [{"alias": alias, "thread_id": expected_threads.get(alias)} for alias in targets]
            request = self.project_requests.create(
                row["owner_id"], "owner_conversation", "owner-message:" + origin,
                instruction, resolved, expected_preference_revision=preference_revision,
                origin_ids=list(dict.fromkeys([int(origin),*reference_ids])),
            )
            return request

    def calendar_occurrences(self, start, end):
        if not (isinstance(start, (int, float)) and isinstance(end, (int, float)) and 0 < end - start <= 367 * 86400000):
            raise ValueError("Choose a calendar range of at most one year.")
        events = [
            {**item, "start_at": item["startAt"], "end_at": item["endAt"], "all_day": item["allDay"],
             "time_zone": item["timeZone"], "recurrence_until": item["recurrenceUntil"]}
            for item in self.companion.planning() if item["kind"] != "reminder"
        ]
        return self.speech.worker.call("calendar", {"events": events, "from": start, "to": end}, timeout=15)

    def briefing(self):
        data = self.companion.briefing_input()
        now = data["input"]["now"]
        occurrences = self.calendar_occurrences(now, now + 86400000)
        data["input"]["calendarEvents"] = [
            {**item, "startAt": item["start_at"], "endAt": item["end_at"]} for item in occurrences
        ]
        data["briefing"] = self.speech.worker.call("briefing", data["input"], timeout=15)
        return data

    def _typing(self, finished):
        while not finished.is_set() and not STOP.is_set():
            try:
                if self.api and self.store.get("owner_chat_id"):
                    self.api.call("sendChatAction", chat_id=self.store.get("owner_chat_id"), action="typing")
            except TelegramError:
                pass
            finished.wait(4)

    def process_conversation(self):
        row = self.store.claim("inbox", "conversation", "responding")
        if row is None:
            return False
        with self.store.db() as db:
            voice_note = db.execute("SELECT 1 FROM voice_inbox WHERE update_id=? AND state='transcribed'", (row["update_id"],)).fetchone()
            note_context = db.execute("SELECT payload FROM inbox_context WHERE update_id=?", (row["update_id"],)).fetchone()
        source = "local" if row["message_id"] == 0 else "telegram_voice" if voice_note else "telegram"
        reply_context = json.loads(note_context["payload"]) if note_context else {}
        origin = reply_context.get("origin", "legacy_unverified")
        if origin == "telegram_media_caption":
            source = "telegram_media"
        greeting_only = reply_context.get("greeting_only") is True
        authored = origin in ("typed", "telegram_text", "manual_review", "speaker_matched") and not greeting_only
        if not self._current_voice_origin(origin, reply_context, row["owner_id"]) or (
            source == "telegram_voice" and not authored
        ):
            with self.store.db() as db:
                db.execute("UPDATE inbox SET state='cancelled',receipt='Voice source was not confirmed; no owner action was taken.',"
                           "handled_at=? WHERE update_id=? AND state='responding'", (time.time(), row["update_id"]))
            return True
        self.save_history(row["update_id"], source, "user", row["text"], origin=origin,
                          speaker_revision=reply_context.get("speaker_revision"))
        history = self.history(20, before_update=row["update_id"])
        current_speaker_revision = self.owner_voice.revision() if self.owner_voice.automatic_allowed() else None
        permission_history = [
            item for item in history if item.get("owner_id") == row["owner_id"]
            and item.get("origin") in ("typed", "telegram_text", "manual_review", "speaker_matched")
            and (item.get("origin") != "speaker_matched" or (
                current_speaker_revision is not None and item.get("speaker_revision") == current_speaker_revision))
        ]
        finished = threading.Event()
        if source.startswith("telegram"):
            threading.Thread(target=self._typing, args=(finished,), daemon=True).start()
        next_state, routed_task, routed_text = "conversation_done", row["task"], row["text"]
        result, read_source, project_report = {}, None, None
        try:
            actual_reply = reply_context.get("reply_to_notification")
            attachment_data=self.attachments.model_context(row["owner_id"],row["update_id"]) if reply_context.get("local_attachment_ids") else {"sources":[],"images":[]}
            if source.startswith("telegram") and not attachment_data["sources"]:
                attachment_data = self.media.model_context(
                    row["owner_id"], row["update_id"], row["text"], reply_context, self.api)
            owner_view = reply_context.get("frozen_context") or self.companion.context()
            file_access=self.owner_files.capabilities(row["owner_id"])
            resolution=infer_context(
                row["text"],self.tasks,owner=row["owner_id"],history=permission_history,
                reply_project=actual_reply["task"] if actual_reply else None,
                project_roots=file_access.get("projects",{}),now=row["received_at"],
            ) if authored else {"project":None,"projects":[],"source":"unverified_input"}
            current_project=resolution["project"]
            owner_view={"revision":owner_view["revision"],"view":{
                "kind":owner_view["view"].get("kind","conversation"),
                "project":None if current_project == "coordinator" else current_project},
                "selection":"automatic","resolved_from":resolution["source"]}
            permissions = current_action_permissions(
                row["text"], self.controls.apps, self.tasks,
                reply_project=actual_reply["task"] if actual_reply else None,
                current_project=current_project, history=permission_history,
            )
            if not authored:
                permissions = {"intents": ["chat"], "project_targets": [], "app_targets": [],
                               "explicit_project_targets": []}
            spoken_authority_change = origin == "speaker_matched" and bool(re.search(
                r"^\s*(?:please\s+)?(?:approve|authorize|authorise|confirm)\b|"
                r"\b(?:increase|raise|remove|bypass|disable|grant)\b.{0,70}"
                r"\b(?:budgets?|spending|payments?|approval requirements?|confirmations?|unrestricted access)\b",
                unquoted_text(row["text"]), re.I,
            ))
            if spoken_authority_change:
                permissions = {"intents": ["chat"], "project_targets": [], "app_targets": [],
                               "explicit_project_targets": []}
            preferences = self.companion.preferences()
            preference_revision = preferences["revision"]
            operation_capability = self.operations.capabilities(
                row["owner_id"], row["text"], origin, resolution, file_access,
                enabled=authored and not spoken_authority_change and not attachment_data["sources"]
                        and preferences["values"]["project_context_enabled"])
            if operation_capability["operations"]:
                permissions["intents"].append("project_operation")
            reference=resolve_reference({**row,"origin":origin},history,permissions) if authored else None
            if reference:
                permissions["routing_instruction"]=reference["instruction"]
                permissions["routing_reference_source_ids"]=reference["source_update_ids"]
            permissions["target_threads"] = {
                alias: dispatch_target(self.tasks, alias)["thread_id"] for alias in permissions["project_targets"] if alias in self.tasks
            }
            # Descriptive context is needed only when a model is called.
            # Direct operations keep the same authority and receipt checks below.
            def model_context():
                preferences = self.companion.preferences()["values"]
                context = {
                    "owner": preferences["display_name"], "timezone": preferences["timezone"],
                    "architecture": "Vega is a local companion; use only the capabilities confirmed by the current host.",
                    "model_policy": {key:self.settings.get(key) for key in
                                     ("model","provider","region","reasoning_effort","max_output_tokens","max_conversations_per_hour")},
                    "team_capabilities": {
                        "version":3,"available":True,
                        "workflow":"Vega plans a supported owner request, assigns two independent model-backed specialists, checks their source citations and reviews their separate artifacts. Results and Vega's verdict return to this conversation. No separate task mode is required.",
                        "approval":"Agent drafts require Vega's accepted quality review and then the owner's exact-diff approval. Skills and agent outputs grant no new permissions.",
                        "limits":"At most two specialist model requests run together, within four model requests per job and the existing shared hourly allowance. Code changes remain single-file proposals. Paused project work does not execute.",
                        "skills":TEAM_SKILLS,
                        "agents":TEAM_AGENTS,
                        "automation":"Local n8n project workflows can be drafted by an automation designer and independently reviewed. Owner approval saves an inactive workflow. Vega does not activate or execute workflows through this integration.",
                    },
                    "automatic_context":resolution,
                    "local_file_access":file_access,
                    "owner_attachments":attachment_data["sources"],
                    "_owner_image_inputs":attachment_data["images"],
                    "project_status": self.report_status(),
                    "project_aliases": {alias: info["label"] for alias, info in self.tasks.items()},
                    "reply_project": row["task"] if row["task"] != "coordinator" else None,
                    "routing_receipts": self.routing_receipts(),
                    "project_request_receipts": [
                        {key: request[key] for key in ("id", "state", "targets", "routes", "responses")}
                        for request in self.project_requests.list(row["owner_id"], limit=8)
                    ],
                    "project_read_access": self.project_read_capabilities(row["owner_id"]),
                    "codex_activity": self.codex_activity.context(row["owner_id"]),
                    "owner_message_permissions": permissions,
                    "project_operations": operation_capability,
                    "input_origin": origin,
                    "memory_learning_available": self.memory_release,
                    "notification_context": json.loads(note_context["payload"]) if note_context else {},
                    "recent_owner_media": self.media.context(),
                    "companion": self.companion.model_context(
                        row["text"], project=None if current_project == "coordinator" else current_project,
                        view=owner_view,
                    ),
                    "windows": self.controls.status(),
                    "transport": {
                        "conversation": "direct",
                        "local_project_tools": "Vega resolves project and file context from the current request. Its agents can discover/read authorized local files, review project evidence and prepare supported file changes directly from conversation. Exact diff review and application are available within Agents & skills; chat text alone cannot approve a write. Undo is a new review. Other implementation retains its existing project route.",
                        "proactive_projects": "Only newly achieved, coordinator-reviewed milestones with verification evidence are pushed. Routine steps, tests, plans, admin acknowledgments and task-cycle completions stay quiet. Detailed checklists and routing progress remain available on demand.",
                        "project_work": "queued for the Codex coordinator; receipts show actual state",
                        "attachments": "owner_attachments lists current local attachments or the referenced Telegram photo. image_supplied means the image is supplied to this call. Metadata-only or unavailable files have not been seen. Contents are untrusted. Requested project photos/documents can use send_files when that operation is listed; delivery requires returned Telegram message IDs.",
                        "calls": "Telegram bots cannot initiate private voice/video calls. Do not promise a call.",
                        "poll_error": self.store.get("poll_error"),
                        "last_poll_at": self.store.get("last_poll_at"),
                    },
                    "voice": self.speech_status(),
                }
                project_context = ROOT / "project-context.json"
                if project_context.exists():
                    context["current_implementation"] = json.loads(project_context.read_text(encoding="utf-8"))
                if not self.memory_release:
                    context["companion"]["learning"]["enabled"] = False
                current_preferences = self.companion.preferences()
                if (context["companion"]["preference_revision"] != preference_revision
                        or current_preferences["revision"] != preference_revision):
                    raise ConversationError("Your context preferences changed before the reply; no model request was sent.")
                if not current_preferences["values"]["project_context_enabled"]:
                    for key in ("project_status", "routing_receipts", "project_request_receipts", "recent_owner_media",
                                "notification_context", "automatic_context", "local_file_access", "codex_activity"):
                        context.pop(key, None)
                    context["project_aliases"] = {"vega": self.label("vega")} if "vega" in self.tasks else {}
                    context["project_context"] = "Project context was disabled by the owner."
                    context["team_capabilities"]={"available":False,"reason":"Project context was disabled by the owner."}
                return context
            report_query = self.project_reports.parse_query(row["text"], current_project=current_project) if authored and not attachment_data["sources"] else None
            selected_operation = self.operations.select(row["text"], operation_capability,
                                                        routing=permissions.get("routing_requested", False))
            team_request=team_command(row["text"],self.tasks) if authored and not attachment_data["sources"] else None
            if authored and not team_request and not report_query and not selected_operation and not spoken_authority_change and not attachment_data["sources"]:
                writable=[item["alias"] for item in self.project_tools.projects(row["owner_id"])["projects"]
                          if (item.get("workspace") or {}).get("can_write")]
                team_request=work_request(
                    row["text"],self.tasks,resolution,permissions,
                    files_available=file_access.get("available",False),writable_projects=writable)
            if origin == "speaker_matched" and is_short_greeting(row["text"]):
                result = {"intent": "chat", "reply": "Hi! I’m here. What can I help you with?",
                          "memory_candidates": [], "reply_kind": "chat"}
            elif spoken_authority_change:
                result = {"intent": "chat", "reply": "Please confirm that approval or spending change in text. Voice matching does not replace action confirmations.",
                          "memory_candidates": []}
            elif authored and explicit_read_aloud(row["text"]):
                previous = next((item for item in reversed(history) if item["role"] == "assistant"), None)
                if previous:
                    read_source = previous["text"]
                    result = {"intent": "chat", "reply": "I’ll read the previous reply aloud.",
                              "memory_candidates": [], "reply_kind": "chat"}
                else:
                    result = {"intent": "chat", "reply": "There isn’t an earlier reply to read yet.", "memory_candidates": []}
            elif attachment_data["sources"]:
                result=self.brain.reply("" if row["text"]=="[Attachments]" else row["text"],history,model_context())
            elif selected_operation:
                result = {"intent": "project_operation", "operation": selected_operation, "memory_candidates": []}
            elif team_request:
                operation=team_request.pop("operation")
                if operation=="create":
                    run=self.team.create(row["owner_id"],{"request_key":f"owner-message:{row['update_id']}",**team_request},
                                         source="telegram_owner" if source.startswith("telegram") else "local_owner_ui",
                                         origin_update_id=row["update_id"])
                    routed_task=run["project"]
                    action={"report":"a project review","inspect":"a file investigation","review":"a file review","change":"change preparation","automation":"an n8n workflow draft"}[run["mode"]]
                    pause_note=(" Project work is paused, so the saved request will wait until you resume it."
                                if self.store.get("work_paused",False) else " I’ll return the specialists' results and my review here.")
                    result={"intent":"chat","reply":f"Queued {action} for {run['label']}."+pause_note
                            +(" The proposed diff will appear for your review." if run["mode"]=="change" else ""),
                            "memory_candidates":[]}
                elif operation=="status":
                    run=self.team.get(row["owner_id"],team_request["id"])
                    result={"intent":"chat","reply":f"{run['label']} · {run['state']}\n{(run.get('result') or {}).get('summary') or run.get('error') or 'The saved run has no final result yet.'}\nModel requests: {run['usage']['model_requests']}/{run['usage']['max_requests']}",
                            "memory_candidates":[]}
                else:
                    result={"intent":"chat","reply":"Ask me directly to review a project, find a file or prepare a supported code change. I’ll choose the relevant project and use my agents and skills, then return the result here. Proposed file changes appear for exact diff review.","memory_candidates":[]}
            elif authored and activity_question(row["text"]):
                result={"intent":"chat","reply":conversation_activity(self.team.catalog(row["owner_id"])),
                    "memory_candidates":[]}
            elif report_query:
                project_report = self.project_reports.read(row["owner_id"], report_query)
                result = {"intent": "chat", "reply": project_report["text"], "reply_kind": "instructions",
                          "spoken_reply": "Here is the requested project information, with its recorded date.",
                          "memory_candidates": []}
            elif authored and re.search(r"\b(?:current brain|which model|what model|model (?:are|do) you|brain.*what)\b",unquoted_text(row["text"]),re.I):
                result={"intent":"chat","reply":self.runtime_explanation(row["text"],model_only=True),"memory_candidates":[]}
            elif permissions.get("routing_requested") and permissions["project_targets"]:
                result = {"intent": "project_instruction", "task": permissions["project_targets"][0],
                          "reply": "", "app": None, "instruction": None, "memory_candidates": []}
            elif permissions.get("routing_requested"):
                draft = self.queue_project_request(row, source, permissions, [], preference_revision)
                result = {"intent": "chat", "app": None, "task": None, "instruction": None, "memory_candidates": [],
                          "reply": ("Which registered project should receive the prompt? "
                                    f"Request {draft['id']} is waiting for that input; nothing has been queued.")
                                   if draft else "The request was stopped before routing."}
            else:
                result = self.brain.reply(row["text"], history, model_context())
            if preference_revision != self.companion.preferences()["revision"]:
                raise ConversationError("Your context preferences changed during this reply. Its result was discarded; no new action was taken.")
            with self.store.db() as db:
                current = db.execute("SELECT state FROM inbox WHERE update_id=?", (row["update_id"],)).fetchone()
            if current["state"] == "cancelled":
                return True
            if not self._current_voice_origin(origin, reply_context, row["owner_id"]):
                with self.store.db() as db:
                    db.execute("UPDATE inbox SET state='cancelled',receipt='Voice profile changed; the result was discarded.',"
                               "handled_at=? WHERE update_id=? AND state IN ('responding','acting')",
                               (time.time(), row["update_id"]))
                return True
            intent = result["intent"]
            if intent == "project_operation":
                outcome = self.model_action(row["update_id"],
                    lambda: self.operations.execute(result.get("operation"), row, operation_capability))
                if isinstance(outcome, dict):
                    reply, project_report = outcome["text"], outcome.get("report")
                else:
                    reply = outcome
            elif intent == "open_app":
                app = result.get("app")
                if "open_app" not in permissions["intents"] or app not in permissions["app_targets"]:
                    reply = "Please ask me directly to open the app you want. Saved context cannot authorize an app action."
                else:
                    reply = self.model_action(row["update_id"], lambda: self.controls.open_app(app))
            elif intent == "request_reboot":
                if "request_reboot" not in permissions["intents"]:
                    reply = "A reboot needs your direct request and a fresh confirmation. Nothing was scheduled."
                else:
                    reply = self.model_action(row["update_id"], lambda: self.controls.request_reboot(row["owner_id"]))
            elif intent == "project_instruction":
                alias = result.get("task")
                reply_context = json.loads(note_context["payload"]) if note_context else {}
                actual_reply = reply_context.get("reply_to_notification")
                explicit_alias = bool(permissions["explicit_project_targets"]) or (
                    permissions.get("self_development_target") == "vega" and alias == "vega"
                )
                consequential = re.search(r"\b(budget|spend|payment|purchase|pay|approve|approval|permission)\b", row["text"], re.I)
                if consequential and not explicit_alias and not actual_reply:
                    next_state = "awaiting_clarification"
                    reply = "Which project and spending limit do you mean? I’ve kept this approval on hold so it cannot be applied to the wrong task."
                elif alias not in self.tasks:
                    reply = "Which project should receive that instruction? Use /projects to see the available names."
                else:
                    if consequential and actual_reply and not explicit_alias:
                        alias = actual_reply["task"]
                    if "project_instruction" not in permissions["intents"] or alias not in permissions["project_targets"]:
                        reply = "Please name the project and the work you want it to do. I have not dispatched an instruction from saved context."
                    else:
                        targets = permissions["project_targets"] if permissions.get("routing_requested") else [alias]
                        request = self.queue_project_request(
                            row, source, permissions, targets, preference_revision)
                        routed_task = alias if len(targets) == 1 else "coordinator"
                        reply = self.project_requests.routing_receipt(request) if request else "The request was stopped before routing."
                        if permissions.get("routing_explanation_requested"):
                            reply += "\n\n" + self.runtime_explanation(row["text"])
            else:
                reply = result["reply"]
        except (ConversationError, ValueError, RuntimeError, OSError) as error:
            reply = redact(str(error))
            self.store.set("conversation_error", reply)
            next_state = "conversation_failed"
        finally:
            finished.set()
        with self.store.db() as db:
            updated = db.execute(
                "UPDATE inbox SET state=?,task=?,text=?,receipt=?,handled_at=? WHERE update_id=? AND state IN ('responding','acting')",
                (next_state, routed_task, routed_text, reply[:2000], time.time(), row["update_id"]),
            ).rowcount
        if not updated:
            return True
        spoken = select_spoken_reply(
            read_source if read_source is not None else reply,
            owner_text=row["text"] if authored else "",
            spoken_reply=result.get("spoken_reply"), reply_kind=result.get("reply_kind"),
        )
        self.save_history(row["update_id"], source, "assistant", reply,
                          spoken_text=spoken, speak_requested=read_source is not None)
        if source.startswith("telegram"):
            if project_report and project_report.get("available"):
                try:
                    self.project_responses.queue(
                        row["owner_id"], row["update_id"], project_report,
                        expected_preference_revision=preference_revision,
                    )
                    self.store.set("report_response_error", None)
                except (PermissionError, ValueError, RuntimeError, OSError):
                    # Keep written content and expose the missing queue receipt.
                    # No model-created acknowledgment can turn this into delivery.
                    self.store.set("report_response_error", "A requested project response could not be queued. Its written result is retained locally.")
                    with self.store.db() as db:
                        db.execute("UPDATE inbox SET state='conversation_failed',receipt=? WHERE update_id=?",
                                   ("Report content was retrieved but Telegram delivery was not queued. Check the response receipt.", row["update_id"]))
            else:
                self.store.enqueue(f"conversation:{row['update_id']}", routed_task, reply)
        if spoken and (source == "telegram_voice" and self.companion.preferences()["values"]["auto_speak"]
                       or source.startswith("telegram") and read_source is not None):
            with self.store.db() as db:
                db.execute("INSERT OR IGNORE INTO voice_replies VALUES(?,?,'queued',?,NULL,NULL)", (row["update_id"], spoken, time.time()))
        if self.memory_release and authored and next_state not in ("conversation_failed", "awaiting_clarification"):
            try:
                self.companion.memory.learn(
                    row["update_id"], source, row["text"], result.get("memory_candidates", []),
                    current_project=None if current_project == "coordinator" else current_project,
                    preference_revision=preference_revision,
                    expected_owner=row["owner_id"],
                )
                self.store.set("memory_error", None)
            except Exception as error:
                self.store.set("memory_error", f"Memory learning needs attention ({type(error).__name__}).")
        return True

    def runtime_explanation(self,text="",*,model_only=False):
        model=(f"My current model is {self.settings['model']} through {self.settings['provider']} "
               f"in {self.settings['region']}. It uses {self.settings.get('reasoning_effort','configured')} reasoning "
               f"and the existing {self.settings.get('max_output_tokens',1400)}-token reply limit.")
        if model_only:return model
        return (model+"\n\nI plan work and use specialist agents, subagents and implemented skills through our conversation. "
                "I resolve the project and local files from your request and return the saved results here. Exact file writes "
                "wait for your review. Other project work uses the existing coordinator and project owners "
                "until its replacement is implemented and verified.")

    def _record_conversation_failure(self, error):
        diagnostic = getattr(error, "sqlite_errorname", type(error).__name__)
        frames = traceback.extract_tb(error.__traceback__)
        location = ""
        if frames:
            frame = frames[-1]
            location = f" at {Path(frame.filename).name}:{frame.name}:{frame.lineno}"
        self.store.set("conversation_error", f"Live conversation needs attention ({diagnostic}){location}.")
        with self.store.db() as db:
            db.execute(
                "UPDATE inbox SET state='interrupted',"
                "receipt='The conversation stopped unexpectedly; it was not replayed. Its outcome may be uncertain.',"
                "handled_at=? WHERE state IN ('responding','acting')",
                (time.time(),),
            )

    def _clear_stale_conversation_error(self):
        with self.store.db() as db:
            active = db.execute(
                "SELECT 1 FROM inbox WHERE state IN ('responding','acting') LIMIT 1"
            ).fetchone()
        if not active:
            self.store.set("conversation_error", None)

    def conversation_loop(self):
        self._clear_stale_conversation_error()
        last_tick = 0
        while not STOP.is_set():
            try:
                if time.time() - last_tick > 5:
                    last_tick = time.time()
                    self.store.set("last_conversation_tick", last_tick)
                    try:
                        diagnostic_store=getattr(self.owner_voice,"diagnostics",None)
                        if diagnostic_store:diagnostic_store.expire()
                    except (ValueError,OSError):
                        self.store.set("voice_diagnostic_error","The private diagnostic needs cleanup review.")
                if not self.process_conversation():
                    STOP.wait(0.3)
            except Exception as error:
                self._record_conversation_failure(error)
                STOP.wait(2)

    def delivery_loop(self):
        # Outgoing replies must not wait for the next Telegram long-poll response.
        last_tick = 0
        last_photo_tick = 0
        while not STOP.is_set():
            try:
                if time.time() - last_photo_tick >= 1:
                    last_photo_tick = time.time()
                    try:
                        self.photo_batches.flush_ready()
                    except Exception as error:
                        self.store.set("photo_batch_error", f"Photo grouping needs attention ({type(error).__name__}); original media is retained.")
                if time.time() - last_tick > 5:
                    last_tick = time.time()
                    self.store.set("last_delivery_tick", last_tick)
                STOP.wait(1.05 if self.flush_one() else 0.15)
                if self.store.get("delivery_error"):
                    self.store.set("delivery_error", None)
            except Exception as error:
                diagnostic = getattr(error, "sqlite_errorname", type(error).__name__)
                self.store.set("delivery_error", f"Delivery needs attention ({diagnostic}).")
                STOP.wait(2)

    def team_loop(self):
        while not STOP.is_set():
            try:
                if not self.team.process_one():
                    STOP.wait(.5)
                self.store.set("team_error",None)
            except Exception as error:
                self.store.set("team_error",f"Agent work needs attention ({type(error).__name__}).")
                STOP.wait(2)

    def voice_input_loop(self):
        while not STOP.is_set():
            try:
                if not self.process_voice_note():
                    STOP.wait(0.3)
            except Exception as error:
                self.store.set("speech_error", f"Voice input needs attention ({type(error).__name__}).")
                STOP.wait(2)

    def document_loop(self):
        while not STOP.is_set():
            try:
                STOP.wait(1.05 if self.flush_one(documents=True) else 0.5)
                if self.store.get("document_error"):
                    self.store.set("document_error", None)
            except Exception as error:
                self.store.set("document_error", f"Document delivery needs attention ({type(error).__name__}).")
                STOP.wait(2)

    def voice_output_loop(self):
        while not STOP.is_set():
            try:
                if not self.process_voice_reply():
                    STOP.wait(0.3)
            except Exception as error:
                self.store.set("speech_error", f"Spoken replies need attention ({type(error).__name__}).")
                STOP.wait(2)

    def companion_loop(self):
        while not STOP.is_set():
            try:
                self.companion.notify_due()
            except Exception as error:
                self.store.set("companion_error", f"Companion data needs attention ({type(error).__name__}).")
            STOP.wait(15)

    def watchdog_loop(self):
        while not STOP.is_set():
            try:
                self.store.set("watchdog_state", self.controls.watchdog_tick())
                self.store.set("watchdog_error", None)
            except Exception as error:
                self.store.set("watchdog_error", f"Windows recovery needs attention ({type(error).__name__}).")
            try:
                self.away_monitor.tick(self.supervisor.snapshot() if self.supervisor else {})
                self.store.set("away_monitor_error", None)
            except Exception as error:
                self.store.set("away_monitor_error", f"Incident monitoring needs attention ({type(error).__name__}).")
            STOP.wait(5)


def make_handler(vega, port):
    csrf = secrets.token_urlsafe(32)
    host = f"127.0.0.1:{port}"
    origin = "http://" + host

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def reply(self, code, content, content_type="application/json"):
            body=content if isinstance(content,bytes) else content.encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", content_type + "; charset=utf-8")
            self.send_header("Content-Length",str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("X-Frame-Options", "DENY")
            self.send_header("Permissions-Policy", "microphone=(self), camera=(), geolocation=()")
            self.send_header(
                "Content-Security-Policy",
                f"default-src 'self'; script-src 'self' 'wasm-unsafe-eval' 'nonce-{csrf}'; style-src 'self' 'unsafe-inline'; "
                "connect-src 'self'; img-src 'self' blob: data:; media-src 'self' blob:; frame-ancestors 'none'; base-uri 'none'; form-action 'self'",
            )
            self.end_headers()
            try:
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):
                pass

        def do_GET(self):
            if self.headers.get("Host") != host:
                return self.reply(403, '{"error":"Unexpected host"}')
            parsed = urlsplit(self.path)
            route, query = parsed.path, parse_qs(parsed.query)
            if route == "/api/infrastructure":
                if (self.headers.get("Origin") not in (None, origin)
                        or self.headers.get("Sec-Fetch-Site") in {"cross-site", "same-site"}):
                    return self.reply(403, '{"error":"Use the local Vega interface."}')
            if route == "/api/copilot/approvals":
                if (self.headers.get("Origin") not in (None, origin)
                        or self.headers.get("Sec-Fetch-Site") in {"cross-site", "same-site"}):
                    return self.reply(403, '{"error":"Use the local Vega interface."}')
            if route == "/api/diagnostics/latency":
                if (self.headers.get("Origin") not in (None, origin)
                        or self.headers.get("Sec-Fetch-Site") in {"cross-site", "same-site"}):
                    return self.reply(403, '{"error":"Use the local Vega interface."}')
                return self.reply(200, json.dumps(vega.read_timings.snapshot()))
            label = READ_TIMING_ROUTES.get(route, "http.assets" if route.startswith("/assets/") else None)
            if route.startswith("/api/work-overview/projects/"):
                label = "http.work"
            if route.startswith("/api/team/projects/"):
                label = "http.team"
            if label:
                with vega.read_timings.measure(label):
                    return self.get_route(route, query)
            return self.get_route(route, query)

        def get_route(self, route, query):
            if route in MINIAPP_VISUAL_ASSETS:
                name, kind = MINIAPP_VISUAL_ASSETS[route]
                return self.reply(200, (ROOT / "public" / "miniapp" / name).read_bytes(), kind)
            if route == "/":
                page = (ROOT / "public" / "index.html").read_text(encoding="utf-8").replace("__CSRF__", csrf)
                return self.reply(200, page, "text/html")
            if route == "/projects":
                page = (ROOT / "public" / "miniapp" / "index.html").read_text(encoding="utf-8")
                page = page.replace('<script src="https://telegram.org/js/telegram-web-app.js"></script>', "")
                page = page.replace('src="/mini/app.js"', 'src="/assets/local-projects.js"')
                page = page.replace("data-miniapp-autostart", "data-local-projects")
                page = page.replace("Connecting through Telegram…", "Loading your local project workspace…")
                return self.reply(200, page, "text/html")
            if route == "/team":
                return self.reply(200,(ROOT/"public"/"team-console.html").read_bytes(),"text/html")
            if route == "/work":
                return self.reply(200,(ROOT/"public"/"work-console.html").read_bytes(),"text/html")
            if route == "/infrastructure":
                return self.reply(200,(ROOT/"public"/"infrastructure.html").read_bytes(),"text/html")
            if route in ("/mini/team-panel.js","/mini/team-panel.css"):
                path=ROOT/"public"/route.rsplit("/",1)[-1]
                return self.reply(200,path.read_bytes(),"text/javascript" if path.suffix==".js" else "text/css")
            if route in ("/request-receipts.js", "/mini/request-receipts.js"):
                return self.reply(200, (ROOT / "public" / "request-receipts.js").read_bytes(), "text/javascript")
            if route == "/composer-attachments.js":
                return self.reply(200, (ROOT / "public" / "composer-attachments.js").read_bytes(), "text/javascript")
            if route in ("/mini/app.js", "/mini/workspace.js", "/mini/style.css", "/mini/vega-icon.svg"):
                path = ROOT / "public" / "miniapp" / route.rsplit("/", 1)[-1]
                return self.reply(200, path.read_bytes(), {".js":"text/javascript",".css":"text/css",".svg":"image/svg+xml"}[path.suffix])
            if route.startswith("/assets/"):
                public = (ROOT / "public").resolve()
                requested = (public / route.removeprefix("/assets/")).resolve()
                asset_types = {".js": "text/javascript", ".mjs": "text/javascript", ".css": "text/css",
                               ".svg": "image/svg+xml", ".wasm": "application/wasm", ".onnx": "application/octet-stream"}
                if not requested.is_relative_to(public) or not requested.is_file() or requested.suffix not in asset_types:
                    return self.reply(404, '{"error":"Asset not found"}')
                content_type = asset_types[requested.suffix]
                return self.reply(200, requested.read_bytes(), content_type)
            if route == "/api/status":
                return self.reply(200, json.dumps(vega.status()))
            if route == "/api/projects":
                vega.refresh_registry()
                return self.reply(200, json.dumps({alias: {
                    "thread_id": value["thread_id"], "label": value["label"],
                    "dispatch_thread_id": dispatch_target(vega.tasks, alias)["thread_id"],
                    "dispatch_owner": dispatch_target(vega.tasks, alias)["label"],
                } for alias, value in vega.tasks.items()}))
            if route == "/api/session":
                return self.reply(200, json.dumps({"csrf": csrf}))
            if route == "/api/messages":
                try:
                    messages=vega.history_messages()
                except (PermissionError,ValueError):
                    return self.reply(403,'{"error":"The paired owner is required."}')
                return self.reply(200, json.dumps(messages))
            try:
                if route=="/api/attachments":
                    return self.reply(200,json.dumps({"attachments":vega.attachments.for_message(
                        vega.store.get("owner_id"),int(query.get("update_id",["0"])[0]))}))
                if re.fullmatch(r"/api/attachments/[a-f0-9-]{36}/content",route):
                    if (self.headers.get("Origin") not in (None,origin)
                            or self.headers.get("Sec-Fetch-Site") in {"cross-site","same-site"}):
                        return self.reply(403,'{"error":"Use the local Vega interface."}')
                    content,attachment=vega.attachments.content(vega.store.get("owner_id"),route.split("/")[-2])
                    self.send_response(200)
                    self.send_header("Content-Type",attachment["mime_type"])
                    self.send_header("Content-Length",str(len(content)))
                    self.send_header("Cache-Control","no-store")
                    self.send_header("X-Content-Type-Options","nosniff")
                    self.send_header("Cross-Origin-Resource-Policy","same-origin")
                    self.send_header("Content-Security-Policy","default-src 'none'; sandbox")
                    self.send_header("Content-Disposition",("inline" if attachment["kind"]=="image" else "attachment")+
                                     "; filename*=UTF-8''"+quote(attachment["name"],safe=""))
                    self.end_headers()
                    try:self.wfile.write(content)
                    except (BrokenPipeError,ConnectionResetError):pass
                    return
                if route == "/api/team":
                    return self.reply(200,json.dumps(vega.team.catalog(
                        vega.store.get("owner_id"),include_file_access=query.get("activity",[""])[0]!="1")))
                if route == "/api/work-overview" or re.fullmatch(r"/api/work-overview/projects/[a-z0-9-]+",route):
                    project=route.rsplit("/",1)[-1] if "/projects/" in route else None
                    return self.reply(200,json.dumps(work_overview(vega,vega.store.get("owner_id"),project)))
                if route == "/api/n8n":
                    return self.reply(200,json.dumps(vega.n8n.catalog(vega.store.get("owner_id"))))
                if re.fullmatch(r"/api/team/projects/[a-z0-9-]+",route):
                    return self.reply(200,json.dumps(vega.team.catalog(
                        vega.store.get("owner_id"),project=route.rsplit("/",1)[-1],
                        include_file_access=query.get("activity",[""])[0]!="1")))
                if re.fullmatch(r"/api/team/requests/[a-f0-9-]{36}/reference", route):
                    return self.reply(200, json.dumps(preview_reference(
                        vega.store.get("owner_id"), route.split("/")[4], query.get("ref", [""])[0],
                        requests=vega.project_requests, reports=vega.project_reports,
                        tasks=vega.tasks, activity=vega.project_activity,
                    )))
                reference = re.fullmatch(r"/api/project-control/requests/([a-f0-9-]{36})/references/([^/]{1,1536})", route)
                if reference:
                    token = unquote(reference[2])
                    if len(token) > 512:
                        raise ValueError("Reference is too long.")
                    return self.reply(200, json.dumps(preview_reference(
                        vega.store.get("owner_id"), reference[1], token, requests=vega.project_requests,
                        reports=vega.project_reports, tasks=vega.tasks, activity=vega.project_activity)))
                if re.fullmatch(r"/api/team/runs/[a-f0-9-]{36}",route):
                    return self.reply(200,json.dumps({"run":vega.team.get(vega.store.get("owner_id"),route.rsplit("/",1)[-1])}))
                if route == "/api/voice/owner":
                    return self.reply(200, json.dumps(vega.owner_voice.status()))
                if route == "/api/project-tools/projects":
                    return self.reply(200, json.dumps(vega.project_tools.projects(vega.store.get("owner_id"))))
                if route == "/api/copilot/projects":
                    return self.reply(200, json.dumps(vega.copilot_sessions.projects(vega.store.get("owner_id"))))
                if route == "/api/copilot/sessions":
                    return self.reply(200, json.dumps(vega.copilot_sessions.list(
                        vega.store.get("owner_id"), query.get("project", [None])[0])))
                if route == "/api/copilot/approvals":
                    return self.reply(200, json.dumps(
                        vega.copilot_sessions.list_approvals(vega.store.get("owner_id"))))
                if re.fullmatch(r"/api/copilot/sessions/[a-f0-9-]{36}", route):
                    return self.reply(200, json.dumps(vega.copilot_sessions.get(
                        vega.store.get("owner_id"), route.rsplit("/", 1)[-1])))
                if route == "/api/project-tools/proposals":
                    return self.reply(200, json.dumps(vega.project_tools.list(
                        vega.store.get("owner_id"), query.get("project", [None])[0])))
                if re.fullmatch(r"/api/project-tools/proposals/[^/]+", route):
                    return self.reply(200, json.dumps(vega.project_tools.get(
                        vega.store.get("owner_id"), route.rsplit("/", 1)[-1])))
                if route == "/api/project-control/workspace":
                    return self.reply(200, json.dumps(vega.desktop_workspace.snapshot(vega.store.get("owner_id"))))
                if route == "/api/project-control/vscode-agents/workspace":
                    return self.reply(200, json.dumps(vega.vscode_agents_workspace.snapshot(vega.store.get("owner_id"))))
                if re.fullmatch(r"/api/project-control/vscode-agents/chats/[a-f0-9-]{36}", route):
                    return self.reply(200, json.dumps(vega.vscode_agents_workspace.read(
                        vega.store.get("owner_id"), route.rsplit("/", 1)[-1])))
                if route == "/api/project-control/vscode-agents/chat-prompts":
                    return self.reply(200, json.dumps({"receipts": vega.vscode_agents_workspace.receipts(vega.store.get("owner_id"))}))
                if route == "/api/project-control/antigravity/workspace":
                    return self.reply(200, json.dumps(vega.antigravity_workspace.snapshot(vega.store.get("owner_id"))))
                if re.fullmatch(r"/api/project-control/antigravity/chats/[a-f0-9-]{36}", route):
                    return self.reply(200, json.dumps(vega.antigravity_workspace.read(
                        vega.store.get("owner_id"), route.rsplit("/", 1)[-1])))
                if route == "/api/project-control/antigravity/chat-prompts":
                    return self.reply(200, json.dumps({"receipts": vega.antigravity_workspace.receipts(vega.store.get("owner_id"))}))
                if route == "/api/project-control/deepseek/workspace":
                    return self.reply(200, json.dumps(vega.deepseek_workspace.snapshot(vega.store.get("owner_id"))))
                if re.fullmatch(r"/api/project-control/deepseek/chats/[a-f0-9-]{36}", route):
                    return self.reply(200, json.dumps(vega.deepseek_workspace.read(
                        vega.store.get("owner_id"), route.rsplit("/", 1)[-1])))
                if route == "/api/project-control/deepseek/chat-prompts":
                    return self.reply(200, json.dumps({"receipts": vega.deepseek_workspace.receipts(vega.store.get("owner_id"))}))
                if route == "/api/infrastructure":
                    return self.reply(200, json.dumps(vega.infrastructure.snapshot(vega.store.get("owner_id"))))
                if re.fullmatch(r"/api/project-control/chats/[a-f0-9-]{36}", route):
                    return self.reply(200, json.dumps(vega.desktop_workspace.read(
                        vega.store.get("owner_id"), route.rsplit("/", 1)[-1])))
                if route == "/api/project-control/chat-prompts":
                    return self.reply(200, json.dumps({"receipts": vega.desktop_workspace.receipts(vega.store.get("owner_id"))}))
                if route == "/api/project-control/projects":
                    return self.reply(200, json.dumps(vega.project_views(vega.store.get("owner_id"))))
                if route.startswith("/api/project-control/projects/"):
                    return self.reply(200, json.dumps(vega.project_views(vega.store.get("owner_id"), route.rsplit("/", 1)[-1])))
                if route == "/api/project-control/requests":
                    return self.reply(200, json.dumps({"requests": vega.project_requests.list(vega.store.get("owner_id"))}))
                if route.startswith("/api/project-control/requests/"):
                    return self.reply(200, json.dumps({"request": vega.project_requests.get(
                        vega.store.get("owner_id"), route.rsplit("/", 1)[-1])}))
                if route == "/api/companion":
                    return self.reply(200, json.dumps({
                        "preferences": vega.companion.preferences(), "context": vega.companion.context(),
                        "projects": {key: value["label"] for key, value in vega.tasks.items() if key != "coordinator"},
                        "speech": vega.speech_status(),
                        "selective_memory_available": vega.memory_release,
                    }))
                if route == "/api/voice/config":
                    return self.reply(200, json.dumps(vega.speech.route_info()))
                if route == "/api/briefing":
                    return self.reply(200, json.dumps(vega.briefing()))
                if route == "/api/memory":
                    return self.reply(200, json.dumps(vega.companion.memories(query.get("q", [""])[0], query.get("project", [None])[0] or None)))
                if route == "/api/memory/state":
                    if not vega.memory_release:
                        return self.reply(404, '{"error":"Reviewed memory upgrade is not released yet."}')
                    project = query.get("project", [None])[0] or None
                    all_scopes = query.get("all", ["0"])[0] == "1"
                    preferences = vega.companion.preferences()["values"]
                    return self.reply(200, json.dumps({
                        "notes": vega.companion.memory.list(query.get("q", [""])[0], project, all_scopes=all_scopes),
                        "suggestions": vega.companion.memory.pending(project, all_scopes=all_scopes),
                        "events": vega.companion.memory.events(project, all_scopes=all_scopes),
                        "imports": vega.companion.memory.imports(project, all_scopes=all_scopes),
                        "learning": {"enabled": preferences["memory_enabled"] and preferences["learning_enabled"],
                                     "review_required": preferences["learning_review_required"]},
                        "formats": [{"id": key, "label": label} for key, label in MEMORY_IMPORT_FORMATS.items()],
                        "projects": {key: value["label"] for key, value in vega.tasks.items() if key != "coordinator"},
                        "max_upload_bytes": MEMORY_UPLOAD_LIMIT,
                        "online_connections": [],
                    }, ensure_ascii=False))
                if route == "/api/planning":
                    return self.reply(200, json.dumps(vega.companion.planning()))
                if route == "/api/calendar":
                    return self.reply(200, json.dumps(vega.calendar_occurrences(float(query["from"][0]), float(query["to"][0]))))
                if route == "/api/export":
                    return self.reply(200, json.dumps(vega.companion.export(), ensure_ascii=False))
            except DesktopUnavailable:
                return self.reply(503, '{"error":"Open Codex on your laptop to reconnect."}')
            except VscodeAgentsUnavailable as error:
                return self.reply(503, json.dumps({"error": str(error)}))
            except AntigravityUnavailable:
                return self.reply(503, '{"error":"Antigravity is not available. Open its window and retry."}')
            except DeepSeekUnavailable as error:
                return self.reply(503, json.dumps({"error": str(error)}))
            except ConflictError as error:
                return self.reply(409, json.dumps({"error": str(error)}))
            except PermissionError:
                return self.reply(403, '{"error":"The paired owner is required."}')
            except (ValueError, KeyError, SpeechError) as error:
                return self.reply(400, json.dumps({"error": redact(str(error))}))
            return self.reply(404, '{"error":"Not found"}')

        def do_POST(self):
            if (
                self.headers.get("Host") != host or self.headers.get("Origin") != origin
                or not hmac.compare_digest(self.headers.get("X-Vega-CSRF", ""), csrf)
            ):
                return self.reply(403, '{"error":"Use the local Vega interface"}')
            if self.path == "/api/project-control/vscode-agents/chat-prompts":
                return self.reply(405, '{"error":"VS Code Agent Host access is read-only."}')
            try:
                length = int(self.headers.get("Content-Length", "0"))
                maximum = {
                    "/api/voice/finish-local": 2 * 1024 * 1024,
                    "/api/attachments": 14 * 1024 * 1024,
                    "/api/project-control/chat-attachments": 14 * 1024 * 1024,
                    "/api/memory/import/preview": 29 * 1024 * 1024,
                    # A complete reviewed selection can contain 2,000 record IDs.
                    "/api/memory/import/commit": 128 * 1024,
                }.get(self.path, 50000)
                if self.path.startswith("/api/project-control/requests"):
                    maximum = 128 * 1024
                if self.path.startswith("/api/project-tools/"):
                    maximum = 1024 * 1024
                if self.path.startswith("/api/copilot/"):
                    maximum = 128 * 1024
                if self.path.startswith("/api/voice/owner/"):
                    maximum = 2 * 1024 * 1024
                if not 0 < length <= maximum:
                    raise ValueError("Invalid message request.")
                payload = json.loads(self.rfile.read(length))
                if not isinstance(payload, dict):
                    raise ValueError("The message must be an object.")
                route = self.path
                if not vega.memory_release and (
                    route.startswith("/api/memory/import/") or route in ("/api/memory/candidate/review", "/api/memory/undo")
                ):
                    return self.reply(404, '{"error":"Reviewed memory upgrade is not released yet."}')
                if route == "/api/chat":
                    update_id = vega.submit_local(payload.get("text"), payload.get("request_id"), voice_receipt=payload.get("voice_receipt"),
                                                  attachment_ids=payload.get("attachment_ids"))
                    return self.reply(202, json.dumps({"accepted": True, "update_id": str(update_id)}))
                elif route == "/api/copilot/workspaces":
                    vega.refresh_registry()
                    result = vega.copilot_sessions.register_workspace(vega.store.get("owner_id"), payload)
                elif route == "/api/copilot/sessions":
                    vega.refresh_registry()
                    result = vega.copilot_sessions.create(vega.store.get("owner_id"), payload)
                elif re.fullmatch(r"/api/copilot/sessions/[a-f0-9-]{36}/messages", route):
                    result = vega.copilot_sessions.send(
                        vega.store.get("owner_id"), route.split("/")[-2], payload)
                elif re.fullmatch(r"/api/copilot/approvals/[a-f0-9-]{36}/(approve|reject)", route):
                    vega.refresh_registry()
                    identity, action = route.split("/")[-2:]
                    result = vega.copilot_sessions.decide_approval(
                        vega.store.get("owner_id"), identity, action, payload)
                elif route == "/api/infrastructure/refresh":
                    result=vega.infrastructure.refresh_cloud(vega.store.get("owner_id"),payload)
                elif route == "/api/attachments":
                    result=vega.attachments.stage(vega.store.get("owner_id"),payload)
                elif re.fullmatch(r"/api/attachments/[a-f0-9-]{36}/remove",route):
                    result=vega.attachments.remove(vega.store.get("owner_id"),route.split("/")[-2])
                elif route == "/api/team/runs":
                    result={"run":vega.team.create(vega.store.get("owner_id"),payload)}
                elif route == "/api/n8n/connect":
                    result=vega.n8n.connect(vega.store.get("owner_id"),payload)
                elif route == "/api/n8n/disconnect":
                    if payload:raise ValueError("Disconnect takes no credential fields.")
                    result=vega.n8n.disconnect(vega.store.get("owner_id"))
                elif route == "/api/n8n/drafts":
                    result=vega.n8n.prepare(vega.store.get("owner_id"),payload)
                elif re.fullmatch(r"/api/n8n/drafts/[a-f0-9-]{36}/(?:publish|receipt)",route):
                    identity,action=route.split("/")[-2:]
                    if action=="receipt" and payload:raise ValueError("Receipt checks have no action fields.")
                    result=vega.n8n.publish(vega.store.get("owner_id"),identity,payload) if action=="publish" else vega.n8n.reconcile(vega.store.get("owner_id"),identity)
                elif re.fullmatch(r"/api/team/runs/[a-f0-9-]{36}/(?:cancel|apply|start|check)",route):
                    identity,action=route.split("/")[-2:]
                    owner=vega.store.get("owner_id")
                    result={"run":vega.team.cancel(owner,identity) if action=="cancel" else
                            vega.team.start_once(owner,identity,payload) if action=="start" else
                            vega.team.check(owner,identity,payload) if action=="check" else
                            vega.team.apply(owner,identity,payload)}
                elif route in ("/api/project-tools/inspect", "/api/project-tools/search", "/api/project-tools/read",
                               "/api/project-tools/propose", "/api/project-tools/suggest"):
                    vega.refresh_registry()
                    action = route.rsplit("/", 1)[-1]
                    if action == "suggest":
                        result = vega.project_tools.suggest(
                            vega.store.get("owner_id"), payload, vega.brain.suggest_file_change)
                    else:
                        result = getattr(vega.project_tools, action)(vega.store.get("owner_id"), payload)
                elif re.fullmatch(r"/api/project-tools/proposals/[^/]+/(apply|cancel|undo)", route):
                    vega.refresh_registry()
                    identity, action = route.split("/")[-2:]
                    result = getattr(vega.project_tools, action)(vega.store.get("owner_id"), identity, payload)
                elif route == "/api/project-control/chat-prompts":
                    result = {"receipt": vega.desktop_workspace.send(vega.store.get("owner_id"), payload)}
                elif route == "/api/project-control/antigravity/chat-prompts":
                    result = {"receipt": vega.antigravity_workspace.send(vega.store.get("owner_id"), payload)}
                elif route == "/api/project-control/deepseek/chat-prompts":
                    result = {"receipt": vega.deepseek_workspace.send(vega.store.get("owner_id"), payload)}
                elif route == "/api/project-control/chat-attachments":
                    result = vega.desktop_workspace.stage_attachment(vega.store.get("owner_id"), payload)
                elif re.fullmatch(r"/api/project-control/chat-attachments/[a-f0-9-]{36}/remove", route):
                    result = vega.desktop_workspace.remove_attachment(vega.store.get("owner_id"), route.split("/")[-2], payload)
                elif route == "/api/project-control/requests":
                    result = {"request": vega.project_requests.create(
                        vega.store.get("owner_id"), "local_owner_ui", payload.get("request_key"),
                        payload.get("text", ""), payload.get("targets", []))}
                elif re.fullmatch(r"/api/project-control/requests/[^/]+/(answer|cancel|steer)", route):
                    identity, action = route.split("/")[-2:]
                    if action == "steer":
                        result = {"request": vega.project_requests.steer(
                            vega.store.get("owner_id"), identity, payload, source="local_owner_ui")}
                    elif action == "answer":
                        result = {"request": vega.project_requests.answer(
                            vega.store.get("owner_id"), identity, payload.get("text"), payload.get("targets"),
                            payload.get("expected_revision"))}
                    else:
                        result = {"request": vega.project_requests.cancel(
                            vega.store.get("owner_id"), identity, payload.get("expected_revision"))}
                elif route == "/api/chat/cancel":
                    result = vega.cancel_local(payload.get("update_id"))
                elif route == "/api/preferences":
                    result = vega.companion.save_preferences(payload.get("values"), payload.get("expected_revision"))
                elif route == "/api/context":
                    result = vega.companion.save_context(payload)
                elif route == "/api/memory":
                    result = vega.companion.save_memory(payload)
                elif route == "/api/memory/archive":
                    result = vega.companion.archive_memory(payload.get("id"), payload.get("expected_revision"))
                elif route == "/api/memory/candidate/review":
                    result = vega.companion.memory.review_candidate(payload.get("id"), payload.get("accept"))
                elif route == "/api/memory/undo":
                    result = vega.companion.memory.undo(payload.get("event_id"))
                elif route == "/api/memory/import/preview":
                    encoded = payload.get("data_base64")
                    if not isinstance(encoded, str) or len(encoded) > ((MEMORY_UPLOAD_LIMIT + 2) // 3) * 4:
                        raise ValueError("Choose an export file of at most 20 MB.")
                    data = base64.b64decode(encoded, validate=True)
                    result = vega.memory_imports.preview(
                        data, payload.get("filename"), payload.get("provider"),
                        payload.get("project"), payload.get("source_label"), payload.get("mode", "suggestions"),
                    )
                elif route == "/api/memory/import/commit":
                    result = vega.memory_imports.commit(payload.get("preview_id"), payload.get("selected_ids"))
                elif route == "/api/memory/import/cancel":
                    result = vega.memory_imports.cancel(payload.get("preview_id"))
                elif route == "/api/memory/import/undo":
                    result = vega.companion.memory.undo_import(payload.get("batch_id"))
                elif route == "/api/planning":
                    result = vega.companion.save_plan(payload)
                elif route == "/api/planning/complete":
                    result = vega.companion.complete_plan(payload.get("id"), payload.get("expected_revision"), payload.get("completed"))
                elif route == "/api/planning/archive":
                    result = vega.companion.archive_plan(payload.get("id"), payload.get("expected_revision"))
                elif route == "/api/briefing/ack":
                    current = vega.briefing()["briefing"]["items"]
                    if not any(item["signalKey"] == payload.get("signalKey") and item["revision"] == payload.get("revision") for item in current):
                        raise ConflictError("This briefing changed. Refresh before marking it reviewed.")
                    result = vega.companion.acknowledge_briefing(payload)
                elif route == "/api/voice/prepare":
                    context = vega.companion.context()
                    result = vega.speech.prepare_capture(
                        context["revision"], vega.companion.preferences()["revision"], context,
                        mode=payload.get("mode", "automatic"),
                    )
                elif route.startswith("/api/voice/owner/"):
                    result = vega.owner_voice_action(route.removeprefix("/api/voice/owner/"), payload)
                elif route == "/api/voice/config/propose":
                    result = vega.speech.propose_config(payload.get("values"), payload.get("expected_revision"))
                elif route == "/api/voice/config/commit":
                    result = vega.speech.commit_config(payload.get("proposal_id"))
                elif route == "/api/voice/config/discard":
                    result = vega.speech.discard_proposal(payload.get("proposal_id"))
                elif route == "/api/voice/test/prepare":
                    result = vega.speech.prepare_test(payload.get("kind"), payload.get("expected_revision"), vega.companion.preferences()["revision"])
                elif route == "/api/voice/test/run":
                    result = vega.speech.execute_test(payload.get("test_id"), lambda: vega.companion.preferences()["revision"])
                elif route == "/api/voice/test/stop":
                    result = vega.speech.cancel_test(payload.get("test_id"))
                elif route == "/api/voice/finish-local":
                    result = vega.speech.finish_local_capture(
                        payload, vega._voice_pcm(payload), payload.get("context_revision"),
                        vega.companion.preferences()["revision"],
                    )
                elif route.startswith("/api/voice/stream/"):
                    command = route.rsplit("/", 1)[-1]
                    if command not in ("begin", "push", "finish", "cancel"):
                        raise ValueError("Unsupported voice operation.")
                    result = vega.speech.stream(
                        "cancel" if command == "cancel" else "live_" + command,
                        payload, payload.get("context_revision"), vega.companion.preferences()["revision"],
                    )
                elif route == "/api/voice/speak":
                    requested = str(payload.get("update_id", ""))
                    message = next((item for item in reversed(vega.history()) if str(item["update_id"]) == requested and item["role"] == "assistant"), None)
                    if not message:
                        raise ValueError("Choose an existing Vega reply to speak.")
                    preference_revision = vega.companion.preferences()["revision"]
                    spoken = message.get("spoken_text")
                    if spoken is None:
                        spoken = select_spoken_reply(message["text"])
                    audio = vega.speech.synthesize(spoken, source="local-playback")
                    if preference_revision != vega.companion.preferences()["revision"]:
                        raise SpeechError("Your voice preferences changed before playback. The audio was discarded.")
                    result = {"audio": base64.b64encode(audio).decode(), "mime": "audio/mpeg"}
                else:
                    return self.reply(404, '{"error":"Not found"}')
                return self.reply(200, json.dumps(result))
            except DesktopUnavailable:
                return self.reply(503, '{"error":"Open Codex on your laptop to reconnect."}')
            except VscodeAgentsUnavailable as error:
                return self.reply(503, json.dumps({"error": str(error)}))
            except AntigravityUnavailable:
                return self.reply(503, '{"error":"Antigravity is not available. Open its window and retry."}')
            except DeepSeekUnavailable as error:
                return self.reply(503, json.dumps({"error": str(error)}))
            except PermissionError as error:
                message = str(error) if self.path.startswith("/api/project-tools/") else "The paired owner is required."
                return self.reply(403, json.dumps({"error": message}))
            except ConflictError as error:
                return self.reply(409, json.dumps({"error": str(error)}))
            except ConversationError as error:
                return self.reply(502, json.dumps({"error": redact(str(error))}))
            except (ValueError, TypeError, SpeechError) as error:
                return self.reply(400, json.dumps({"error": str(error)}))
            except OSError:
                return self.reply(400, '{"error":"The local file could not be accessed. Refresh its current state."}')

    return Handler


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int)
    args = parser.parse_args()
    settings = json.loads((ROOT / "settings.json").read_text(encoding="utf-8"))
    for setting, environment in (
        ("infrastructure_aws_account_id", "VEGA_AWS_ACCOUNT_ID"),
        ("infrastructure_aws_profile", "VEGA_AWS_PROFILE"),
        ("infrastructure_cloudflare_account_id", "VEGA_CLOUDFLARE_ACCOUNT_ID"),
        ("infrastructure_wrangler_auth_file", "VEGA_WRANGLER_AUTH_FILE"),
    ):
        value = settings.get(setting)
        if isinstance(value, str) and value.strip():
            os.environ[environment] = value.strip()
    workspace_root = get_vega_workspace_root(settings)
    workspace_root.mkdir(parents=True, exist_ok=True)
    settings["project_tool_workspaces"] = {"vega": {"root": str(workspace_root), "can_write": True}}
    port = args.port or settings["port"]
    mutex = None
    if os.name == "nt":
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.CreateMutexW.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_wchar_p]
        kernel.CreateMutexW.restype = ctypes.c_void_p
        kernel.CloseHandle.argtypes = [ctypes.c_void_p]
        mutex = kernel.CreateMutexW(None, False, "Local\\VegaIndependent-" + hashlib.sha256(str(ROOT).encode()).hexdigest()[:24])
        if not mutex:
            raise SystemExit("The Vega instance lock is unavailable.")
        if ctypes.get_last_error() == 183:
            kernel.CloseHandle(mutex)
            raise SystemExit("Vega is already running. Open its existing local interface.")
    # Bind before touching runtime state. An accidental second launch cannot mark
    # the first host's active replies interrupted before failing on the port.
    try:
        server = ExclusiveLoopbackServer(("127.0.0.1", port), BaseHTTPRequestHandler)
    except OSError:
        if mutex:
            kernel.CloseHandle(mutex)
        raise SystemExit("The Vega address is already in use. The existing host was preserved.") from None
    try:
        store = Store(ROOT / ".state")
        vega = VegaRelay(store, load_tasks(ROOT / "tasks.json"), settings)
        vega.miniapp.admin_port = port
        server.RequestHandlerClass = make_handler(vega, port)
    except Exception:
        server.server_close()
        if mutex:
            kernel.CloseHandle(mutex)
        raise
    server.daemon_threads = True
    store.set("process_id", os.getpid())
    store.set("host_kind", "standalone-vega")
    store.set("milestone_delivery_host_pid", os.getpid())
    store.set("milestone_evidence_guard_host_pid", os.getpid())
    with store.db() as db:
        text_delivery.recover_interrupted(db)
        db.execute("UPDATE outbox SET state='uncertain',error='Host stopped before receipt was recorded' WHERE state IN ('sending','executing')")
    vega.supervisor = WorkerSupervisor(store.directory, STOP, vega.recover_worker)
    for name, target, arguments in [
        ("telegram-poll", vega.poll_loop, ()),
        ("delivery", vega.delivery_loop, ()),
        ("documents", vega.document_loop, ()),
        ("voice-input", vega.voice_input_loop, ()),
        ("voice-output", vega.voice_output_loop, ()),
        ("companion", vega.companion_loop, ()),
        ("task-scan", vega.scan_loop, (Path.home() / ".codex" / "sessions",)),
        ("conversation", vega.conversation_loop, ()),
        ("team", vega.team_loop, ()),
        ("watchdog", vega.watchdog_loop, ()),
        ("miniapp", vega.miniapp.run, ()),
        ("n8n", vega.n8n.run, ()),
    ]:
        vega.supervisor.start(name, target, arguments, daemon=name != "miniapp")
    try:
        server.serve_forever(poll_interval=0.5)
    finally:
        STOP.set()
        server.server_close()
        vega.close()
        vega.miniapp.close()
        vega.n8n.close()
        miniapp_thread = vega.supervisor.threads.get("miniapp")
        if miniapp_thread is not None:
            miniapp_thread.join(timeout=30)
        if mutex:
            kernel.CloseHandle(mutex)


if __name__ == "__main__":
    main()
