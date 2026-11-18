"""Pure, Telegram-only formatting for the standard Bot API sendMessage method.

Input is raw text/Markdown, never a serialized model response or already-rendered
HTML. No provider, filesystem, transport, speech, or desktop renderer dependencies.
See docs/TELEGRAM_FORMATTING_20260927.md for the integration and fallback contract.
"""

from __future__ import annotations

from dataclasses import dataclass
from html import escape
from html.parser import HTMLParser
import re
import unicodedata
from urllib.parse import urlsplit


DEFAULT_LIMIT = 3900
TELEGRAM_LIMIT = 4096
MAX_LINK_UNITS = 2048  # Longer destinations stay complete, as visible literal text.
MAX_NESTING = 24
MATH_FALLBACK = "Formula (LaTeX source; equation layout is unavailable in this text message):"

__all__ = [
    "DEFAULT_LIMIT", "TELEGRAM_LIMIT", "format_telegram", "format_telegram_plain",
    "plain_fallback", "utf16_units",
]


@dataclass(frozen=True)
class _Tag:
    name: str
    attribute: str = ""

    def opening(self) -> str:
        if self.name == "a":
            return f'<a href="{escape(self.attribute, quote=True)}">'
        if self.name == "code" and self.attribute:
            return f'<code class="language-{escape(self.attribute, quote=True)}">'
        return f"<{self.name}>"

    def closing(self) -> str:
        return f"</{self.name}>"


@dataclass(frozen=True)
class _Span:
    text: str
    tags: tuple[_Tag, ...] = ()


@dataclass(frozen=True)
class _Marker:
    text: str
    can_open: bool
    can_close: bool


_STYLE_NAMES = frozenset(("b", "i", "s", "tg-spoiler"))
_LANGUAGE = re.compile(r"[A-Za-z][A-Za-z0-9_+#.-]{0,31}\Z")
_FENCE = re.compile(r" {0,3}(`{3,}|~{3,})([^\r\n]*)\Z")
_QUOTE_FENCE = re.compile(r" {0,3}(['‘’]{3})[ \t]*([A-Za-z][A-Za-z0-9_+#.-]{0,31})[ \t]*\Z")
_QUOTE_CLOSE = re.compile(r" {0,3}['‘’]{3}[ \t]*\Z")
_QUOTE_LANGUAGES = frozenset((
    "text", "plaintext", "plain", "prompt", "markdown", "md", "python", "py",
    "javascript", "js", "typescript", "ts", "json", "jsonc", "yaml", "yml",
    "html", "xml", "css", "sql", "bash", "sh", "shell", "powershell", "ps1",
    "c", "cpp", "c++", "csharp", "c#", "java", "rust", "go", "ruby", "php",
    "latex", "tex",
))
_LANGUAGE_ALIASES = {
    "js": "javascript", "ts": "typescript", "py": "python", "ps1": "powershell",
    "sh": "bash", "shell": "bash", "c++": "cpp", "c#": "csharp",
    "prompt": "text", "plain": "text", "plaintext": "text", "md": "markdown",
}
_ESCAPABLE = frozenset(r"\`*_{}[]()#+-.!>~|$")
_SPECIAL = re.compile(r"[\\`$[*_~|<]")
_URL_AUTOLINK = re.compile(r"<((?:https?://|mailto:)[^<>\r\n]+)>", re.IGNORECASE)
_WORD = re.compile(r"\w", re.UNICODE)
_BLOCK_QUOTE = re.compile(r"([ \t]{0,3})>(?:[ \t]|$)(.*)\Z")
_HEADING = re.compile(r"( {0,3})#{1,6}[ \t]+(.+)\Z")
_BULLET = re.compile(r"([ \t]*)([-+*])[ \t]+(.*)\Z")
_CHECKBOX = re.compile(r"\[([ xX])\][ \t]+")


def _require_text(text: str) -> str:
    if not isinstance(text, str):
        raise TypeError("Telegram text must be a string; extract the reply field before formatting.")
    try:
        text.encode("utf-8")
    except UnicodeEncodeError:
        raise ValueError("Telegram text contains an unpaired Unicode surrogate.") from None
    if "\0" in text:
        raise ValueError("Telegram text contains NUL; retain the original response for correction.")
    return text


def _limit(limit: int) -> int:
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= TELEGRAM_LIMIT:
        raise ValueError("The Telegram visible-text limit must be an integer from 1 to 4096.")
    return limit


def utf16_units(text: str) -> int:
    """Count visible text, not HTML source bytes or entity spellings."""
    return len(_require_text(text).encode("utf-16-le")) // 2


def _decorate(spans: list[_Span], tags: tuple[_Tag, ...]) -> list[_Span]:
    result = []
    for span in spans:
        # Telegram forbids styling inside/around code and pre. A code-containing
        # heading or emphasis keeps its code span, with styling on its other text.
        if any(tag.name in ("pre", "code") for tag in span.tags):
            result.append(span)
            continue
        combined = list(tags)
        for tag in span.tags:
            if tag not in combined:
                combined.append(tag)
        # Links and blockquotes must not contain one another. Keep the link.
        if any(tag.name == "a" for tag in combined):
            combined = [tag for tag in combined if tag.name != "blockquote"]
        result.append(_Span(span.text, tuple(combined)))
    return result


def _literal(text: str, language: str = "", *, block: bool = False) -> _Span:
    if not block:
        return _Span(text, (_Tag("code"),))
    language = _LANGUAGE_ALIASES.get(language.lower(), language.lower())
    tags = (_Tag("pre"), _Tag("code", language)) if language else (_Tag("pre"),)
    return _Span(text, tags)


def _safe_url(url: str) -> bool:
    if not url or utf16_units(url) > MAX_LINK_UNITS:
        return False
    if any(char.isspace() or ord(char) < 32 or ord(char) == 127
           or unicodedata.category(char) == "Cf" or char in '\\<>"`'
           for char in url):
        return False
    try:
        parsed = urlsplit(url)
        if parsed.scheme.lower() in ("http", "https"):
            return bool(parsed.hostname and not parsed.username and not parsed.password
                        and (parsed.port is None or 0 < parsed.port < 65536))
        if parsed.scheme.lower() == "mailto":
            return bool(not parsed.netloc and re.fullmatch(r"[^@/?#]+@[^@/?#]+", parsed.path)
                        and not parsed.query and not parsed.fragment)
    except ValueError:
        return False
    return False


def _line_body(line: str) -> tuple[str, str]:
    if line.endswith("\r\n"):
        return line[:-2], "\r\n"
    if line.endswith(("\r", "\n")):
        return line[:-1], line[-1:]
    return line, ""


def _parse_blocks(text: str) -> list[_Span]:
    import bisect
    lines = text.splitlines(keepends=True)
    offsets, offset = [], 0
    for line in lines:
        offsets.append(offset)
        offset += len(line)
    result: list[_Span] = []
    paragraph: list[str] = []
    absent_multiline_runs = set()

    def flush() -> None:
        if paragraph:
            result.extend(_inline("".join(paragraph)))
            paragraph.clear()

    index = 0
    while index < len(lines):
        body, ending = _line_body(lines[index])
        fence = _FENCE.fullmatch(body)
        quote_fence = _QUOTE_FENCE.fullmatch(body)
        if quote_fence and quote_fence[2].lower() not in _QUOTE_LANGUAGES:
            quote_fence = None
        if fence or quote_fence:
            marker = fence[1] if fence else quote_fence[1]
            info = fence[2].strip() if fence else quote_fence[2]
            # Backticks inside an info string are not a Markdown fence.
            if fence and marker[0] == "`" and "`" in info:
                paragraph.append(lines[index])
                index += 1
                continue
            closing = None
            for candidate in range(index + 1, len(lines)):
                candidate_body, _ = _line_body(lines[candidate])
                if quote_fence:
                    matched = _QUOTE_CLOSE.fullmatch(candidate_body)
                else:
                    matched = re.fullmatch(
                        r" {0,3}" + re.escape(marker[0]) + "{" + str(len(marker)) + r",}[ \t]*",
                        candidate_body,
                    )
                if matched:
                    closing = candidate
                    break
            # Repair apostrophes only with a labeled, closed, standalone wrapper.
            # Do not "repair" quotations, Python strings, inline mentions, or EOF.
            if quote_fence and (closing is None or closing == index + 1):
                paragraph.append(lines[index])
                index += 1
                continue
            flush()
            last = closing if closing is not None else len(lines)
            code = "".join(lines[index + 1:last])
            language = info if _LANGUAGE.fullmatch(info) else ""
            if code:
                result.append(_literal(code, language, block=True))
            index = last + 1 if closing is not None else last
            continue

        # Markdown code spans may cross lines. Their internal headings/lists
        # are literal data, so keep the entire span in the inline parser.
        multiline_code_end = None
        examined_until = -1
        for tick in re.finditer(r"`+", body):
            start = offsets[index] + tick.start()
            run = len(tick[0])
            if start < examined_until or run in absent_multiline_runs:
                continue
            end = _code_end(text, start, run)
            if end is None:
                absent_multiline_runs.add(run)
            else:
                examined_until = end + run
            if end is not None and end >= offsets[index] + len(lines[index]):
                multiline_code_end = bisect.bisect_right(offsets, end + run - 1) - 1
                break
        if multiline_code_end is not None:
            paragraph.extend(lines[index:multiline_code_end + 1])
            index = multiline_code_end + 1
            continue

        multiline_math_end = None
        for opener in re.finditer(r"\$\$|\\\[|\\\(", body if body.strip() not in ("$$", r"\[") else ""):
            math = _math_at(text, offsets[index] + opener.start())
            if math and math[1] > offsets[index] + len(lines[index]):
                multiline_math_end = bisect.bisect_right(offsets, math[1] - 1) - 1
                break
        if multiline_math_end is not None:
            paragraph.extend(lines[index:multiline_math_end + 1])
            index = multiline_math_end + 1
            continue

        # Protect complete display-math regions before looking for list/headings.
        if body.strip() in ("$$", r"\["):
            closer = "$$" if body.strip() == "$$" else r"\]"
            closing = next((j for j in range(index + 1, len(lines))
                            if _line_body(lines[j])[0].strip() == closer), None)
            if closing is not None:
                flush()
                expression = "".join(lines[index + 1:closing])
                result.extend(_math_spans(expression, display=True))
                index = closing + 1
                continue

        quote = _BLOCK_QUOTE.fullmatch(body)
        if quote:
            flush()
            quoted = []
            while index < len(lines):
                body, ending = _line_body(lines[index])
                match = _BLOCK_QUOTE.fullmatch(body)
                if not match:
                    break
                quoted.append(match[2] + ending)
                index += 1
            result.extend(_decorate(_inline("".join(quoted)), (_Tag("blockquote"),)))
            continue
        heading = _HEADING.fullmatch(body)
        if heading:
            flush()
            heading_text = re.sub(r"[ \t]+#+[ \t]*$", "", heading[2])
            result.append(_Span(heading[1]))
            result.extend(_decorate(_inline(heading_text), (_Tag("b"),)))
            result.append(_Span(ending))
            index += 1
            continue
        bullet = _BULLET.fullmatch(body)
        if bullet:
            flush()
            item = bullet[3]
            checkbox = _CHECKBOX.match(item)
            mark = "• "
            if checkbox:
                mark = "☑ " if checkbox[1].lower() == "x" else "☐ "
                item = item[checkbox.end():]
            result.append(_Span(bullet[1] + mark))
            result.extend(_inline(item + ending))
            index += 1
            continue
        paragraph.append(lines[index])
        index += 1
    flush()
    return [span for span in result if span.text]


def _code_end(text: str, start: int, run: int) -> int | None:
    cursor = start + run
    marker = "`" * run
    while True:
        cursor = text.find(marker, cursor)
        if cursor < 0:
            return None
        end = cursor + run
        if (cursor == 0 or text[cursor - 1] != "`") and (end == len(text) or text[end] != "`"):
            return cursor
        cursor = end


def _link_at(text: str, start: int) -> tuple[str, str, int] | None:
    depth = 1
    cursor = start + 1
    while cursor < len(text):
        char = text[cursor]
        if char == "\\":
            cursor += 2
            continue
        if char == "[":
            depth += 1
        if char == "]":
            depth -= 1
            if depth == 0:
                break
        cursor += 1
    if cursor + 1 >= len(text) or text[cursor + 1] != "(":
        return None
    label = text[start + 1:cursor]
    cursor += 2
    destination_start = cursor
    depth = 1
    while cursor < len(text):
        char = text[cursor]
        if char == "\\" and cursor + 1 < len(text) and text[cursor + 1] in "()\\":
            cursor += 2
            continue
        if char == "(":
            depth += 1
        if char == ")":
            depth -= 1
            if depth == 0:
                raw_url = text[destination_start:cursor]
                if raw_url.startswith("<") and raw_url.endswith(">"):
                    raw_url = raw_url[1:-1]
                url = re.sub(r"\\([()\\])", r"\1", raw_url)
                return label, url, cursor + 1
        cursor += 1
    return None


def _math_at(text: str, start: int) -> tuple[str, int, bool] | None:
    if text.startswith(r"\(", start):
        opener, closer, display = r"\(", r"\)", False
    elif text.startswith(r"\[", start):
        opener, closer, display = r"\[", r"\]", True
    elif text.startswith("$$", start):
        opener, closer, display = "$$", "$$", True
    elif text[start] == "$":
        opener, closer, display = "$", "$", False
        following = text[start + 1:start + 2]
        if not following or following.isspace() or (start and text[start - 1].isalnum()):
            return None
        if re.match(r"[A-Z]{2,10}@[A-Za-z0-9_]", text[start + 1:]):
            return None
    else:
        return None
    cursor = start + len(opener)
    while True:
        end = text.find(closer, cursor)
        if end < 0:
            return None
        # A delimiter preceded by an odd number of backslashes is escaped.
        backslashes = 0
        before = end - 1
        while before >= 0 and text[before] == "\\":
            backslashes += 1
            before -= 1
        if backslashes % 2:
            cursor = end + len(closer)
            continue
        expression = text[start + len(opener):end]
        if opener == r"\[" and re.fullmatch(r"[\w ]+", expression):
            return None  # An escaped Markdown [label] is not an equation.
        if opener == "$":
            if ("\n" in expression or "\r" in expression) and not re.search(r"[\\=+^_%{}]", expression):
                return None
            if not expression or expression[-1].isspace() or text[end + 1:end + 2].isdigit():
                return None  # A pair of currency amounts is not a formula.
            if text[end + 1:end + 2] == "$":
                return None
        return expression, end + len(closer), display


def _marker_tags(marker: str) -> tuple[_Tag, ...]:
    if marker in ("**", "__"):
        return (_Tag("b"),)
    if marker in ("***", "___"):
        return (_Tag("b"), _Tag("i"))
    return (_Tag({"*": "i", "_": "i", "~~": "s", "||": "tg-spoiler"}[marker]),)


def _resolve_markers(tokens: list[_Span | _Marker]) -> list[_Span]:
    stack: list[int] = []
    openings: dict[int, tuple[_Tag, ...]] = {}
    closings: dict[int, list[int]] = {}
    for index, token in enumerate(tokens):
        if not isinstance(token, _Marker):
            continue
        if token.can_close and stack:
            top = tokens[stack[-1]]
            if top.text == token.text:
                opened = stack.pop()
                openings[opened] = _marker_tags(token.text)
                closings[index] = [opened]
                continue
            # Common nested endings, **bold *italic*** and *italic **bold***.
            if len(token.text) == 3 and len(stack) >= 2:
                underneath = tokens[stack[-2]]
                if (top.text[0] == underneath.text[0] == token.text[0]
                        and {len(top.text), len(underneath.text)} == {1, 2}):
                    closed = [stack.pop(), stack.pop()]
                    for opened in closed:
                        openings[opened] = _marker_tags(tokens[opened].text)
                    closings[index] = closed
                    continue
        if token.can_open:
            stack.append(index)
    result = []
    active: list[tuple[int, tuple[_Tag, ...]]] = []
    for index, token in enumerate(tokens):
        if index in openings:
            active.append((index, openings[index]))
        elif index in closings:
            closed = closings[index]
            active = [(opened, tags) for opened, tags in active if opened not in closed]
        else:
            span = _Span(token.text) if isinstance(token, _Marker) else token
            tags = tuple(tag for _, group in active for tag in group)
            result.extend(_decorate([span], tags))
    return result


def _inline(text: str, *, links: bool = True, depth: int = 0) -> list[_Span]:
    if depth >= MAX_NESTING:
        return [_Span(text)]  # Bounded parsing, never truncate a deep label.
    tokens: list[_Span | _Marker] = []
    cursor = 0
    absent_code_runs: set[int] = set()
    no_link_closer = "](" not in text and "]\\(" not in text
    while cursor < len(text):
        match = _SPECIAL.search(text, cursor)
        if match is None:
            tokens.append(_Span(text[cursor:]))
            break
        if match.start() > cursor:
            tokens.append(_Span(text[cursor:match.start()]))
            cursor = match.start()
        char = text[cursor]
        if char == "`":
            end_run = cursor + 1
            while end_run < len(text) and text[end_run] == "`":
                end_run += 1
            run = end_run - cursor
            end = None if run in absent_code_runs else _code_end(text, cursor, run)
            if end is not None:
                tokens.append(_literal(text[end_run:end]))
                cursor = end + run
                continue
            absent_code_runs.add(run)
            tokens.append(_Span(text[cursor:end_run]))
            cursor = end_run
            continue
        if char in "\\$":
            math = _math_at(text, cursor)
            if math:
                expression, cursor, display = math
                tokens.extend(_math_spans(expression, display=display))
                continue
        if char == "\\" and cursor + 1 < len(text) and text[cursor + 1] in _ESCAPABLE:
            tokens.append(_Span(text[cursor + 1]))
            cursor += 2
            continue
        if char == "[" and links and not no_link_closer:
            link = _link_at(text, cursor)
            if link:
                label, url, cursor = link
                label_spans = _inline(label, links=False, depth=depth + 1)
                if _safe_url(url) and label:
                    # Code cannot nest in a link; its literal label text survives.
                    label_spans = [
                        _Span(span.text, tuple(tag for tag in span.tags if tag.name in _STYLE_NAMES))
                        for span in label_spans
                    ]
                    tokens.extend(_decorate(label_spans, (_Tag("a", url),)))
                else:
                    tokens.extend(label_spans)
                    tokens.append(_Span(" (" if label else ""))
                    tokens.append(_literal(url))
                    tokens.append(_Span(")" if label else ""))
                continue
        if char == "<" and links:
            autolink = _URL_AUTOLINK.match(text, cursor)
            if autolink and _safe_url(autolink[1]):
                tokens.append(_Span(autolink[1], (_Tag("a", autolink[1]),)))
                cursor = autolink.end()
                continue
        if char in "*_~|":
            end_run = cursor + 1
            while end_run < len(text) and text[end_run] == char:
                end_run += 1
            marker = text[cursor:end_run]
            if ((char in "*_" and len(marker) <= 3)
                    or (char in "~|" and len(marker) == 2)):
                previous = text[cursor - 1:cursor] if cursor else ""
                following = text[end_run:end_run + 1]
                tokens.append(_Marker(
                    marker,
                    bool(following and not following.isspace() and not _WORD.fullmatch(previous)),
                    bool(previous and not previous.isspace() and not _WORD.fullmatch(following)),
                ))
            else:
                tokens.append(_Span(marker))
            cursor = end_run
            continue
        tokens.append(_Span(char))
        cursor += 1
    return _resolve_markers(tokens)


_SYMBOLS = {
    "alpha": "α", "beta": "β", "gamma": "γ", "delta": "δ", "epsilon": "ε",
    "varepsilon": "ϵ", "zeta": "ζ", "eta": "η", "theta": "θ", "vartheta": "ϑ",
    "iota": "ι", "kappa": "κ", "lambda": "λ", "mu": "μ", "nu": "ν", "xi": "ξ",
    "pi": "π", "varpi": "ϖ", "rho": "ρ", "varrho": "ϱ", "sigma": "σ",
    "varsigma": "ς", "tau": "τ", "upsilon": "υ", "phi": "ϕ", "varphi": "φ",
    "chi": "χ", "psi": "ψ", "omega": "ω", "Gamma": "Γ", "Delta": "Δ",
    "Theta": "Θ", "Lambda": "Λ", "Xi": "Ξ", "Pi": "Π", "Sigma": "Σ",
    "Upsilon": "Υ", "Phi": "Φ", "Psi": "Ψ", "Omega": "Ω",
    "times": "×", "cdot": "·", "div": "÷", "pm": "±", "mp": "∓",
    "le": "≤", "leq": "≤", "ge": "≥", "geq": "≥", "ne": "≠", "neq": "≠",
    "approx": "≈", "equiv": "≡", "sim": "∼", "propto": "∝", "infty": "∞",
    "to": "→", "rightarrow": "→", "leftarrow": "←", "leftrightarrow": "↔",
    "Rightarrow": "⇒", "Leftarrow": "⇐", "Leftrightarrow": "⇔",
    "in": "∈", "notin": "∉", "subset": "⊂", "subseteq": "⊆", "supset": "⊃",
    "supseteq": "⊇", "cup": "∪", "cap": "∩", "emptyset": "∅",
    "forall": "∀", "exists": "∃", "neg": "¬", "land": "∧", "lor": "∨",
    "partial": "∂", "nabla": "∇", "degree": "°", "ldots": "…", "cdots": "⋯",
    "langle": "⟨", "rangle": "⟩", "lvert": "|", "rvert": "|", "vert": "|",
    "sin": "sin", "cos": "cos", "tan": "tan", "log": "log", "ln": "ln", "exp": "exp",
}
_SUPER = dict(zip("0123456789+-=()ni", "⁰¹²³⁴⁵⁶⁷⁸⁹⁺⁻⁼⁽⁾ⁿⁱ"))
_SUB = dict(zip("0123456789+-=()aehijklmnoprstuvx", "₀₁₂₃₄₅₆₇₈₉₊₋₌₍₎ₐₑₕᵢⱼₖₗₘₙₒₚᵣₛₜᵤᵥₓ"))


class _UnsupportedMath(ValueError):
    pass


class _MathReader:
    """A deliberately small, all-or-literal LaTeX reader; never evaluates math."""

    def __init__(self, text: str):
        self.text = text
        self.position = 0

    def sequence(self, closing: str = "", depth: int = 0) -> str:
        if depth > MAX_NESTING:
            raise _UnsupportedMath()
        output = []
        while self.position < len(self.text):
            char = self.text[self.position]
            if char == closing:
                self.position += 1
                return "".join(output)
            if char == "}":
                raise _UnsupportedMath()
            if char in "^_":
                self.position += 1
                if not output:
                    raise _UnsupportedMath()
                while self.position < len(self.text) and self.text[self.position].isspace():
                    self.position += 1
                value = self.atom(depth + 1)
                mapping = _SUPER if char == "^" else _SUB
                output.append("".join(mapping[item] for item in value)
                              if value and all(item in mapping for item in value)
                              else char + "(" + value + ")")
            else:
                output.append(self.atom(depth + 1, grouped=True))
        if closing:
            raise _UnsupportedMath()
        return "".join(output)

    def group(self, depth: int) -> str:
        while self.position < len(self.text) and self.text[self.position].isspace():
            self.position += 1
        if self.text[self.position:self.position + 1] != "{":
            raise _UnsupportedMath()
        self.position += 1
        return self.sequence("}", depth + 1)

    def atom(self, depth: int, *, grouped: bool = False) -> str:
        if depth > MAX_NESTING or self.position >= len(self.text):
            raise _UnsupportedMath()
        char = self.text[self.position]
        self.position += 1
        if char == "{":
            value = self.sequence("}", depth + 1)
            return "(" + value + ")" if grouped else value
        if char in "}&%^_":
            raise _UnsupportedMath()
        if char != "\\":
            return char
        if self.position >= len(self.text):
            raise _UnsupportedMath()
        escaped = self.text[self.position]
        if escaped in "{}%$&_#":
            self.position += 1
            return escaped
        if escaped in ",;: ":
            self.position += 1
            return " "
        if escaped == "!":
            self.position += 1
            return ""
        match = re.match(r"[A-Za-z]+", self.text[self.position:])
        if not match:
            raise _UnsupportedMath()
        name = match[0]
        self.position += len(name)
        if name in _SYMBOLS:
            return _SYMBOLS[name]
        if name in ("frac", "dfrac", "tfrac"):
            numerator = self.group(depth)
            denominator = self.group(depth)
            return f"({numerator})/({denominator})"
        if name == "sqrt":
            if self.text[self.position:self.position + 1] == "[":
                self.position += 1
                degree = self.sequence("]", depth + 1)
                return f"root({degree}, {self.group(depth)})"
            return f"√({self.group(depth)})"
        if name == "text":
            while self.position < len(self.text) and self.text[self.position].isspace():
                self.position += 1
            if self.text[self.position:self.position + 1] != "{":
                raise _UnsupportedMath()
            self.position += 1
            level, literal = 1, []
            while self.position < len(self.text):
                value = self.text[self.position]
                self.position += 1
                if value == "\\":
                    if self.text[self.position:self.position + 1] not in "{}%$&_#\\":
                        raise _UnsupportedMath()
                    literal.append(self.text[self.position])
                    self.position += 1
                elif value == "{":
                    level += 1
                    if level > MAX_NESTING:
                        raise _UnsupportedMath()
                elif value == "}":
                    level -= 1
                    if level == 0:
                        return "".join(literal)
                else:
                    literal.append(value)
            raise _UnsupportedMath()
        if name in ("mathrm", "operatorname"):
            return self.group(depth)
        if name == "mathbb":
            value = self.group(depth)
            if value in {"R", "N", "Z", "Q", "C"}:
                return {"R": "ℝ", "N": "ℕ", "Z": "ℤ", "Q": "ℚ", "C": "ℂ"}[value]
            raise _UnsupportedMath()
        if name in ("left", "right"):
            if self.text[self.position:self.position + 1] in ("(", ")", "[", "]", "|", "\\"):
                return ""
            raise _UnsupportedMath()
        if name in ("quad", "qquad"):
            return " "
        raise _UnsupportedMath()


def _math_spans(expression: str, *, display: bool) -> list[_Span]:
    try:
        if len(expression) > 8000:
            raise _UnsupportedMath()
        converted = _MathReader(expression).sequence()
        return [_Span(converted)]
    except _UnsupportedMath:
        separator = "\n" if display or "\n" in expression or "\r" in expression else " "
        return [_Span(MATH_FALLBACK + separator),
                _literal(expression, block=separator == "\n")]


def _unsafe_boundary(text: str, position: int) -> bool:
    if position <= 0 or position >= len(text):
        return False
    before, after = text[position - 1], text[position]
    if before == "\r" and after == "\n":
        return True
    if before == "\u200d" or after == "\u200d":
        return True
    value = ord(after)
    if unicodedata.category(after).startswith("M") or 0x1F3FB <= value <= 0x1F3FF:
        return True
    if 0x1F1E6 <= value <= 0x1F1FF:
        cursor = position - 1
        while cursor >= 0 and 0x1F1E6 <= ord(text[cursor]) <= 0x1F1FF:
            cursor -= 1
        return (position - cursor - 1) % 2 == 1
    return False


def _ranges(text: str, limit: int):
    start = 0
    while start < len(text):
        cursor = start
        units = 0
        while cursor < len(text):
            width = 2 if ord(text[cursor]) > 0xFFFF else 1
            if units + width > limit:
                break
            units += width
            cursor += 1
        if cursor == start:
            raise ValueError("The chosen limit cannot fit one Unicode character.")
        end = cursor
        if end < len(text):
            floor = start + (end - start) // 2
            # Prefer complete paragraphs, then lines, then words; keep every
            # boundary character instead of strip/lstrip or continuation labels.
            for separator in ("\n\n", "\n", " "):
                boundary = text.rfind(separator, floor, end)
                if boundary >= floor:
                    end = boundary + len(separator)
                    break
            while end > start and _unsafe_boundary(text, end):
                end -= 1
            if end == start:
                # One oversized combining/ZWJ sequence cannot fit in a message.
                # A code-point boundary retains all text without invalid UTF-16.
                end = cursor
        yield start, end
        start = end


def _chunks(spans: list[_Span], limit: int) -> list[list[_Span]]:
    visible = "".join(span.text for span in spans)
    chunks = []
    span_index = 0
    span_offset = 0
    for start, end in _ranges(visible, limit):
        needed = end - start
        chunk = []
        while needed:
            span = spans[span_index]
            take = min(needed, len(span.text) - span_offset)
            if take:
                chunk.append(_Span(span.text[span_offset:span_offset + take], span.tags))
                needed -= take
                span_offset += take
            if span_offset == len(span.text):
                span_index += 1
                span_offset = 0
        chunks.append(chunk)
    return chunks


def _html(spans: list[_Span]) -> str:
    output = []
    current: tuple[_Tag, ...] = ()
    for span in spans:
        common = 0
        while common < min(len(current), len(span.tags)) and current[common] == span.tags[common]:
            common += 1
        output.extend(tag.closing() for tag in reversed(current[common:]))
        output.extend(tag.opening() for tag in span.tags[common:])
        output.append(escape(span.text, quote=False))
        current = span.tags
    output.extend(tag.closing() for tag in reversed(current))
    return "".join(output)


def _plain(spans: list[_Span]) -> str:
    output = []
    active_url = ""
    label: list[str] = []

    def close_link() -> None:
        if active_url and "".join(label) != active_url:
            output.append(" (" + active_url + ")")
        label.clear()

    for span in spans:
        url = next((tag.attribute for tag in span.tags if tag.name == "a"), "")
        if url != active_url:
            close_link()
            active_url = url
        output.append(span.text)
        if active_url:
            label.append(span.text)
    close_link()
    return "".join(output)


def format_telegram(text: str, limit: int = DEFAULT_LIMIT, *, literal: bool = False) -> list[dict]:
    """Return independently balanced sendMessage kwargs, in original text order.

    ``literal=True`` protects an entire unfenced prompt/file as preformatted text.
    An empty source returns []; callers must not treat that as a delivery receipt.
    """
    _require_text(text)
    _limit(limit)
    spans = [_literal(text, block=True)] if literal and text else _parse_blocks(text)
    return [{"text": _html(chunk), "parse_mode": "HTML"} for chunk in _chunks(spans, limit)]


def format_telegram_plain(text: str, limit: int = DEFAULT_LIMIT, *, literal: bool = False) -> list[dict]:
    """Readable full-response fallback, without parse_mode; link targets survive.

    Use plain_fallback for a rejected *part* after a multipart send has started.
    Do not stringify a dict/list into this function; non-string inputs fail closed.
    """
    _require_text(text)
    _limit(limit)
    plain = text if literal else _plain(_parse_blocks(text))
    return [{"text": plain[start:end]} for start, end in _ranges(plain, limit)]


class _PreparedHTML(HTMLParser):
    """Read only the balanced, small HTML vocabulary this module emits."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.stack: list[_Tag] = []
        self.spans: list[_Span] = []

    def handle_starttag(self, tag, attrs):
        attribute = ""
        if tag == "a":
            if len(attrs) != 1 or attrs[0][0] != "href" or not _safe_url(attrs[0][1] or ""):
                raise ValueError("Invalid link in prepared Telegram HTML.")
            attribute = attrs[0][1]
        elif tag == "code" and attrs:
            if (len(attrs) != 1 or attrs[0][0] != "class"
                    or not self.stack or self.stack[-1].name != "pre"
                    or not re.fullmatch(r"language-[A-Za-z][A-Za-z0-9_+#.-]{0,31}", attrs[0][1] or "")):
                raise ValueError("Invalid code language in prepared Telegram HTML.")
            attribute = attrs[0][1][len("language-"):]
        elif tag not in _STYLE_NAMES | {"blockquote", "pre", "code"} or attrs:
            raise ValueError("Unsupported tag in prepared Telegram HTML.")
        self.stack.append(_Tag(tag, attribute))

    def handle_endtag(self, tag):
        if not self.stack or self.stack[-1].name != tag:
            raise ValueError("Unbalanced prepared Telegram HTML.")
        self.stack.pop()

    def handle_startendtag(self, tag, attrs):
        raise ValueError("Unsupported self-closing tag in prepared Telegram HTML.")

    def handle_data(self, data):
        self.spans.append(_Span(data, tuple(self.stack)))

    def handle_comment(self, data):
        raise ValueError("Unexpected comment in prepared Telegram HTML.")

    def handle_decl(self, decl):
        raise ValueError("Unexpected declaration in prepared Telegram HTML.")

    def handle_pi(self, data):
        raise ValueError("Unexpected processing instruction in prepared Telegram HTML.")


def plain_fallback(part: dict, limit: int = DEFAULT_LIMIT) -> list[dict]:
    """Convert just one formatter-produced part to complete, unformatted kwargs.

    This does not send/retry anything. A transport may use it only after a definite
    formatting rejection, never after an uncertain send, and must persist any
    replacement parts before delivery. Link expansion can increase part count.
    """
    _limit(limit)
    if not isinstance(part, dict) or set(part) - {"text", "parse_mode"} or "text" not in part:
        raise TypeError("Pass one prepared Telegram text part, not a serialized response object.")
    text = _require_text(part["text"])
    if part.get("parse_mode") is None:
        plain = text
    elif part["parse_mode"] == "HTML":
        reader = _PreparedHTML()
        reader.feed(text)
        reader.close()
        if reader.stack:
            raise ValueError("Unbalanced prepared Telegram HTML.")
        plain = _plain(reader.spans)
    else:
        raise ValueError("Unsupported parse_mode in prepared Telegram part.")
    return [{"text": plain[start:end]} for start, end in _ranges(plain, limit)]
