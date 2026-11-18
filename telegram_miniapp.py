"""Separate owner-authenticated project gateway; never exposes the local admin API."""
from __future__ import annotations

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import re
import secrets
import sqlite3
import subprocess
import threading
import time
import urllib.request
from urllib.parse import urlsplit, unquote

from companion import ConflictError
from telegram_mini_auth import MiniAppAuthError, verify_telegram_init_data
from http_runtime import ExclusiveLoopbackServer
from project_freshness import project_response
from work_references import preview_reference


ROOT = Path(__file__).resolve().parent
MINIAPP_VISUAL_ASSETS = {
    "/mini/visuals.js": ("visuals.js", "text/javascript"),
    "/mini/visuals.css": ("visuals.css", "text/css"),
}
MINIAPP_VISUAL_FILES = tuple("public/miniapp/" + name for name, _ in MINIAPP_VISUAL_ASSETS.values())


def miniapp_visual_assets(settings):
    assets = dict(MINIAPP_VISUAL_ASSETS)
    names = settings.get("miniapp_brand_assets", [])
    if type(names) is not list or len(names) > 100:
        raise ValueError("The Mini App brand asset configuration is invalid.")
    for name in names:
        if not isinstance(name, str) or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,99}\.(?:svg|png)", name, re.I) is None:
            raise ValueError("The Mini App brand asset configuration is invalid.")
        kind = "image/svg+xml" if name.lower().endswith(".svg") else "image/png"
        assets[f"/mini/brands/{name}"] = (f"brands/{name}", kind)
    if len({name.casefold() for name in names}) != len(names):
        raise ValueError("The Mini App brand asset configuration contains duplicates.")
    return assets


def spawn_owned_process(*arguments, **options):
    # The optional gateway is disabled until configured. Its Windows job
    # launcher is loaded only when starting that owned gateway.
    from process_lifetime import spawn_owned_process as launch
    return launch(*arguments, **options)


def miniapp_handler(vega, *, token_provider=None, clock=time.time, instance_id=None):
    token_provider = token_provider or (lambda: getattr(vega.api, "token", None))

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def reply(self, status, value, kind="application/json"):
            content = value if isinstance(value, bytes) else (
                json.dumps(value, ensure_ascii=False).encode("utf-8") if kind == "application/json"
                else value.encode("utf-8"))
            self.send_response(status)
            self.send_header("Content-Type", kind + "; charset=utf-8")
            self.send_header("Content-Length", str(len(content)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("Permissions-Policy", "microphone=(), camera=(), geolocation=()")
            self.send_header("Content-Security-Policy",
                             "default-src 'self'; script-src 'self' https://telegram.org; style-src 'self' 'unsafe-inline'; "
                             "connect-src 'self'; img-src 'self' data: blob:; frame-ancestors https://web.telegram.org; "
                             "base-uri 'none'; form-action 'self'")
            self.end_headers()
            try:
                self.wfile.write(content)
            except (BrokenPipeError, ConnectionResetError):
                pass

        def principal(self):
            raw = self.headers.get("X-Telegram-Init-Data", "")
            if not raw or len(raw) > 16384:
                raise MiniAppAuthError("Invalid Telegram Mini App authentication.")
            token = token_provider()
            owner = vega.store.get("owner_id")
            return verify_telegram_init_data(raw, token, owner, now=clock())

        def do_GET(self):
            path = urlsplit(self.path).path
            visual_assets = miniapp_visual_assets(vega.settings)
            if path in visual_assets:
                name, kind = visual_assets[path]
                return self.reply(200, (ROOT / "public" / "miniapp" / name).read_bytes(), kind)
            static = {"/": "index.html", "/mini": "index.html", "/mini/": "index.html",
                      "/mini/app.js": "app.js", "/mini/style.css": "style.css",
                      "/mini/workspace.js": "workspace.js",
                      "/mini/vega-icon.svg": "vega-icon.svg"}
            team_assets={"/mini/team-panel.js":"team-panel.js","/mini/team-panel.css":"team-panel.css",
                         "/composer-attachments.js":"composer-attachments.js",
                         "/mini/request-receipts.js":"request-receipts.js",
                         "/request-receipts.js":"request-receipts.js"}
            if path in team_assets:
                name=team_assets[path]
                return self.reply(200,(ROOT/"public"/name).read_bytes(),
                                  "text/javascript" if name.endswith(".js") else "text/css")
            if path == "/mini/health":
                value = {"service": "vega-projects", "version": 1, "auth_required": True}
                if instance_id is not None:
                    value["instance_id"] = instance_id
                return self.reply(200, value)
            if path in static:
                name = static[path]
                kind = "text/html" if name.endswith(".html") else "text/javascript" if name.endswith(".js") else "image/svg+xml" if name.endswith(".svg") else "text/css"
                return self.reply(200, (ROOT / "public" / "miniapp" / name).read_bytes(), kind)
            if not path.startswith("/mini/api/"):
                return self.reply(404, {"error": "Not found."})
            try:
                owner = self.principal()["user_id"]
                if path == "/mini/api/workspace":
                    return self.reply(200, vega.desktop_workspace.snapshot(owner))
                if re.fullmatch(r"/mini/api/chats/[a-f0-9-]{36}", path):
                    return self.reply(200, vega.desktop_workspace.read(owner, path.rsplit("/", 1)[-1]))
                if path == "/mini/api/chat-prompts":
                    return self.reply(200, {"receipts": vega.desktop_workspace.receipts(owner)})
                if path == "/mini/api/team":
                    return self.reply(200,vega.team.catalog(owner))
                if re.fullmatch(r"/mini/api/team/projects/[a-z0-9-]+",path):
                    return self.reply(200,vega.team.catalog(owner,project=path.rsplit("/",1)[-1]))
                if re.fullmatch(r"/mini/api/team/runs/[a-f0-9-]{36}",path):
                    return self.reply(200,{"run":vega.team.get(owner,path.rsplit("/",1)[-1])})
                if path == "/mini/api/projects":
                    return self.reply(200, project_response(vega.project_views(owner), now=clock()))
                if re.fullmatch(r"/mini/api/projects/[a-z0-9-]+", path):
                    return self.reply(200, project_response(
                        vega.project_views(owner, path.rsplit("/", 1)[-1]), now=clock()))
                if path == "/mini/api/requests":
                    return self.reply(200, {"requests": vega.project_requests.list(owner), "snapshot_at": clock()})
                reference = re.fullmatch(r"/mini/api/requests/([a-f0-9-]{36})/references/([^/]{1,1536})", path)
                if reference:
                    token = unquote(reference[2])
                    if len(token) > 512:
                        raise ValueError("Reference is too long.")
                    return self.reply(200, preview_reference(
                        owner, reference[1], token, requests=vega.project_requests, reports=vega.project_reports,
                        tasks=vega.tasks, activity=vega.project_activity))
                if re.fullmatch(r"/mini/api/requests/[a-f0-9-]{36}", path):
                    return self.reply(200, {
                        "request": vega.project_requests.get(owner, path.rsplit("/", 1)[-1]), "snapshot_at": clock(),
                    })
                return self.reply(404, {"error": "Not found."})
            except (MiniAppAuthError, PermissionError):
                return self.reply(401, {"error": "Open this Mini App from the paired Vega Telegram account."})
            except ConflictError as error:
                return self.reply(409, {"error": str(error)})
            except (ValueError, TypeError):
                return self.reply(400, {"error": "The project or request is unavailable. Refresh the project list."})
            except (OSError, sqlite3.Error, RuntimeError):
                return self.reply(503, {
                    "error": "Project information could not be refreshed. Last known information and drafts are kept.",
                    "retryable": True, "snapshot_at": clock(),
                })

        def do_POST(self):
            path = urlsplit(self.path).path
            team_path=path=="/mini/api/team/runs" or re.fullmatch(r"/mini/api/team/runs/[a-f0-9-]{36}/(?:cancel|apply)",path)
            attachment_path = path == "/mini/api/chat-attachments" or re.fullmatch(r"/mini/api/chat-attachments/[a-f0-9-]{36}/remove", path)
            if not (attachment_path or team_path or path in ("/mini/api/requests", "/mini/api/chat-prompts") or re.fullmatch(r"/mini/api/requests/[a-f0-9-]{36}/(?:answer|cancel|steer)", path)):
                return self.reply(404, {"error": "Not found."})
            try:
                owner = self.principal()["user_id"]
                length = int(self.headers.get("Content-Length", "0"))
                if not 0 < length <= (14 * 1024 * 1024 if path == "/mini/api/chat-attachments" else 128 * 1024):
                    raise ValueError("Invalid request size.")
                payload = json.loads(self.rfile.read(length))
                if not isinstance(payload, dict):
                    raise ValueError("Use a project request object.")
                if path == "/mini/api/chat-prompts":
                    return self.reply(200, {"receipt": vega.desktop_workspace.send(owner, payload)})
                if attachment_path:
                    return self.reply(200, vega.desktop_workspace.stage_attachment(owner, payload)
                                      if path == "/mini/api/chat-attachments" else
                                      vega.desktop_workspace.remove_attachment(owner, path.split("/")[-2], payload))
                if team_path:
                    if path=="/mini/api/team/runs":
                        run=vega.team.create(owner,payload,source="telegram_miniapp")
                    else:
                        identity,action=path.split("/")[-2:]
                        run=vega.team.cancel(owner,identity) if action=="cancel" else vega.team.apply(owner,identity,payload)
                    return self.reply(200,{"run":run})
                if path == "/mini/api/requests":
                    request = vega.project_requests.create(
                        owner, "telegram_miniapp", payload.get("request_key"),
                        payload.get("text", ""), payload.get("targets", []))
                else:
                    identity, action = path.split("/")[-2:]
                    if action == "steer":
                        request = vega.project_requests.steer(owner, identity, payload)
                    elif action == "answer":
                        request = vega.project_requests.answer(
                            owner, identity, payload.get("text"), payload.get("targets"), payload.get("expected_revision"))
                    else:
                        request = vega.project_requests.cancel(owner, identity, payload.get("expected_revision"))
                return self.reply(200, {"request": request})
            except (MiniAppAuthError, PermissionError):
                return self.reply(401, {"error": "Open this Mini App from the paired Vega Telegram account."})
            except ConflictError as error:
                return self.reply(409, {"error": str(error)})
            except (ValueError, TypeError, UnicodeError, RecursionError) as error:
                return self.reply(400, {"error": str(error) if attachment_path and isinstance(error, ValueError)
                                      else "The project request is invalid. Refresh and review its fields."})
            except (OSError, sqlite3.Error, RuntimeError):
                return self.reply(503, {"error": "Receipt unavailable. Refresh before retrying the same request."})

    return Handler


class MiniAppManager:
    """Windows-owned gateway/tunnel lifecycle. Contains no provider/model calls."""
    def __init__(self, vega, stop, *, executable=None):
        self.vega, self.stop_event = vega, stop
        self.directory = vega.store.directory
        self.config_path = self.directory / "miniapp-config.json"
        self.executable = Path(executable or r"C:\Program Files (x86)\cloudflared\cloudflared.exe")
        self.lock = threading.RLock()
        self.launch_lock = threading.RLock(); self.lifecycle_generation = 0
        self.instance_id = None; self.admin_port = vega.settings.get("port", 8769)
        self.server = None; self.server_thread = None; self.process = None
        self.cleanup_pending = False
        self.public_url = None; self.menu_configured = False; self.error = None
        self.menu_owner = None; self.public_healthy = False; self.last_health_check = 0
        self.last_attempt = 0; self.last_menu_attempt = 0; self.attempts = []
        self.enabled = False; self.port = 8771

    def status(self):
        with self.lock:
            running = self.process is not None and self.process.poll() is None
            return {
                "enabled": self.enabled, "state": "unavailable" if self.cleanup_pending else
                    "ready" if running and self.menu_configured and self.public_healthy else
                    "starting" if running else "unavailable" if self.enabled else "disabled",
                "url": self.public_url + "/mini/" if self.public_url and running else None,
                "menu_configured": self.menu_configured and running,
                "temporary_tunnel": True, "error": self.error,
            }

    def _read_tunnel(self, process):
        try:
            while process.poll() is None:
                line = process.stdout.readline(8192)
                if not line:
                    break
                match = re.search(r"https://[a-z0-9-]+\.trycloudflare\.com\b", line)
                if match:
                    with self.lock:
                        if self.process is process and not self.cleanup_pending and not self.stop_event.is_set() and self.public_url is None:
                            self.public_url = match[0]
        except (OSError, ValueError):
            pass  # Closing the owned process can close its output pipe.
        finally:
            if process.stdout:
                process.stdout.close()

    def _start(self):
        with self.launch_lock:
            if self.stop_event.is_set():
                return
            self._start_locked()

    def _start_locked(self):
        if self.process is not None:
            if self.cleanup_pending or self.process.poll() is None:
                raise RuntimeError("The owned tunnel must finish cleanup before another can start.")
            self._terminate(self.process)
            with self.lock:
                self.process = None
        if not self.executable.is_file():
            raise RuntimeError("The installed Cloudflare tunnel client is unavailable.")
        if self.port == self.admin_port:
            raise RuntimeError("The Mini App must not use the private admin port.")
        if self.server is not None and self.server.server_address[1] != self.port:
            self.close()
        with self.lock:
            generation = self.lifecycle_generation
        if self.server is None:
            self.instance_id = secrets.token_urlsafe(24)
            server = ExclusiveLoopbackServer(("127.0.0.1", self.port), miniapp_handler(self.vega, instance_id=self.instance_id))
            server.daemon_threads = True
            thread = threading.Thread(target=server.serve_forever, name="vega-miniapp-http", daemon=True)
            self.server, self.server_thread = server, thread
            thread.start()
        with urllib.request.urlopen(f"http://127.0.0.1:{self.server.server_address[1]}/mini/health", timeout=3) as response:
            local_health = json.load(response)
        if local_health != self._health_identity():
            raise RuntimeError("The owned gateway identity could not be verified before starting a tunnel.")
        with self.lock:
            if generation != self.lifecycle_generation or self.stop_event.is_set():
                return
        # An explicit private config avoids adopting another project's tunnel
        # settings. No account credential or DNS setting is changed.
        config = self.directory / "miniapp-cloudflared.yml"
        if not config.exists():
            config.write_text("{}\n", encoding="utf-8")
        if config.read_text(encoding="utf-8").strip() != "{}":
            raise RuntimeError("The dedicated Mini App tunnel config changed; review it before starting.")
        allowed = {"SYSTEMROOT", "WINDIR", "PATH", "PATHEXT", "TEMP", "TMP", "USERPROFILE",
                   "APPDATA", "LOCALAPPDATA", "PROGRAMDATA", "PROGRAMFILES", "PROGRAMFILES(X86)",
                   "HOMEDRIVE", "HOMEPATH", "COMSPEC", "HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY"}
        environment = {key: value for key, value in os.environ.items() if key.upper() in allowed}
        process = spawn_owned_process(
            [str(self.executable), "tunnel", "--config", str(config), "--url", f"http://127.0.0.1:{self.server.server_address[1]}",
             "--no-autoupdate", "--loglevel", "info"],
            shell=False, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, encoding="utf-8", errors="replace", env=environment,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        )
        with self.lock:
            abandoned = generation != self.lifecycle_generation or self.stop_event.is_set()
            # Keep ownership even if shutdown began during creation. If
            # termination fails, close() must still be able to retry it.
            self.process = process
            if not abandoned:
                self.public_url = None; self.menu_configured = False; self.error = None
                self.public_healthy = False; self.menu_owner = None
        if abandoned:
            with self.lock:
                self.cleanup_pending = True
            self._terminate(process)
            with self.lock:
                self.process = None
                self.cleanup_pending = False
            return
        threading.Thread(target=self._read_tunnel, args=(process,), name="vega-miniapp-tunnel-output", daemon=True).start()

    def _health_identity(self):
        return {"service": "vega-projects", "version": 1, "auth_required": True, "instance_id": self.instance_id}

    def _check_health(self):
        with urllib.request.urlopen(self.public_url + "/mini/health", timeout=8) as response:
            health = json.load(response)
        if health != self._health_identity():
            self.close()
            raise RuntimeError("The public Mini App gateway did not match its health response.")
        self.public_healthy = True; self.last_health_check = time.time()
        self.error = None

    def _publish_menu(self):
        if not self.public_url or not self.vega.api:
            return
        self._check_health()
        with self.lock:
            generation = self.lifecycle_generation
            public_url = self.public_url
        if self.stop_event.is_set() or not public_url:
            return
        owner = self.vega.store.get("owner_id")
        if type(owner) is not int or self.vega.store.get("owner_chat_id") != owner:
            raise RuntimeError("A paired private owner is required.")
        button = {"type": "web_app", "text": "Projects", "web_app": {"url": public_url + "/mini/"}}
        def matches(value):
            return isinstance(value, dict) and value.get("type") == "web_app" and value.get("text") == "Projects" and \
                isinstance(value.get("web_app"), dict) and value["web_app"].get("url") == button["web_app"]["url"]
        current = self.vega.api.call("getChatMenuButton", chat_id=owner)
        if not matches(current):
            if generation != self.lifecycle_generation or self.stop_event.is_set():
                return
            if self.vega.store.get("owner_id") != owner or self.vega.store.get("owner_chat_id") != owner:
                raise RuntimeError("The paired owner changed before Mini App configuration.")
            self.vega.api.call("setChatMenuButton", chat_id=owner, menu_button=button)
        current = self.vega.api.call("getChatMenuButton", chat_id=owner)
        if not matches(current) or self.vega.store.get("owner_id") != owner:
            raise RuntimeError("Telegram did not confirm the Projects menu configuration.")
        with self.lock:
            if generation != self.lifecycle_generation or self.stop_event.is_set():
                return
            self.menu_configured = True; self.menu_owner = owner; self.error = None
        self.vega.store.set("miniapp_public_url", button["web_app"]["url"])
        self.vega.store.set("miniapp_menu_verified_at", time.time())

    def run(self):
        while not self.stop_event.is_set():
            try:
                config = json.loads(self.config_path.read_text("utf-8")) if self.config_path.exists() else {}
                self.enabled = config.get("enabled") is True
                self.port = config.get("port", 8771)
                if type(self.port) is not int or not 1024 <= self.port <= 65535:
                    raise ValueError("Invalid Mini App port.")
                if self.cleanup_pending:
                    self.close()
                if not self.enabled:
                    self.close()
                else:
                    now = time.time()
                    if self.menu_owner != self.vega.store.get("owner_id"):
                        self.menu_configured = False
                    if self.server is not None and self.server.server_address[1] != self.port:
                        self.close()
                    self.attempts = [item for item in self.attempts if now - item < 3600]
                    if self.process is None or self.process.poll() is not None:
                        if now - self.last_attempt >= 60 and len(self.attempts) < 3:
                            self.last_attempt = now; self.attempts.append(now)
                            self._start()
                        elif len(self.attempts) >= 3:
                            self.error = "The Mini App tunnel reached its restart limit. Review the connection."
                    if self.public_url and not self.menu_configured and now - self.last_menu_attempt >= 30:
                        self.last_menu_attempt = now
                        self._publish_menu()
                    elif self.public_url and self.menu_configured and now - self.last_health_check >= 60:
                        self.last_health_check = now
                        self._check_health()
            except Exception as error:
                with self.lock:
                    self.error = f"The Mini App connection needs attention ({type(error).__name__})."
                    self.public_healthy = False
            self.stop_event.wait(5)
        self.close()

    def close(self):
        with self.lock:
            self.lifecycle_generation += 1
        # A concurrent launch must finish registering or destroying its child
        # before shutdown is allowed to return.
        with self.launch_lock:
            with self.lock:
                process, server = self.process, self.server
                self.cleanup_pending = process is not None or server is not None
                self.public_url = None; self.menu_configured = False; self.menu_owner = None; self.public_healthy = False
            try:
                self._terminate(process)
                if server is not None:
                    server.shutdown(); server.server_close()
            except Exception:
                with self.lock:
                    self.error = "The owned Mini App connection has not finished cleanup."
                raise
            with self.lock:
                self.process = None; self.server = None; self.server_thread = None
                if self.cleanup_pending:
                    self.error = None
                self.cleanup_pending = False

    @staticmethod
    def _terminate(process):
        if process is None:
            return
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                process.kill(); process.wait(timeout=3)
        if process.poll() is None:
            raise RuntimeError("The owned tunnel exit has not been confirmed.")
        release = getattr(process, "release", None)
        if release is not None:
            release()
