"""Reviewed ZIPs and optional source-bound documents for the paired owner.

Requested-file admission is an internal controller operation, never a model or
HTTP file tool. The pinned review below is deliberately not a general allowlist.
"""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
import hashlib
import io
import json
import os
from pathlib import Path
import re
import stat
import time
import zipfile


MAX_DOCUMENT_BYTES = 50_000_000
REQUESTED_FILE_PREFIX = "manual:requested-file:"


@dataclass(frozen=True)
class _RequestedFile:
    record_path: Path
    source_file: Path
    source_line: int
    source_line_sha256: str
    source_thread_id: str
    source_turn_id: str
    source_message_id: str
    source_cwd: Path
    observed_at: str
    owner_request_verbatim: str
    primary_file: Path
    root_identity: str
    file_identity: str
    size: int
    sha256: str
    delivery_task: str
    integration_owner: str
    requested_transport: str
    mime_type: str
    caption: str

    @property
    def event_id(self):
        # Neither a caller key, file hash nor recipient can create another
        # attempt for the same ordinary owner message.
        return REQUESTED_FILE_PREFIX + self.source_thread_id + ":" + self.source_message_id

    def source_fields(self):
        return {
            "source_file": str(self.source_file), "source_line": self.source_line,
            "source_line_sha256": self.source_line_sha256,
            "source_thread_id": self.source_thread_id, "observed_at": self.observed_at,
            "owner_request_verbatim": self.owner_request_verbatim,
            "primary_file": str(self.primary_file), "bytes": self.size, "sha256": self.sha256,
            "delivery_owner": self.delivery_task, "integration_owner": self.integration_owner,
            "requested_transport": self.requested_transport,
        }

    def binding(self, owner):
        return {
            **self.source_fields(), "version": 1, "owner": owner,
            "record_path": str(self.record_path), "source_turn_id": self.source_turn_id,
            "source_message_id": self.source_message_id, "source_cwd": str(self.source_cwd),
            "root_identity": self.root_identity, "file_identity": self.file_identity,
            "filename": self.primary_file.name, "mime_type": self.mime_type,
            "caption": self.caption,
        }


REQUESTED_FILE_CONFIG_PATH = Path(__file__).resolve().parent / ".state" / "requested-file.local.json"
_REQUESTED_FILE_FIELDS = {
    "record_path", "source_file", "source_line", "source_line_sha256",
    "source_thread_id", "source_turn_id", "source_message_id", "source_cwd",
    "observed_at", "owner_request_verbatim", "primary_file", "root_identity",
    "file_identity", "size", "sha256", "delivery_task", "integration_owner",
    "requested_transport", "mime_type", "caption",
}


def _load_requested_file():
    try:
        value = json.loads(REQUESTED_FILE_CONFIG_PATH.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    if type(value) is not dict or set(value) != _REQUESTED_FILE_FIELDS:
        raise ValueError("The local requested-file configuration is invalid.")
    path_fields = ("record_path", "source_file", "source_cwd", "primary_file")
    if any(not isinstance(value[key], str) or not Path(value[key]).is_absolute() for key in path_fields):
        raise ValueError("Requested-file paths must be absolute local paths.")
    string_fields = (
        "source_line_sha256", "source_thread_id", "source_turn_id", "source_message_id",
        "observed_at", "owner_request_verbatim", "root_identity", "file_identity",
        "sha256", "delivery_task", "integration_owner", "requested_transport",
        "mime_type", "caption",
    )
    if any(not isinstance(value[key], str) for key in string_fields):
        raise ValueError("The local requested-file review fields are invalid.")
    if (type(value["source_line"]) is not int or value["source_line"] <= 0
            or type(value["size"]) is not int or not 0 < value["size"] <= MAX_DOCUMENT_BYTES
            or re.fullmatch(r"[a-f0-9]{64}", value["source_line_sha256"]) is None
            or re.fullmatch(r"[a-f0-9]{64}", value["sha256"]) is None
            or re.fullmatch(r"[a-f0-9-]{36}", value["source_thread_id"]) is None
            or not all(isinstance(value[key], str) and value[key] for key in (
                "source_turn_id", "source_message_id", "observed_at",
                "owner_request_verbatim", "delivery_task", "integration_owner",
                "requested_transport", "mime_type", "caption", "root_identity",
                "file_identity"))
            or not re.fullmatch(r"\d+:\d+", value["root_identity"])
            or not re.fullmatch(r"\d+:\d+", value["file_identity"])
            or Path(value["primary_file"]).suffix.lower() != ".m4a"
            or value["mime_type"] != "audio/mp4"):
        raise ValueError("The local requested-file review fields are invalid.")
    return _RequestedFile(**{
        **value,
        **{key: Path(value[key]) for key in path_fields},
    })


_REQUESTED_FILE = _load_requested_file()


def _require_requested_file():
    if _REQUESTED_FILE is None:
        raise ValueError("No owner-requested file is configured for this installation.")
    return _REQUESTED_FILE


@contextmanager
def _requested_open(path, *, root_identity=None, expected_identity=None, appendable=False):
    """Hold checked ancestors and the actual file, including during a read."""
    if os.name != "nt":
        raise ValueError("Requested-file access requires the reviewed Windows host.")
    from project_files import ProjectFiles, _windows_handle, file_identity

    with ProjectFiles(path.parent, expected_identity=root_identity).open(directory=True):
        before = path.lstat()
        if (not stat.S_ISREG(before.st_mode) or before.st_nlink != 1
                or getattr(before, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT):
            raise ValueError("Requested files cannot be links or reparse points.")
        with _windows_handle(path, directory=False, appendable_read=appendable) as stream:
            opened = os.fstat(stream.fileno())
            identity = file_identity(opened)
            if identity != file_identity(before) or (expected_identity and identity != expected_identity):
                raise ValueError("The reviewed file was substituted.")
            yield stream
            after = os.fstat(stream.fileno())
            if (file_identity(after) != identity or file_identity(path.lstat()) != identity
                    or (not appendable and (opened.st_size, opened.st_mtime_ns) != (after.st_size, after.st_mtime_ns))):
                raise ValueError("The reviewed file changed during its read.")


def load_requested_source_record() -> dict:
    """Read only the pinned record; this takes no caller-controlled path."""
    spec = _require_requested_file()
    with _requested_open(spec.record_path) as stream:
        data = stream.read(32 * 1024 + 1)
    if len(data) > 32 * 1024:
        raise ValueError("The requested-file review record is too large.")
    record = json.loads(data)
    if type(record) is not dict:
        raise ValueError("A concrete reviewed source record is required.")
    return record


def _review_requested_source(record):
    spec = _require_requested_file()
    fields = spec.source_fields()
    # These descriptive peer fields do not grant authority or choose transport
    # parameters. In particular, changing stable_logical_key cannot replay.
    hints = {
        "checked_at", "suggested_caption", "stable_logical_key", "review_state",
        "current_supported_cli_gap", "no_queue_or_send_attempted",
        "do_not_relabel_as_another_project_or_wrap_to_bypass_scope",
        "requested_file_delivery_does_not_authorize_unrelated_messages_or_accounts",
        "actual_file_queued_or_sent", "dispatches", "independent_preflight",
        "native_handoff", "native_handoff_receipt", "state",
    }
    if type(record) is not dict or record.keys() - (fields.keys() | hints):
        raise ValueError("Unrecognized requested-file scope or transport fields.")
    if any(type(record.get(key)) is not type(value) or record[key] != value for key, value in fields.items()):
        raise ValueError("The owner source, reviewed path, content or delivery scope changed.")
    # Read only through the pinned line. Appending later chat events is safe;
    # a peer's review_state or a copied quotation is never source authority.
    with _requested_open(spec.source_file, appendable=True) as stream:
        total, first, raw = 0, None, b""
        for number in range(1, spec.source_line + 1):
            raw = stream.readline(1024 * 1024 + 1)
            total += len(raw)
            if not raw or len(raw) > 1024 * 1024 or total > 8 * 1024 * 1024:
                raise ValueError("The ordinary owner source is unavailable within its review bounds.")
            if number == 1:
                first = json.loads(raw)
    if hashlib.sha256(raw).hexdigest() != spec.source_line_sha256:
        raise ValueError("The original owner source line changed.")
    item = json.loads(raw)
    if not isinstance(item, dict) or not isinstance(first, dict):
        raise ValueError("The owner source contains invalid message records.")
    payload = item.get("payload", {})
    if not isinstance(payload, dict):
        raise ValueError("The owner source contains an invalid message.")
    metadata = payload.get("internal_chat_message_metadata_passthrough", {})
    session = first.get("payload", {}) if isinstance(first, dict) else {}
    if (not isinstance(session, dict) or not isinstance(metadata, dict)
            or first.get("type") != "session_meta" or session.get("id") != spec.source_thread_id
            or session.get("cwd") != str(spec.source_cwd)
            or item.get("type") != "response_item" or item.get("timestamp") != spec.observed_at
            or payload.get("type") != "message" or payload.get("role") != "user"
            or payload.get("id") != spec.source_message_id
            or metadata.get("turn_id") != spec.source_turn_id
            or metadata.get("content_item_kinds") != ["user.text"]
            or payload.get("content") != [{"type": "input_text", "text": spec.owner_request_verbatim}]):
        raise ValueError("The source is not the reviewed ordinary owner message.")


def _requested_data(stream):
    spec = _require_requested_file()
    data = stream.read(spec.size + 1)
    if len(data) != spec.size or hashlib.sha256(data).hexdigest() != spec.sha256:
        raise ValueError("The requested file's reviewed bytes changed.")
    # A conservative guard for this reviewed M4A, not a general media decoder.
    box_size = int.from_bytes(data[:4], "big")
    if (not 20 <= box_size <= len(data) or (box_size - 16) % 4
            or data[4:12] != b"ftypM4A "):
        raise ValueError("Only the reviewed M4A document type is supported.")
    return data


class OwnerDocuments:
    def __init__(self, store):
        self.store = store

    def queue(self, *, source, sha256, size, key, task, label):
        owner = self.store.get("owner_id")
        if type(owner) is not int or self.store.get("owner_chat_id") != owner:
            raise ValueError("A paired private owner is required.")
        if not isinstance(key, str) or not re.fullmatch(r"[A-Za-z0-9_.:-]{1,120}", key):
            raise ValueError("Provide a stable document delivery key.")
        if not isinstance(sha256, str) or not re.fullmatch(r"[a-fA-F0-9]{64}", sha256):
            raise ValueError("Provide the reviewed ZIP's SHA-256.")
        sha256 = sha256.lower()
        if type(size) is not int or not 0 < size <= MAX_DOCUMENT_BYTES:
            raise ValueError("The reviewed ZIP must be at most 50,000,000 bytes.")
        source = Path(source)
        if not source.is_absolute() or source.suffix.lower() != ".zip":
            raise ValueError("Provide the absolute path to the reviewed ZIP.")
        event_id = "manual:document:" + key
        with self.store.db() as db:
            existing = db.execute(
                "SELECT o.task,o.state,o.message_id,d.owner,d.sha256,d.size "
                "FROM outbox o LEFT JOIN outbound_documents d ON d.event_id=o.event_id WHERE o.event_id=?",
                (event_id,),
            ).fetchone()
        if existing:
            if (existing["task"], existing["owner"], existing["sha256"], existing["size"]) != (task, owner, sha256, size):
                raise ValueError("That delivery key already belongs to different reviewed content or scope.")
            return {"event_id": event_id, "state": existing["state"], "message_id": existing["message_id"], "created": False}
        source = source.resolve(strict=True)
        if not source.is_file() or source.stat().st_size != size:
            raise ValueError("The ZIP's size changed since review. Nothing was queued.")
        filename = source.name
        if len(filename) > 180 or any(character in filename for character in ('"', "\r", "\n", "\0")):
            raise ValueError("Use a ZIP filename without control characters.")
        with source.open("rb") as stream:
            data = stream.read(MAX_DOCUMENT_BYTES + 1)
        if len(data) != size or hashlib.sha256(data).hexdigest() != sha256:
            raise ValueError("The ZIP's bytes changed since review. Nothing was queued.")
        if not zipfile.is_zipfile(io.BytesIO(data)):
            raise ValueError("The reviewed file is not a valid ZIP container.")
        directory = (self.store.directory / "outgoing-documents").resolve()
        if not directory.is_relative_to(self.store.directory.resolve()):
            raise ValueError("The document directory is outside Vega's private state.")
        directory.mkdir(exist_ok=True)
        snapshot = directory / (sha256 + ".zip")
        if not snapshot.resolve().is_relative_to(directory):
            raise ValueError("The private snapshot path is invalid.")
        if snapshot.exists():
            if snapshot.stat().st_size != size or hashlib.sha256(snapshot.read_bytes()).hexdigest() != sha256:
                raise ValueError("An existing private snapshot changed. It was preserved; nothing was queued.")
        else:
            # Only reviewed bytes enter the snapshot; later project builds cannot
            # alter what the owner approved for this delivery.
            with snapshot.open("xb") as stream:
                stream.write(data)
        caption = (str(label) + " · reviewed ZIP\n" + filename)[:1000]
        with self.store.db() as db:
            db.execute("BEGIN IMMEDIATE")
            if db.execute("SELECT 1 FROM outbox WHERE event_id=?", (event_id,)).fetchone():
                raise ValueError("This delivery was admitted concurrently. Check its existing receipt.")
            db.execute(
                "INSERT INTO outbox(event_id,task,text,created_at,state) VALUES(?,?,?,?,'document_queued')",
                (event_id, task, caption, time.time()),
            )
            db.execute(
                "INSERT INTO outbound_documents(event_id,owner,path,sha256,size,filename) VALUES(?,?,?,?,?,?)",
                (event_id, owner, str(snapshot), sha256, size, filename),
            )
        return {"event_id": event_id, "state": "document_queued", "message_id": None, "created": True, "sha256": sha256, "size": size}

    @staticmethod
    def is_requested(record) -> bool:
        return str(record["event_id"]).startswith(REQUESTED_FILE_PREFIX)

    @staticmethod
    def _paired_owner(db):
        pair = {row["key"]: json.loads(row["value"]) for row in db.execute(
            "SELECT key,value FROM settings WHERE key IN ('owner_id','owner_chat_id')"
        )}
        owner, chat = pair.get("owner_id"), pair.get("owner_chat_id")
        if type(owner) is not int or owner <= 0 or type(chat) is not int or owner != chat:
            raise ValueError("A paired private owner is required.")
        return owner

    @staticmethod
    def _requested_row(db, event_id):
        return db.execute(
            "SELECT o.task,o.text,o.state,o.message_id,o.sent_at,o.error,d.* "
            "FROM outbox o LEFT JOIN outbound_documents d ON d.event_id=o.event_id WHERE o.event_id=?",
            (event_id,),
        ).fetchone()

    def _requested_binding(self, row, owner):
        spec = _require_requested_file()
        row = dict(row)
        try:
            review = json.loads(row.get("requested_review") or "null")
        except (ValueError, TypeError):
            review = None
        base = spec.binding(owner)
        if (not isinstance(review, dict) or set(review) - (base.keys() | {"snapshot_identity", "sent_receipt"})
                or any(type(review.get(key)) is not type(value) or review[key] != value for key, value in base.items())
                or not isinstance(review.get("snapshot_identity"), str)
                or not re.fullmatch(r"\d+:\d+", review["snapshot_identity"])
                or row.get("event_id") != spec.event_id or row.get("task") != spec.delivery_task
                or row.get("text") != base["caption"] or type(row.get("owner")) is not int
                or row.get("owner") != owner or row.get("sha256") != spec.sha256
                or row.get("size") != spec.size or row.get("filename") != base["filename"]
                or row.get("path") != str(self.store.directory / "outgoing-documents" /
                                         (spec.sha256 + spec.primary_file.suffix.lower()))):
            raise ValueError("The saved requested-file owner, source or snapshot binding changed.")
        return review

    @staticmethod
    def _requested_receipt(row, *, created=False):
        row = dict(row)
        review = json.loads(row.get("requested_review") or "{}")
        saved = review.get("sent_receipt") or {}
        delivered = (
            row["state"] == "sent" and type(row["message_id"]) is int and row["message_id"] > 0
            and isinstance(row["telegram_file_id"], str) and bool(row["telegram_file_id"])
            and type(saved) is dict and type(saved.get("message_id")) is int and type(saved.get("chat_id")) is int
            and saved == {"message_id": row["message_id"], "file_id": row["telegram_file_id"],
                          "chat_id": row["owner"], "chat_type": "private"}
        )
        return {
            "event_id": row["event_id"], "state": "uncertain" if row["state"] == "sent" and not delivered else row["state"],
            "transport_state": row["state"], "created": created, "delivered": delivered,
            "pending": not delivered, "owner_read_confirmed": False,
            "message_id": row["message_id"] if delivered else None,
            "telegram_file_id": row["telegram_file_id"] if delivered else None,
            "sent_at": row["sent_at"] if delivered else None,
            "sha256": row["sha256"], "size": row["size"],
        }

    def queue_requested(self, *, source_record: dict, reviewed_owner: int) -> dict:
        """Admit only the pinned ordinary owner's personal M4A request.

        reviewed_owner comes from trusted controller review of the existing
        private pairing, never from source_record, a model argument or a URL.
        """
        if os.name != "nt":
            raise ValueError("Requested-file admission requires the reviewed Windows host.")
        spec = _require_requested_file()
        with self.store.db() as db:
            owner = self._paired_owner(db)
        if type(reviewed_owner) is not int or reviewed_owner != owner:
            raise ValueError("The paired owner changed since source review.")
        _review_requested_source(source_record)
        from project_files import ProjectFiles, file_identity

        with self.store.db() as db:
            db.execute("BEGIN IMMEDIATE")
            if self._paired_owner(db) != reviewed_owner:
                raise ValueError("The paired owner changed since source review.")
            existing = self._requested_row(db, spec.event_id)
            if existing:
                self._requested_binding(existing, owner)
                return self._requested_receipt(existing)
            with _requested_open(spec.primary_file, root_identity=spec.root_identity,
                                 expected_identity=spec.file_identity) as stream:
                data = _requested_data(stream)
            directory = self.store.directory / "outgoing-documents"
            snapshot = directory / (spec.sha256 + spec.primary_file.suffix.lower())
            # Hold the state directory before mkdir, and all snapshot ancestors
            # throughout exclusive creation and validation. No symlink resolve.
            with ProjectFiles(self.store.directory).open(directory=True):
                directory.mkdir(exist_ok=True)
                with ProjectFiles(directory).open(directory=True):
                    if not snapshot.exists():
                        with snapshot.open("xb") as stream:
                            stream.write(data)
                            stream.flush()
                            os.fsync(stream.fileno())
                    with _requested_open(snapshot) as stream:
                        _requested_data(stream)
                        snapshot_identity = file_identity(os.fstat(stream.fileno()))
            review = {**spec.binding(owner), "snapshot_identity": snapshot_identity}
            # Metadata belongs to the existing document row, not a second
            # delivery ledger. Old ZIP rows and named-column inserts stay valid.
            columns = {row["name"] for row in db.execute("PRAGMA table_info(outbound_documents)")}
            if "requested_review" not in columns:
                db.execute("ALTER TABLE outbound_documents ADD COLUMN requested_review TEXT")
            db.execute(
                "INSERT INTO outbox(event_id,task,text,created_at,state) VALUES(?,?,?,?,'document_queued')",
                (spec.event_id, spec.delivery_task, review["caption"], time.time()),
            )
            db.execute(
                "INSERT INTO outbound_documents(event_id,owner,path,sha256,size,filename,requested_review) VALUES(?,?,?,?,?,?,?)",
                (spec.event_id, owner, str(snapshot), spec.sha256, spec.size, review["filename"], json.dumps(review)),
            )
            return self._requested_receipt(self._requested_row(db, spec.event_id), created=True)

    def requested_receipt(self, *, event_id: str) -> dict:
        """Read persisted evidence; queued, rejected and uncertain are not sent."""
        spec = _require_requested_file()
        if event_id != spec.event_id:
            raise ValueError("Unknown reviewed requested-file source.")
        with self.store.db() as db:
            row = self._requested_row(db, event_id)
            if row is None:
                return {"event_id": event_id, "state": "unavailable", "delivered": False,
                        "pending": True, "owner_read_confirmed": False}
            self._requested_binding(row, self._paired_owner(db))
            return self._requested_receipt(row)

    def _checked_requested_record(self, record):
        with self.store.db() as db:
            row = self._requested_row(db, record["event_id"])
            if row is None:
                raise ValueError("The requested document has no admitted source.")
            review = self._requested_binding(row, self._paired_owner(db))
            fields = ("event_id", "owner", "path", "sha256", "size", "filename", "requested_review")
            supplied = dict(record)
            if any(supplied.get(key) != row[key] for key in fields):
                raise ValueError("The requested document record changed before delivery.")
            if row["state"] not in {"document_queued", "sending"}:
                raise ValueError("This requested-file attempt is held or already finished.")
            return review

    def mime_type(self, record) -> str:
        """Return a fixed reviewed MIME value; no source-supplied MIME is used."""
        if self.is_requested(record):
            return self._checked_requested_record(record)["mime_type"]
        if Path(record["filename"]).suffix.lower() != ".zip" or Path(record["path"]).suffix.lower() != ".zip":
            raise ValueError("A non-ZIP document requires explicit requested-file admission.")
        return "application/zip"

    def record_requested_sent(self, *, event_id: str, result: dict) -> dict:
        """Persist an actual sendDocument result after the sender's atomic claim.

        Malformed or mismatched acknowledgements become uncertain, never a
        definitely rejected/failed attempt. This method performs no upload.
        """
        spec = _require_requested_file()
        if event_id != spec.event_id:
            raise ValueError("Unknown reviewed requested-file source.")
        with self.store.db() as db:
            db.execute("BEGIN IMMEDIATE")
            row = self._requested_row(db, event_id)
            if row is None or row["state"] != "sending":
                raise ValueError("Only a claimed requested-file send may record a receipt.")
            try:
                review = self._requested_binding(row, self._paired_owner(db))
                chat, document = result.get("chat"), result.get("document")
                message_id = result.get("message_id")
                if (type(message_id) is not int or not 0 < message_id < 2**63 or not isinstance(chat, dict)
                        or type(chat.get("id")) is not int or chat["id"] != row["owner"] or chat.get("type") != "private"
                        or not isinstance(document, dict) or not isinstance(document.get("file_id"), str)
                        or not 1 <= len(document["file_id"]) <= 1024
                        or any(not 33 <= ord(c) <= 126 for c in document["file_id"])
                        or ("file_name" in document and document["file_name"] != row["filename"])
                        or ("file_size" in document and (type(document["file_size"]) is not int or document["file_size"] != row["size"]))
                        or ("mime_type" in document and document["mime_type"] != review["mime_type"])):
                    raise ValueError("The document acknowledgement does not match the reviewed delivery.")
            except (ValueError, TypeError, AttributeError):
                db.execute("UPDATE outbox SET state='uncertain',error=? WHERE event_id=?",
                           ("The requested document acknowledgement could not be verified.", event_id))
                return {"event_id": event_id, "state": "uncertain", "delivered": False,
                        "pending": True, "owner_read_confirmed": False}
            review["sent_receipt"] = {
                "message_id": message_id, "file_id": document["file_id"],
                "chat_id": chat["id"], "chat_type": chat["type"],
            }
            db.execute("UPDATE outbox SET state='sent',message_id=?,sent_at=?,error=NULL WHERE event_id=?",
                       (message_id, time.time(), event_id))
            db.execute("UPDATE outbound_documents SET telegram_file_id=?,requested_review=? WHERE event_id=?",
                       (document["file_id"], json.dumps(review), event_id))
            return self._requested_receipt(self._requested_row(db, event_id))

    def verified_bytes(self, record):
        if self.is_requested(record):
            review = self._checked_requested_record(record)
            with _requested_open(Path(record["path"]), expected_identity=review["snapshot_identity"]) as stream:
                data = _requested_data(stream)
            self._checked_requested_record(record)
            return data
        self.mime_type(record)
        owner = self.store.get("owner_id")
        if owner != record["owner"] or self.store.get("owner_chat_id") != owner:
            raise ValueError("The paired owner changed before document delivery.")
        directory = (self.store.directory / "outgoing-documents").resolve()
        if not directory.is_relative_to(self.store.directory.resolve()):
            raise ValueError("The document directory is outside Vega's private state.")
        path = Path(record["path"]).resolve(strict=True)
        if not path.is_relative_to(directory) or not 0 < record["size"] <= MAX_DOCUMENT_BYTES:
            raise ValueError("The private document snapshot is invalid.")
        if not path.is_file() or path.stat().st_size != record["size"]:
            raise ValueError("The private document snapshot changed before delivery.")
        with path.open("rb") as stream:
            data = stream.read(MAX_DOCUMENT_BYTES + 1)
        if len(data) != record["size"] or hashlib.sha256(data).hexdigest() != record["sha256"]:
            raise ValueError("The private document snapshot changed before delivery.")
        if self.store.get("owner_id") != record["owner"] or self.store.get("owner_chat_id") != record["owner"]:
            raise ValueError("The paired owner changed before document delivery.")
        return data
