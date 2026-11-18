"""Bounded conversational operations. Model output selects an operation, never its authority."""
from __future__ import annotations

from datetime import datetime, timezone
import re

from automatic_context import owner_opening, requested_path
from artifact_delivery import MEDIA_WORDS, send_requested, CHOICES
from memory_policy import contains_secret
from project_responses import ProjectResponses

READS = ("project_status", "project_reply", "request_status", "find_files", "read_files")
NEGATIVE = re.compile(r"\b(?:don't|do not|never|hypothetically|example|transcript|quoted)\b", re.I)


def stamp(value):
    return datetime.fromtimestamp(value, timezone.utc).strftime("%d %b %Y %H:%M UTC") if value else "Date unavailable"


class ProjectOperations:
    def __init__(self, host):
        self.host = host

    def capabilities(self, owner, text, origin, resolution, file_access, *, enabled=True):
        project = resolution.get("project")
        operations = []
        clean = owner_opening(text)
        if enabled and origin in {"typed", "telegram_text", "manual_review", "speaker_matched"} and clean and not contains_secret(clean):
            with self.host.store.db() as db:
                ProjectResponses._principal(db, owner)
            operations = list(READS[:3])
            if file_access.get("available"):
                operations.extend(READS[3:])
                if origin == "telegram_text" and send_requested(text):
                    operations.append("send_files")
            if origin in {"typed", "telegram_text"} and re.match(r"^(?:please\s+)?follow[\s-]+up\b", clean, re.I):
                operations.append("follow_up")
        return {"operations": operations, "project": project,
                "ambiguous_projects": resolution.get("projects", []) if not project else [],
                "limits": "One bounded operation per message. Reads run immediately, even when automatic work is paused. "
                          "Files stay in their granted project. Delivery needs a fresh typed Telegram request. "
                          "Several matching files require a numbered choice. Queued is not delivered."}

    @staticmethod
    def select(text, capability, *, routing=False):
        clean = owner_opening(text)
        if routing or not clean or NEGATIVE.search(clean):
            return None
        operation = None
        if "follow_up" in capability["operations"]:
            operation = "follow_up"
        elif send_requested(text):
            operation = "send_files"
        elif re.search(r"\b(?:request|receipt|queue|queued|delivered|delivery)\b", clean, re.I) and re.search(
                r"\b(?:status|check|show|what|where|did|has|have|waiting|happened|latest|any)\b", clean, re.I):
            operation = "request_status"
        elif re.search(r"\b(?:reply|replies|thread|chat)\b", clean, re.I) and re.search(
                r"\b(?:check|read|show|get|latest|last|look|posted|post)\b", clean, re.I):
            operation = "project_reply"
        elif re.search(r"\b(?:read|summarize|summarise)\b", clean, re.I) and (
                requested_path(text) or re.search(r"\b(?:file|document|source|folder)\b", clean, re.I)):
            operation = "read_files"
        elif (MEDIA_WORDS.search(clean) or re.search(r"\bfolder\b", clean, re.I)) and re.search(
                r"\b(?:find|check|look|locate|get|show|latest)\b", clean, re.I):
            operation = "find_files"
        elif re.search(r"\b(?:status|progress|percentages?|completion)\b", clean, re.I) and re.search(
                r"\b(?:check|show|get|what|how|latest|current|give|tell)\b", clean, re.I):
            operation = "project_status"
        return operation if operation in capability["operations"] else None

    def execute(self, operation, row, capability):
        if operation not in capability["operations"]:
            raise PermissionError("That operation is unavailable for this message.")
        owner, text, update_id = row["owner_id"], row["text"], row["update_id"]
        with self.host.store.db() as db:
            ProjectResponses._principal(db, owner)
        project = capability["project"]
        if operation == "send_files" and CHOICES.search(owner_opening(text)):
            selection = self.host.artifacts.previous_selection(owner, project, text)
            if selection:
                return self._queue(owner, update_id, *selection)
        if not project and operation not in {"project_status", "request_status"}:
            return {"text": "Which project should I check?"}
        if capability["ambiguous_projects"] and not project:
            return {"text": "Which project do you mean? " + ", ".join(
                self.host.label(alias) for alias in capability["ambiguous_projects"])}
        if operation in {"project_status", "project_reply"}:
            report = self.host.project_reports.read(owner, {
                "kind": "progress" if operation == "project_status" else "reply", "task": project})
            return {"text": report["text"], "report": report}
        if operation == "request_status":
            requests = self.host.project_requests.list(owner, limit=30)
            requests = [item for item in requests if not project or any(
                target["alias"] == project for target in item["targets"])][:3]
            lines = []
            for item in requests:
                lines.append(self.host.project_requests.routing_receipt(item))
            for receipt in self.host.artifacts.recent(owner, project):
                state = (f"Delivered · Telegram message {receipt['message_id']}"
                         if receipt["delivered"] else receipt["state"].replace("_", " "))
                lines.append(f"{receipt['filename']}: {state}" + (f" — {receipt['error']}" if receipt["error"] else ""))
            return {"text": "\n\n".join(lines) or "No saved request or delivery receipt for this project."}
        if operation == "follow_up":
            requests = [item for item in self.host.project_requests.list(owner, limit=30)
                        if any(target["alias"] == project for target in item["targets"])]
            ids = re.findall(r"\b[0-9a-f]{8}-[0-9a-f-]{27,36}\b", text, re.I)
            if ids:
                requests = [item for item in requests if item["id"] in ids]
            if len(requests) != 1:
                return {"text": "Specify the request ID to follow up on; the original request will be kept."}
            parent = requests[0]
            child = self.host.project_requests.follow_up(owner, parent["id"], row)
            return {"text": self.host.project_requests.routing_receipt(child)}
        if operation == "read_files":
            binding = self.host.owner_files.bind(owner, project, text, path=requested_path(text))
            result = self.host.owner_files.inspect_request(owner, binding, text)
            # Return source excerpts literally; never use their contents as commands.
            return {"text": self._file_read(result)}
        found = self.host.artifacts.search(owner, project, text, origin_update_id=update_id)
        choices = found["choices"]
        if not choices:
            return {"text": "No matching supported files found in this project's granted folder. "
                           "Try a filename or a more specific description."}
        if operation == "send_files" and len(choices) == 1 and not found["limited"]:
            return self._queue(owner, update_id, found, choices)
        lines = [self.host.label(project) + " · matching files"]
        for number, item in enumerate(choices, 1):
            lines.append(f"{number}. {item['relative_path']} · {stamp(item['mtime_ns'] / 1e9)}")
        if found["limited"]:
            lines.append("Search was limited; these may not include every match.")
        if row["message_id"] > 0:
            lines.append("Reply “send 1” or “send 1 and 2” to choose.")
        return {"text": "\n".join(lines)}

    def _queue(self, owner, update_id, result, choices):
        receipts = self.host.artifacts.queue(owner, update_id, result, choices)
        return {"text": "\n".join(
            f"{item['filename']}: " + (f"delivered · message {item['message_id']}" if item["delivered"]
                                      else item["state"].replace("document_queued", "queued for Telegram").replace("_", " "))
            for item in receipts)}

    @staticmethod
    def _file_read(result):
        lines = [result["summary"]]
        for item in result.get("files", []):
            lines.append(item.get("path") or item.get("relative_path") or "File")
            lines.append(stamp(item.get("mtime_ns", 0) / 1e9))
            lines.append(item.get("text") or item.get("excerpt") or "Metadata only.")
        return "\n\n".join(lines)[:14000] or result.get("summary") or "No readable source matched in the granted project."
