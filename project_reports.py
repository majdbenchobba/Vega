"""Scoped, owner-bound report and visible assistant-reply reads.

The host must admit only the current authored owner message before calling
parse_query/read. These methods do not create action permissions, model calls,
memory, routing requests, notifications or delivery receipts. Retrieved text is
source data, including when it contains quoted instructions or copyable code.
"""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import re
import stat
import time

from companion import ConflictError
from memory_policy import contains_secret, unquoted_text, _project_name_matches
from project_files import MAX_BYTES, ProjectFiles, digest, file_identity, relative_path, text_from_bytes
from routing_targets import dispatch_target
from project_progress import LiveProgress


CATALOG_PATH = Path(__file__).parent / "project-report-catalog.json"
MAX_REPORTS = 64
MAX_REPLY_CHARS = 48_000
MAX_REPLY_INPUT_CHARS = 192 * 1024
MAX_SESSION_FILES = 4
MAX_SESSION_TAIL_BYTES = 2 * 1024 * 1024
MAX_SESSION_HEADER_BYTES = 64 * 1024
MAX_SESSION_RECORD_BYTES = 512 * 1024
READ_VERSION = 1
ID = re.compile(r"[A-Za-z0-9_.:-]{1,160}\Z")
ALIAS = re.compile(r"[a-z0-9][a-z0-9_-]{0,79}\Z")
THREAD = re.compile(r"[a-fA-F0-9]{8}(?:-[a-fA-F0-9]{4}){3}-[a-fA-F0-9]{12}\Z")
HASH = re.compile(r"[a-f0-9]{64}\Z")
FUTURE_TOLERANCE = 30


def owner_binding(owner, chat):
    """A catalog binding, not a credential or replacement for principal checks."""
    return digest(json.dumps([owner, chat], separators=(",", ":")).encode("utf-8"))


def _seconds(value):
    if isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            return parsed.timestamp() if parsed.tzinfo is not None else None
        except (ValueError, OverflowError):
            return None
    if type(value) not in (int, float) or not math.isfinite(value) or value <= 0:
        return None
    return float(value)


def _iso(value):
    return datetime.fromtimestamp(value, timezone.utc).isoformat()


def _date_label(value):
    stamp = _seconds(value)
    return datetime.fromtimestamp(stamp, timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC") if stamp else "Not recorded"


def _string(value, maximum=1000, *, empty=False):
    if not isinstance(value, str) or len(value) > maximum or (not empty and not value.strip()):
        raise ValueError("The registered report has invalid metadata.")
    # Never stringify untrusted objects or allow credential-like labels through.
    text_from_bytes(value.encode("utf-8"))
    return value


def _json(data):
    def pairs(items):
        value = {}
        for key, item in items:
            if key in value:
                raise ValueError("Duplicate source fields.")
            value[key] = item
        return value
    return json.loads(data, object_pairs_hook=pairs,
                      parse_constant=lambda _: (_ for _ in ()).throw(ValueError("Invalid number.")))


def _key(value):
    # Mapping only: never resolve, expand or normalize traversal in caller input.
    return value.replace("\\", "/").casefold() if isinstance(value, str) else ""


def _target(tasks, alias):
    target = dict(dispatch_target(tasks, alias))
    # Some host-supplied registries carry the already resolved dispatch field.
    explicit = tasks[alias].get("dispatch_thread_id")
    if explicit is not None:
        target["thread_id"] = explicit
    if not isinstance(target.get("thread_id"), str) or not THREAD.fullmatch(target["thread_id"]):
        raise ValueError("This project's current reply source is unavailable.")
    return target


class ProjectReports:
    def __init__(self, store, tasks, *, catalog=None, catalog_path=None, session_root=None, clock=time.time):
        self.store, self.tasks, self.clock = store, tasks, clock
        self.catalog_path = Path(catalog_path) if catalog_path is not None else CATALOG_PATH
        self.session_root = Path(session_root) if session_root is not None else Path.home() / ".codex" / "sessions"
        # Injected catalogs are trusted host configuration, never HTTP/model data.
        self._injected_catalog = json.loads(json.dumps(catalog)) if catalog is not None else None
        self._catalog_root_identity = None
        self.progress = LiveProgress(store, self._registry, clock=clock)
        if self._injected_catalog is None:
            try:
                self._catalog_root_identity = ProjectFiles(self.catalog_path.parent).identity()
            except (ValueError, OSError):
                pass
        with store.db() as db:
            db.execute("""
                CREATE TABLE IF NOT EXISTS project_visible_replies (
                    owner INTEGER NOT NULL, task TEXT NOT NULL, thread_id TEXT NOT NULL,
                    text TEXT NOT NULL, observed_at REAL NOT NULL, source_ref TEXT NOT NULL,
                    sha256 TEXT, truncated INTEGER NOT NULL, original_characters INTEGER NOT NULL,
                    source_error TEXT, PRIMARY KEY(owner,task,thread_id)
                )
            """)

    def _registry(self):
        value = self.tasks() if callable(self.tasks) else self.tasks
        if not isinstance(value, dict) or len(value) > 256:
            raise ValueError("The registered project list is unavailable.")
        return json.loads(json.dumps(value))

    def _catalog(self):
        if self._injected_catalog is not None:
            value = json.loads(json.dumps(self._injected_catalog))
        else:
            if self._catalog_root_identity is None:
                raise ValueError("The local report catalog is unavailable.")
            source = ProjectFiles(self.catalog_path.parent, self._catalog_root_identity).read(self.catalog_path.name)
            value = _json(source["text"])
        if (not isinstance(value, dict) or value.get("version") != READ_VERSION
                or not isinstance(value.get("owner_binding"), str)
                or not HASH.fullmatch(value["owner_binding"])
                or not isinstance(value.get("reports"), list) or len(value["reports"]) > MAX_REPORTS):
            raise ValueError("The local report catalog is invalid.")
        identifiers = set()
        for entry in value["reports"]:
            if not isinstance(entry, dict) or not ID.fullmatch(_string(entry.get("id"), 160)):
                raise ValueError("The local report catalog is invalid.")
            if entry["id"] in identifiers or entry.get("kind") not in ("markdown", "progress_overview"):
                raise ValueError("The local report catalog is invalid.")
            identifiers.add(entry["id"])
            if (not ALIAS.fullmatch(_string(entry.get("task"), 80))
                    or not THREAD.fullmatch(_string(entry.get("thread_id"), 36))):
                raise ValueError("The local report catalog has an invalid project binding.")
            _string(entry.get("label"), 160)
            _string(entry.get("root_identity"), 100)
            ProjectFiles(entry.get("root"), entry["root_identity"])
            path = relative_path(entry.get("path"))
            if Path(path).suffix.lower() not in (".md", ".txt"):
                raise ValueError("Only registered text reports are available.")
            for field in ("sha256", "record_sha256"):
                if entry.get(field) is not None and not HASH.fullmatch(_string(entry[field], 64)):
                    raise ValueError("The registered report hash is invalid.")
            assessed = entry.get("assessed_at")
            if assessed is not None and _seconds(assessed) is None:
                raise ValueError("The registered assessment date is invalid.")
            if entry["kind"] == "progress_overview":
                if Path(relative_path(entry.get("record_path"))).suffix.lower() != ".json":
                    raise ValueError("A registered assessment record is required.")
                if type(entry.get("expected_projects")) is not int or not 1 <= entry["expected_projects"] <= 64:
                    raise ValueError("The registered assessment count is invalid.")
            paths = entry.get("known_paths", [])
            if not isinstance(paths, list) or len(paths) > 16:
                raise ValueError("The registered report references are invalid.")
            for path in paths:
                _string(path, 500)
        default = value.get("default_report_id")
        if default is not None and default not in identifiers:
            raise ValueError("The default report is no longer registered.")
        sessions = value.get("sessions")
        if sessions is not None:
            if not isinstance(sessions, dict):
                raise ValueError("The registered reply source is invalid.")
            ProjectFiles(sessions.get("root"), _string(sessions.get("root_identity"), 100))
            if _key(str(Path(sessions["root"]))) != _key(str(self.session_root)):
                raise ValueError("The configured reply source differs from the registered session root.")
        return value

    def _principal(self, db, owner, catalog):
        values = {row["key"]: _json(row["value"]) for row in db.execute(
            "SELECT key,value FROM settings WHERE key IN ('owner_id','owner_chat_id')")}
        if (type(owner) is not int or type(values.get("owner_id")) is not int
                or type(values.get("owner_chat_id")) is not int
                or values["owner_id"] != owner or values["owner_chat_id"] != owner):
            raise PermissionError("Only the currently paired owner can read project reports.")
        if catalog and owner_binding(owner, values["owner_chat_id"]) != catalog["owner_binding"]:
            raise PermissionError("This owner has no registered report-read scope.")
        tables = {row[0] for row in db.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name IN ('companion_preferences','companion_context')")}
        preference = db.execute("SELECT revision,payload FROM companion_preferences WHERE owner=?", (owner,)).fetchone() if "companion_preferences" in tables else None
        context = db.execute("SELECT revision,payload FROM companion_context WHERE owner=?", (owner,)).fetchone() if "companion_context" in tables else None
        preferences = _json(preference["payload"]) if preference else {}
        view = _json(context["payload"]) if context else {}
        if not isinstance(preferences, dict) or not isinstance(view, dict):
            raise PermissionError("The owner's current project scope is unavailable.")
        return {
            "owner": owner, "preference_revision": preference["revision"] if preference else 0,
            # View navigation is not an access grant. Each read binds its own
            # explicitly requested/inferred project; privacy preferences remain live.
            "context_revision": 0,
            "enabled": preferences.get("project_context_enabled", True) is True,
            "selected_project": None,
        }

    def _snapshot(self, owner):
        # Check the live owner even when the catalog has become unreadable.
        with self.store.db() as db:
            self._principal(db, owner, None)
        catalog = self._catalog()
        with self.store.db() as db:
            principal = self._principal(db, owner, catalog)
        return {"principal": principal, "catalog": catalog, "tasks": self._registry()}

    def _recheck(self, snapshot, aliases=()):
        current = self._snapshot(snapshot["principal"]["owner"])
        if current["principal"] != snapshot["principal"] or current["catalog"] != snapshot["catalog"]:
            raise ConflictError("The owner, report catalog or privacy preferences changed during the read. Please request it again.")
        for alias in aliases:
            if (alias not in current["tasks"] or alias not in snapshot["tasks"]
                    or _target(current["tasks"], alias) != _target(snapshot["tasks"], alias)):
                raise ConflictError("This project's registered thread changed during the read. Please refresh its source.")
        return current

    @staticmethod
    def _scope(snapshot, supplied=None):
        if supplied is not None and supplied not in snapshot["tasks"]:
            raise ValueError("Choose a currently registered project scope.")
        return supplied

    @staticmethod
    def _lookup(catalog, value):
        if not isinstance(value, str) or len(value) > 600:
            return None
        exact = [entry for entry in catalog["reports"] if value == entry["id"]]
        if exact:
            return exact[0]
        matches = []
        for entry in catalog["reports"]:
            paths = list(entry.get("known_paths", []))
            if paths:
                paths.extend(str(Path(entry["root"]) / path) for path in (entry["path"], entry.get("record_path")) if path)
            if _key(value) in {_key(path) for path in paths}:
                matches.append(entry)
        return matches[0] if len(matches) == 1 else None

    def _alias(self, name, tasks):
        name = re.sub(r"\s+", " ", name).strip().casefold()
        matches = []
        for alias, info in tasks.items():
            names = {alias, alias.replace("-", " "), str(info.get("label", "")).casefold()}
            names.add(re.sub(r"\s*[·|]\s*main\s*$", "", str(info.get("label", "")), flags=re.I).casefold())
            if name in names:
                matches.append(alias)
        return matches[0] if len(matches) == 1 else None

    def parse_query(self, text, current_project=None):
        """Select read-only intent from current unquoted text; no history input."""
        if not isinstance(text, str) or not 0 < len(text) <= 16000:
            return None
        clean = unquoted_text(text)
        tasks = self._registry()
        for raw in clean.splitlines():
            line = raw.strip()
            if re.search(r"\b(?:do not|don't|don’t|never|hypothetically|example|suppose|if)\b", line, re.I):
                continue
            if line.casefold() == "/report":
                return {"kind": "progress", "task": current_project, "project": current_project}
            if (re.match(r"(?:(?:please|vega)[,:]?\s+)*(?:(?:can|could|would|will)\s+you\s+)?"
                         r"(?:show|give|send|tell|read|get|retrieve|update\s+me|what|how)\b", line, re.I)
                    and re.search(r"%|percent(?:age)?s?|\bprogress\b", line, re.I)
                    and not re.search(r"\b(?:write|draft|create|fix|implement|approve|authorize|deploy|publish|prompt|code|reply|response|message)\b", line, re.I)):
                aliases = _project_name_matches(line, tasks)
                all_projects = re.search(r"\b(?:(?:all|each|every)(?:\s+of)?(?:\s+(?:my|the))?\s+projects?|projects)\b", line, re.I)
                if all_projects or aliases or re.search(r"\b(?:project|completion)\b", line, re.I):
                    return {"kind": "progress", "task": None if all_projects or len(aliases) != 1 else aliases[0],
                            "project": current_project}
        try:
            catalog = self._catalog()
        except (ValueError, OSError, TypeError):
            catalog = {"reports": [], "default_report_id": None}
        def report(value=None):
            entry = self._lookup(catalog, value) if value else None
            if value is None:
                if current_project is not None:
                    candidates = [r for r in catalog["reports"] if r["task"] == current_project]
                    entry = candidates[0] if len(candidates) == 1 else None
                else:
                    entry = self._lookup(catalog, catalog.get("default_report_id"))
            return {"kind": "report", "report_id": entry["id"] if entry else (value if value and ID.fullmatch(value) else None),
                    "project": current_project}
        for raw in clean.splitlines():
            line = raw.strip()
            if not line or re.search(r"\b(?:do not|don't|don’t|never|not yet|not now|if|hypothetically|example|suppose)\b", line, re.I):
                continue
            command = re.fullmatch(r"/report(?:\s+(.+))?", line, re.I)
            if command:
                return report(command[1].strip() if command[1] else None)
            # The owner may explicitly repeat an earlier unanswered request:
            # "Please update me i asked coordinator ... please send me those %".
            progress = re.search(r"(?:%|percent(?:age)?s?)", line, re.I)
            project_completion = re.search(r"\b(?:completion|progress)\b", line, re.I)
            all_projects = re.search(r"\b(?:(?:each|every|all)(?:\s+of)?(?:\s+(?:my|the))?\s+projects?|projects)\b", line, re.I)
            current_read = re.match(
                r"(?:(?:please|vega)[,:]?\s+)*(?:(?:can|could|would|will)\s+you\s+)?"
                r"(?:send|show|give|get|read|retrieve|update\s+me|tell\s+me|what\s+(?:are|is))\b", line, re.I)
            if progress and project_completion and all_projects and current_read and not re.search(
                r"\b(?:write|draft|create|change|fix|deploy|publish|delete|implement|approve|authorize|authorise)\b", line, re.I
            ):
                return report(catalog.get("default_report_id") or "project-progress-20260927")
            line = re.sub(r"^(?:(?:please|vega)[,:]?\s+)+", "", line, flags=re.I)
            line = re.sub(r"^(?:can|could|would|will)\s+you\s+", "", line, flags=re.I)
            reply = re.fullmatch(r"(?:get|show|read|retrieve|send(?:\s+me)?)(?:\s+me)?\s+(?:the\s+)?(?:latest|last|recent)\s+(.+?)\s+(?:reply|response|message)[?.!]*", line, re.I)
            asked = re.fullmatch(r"what\s+did\s+(.+?)\s+(?:reply|say|respond)[?.!]*", line, re.I)
            for match in (reply, asked):
                if match:
                    name = match[1].strip()
                    if name.casefold() in ("you", "i", "they", "he", "she"):
                        return None
                    alias = self._alias(name, tasks)
                    return {"kind": "reply", "task": alias, "project": current_project}
            if re.fullmatch(r"(?:get|show|read|retrieve|send(?:\s+me)?)\s+(?:me\s+)?(?:the\s+)?(?:latest|last)\s+(?:reply|response)[?.!]*", line, re.I):
                return {"kind": "reply", "task": current_project, "project": current_project}
            query = re.fullmatch(
                r"(?:show|read|retrieve|get|send(?:\s+me)?|give(?:\s+me)?)\s+(?:me\s+)?(?:the\s+)?"
                r"(?:(latest|last|project\s+progress)\s+)?report(?:\s+(.+?))?[?.!]*", line, re.I)
            if query:
                detail = (query[2] or "").strip()
                if re.search(r"\b(?:and|then|after|before|to)\b", detail, re.I):
                    continue
                for_project = re.fullmatch(r"(?:for|from|about)\s+(.+)", detail, re.I)
                if for_project:
                    alias = self._alias(for_project[1], tasks)
                    entries = [r for r in catalog["reports"] if r["task"] == alias and r["kind"] != "markdown"]
                    if not entries:
                        entries = [r for r in catalog["reports"] if r["task"] == alias]
                    return report(entries[0]["id"]) if len(entries) == 1 else report("__unavailable__")
                return report(detail or None)
            # A known file reference is an ID mapping, not filesystem access.
            known = re.fullmatch(r"(?:show|read|retrieve|get|send(?:\s+me)?)\s+(.+)", line, re.I)
            if known and self._lookup(catalog, known[1].strip()):
                return report(known[1].strip())
        return None

    def _unavailable(self, kind, message, *, report_id=None, task=None, thread_id=None):
        return {
            "available": False, "kind": kind, "text": message, "report_id": report_id,
            "task": task, "thread_id": thread_id, "assessed_at": None, "source": None,
            "sha256": None, "retrieved_at": self.clock(), "source_error": message,
            "truncated": False, "authority": False,
        }

    def read(self, owner, query):
        if not isinstance(query, dict) or set(query) - {"kind", "report_id", "task", "project"}:
            raise ValueError("Use a registered report ID or project reply selector; file paths and actions are not accepted.")
        kind = query.get("kind")
        if kind not in ("report", "reply", "progress"):
            raise ValueError("Choose a report or a visible project reply.")
        try:
            snapshot = self._snapshot(owner)
        except PermissionError:
            raise
        except (ValueError, OSError, TypeError):
            return self._unavailable(kind, "The registered report catalog is unavailable. No report content was read.")
        if not snapshot["principal"]["enabled"]:
            return self._unavailable(kind, "Project context is disabled in your preferences. Project reports and replies were not read.")
        try:
            scope = self._scope(snapshot, query.get("project"))
        except ValueError as error:
            return self._unavailable(kind, str(error))
        if kind == "reply":
            return self._read_reply(owner, query.get("task") or scope, snapshot, scope)
        if kind == "progress":
            return self._read_current_progress(snapshot, query.get("task"), scope)
        entry = self._lookup(snapshot["catalog"], query.get("report_id"))
        if entry is None:
            return self._unavailable(kind, "That report is not registered or the request needs a report ID. Choose an available report; arbitrary files are not readable.")
        alias = entry["task"]
        if scope is not None and alias != scope:
            return self._unavailable(kind, "That report belongs to a different project scope. Select its project or personal scope before requesting it.",
                                     report_id=entry["id"], task=alias, thread_id=entry["thread_id"])
        try:
            self._binding(snapshot, entry)
            result = self._read_report(snapshot, entry)
        except ConflictError:
            raise
        except (ValueError, OSError, TypeError, KeyError, UnicodeError):
            self._recheck(snapshot)
            return self._unavailable(kind, "The registered report source could not be verified. No older report is being shown as current.",
                                     report_id=entry["id"], task=alias, thread_id=entry["thread_id"])
        self._recheck(snapshot, (alias,))
        return result

    @staticmethod
    def _binding(snapshot, entry):
        tasks = snapshot["tasks"]
        if entry["task"] not in tasks or _target(tasks, entry["task"])["thread_id"] != entry["thread_id"]:
            raise ConflictError("The report's registered project thread changed. Its old read binding is no longer valid.")

    def _read_current_progress(self, snapshot, task, scope):
        if task is not None and task not in snapshot["tasks"]:
            return self._unavailable("progress", "Choose a registered project.")
        if scope is not None and task not in (None, scope):
            return self._unavailable("progress", "Choose this project's context before reading its progress.")
        selected = task or scope
        entry = self._lookup(snapshot["catalog"], snapshot["catalog"].get("default_report_id"))
        if not entry:
            return self._unavailable("progress", "No project progress report is registered.")
        self._binding(snapshot, entry)
        try:
            base = self._read_report(snapshot, entry)
        except (ValueError, OSError, KeyError):
            return self._unavailable("progress", "The project progress source is unavailable.")
        if "projects" not in base:
            return base
        live = self.progress.current(snapshot["principal"]["owner"], scope=selected)
        rows = []
        for original in base["projects"]:
            if selected is not None and original["task"] != selected:
                continue
            row = dict(original)
            current = live["projects"].get(row["task"])
            if current:
                row.update(current)
            else:
                row["kind"] = "estimate" if str(row.get("display_progress", "")).startswith("≈") else "reported"
            if row["task"] in live["errors"]:
                row["source_error"] = live["errors"][row["task"]]
            rows.append(row)
        if not rows:
            return self._unavailable("progress", "A percentage has not been reported for this project.")
        lines = ["**Project progress**"]
        for index, row in enumerate(rows, 1):
            label = row.get("display_progress") or "Not yet estimated"
            kind = "Measured" if row.get("kind") == "measured" else "Estimate" if row.get("kind") == "estimate" else "Reported"
            lines.append(f"{index}. **{row['project']} — {label}**\n"
                         f"{row.get('basis_and_remaining') or 'Remaining work not reported.'}\n"
                         + (f"{row['summary']}\n" if row.get("automatic") and row.get("summary") else "")
                         + f"{kind} · {_date_label(row.get('assessed_at'))}")
            if row.get("source_error") or row.get("stale"):
                lines[-1] += "\nAwaiting a fresh source update."
        self._recheck(snapshot, [selected] if selected else [])
        body = "\n\n".join(lines)
        source = {"kind": "current_project_progress", "baseline": base["source"],
                  "project_sources": [{"task": row["task"], "assessed_at": row.get("assessed_at"),
                                       "kind": row.get("kind"), "source": row.get("source")} for row in rows]}
        return {
            "available": True, "kind": "progress", "report_id": "project-progress:" + (selected or "all"),
            "task": selected or entry["task"], "thread_id": _target(snapshot["tasks"], selected or entry["task"])["thread_id"],
            "text": body, "projects": rows, "source": source, "sha256": digest(body.encode()),
            "assessed_at": None, "retrieved_at": self.clock(), "truncated": False, "authority": False,
        }

    def _read_report(self, snapshot, entry):
        files = ProjectFiles(entry["root"], entry["root_identity"])
        document = files.read(entry["path"])
        if entry.get("sha256") and document["sha256"] != entry["sha256"]:
            raise ValueError("The registered report document changed.")
        source = {"kind": "registered_report", "path": entry["path"], "sha256": document["sha256"]}
        assessed = entry.get("assessed_at")
        if assessed is not None and _seconds(assessed) > self.clock() + FUTURE_TOLERANCE:
            raise ValueError("The registered assessment is dated in the future.")
        result = {
            "available": True, "kind": "report", "report_id": entry["id"], "task": entry["task"],
            "thread_id": entry["thread_id"], "assessed_at": assessed, "source": source,
            "sha256": document["sha256"], "retrieved_at": self.clock(), "source_error": None,
            "truncated": False, "authority": False, "freshness": "dated_assessment" if assessed else "undated_source",
        }
        if entry["kind"] == "markdown":
            result["text"] = (f"{entry['label']}\nAssessment: {_date_label(assessed)}.\n"
                              "This is a recorded report, not a live project-status check.\n\n" + document["text"])
            return result
        record = files.read(entry["record_path"])
        if entry.get("record_sha256") and record["sha256"] != entry["record_sha256"]:
            raise ValueError("The registered assessment record changed.")
        value = self._assessment_record(record["text"], document["text"], entry)
        source.update({"record_path": entry["record_path"], "record_sha256": record["sha256"]})
        result.update({
            "sha256": record["sha256"], "assessed_at": value["assessed_at"],
            "scope_note": value["scope_note"], "source_freshness_note": value["source_freshness_note"],
            "project_count": len(value["projects"]), "projects": value["projects"],
        })
        parts = [f"**{entry['label']}**", f"Assessment: {_date_label(value['assessed_at'])}.",
                 "These are recorded assessments, not current project completion measurements.",
                 value["scope_note"], value["source_freshness_note"]]
        for index, item in enumerate(value["projects"], 1):
            parts.append(
                f"{index}. **{item['project']} — {item.get('display_progress') or 'Progress not recorded'}**\n"
                f"Remaining / recorded state: {item.get('basis_and_remaining') or 'Not recorded'}\n"
                f"Recorded workflow: {(item.get('scheduling_state') or 'Not recorded').replace('_', ' ')}"
            )
            if item.get("source_event_at"):
                parts[-1] += f"\nSource measurement: {_date_label(item['source_event_at'])}."
            if item["missing_fields"]:
                parts[-1] += "\nNot recorded: " + ", ".join(field.replace("_", " ") for field in item["missing_fields"]) + "."
        result["text"] = "\n\n".join(parts)
        return result

    def _assessment_record(self, text, markdown, entry):
        value = _json(text)
        if (not isinstance(value, dict) or not isinstance(value.get("projects"), list)
                or len(value["projects"]) != entry["expected_projects"]):
            raise ValueError("The assessment project count does not match its registered source.")
        assessed = value.get("assessed_at")
        stamp = _seconds(assessed)
        if stamp is None or stamp > self.clock() + FUTURE_TOLERANCE or (
            entry.get("assessed_at") is not None and _seconds(entry["assessed_at"]) != stamp
        ):
            raise ValueError("The assessment date does not match its registered source.")
        for field in ("scope_note", "source_freshness_note"):
            _string(value.get(field), 4000)
            if " ".join(value[field].split()) not in " ".join(markdown.split()):
                raise ValueError("The document and assessment source notes do not match.")
        rows = []
        seen = set()
        for item in value["projects"]:
            if not isinstance(item, dict) or not ALIAS.fullmatch(_string(item.get("task"), 80)):
                raise ValueError("The assessment has an invalid project entry.")
            if item["task"] in seen:
                raise ValueError("The assessment repeats a project.")
            seen.add(item["task"])
            row = {"task": item["task"], "project": _string(item.get("project"), 200), "missing_fields": []}
            for field in ("display_progress", "basis_and_remaining", "scheduling_state", "measurement_kind"):
                row[field] = _string(item[field], 4000) if item.get(field) is not None else None
                if row[field] is None:
                    row["missing_fields"].append(field)
            percent = item.get("percent")
            if percent is not None and (type(percent) not in (int, float) or not math.isfinite(percent) or not 0 <= percent <= 100):
                raise ValueError("The assessment percentage is invalid.")
            row["percent"] = percent
            if "percent" not in item:
                row["missing_fields"].append("percent")
            row["assessed_at"] = item.get("assessed_at")
            if row["assessed_at"] is None:
                row["missing_fields"].append("assessed_at")
            elif _seconds(row["assessed_at"]) != stamp:
                raise ValueError("The per-project assessment date does not match the report.")
            if item.get("source_event_at") is not None:
                if _seconds(item["source_event_at"]) is None or _seconds(item["source_event_at"]) > stamp:
                    raise ValueError("The source event date is invalid.")
                row["source_event_at"] = item["source_event_at"]
            rows.append(row)
        table = []
        for line in markdown.splitlines():
            if not line.lstrip().startswith("|"):
                continue
            cells = [cell.strip().replace(r"\|", "|") for cell in re.split(r"(?<!\\)\|", line.strip())[1:-1]]
            if len(cells) == 3 and cells[0] != "Project" and not re.fullmatch(r"[-: ]+", cells[0]):
                table.append(cells)
        expected = [[row["project"], row["display_progress"] or "", row["basis_and_remaining"] or ""] for row in rows]
        if table != expected:
            raise ValueError("The report table and assessment record disagree.")
        return {"assessed_at": assessed, "scope_note": value["scope_note"],
                "source_freshness_note": value["source_freshness_note"], "projects": rows}

    def capabilities(self, owner):
        try:
            snapshot = self._snapshot(owner)
        except PermissionError:
            raise
        except (ValueError, OSError, TypeError):
            return {"version": READ_VERSION, "available": False, "reports": [], "reply_projects": [],
                    "source_error": "The registered report catalog is unavailable.",
                    "telegram": "No registered report source is currently available.",
                    "local_project_tools": "The separate private Project tools view retains its own registered folder grants."}
        enabled = snapshot["principal"]["enabled"]
        scope = self._scope(snapshot)
        reports, replies = [], []
        if enabled:
            for entry in snapshot["catalog"]["reports"]:
                if scope is not None and entry["task"] != scope:
                    continue
                try:
                    self._binding(snapshot, entry)
                except (ValueError, KeyError):
                    continue
                reports.append({key: entry.get(key) for key in ("id", "label", "task", "thread_id", "assessed_at", "kind")})
            if snapshot["catalog"].get("sessions"):
                for alias, info in snapshot["tasks"].items():
                    if scope is not None and alias != scope:
                        continue
                    try:
                        target = _target(snapshot["tasks"], alias)
                    except (ValueError, KeyError):
                        continue
                    replies.append({"alias": alias, "label": info["label"], "thread_id": target["thread_id"]})
        self._recheck(snapshot)
        return {
            "version": READ_VERSION, "available": enabled, "project_context_enabled": enabled,
            "selected_project": scope, "reports": reports, "reply_projects": replies, "source_error": None,
            "telegram": ("Explicit owner requests can read cataloged report content and available visible final replies "
                         "from registered current project threads. Reads do not route work or approve actions."
                         if enabled else "Project context is disabled; project reports and replies cannot currently be read."),
            "local_project_tools": ("Folder browsing, file search, model-prepared edits and reviewed writes remain in the "
                                    "private local Project tools view under its separate grants. Telegram read access does not expose those tools."),
            "limitations": ["Report dates are retained; a retrieval time is not a new assessment.",
                            "Reply reads use visible-final cache and bounded tails of exact existing watcher files.",
                            "No arbitrary files, account access, reasoning, tool output, imported user messages, model calls or actions."],
            "authority": False,
            "progress": {
                "available": enabled,
                "default_report": "Current measured progress and the latest recorded project estimates.",
                "automatic_source": "Accepted source events are tracked separately from project progress updates.",
                "coordinator_updates": "Source-dated progress fields can update other project estimates.",
                "historical_report": "An explicit dated report ID still returns its unchanged historical content.",
            },
        }

    def progress_assessments(self, owner):
        """Return dated UI fields only for aliases that are currently registered."""
        result = {"projects": {}, "assessed_at": None, "source": None, "source_error": None, "checked_at": None,
                  "source_errors": {}}
        snapshot, scope = None, None
        try:
            snapshot = self._snapshot(owner)
            if not snapshot["principal"]["enabled"]:
                result["source_error"] = "Project context is disabled."
                return result
            scope = self._scope(snapshot)
            entries = [r for r in snapshot["catalog"]["reports"] if r["kind"] == "progress_overview"]
            if len(entries) != 1:
                result["source_error"] = "No single registered progress assessment is available."
                return result
            entry = entries[0]
            self._binding(snapshot, entry)
            report = self._read_report(snapshot, entry)
            aliases = [entry["task"]]
            for item in report["projects"]:
                alias = item["task"]
                if alias not in snapshot["tasks"] or (scope is not None and alias != scope):
                    continue
                target = _target(snapshot["tasks"], alias)
                aliases.append(alias)
                result["projects"][alias] = {
                    **item, "thread_id": target["thread_id"], "source": report["source"],
                    "report_id": report["report_id"], "source_error": None,
                    "source_freshness_note": report["source_freshness_note"], "scope_note": report["scope_note"],
                    "freshness": "dated_assessment",
                    "kind": "estimate" if str(item.get("display_progress", "")).startswith("≈") else "reported",
                }
            self._recheck(snapshot, aliases)
            result.update({key: report[key] for key in ("assessed_at", "source")})
            result["checked_at"] = report["retrieved_at"]
        except (PermissionError, ConflictError):
            raise
        except (ValueError, OSError, TypeError, KeyError, UnicodeError):
            result = {**result, "projects": {}, "source_error": "The registered progress source could not be verified; no current percentage is available."}
        if snapshot and snapshot["principal"]["enabled"]:
            current = self.progress.current(owner, scope=scope)
            result["source_errors"] = current["errors"]
            for alias, value in current["projects"].items():
                previous = result["projects"].get(alias, {})
                if value["assessed_at"] >= (_seconds(previous.get("assessed_at")) or 0):
                    result["projects"][alias] = {
                        **previous, **value, "task": alias,
                        "project": snapshot["tasks"][alias]["label"],
                        "thread_id": _target(snapshot["tasks"], alias)["thread_id"],
                    }
            self._recheck(snapshot, result["projects"])
        return result

    def cached_replies(self, owner):
        """Current registered final replies for views, without rescanning histories."""
        snapshot = self._snapshot(owner)
        if not snapshot["principal"]["enabled"]:
            return {}
        scope = self._scope(snapshot)
        with self.store.db() as db:
            rows = [dict(row) for row in db.execute(
                "SELECT * FROM project_visible_replies WHERE owner=? ORDER BY observed_at DESC", (owner,))]
        result = {}
        for alias in snapshot["tasks"]:
            if scope is not None and scope != alias:
                continue
            target = _target(snapshot["tasks"], alias)
            match = next((row for row in rows if row["thread_id"] == target["thread_id"]
                          and row["task"] in (alias, target["alias"])), None)
            if match:
                result[alias] = {
                    "thread_id": match["thread_id"], "observed_at": match["observed_at"],
                    "text": match["text"], "source_error": match["source_error"],
                    "source_ref": match["source_ref"], "truncated": bool(match["truncated"]),
                }
        self._recheck(snapshot, result)
        return result

    def _reply_content(self, text):
        if not isinstance(text, str) or not text.strip():
            raise ValueError("No visible final reply text was recorded.")
        if len(text) > MAX_REPLY_INPUT_CHARS:
            raise ValueError("The visible reply exceeds the bounded source size.")
        # Check the whole bounded source, including beyond the displayed prefix.
        safe = text_from_bytes(text.encode("utf-8"))
        if contains_secret(safe):
            raise ValueError("The latest visible reply contains excluded credential-like content.")
        return {"body": safe[:MAX_REPLY_CHARS], "sha256": digest(text.encode("utf-8")),
                "truncated": len(safe) > MAX_REPLY_CHARS, "original_characters": len(safe)}

    def record_reply(self, task, thread_id, text, observed_at, source_ref):
        """Internal watcher hook for visible task_complete finals; no delivery."""
        owner = self.store.get("owner_id")
        if type(owner) is not int:
            return False
        try:
            snapshot = self._snapshot(owner)
            if not snapshot["principal"]["enabled"] or task not in snapshot["tasks"]:
                return False
            target = _target(snapshot["tasks"], task)
            observed = _seconds(observed_at)
            if target["thread_id"] != thread_id or observed is None or observed > self.clock() + FUTURE_TOLERANCE:
                return False
            if isinstance(text, str) and (text.lstrip().startswith("<heartbeat>") or text.strip() == "DONT_NOTIFY"):
                return False
            try:
                content = self._reply_content(text)
                error = None
            except (ValueError, UnicodeError):
                content = {"body": "", "sha256": None, "truncated": False, "original_characters": 0}
                error = "The latest visible reply could not be exposed safely. Its text was not retained."
            reference = source_ref if isinstance(source_ref, str) and re.fullmatch(r"[A-Za-z0-9_.: -]{1,200}", source_ref) and not contains_secret(source_ref) else "visible-task-completion"
            with self.store.db() as db:
                db.execute("BEGIN IMMEDIATE")
                if self._principal(db, owner, snapshot["catalog"]) != snapshot["principal"]:
                    return False
                if _target(self._registry(), task)["thread_id"] != thread_id:
                    return False
                return db.execute(
                    "INSERT INTO project_visible_replies VALUES(?,?,?,?,?,?,?,?,?,?) "
                    "ON CONFLICT(owner,task,thread_id) DO UPDATE SET text=excluded.text,"
                    "observed_at=excluded.observed_at,source_ref=excluded.source_ref,sha256=excluded.sha256,"
                    "truncated=excluded.truncated,original_characters=excluded.original_characters,source_error=excluded.source_error "
                    "WHERE excluded.observed_at>project_visible_replies.observed_at",
                    (owner, task, thread_id, content["body"], observed, reference, content["sha256"],
                     int(content["truncated"]), content["original_characters"], error),
                ).rowcount == 1
        except (PermissionError, ValueError, OSError, TypeError, KeyError):
            return False

    @contextmanager
    def _session_stream(self, files, relative):
        relative = relative_path(relative)
        path = files.root / relative
        if path.suffix != ".jsonl":
            raise ValueError("Only the exact registered watcher session is readable.")
        parent = str(Path(relative).parent).replace("\\", "/")
        parent = "" if parent == "." else parent
        # ProjectFiles pins/verifies the root and each ancestor on Windows.
        with files.open(parent, directory=True):
            if os.name == "nt":
                from project_files import _windows_handle
                # Codex retains a writer on an active event stream. Permit its
                # appends while pinning identity/no-delete; _session_tail checks
                # size and modification time again before exposing any text.
                with _windows_handle(path, directory=False, appendable_read=True) as stream:
                    yield stream
            else:
                descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
                with os.fdopen(descriptor, "rb") as stream:
                    info = os.fstat(stream.fileno())
                    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                        raise ValueError("Linked session sources are excluded.")
                    yield stream

    def _session_tail(self, path, thread_id, sessions):
        root = self.session_root
        path = Path(path)
        if not path.is_absolute():
            raise ValueError("The watcher path is not an absolute registered source.")
        relative = path.relative_to(root).as_posix()
        relative_path(relative)
        if not re.fullmatch(r"\d{4}/\d{2}/\d{2}/rollout-[0-9T-]+-" + re.escape(thread_id) + r"\.jsonl", relative):
            raise ValueError("The watcher path does not identify the current registered session.")
        files = ProjectFiles(root, sessions["root_identity"])
        with self._session_stream(files, relative) as stream:
            before = os.fstat(stream.fileno())
            header = stream.readline(MAX_SESSION_HEADER_BYTES + 1)
            if len(header) > MAX_SESSION_HEADER_BYTES or not header.endswith(b"\n"):
                raise ValueError("The session identity header is unavailable.")
            meta = _json(header)
            if (not isinstance(meta, dict) or meta.get("type") != "session_meta"
                    or not isinstance(meta.get("payload"), dict) or meta["payload"].get("id") != thread_id):
                raise ValueError("The session header does not match the registered project thread.")
            start = max(len(header), before.st_size - MAX_SESSION_TAIL_BYTES)
            stream.seek(start)
            data = stream.read(min(MAX_SESSION_TAIL_BYTES, before.st_size - start))
            after = os.fstat(stream.fileno())
            if (file_identity(before) != file_identity(after) or before.st_size != after.st_size
                    or before.st_mtime_ns != after.st_mtime_ns):
                raise ValueError("The session changed during the bounded read.")
        lines = data.splitlines(keepends=True)
        if start > len(header) and lines:
            lines = lines[1:]
        selected = None
        for line in lines:
            if not line.endswith(b"\n") or len(line) > MAX_SESSION_RECORD_BYTES:
                continue
            try:
                event = _json(line)
                if not isinstance(event, dict) or not isinstance(event.get("payload"), dict):
                    continue
                payload = event["payload"]
                body = None
                if event.get("type") == "event_msg" and payload.get("type") == "task_complete":
                    body = payload.get("last_agent_message")
                elif (event.get("type") == "response_item" and payload.get("type") == "message"
                      and payload.get("role") == "assistant"
                      and payload.get("phase", payload.get("channel")) in ("final", "final_answer")
                      and payload.get("recipient") in (None, "all")):
                    chunks = payload.get("content")
                    if isinstance(chunks, list) and chunks and all(isinstance(c, dict) and c.get("type") == "output_text" and isinstance(c.get("text"), str) for c in chunks):
                        body = "".join(chunk["text"] for chunk in chunks)
                if not isinstance(body, str) or body.lstrip().startswith("<heartbeat>") or body.strip() == "DONT_NOTIFY":
                    continue
                observed = _seconds(event.get("timestamp"))
                if observed is None or observed > self.clock() + FUTURE_TOLERANCE:
                    continue
                if selected is not None and selected["observed_at"] > observed:
                    continue
                try:
                    content = self._reply_content(body)
                    error = None
                except (ValueError, UnicodeError):
                    content = {"body": "", "sha256": None, "truncated": False, "original_characters": 0}
                    error = "The latest visible reply could not be exposed safely."
                selected = {**content, "observed_at": observed, "source_error": error,
                            "source_ref": "session-visible-final", "record_sha256": digest(line)}
            except (ValueError, UnicodeError, TypeError, KeyError, OverflowError):
                continue
        return selected, start > len(header)

    def _read_reply(self, owner, alias, snapshot, scope):
        if not isinstance(alias, str) or alias not in snapshot["tasks"]:
            return self._unavailable("reply", "Choose a registered project whose visible reply you want to read.")
        target = _target(snapshot["tasks"], alias)
        thread_id = target["thread_id"]
        report_id = f"reply:{alias}:{thread_id}"
        if scope is not None and scope != alias:
            return self._unavailable("reply", "That reply belongs to a different selected project scope.",
                                     report_id=report_id, task=alias, thread_id=thread_id)
        with self.store.db() as db:
            self._principal(db, owner, snapshot["catalog"])
            row = db.execute(
                "SELECT * FROM project_visible_replies WHERE owner=? AND thread_id=? AND task IN (?,?) "
                "ORDER BY observed_at DESC LIMIT 1", (owner, thread_id, alias, target["alias"]),
            ).fetchone()
            suffix = thread_id + ".jsonl"
            paths = [item["path"] for item in db.execute(
                "SELECT path FROM cursors WHERE substr(path,-?)=? ORDER BY path LIMIT ?",
                (len(suffix), suffix, MAX_SESSION_FILES + 1))]
        best = None
        if row:
            best = {**dict(row), "body": row["text"]}
            if best["body"]:
                try:
                    self._reply_content(best["body"])
                except (ValueError, UnicodeError):
                    best["body"] = ""
                    best["source_error"] = "The cached visible reply could not be exposed safely."
        errors = []
        limited = False
        sessions = snapshot["catalog"].get("sessions")
        if not sessions:
            errors.append("No session-tail source is registered; only an observed final-reply cache is available.")
        elif len(paths) > MAX_SESSION_FILES:
            errors.append("Too many watcher sources match this thread; the current reply source could not be verified.")
        else:
            if not paths:
                errors.append("No existing watcher file is registered for this thread; only an observed final-reply cache is available.")
            for path in paths:
                try:
                    candidate, bounded = self._session_tail(path, thread_id, sessions)
                    limited = limited or bounded
                    if candidate is not None and (best is None or candidate["observed_at"] >= best["observed_at"]):
                        best = candidate
                except (ValueError, OSError, TypeError, KeyError, UnicodeError):
                    errors.append("A registered session source could not be verified; the last cached reply may be older.")
        self._recheck(snapshot, (alias,))
        if best is None:
            return self._unavailable("reply", "No visible final reply was found in this project's bounded registered sources. " + " ".join(dict.fromkeys(errors)),
                                     report_id=report_id, task=alias, thread_id=thread_id)
        if best["source_error"]:
            return self._unavailable("reply", best["source_error"], report_id=report_id, task=alias, thread_id=thread_id)
        source_error = " ".join(dict.fromkeys(errors)) or None
        source = {"kind": "visible_assistant_reply", "reference": best["source_ref"],
                  "thread_id": thread_id, "observed_at": _iso(best["observed_at"]),
                  "record_sha256": best.get("record_sha256"), "history_limited": limited,
                  "tail_limit_bytes": MAX_SESSION_TAIL_BYTES, "authority": False}
        lines = [f"{snapshot['tasks'][alias]['label']} — available visible reply",
                 f"Source reply date: {_date_label(best['observed_at'])}.",
                 "This is a recorded reply, not a new project status check."]
        if source_error:
            lines.append("Source check: " + source_error)
        lines.append(best["body"])
        if best["truncated"]:
            lines.append(f"[Reply truncated to {MAX_REPLY_CHARS:,} characters from {best['original_characters']:,}.]")
        return {
            "available": True, "kind": "reply", "report_id": report_id, "task": alias, "thread_id": thread_id,
            "assessed_at": _iso(best["observed_at"]), "observed_at": best["observed_at"],
            "source": source, "sha256": best["sha256"], "retrieved_at": self.clock(),
            "source_error": source_error, "text": "\n\n".join(lines), "truncated": bool(best["truncated"]),
            "history_limited": limited, "authority": False, "freshness": "recorded_reply",
        }
