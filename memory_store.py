"""Selective owner memory with source records, review, deduplication and undo."""
from __future__ import annotations

import json
import re
import uuid

from companion import ConflictError, text, revision
from memory_policy import (
    canonical_topic, contains_secret, fingerprint, local_candidates,
    normalize, select_candidate,
)


class MemoryStore:
    def __init__(self, companion):
        self.companion = companion
        self.store = companion.store
        with self.store.db() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS memory_metadata (
                    memory_id TEXT PRIMARY KEY, kind TEXT NOT NULL,
                    topic TEXT NOT NULL, value_key TEXT NOT NULL, body_hash TEXT NOT NULL,
                    source_kind TEXT NOT NULL, provenance TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS memory_body_hash ON memory_metadata(body_hash);
                CREATE TABLE IF NOT EXISTS memory_candidates (
                    id TEXT PRIMARY KEY, owner INTEGER NOT NULL, project TEXT,
                    payload TEXT NOT NULL, status TEXT NOT NULL, created_at INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS memory_events (
                    id TEXT PRIMARY KEY, owner INTEGER NOT NULL, memory_id TEXT NOT NULL,
                    action TEXT NOT NULL, before_payload TEXT, after_payload TEXT NOT NULL,
                    created_at INTEGER NOT NULL, undone_at INTEGER
                );
                CREATE TABLE IF NOT EXISTS memory_suppressions (
                    owner INTEGER NOT NULL, scope TEXT NOT NULL, topic TEXT NOT NULL,
                    value_key TEXT NOT NULL, created_at INTEGER NOT NULL,
                    PRIMARY KEY(owner,scope,topic,value_key)
                );
                CREATE TABLE IF NOT EXISTS memory_learning_receipts (
                    owner INTEGER NOT NULL, update_id TEXT NOT NULL,
                    outcome TEXT NOT NULL, created_at INTEGER NOT NULL,
                    PRIMARY KEY(owner,update_id)
                );
                CREATE TABLE IF NOT EXISTS memory_import_batches (
                    id TEXT PRIMARY KEY, owner INTEGER NOT NULL, source_label TEXT NOT NULL,
                    metadata TEXT NOT NULL, result TEXT NOT NULL, created_at INTEGER NOT NULL,
                    undone_at INTEGER
                );
            """)
            legacy = db.execute(
                "SELECT m.id,m.body FROM companion_memory m LEFT JOIN memory_metadata d ON d.memory_id=m.id WHERE d.memory_id IS NULL"
            ).fetchall()
            for row in legacy:
                db.execute("INSERT INTO memory_metadata VALUES(?,?,?,?,?,?,?)",
                           (row["id"], "note", "", fingerprint(row["body"]), fingerprint(row["body"]),
                            "manual", json.dumps({"label": "Existing owner-reviewed Vega note"})))

    def _owner(self):
        return self.companion.owner()

    def _now(self):
        return self.companion.now()

    def version(self):
        owner = self._owner()
        with self.store.db() as db:
            memory = db.execute("SELECT count(*),coalesce(max(updated_at),0),coalesce(sum(revision),0) FROM companion_memory WHERE owner=?", (owner,)).fetchone()
            pending = db.execute("SELECT count(*),coalesce(max(created_at),0) FROM memory_candidates WHERE owner=? AND status='pending'", (owner,)).fetchone()
            batches = db.execute("SELECT count(*),coalesce(max(created_at),0),coalesce(max(undone_at),0) FROM memory_import_batches WHERE owner=?", (owner,)).fetchone()
        return ":".join(str(value) for value in (*memory, *pending, *batches))

    def _record(self, db, identity, owner):
        row = db.execute("SELECT * FROM companion_memory WHERE id=? AND owner=?", (identity, owner)).fetchone()
        if not row:
            return None
        metadata = db.execute("SELECT * FROM memory_metadata WHERE memory_id=?", (identity,)).fetchone()
        record = dict(row)
        record["metadata"] = dict(metadata) if metadata else {
            "memory_id": identity, "kind": "note", "topic": "",
            "value_key": fingerprint(row["body"]), "body_hash": fingerprint(row["body"]),
            "source_kind": "manual", "provenance": json.dumps({"label": "Owner-reviewed note"}),
        }
        return record

    def _public(self, record):
        metadata = record["metadata"]
        return {
            key: record[key] for key in ("id", "project", "title", "body", "revision", "created_at", "updated_at", "archived")
        } | {
            "kind": metadata["kind"], "topic": metadata["topic"],
            "source_kind": metadata["source_kind"],
            "provenance": json.loads(metadata["provenance"]),
            "authority": False,
            "recall_allowed": not contains_secret(record["body"]),
        }

    def list(self, query="", project=None, *, all_scopes=False, archived=False, limit=100):
        owner = self._owner()
        project = self.companion.project(project)
        query = text(query, "Memory search", 300, empty=True)
        terms = re.findall(r"\w+", normalize(query))[:12]
        where = "owner=? AND archived=?"
        parameters = [owner, int(archived)]
        if not all_scopes:
            where += " AND (project IS NULL OR project=?)"
            parameters.append(project)
        with self.store.db() as db:
            db.create_function("memory_normalize", 1, lambda value: normalize(value or ""), deterministic=True)
            if terms:
                where += " AND (" + " OR ".join("memory_normalize(title||' '||body) LIKE ?" for _ in terms) + ")"
                parameters.extend("%" + term + "%" for term in terms)
            rows = db.execute(
                "SELECT id FROM companion_memory WHERE " + where + " ORDER BY updated_at DESC LIMIT ?",
                (*parameters, min(500, max(1, limit))),
            ).fetchall()
            records = [self._record(db, row["id"], owner) for row in rows]
        records.sort(key=lambda item: (
            sum(term in normalize(item["title"] + " " + item["body"]) for term in terms), item["updated_at"],
        ), reverse=True)
        return [self._public(record) for record in records]

    def context_notes(self, query, project=None):
        # Conversation messages may be longer than the interactive search box.
        # Bound only the retrieval query; the owner message remains unchanged.
        query = text(query, "Conversation context", 12000, empty=True)[:300]
        owner, project = self._owner(), self.companion.project(project)
        standing_topics = ("preference.response-length", "preference.response-language", "fact.preferred-name", "fact.location")
        with self.store.db() as db:
            rows = db.execute(
                "SELECT m.id FROM companion_memory m JOIN memory_metadata d ON d.memory_id=m.id "
                "WHERE m.owner=? AND m.archived=0 AND (m.project IS NULL OR m.project=?) "
                "AND d.topic IN (?,?,?,?) ORDER BY m.updated_at DESC LIMIT 4",
                (owner, project, *standing_topics),
            ).fetchall()
            standing = [self._public(self._record(db, row["id"], owner)) for row in rows]
        selected, seen = [], set()
        terms = re.findall(r"\w+", normalize(query))[:12]
        for note in standing + self.list(query, project, limit=20):
            if note["id"] in seen or not note["recall_allowed"]:
                continue
            seen.add(note["id"])
            body = note["body"]
            positions = [normalize(body).find(term) for term in terms if term in normalize(body)]
            start = max(0, min(positions) - 400) if positions and len(body) > 1800 else 0
            provenance = note["provenance"]
            safe_provenance = {key: value for key, value in provenance.items()
                               if key in ("label", "provider", "source_id", "channel", "update_id", "recorded_at", "reviewed_at", "import_batch_id")}
            selected.append({**note, "body": body[start:start + 1800], "excerpt_offset": start,
                             "provenance": safe_provenance, "authority": False})
            if len(selected) >= 6:
                break
        return selected

    def _duplicate(self, db, owner, project, candidate):
        row = db.execute(
            "SELECT m.id FROM companion_memory m JOIN memory_metadata d ON d.memory_id=m.id "
            "WHERE m.owner=? AND m.project IS ? AND m.archived=0 "
            "AND (d.body_hash=? OR (d.topic=? AND d.value_key=?)) LIMIT 1",
            (owner, project, fingerprint(candidate["body"]), candidate["topic"], candidate["value_key"]),
        ).fetchone()
        return self._record(db, row["id"], owner) if row else None

    def _topic_match(self, db, owner, project, topic):
        row = db.execute(
            "SELECT m.id FROM companion_memory m JOIN memory_metadata d ON d.memory_id=m.id "
            "WHERE m.owner=? AND m.project IS ? AND m.archived=0 AND d.topic=? ORDER BY m.updated_at DESC LIMIT 1",
            (owner, project, topic),
        ).fetchone()
        return self._record(db, row["id"], owner) if row else None

    def _write(self, db, owner, candidate, source_kind, provenance, *, identity=None, expected=0, action=None):
        identity = identity or str(uuid.uuid4())
        before = self._record(db, identity, owner)
        if (before["revision"] if before else 0) != expected or (before and before["archived"]):
            raise ConflictError("This memory changed. Review its current version before continuing.")
        now = self._now()
        if before:
            db.execute(
                "UPDATE companion_memory SET project=?,title=?,body=?,revision=?,updated_at=? WHERE id=? AND owner=?",
                (candidate["project"], candidate["title"], candidate["body"], expected + 1, now, identity, owner),
            )
        else:
            db.execute(
                "INSERT INTO companion_memory VALUES(?,?,?,?,?,?,?,?,0)",
                (identity, owner, candidate["project"], candidate["title"], candidate["body"], 1, now, now),
            )
        db.execute(
            "INSERT OR REPLACE INTO memory_metadata VALUES(?,?,?,?,?,?,?)",
            (identity, candidate["kind"], candidate["topic"], candidate["value_key"],
             fingerprint(candidate["body"]), source_kind, json.dumps(provenance, ensure_ascii=False)),
        )
        after = self._record(db, identity, owner)
        event_id = str(uuid.uuid4())
        db.execute(
            "INSERT INTO memory_events VALUES(?,?,?,?,?,?,?,NULL)",
            (event_id, owner, identity, action or ("updated" if before else "created"),
             json.dumps(before, ensure_ascii=False) if before else None,
             json.dumps(after, ensure_ascii=False), now),
        )
        self.companion.audit(db, owner, "memory." + (action or ("updated" if before else "created")), identity, expected + 1)
        return {"id": identity, "revision": expected + 1, "event_id": event_id}

    def save_manual(self, payload):
        if not isinstance(payload, dict):
            raise ValueError("A memory form is required.")
        owner = self._owner()
        body = text(payload.get("body"), "Memory", 6000)
        title = text(payload.get("title"), "Memory title", 160)
        if contains_secret(body) or contains_secret(title):
            raise ValueError("Keep credentials in the secure connection settings, not in memory notes.")
        project = self.companion.project(payload.get("project"))
        expected = revision(payload.get("expected_revision"))
        identity = text(payload["id"], "Memory identity", 80) if payload.get("id") else None
        topic, value_key = canonical_topic(body, title)
        candidate = {"kind": "note", "title": title, "body": body, "project": project, "topic": topic, "value_key": value_key}
        with self.store.db() as db:
            db.execute("BEGIN IMMEDIATE")
            if not identity:
                duplicate = self._duplicate(db, owner, project, candidate)
                if duplicate:
                    return {"id": duplicate["id"], "revision": duplicate["revision"], "duplicate": True}
            before = self._record(db, identity, owner) if identity else None
            provenance = {"label": "Owner-reviewed note", "recorded_at": self._now()}
            if before:
                previous = json.loads(before["metadata"]["provenance"])
                provenance["origin"] = previous.get("origin", previous)
            return self._write(db, owner, candidate, "manual", provenance,
                               identity=identity, expected=expected)

    def archive(self, identity, expected_revision):
        owner, expected = self._owner(), revision(expected_revision)
        with self.store.db() as db:
            db.execute("BEGIN IMMEDIATE")
            before = self._record(db, identity, owner)
            if not before or before["archived"] or before["revision"] != expected:
                raise ConflictError("This memory changed. Refresh before archiving.")
            db.execute("UPDATE companion_memory SET archived=1,revision=revision+1,updated_at=? WHERE id=? AND owner=?", (self._now(), identity, owner))
            after = self._record(db, identity, owner)
            event_id = str(uuid.uuid4())
            db.execute("INSERT INTO memory_events VALUES(?,?,?,?,?,?,?,NULL)",
                       (event_id, owner, identity, "archived", json.dumps(before), json.dumps(after), self._now()))
            self._suppress(db, owner, before)
        return {"archived": True, "event_id": event_id}

    def _suppress(self, db, owner, record):
        metadata = record["metadata"]
        db.execute(
            "INSERT OR REPLACE INTO memory_suppressions VALUES(?,?,?,?,?)",
            (owner, record["project"] or "", metadata["topic"], metadata["value_key"], self._now()),
        )

    def events(self, project=None, *, all_scopes=False):
        owner = self._owner()
        project = self.companion.project(project)
        with self.store.db() as db:
            rows = db.execute("SELECT * FROM memory_events WHERE owner=? ORDER BY created_at DESC LIMIT 100", (owner,)).fetchall()
        result = []
        for row in rows:
            after = json.loads(row["after_payload"])
            if not all_scopes and after["project"] not in (None, project):
                continue
            result.append({
                "id": row["id"], "memory_id": row["memory_id"], "action": row["action"],
                "title": after["title"], "project": after["project"], "revision": after["revision"],
                "source_kind": after["metadata"]["source_kind"], "created_at": row["created_at"],
                "undone": row["undone_at"] is not None,
            })
        return result

    def _undo(self, db, owner, event):
        if event["undone_at"] is not None:
            return
        after = json.loads(event["after_payload"])
        current = self._record(db, event["memory_id"], owner)
        if not current or current["revision"] != after["revision"]:
            raise ConflictError("This memory was edited after that change. Review it instead of undoing a newer edit.")
        before = json.loads(event["before_payload"]) if event["before_payload"] else None
        self._suppress(db, owner, current)
        if before:
            db.execute(
                "UPDATE companion_memory SET project=?,title=?,body=?,revision=?,updated_at=?,archived=? WHERE id=? AND owner=?",
                (before["project"], before["title"], before["body"], current["revision"] + 1, self._now(),
                 before["archived"], current["id"], owner),
            )
            metadata = before["metadata"]
            db.execute("INSERT OR REPLACE INTO memory_metadata VALUES(?,?,?,?,?,?,?)",
                       tuple(metadata[key] for key in ("memory_id", "kind", "topic", "value_key", "body_hash", "source_kind", "provenance")))
        else:
            db.execute("UPDATE companion_memory SET archived=1,revision=revision+1,updated_at=? WHERE id=? AND owner=?", (self._now(), current["id"], owner))
        db.execute("UPDATE memory_events SET undone_at=? WHERE id=?", (self._now(), event["id"]))
        self.companion.audit(db, owner, "memory.undone", current["id"], current["revision"] + 1)

    def undo(self, event_id):
        owner = self._owner()
        with self.store.db() as db:
            db.execute("BEGIN IMMEDIATE")
            event = db.execute("SELECT * FROM memory_events WHERE id=? AND owner=?", (event_id, owner)).fetchone()
            if not event:
                raise ValueError("This memory change is unavailable.")
            self._undo(db, owner, event)
        return {"undone": True}

    def pending(self, project=None, *, all_scopes=False):
        owner, project = self._owner(), self.companion.project(project)
        with self.store.db() as db:
            rows = db.execute("SELECT * FROM memory_candidates WHERE owner=? AND status='pending' ORDER BY created_at DESC LIMIT 200", (owner,)).fetchall()
        return [
            {"id": row["id"], **json.loads(row["payload"]), "created_at": row["created_at"]}
            for row in rows if all_scopes or row["project"] in (None, project)
        ]

    def review_candidate(self, identity, accept):
        if type(accept) is not bool:
            raise ValueError("Choose Keep or Dismiss.")
        owner = self._owner()
        with self.store.db() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM memory_candidates WHERE id=? AND owner=?", (identity, owner)).fetchone()
            if not row:
                raise ValueError("This memory suggestion is unavailable.")
            if row["status"] != "pending":
                return {"status": row["status"]}
            data = json.loads(row["payload"])
            if not accept:
                db.execute("UPDATE memory_candidates SET status='dismissed' WHERE id=?", (identity,))
                db.execute("INSERT OR REPLACE INTO memory_suppressions VALUES(?,?,?,?,?)",
                           (owner, data["project"] or "", data["topic"], data["value_key"], self._now()))
                return {"status": "dismissed"}
            duplicate = self._duplicate(db, owner, data["project"], data)
            if duplicate:
                result = {"id": duplicate["id"], "revision": duplicate["revision"], "duplicate": True}
            else:
                result = self._write(
                    db, owner, data, "reviewed", data["provenance"],
                    identity=data.get("replaces_id"), expected=data.get("expected_revision", 0),
                    action="corrected" if data.get("replaces_id") else "reviewed",
                )
            db.execute("UPDATE memory_candidates SET status='accepted' WHERE id=?", (identity,))
            db.execute("DELETE FROM memory_suppressions WHERE owner=? AND scope=? AND topic=? AND value_key=?",
                       (owner, data["project"] or "", data["topic"], data["value_key"]))
        return {"status": "accepted", **result}

    def learn(self, update_id, source, owner_text, candidates, *, current_project=None, preference_revision=None, expected_owner=None):
        owner = self._owner()
        if expected_owner is not None and owner != expected_owner:
            return {"saved": 0, "proposed": 0, "ignored": 0, "revoked": True}
        preferences = self.companion.preferences()
        values = preferences["values"]
        if source not in ("local", "telegram", "telegram_voice") or not values.get("memory_enabled") or not values.get("learning_enabled", False):
            return {"saved": 0, "proposed": 0, "ignored": 0}
        if preference_revision is not None and preferences["revision"] != preference_revision:
            return {"saved": 0, "proposed": 0, "ignored": 0, "revoked": True}
        current_project = self.companion.project(current_project)
        supplied = candidates if isinstance(candidates, list) else []
        supplied = supplied[:3] + local_candidates(owner_text, current_project)
        selected, seen, ignored = [], set(), 0
        for item in supplied:
            candidate, reason = select_candidate(item, owner_text, self.companion.tasks, current_project)
            if not candidate:
                ignored += 1
                continue
            key = (candidate["project"], candidate["topic"], candidate["value_key"])
            if key in seen:
                continue
            seen.add(key); selected.append(candidate)
        outcome = {"saved": 0, "proposed": 0, "ignored": ignored}
        if not selected:
            return outcome
        with self.store.db() as db:
            db.execute("BEGIN IMMEDIATE")
            stored_owner = db.execute("SELECT value FROM settings WHERE key='owner_id'").fetchone()
            if not stored_owner or json.loads(stored_owner["value"]) != owner:
                return {"saved": 0, "proposed": 0, "ignored": 0, "revoked": True}
            if db.execute("SELECT 1 FROM memory_learning_receipts WHERE owner=? AND update_id=?", (owner, str(update_id))).fetchone():
                return {"saved": 0, "proposed": 0, "ignored": 0, "duplicate_turn": True}
            preference = db.execute("SELECT revision FROM companion_preferences WHERE owner=?", (owner,)).fetchone()
            if (preference["revision"] if preference else 0) != preferences["revision"]:
                return {"saved": 0, "proposed": 0, "ignored": 0, "revoked": True}
            for candidate in selected[:3]:
                if self._duplicate(db, owner, candidate["project"], candidate):
                    continue
                match = self._topic_match(db, owner, candidate["project"], candidate["topic"])
                suppressed = db.execute(
                    "SELECT 1 FROM memory_suppressions WHERE owner=? AND scope=? AND topic=? AND value_key=?",
                    (owner, candidate["project"] or "", candidate["topic"], candidate["value_key"]),
                ).fetchone()
                provenance = {"label": "Owner statement", "channel": source, "update_id": str(update_id),
                              "evidence": candidate["body"], "recorded_at": self._now(), "authority": False}
                automatic = candidate["automatic_eligible"] and not values.get("learning_review_required", False) and not suppressed
                if match and (match["metadata"]["source_kind"] != "automatic" or not candidate.get("rule_topic")):
                    automatic = False
                if automatic:
                    self._write(
                        db, owner, candidate, "automatic", provenance,
                        identity=match["id"] if match else None, expected=match["revision"] if match else 0,
                        action="corrected" if match else "learned",
                    )
                    outcome["saved"] += 1
                else:
                    data = {**candidate, "provenance": provenance, "replaces_id": match["id"] if match else None,
                            "expected_revision": match["revision"] if match else 0,
                            "review_reason": "Previously dismissed/undone" if suppressed else "Review the scope or correction"}
                    duplicate = db.execute(
                        "SELECT 1 FROM memory_candidates WHERE owner=? AND project IS ? AND status='pending' AND json_extract(payload,'$.topic')=? AND json_extract(payload,'$.value_key')=?",
                        (owner, candidate["project"], candidate["topic"], candidate["value_key"]),
                    ).fetchone()
                    if not duplicate:
                        db.execute("INSERT INTO memory_candidates VALUES(?,?,?,?,'pending',?)",
                                   (str(uuid.uuid4()), owner, candidate["project"], json.dumps(data, ensure_ascii=False), self._now()))
                        outcome["proposed"] += 1
            db.execute("INSERT INTO memory_learning_receipts VALUES(?,?,?,?)",
                       (owner, str(update_id), json.dumps(outcome), self._now()))
        return outcome

    def import_records(self, records, source_label, *, batch_id, batch_metadata=None, expected_owner=None):
        """Called only after the owner reviews a bounded import preview."""
        owner = self._owner()
        if expected_owner is not None and owner != expected_owner:
            raise ConflictError("The owner changed before the reviewed import could be stored.")
        added, duplicates, events = 0, 0, []
        with self.store.db() as db:
            db.execute("BEGIN IMMEDIATE")
            stored_owner = db.execute("SELECT value FROM settings WHERE key='owner_id'").fetchone()
            if not stored_owner or json.loads(stored_owner["value"]) != owner:
                raise ConflictError("The owner changed before the reviewed import could be stored.")
            existing = db.execute("SELECT result FROM memory_import_batches WHERE id=? AND owner=?", (batch_id, owner)).fetchone()
            if existing:
                return json.loads(existing["result"])
            for record in records:
                candidate = {
                    "kind": "reference", "title": record["title"], "body": record["text"],
                    "project": self.companion.project(record.get("project")),
                    "topic": "import." + record["dedupe_key"],
                    "value_key": fingerprint(record["text"]),
                }
                if contains_secret(candidate["body"]) or contains_secret(candidate["title"]):
                    continue
                if self._duplicate(db, owner, candidate["project"], candidate):
                    duplicates += 1
                    continue
                provenance = {
                    "label": source_label, "provider": record["provider"], "source_id": record["source_id"],
                    "role": record.get("role", "note"), "original_scope": record.get("original_scope"),
                    "import_batch_id": batch_id, "reviewed_at": self._now(), "authority": False,
                }
                result = self._write(db, owner, candidate, "imported_reference", provenance, action="imported")
                events.append(result["event_id"]); added += 1
            result = {"added": added, "duplicates": duplicates, "event_ids": events}
            db.execute("INSERT INTO memory_import_batches VALUES(?,?,?,?,?,?,NULL)",
                       (batch_id, owner, source_label, json.dumps(batch_metadata or {}, ensure_ascii=False),
                        json.dumps(result), self._now()))
        return result

    def imports(self, project=None, *, all_scopes=True):
        project = self.companion.project(project)
        with self.store.db() as db:
            rows = db.execute("SELECT * FROM memory_import_batches WHERE owner=? ORDER BY created_at DESC LIMIT 50", (self._owner(),)).fetchall()
        return [{"id": row["id"], "source_label": row["source_label"], "metadata": json.loads(row["metadata"]),
                 "result": json.loads(row["result"]), "created_at": row["created_at"], "undone": row["undone_at"] is not None}
                for row in rows if all_scopes or json.loads(row["metadata"]).get("project") in (None, project)]

    def undo_import(self, identity):
        owner = self._owner()
        with self.store.db() as db:
            db.execute("BEGIN IMMEDIATE")
            batch = db.execute("SELECT * FROM memory_import_batches WHERE id=? AND owner=?", (identity, owner)).fetchone()
            if not batch:
                raise ValueError("This import is unavailable.")
            if batch["undone_at"] is not None:
                return {"undone": True, "already_undone": True}
            events = []
            for event_id in json.loads(batch["result"])["event_ids"]:
                event = db.execute("SELECT * FROM memory_events WHERE id=? AND owner=?", (event_id, owner)).fetchone()
                if event and event["undone_at"] is None:
                    current = self._record(db, event["memory_id"], owner)
                    if not current or current["revision"] != json.loads(event["after_payload"])["revision"]:
                        raise ConflictError("An imported note was edited later. Review individual changes before undoing this import.")
                    events.append(event)
            for event in events:
                self._undo(db, owner, event)
            db.execute("UPDATE memory_import_batches SET undone_at=? WHERE id=?", (self._now(), identity))
        return {"undone": True, "changes": len(events)}
