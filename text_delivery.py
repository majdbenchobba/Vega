"""Durable, ordered Telegram text parts. A send receipt is never a read receipt."""
from __future__ import annotations

import hashlib
from html.parser import HTMLParser
import json
import time


MAX_TEXT_BYTES = 256 * 1024


def install(db):
    db.executescript("""
        CREATE TABLE IF NOT EXISTS outbound_text (
            event_id TEXT PRIMARY KEY, owner INTEGER NOT NULL, body_hash TEXT NOT NULL,
            created_at REAL NOT NULL
        );
        CREATE TABLE IF NOT EXISTS outbound_text_parts (
            event_id TEXT NOT NULL, part_index INTEGER NOT NULL, text TEXT NOT NULL,
            parse_mode TEXT, state TEXT NOT NULL, message_id INTEGER, sent_at REAL,
            error TEXT, PRIMARY KEY(event_id,part_index)
        );
    """)
    columns = {row["name"] for row in db.execute("PRAGMA table_info(outbound_text_parts)")}
    for name, kind in (("returned_text_hash", "TEXT"), ("rendered_text_matches", "INTEGER"),
                       ("returned_entities", "TEXT")):
        if name not in columns:
            db.execute(f"ALTER TABLE outbound_text_parts ADD COLUMN {name} {kind}")


def prepare(db, row, owner, formatter):
    """Freeze the exact rendered parts once, inside the caller's transaction."""
    previous = db.execute("SELECT * FROM outbound_text WHERE event_id=?", (row["event_id"],)).fetchone()
    digest = hashlib.sha256(row["text"].encode("utf-8")).hexdigest()
    if previous:
        if previous["owner"] != owner or previous["body_hash"] != digest:
            raise ValueError("The saved message or its paired recipient changed. Delivery is held.")
        if db.execute("SELECT 1 FROM outbound_text_parts WHERE event_id=?", (row["event_id"],)).fetchone():
            return
    parts = formatter(row["text"])
    if not parts:
        raise ValueError("There is no readable text to deliver.")
    total = len(parts)
    db.execute("INSERT OR IGNORE INTO outbound_text VALUES(?,?,?,?)",
               (row["event_id"], owner, digest, time.time()))
    for index, part in enumerate(parts):
        prefix = f"<b>Part {index + 1} of {total}</b>\n\n" if total > 1 else ""
        db.execute(
            "INSERT INTO outbound_text_parts(event_id,part_index,text,parse_mode,state) VALUES(?,?,?,?,'queued')",
            (row["event_id"], index, prefix + part["text"], part.get("parse_mode")),
        )


def claim_part(db, event_id):
    pending = db.execute(
        "SELECT * FROM outbound_text_parts WHERE event_id=? AND state!='sent' ORDER BY part_index LIMIT 1",
        (event_id,),
    ).fetchone()
    if pending is None:
        return None
    if pending["state"] != "queued":
        raise ValueError("A text part needs delivery reconciliation before later parts can be sent.")
    db.execute("UPDATE outbound_text_parts SET state='sending' WHERE event_id=? AND part_index=? AND state='queued'",
               (event_id, pending["part_index"]))
    return dict(pending)


class _VisibleText(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.chunks = []

    def handle_data(self, data):
        self.chunks.append(data)


def record_sent(db, event_id, part_index, message_id, *, now=None, result=None):
    if type(message_id) is not int or message_id <= 0:
        raise ValueError("Telegram did not return a valid message receipt.")
    now = now if now is not None else time.time()
    db.execute(
        "UPDATE outbound_text_parts SET state='sent',message_id=?,sent_at=?,error=NULL "
        "WHERE event_id=? AND part_index=? AND state='sending'",
        (message_id, now, event_id, part_index),
    )
    if isinstance(result, dict) and isinstance(result.get("text"), str):
        planned = db.execute("SELECT text,parse_mode FROM outbound_text_parts WHERE event_id=? AND part_index=?",
                             (event_id, part_index)).fetchone()
        parser = _VisibleText()
        parser.feed(planned["text"])
        expected = "".join(parser.chunks) if planned["parse_mode"] == "HTML" else planned["text"]
        entities = [{"type": item["type"], "offset": item.get("offset"), "length": item.get("length")}
                    for item in result.get("entities", []) if isinstance(item, dict) and isinstance(item.get("type"), str)]
        db.execute(
            "UPDATE outbound_text_parts SET returned_text_hash=?,rendered_text_matches=?,returned_entities=? "
            "WHERE event_id=? AND part_index=?",
            (hashlib.sha256(result["text"].encode("utf-8")).hexdigest(),
             int(result["text"] == expected), json.dumps(entities), event_id, part_index),
        )
    remaining = db.execute("SELECT count(*) FROM outbound_text_parts WHERE event_id=? AND state!='sent'",
                           (event_id,)).fetchone()[0]
    # The parent keeps the first message id for compatibility; every part's id
    # remains available for reply routing and truthful delivery reporting.
    db.execute(
        "UPDATE outbox SET state=?,message_id=COALESCE(message_id,?),sent_at=?,error=NULL WHERE event_id=?",
        ("queued" if remaining else "sent", message_id, None if remaining else now, event_id),
    )


def record_failure(db, event_id, part_index, state, error):
    if state not in {"failed", "uncertain"}:
        raise ValueError("Invalid text delivery failure state.")
    if part_index is not None:
        db.execute("UPDATE outbound_text_parts SET state=?,error=? WHERE event_id=? AND part_index=? AND state='sending'",
                   (state, error, event_id, part_index))
    db.execute("UPDATE outbox SET state=?,error=? WHERE event_id=?", (state, error, event_id))


def recover_interrupted(db, message="Host stopped before a text delivery receipt was recorded"):
    db.execute("UPDATE outbound_text_parts SET state='uncertain',error=? WHERE state='sending'", (message,))
    db.execute(
        "UPDATE outbox SET state='uncertain',error=? WHERE state IN ('queued','sending') "
        "AND event_id IN (SELECT event_id FROM outbound_text_parts WHERE state='uncertain')",
        (message,),
    )


def receipt(db, event_id):
    parent = db.execute("SELECT state,message_id,sent_at,error FROM outbox WHERE event_id=?", (event_id,)).fetchone()
    if parent is None:
        return {"state": "unavailable", "parts": [], "delivered": False, "pending": True}
    parts = [dict(row) for row in db.execute(
        "SELECT part_index,state,message_id,sent_at,error,rendered_text_matches,returned_entities "
        "FROM outbound_text_parts WHERE event_id=? ORDER BY part_index",
        (event_id,),
    )]
    for part in parts:
        part["returned_entities"] = json.loads(part["returned_entities"]) if part["returned_entities"] else None
    delivered = parent["state"] == "sent" and (not parts or all(
        part["state"] == "sent" and type(part["message_id"]) is int for part in parts))
    state = "delivered" if delivered else parent["state"]
    if state == "queued" and any(part["state"] == "sent" for part in parts):
        state = "partially_delivered"
    return {
        "state": state, "transport_state": parent["state"], "delivered": delivered,
        "pending": not delivered, "owner_read_confirmed": False,
        "message_id": parent["message_id"], "sent_at": parent["sent_at"], "error": parent["error"],
        "parts": parts, "delivered_parts": sum(part["state"] == "sent" for part in parts),
        "total_parts": len(parts) or None,
    }
