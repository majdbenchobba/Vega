"""Shared redaction for owner-facing transport text."""
import re

TOKEN_PATTERN = re.compile(r"\b\d{8,12}:[A-Za-z0-9_-]{30,}\b")


def redact(text):
    text = TOKEN_PATTERN.sub("[credential hidden]", str(text))
    return re.sub(
        r"(?im)^.*(?:password\s*[:=]|api[_ -]?key\s*[:=]|secret\s*[:=]|"
        r"bearer\s+[A-Za-z0-9_.-]+|AWS_ACCESS_KEY_ID|AWS_SECRET_ACCESS_KEY).*$",
        "[credential line hidden]",
        text,
    )
