"""Pure, offline parsing of *reported* progress, never execution authority.

The public checklist is an ordinary dict with exactly five list-valued keys.
Only explicit section labels or standalone checkboxes classify source text.
Unstructured prose yields empty lists; a cycle status never supplies task work.
The formatter then reports the observed cycle status without inventing a result.

Callers must send the returned notification as plain text (no Telegram parse
mode). No state, providers, files, or message transports are used here.
"""

from __future__ import annotations

import html
import re
import unicodedata


MAX_INPUT_CHARS = 65_536
MAX_LINE_CHARS = 4_096
MAX_ITEMS_PER_SECTION = 8
MAX_ITEM_CHARS = 280
MAX_NOTIFICATION_CHARS = 3_900

_SECTIONS = (
    ("done", "Done", "✓"),
    ("working", "Working on", "◷"),
    ("next", "Next/Planned", "☐"),
    ("blockers", "Blockers", "!"),
    ("suggestions", "Suggestions", "◇"),
)
_ALIASES = {
    "done": (
        "done", "complete", "completed", "finished", "completed work",
        "work completed", "accomplished", "accomplishments", "what's done",
    ),
    "working": (
        "working", "working on", "currently working on", "in progress",
        "ongoing", "current work", "active work",
    ),
    "next": (
        "next", "planned", "next/planned", "plan", "plans", "planned work",
        "next steps", "up next", "to do", "todo", "to-do", "pending",
    ),
    "blockers": (
        "blocker", "blockers", "blocked", "blocked on", "blocking issues",
        "impediments",
    ),
    "suggestions": (
        "suggestion", "suggestions", "recommendation", "recommendations",
        "idea", "ideas", "proposal", "proposals",
    ),
}


def _title(value):
    return re.sub(r"[\W_]+", " ", value.casefold()).strip()


_SECTION_NAMES = {_title(alias): key for key, aliases in _ALIASES.items() for alias in aliases}
_ATX = re.compile(r"^[ \t]{0,3}(#{1,6})[ \t]+(.+?)(?:[ \t]+#+)?[ \t]*$")
_SETEXT = re.compile(r"^[ \t]{0,3}(?:={3,}|-{3,})[ \t]*$")
_LIST = re.compile(r"^[ \t]*(?:[-*+•‣–—]|\d{1,4}[.)])[ \t]+")
_CHECKBOX = re.compile(
    r"^(?:\[(?P<box>[ xX✓✔])\]|(?P<symbol>[✓✔✅☑☐□]))"
    r"\ufe0f?(?:[ \t]+|$)(?P<body>.*)$"
)
_FENCE = re.compile(r"^[ \t]*(`{3,}|~{3,})(.*)$")
_PRIVATE_NAMES = {
    "think", "thinking", "analysis", "reasoning", "private reasoning",
    "internal reasoning", "internal analysis", "private analysis", "chain of thought",
    "scratchpad", "scratch pad", "thinking process", "internal monologue",
    "reasoning content", "reasoning summary", "reasoning trace", "private notes",
    "private", "internal", "hidden", "system", "developer", "instructions",
    "tool", "tools", "tool call", "tool calls", "tool result", "tool results",
    "tool output", "tool outputs", "raw tool content", "function call",
    "tool use", "tool response", "tool input", "tool invocation", "tool arguments",
    "function", "function result", "function response", "stdout", "stderr",
    "assistant", "environment context", "permissions",
    "memory", "script", "style", "details", "iframe", "object", "template",
}
_BRACKET_NAMES = (
    r"think|thinking|analysis|reasoning|scratchpad|private|internal|hidden|"
    r"chain[_ -]of[_ -]thought|tool(?:[_ -](?:call|result|output|use|response|input))?"
)
_MARKUP = re.compile(
    r"(?P<comment><!--.*?(?:-->|$)|<!\[CDATA\[.*?(?:\]\]>|$))"
    r"|<(?P<closing>/)?[ \t]*(?P<tag>[A-Za-z][\w:.-]*)"
    r"""(?P<attrs>(?:[^>"']|"[^"]*(?:"|$)|'[^']*(?:'|$))*)(?:>|$)"""
    r"|\[(?P<bclosing>/)?(?P<btag>" + _BRACKET_NAMES + r")\]",
    re.I | re.S,
)
_HIDDEN_ATTR = re.compile(
    r"\bhidden\b|\bdisplay\s*:\s*none\b|\bvisibility\s*:\s*hidden\b", re.I
)
_VOID_TAGS = {"br", "hr", "img", "input", "meta", "link", "wbr", "source"}
_INLINE_TAGS = {"a", "b", "strong", "i", "em", "span", "code", "u", "s", "mark", "sub", "sup"}
_PEM = re.compile(
    r"-----BEGIN (?:[A-Z0-9]+ )*PRIVATE KEY-----.*?"
    r"(?:-----END (?:[A-Z0-9]+ )*PRIVATE KEY-----|$)", re.I | re.S
)
_RAW_TRANSCRIPT = re.compile(
    r"<\|[^>\n]*\|>|"
    r"^[ \t]*(?:assistant[ \t]+(?:to=|channel=)|"
    r"(?:role|recipient|tool_call_id)[ \t]*:|"
    r"\{|"
    r"\[[ \t]*(?:\{|[\"']|$))",
    re.I,
)
_SECRETS = (
    re.compile(r"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b"),
    re.compile(r"\bsk-(?:proj-|ant-)?[A-Za-z0-9_-]{16,}\b"),
    re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9_]{16,}|github_pat_[A-Za-z0-9_]{16,})\b"),
    re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}\b"),
    re.compile(r"\b\d{6,14}:[A-Za-z0-9_-]{20,}\b"),
    re.compile(r"\bBearer[ \t]+\S+", re.I),
    re.compile(r"\beyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\b"),
    re.compile(
        r"""\b(?:[A-Za-z0-9]+_)*(?:password|passwd|pwd|passphrase|api[_ -]?key|secret|"""
        r"""secret[_ -]?key|client[_ -]?secret|token|access[_ -]?token|"""
        r"""refresh[_ -]?token|bot[_ -]?token|private[_ -]?key|"""
        r"""aws_secret_access_key|authorization|credentials?|"""
        r"""connection[_ -]?string|database[_ -]?url)"""
        r"""[\s"'`]*(?::|=|\bis\b)""", re.I
    ),
    re.compile(r"[A-Za-z][A-Za-z0-9+.-]*://[^/\s:@]+:[^/\s@]+@"),
    # Long opaque values are not useful checklist prose. Drop the whole item.
    re.compile(r"(?<![\w])[A-Za-z0-9_+/=-]{40,}(?![\w])"),
)
_MARKDOWN_LINK = re.compile(r"!?\[([^\[\]\n]*)\]\((?:\\.|[^)\\\n])*\)")
_REFERENCE_LINK = re.compile(r"!?\[([^\[\]\n]+)\]\[[^\[\]\n]*\]")
_REFERENCE_DEFINITION = re.compile(r"^[ \t]*\[[^\]\n]+\]:[ \t]*.*$", re.M)
_URL = re.compile(
    r"\b[A-Za-z][A-Za-z0-9+.-]{1,24}://[^\s<>]+|"
    r"\b(?:mailto|javascript|vbscript|data|file):[^\s<>]+|"
    r"\bwww\.[^\s<>]+|"
    r"\b[\w.+-]+@[\w.-]+\.[A-Za-z]{2,24}\b|"
    r"(?<![\w.-])(?:[A-Za-z0-9-]+\.)+"
    r"(?:com|org|net|io|dev|app|ai|co|edu|gov|info|biz|me|uk|us|de|fr|tn|cloud|test|invalid)"
    r"\b(?:/[^\s<>]*)?", re.I
)
_STATUS_LABELS = {
    **dict.fromkeys(("complete", "completed", "done", "finished", "task_complete",
                     "success", "succeeded"), "completed (cycle only; project completion unverified)"),
    **dict.fromkeys(("running", "working", "active", "in_progress", "in progress",
                     "executing"), "in progress"),
    **dict.fromkeys(("queued", "pending", "scheduled"), "queued"),
    **dict.fromkeys(("error", "failed", "failure"), "failed"),
    **dict.fromkeys(("aborted", "interrupted", "turn_aborted", "stopped"), "interrupted"),
    **dict.fromkeys(("cancelled", "canceled"), "cancelled"),
    **dict.fromkeys(("needs_input", "needs_attention", "waiting", "waiting_for_input",
                     "awaiting_input", "awaiting_approval"), "waiting for input"),
    "idle": "idle", "blocked": "blocked", "paused": "paused",
    "unknown": "unknown", "not checked": "not checked",
}


def _bounded_source(value):
    if type(value) is not str:
        return ""
    # Never expose a credential prefix cut in the middle of a long source line.
    if len(value) > MAX_INPUT_CHARS:
        value = value[:MAX_INPUT_CHARS]
        boundary = max(value.rfind("\n"), value.rfind("\r"))
        value = value[:boundary] if boundary >= 0 else ""
    value = unicodedata.normalize("NFKC", value)
    for _ in range(4):
        decoded = html.unescape(value)
        if decoded == value:
            break
        value = decoded
    else:
        if html.unescape(value) != value:
            return ""
    value = value.replace("\r\n", "\n").replace("\r", "\n")
    value = value.replace("\u2028", "\n").replace("\u2029", "\n")
    return "".join(
        char for char in value
        if char in "\n\t" or unicodedata.category(char) not in ("Cc", "Cf", "Cs")
    )


def _without_private_markup(value):
    value = _PEM.sub("\n", value)
    pieces, stack, position = [], [], 0
    for match in _MARKUP.finditer(value):
        if not stack:
            pieces.append(value[position:match.start()])
        name = (match["tag"] or match["btag"] or "").casefold()
        private = _title(name) in _PRIVATE_NAMES
        closing = match["closing"] or match["bclosing"]
        if name:
            if closing:
                if stack:
                    if name != stack[-1]:
                        return "".join(pieces)  # Malformed private blocks fail closed.
                    stack.pop()
                elif private:
                    pieces.clear()  # An orphan closing tag cannot validate its prefix.
            elif stack or private or _HIDDEN_ATTR.search(match["attrs"] or ""):
                if not (match["attrs"] or "").rstrip().endswith("/") and name not in _VOID_TAGS:
                    stack.append(name)
        if not stack:
            pieces.append("\n" if private or match["comment"] else "" if name in _INLINE_TAGS else " ")
        position = match.end()
    if not stack:
        pieces.append(value[position:])
    return "".join(pieces)


def _visible_lines(value):
    """Discard code, private blocks/sections, and raw protocol transcripts."""
    fence = None
    private_level = None
    for line in _without_private_markup(_bounded_source(value)).splitlines():
        marker = _FENCE.match(line)
        if fence:
            if marker and marker[1][0] == fence[0] and len(marker[1]) >= fence[1] and not marker[2].strip():
                fence = None
            continue
        if marker:
            fence = (marker[1][0], len(marker[1]))
            yield ""
            continue
        if _RAW_TRANSCRIPT.search(line):
            break
        heading = _ATX.match(line)
        name = _title((heading[2] if heading else line).split(":", 1)[0])
        if private_level is not None:
            final = name in ("final", "final answer", "final response")
            peer = heading and private_level > 0 and len(heading[1]) <= private_level
            if not (final or peer):
                continue
            private_level = None
        if name in _PRIVATE_NAMES:
            private_level = len(heading[1]) if heading else 0
            yield ""
            continue
        yield line if len(line) <= MAX_LINE_CHARS else ""


def _units(value):
    """Also bound UTF-16 units, so astral Unicode cannot exceed Telegram's cap."""
    return sum(2 if ord(char) > 0xFFFF else 1 for char in value)


def _clip(value, limit):
    if _units(value) <= limit:
        return value
    parts, size = [], 0
    for char in value:
        size += 2 if ord(char) > 0xFFFF else 1
        if size > limit - 1:
            break
        parts.append(char)
    return "".join(parts).rstrip() + "…"


def _detail(value, limit=MAX_ITEM_CHARS):
    visible = "\n".join(_visible_lines(value))
    visible = visible.replace("**", "").replace("__", "").replace("`", "")
    if any(pattern.search(visible) for pattern in _SECRETS):
        return ""
    visible = _REFERENCE_DEFINITION.sub("", visible)
    visible = _MARKDOWN_LINK.sub(r"\1", visible)
    visible = _REFERENCE_LINK.sub(r"\1", visible)
    visible = _URL.sub("", visible)
    visible = visible.replace("<", "").replace(">", "")
    visible = " ".join(visible.split()).strip()
    # Removing link/format wrappers can reveal a credential key or token.
    if any(pattern.search(visible) for pattern in _SECRETS):
        return ""
    return _clip(visible, limit) if any(char.isalnum() for char in visible) else ""


def _section_label(value):
    key = _SECTION_NAMES.get(_title(value))
    if key:
        return key, ""
    if ":" in value:
        label, remainder = value.split(":", 1)
        key = _SECTION_NAMES.get(_title(label))
        if key:
            return key, remainder.strip(" *_\t")
    return None, ""


def extract_project_checklist(text, status) -> dict:
    """Return done/working/next/blockers/suggestions, each <=8 strings of <=280
    UTF-16 units. Non-string input and unstructured prose give empty lists.

    Status is deliberately not evidence of task completion or active work.
    Explicit section labels take precedence over checked boxes; an unchecked
    item under Done goes to Next/Planned. Unsectioned [x]/[ ] items go to
    Done/Next respectively. No inference is made from an ordinary unlabelled list.
    """
    result = {key: [] for key, _, _ in _SECTIONS}
    section, pending_key, pending = None, None, []

    def flush():
        nonlocal pending_key, pending
        if pending_key and len(result[pending_key]) < MAX_ITEMS_PER_SECTION:
            item = _detail("\n".join(pending))
            if item and item not in result[pending_key]:
                result[pending_key].append(item)
        pending_key, pending = None, []

    lines = list(_visible_lines(text))
    index = 0
    while index < len(lines):
        line = lines[index]
        index += 1
        if not line.strip():
            flush()
            continue
        atx = _ATX.match(line)
        setext = index < len(lines) and _SETEXT.fullmatch(lines[index]) is not None
        if setext:
            index += 1
        bullet = _LIST.match(line)
        content = (atx[2] if atx else line[bullet.end():] if bullet else line).strip()
        checkbox = _CHECKBOX.match(content)
        checked = None
        if checkbox:
            checked = checkbox["box"] != " " if checkbox["box"] is not None else checkbox["symbol"] not in ("☐", "□")
            content = checkbox["body"].strip()
        key, remainder = _section_label(content)
        if key:
            flush()
            if not bullet or not remainder:
                section = key
            if remainder:
                inline_box = _CHECKBOX.match(remainder)
                if inline_box:
                    checked = (
                        inline_box["box"] != " " if inline_box["box"] is not None
                        else inline_box["symbol"] not in ("☐", "□")
                    )
                    remainder = inline_box["body"].strip()
                nested_key, nested_remainder = _section_label(remainder)
                if nested_key and nested_remainder:
                    key, remainder = nested_key, nested_remainder
                pending_key = "next" if key == "done" and checked is False else key
                pending = [remainder]
            continue
        if atx or setext:
            flush()
            section = None
            continue
        if bullet or checkbox:
            flush()
            key = section or ("done" if checked else "next" if checked is False else None)
            if key == "done" and checked is False:
                key = "next"
            if key:
                pending_key, pending = key, [content]
        elif pending and (section is not None or line[0].isspace()):
            pending.append(line.strip())
        else:
            flush()
            if section:
                pending_key, pending = section, [content]
    flush()
    return result


def sanitize_project_checklist(checklist) -> dict:
    result = {key: [] for key, _, _ in _SECTIONS}
    if type(checklist) is not dict:
        return result
    for key in result:
        values = checklist.get(key)
        if type(values) not in (list, tuple):
            continue
        for value in values[:MAX_ITEMS_PER_SECTION]:
            cleaned = _detail(value)
            if cleaned and cleaned not in result[key]:
                result[key].append(cleaned)
    return result


def format_project_notification(alias, label, status, checklist, *, allow_summary=True) -> str:
    """Return <=3900 characters AND UTF-16 units of plain Telegram text.

    Labels and checklist strings are scrubbed again; unknown statuses are never
    echoed. With allow_summary=False the checklist is not read, enumerated, or
    coerced at all. Only sanitized project identity and generic cycle status
    appear, followed by the user's five sections with details omitted.
    """
    safe_alias = _detail(alias, 48)
    safe_label = _detail(label, 80)
    identity = safe_label or safe_alias or "Project"
    if safe_alias and safe_label and safe_alias != safe_label:
        identity += f" ({safe_alias})"
    observed = _STATUS_LABELS.get(status.strip().casefold(), "unknown") if type(status) is str and len(status) <= 80 else "unknown"
    items = {key: [] for key, _, _ in _SECTIONS}
    if allow_summary is True and type(checklist) is dict:
        for key, _, _ in _SECTIONS:
            values = checklist.get(key)
            if type(values) not in (list, tuple):
                continue
            for value in values[:MAX_ITEMS_PER_SECTION]:
                item = _detail(value)
                if item and item not in items[key]:
                    items[key].append(item)
    if allow_summary is not True:
        source, empty = "Source: observed cycle status; project details omitted.", "Details omitted."
    elif any(items.values()):
        source, empty = "Source: reported task text (unverified).", "Not reported."
    else:
        source, empty = "Source: observed cycle status only.", "Not reported."
    prefix = f"{identity}\nObserved cycle status: {observed}\n{source}"
    # Reserve all five sections first; long Done lists cannot crowd out blockers.
    section_budget = (MAX_NOTIFICATION_CHARS - _units(prefix) - 2 * len(_SECTIONS)) // len(_SECTIONS)
    sections = []
    for key, title, symbol in _SECTIONS:
        values = items[key]
        if values:
            item_budget = (section_budget - _units(title) - len(values) * 3) // len(values)
            rows = [f"{symbol} {_clip(value, item_budget)}" for value in values]
        else:
            rows = [f"• {empty}"]
        sections.append(title + "\n" + "\n".join(rows))
    return prefix + "\n\n" + "\n\n".join(sections)
