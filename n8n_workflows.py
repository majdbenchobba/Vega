"""Small, executable n8n templates for reading one registered Vega project."""
from __future__ import annotations

import hashlib
import json
import re


TEMPLATES = [
    {"id": "project_snapshot", "label": "Project snapshot",
     "description": "Read the project's current checklist, status and routing receipts."},
    {"id": "review_queue", "label": "Agent and review queue",
     "description": "Read the project's saved agent results and pending reviews."},
]
TRIGGERS = ("manual", "every_10_minutes")


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False, separators=(",", ":"))


def digest(value):
    return hashlib.sha256(canonical(value).encode("utf-8")).hexdigest()


def workflow_spec(value):
    if (not isinstance(value, dict) or set(value) != {"name", "template", "trigger"}
            or not isinstance(value["name"], str) or not 1 <= len(value["name"].strip()) <= 100
            or any(ord(char) < 32 for char in value["name"])
            or value["template"] not in [item["id"] for item in TEMPLATES]
            or value["trigger"] not in TRIGGERS):
        raise ValueError("Choose a supported workflow template, name and trigger.")
    return {**value, "name": value["name"].strip()}


def build_workflow(project, spec):
    spec = workflow_spec(spec)
    if not isinstance(project, str) or not re.fullmatch(r"[a-z0-9-]{1,80}", project):
        raise ValueError("This workflow needs a registered project alias.")
    scheduled = spec["trigger"] == "every_10_minutes"
    trigger = {"id": "vega-trigger", "name": "Every 10 minutes" if scheduled else "Run manually",
               "type": "n8n-nodes-base.scheduleTrigger" if scheduled else "n8n-nodes-base.manualTrigger",
               "typeVersion": 1.2 if scheduled else 1, "position": [240, 240],
               "parameters": {"rule": {"interval": [{"field": "minutes", "minutesInterval": 10}]}}
               if scheduled else {}}
    endpoint = "/api/project-control/projects/" if spec["template"] == "project_snapshot" else "/api/team/projects/"
    read = {"id": "vega-project-read", "name": "Read project",
            "type": "n8n-nodes-base.httpRequest", "typeVersion": 4.2, "position": [500, 240],
            "parameters": {"method": "GET", "url": "http://127.0.0.1:8769" + endpoint + project,
                           "options": {"timeout": 10000}}}
    return {"name": spec["name"], "nodes": [trigger, read],
            "connections": {trigger["name"]: {"main": [[{"node": read["name"], "type": "main", "index": 0}]]}},
            "settings": {"executionOrder": "v1"}}


def validate_workflow(project, spec, workflow):
    expected = build_workflow(project, spec)
    if workflow != expected:
        raise ValueError("The workflow changed outside its reviewed project template.")
    return {"state": "passed", "summary": "Validated the complete workflow and its one-project read scope.",
            "project": project, "nodes": len(expected["nodes"]), "method": "GET",
            "writes": "none", "external_actions": False, "model_calls": 0,
            "activation": "requires_separate_owner_action"}


def skill_catalog(project):
    return {"project": project, "templates": TEMPLATES, "triggers": list(TRIGGERS),
            "artifact_format": {"name": "A concise workflow name", "template": "project_snapshot", "trigger": "manual"},
            "rules": "Return the JSON artifact_format with one listed template and trigger. "
                     "Vega builds and validates the actual n8n graph. These templates only read the selected "
                     "project locally. Saving to n8n requires owner review and always saves an inactive workflow. "
                     "No messages, code execution, purchases, model calls or credential changes are included."}
