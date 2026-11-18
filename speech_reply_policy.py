"""Pure selection of speech alongside an independently preserved written reply.

The host must display/persist its original ``display_text``, not this function's
return value. Only the current owner message belongs in ``owner_text``. For a
request to read an earlier prompt, the host resolves that reference and passes
the earlier text as ``display_text`` in a separate speech selection call.

Model fields are hints, never read-aloud consent or evidence of execution.
Suggested ``reply_kind`` values are ``chat``, ``copyable_prompt``, ``code`` and
``instructions``; malformed/unknown labels cannot weaken content detection.
This module neither grants action permissions nor resolves conversational
references, records history, calls providers, or performs I/O.
"""
from __future__ import annotations

import re

from memory_policy import _owner_spans


MAX_SPOKEN_REPLY_CHARS = 320
MAX_SPOKEN_REPLY_WORDS = 60
MAX_PROMPT_INTRO_CHARS = 160
MAX_READ_ALOUD_CHARS = 12_000

__all__ = [
    "select_spoken_reply", "explicit_read_aloud", "MAX_SPOKEN_REPLY_CHARS",
    "MAX_SPOKEN_REPLY_WORDS", "MAX_PROMPT_INTRO_CHARS", "MAX_READ_ALOUD_CHARS",
]

_PROMPT_INTRO = "Here's the prompt. You can copy it from the screen."
_VEGA_PROMPT_INTRO = "Here's the prompt to send to Vega Main."
_SCREEN_INTROS = {
    "prompt": "The prompt is on screen.",
    "code": "The code is on screen.",
    "instructions": "The full instructions are on screen.",
    "reply": "The full reply is on screen.",
}
_READ_REMAINDER = "\nThe remaining text is on screen."
_READ_TOO_LONG = "That text is too long to read in one reply. The full text is on screen."

# Keep requests within authored spans. In particular, stripping a quote must
# not join "read" on one side to "this aloud" on the other.
_COURTESY = (
    r"(?:(?:hey|hi|vega|please|yes|okay|ok|alright|now|then|also|actually)"
    r"[\s,!:]+)*"
    r"(?:(?:i (?:want|need|would like)|i'd like)\s+you\s+to\s+)?"
    r"(?:(?:can|could|would|will)\s+you\s+)?(?:please\s+)?"
    r"(?:go ahead(?: and)?\s+)?"
)
_TARGET = (
    r"(?:it|this|that|everything|all of (?:it|this|that)|"
    r"what you (?:just )?(?:wrote|displayed|said)|"
    r"(?:(?:the|this|that|your|my)\s+)?"
    r"(?:(?:full|whole|entire|complete|previous|last|displayed|written|above|exact|on-screen)\s+)*"
    r"(?:prompt|reply|response|answer|text|message|code(?: block)?|instructions|thing)"
    r"(?:\s+(?:above|on (?:the )?screen))?)"
)
_AUDIBLE = r"(?:aloud|out loud|to me|back(?: to me)?|verbatim|word for word)"
_READ_REQUEST = re.compile(
    r"^" + _COURTESY
    + r"(?:read\s+(?:(?:" + _TARGET + r"\s+)?" + _AUDIBLE
    + r"(?:\s+" + _TARGET + r")?|me\s+" + _TARGET + r")|"
    + r"(?:say|speak|recite|voice|narrate)\s+" + _TARGET
    + r"(?:\s+" + _AUDIBLE + r")?)"
    r"(?:\s*,?\s*(?:please|now|for me|to me))*"
    r"(?:\s*,?\s+(?:(?:and|but)\s+(?:do not|don't)|without)"
    r"\s+(?:run(?:ning)?|execut(?:e|ing)|implement(?:ing)?|send(?:ing)?|"
    r"rout(?:e|ing)|open(?:ing)?|delet(?:e|ing)|chang(?:e|ing)|grant(?:ing)?)\b[^.!?]*)?"
    r"[.!?]*$", re.I,
)
_SPEECH_VETO = re.compile(
    r"\b(?:do not|don't|dont|never|must not|mustn't|should not|shouldn't|"
    r"will not|won't|cannot|can't)\s+(?:(?:ever|please|actually)\s+)*"
    r"(?:read|say|speak|recite|voice|narrate)\b|"
    r"\b(?:do not|don't|would not|wouldn't)\s+(?:want|need)\s+(?:you\s+)?to\s+"
    r"(?:read|say|speak|recite|voice|narrate)\b|"
    r"\bnot\s+(?:asking|telling)\s+(?:you\s+)?to\s+(?:read|say|speak|recite)\b|"
    r"\b(?:stop|avoid|skip)\s+(?:reading|speaking|saying|reciting|narrating)\b|"
    r"\b(?:no|without)\s+(?:speech|audio|voice|narration|reading aloud)\b|"
    r"\b(?:stay|keep)\s+(?:silent|quiet)\b|"
    r"\b(?:text only|on screen only|only on screen|silently)\b|"
    r"\b(?:only|just)\s+(?:the\s+)?(?:intro(?:duction)?|summary|gist|first|last|"
    r"opening|excerpt|one|a single|line|sentence|paragraph)\b|"
    r"\b(?:only|just)\s+(?:summari[sz]e|paraphrase|introduce)\b|"
    r"\b(?:not|except(?: for)?|exclude|excluding|skip|omit)\s+(?:the\s+)?"
    r"(?:full|whole|entire|prompt|text|code|instructions|body|first|last|section)\b|"
    r"\b(?:not now|not yet|never mind|nevermind|cancel that)\b|"
    r"\b(?:do not|don't)\s+do\s+(?:it|that|so)\b|"
    r"(?:^|[.!?]\s+)(?:actually[ ,]+)?(?:no|stop)[.!?]*\s*$|"
    r"\b(?:do not|don't|never)\s*$", re.I,
)
_DEFERRED_READ = re.compile(
    r"\b(?:if|when|whenever|unless|until|later|tomorrow|next time|always|from now on)\b", re.I,
)
_OWNER_PAYLOAD_HEADER = re.compile(
    r"\b(?:prompts?|instructions?|payload|example|text|message|code)\s*:|"
    r"\b(?:show|write|draft|compose|generate|create|display|print)\b[^.!?\n]{0,100}:",
    re.I,
)
_WRITE_PROMPT = re.compile(
    r"^" + _COURTESY
    + r"(?:write|show|draft|give|compose|generate|prepare|create|display|print|provide|make)\b"
    r"[^.!?\n]*\b(?:prompts?|copyable instructions|text to (?:send|paste))\b", re.I,
)

_FENCE = re.compile(r"`{3,}|~{3,}")
_WHOLE_INLINE_CODE = re.compile(r"^\s*`+[^\n]+`+\s*$")
_TAGS = re.compile(r"<[ /]*(?:system|developer|instructions?|prompt|code|speak)\b", re.I)
_PROMPT_MARKER = re.compile(
    r"\b(?:copyable|ready[ -]to[ -](?:copy|paste|send))\s+(?:prompt|text|instructions)\b|"
    r"\b(?:copy and paste|copy/paste|paste this|copy this)\b|"
    r"\b(?:here(?:'s|’s| is)|below is|use|send)\s+(?:(?:a|the|your|full|complete)\s+)*prompt\b|"
    r"\b(?:here(?:'s|’s| is)|this is)\s+(?:what|the text|the instructions)\s+to\s+(?:copy|paste|send)\b|"
    r"\b(?:the|your)\s+(?:(?:exact|full|complete|copyable)\s+)*prompt\s+(?:is\s+(?:below|here)|follows)\b|"
    r"\b(?:(?:full|complete|copyable|system|developer|user)\s+)?"
    r"prompt(?:\s+(?:for|to)\s+[^:\n]{1,80})?[ \t*_]*:|"
    r"(?m:^[ \t#*_>]*(?:(?:full|complete|copyable|system|developer|user)\s+)?"
    r"prompt(?:\s+(?:for|to)\s+[^:\n]{1,80})?[ \t*_]*[:\n])",
    re.I,
)
_INSTRUCTION_START = re.compile(
    r"""^[ \t\r\n#*_`>"'“‘«]*(?:you are (?:vega\b|(?:an?|the)\s+.{0,60}"""
    r"\b(?:assistant|agent|engineer|developer|reviewer|expert|designer|writer|bot)\b)|"
    r"act as\b|your (?:task|role|objective|mission) is\b|"
    r"i (?:want|need) you to\b|"
    r"(?:please\s+)?(?:implement|build|create|write|fix|update|add|remove|"
    r"delete|run|execute|send|review|audit|ensure|preserve|return|follow|read|"
    r"ignore|do not|don't)\b)", re.I,
)
_ROLE_PROMPT = re.compile(
    r"""(?m:^[ \t#*_`>"'“‘«]*(?:you are (?:vega\b|(?:an?|the)\s+.{0,60}"""
    r"\b(?:assistant|agent|engineer|developer|reviewer|expert|designer|writer|bot)\b)|"
    r"act as\b|your (?:task|role|objective|mission) is\b|"
    r"(?:task|instructions|requirements|constraints|deliverables|acceptance criteria)"
    r"[ \t*_]*[:\n]))", re.I,
)
_LIST_ITEM = re.compile(r"(?m)^[ \t]*(?:[-*+]|\d+[.)])[ \t]+\S")
_CONVERSATIONAL_START = re.compile(
    r"^(?:i(?:['’](?:m|ve|ll|d))?|we(?:['’](?:re|ve|ll|d))?|"
    r"it|that|this|the (?:request|task)|queued|routed|sent|done|implemented)\b", re.I,
)
_STATUS = re.compile(
    r"\b(?:queued?|queueing|pending|waiting|routed?|routing|sent|forwarded|delivered|"
    r"implemented|implementation|completed?|done|finished|fixed|applied|updated|"
    r"changed|successful(?:ly)?|executed|saved|requested|started|launched|built|"
    r"installed|opened|deleted|approved?|authori[sz](?:ed|ation)|permission|"
    r"granted|enabled|disabled|deployed|published|failed|failure|blocked|"
    r"cancelled|canceled|declined|accepted|handled|ready|working|running)\b|"
    r"\b(?:all set|took care of|taken care of|i did it)\b", re.I,
)
_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f\ud800-\udfff]")
_PRESENTATION_ONLY = re.compile(
    r"(?:(?:here(?:'s|’s| is)|here are)\s+(?:the|your|a)\s+"
    r"(?:(?:full|complete|copyable)\s+)?(?:prompt|code|instructions|text)"
    r"(?:\s+to send to Vega Main)?(?:\s+on (?:the |your )?screen)?|"
    r"the\s+(?:(?:full|complete|copyable)\s+)?(?:prompt|code|instructions|text)"
    r"\s+(?:is|are)\s+on (?:the |your )?screen)"
    r"[.!]?"
    r"(?:\s+you can (?:copy|read|review) (?:it|them|the prompt|the code|the text)"
    r"\s+(?:from|on) (?:the |your )?screen[.!]?)?", re.I,
)
_VEGA_DESTINATION = re.compile(r"\b(?:for|to)\s+Vega(?:\s*[·-]?\s+Main)?\b", re.I)


def _owner_fragments(owner_text):
    if not isinstance(owner_text, str):
        return []
    fragments = []
    for start, end in _owner_spans(owner_text):
        fragment = owner_text[start:end].replace("’", "'")
        # A plain, labelled payload has no reliable owner boundary after its
        # colon. A later separately owned span may still contain a request.
        header = _OWNER_PAYLOAD_HEADER.search(fragment)
        at_boundary = start == 0 or bool(re.match(r"^[ \t\r]*(?:\n|[.!?;])", fragment))
        fragments.append((fragment[:header.end() - 1] if header else fragment, at_boundary))
    return fragments


def _clauses(fragments):
    for fragment, at_boundary in fragments:
        # Newlines alone do not turn "If I ask,\nread it aloud" into consent.
        for index, clause in enumerate(re.split(r"(?<=[.!?;])\s+", fragment)):
            # A quote cannot finish an unfinished conditional or negation.
            # Later standalone owner sentences are still eligible.
            if index == 0 and not at_boundary:
                continue
            clause = clause.strip(" \t\r\n.!?;:")
            if clause:
                yield " ".join(clause.split())


def explicit_read_aloud(owner_text: str) -> bool:
    """Recognize a current, direct English request to speak the whole text.

    Read + an audible cue (e.g. "out loud" or "to me"), or an explicit
    speak/say/recite request is required. Quotes, imported instructions,
    hypotheticals, negation and requests for only an excerpt do not opt in.
    Bare "read/review the prompt" and "write/show the prompt" are not consent.
    This boolean has no connection to action/tool permissions.
    """
    fragments = _owner_fragments(owner_text)
    if any(_SPEECH_VETO.search(fragment) for fragment, _ in fragments):
        return False
    return any(
        not _DEFERRED_READ.search(clause)
        and bool(_READ_REQUEST.fullmatch(clause.split(":", 1)[0].rstrip()))
        for clause in _clauses(fragments)
    )


def _short(text, limit=MAX_SPOKEN_REPLY_CHARS):
    return (
        0 < len(text) <= limit and bool(text.strip())
        and len(text.split()) <= MAX_SPOKEN_REPLY_WORDS
        and not _CONTROL.search(text)
    )


def _prompt_requested(owner_text):
    return any(_WRITE_PROMPT.match(clause) for clause in _clauses(_owner_fragments(owner_text)))


def _payload_kind(text, owner_text="", reply_kind=None):
    hint = reply_kind.strip().casefold().replace("-", "_") if (
        isinstance(reply_kind, str) and len(reply_kind) <= 64
    ) else ""
    if hint in ("prompt", "copyable_prompt", "copyable", "instruction_payload"):
        return "prompt"
    if _PROMPT_MARKER.search(text) or _ROLE_PROMPT.search(text):
        return "prompt"
    if _prompt_requested(owner_text):
        conversational = _short(text) and (
            text.rstrip().endswith("?") or _CONVERSATIONAL_START.match(text.lstrip())
        )
        if _INSTRUCTION_START.match(text) or not conversational:
            return "prompt"
    if hint in ("code", "code_block") or _FENCE.search(text) or _WHOLE_INLINE_CODE.fullmatch(text):
        return "code"
    if hint in ("instructions", "long_instructions") or _TAGS.search(text):
        return "instructions"
    if not _short(text) and (_INSTRUCTION_START.match(text) or len(_LIST_ITEM.findall(text)) >= 3):
        return "instructions"
    return None


def _status_preamble(text, *, payload=False):
    """Keep an actual short status paragraph, including its qualifications."""
    paragraphs = re.split(r"\r?\n[ \t]*\r?\n", text, maxsplit=1)
    head = paragraphs[0]
    boundaries = [
        match.start() for pattern in (_FENCE, _PROMPT_MARKER, _ROLE_PROMPT, _TAGS)
        if (match := pattern.search(head))
    ]
    if boundaries:
        head = head[:min(boundaries)]
    if payload and not boundaries:
        tail = paragraphs[1] if len(paragraphs) > 1 else ""
        if not any(pattern.search(tail) for pattern in (_FENCE, _PROMPT_MARKER, _ROLE_PROMPT, _TAGS)):
            # A payload can itself contain "queued" or "implemented". Require
            # an identifiable body after the status, not only a model label.
            return ""
    head = head.strip()
    if (
        _short(head) and _STATUS.search(head) and not _INSTRUCTION_START.match(head)
        and not _WHOLE_INLINE_CODE.fullmatch(head) and not _LIST_ITEM.search(head)
        and _owner_spans(head) == [(0, len(head))]
        and not any(_INSTRUCTION_START.match(part) for part in re.split(r"(?<=[.!?])\s+", head))
    ):
        return head
    return ""


def _intro(kind, display_text, owner_text, *, has_status=False):
    if kind != "prompt" or has_status:
        return _SCREEN_INTROS[kind]
    # Only a fixed, explicitly named destination is reused, never arbitrary
    # model text extracted from an instruction body.
    opening = display_text.lstrip().split("\n", 1)[0]
    context = list(_clauses(_owner_fragments(owner_text)))
    if _PROMPT_MARKER.search(opening) and not _INSTRUCTION_START.match(opening):
        context.append(opening)
    if any(_VEGA_DESTINATION.search(part) for part in context):
        return _VEGA_PROMPT_INTRO
    return _PROMPT_INTRO


def _with_notice(prefix, notice):
    combined = prefix + " " + notice
    # Never clip a status sentence: doing so can remove "not implemented".
    return combined if _short(combined) else prefix


def _read_text(text):
    if len(text) <= MAX_READ_ALOUD_CHARS:
        return text
    prefix = text[:MAX_READ_ALOUD_CHARS - len(_READ_REMAINDER)]
    breaks = list(re.finditer(r"\s+", prefix))
    if not breaks:
        return _READ_TOO_LONG
    return prefix[:breaks[-1].start()].rstrip() + _READ_REMAINDER


def select_spoken_reply(
    display_text: str, *, owner_text: str = "", spoken_reply: str | None = None,
    reply_kind: str | None = None,
) -> str:
    """Return only speech; leave the caller's written text untouched.

    Routine output is at most 320 characters and 60 words. Short ordinary
    replies are preserved as-is. For longer prose, a valid short model summary
    is preferred. Prompt/code/instruction bodies get an introduction of at
    most 160 characters, optionally preceded by an exact bounded status.
    ``reply_kind`` may strengthen payload detection, never disable it.

    Explicit current read-aloud requests instead read the supplied source,
    ignoring model speech fields, up to 12,000 characters including an
    omission notice when necessary. The host must resolve the intended source.
    Non-string/empty display text produces silence; malformed optional fields
    are ignored. No returned text is an execution receipt.
    """
    if not isinstance(display_text, str) or not display_text.strip():
        return ""
    if explicit_read_aloud(owner_text):
        return _read_text(display_text)

    kind = _payload_kind(display_text, owner_text, reply_kind)
    candidate = spoken_reply.strip() if isinstance(spoken_reply, str) else ""
    status = _status_preamble(display_text, payload=bool(kind))
    if kind:
        intro = _intro(kind, display_text, owner_text, has_status=bool(status))
        if status:
            return _with_notice(status, intro)
        if _short(candidate, MAX_PROMPT_INTRO_CHARS) and _PRESENTATION_ONLY.fullmatch(candidate):
            # Do not introduce a destination absent from the written/owner text.
            if "vega main" not in candidate.casefold() or intro == _VEGA_PROMPT_INTRO:
                return candidate
        return intro

    if _short(display_text):
        return display_text
    if (
        _short(candidate) and not _payload_kind(candidate)
        and not _INSTRUCTION_START.match(candidate) and not _LIST_ITEM.search(candidate)
    ):
        # Status paraphrases can change "queued/not implemented" into "done".
        # Reuse an exact status paragraph, or leave those claims to the host.
        if candidate == status or not (status or _STATUS.search(candidate)):
            return candidate
    if status:
        return _with_notice(status, _SCREEN_INTROS["reply"])

    # A complete, non-instructional opening sentence is a useful prose fallback;
    # never chop arbitrary payload text to the routine character limit.
    opening = re.split(r"(?<=[.!?])\s+", display_text.strip(), maxsplit=1)[0]
    if (
        _short(opening, MAX_SPOKEN_REPLY_CHARS - len(_SCREEN_INTROS["reply"]) - 1)
        and opening.endswith((".", "!", "?"))
        and not _INSTRUCTION_START.match(opening) and not _STATUS.search(display_text)
        and not _payload_kind(opening)
    ):
        return _with_notice(opening, _SCREEN_INTROS["reply"])
    return _SCREEN_INTROS["reply"]
