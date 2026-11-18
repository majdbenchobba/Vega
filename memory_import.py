"""Local, reviewed imports of recognized visible-message export formats.

No account login, URL fetching, code execution, tool-output import or reasoning
decryption is performed. Original files are never modified or extracted to disk.
"""
from __future__ import annotations

import hashlib
import io
import json
from pathlib import PurePosixPath, PureWindowsPath
import re
import threading
import time
import uuid
import zipfile

from companion import ConflictError, text
from memory_policy import contains_secret, fingerprint, local_candidates, scrub_secrets


MAX_UPLOAD = 20 * 1024 * 1024
MAX_EXPANDED = 25 * 1024 * 1024
MAX_RECORDS = 2000
FORMATS = {
    "chatgpt": "ChatGPT conversation JSON",
    "codex": "Codex visible-message JSONL or thread JSON",
    "claude": "Claude conversation JSON",
    "vega": "Vega context export",
    "notes": "Plain text, Markdown or notes JSON",
}


class ImportFormatError(ValueError):
    pass


def _parts(value):
    if isinstance(value, str):
        return value
    if not isinstance(value, list):
        return ""
    output = []
    for part in value:
        if isinstance(part, str):
            output.append(part)
        elif isinstance(part, dict) and part.get("type") in ("text", "input_text", "output_text") and isinstance(part.get("text"), str):
            output.append(part["text"])
    return "\n".join(output)


def _identifier(value, fallback):
    selected = value if isinstance(value, str) and value else str(value) if type(value) is int else str(fallback)
    _scalar_text(selected)
    if contains_secret(selected):
        return "redacted-" + fingerprint(selected)[:20]
    return selected[:256]


def _title(value, fallback):
    if isinstance(value, str) and value.strip():
        _scalar_text(value)
        return scrub_secrets(value)[:180]
    return fallback


def _scalar_text(value):
    try:
        value.encode("utf-8")
    except UnicodeError:
        raise ImportFormatError("The export contains invalid Unicode text.") from None
    return value


def _scope_label(value):
    if not isinstance(value, str):
        return None
    _scalar_text(value)
    return None if contains_secret(value) else value[:180]


def _visible(value):
    _scalar_text(value)
    output, stack, previous = [], [], 0
    for tag in re.finditer(r"<\s*(/?)\s*(analysis|thinking|reasoning)\b[^>]*>", value, re.I):
        if not stack:
            output.append(value[previous:tag.start()])
        name = tag[2].lower()
        if not tag[1]:
            stack.append(name)
        elif name in stack:
            while stack:
                if stack.pop() == name:
                    break
        previous = tag.end()
    if not stack:
        output.append(value[previous:])
    value = "".join(output)
    if re.match(r"\s*<(?:environment_context|permissions instructions|codex_delegation|system_reminder)\b", value, re.I):
        return ""
    return value.strip()


def _chatgpt(data, stats):
    conversations = data.get("conversations") if isinstance(data, dict) else data
    if not isinstance(conversations, list):
        raise ImportFormatError("Expected ChatGPT conversations.json with conversation mappings.")
    recognized = False
    for conversation in conversations:
        if not isinstance(conversation, dict) or not isinstance(conversation.get("mapping"), dict):
            continue
        recognized = True
        mapping = conversation["mapping"]
        if len(mapping) > 30000:
            raise ImportFormatError("A conversation exceeds the supported node limit.")
        current = conversation.get("current_node") if isinstance(conversation.get("current_node"), str) else None
        if current not in mapping:
            leaves = [key for key, value in mapping.items() if isinstance(value, dict) and not value.get("children")]
            if len(leaves) != 1:
                stats["ambiguous_branches"] += 1
                continue
            current = leaves[0]
        chain, seen = [], set()
        while current in mapping:
            if current in seen:
                raise ImportFormatError("A conversation mapping contains a cycle.")
            seen.add(current)
            node = mapping[current]
            if not isinstance(node, dict):
                break
            chain.append((current, node))
            current = node.get("parent") if isinstance(node.get("parent"), str) else None
        title = _title(conversation.get("title"), "ChatGPT conversation")
        conversation_id = _identifier(conversation.get("id") or conversation.get("conversation_id"), fingerprint(title))
        for key, node in reversed(chain):
            message = node.get("message")
            if not isinstance(message, dict):
                continue
            author = message.get("author")
            role = author.get("role") if isinstance(author, dict) else None
            content = message.get("content") or {}
            metadata = message.get("metadata")
            if (role not in ("user", "assistant") or message.get("channel") not in (None, "final")
                    or message.get("recipient") not in (None, "all")
                    or isinstance(metadata, dict) and metadata.get("is_visually_hidden_from_conversation") is True
                    or not isinstance(content, dict) or content.get("content_type") not in ("text", "multimodal_text")):
                stats["non_visible"] += 1
                continue
            yield {"title": title, "text": _parts(content.get("parts")), "role": role,
                   "source_id": conversation_id + ":" + _identifier(message.get("id"), key),
                   "original_scope": None}
    if conversations and not recognized:
        raise ImportFormatError("This JSON does not contain supported ChatGPT conversation mappings.")


def _claude(data, stats):
    conversations = data.get("conversations") if isinstance(data, dict) else data
    if not isinstance(conversations, list):
        raise ImportFormatError("Expected Claude conversation JSON.")
    recognized = False
    for conversation in conversations:
        if not isinstance(conversation, dict) or not isinstance(conversation.get("chat_messages"), list):
            continue
        recognized = True
        title = _title(conversation.get("name") or conversation.get("title"), "Claude conversation")
        conversation_id = _identifier(conversation.get("uuid") or conversation.get("id"), fingerprint(title))
        for index, message in enumerate(conversation["chat_messages"]):
            if not isinstance(message, dict):
                continue
            sender = message.get("sender")
            role = {"human": "user", "user": "user", "assistant": "assistant"}.get(sender) if isinstance(sender, str) else None
            if not role:
                stats["non_visible"] += 1
                continue
            body = _parts(message["content"]) if isinstance(message.get("content"), list) else message.get("text", "")
            yield {"title": title, "text": body if isinstance(body, str) else "", "role": role,
                   "source_id": conversation_id + ":" + _identifier(message.get("uuid") or message.get("id"), index),
                   "original_scope": _scope_label(conversation.get("project_uuid"))}
    if conversations and not recognized:
        raise ImportFormatError("This JSON does not contain supported Claude chat_messages.")


def _codex_json(data, stats):
    if isinstance(data, dict) and isinstance(data.get("result"), dict):
        data = data["result"]
    thread = data.get("thread", data) if isinstance(data, dict) else None
    if not isinstance(thread, dict) or not isinstance(thread.get("turns"), list):
        raise ImportFormatError("Expected Codex thread JSON or a JSONL session export.")
    thread_id = _identifier(thread.get("id"), "codex-thread")
    title = _title(thread.get("title") or thread.get("name"), "Codex conversation")
    for turn_index, turn in enumerate(thread["turns"]):
        if not isinstance(turn, dict):
            continue
        items = turn.get("items")
        for index, item in enumerate(items if isinstance(items, list) else []):
            if not isinstance(item, dict):
                continue
            kind = item.get("type")
            role = {"userMessage": "user", "agentMessage": "assistant"}.get(kind) if isinstance(kind, str) else None
            if not role or item.get("phase") not in (None, "final", "final_answer") or item.get("channel") not in (None, "final", "final_answer"):
                stats["non_visible"] += 1
                continue
            body = _parts(item.get("content")) if kind == "userMessage" else item.get("text", "")
            if not body and isinstance(item.get("text"), str):
                body = item["text"]
            yield {"title": title, "text": body, "role": role,
                   "source_id": thread_id + ":" + _identifier(item.get("id"), f"{turn_index}:{index}"),
                   "original_scope": None}


def _codex_lines(raw, stats):
    thread_id, title = "codex-session", "Codex conversation"
    records, recognized = [], False
    for index, line in enumerate(raw.splitlines()):
        if not line.strip():
            continue
        try:
            item = json.loads(line)
        except (ValueError, RecursionError):
            stats["invalid_records"] += 1
            continue
        if not isinstance(item, dict):
            continue
        kind, payload = item.get("type"), item.get("payload")
        if not isinstance(payload, dict):
            continue
        if kind == "session_meta":
            recognized = True
            thread_id = _identifier(payload.get("id"), thread_id)
            continue
        role, body = None, ""
        if kind == "response_item":
            recognized = True
            if (payload.get("type") == "message" and payload.get("role") in ("user", "assistant")
                    and payload.get("phase") in (None, "final", "final_answer")
                    and payload.get("channel") in (None, "final", "final_answer")):
                role, body = payload["role"], _parts(payload.get("content"))
            else:
                stats["non_visible"] += 1
        elif kind == "event_msg":
            recognized = True
            event = payload.get("type")
            if event == "user_message":
                role, body = "user", _parts(payload.get("message"))
            elif event == "agent_message" and payload.get("phase") in (None, "final", "final_answer") and payload.get("channel") in (None, "final", "final_answer"):
                role, body = "assistant", payload.get("message", "")
            else:
                stats["non_visible"] += 1
        if role and isinstance(body, str):
            records.append({"title": title, "text": body, "role": role,
                            "source_id": thread_id + ":" + _identifier(payload.get("id") or item.get("timestamp"), index),
                            "original_scope": None})
    if not recognized:
        raise ImportFormatError("This JSONL file has no supported Codex visible-message records.")
    yield from records


def _notes(data, provider, stats):
    if provider == "vega":
        if not isinstance(data, dict) or data.get("format") not in ("vega-owner-context-v1", "vega-memory-v2") or not isinstance(data.get("memory"), list):
            raise ImportFormatError("Expected a Vega context export with a memory list.")
        entries = data["memory"]
    elif isinstance(data, dict):
        entries = data.get("memories", data.get("notes"))
    else:
        entries = data
    if not isinstance(entries, list):
        raise ImportFormatError("Expected a list of text notes or a memories/notes list.")
    for index, item in enumerate(entries):
        if isinstance(item, str):
            title, body, source_id, scope = "Imported note", item, str(index), None
        elif isinstance(item, dict):
            title = _title(item.get("title"), "Imported note")
            body = item.get("body", item.get("text"))
            source_id = _identifier(item.get("id"), index)
            scope = _scope_label(item.get("project"))
        else:
            stats["invalid_records"] += 1
            continue
        if isinstance(body, str):
            yield {"title": title, "text": body, "role": "note", "source_id": source_id, "original_scope": scope}


def _plain(raw):
    raw = re.sub(r"\A---\s*\n[\s\S]*?\n---\s*\n", "", raw)
    pieces = re.split(r"\n\s*\n|^\s*[-*]\s+", raw, flags=re.M)
    for index, piece in enumerate(pieces):
        piece = piece.strip()
        if piece:
            yield {"title": "Imported note", "text": piece, "role": "note", "source_id": str(index), "original_scope": None}


def parse_export(data, filename, provider, *, mode="suggestions"):
    if not isinstance(provider, str) or provider not in FORMATS or mode not in ("suggestions", "references"):
        raise ImportFormatError("Choose a supported source format and review mode.")
    if not isinstance(data, bytes) or not 0 < len(data) <= MAX_UPLOAD:
        raise ImportFormatError("Choose a file of at most 20 MB.")
    filename = text(filename, "Filename", 180)
    if contains_secret(filename):
        raise ImportFormatError("The filename contains a credential-like value.")
    stats = {"visible_records": 0, "non_visible": 0, "sensitive": 0, "invalid_records": 0,
             "duplicates": 0, "ambiguous_branches": 0, "incidental": 0, "limit_reached": False}
    source_member = None
    if filename.lower().endswith(".zip"):
        try:
            with zipfile.ZipFile(io.BytesIO(data)) as archive:
                members = archive.infolist()
                if len(members) > 2000:
                    raise ImportFormatError("This ZIP contains too many entries.")
                wanted = ["conversations.json"] if provider in ("chatgpt", "claude") else ["memory.json", "memories.json", "vega-context.json"]
                matches = [member for member in members if PurePosixPath(member.filename).name.lower() in wanted]
                if provider == "codex":
                    matches = [member for member in members if member.filename.lower().endswith(".jsonl")]
                if len(matches) != 1:
                    raise ImportFormatError("Choose an archive containing one supported conversation/notes file, or select that file directly.")
                member = matches[0]
                parts = PurePosixPath(member.filename)
                windows_path = PureWindowsPath(member.filename)
                if (parts.is_absolute() or ".." in parts.parts or windows_path.drive or windows_path.root
                        or ".." in windows_path.parts or member.flag_bits & 1 or member.file_size > MAX_EXPANDED):
                    raise ImportFormatError("The selected ZIP member is encrypted, unsafe, or too large.")
                if contains_secret(member.filename):
                    raise ImportFormatError("An archive member contains credential-like metadata.")
                with archive.open(member) as stream:
                    data = stream.read(MAX_EXPANDED + 1)
                if len(data) > MAX_EXPANDED:
                    raise ImportFormatError("The expanded file exceeds the 25 MB limit.")
                source_member = member.filename
                filename = parts.name
        except (zipfile.BadZipFile, NotImplementedError, RuntimeError):
            raise ImportFormatError("This file is not a readable ZIP export.") from None
    try:
        raw = data.decode("utf-8-sig")
    except UnicodeError:
        raise ImportFormatError("Use a UTF-8 JSON, JSONL, Markdown or text export.") from None
    if provider == "codex" and filename.lower().endswith(".jsonl"):
        records = _codex_lines(raw, stats)
    elif provider == "notes" and filename.lower().endswith((".txt", ".md", ".markdown")):
        records = _plain(raw)
    else:
        try:
            parsed = json.loads(raw)
        except (ValueError, RecursionError):
            raise ImportFormatError("The selected file is not valid JSON for this source.") from None
        records = _chatgpt(parsed, stats) if provider == "chatgpt" else _claude(parsed, stats) if provider == "claude" else _codex_json(parsed, stats) if provider == "codex" else _notes(parsed, provider, stats)
    output, seen = [], set()
    for record in records:
        body = _visible(record["text"]) if isinstance(record.get("text"), str) else ""
        if not body:
            stats["non_visible"] += 1
            continue
        if contains_secret(body):
            stats["sensitive"] += 1
            continue
        stats["visible_records"] += 1
        if mode == "suggestions" and record["role"] not in ("user", "note"):
            stats["incidental"] += 1
            continue
        selected = [item["evidence"] for item in local_candidates(body)] if mode == "suggestions" and record["role"] != "note" else [body]
        if not selected:
            stats["incidental"] += 1
            continue
        for body in selected:
            for offset in range(0, len(body), 6000):
                chunk = body[offset:offset + 6000].strip()
                digest = fingerprint(chunk)
                if digest in seen:
                    stats["duplicates"] += 1
                    continue
                seen.add(digest)
                source_id = (str(record["source_id"]) + (f":part-{offset // 6000 + 1}" if len(body) > 6000 else ""))[:350]
                output.append({
                    "id": hashlib.sha256((provider + ":" + source_id + ":" + digest).encode()).hexdigest()[:32],
                    "title": scrub_secrets(str(record["title"]))[:160], "text": chunk,
                    "provider": provider, "role": record["role"], "source_id": source_id,
                    "original_scope": record.get("original_scope")[:180] if isinstance(record.get("original_scope"), str) else None,
                    "dedupe_key": digest, "authority": False,
                })
                if len(output) >= MAX_RECORDS:
                    stats["limit_reached"] = True
                    return {"records": output, "stats": stats, "member": source_member}
    return {"records": output, "stats": stats, "member": source_member}


class MemoryImports:
    def __init__(self, companion, *, clock=time.time):
        self.companion, self.clock = companion, clock
        self.lock = threading.RLock()
        self.previews = {}

    def preview(self, data, filename, provider, project, source_label, mode):
        if not isinstance(provider, str) or provider not in FORMATS:
            raise ImportFormatError("Choose a supported local export format.")
        owner = self.companion.owner()
        project = self.companion.project(project)
        source_label = text(source_label or FORMATS.get(provider, "Imported source"), "Source label", 120)
        if contains_secret(source_label):
            raise ImportFormatError("The source label contains a credential-like value.")
        parsed = parse_export(data, filename, provider, mode=mode)
        records = [{**record, "project": project} for record in parsed["records"]]
        identity = str(uuid.uuid4())
        with self.lock:
            self.previews = {key: value for key, value in self.previews.items() if value["expires"] > self.clock()}
            if sum(value["state"] != "committed" for value in self.previews.values()) >= 4:
                raise ImportFormatError("Finish or close an existing import review before opening another.")
            self.previews[identity] = {
                "owner": owner, "records": {record["id"]: dict(record) for record in records},
                "expires": self.clock() + 1800, "state": "review",
                "source_label": source_label, "provider": provider, "filename": filename,
                "file_hash": hashlib.sha256(data).hexdigest(), "project": project,
                "stats": dict(parsed["stats"]),
            }
        return {
            "preview_id": identity, "provider": provider, "source_label": source_label,
            "project": project, "filename": filename, "records": records,
            "stats": parsed["stats"], "archive_member": parsed["member"], "authority": False,
        }

    def cancel(self, identity):
        if not isinstance(identity, str) or len(identity) > 100:
            raise ImportFormatError("This import review identity is invalid.")
        with self.lock:
            item = self.previews.get(identity)
            if item and item["owner"] == self.companion.owner() and item["state"] == "review":
                self.previews.pop(identity, None)
        return {"cancelled": True}

    def commit(self, identity, selected_ids):
        if not isinstance(identity, str) or len(identity) > 100:
            raise ImportFormatError("This import review identity is invalid.")
        if not isinstance(selected_ids, list) or not 0 < len(selected_ids) <= MAX_RECORDS or any(not isinstance(value, str) for value in selected_ids):
            raise ImportFormatError("Select the items you reviewed before importing.")
        selection = tuple(sorted(set(selected_ids)))
        with self.lock:
            item = self.previews.get(identity)
            if not item or item["owner"] != self.companion.owner() or item["expires"] <= self.clock():
                raise ConflictError("This import review expired. Select the file again.")
            if item["state"] == "committed":
                if item["selection"] != selection:
                    raise ConflictError("This review already imported a different selection. Open a new review for more items.")
                return json.loads(json.dumps(item["result"]))
            if item["state"] != "review":
                raise ConflictError("This import is already being committed.")
            if any(value not in item["records"] for value in selected_ids):
                raise ImportFormatError("An imported item does not belong to this reviewed preview.")
            self.companion.project(item["project"])
            records = [item["records"][key] for key in dict.fromkeys(selected_ids)]
            item["selection"] = selection
            item["state"] = "committing"
        try:
            result = self.companion.memory.import_records(
                records, item["source_label"], batch_id=identity,
                batch_metadata={key: item[key] for key in ("provider", "filename", "file_hash", "project", "stats")},
                expected_owner=item["owner"],
            )
            with self.lock:
                item["state"] = "committed"
                item["result"] = json.loads(json.dumps({"batch_id": identity, **result}))
                item["records"] = {}
            return json.loads(json.dumps(item["result"]))
        except Exception:
            with self.lock:
                item["state"] = "review"
            raise
