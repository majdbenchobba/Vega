"""Preserve owner-supplied Telegram media without a second update consumer."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import re
import time
from automatic_context import owner_opening
from local_attachments import LocalAttachments, media_type


class OwnerMedia:
    def __init__(self, store):
        self.store = store
        with store.db() as db:
            db.execute(
                "CREATE TABLE IF NOT EXISTS owner_media "
                "(update_id INTEGER PRIMARY KEY,message_id INTEGER NOT NULL,owner INTEGER NOT NULL,"
                "kind TEXT NOT NULL,file_id TEXT NOT NULL,metadata TEXT NOT NULL,"
                "received_at REAL NOT NULL,local_path TEXT,sha256 TEXT)"
            )

    def accept(self, update):
        message = update["message"]
        kind, media = None, None
        if isinstance(message.get("photo"), list) and message["photo"]:
            candidates = [item for item in message["photo"] if isinstance(item, dict)]
            if candidates:
                kind, media = "photo", max(candidates, key=lambda item: (item.get("width", 0) or 0) * (item.get("height", 0) or 0))
        for name in ("video", "document"):
            if isinstance(message.get(name), dict):
                kind, media = name, message[name]
                break
        if not media or not isinstance(media.get("file_id"), str) or not 0 < len(media["file_id"]) <= 512:
            raise ValueError("The media reference is invalid.")
        caption = message.get("caption") if isinstance(message.get("caption"), str) else ""
        metadata = {
            key: media.get(key) for key in ("file_unique_id", "file_size", "width", "height", "duration", "file_name", "mime_type")
        }
        metadata.update({
            "caption": caption[:4000], "media_group_id": message.get("media_group_id"),
            "forwarded_data": any(key in message for key in ("forward_origin", "forward_date", "via_bot")),
            "forward_origin": message.get("forward_origin"),
            "forward_date": message.get("forward_date"),
            "forward_from_message_id": message.get("forward_from_message_id"),
            "via_bot": message.get("via_bot"),
            "reply_to_message_id": (message.get("reply_to_message") or {}).get("message_id"),
        })
        if len(json.dumps(metadata)) > 16000:
            raise ValueError("The media metadata exceeds its local limit.")
        with self.store.db() as db:
            inserted = db.execute(
                "INSERT OR IGNORE INTO owner_media VALUES(?,?,?,?,?,?,?,NULL,NULL)",
                (update["update_id"], message["message_id"], self.store.get("owner_id"), kind, media["file_id"], json.dumps(metadata), time.time()),
            ).rowcount
        return bool(inserted), kind

    def list(self, limit=30, *, since=None):
        owner = self.store.get("owner_id")
        with self.store.db() as db:
            rows = db.execute(
                "SELECT update_id,message_id,kind,metadata,received_at,local_path,sha256 "
                "FROM owner_media WHERE owner=? AND received_at>=? ORDER BY received_at DESC LIMIT ?",
                (owner, since or 0, min(100, max(1, limit))),
            ).fetchall()
        return [{**dict(row), "metadata": json.loads(row["metadata"])} for row in rows]

    def context(self):
        return [
            {**item, "metadata": {
                key: item["metadata"].get(key)
                for key in ("caption", "file_name", "file_size", "width", "height", "duration", "forwarded_data", "media_group_id")
            }}
            for item in self.list(12, since=time.time() - 1800)
        ]

    def model_context(self, owner, update_id, text, context, telegram):
        """Fetch only the current photo or an explicitly referenced recent owner photo."""
        from relay import TelegramError
        empty = {"sources": [], "images": []}
        if owner != self.store.get("owner_id") or owner != self.store.get("owner_chat_id"):
            raise PermissionError("The paired owner changed.")
        current = context.get("telegram_photo_update_id")
        reply = context.get("owner_media_reply_message_id")
        clean = owner_opening(text)
        references = bool(re.search(
            r"\b(?:did you (?:get|receive|see)|can you (?:see|read|describe)|describe|analyse|analyze|"
            r"what(?:'s| is) (?:in|on)|look at|check)\b.{0,55}\b(?:pic|picture|photo|image|screenshot)\b|"
            r"\b(?:this|that|the attached|my last) (?:pic|picture|photo|image|screenshot)\b", clean, re.I))
        if not current and not reply and not references:
            return empty
        with self.store.db() as db:
            rows = db.execute("SELECT * FROM owner_media WHERE owner=? AND kind='photo' "
                              "AND received_at>=? ORDER BY received_at DESC LIMIT 8",
                              (owner, time.time() - 1800)).fetchall()
        if current:
            selected = [row for row in rows if row["update_id"] == current]
        elif reply:
            selected = [row for row in rows if row["message_id"] == reply]
        else:
            selected = rows[:1]
        if not selected:
            return {"sources": [{"content_status": "unavailable", "authority": False,
                                 "error": "The referenced photo is not available in the recent owner messages."}], "images": []}
        selected = selected[:1]
        sources, images = [], []
        for row in selected:
            metadata = json.loads(row["metadata"])
            source = {"source": "telegram_photo", "message_id": row["message_id"],
                      "received_at": row["received_at"], "caption": metadata.get("caption"),
                      "forwarded_data": bool(metadata.get("forwarded_data")),
                      "authority": False, "content_status": "unavailable"}
            try:
                if telegram is None:
                    raise ValueError("The Telegram download connection is unavailable.")
                if (metadata.get("file_size") or 0) > 10_000_000:
                    raise ValueError("The photo exceeds the 10 MB image limit.")
                data = telegram.download_file(row["file_id"], maximum=10_000_000)
                kind, _ = media_type(data, "owner-photo.jpg", "")
                if kind != "image":
                    raise ValueError("Telegram returned unsupported image bytes.")
                url = LocalAttachments._image(data)
                with self.store.db() as db:
                    latest = db.execute("SELECT * FROM owner_media WHERE update_id=? AND owner=?",
                                        (row["update_id"], owner)).fetchone()
                if (not latest or dict(latest) != dict(row) or self.store.get("owner_id") != owner
                        or self.store.get("owner_chat_id") != owner):
                    raise PermissionError("The photo or owner pairing changed during download.")
                source.update(content_status="image_supplied", sha256=hashlib.sha256(data).hexdigest())
                images.append({"image_url": url})
            except (ValueError, OSError, RuntimeError, TelegramError) as error:
                source["error"] = str(error)[:250]
            sources.append(source)
        # Bytes/data URLs are used for this reply only; no extra raw copy is saved.
        return {"sources": sources, "images": images}

    def download(self, update_id, telegram):
        owner = self.store.get("owner_id")
        with self.store.db() as db:
            row = db.execute("SELECT * FROM owner_media WHERE update_id=? AND owner=?", (update_id, owner)).fetchone()
        if not row:
            raise ValueError("This paired-owner media reference is unavailable.")
        if row["local_path"] and Path(row["local_path"]).is_file():
            return {"update_id": update_id, "path": row["local_path"], "sha256": row["sha256"]}
        metadata = json.loads(row["metadata"])
        if isinstance(metadata.get("file_size"), int) and metadata["file_size"] > 20 * 1024 * 1024:
            raise ValueError("Telegram's Bot API download limit is 20 MB. The reference is preserved; request a smaller file or a link.")
        data = telegram.download_file(row["file_id"], maximum=20 * 1024 * 1024)
        suffix = ".jpg" if data.startswith(b"\xff\xd8\xff") else ".png" if data.startswith(b"\x89PNG\r\n\x1a\n") else ".webp" if data.startswith(b"RIFF") and data[8:12] == b"WEBP" else ".mp4" if data[4:8] == b"ftyp" else ".bin"
        directory = (self.store.directory / "media").resolve()
        directory.mkdir(exist_ok=True)
        target = directory / (str(int(update_id)) + suffix)
        if not target.resolve().is_relative_to(directory):
            raise ValueError("The local media path is invalid.")
        temporary = target.with_suffix(suffix + ".tmp")
        temporary.write_bytes(data)
        temporary.replace(target)
        digest = hashlib.sha256(data).hexdigest()
        with self.store.db() as db:
            db.execute("UPDATE owner_media SET local_path=?,sha256=? WHERE update_id=? AND owner=?", (str(target), digest, update_id, owner))
        return {"update_id": update_id, "path": str(target), "sha256": digest}
