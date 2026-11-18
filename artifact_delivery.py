"""Requested project artifacts, separate from the legacy reviewed ZIP/audio path."""
from __future__ import annotations

import hashlib
import io
import json
import os
from pathlib import Path
import re
import time

from automatic_context import owner_opening, requested_path
from companion import ConflictError
from local_attachments import media_type
from memory_policy import contains_secret
from owner_files import _file, _hash
from project_files import file_identity, text_from_bytes
from project_responses import ProjectResponses
from routing_targets import dispatch_target

MAX_BYTES = 10_000_000
MAX_CHOICES = 5
LIFETIME = 1800
SUFFIXES = {".png", ".jpg", ".jpeg", ".webp", ".pdf", ".txt", ".md", ".csv"}
MEDIA_WORDS = re.compile(r"\b(?:files?|photos?|pictures?|images?|designs?|visuals?|diagrams?|pdfs?|documents?|screenshots?)\b", re.I)
SEND = re.compile(r"\b(?:send|share|attach|show)\s+(?:(?:me|us)\s+)?", re.I)
NEGATIVE = re.compile(r"\b(?:don't|do not|never|not yet|hypothetically|example|transcript|quoted)\b", re.I)
CHOICES = re.compile(r"\b(?:send|share|attach|show)\s+(?:me\s+)?(?:(?:files?|images?|pictures?|photos?)\s+)?"
                     r"([1-5](?:\s*(?:,|and|&)\s*[1-5])*)(?:\s*[.!?])?$", re.I)


def send_requested(text):
    value = owner_opening(text)
    return bool(value and not NEGATIVE.search(value) and not contains_secret(value)
                and SEND.search(value) and (MEDIA_WORDS.search(value) or requested_path(text)
                                            or CHOICES.search(value)))


def payload(data, path):
    """Validate the actual bytes; never trust the filename's claimed content."""
    if not 0 < len(data) <= MAX_BYTES or Path(path).suffix.lower() not in SUFFIXES:
        raise ValueError("Supported artifacts are images, PDF or plain-text documents up to 10 MB.")
    kind, mime = media_type(data, Path(path).name, "")
    if kind == "image":
        if Path(path).suffix.lower() not in {".png", ".jpg", ".jpeg", ".webp"}:
            raise ValueError("The artifact's image type does not match its filename.")
        from PIL import Image
        with Image.open(io.BytesIO(data)) as image:
            width, height = image.size
        as_photo = (mime in {"image/png", "image/jpeg"} and width + height <= 10000
                    and max(width, height) / min(width, height) <= 20)
        # Upload the verified original bytes. Very wide images/WebP use the
        # document endpoint rather than silently changing the design.
        return data, Path(path).name, mime, "photo" if as_photo else "document"
    if mime == "application/pdf":
        if Path(path).suffix.lower() != ".pdf":
            raise ValueError("The PDF content does not match its filename.")
        from pypdf import PdfReader
        reader = PdfReader(io.BytesIO(data), strict=True)
        if reader.is_encrypted or len(reader.pages) > 100:
            raise ValueError("Encrypted PDFs and PDFs over 100 pages are unavailable.")
        for page in reader.pages:
            if contains_secret(page.extract_text() or ""):
                raise ValueError("This document contains excluded sensitive content.")
    elif Path(path).suffix.lower() in {".txt", ".md", ".csv"}:
        text_from_bytes(data)
        mime = "text/plain"
    else:
        raise ValueError("The file content is not a supported image or document.")
    return data, Path(path).name, mime, "document"


class ArtifactDelivery:
    def __init__(self, store, tasks, files, *, clock=time.time):
        self.store, self.tasks, self.files, self.clock = store, tasks, files, clock
        with store.db() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS artifact_searches (
                    owner INTEGER NOT NULL, origin_update_id INTEGER NOT NULL,
                    project TEXT NOT NULL, created_at REAL NOT NULL, result TEXT NOT NULL,
                    PRIMARY KEY(owner,origin_update_id)
                );
                CREATE TABLE IF NOT EXISTS outbound_artifacts (
                    event_id TEXT PRIMARY KEY, owner INTEGER NOT NULL, origin_update_id INTEGER NOT NULL,
                    project TEXT NOT NULL, thread_id TEXT NOT NULL, request_hash TEXT NOT NULL,
                    preference_revision INTEGER NOT NULL, binding TEXT NOT NULL, candidate TEXT NOT NULL,
                    source_sha256 TEXT NOT NULL, payload_sha256 TEXT NOT NULL,
                    mime TEXT NOT NULL, kind TEXT NOT NULL, created_at REAL NOT NULL
                );
            """)

    def _origin(self, db, owner, update_id):
        revision = ProjectResponses._principal(db, owner)
        row = ProjectResponses._origin(db, owner, update_id)
        context = json.loads(db.execute("SELECT payload FROM inbox_context WHERE update_id=?", (update_id,)).fetchone()["payload"])
        if context.get("origin") != "telegram_text" or context.get("forwarded_data"):
            raise PermissionError("File delivery needs a current typed request from the paired Telegram owner.")
        if not 0 <= self.clock() - row["received_at"] <= LIFETIME:
            raise ConflictError("This file request has expired. Send a fresh request when you want the file.")
        if not send_requested(row["text"]):
            raise PermissionError("The current owner message does not request an attachment.")
        return row, revision

    def search(self, owner, project, text, *, origin_update_id):
        if project not in self.tasks():
            raise ValueError("Which registered project's files should I check?")
        with self.store.db() as db:
            ProjectResponses._principal(db, owner)
        direct = requested_path(text)
        words = re.findall(r"[a-z0-9]+", owner_opening(text).lower().replace("hexagone", "hexagon"))
        ignored = set(("please send share attach show me us the latest newest current last pictures picture photos photo "
                       "images image designs design visuals visual files file documents document diagrams diagram "
                       "pdf screenshots screenshot find locate check look for get want need can could would you my "
                       "our desktop folder thread it its this that those these some or and etc hi vega").split())
        ignored.update(re.findall(r"[a-z0-9]+", project.lower() + " " + self.tasks()[project]["label"].lower()))
        keywords = [word for word in words if len(word) > 2 and word not in ignored][:12]
        query = "find " + " ".join(keywords or ["png", "jpg", "jpeg", "webp", "pdf"])
        binding = self.files.bind(owner, project, query, path=direct)
        if direct:
            candidates = [{"path": binding["path"], "root": binding["file_root"], "matched_words": []}]
            search = {"reasons": [], "visited": 0}
        else:
            candidates, search = self.files._discover(binding)
        choices = []
        for candidate in candidates:
            path = Path(candidate["path"])
            # A project operation cannot drift into a different granted project.
            if not path.is_relative_to(Path(binding["root"])) or path.suffix.lower() not in SUFFIXES:
                continue
            try:
                with _file(candidate["root"], path) as (_, info):
                    if not 0 < info.st_size <= MAX_BYTES:
                        continue
                    choices.append({"path": str(path), "root": candidate["root"], "identity": file_identity(info),
                                    "size": info.st_size, "mtime_ns": info.st_mtime_ns,
                                    "name": path.name, "relative_path": path.relative_to(Path(binding["root"])).as_posix(),
                                    "matches": len(candidate.get("matched_words", []))})
            except (OSError, ValueError):
                continue
        self.files.validate(owner, binding)
        choices.sort(key=lambda item: (-item["matches"], -item["mtime_ns"], item["relative_path"]))
        limited = len(choices) > MAX_CHOICES or any(reason in {"time_limit", "visit_limit", "match_limit"} for reason in search["reasons"])
        result = {"project": project, "origin_update_id": origin_update_id,
                  "binding": binding, "choices": choices[:MAX_CHOICES],
                  "limited": limited, "search": search, "created_at": self.clock()}
        with self.store.db() as db:
            ProjectResponses._principal(db, owner)
            existing = db.execute("SELECT result FROM artifact_searches WHERE owner=? AND origin_update_id=?",
                                  (owner, origin_update_id)).fetchone()
            if existing:
                return json.loads(existing["result"])
            db.execute("INSERT OR IGNORE INTO artifact_searches VALUES(?,?,?,?,?)",
                       (owner, origin_update_id, project, self.clock(), json.dumps(result)))
            saved = db.execute("SELECT result FROM artifact_searches WHERE owner=? AND origin_update_id=?",
                               (owner, origin_update_id)).fetchone()
        return json.loads(saved["result"])

    def previous_selection(self, owner, project, text):
        match = CHOICES.search(owner_opening(text))
        if not match:
            return None
        with self.store.db() as db:
            ProjectResponses._principal(db, owner)
            saved = db.execute("SELECT * FROM artifact_searches WHERE owner=? AND created_at>=? "
                               "ORDER BY created_at DESC LIMIT 1", (owner, self.clock() - LIFETIME)).fetchone()
        if not saved or project and saved["project"] != project:
            raise ValueError("Please search that project's files again before choosing a number.")
        result = json.loads(saved["result"])
        numbers = list(dict.fromkeys(int(n) for n in re.findall("[1-5]", match[1])))
        if any(number > len(result["choices"]) for number in numbers):
            raise ValueError("Choose one of the numbered files from the latest search.")
        self.files.validate(owner, result["binding"])
        return result, [result["choices"][number - 1] for number in numbers]

    def _bytes(self, owner, binding, candidate):
        self.files.validate(owner, binding)
        path = Path(candidate["path"])
        if not path.is_relative_to(Path(binding["root"])):
            raise PermissionError("This artifact is outside the requested project.")
        with _file(candidate["root"], path) as (stream, info):
            if (file_identity(info), info.st_size, info.st_mtime_ns) != (
                    candidate["identity"], candidate["size"], candidate["mtime_ns"]):
                raise ConflictError("The selected file changed. Search again before sending it.")
            data = stream.read(MAX_BYTES + 1)
            after = os.fstat(stream.fileno())
            if (after.st_size, after.st_mtime_ns) != (info.st_size, info.st_mtime_ns) or len(data) != info.st_size:
                raise ConflictError("The file changed while it was being read.")
        self.files.validate(owner, binding)
        content, name, mime, kind = payload(data, path)
        return data, content, name, mime, kind

    def queue(self, owner, update_id, result, choices):
        if not 1 <= len(choices) <= MAX_CHOICES:
            raise ValueError("Choose up to five matching files.")
        with self.store.db() as db:
            row, revision = self._origin(db, owner, update_id)
            existing = db.execute("SELECT event_id FROM outbound_artifacts WHERE owner=? AND origin_update_id=?",
                                  (owner, update_id)).fetchall()
            if existing:
                return [self.receipt(item["event_id"], owner) for item in existing]
        project, binding = result["project"], result["binding"]
        if project != binding.get("project"):
            raise PermissionError("The selected file does not belong to this project.")
        with self.store.db() as db:
            search = db.execute("SELECT result,created_at FROM artifact_searches WHERE owner=? AND origin_update_id=?",
                                (owner, result.get("origin_update_id"))).fetchone()
        if not search or json.loads(search["result"]) != result or not 0 <= self.clock() - search["created_at"] <= LIFETIME:
            raise ConflictError("The file selection expired or changed. Search again.")
        prepared = []
        for candidate in choices:
            if candidate not in result["choices"]:
                raise PermissionError("Only a file from the bounded search can be sent.")
            data, content, name, mime, kind = self._bytes(owner, binding, candidate)
            prepared.append((candidate, hashlib.sha256(data).hexdigest(),
                             hashlib.sha256(content).hexdigest(), len(content), name, mime, kind))
        events = []
        with self.store.db() as db:
            db.execute("BEGIN IMMEDIATE")
            latest, latest_revision = self._origin(db, owner, update_id)
            if latest_revision != revision or dict(latest) != dict(row):
                raise ConflictError("The owner request changed before attachment admission.")
            existing = db.execute("SELECT event_id FROM outbound_artifacts WHERE owner=? AND origin_update_id=?",
                                  (owner, update_id)).fetchall()
            if existing:
                return [self.receipt(item["event_id"], owner) for item in existing]
            self.files.validate(owner, binding)
            for candidate, source_hash, body_hash, size, name, mime, kind in prepared:
                event = f"artifact:{update_id}:{_hash(candidate)[:24]}"
                text = f"{self.tasks()[project]['label']} · {name}"
                db.execute("INSERT INTO outbound_artifacts VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                           (event, owner, update_id, project, dispatch_target(self.tasks(), project)["thread_id"],
                            _hash(row["text"]), revision, json.dumps(binding), json.dumps(candidate),
                            source_hash, body_hash, mime, kind, self.clock()))
                db.execute("INSERT INTO outbound_documents VALUES(?,?,?,?,?,?,NULL)",
                           (event, owner, candidate["path"], body_hash, size, name))
                db.execute("INSERT INTO outbox(event_id,task,text,created_at,state) VALUES(?,?,?,?,'document_queued')",
                           (event, project, text, self.clock()))
                events.append(event)
        return [self.receipt(event, owner) for event in events]

    def delivery_reason(self, db, row):
        saved = db.execute("SELECT * FROM outbound_artifacts WHERE event_id=?", (row["event_id"],)).fetchone()
        if not saved:
            return "The requested artifact has no verified admission."
        try:
            origin, revision = self._origin(db, saved["owner"], saved["origin_update_id"])
            candidate = json.loads(saved["candidate"])
            if (_hash(origin["text"]) != saved["request_hash"] or revision != saved["preference_revision"]
                    or dispatch_target(self.tasks(), saved["project"])["thread_id"] != saved["thread_id"]
                    or row["task"] != saved["project"]):
                return "The owner request, preferences or project changed."
            document = db.execute("SELECT filename,path FROM outbound_documents WHERE event_id=?", (row["event_id"],)).fetchone()
            if (not document or document["path"] != candidate["path"]
                    or row["text"] != f"{self.tasks()[saved['project']]['label']} · {document['filename']}"):
                return "The attachment identity or caption changed."
            self.files.validate(saved["owner"], json.loads(saved["binding"]))
        except (ValueError, RuntimeError, PermissionError, OSError, KeyError):
            return "The requested artifact's owner or project scope is no longer available."
        return None

    def verified(self, document):
        with self.store.db() as db:
            row = db.execute("SELECT * FROM outbox WHERE event_id=?", (document["event_id"],)).fetchone()
            reason = self.delivery_reason(db, row)
            saved = db.execute("SELECT * FROM outbound_artifacts WHERE event_id=?", (document["event_id"],)).fetchone()
            if reason:
                raise PermissionError(reason)
        data, content, name, mime, kind = self._bytes(saved["owner"], json.loads(saved["binding"]), json.loads(saved["candidate"]))
        if (hashlib.sha256(data).hexdigest() != saved["source_sha256"]
                or hashlib.sha256(content).hexdigest() != saved["payload_sha256"]
                or (len(content), name, saved["owner"]) != (document["size"], document["filename"], document["owner"])
                or (mime, kind) != (saved["mime"], saved["kind"])
                or document["sha256"] != saved["payload_sha256"]):
            raise ConflictError("The attachment bytes or delivery record changed. Nothing was uploaded.")
        return content, mime, kind

    def receipt(self, event, owner):
        with self.store.db() as db:
            ProjectResponses._principal(db, owner)
            row = db.execute("SELECT o.event_id,o.state,o.message_id,o.sent_at,o.error,d.filename,d.sha256 "
                             "FROM outbox o JOIN outbound_artifacts a ON a.event_id=o.event_id "
                             "JOIN outbound_documents d ON d.event_id=o.event_id WHERE o.event_id=? AND a.owner=?",
                             (event, owner)).fetchone()
            if not row:
                raise ValueError("This attachment receipt is unavailable.")
            return {**dict(row), "delivered": row["state"] == "sent" and type(row["message_id"]) is int and row["message_id"] > 0}

    @staticmethod
    def verify_response(document, result, kind):
        if (not isinstance(result, dict) or type(result.get("message_id")) is not int or result["message_id"] <= 0
                or not isinstance(result.get("chat"), dict)
                or result["chat"].get("id") != document["owner"] or result["chat"].get("type") != "private"):
            raise ValueError("The attachment has no matching private Telegram delivery receipt.")
        media = result.get("photo") if kind == "photo" else [result.get("document")]
        if not isinstance(media, list) or not media or any(
                not isinstance(item, dict) or not isinstance(item.get("file_id"), str) or not item["file_id"]
                for item in media):
            raise ValueError("Telegram did not return the expected attachment receipt.")

    def recent(self, owner, project=None):
        with self.store.db() as db:
            ProjectResponses._principal(db, owner)
            rows = db.execute("SELECT event_id FROM outbound_artifacts WHERE owner=? AND (? IS NULL OR project=?) "
                              "ORDER BY created_at DESC LIMIT 5", (owner, project, project)).fetchall()
        return [self.receipt(row["event_id"], owner) for row in rows]
