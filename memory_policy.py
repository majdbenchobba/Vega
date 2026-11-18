"""Conservative memory selection and current-message action boundaries."""
from __future__ import annotations

import hashlib
import re
import unicodedata
from collections.abc import Mapping


SECRET_PATTERNS = (
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----", re.I),
    re.compile(r"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b"),
    re.compile(r"\bsk-(?:proj-)?[A-Za-z0-9_-]{18,}\b"),
    re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}|xox[baprs]-[A-Za-z0-9-]{10,})\b"),
    re.compile(r"\b\d{8,12}:[A-Za-z0-9_-]{30,}\b"),
    re.compile(r"\bBearer\s+[A-Za-z0-9._~+/=-]{12,}", re.I),
    re.compile(r"(?i)\b(?:password|passwd|api[_ -]?key|secret[_ -]?(?:key|token)?|access[_ -]?token|refresh[_ -]?token|slack[_ -]?token|aws_secret_access_key)\s*(?::|=|\bis\b)\s*\S+"),
    re.compile(r"https?://[^/\s:@]+:[^/\s@]+@", re.I),
)
AUTHORITY = re.compile(
    r"\b(?:approve|approval|authori[sz](?:e|ation)|permission|bypass|"
    r"spend|budget|payment|purchase|pay|charge|credentials?|password|"
    r"reboot|shutdown|deploy|publish|delete|unrestricted|full access)\b|"
    r"ignore\s+(?:all\s+)?(?:rules|instructions|polic)|"
    r"send\s+.{0,60}\b(?:email|message|money)\b", re.I,
)
SENSITIVE = re.compile(
    r"\b(?:diagnos(?:is|ed)|medication|medical|passport|social security|"
    r"bank account|credit card|sexual|religion|religious|political affiliation)\b", re.I,
)
TRANSIENT = re.compile(
    r"\b(?:for this (?:answer|message|task|test)|this time|right now|"
    r"today only|tomorrow|tonight|this is (?:a |an )?(?:test|example)|"
    r"do not remember|don't remember|just testing|for example|as an example|"
    r"I'm tired|I am tired|I'm hungry|I am hungry)\b", re.I,
)
DURABLE = re.compile(
    r"\b(?:prefer|preference|my name|call me|I live|I am based|I'm based|"
    r"I work (?:as|in|at)|I use|we decided|we have decided|we use|"
    r"default|always use|keep (?:your |the )?(?:answers|replies|responses)|"
    r"respond in|reply in|remember that|remember this|favorite|favourite|my time zone|my timezone)\b|"
    r"\b(?:je préfère|je prefere|je m'appelle|j'habite|nous avons décidé|réponds en)\b|"
    r"(?:أفضل|اسمي|أعيش|تذكر أن|نفضل|رد بالعربية)", re.I,
)
OWNER_ANCHOR = re.compile(
    r"^(?:(?:please|vega|remember(?: that| this)?|actually|correction|"
    r"for future reference)[,:]?\s+)*"
    r"(?:I\b|I'm\b|I’m\b|my\b|we\b|our\b|call me\b|"
    r"keep (?:your |the )?(?:answers|replies|responses)\b|"
    r"respond in\b|reply in\b|je\b|j'|nous\b|"
    r"أفضل|اسمي|أعيش|نفضل|رد بالعربية)", re.I,
)
REPORTED_TEXT = re.compile(
    r"\b(?:my (?:friend|colleague|client|boss|partner|customer)|he|she|they)\s+"
    r"(?:said|says|prefers?|asked|asks|wants?|wrote|writes)\b|"
    r"\b(?:the document|the article|the email|the website)\s+(?:says|said|instructs)\b", re.I,
)
INSTRUCTION_TAG = re.compile(
    r"<(?P<closing>/)?[ \t]*(?P<name>"
    r"system(?:[_-]reminder)?|developer|instructions?|"
    r"tool(?:[_-](?:result|output|call))?|reasoning|analysis|thinking|"
    r"environment_context|permissions|codex_delegation|memory|"
    r"(?:reviewed|saved|imported)[_-](?:memory|context)|quote|quoted_text|blockquote"
    r")(?=[\s/>]|$)(?P<attributes>[^>]*)(?:>|$)", re.I,
)
FORWARDED_HEADER = re.compile(
    r"[ \t]*(?:from[ \t]*:|subject[ \t]*:|"
    r"(?:-{2,}[ \t]*)?(?:begin[ \t]+)?(?:forwarded|original)[ \t]+message\b|"
    r"on[ \t]+.{0,300}\bwrote[ \t]*:)", re.I,
)
REPORTED_INTRO = re.compile(
    r"\b(?:(?:my[ \t]+(?:friend|colleague|client|boss|partner|customer)|he|she|they|"
    r"the[ \t]+(?:(?:imported|saved|old)[ \t]+)?(?:document|article|email|website|note|message))"
    r"[ \t]+(?:said|says|asked|asks|wrote|writes|instructs)[ \t]*(?::|(?=\r?\n)|$)|"
    r"(?:imported|saved|reviewed|quoted)[ \t]+(?:memory|memories|context|note|notes|text|message)"
    r"[ \t]*:)", re.I,
)
FENCE_OPEN = re.compile(r"[ \t]*(`{3,}|~{3,})[^\r\n]*(?:\r?\n|$)")
CODE_RUN = re.compile(r"`+")
BLOCKQUOTE_OPEN = re.compile(r"[ \t]*>")
QUOTE_PAIRS = {'"': '"', "“": "”", "'": "'", "‘": "’", "«": "»"}
# Skip ordinary text in the regex engine rather than scanning large exports
# one character at a time. Delimited regions are consumed before their contents.
OWNER_BOUNDARY = re.compile(r"""(?m)^[ \t]*(?=\S)|[<`"“'‘«]|""" + REPORTED_INTRO.pattern, re.I)


def normalize(value):
    return " ".join(unicodedata.normalize("NFKC", value).casefold().split())


def fingerprint(value):
    return hashlib.sha256(normalize(value).encode("utf-8")).hexdigest()


def contains_secret(value):
    return any(pattern.search(value) for pattern in SECRET_PATTERNS)


def scrub_secrets(value):
    result = value
    for pattern in SECRET_PATTERNS:
        result = pattern.sub("[sensitive value omitted]", result)
    return result


def _instruction_end(value, opening):
    """Malformed or unfinished instruction blocks stay excluded through EOF."""
    if opening["closing"] or opening["attributes"].rstrip().endswith("/"):
        return opening.end()
    stack = [opening["name"].lower()]
    for tag in INSTRUCTION_TAG.finditer(value, opening.end()):
        if tag["closing"]:
            if tag["name"].lower() != stack[-1]:
                return len(value)
            stack.pop()
            if not stack:
                return tag.end()
        elif not tag["attributes"].rstrip().endswith("/"):
            stack.append(tag["name"].lower())
    return len(value)


def _fence_end(value, position):
    opening = FENCE_OPEN.match(value, position)
    if not opening:
        return None
    marker = opening[1]
    # A backtick run used twice on one line is an inline code span.
    if marker[0] == "`" and "`" in value[opening.start(1) + len(marker):opening.end()].rstrip("\r\n"):
        return None
    closing = re.compile(r"(?m)^[ \t]*" + re.escape(marker[0]) + "{" + str(len(marker)) + r",}[ \t]*\r?$")
    found = closing.search(value, opening.end())
    return found.end() if found else len(value)


def _quoted_end(value, position):
    opening = value[position]
    if opening == "`":
        run = CODE_RUN.match(value, position)
        for closing in CODE_RUN.finditer(value, run.end()):
            if len(closing[0]) == len(run[0]):
                return closing.end()
        return len(value)
    closing = QUOTE_PAIRS.get(opening)
    if closing is None:
        return None
    # Apostrophes in I'm/don't and inch marks such as 24" are ordinary text.
    if opening in ("'", '"') and position and (value[position - 1].isalnum() or value[position - 1] == "_"):
        return None
    end = position + 1
    while True:
        end = value.find(closing, end)
        if end < 0:
            return len(value)
        backslashes = 0
        before = end - 1
        while before >= 0 and value[before] == "\\":
            backslashes += 1
            before -= 1
        apostrophe = closing in ("'", "’") and end + 1 < len(value) and value[end - 1].isalnum() and value[end + 1].isalnum()
        if not backslashes % 2 and not apostrophe:
            return end + 1
        end += 1


def _blockquote_end(value, position):
    # Markdown permits an unmarked continuation of a quoted paragraph.
    end = value.find("\n", position)
    while end >= 0 and end + 1 < len(value):
        next_end = value.find("\n", end + 1)
        if not value[end + 1:next_end if next_end >= 0 else len(value)].strip():
            return end + 1
        end = next_end
    return len(value)


def _delimited_end(value, position, *, line_start=False):
    if position >= len(value):
        return len(value)
    if line_start:
        fence_end = _fence_end(value, position)
        if fence_end is not None:
            return fence_end
        if BLOCKQUOTE_OPEN.match(value, position):
            return _blockquote_end(value, position)
    tag = INSTRUCTION_TAG.match(value, position)
    if tag:
        return _instruction_end(value, tag)
    return _quoted_end(value, position)


def _owner_spans(value):
    """Keep separate verbatim spans; removing a quote must never join intent."""
    spans = []
    start = position = 0
    while position < len(value):
        boundary = OWNER_BOUNDARY.search(value, position)
        if boundary is None:
            break
        position = boundary.start()
        end = None
        line_start = position == 0 or value[position - 1] == "\n"
        if line_start and FORWARDED_HEADER.match(value, position):
            end = len(value)
        if end is None:
            end = _delimited_end(value, position, line_start=line_start)
        if end is None:
            report = REPORTED_INTRO.match(value, position)
            if report:
                body = report.end()
                while body < len(value) and value[body].isspace():
                    body += 1
                # A bounded quote has a clear end. A plain reported tail,
                # including additional paragraphs, has no reliable owner boundary.
                end = _delimited_end(value, body, line_start=True) or len(value)
        if end is not None:
            if start < position:
                spans.append((start, position))
            start = position = end
        else:
            position = max(position + 1, boundary.end())
    if start < len(value):
        spans.append((start, len(value)))
    return spans


def unquoted_text(value):
    """Quoted/imported instructions cannot masquerade as fresh owner intent."""
    return "\n".join(value[start:end] for start, end in _owner_spans(value))


def _owner_sentences(value):
    for start, end in _owner_spans(value):
        segment = value[start:end]
        if end < len(value) and not re.search(r"(?:[.!?]|\n)[ \t\r]*$", segment):
            # Do not learn a dangling "I prefer" from I prefer "quoted" text.
            boundaries = list(re.finditer(r"(?<=[.!?])\s+|\n+", segment))
            segment = segment[:boundaries[-1].end()] if boundaries else ""
        yield from (part.strip() for part in re.split(r"(?<=[.!?])\s+|\n+", segment) if part.strip())


def canonical_topic(evidence, proposed_topic=""):
    lowered = normalize(evidence)
    stem = re.sub(r"^(?:(?:please|vega|remember(?: that| this)?|actually|correction)[,:]?\s+)+", "", lowered)
    if re.search(r"\b(?:answers?|replies|responses?)\b", lowered):
        if re.search(r"\b(?:short|concise|brief|succinct)\b", lowered):
            return "preference.response-length", "concise"
        if re.search(r"\b(?:detailed|thorough|long|comprehensive)\b", lowered):
            return "preference.response-length", "detailed"
    language = re.search(r"\b(?:respond|reply|answers?|responses?)\s+(?:to me\s+)?in\s+(english|french|arabic|italian|german|spanish)\b", lowered)
    if language:
        return "preference.response-language", language[1]
    name = re.match(r"^(?:my name is|call me)\s+(.+?)[.!]?$", stem)
    if name:
        return "fact.preferred-name", name[1].rstrip(".!")
    location = re.search(r"\b(?:i live in|i am based in|i'm based in|j'habite)\s+(.+?)[.!]?$", stem)
    if location:
        return "fact.location", location[1].rstrip(".!")
    work = re.search(r"\bi work (as|in|at)\s+(.+?)[.!]?$", stem)
    if work:
        return {"as": "fact.occupation", "in": "fact.work-field", "at": "fact.employer"}[work[1]], work[2].rstrip(".!")
    topic = re.sub(r"[^a-z0-9._-]+", "-", normalize(proposed_topic)).strip("-")[:80]
    return (topic or "detail." + fingerprint(evidence)[:20]), fingerprint(evidence)


def local_candidates(value, current_project=None):
    """Fallback selection; a normal conversation need not call a second model."""
    candidates = []
    for sentence in _owner_sentences(value):
        sentence = sentence.strip(" \t-")
        if not 8 <= len(sentence) <= 600 or not DURABLE.search(sentence):
            continue
        kind = "project_decision" if re.search(r"\b(?:we decided|we have decided|we use|default|always use)\b", sentence, re.I) and current_project else "preference"
        candidates.append({
            "kind": kind, "title": "Project decision" if kind == "project_decision" else "Owner preference",
            "evidence": sentence, "topic": "", "project": current_project if kind == "project_decision" else None,
        })
    return candidates[:3]


def select_candidate(candidate, owner_text, projects, current_project=None, *, imported=False):
    if not isinstance(candidate, dict):
        return None, "invalid"
    evidence = candidate.get("evidence")
    if not isinstance(evidence, str) or not 8 <= len(evidence.strip()) <= 1200:
        return None, "invalid"
    evidence = evidence.strip()
    kind = candidate.get("kind")
    if kind not in ("preference", "fact", "project_decision", "reference"):
        return None, "invalid"
    if contains_secret(evidence) or contains_secret(str(candidate.get("title", ""))):
        return None, "sensitive"
    if not imported:
        source_sentence = next((part for part in _owner_sentences(owner_text) if evidence in part), "")
        if not source_sentence:
            return None, "quoted_or_not_present"
        prefix = source_sentence.split(evidence, 1)[0].strip()
        if prefix and not re.fullmatch(r"(?:(?:please|vega|remember(?: that| this)?|actually|correction)[,:]?\s*)+", prefix, re.I):
            return None, "not_owner_statement"
        if AUTHORITY.search(source_sentence):
            return None, "authority"
        if SENSITIVE.search(source_sentence) or contains_secret(source_sentence):
            return None, "sensitive"
        if TRANSIENT.search(source_sentence):
            return None, "incidental"
        if REPORTED_TEXT.search(source_sentence):
            return None, "third_party"
        if not DURABLE.search(evidence) and not (kind in ("fact", "project_decision") and OWNER_ANCHOR.search(evidence)):
            return None, "incidental"
    project = candidate.get("project")
    if project is not None and (not isinstance(project, str) or project not in projects or project == "coordinator"):
        return None, "scope"
    if kind == "project_decision" and not project:
        return None, "scope"
    named_project = bool(project and re.search(r"(?<!\w)" + re.escape(project) + r"(?!\w)", unquoted_text(owner_text), re.I))
    if not imported and project and project != current_project and not named_project:
        return None, "scope"
    topic, value_key = canonical_topic(evidence, candidate.get("topic", "") if isinstance(candidate.get("topic"), str) else "")
    rule_topic = canonical_topic(evidence)[0] == topic and not topic.startswith("detail.")
    title = {
        "preference.response-length": "Preferred response length",
        "preference.response-language": "Preferred response language",
        "fact.preferred-name": "Preferred name",
        "fact.location": "Owner location",
        "fact.occupation": "Owner occupation",
        "fact.work-field": "Owner work field",
        "fact.employer": "Owner workplace",
    }.get(topic, evidence[:120])
    clear_owner = bool(OWNER_ANCHOR.search(evidence))
    clear_project = kind == "project_decision" and (project == current_project or named_project) and bool(re.search(r"\b(?:we decided|we have decided|we use|default)\b", evidence, re.I))
    conditional = bool(re.search(r"\b(?:if|when|sometimes|for this|maybe|perhaps)\b", evidence, re.I))
    automatic = not imported and bool(DURABLE.search(evidence)) and (clear_owner or clear_project) and not conditional and len(evidence) <= 600
    return {
        "kind": "reference" if imported else kind, "title": title, "body": evidence,
        "project": project, "topic": topic, "value_key": value_key,
        "automatic_eligible": automatic, "rule_topic": rule_topic, "authority": False,
    }, None


def _project_name_text(value):
    return " ".join(normalize(value).replace("·", " ").replace("-", " ").split())


def _project_names(projects):
    names_by_alias = {}
    for alias, info in projects.items():
        label = info.get("label", "") if isinstance(info, Mapping) else ""
        label = label if isinstance(label, str) else ""
        names = {alias.replace("-", " "), label}
        if label:
            names.add(re.sub(r"\s*(?:·|[-–—])?\s*main\s*$", "", label, flags=re.I).strip())
        if alias == "coordinator":
            names.update(("coordinating main", "main coordinator"))
        names_by_alias[alias] = {_project_name_text(name) for name in names} - {"", "main"}
    return names_by_alias


def _project_name_spans(value, projects):
    """Longest registered names win; a shared name cannot choose an owner."""
    value = _project_name_text(value)
    matches = {}
    for alias, names in _project_names(projects).items():
        for name in names:
            for match in re.finditer(r"(?<!\w)" + re.escape(name) + r"(?!\w)", value):
                matches.setdefault(match.span(), set()).add(alias)
    selected = []
    for start, end in sorted(matches, key=lambda item: (-(item[1] - item[0]), item[0])):
        if any(start < right and end > left for left, right, _ in selected):
            continue
        aliases = matches[(start, end)]
        selected.append((start, end, next(iter(aliases)) if len(aliases) == 1 else None))
    return sorted(selected)


def _project_name_matches(value, projects):
    """Resolve aliases/display names without a shorter label shadowing a longer one."""
    return list(dict.fromkeys(alias for _, _, alias in _project_name_spans(value, projects) if alias is not None))


def _routing_recipient_matches(value, projects):
    # This one observed typing error is accepted only as a routing recipient.
    # In particular, it is never normalized in budget/approval project evidence.
    typo = "coordinatocoordinatorr"
    names = _project_names(projects)
    if (
        "coordinator" in projects
        and _project_name_matches("coordinator", projects) == ["coordinator"]
        and not any(typo in values for values in names.values())
    ):
        value = re.sub(r"(?<!\w)" + typo + r"(?!\w)", "coordinator", value, flags=re.I)
    value = _project_name_text(value)
    spans = _project_name_spans(value, projects)
    if not spans or any(alias is None for _, _, alias in spans):
        return []
    remainder, position = [], 0
    for start, end, _ in spans:
        remainder.append(value[position:start])
        position = end
    remainder.append(value[position:])
    residue = " ".join(remainder)
    residue = re.sub(r"\b(?:and|the|project|thread|main|please)\b|[,&.!?]", " ", residue)
    if residue.strip():
        # Unknown names and alternatives ("Vega or Coordinator") do not fall
        # through to a previous recipient or the currently selected project.
        return []
    return list(dict.fromkeys(alias for _, _, alias in spans))


def _prompt_recipients(spans, remainder, verb, projects):
    """Return (targets, explicitly addressed); an unknown address stays unknown."""
    send_verbs = ("send", "submit", "put", "forward", "route", "give", "envoie", "envoyer", "أرسل", "ارسل", "ابعث", "ابعت")
    if verb not in send_verbs:
        if re.match(r"^(?:me|us)\b", remainder):
            return [], False
        recipient = re.split(r"\b(?:to|that|about|regarding)\b|[.:\n]", remainder, maxsplit=1)[0]
        targets = _routing_recipient_matches(recipient, projects)
        if targets:
            return targets, True
        leading = _project_name_spans(remainder, projects)
        if leading and leading[0][0] == 0 and leading[0][2] is not None:
            return [leading[0][2]], True
        return [], bool(recipient.strip())
    direct = re.split(r"\b(?:a|the|this|these|that|those)\s+(?:prompt|message|instruction)", remainder, maxsplit=1)[0]
    direct_targets = _routing_recipient_matches(direct, projects)
    if direct_targets:
        return direct_targets, True
    segments = [remainder, *[normalize(span) for span in spans[1:]]]
    for segment in segments:
        destination = re.search(r"\b(?:to|à|إلى|الى)\s+(.+)", segment)
        if not destination:
            destination = re.search(r"\bfor\s+(.+)", segment)
        if destination:
            recipient = re.split(
                r"\b(?:about|regarding|asking|saying|that|with|so|because|for|to|pour|explaining|expliquant)\b|"
                r"\band\s+(?:(?:please|also)\s+)*(?:explain|tell me|how|why)\b|"
                r"[,;]\s*(?:do|does|how|why|what|please|can|could|would)\b|[.:\n]",
                destination[1], maxsplit=1,
            )[0]
            return _routing_recipient_matches(recipient, projects), True
    recipient = re.split(r"\b(?:a|the|this|these|that|those)\s+(?:prompt|message|instruction)", remainder, maxsplit=1)[0]
    targets = _routing_recipient_matches(recipient, projects)
    return targets, bool(targets)


_ROUTING_REFERENCE = re.compile(
    r"^(?:(?:all(?: of)?\s+)?(?:it|that|this|those|these)(?:\s+all)?|"
    r"the\s+(?:(?:above|previous|last|same|expanded|full|complete|copyable)\s+)*"
    r"(?:prompts?|instructions?|messages?|requests?|text|plans?|proposals?|updates?))"
    r"(?:\s+(?:prompts?|instructions?|messages?|requests?|text|plans?|proposals?|updates?))?"
    r"(?=\s*(?:$|[.!?,;]|\b(?:to|for|now|again|so|and)\b))", re.I,
)


def _routing_reference(remainder):
    # An inline payload belongs to this turn and must never be overwritten by
    # a historical prompt merely because its opening says "send this prompt".
    return bool(_ROUTING_REFERENCE.match(remainder)) and not re.search(r":\s*\S|\n\s*\n\s*\S", remainder)


def routing_explanation_requested(value):
    """Only a current owner question can request an explanation alongside a send."""
    return any(re.search(
        r"\bplease\s+explain\b|"
        r"(?:^|[,;.!?]\s*|\band\s+)(?:(?:please|also)\s+)*"
        r"(?:explain\b|(?:can|could|would|will)\s+you\s+explain\b|"
        r"(?:how|why)\s+(?:it|this|that|you|does|do|would|will|is)\b|"
        r"do\s+you\s+control\b|does\s+\w+\s+control\b)", normalize(value[start:end]),
    ) for start, end in _owner_spans(value))


_DEVELOPMENT_VERBS = (
    r"fix|update|change|implement|develop|build|add|remove|improve|configure|enable|disable|extend|"
    r"corrig(?:e[sz]?|er)|ajout(?:e[sz]?|er)|modifi(?:e[sz]?|er)|"
    r"am[eé]lior(?:e[sz]?|er)|configur(?:e[sz]?|er)|d[eé]velopp(?:e[sz]?|er)|"
    r"impl[eé]ment(?:e[sz]?|er)|activ(?:e[sz]?|er)|d[eé]sactiv(?:e[sz]?|er)|"
    r"(?:mets|mettez|mettre) [aà] jour"
)
_SELF_COMPONENT = (
    r"(?:voice|speech|interface|ui|routing|permissions?|configuration|config|settings|"
    r"code(?:base)?|implementation|features?|capabilities|memory|calendar|notifications?|"
    r"voix|routage|param[eè]tres|fonctionnalit[eé]s|reconnaissance vocale|m[eé]moire)"
)


def _directive_text(span):
    """Remove presentation markup within one authored span, never across quotes."""
    span = re.sub(r"^#{1,6}[ \t]+", "", span)
    return re.sub(
        r"(?<!\w)(?P<marker>\*{1,3}|_{1,3})(?=\S)(?P<body>[^\r\n]+?)(?<=\S)(?P=marker)(?!\w)",
        r"\g<body>", span,
    )


def _development_chat_only(verb, action):
    """An artifact or personal-data request does not authorize its described work."""
    if re.fullmatch(_DEVELOPMENT_VERBS, verb) and (
        not action.rstrip(".!?")
        or re.fullmatch(r"(?:the|an?|your|le|la|les|un|une|ton|ta|tes|votre|vos)[.!?]?", action)
    ):
        # A quoted feature cannot complete "add" or "fix your" via context.
        return True
    # Inspect the requested change, not negations or conditions in its purpose.
    clause = re.split(r"\b(?:so(?: that)?|in order to|pour que|afin (?:de|que))\b|[.!?;]", action, maxsplit=1)[0]
    if re.match(r"^(?:no|not|never|nothing|pas|jamais|rien|aucune?|whether|if|how)\b", clause):
        return True
    if re.search(
        r"\b(?:in theory|hypothetically|maybe|perhaps|someday|some day|one day|"
        r"only (?:if|when)|(?:hypothetical|hypoth[eé]tique) example|seulement si|"
        r"hypoth[eé]tiquement|peut[- ]être|un jour)\b", clause
    ):
        return True
    if re.search(
        r"\b(?:or (?:is|would|could|can|do) (?:that|it|you)|(?:only|just) asking|"
        r"(?:do not|don['’]t) (?:do (?:that|it)|implement|add|change)|no changes)\b", action
    ):
        return True
    if verb == "review" and re.search(r"\b(?:whether|idea|possibility|feasibility|capabilit(?:y|ies))\b", action):
        return True
    draft = re.match(
        r"^(?:(?:me|us)\s+)?(?:(?:an?|the|this|that|some|un|une|le|la|les|des|ce|cette)\s+)?"
        r"(?:(?:copyable|copy[- ]and[- ]paste|ready[- ]to[- ]copy|sample|detailed|short|concise|complete)\s+)*"
        r"(?:prompts?|messages?|instructions?|text|wording|plans?|proposals?|examples?|"
        r"descriptions?|summar(?:y|ies)|essays?|texte|plan)\b"
        r"(?!\s+(?:editor|builder|feature|system|tool|support)\b)", action
    )
    if draft and (verb != "give" or re.search(
        r"\b(?:i can (?:copy|paste)|for me to (?:copy|paste)|copyable|[aà] copier|copiable)\b", action
    )):
        return True
    personal_destination = re.search(
        r"\b(?:to|into|in|on|from)\s+(?:my|our|the)\s+(?:personal\s+)?"
        r"(?:calendar|memory|memories|notes?|reminders?|contacts|preferences)\b|"
        r"\b(?:[aà]|dans|sur|de)\s+(?:mon|ma|mes|notre|nos)\s+"
        r"(?:calendrier|agenda|m[eé]moire|notes?|rappels?|pr[eé]f[eé]rences)\b", clause
    )
    personal_object = re.match(
        r"^(?:my|our|mon|ma|mes|notre|nos)\s+(?:(?:saved|personal|notification)\s+)?"
        r"(?:calendar|memory|memories|notes?|preferences?|profile|time\s?zone|birthday|"
        r"calendrier|agenda|m[eé]moire|pr[eé]f[eé]rences)\b|"
        r"^(?:(?:an?|the|un|une|le|la|les|des)\s+)?"
        r"(?:reminders?|appointments?|events?|notes?|rappels?|rendez-vous|[eé]v[eé]nements?|souvenirs?)\b"
        r"(?!\s+(?:feature|system|integration|support|handler|editor|fonctionnalit[eé])\b)", clause
    )
    personal_list = re.search(r"\b(?:check ?list|list)\b.*\b(?:groceries|shopping|courses)\b", clause)
    return bool(personal_destination or personal_object or personal_list)


def _vega_development_feature(action, named_projects, *, personal_context):
    # Require a concrete feature as the object of the directive. Merely mentioning
    # voice recognition while requesting a different action is not sufficient.
    action = re.sub(
        r"^(?:(?:implementing|developing|building|adding|fixing|updating|configuring|and|even)\s+)+", "", action
    )
    self_component = bool(re.match(
        r"^(?:your|ton|ta|tes|votre|vos)\s+(?:(?:own|propre)\s+)?" + _SELF_COMPONENT + r"\b", action
    ))
    own_target = bool(re.search(
        r"\b(?:in|to|for|on|dans|[aà]|pour)\s+(?:yourself|toi-m[eê]me|your\s+" + _SELF_COMPONENT + r")\b", action
    ))
    self_ability = bool(re.match(
        r"^(?:it so you can|yourself able to|it possible for you to)\s+"
        r"(?:recogni[sz]e my voice|transcribe (?:my )?(?:voice|speech))\b", action
    ))
    routing_configuration = bool(re.match(
        r"^(?:(?:the|les?|la)\s+)?(?:permissions?|configuration|settings|routing|routage)\b", action
    ) and re.search(
        r"\b(?:so(?: that)? you can (?:queue|route|send|forward)\s+"
        r"(?:(?:this|these|the|a|project)\s+)*(?:requests?|prompts?|instructions?)|"
        r"pour que tu puisses (?:envoyer|router)\s+(?:(?:cette|ces|la|les|une?)\s+)*(?:demandes?|requ[eê]tes?|prompts?))\b",
        action,
    ))
    explicit_self = "vega" in named_projects or self_component or own_target or self_ability or routing_configuration
    if not (personal_context or explicit_self):
        return False
    if self_component or self_ability or routing_configuration:
        return True
    if "vega" in named_projects:
        named_object = re.match(
            r"^(?:(?:the|my|our|le|mon|notre)\s+)?vega(?:\s+(?:·\s*)?main)?(?:['’]s)?(?=\s|[.!?]|$)", action
        )
        if named_object:
            component = action[named_object.end():].strip()
            if not component.rstrip(".!?") or re.match(_SELF_COMPONENT + r"\b", component):
                return True
    subject = re.sub(r"^(?:(?:the|an?|your|le|la|les|un|une|des|ton|ta|tes|votre|vos)\s+)+", "", action)
    subject = re.sub(r"^(?:explicit|existing|current)\s+", "", subject)
    subject = re.sub(
        r"^(?:(?:support|feature|ability|capability)\s+(?:for|to)|"
        r"(?:fonctionnalit[eé]|capacit[eé])\s+(?:de|pour|[aà]))\s+", "", subject
    )
    intrinsic_feature = re.match(
        r"^(?:(?:owner(?:[- ]only)?[- ])?(?:voice|speaker|speech) recognition|speech[- ]to[- ]text|text[- ]to[- ]speech|"
        r"voice (?:input|commands?|authentication)|recogni[sz](?:e|ing) my voice|"
        r"routing (?:permissions?|rules?|checks?|logic)|(?:prompt|project|request|project[- ]request)[- ](?:routing|classification)|"
        r"reconnaissance vocale|reconna[iî]tre ma voix|permissions? de routage|"
        r"transcription vocale|synth[eè]se vocale|commandes vocales|(?:telegram )?mini[ -]?app|notifications?)\b", subject
    )
    software_feature = re.match(
        r"^(?:(?:new|calendar|voice|speech|routing|notification|memory|prompt|dark|light|nouvelle?)\s+)*"
        r"(?:features?|integrations?|plugins?|modes?|buttons?|editors?|builders?|tools?|settings|configuration|code|interface|"
        r"fonctionnalit[eé]s?|int[eé]grations?|param[eè]tres)\b", subject
    )
    return bool(intrinsic_feature or (explicit_self and software_feature))


def current_action_permissions(value, apps, projects, *, reply_project=None, current_project=None, history=None):
    # Preserve the first authored span as a unit. In particular, "Open" and
    # "notepad" on opposite sides of quoted content are not one fresh command.
    spans = [value[start:end].strip() for start, end in _owner_spans(value) if value[start:end].strip()]
    clean = spans[0] if spans else ""
    directive_text = _directive_text(clean)
    lower = normalize(directive_text)
    question = bool(re.match(r"^(?:what|why|how|when|where|do i|does|is it|can i|should i|did you|have you)\b", lower))
    directive = re.match(
        r"^(?:(?:then|so|also|yes|okay|ok|alright|now)[,!:]?\s+)*"
        r"(?:(?:hey|hi)[, ]+)?(?:(?:vega|please|s['’]il (?:te|vous) pla[iî]t)[, ]+)*"
        r"(?:(?:i (?:want|need|am asking)|i['’]d like|i['’]m asking)\s+you\s+to\s+|"
        r"je (?:veux|voudrais|souhaite) que (?:tu|vous)\s+)?"
        r"(?:(?:can|could|would|will) you\s+|(?:peux|pouvez|pourrais|pourriez)[-\s](?:tu|vous)\s+)?"
        r"(?:(?:please|s['’]il (?:te|vous) pla[iî]t)[, ]+)?"
        r"(?:(?:yes[, ]+)?go ahead(?: and)?\s+)?(?:help(?: me)?(?: to)?\s+)?"
        r"(?P<verb>" + _DEVELOPMENT_VERBS + r"|open|launch|start|run|reboot|restart|tell|ask|have|send|submit|put|forward|"
        r"review|continue|resume|keep|maintain|make|approve|confirm|route|give|"
        r"ouvre|ouvrir|lance|lancer|envoie|envoyer|افتح|شغل|أرسل|ارسل|ابعث|ابعت)\b", lower
    )
    named_projects = _project_name_matches(clean, projects)
    permissions = {
        "intents": ["chat"], "project_targets": named_projects, "app_targets": [],
        "explicit_project_targets": list(named_projects),
    }
    # An explicit intermediary is the recipient; the project in its payload is
    # not a second dispatch target. Only this current authored opening is used.
    intermediary = re.match(
        r"^(?:(?:please|also)\s+)?(?:u|you)\s+can\s+check\s+with\s+"
        r"(?P<recipient>coordinator|the coordinator)\s+(?:and\s+)?"
        r"(?:tell|ask)\s+(?:him|her|them)\s+to\s+\S", lower)
    if intermediary and "coordinator" in projects:
        permissions.update(intents=["chat", "project_instruction"], project_targets=["coordinator"],
                           explicit_project_targets=["coordinator"], routing_requested=True,
                           routing_reference_requested=True)
        return permissions
    if named_projects and re.match(r"^(?:when|once)\b", lower) and re.search(r"\bplease\s+(?:send|forward|tell|ask|review|continue)\b", lower):
        permissions["intents"].append("project_instruction")
        return permissions
    if not directive or question:
        return permissions
    verb = directive["verb"]
    remainder = lower[directive.end():].strip()
    send_verbs = ("tell", "ask", "have", "send", "submit", "put", "forward", "route", "give", "envoie", "envoyer", "أرسل", "ارسل", "ابعث", "ابعت")
    development_verb = bool(re.fullmatch(_DEVELOPMENT_VERBS, verb))
    work_request = development_verb or verb in ("review", "continue", "resume", "keep", "maintain", "make", "approve", "confirm", "start")
    opening = normalize(re.split(r"\r?\n[ \t]*\r?\n", directive_text, maxsplit=1)[0])
    opening_action = opening[directive.end():].strip()
    if (work_request or verb == "give") and _development_chat_only(verb, opening_action):
        return permissions
    route_requested = verb in send_verbs and not re.match(r"^(?:me|us)\b", remainder)
    if verb in send_verbs:
        recipients, addressed = _prompt_recipients(spans, remainder, verb, projects)
        permissions["project_targets"] = recipients
        permissions["explicit_project_targets"] = list(recipients)
        permissions["routing_reference_requested"] = _routing_reference(remainder)
        permissions["routing_explanation_requested"] = routing_explanation_requested(value)
        permissions["routing_target_unresolved"] = addressed and not recipients
        route_requested = route_requested and bool(recipients or addressed or permissions["routing_reference_requested"]
                                                   or re.search(r"\b(?:prompts?|instructions?|projects?|threads?|messages?|tasks?|updates?)\b", remainder))
        if verb == "put" and not addressed:
            route_requested = False
    # These are requests to the companion's own implementation/coordinator.
    # Only the owner-authored opening paragraph is considered, not pasted examples.
    opening_targets = _project_name_matches(opening, projects)
    self_scope = not opening_targets or set(opening_targets).issubset({"vega", "coordinator"})
    followup_request = work_request and self_scope and bool(re.search(
        r"\b(?:keep|maintain|continue|resume)\s+(?:the |my |all |these )?(?:tasks|threads|projects)\s+(?:running|working|moving|progressing)\b", opening))
    context_project = reply_project if reply_project in projects else current_project
    personal_context = context_project not in projects or context_project in ("vega", "coordinator")
    feature_request = "vega" in projects and self_scope and (
        (development_verb or verb in ("review", "keep", "make", "start"))
        and _vega_development_feature(opening_action, opening_targets, personal_context=personal_context)
        or followup_request and bool(re.search(r"\bcheck ?lists?\b", opening))
    )
    if feature_request or followup_request:
        targets = []
        if feature_request:
            targets.append("vega")
            # Explicit self-development scope, not spending/approval authority.
            # Literal project-name evidence remains in explicit_project_targets.
            permissions["self_development_target"] = "vega"
        if followup_request and "coordinator" in projects: targets.append("coordinator")
        permissions["project_targets"] = list(dict.fromkeys(targets))
        route_requested = bool(targets)
    if route_requested and re.search(r"\b(?:no prompts?|no messages?|nothing|none)\b", remainder):
        route_requested = False
        permissions["project_targets"] = []
        permissions["explicit_project_targets"] = []
        permissions.pop("self_development_target", None)
        return permissions
    if route_requested and not permissions["project_targets"] and not permissions.get("routing_target_unresolved") and reply_project not in projects and history:
        for item in reversed(history[-12:]):
            if not isinstance(item, dict) or item.get("role") != "user" or item.get("source") not in ("local", "telegram", "telegram_voice"):
                continue
            earlier = item.get("text")
            if not isinstance(earlier, str) or contains_secret(earlier):
                continue
            prior = current_action_permissions(earlier, apps, projects)
            if prior.get("routing_requested") and prior["project_targets"]:
                permissions["project_targets"] = prior["project_targets"]
                permissions["routing_origin_update_id"] = str(item.get("update_id", ""))
                permissions["routing_instruction"] = earlier
                break
    if verb in ("open", "launch", "start", "run", "ouvre", "ouvrir", "lance", "lancer", "افتح", "شغل"):
        for alias in apps:
            names = (alias, "chatgpt") if alias == "codex" else (alias,)
            if any(re.match(r"(?:(?:the|l'|le|application|app|برنامج)\s*)*" + re.escape(name) + r"(?!\w)", remainder, re.I) for name in names):
                permissions["app_targets"].append(alias)
        if permissions["app_targets"]:
            permissions["intents"].append("open_app")
    if verb in ("reboot", "restart") and (
        re.search(r"\b(?:laptop|computer|pc|windows|machine|system)\b", remainder)
        or not remainder.rstrip(".!") and clean == value.strip()
    ):
        permissions["intents"].append("request_reboot")
    if (verb in send_verbs and route_requested) or work_request:
        if verb == "start" and permissions["app_targets"] and not (feature_request or followup_request):
            return permissions
        if not permissions["project_targets"] and not permissions.get("routing_target_unresolved"):
            if reply_project in projects:
                permissions["project_targets"] = [reply_project]
            elif current_project in projects:
                permissions["project_targets"] = [current_project]
            elif re.search(r"\b(?:your|vega)\b", lower) and "vega" in projects:
                permissions["project_targets"] = ["vega"]
        if permissions["project_targets"]:
            permissions["intents"].append("project_instruction")
    if route_requested:
        permissions["routing_requested"] = True
    return permissions
