"""Resolve conversational subjects without turning source text into authority."""
from __future__ import annotations

from pathlib import PureWindowsPath
import re
import time

from memory_policy import (
    _directive_text, _owner_spans, _project_name_matches, contains_secret, unquoted_text,
)

OWNER_ORIGINS = {"typed", "telegram_text", "manual_review", "speaker_matched"}
FOLLOWUP = re.compile(
    r"\b(?:it|its|that|this|those|these|same|continue|resume|again|also|next)\b", re.I
)
PREFIX = (
    r"^(?:(?:then|so|also|yes|okay|ok|now)[,!:]?\s+)*"
    r"(?:(?:hey|hi)[, ]+)?(?:(?:vega|please)[, ]+)*"
    r"(?:(?:i (?:want|need)|i['’]d like)\s+you\s+to\s+)?"
    r"(?:(?:can|could|would|will)\s+you\s+)?(?:please\s+)?"
)
READ = re.compile(PREFIX + r"(?:review|inspect|investigate|analy[sz]e|examine|check|find|locate|read|search|summari[sz]e|compare)\b", re.I)
CHANGE = re.compile(PREFIX + r"(?:fix|edit|update|change|remove|replace|add|implement|improve|refactor|repair|adjust)\b", re.I)
FILE_WORDS = re.compile(r"\b(?:files?|folders?|directories|directory|code|source|implementation|function|module|readme|script)\b", re.I)
PATH = re.compile(
    r"(?<!\w)(?:[A-Za-z]:[\\/][^\r\n\"<>|?*]+?"
    r"(?=(?:[.!?]?\s+(?:and|then|please|to|for|with)\b)|[\"\r\n]|$)"
    r"|(?:[\w.-]+[\\/])*[\w.-]+\.[A-Za-z0-9]{1,12}(?=$|[\s,;!?\]\)\"'`]|[.](?:\s|$)))",
    re.I,
)


def owner_opening(text):
    """Only a present, unquoted owner-authored opening can request work."""
    if not isinstance(text, str):
        return ""
    spans = [text[left:right].strip() for left, right in _owner_spans(text)
             if text[left:right].strip()]
    return _directive_text(spans[0]) if spans else ""


def requested_path(text):
    """Bind file data to the current owner clause, not appended source quotes."""
    if not isinstance(text,str):
        return None
    spans=[(left,right) for left,right in _owner_spans(text) if text[left:right].strip()]
    if not spans:
        return None
    left,right=spans[0]
    clause=text[left:right]
    urls=[match.span() for match in re.finditer(r"\b[A-Za-z][A-Za-z0-9+.-]*://\S+",clause)]
    source_intro=re.search(
        r"(?:[.!?]\s+|\n\s*\n)|\b(?:background|example|quoted text|transcript|history)\s*:",
        clause,re.I)
    limit=source_intro.start() if source_intro else len(clause)
    for match in PATH.finditer(clause[:limit]):
        if not any(start<=match.start()<end for start,end in urls):
            return match[0].strip().rstrip(".!?")
    # A directly requested quoted filename is an argument, e.g. Read "a b.md".
    # The quote must immediately follow the first current directive. Do not
    # search later quotes or a reported/background paragraph for another target.
    opening=_directive_text(clause.strip())
    action=READ.match(opening) or CHANGE.match(opening) or re.match(PREFIX+r"(?:open|look\s+at)\b",opening,re.I)
    if (not action or source_intro or len(opening)>160 or re.search(
        r"\b(?:says?|said|mentions?|quoted?|background|transcript|imported|example|history|report)\b",
        opening[action.end():],re.I)):
        return None
    literal=re.match(r'(["`\'“])([^"\r\n`\'“”]+)(["`\'”])',text[right:])
    if not literal or {"\"":"\"","`":"`","'":"'","“":"”"}[literal[1]]!=literal[3]:
        return None
    value=literal[2].strip()
    if re.match(r"^[A-Za-z][A-Za-z0-9+.-]*://",value):
        return None
    if re.fullmatch(r"(?:[A-Za-z]:[\\/])?[^:<>|?*\r\n]+\.[A-Za-z0-9]{1,8}",value):
        return value
    return None


def infer_context(text, projects, *, owner, history=(), reply_project=None,
                  project_roots=None, now=None):
    clean = unquoted_text(text)
    subject=re.sub(r"^\s*(?:(?:hey|hi)\s+)?vega[,!:]\s*", "", clean, flags=re.I)
    named = _project_name_matches(subject, projects)
    # Addressing the assistant is not a competing project selection.
    if len(named) > 1 and re.match(r"^\s*(?:hey\s+)?vega[,!:\s]", clean, re.I):
        named = [alias for alias in named if alias != "vega"]
    if named:
        return {"project": named[0] if len(named) == 1 else None,
                "projects": named, "source": "owner_message"}
    path = requested_path(text)
    if path and PureWindowsPath(path).is_absolute():
        candidates = []
        for alias, root in (project_roots or {}).items():
            root = root.get("root") if isinstance(root, dict) else root
            try:
                if alias in projects and PureWindowsPath(path).is_relative_to(PureWindowsPath(root)):
                    candidates.append((len(PureWindowsPath(root).parts), alias))
            except (TypeError, ValueError):
                continue
        if candidates:
            depth = max(item[0] for item in candidates)
            aliases = [alias for size, alias in candidates if size == depth]
            return {"project": aliases[0] if len(aliases) == 1 else None,
                    "projects": aliases, "source": "named_file"}
    if reply_project in projects:
        return {"project": reply_project, "projects": [reply_project], "source": "notification_reply"}
    if FOLLOWUP.search(clean):
        now = time.time() if now is None else now
        for row in reversed(list(history)[-12:]):
            if (row.get("owner_id") != owner or row.get("role") != "user"
                    or row.get("origin") not in OWNER_ORIGINS
                    or row.get("source") not in {"local", "telegram", "telegram_voice"}
                    or not 0 <= now - row.get("created_at", 0) <= 1800):
                continue
            prior = _project_name_matches(unquoted_text(row.get("text", "")), projects)
            if prior:
                return {"project": prior[0] if len(prior) == 1 else None,
                        "projects": prior, "source": "recent_owner_turn"}
    return {"project": None, "projects": [], "source": "automatic"}


def work_request(text, projects, resolution, permissions, *, files_available=False,
                 writable_projects=()):
    """Route supported fresh owner work into Vega, preserving explicit sends."""
    clean = owner_opening(text)
    if not clean or contains_secret(clean):
        return None
    if re.search(r"\b(?:don't|do not|never|not yet|hypothetically|suppose)\b", clean, re.I):
        return None
    if permissions.get("routing_requested") and not permissions.get("self_development_target"):
        return None
    # Approval/account/budget changes remain with their existing review workflow.
    if re.search(r"\b(?:approve|authori[sz]e|bypass|spending|budgets?|payment|credentials?|permissions?)\b", clean, re.I):
        if not READ.match(clean):
            return None
    read, change = READ.match(clean), CHANGE.match(clean)
    automation = bool(re.match(PREFIX+r"(?:build|prepare|design|draft|create)\b",clean,re.I)
                      and re.search(r"\b(?:n8n|automation|workflow)\b",clean,re.I))
    if not read and not change and not automation:
        return None
    project = permissions.get("self_development_target") or resolution.get("project")
    if not project:
        targets = permissions.get("project_targets") or []
        if len(targets) == 1:
            project = targets[0]
    if len(resolution.get("projects", [])) > 1:
        return None
    path = requested_path(text)
    local_files=not project
    if not project and files_available and (path or FILE_WORDS.search(clean)):
        project = "vega" if "vega" in projects else None
    if project not in projects:
        return None
    # The first span admits the operation. Other authored wording supplies
    # details (including text after a quoted filename), never new permissions.
    goal = unquoted_text(text).strip()
    if not 1 <= len(goal) <= 2000:
        return None
    if automation:
        # Artifact-only delegation: a present owner request names the project.
        # It cannot publish or activate anything; those are separate UI actions.
        if project not in permissions.get("explicit_project_targets", []):
            return None
        return {"operation":"create","project":project,"mode":"automation","goal":goal}
    if change:
        if ("project_instruction" not in permissions.get("intents", [])
                or project not in permissions.get("project_targets", [])
                or project not in writable_projects):
            return None
        return {"operation": "create", "project": project, "mode": "change",
                "goal": goal, **({"path": path} if path else {})}
    inspect = bool(path or FILE_WORDS.search(clean)
                   or re.match(PREFIX + r"(?:find|locate|search|inspect|investigate)\b", clean, re.I))
    if inspect and not files_available:
        return None
    return {"operation": "create", "project": project, "mode": "inspect" if inspect else "report",
            "goal": goal, **({"file_scope":"local"} if local_files and inspect else {}),
            **({"path": path} if path else {})}


def activity_question(text):
    clean = owner_opening(text)
    return bool(re.search(
        r"^(?:(?:please|vega)[, ]+)*(?:show|what (?:are|is)|how (?:are|is))\b.*"
        r"\b(?:agents?|subagents?|current work|active tasks)\b", clean, re.I
    ))
