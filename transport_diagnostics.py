"""Pure, allowlisted diagnostics for future Telegram transport failures.

Never format the supplied exception or retain it in the result. Diagnostics
describe one failure; they do not authorize retries or establish delivery.
"""

from __future__ import annotations

import errno
import http.client
import json
import math
import socket
import ssl
import urllib.error


MAX_SUMMARY_LENGTH = 160
MAX_REASON_DEPTH = 8

_METHODS = frozenset({
    "getMe", "getWebhookInfo", "getUpdates", "getFile",
    "sendMessage", "sendVoice", "sendDocument", "sendChatAction", "getChatMenuButton", "setChatMenuButton",
})
_RESPONSE_STAGES = frozenset({
    "read_response", "decode_response", "validate_response",
})
_STAGES = _RESPONSE_STAGES | {"request"}
_MAX_ELAPSED_MS = 86_400_000
_MAX_BYTE_COUNT = (1 << 63) - 1
_MAX_ERROR_CODE = (1 << 31) - 1

_SUMMARIES = {
    "timeout": "The request timed out; delivery was not confirmed.",
    "tls_error": "A TLS transport failure occurred; delivery was not confirmed.",
    "connection_reset": "The connection was reset; delivery was not confirmed.",
    "connection_refused": "The connection was refused; delivery was not confirmed.",
    "dns_error": "Name resolution failed; delivery was not confirmed.",
    "connection_error": "The connection failed; delivery was not confirmed.",
    "url_error": "The URL transport failed; delivery was not confirmed.",
    "transport_error": "A transport failure occurred; delivery was not confirmed.",
    "malformed_json": "The response was not valid JSON; delivery was not confirmed.",
    "malformed_response": "The response was incomplete or invalid; delivery was not confirmed.",
    "http_rejection": "The server returned an HTTP rejection.",
    "http_error": "An HTTP response failure occurred; delivery was not confirmed.",
    "unknown": "The operation failed; delivery was not confirmed.",
}


class MalformedResponseError(ValueError):
    """Marker for an invalid response envelope or missing delivery receipt."""


# Order matters: HTTPError is a URLError, TLS errors are OSErrors, and
# RemoteDisconnected is both a ConnectionResetError and a BadStatusLine.
# Labels are literals, never an arbitrary exception class's name or module.
_EXCEPTION_RULES = (
    (urllib.error.HTTPError, "HTTPError", "http_error"),
    (TimeoutError, "TimeoutError", "timeout"),
    (ssl.SSLCertVerificationError, "SSLCertVerificationError", "tls_error"),
    (ssl.SSLError, "SSLError", "tls_error"),
    (socket.gaierror, "gaierror", "dns_error"),
    (socket.herror, "herror", "dns_error"),
    (http.client.RemoteDisconnected, "RemoteDisconnected", "connection_reset"),
    (ConnectionResetError, "ConnectionResetError", "connection_reset"),
    (ConnectionRefusedError, "ConnectionRefusedError", "connection_refused"),
    (ConnectionAbortedError, "ConnectionAbortedError", "connection_error"),
    (BrokenPipeError, "BrokenPipeError", "connection_error"),
    (ConnectionError, "ConnectionError", "connection_error"),
    (MalformedResponseError, "MalformedResponseError", "malformed_response"),
    (json.JSONDecodeError, "JSONDecodeError", "malformed_json"),
    (UnicodeDecodeError, "UnicodeDecodeError", "malformed_response"),
    (http.client.IncompleteRead, "IncompleteRead", "malformed_response"),
    (http.client.BadStatusLine, "BadStatusLine", "malformed_response"),
    (http.client.LineTooLong, "LineTooLong", "malformed_response"),
    (http.client.HTTPException, "HTTPException", "malformed_response"),
    (urllib.error.URLError, "URLError", "url_error"),
    (OSError, "OSError", "transport_error"),
    (KeyError, "KeyError", "unknown"),
    (TypeError, "TypeError", "unknown"),
    (AttributeError, "AttributeError", "unknown"),
    (ValueError, "ValueError", "unknown"),
    (Exception, "Exception", "unknown"),
)

_ERROR_CODE_CATEGORIES = {
    errno.ETIMEDOUT: "timeout",
    errno.ECONNRESET: "connection_reset",
    errno.ECONNREFUSED: "connection_refused",
    errno.ECONNABORTED: "connection_error",
    errno.EPIPE: "connection_error",
    errno.ENETUNREACH: "connection_error",
    errno.EHOSTUNREACH: "connection_error",
    # Winsock codes can appear in errno or winerror, including on fixtures
    # constructed on another platform. No localized error text is inspected.
    10060: "timeout",
    10054: "connection_reset",
    10061: "connection_refused",
    10053: "connection_error",
    10051: "connection_error",
    10065: "connection_error",
    11001: "dns_error",
    11002: "dns_error",
    11003: "dns_error",
    11004: "dns_error",
}


def _attribute(error, name):
    try:
        # Do not invoke __getattr__: HTTPError can delegate missing fields
        # to its response stream through its file-wrapper base class.
        return object.__getattribute__(error, name)
    except Exception:
        # A diagnostic must not expose a secondary attribute-access error.
        return None


def _integer(value, minimum, maximum):
    # bool and user-defined numeric subclasses are deliberately excluded.
    if type(value) is int and minimum <= value <= maximum:
        return value
    return None


def _label(value, allowed):
    return value if type(value) is str and value in allowed else "unknown"


def _reason(error):
    seen = {id(error)}
    for _ in range(MAX_REASON_DEPTH):
        if not isinstance(error, urllib.error.URLError) or isinstance(error, urllib.error.HTTPError):
            break
        nested = _attribute(error, "reason")
        if not isinstance(nested, BaseException) or id(nested) in seen:
            break
        seen.add(id(nested))
        error = nested
    return error


def classify_transport_failure(
    error: BaseException,
    *,
    method: str | None = None,
    stage: str | None = None,
    elapsed_ms: int | float | None = None,
    byte_count: int | None = None,
) -> dict[str, str | int | float | bool]:
    """Return a fresh JSON-safe dictionary containing only allowlisted data.

    Follow at most eight exception-valued URLError.reason links. Exception
    types are canonical allowlisted base types; unknown types become
    "Exception". No args, text reasons, cause/context chains, URLs, headers,
    bodies, tracebacks, filenames, or response objects are inspected.

    All failures remain uncertain except a typed HTTPError with a native
    integer 4xx status. Even that explicit rejection is not permission to
    replay. HTTP 5xx failures remain uncertain because a server or gateway
    failure does not establish whether the remote operation took effect.

    method/stage must exactly match the allowlists above, otherwise "unknown"
    is emitted. elapsed_ms is finite, nonnegative, at most one day, and rounded
    to three decimal places. byte_count is a nonnegative signed-64-bit integer
    describing prepared bytes, not proof of transmitted bytes. Invalid numeric
    metadata is omitted, without coercion.
    """
    selected = _reason(error)
    category, exception_type = "unknown", "Exception"
    for exception_class, label, candidate in _EXCEPTION_RULES:
        if isinstance(selected, exception_class):
            category, exception_type = candidate, label
            break

    diagnostic = {
        "category": category,
        "exception_type": exception_type,
        "method": _label(method, _METHODS),
        "stage": _label(stage, _STAGES),
    }

    if isinstance(selected, OSError):
        for name, minimum in (("errno", -(1 << 31)), ("winerror", 0)):
            value = _integer(_attribute(selected, name), minimum, _MAX_ERROR_CODE)
            if value is not None:
                diagnostic[name] = value
        if category in {"transport_error", "connection_error", "url_error"}:
            for name in ("winerror", "errno"):
                mapped = _ERROR_CODE_CATEGORIES.get(diagnostic.get(name))
                if mapped is not None:
                    category = mapped
                    break

    if isinstance(selected, urllib.error.HTTPError):
        status = _integer(_attribute(selected, "code"), 100, 599)
        if status is not None:
            diagnostic["http_status"] = status
            if 400 <= status <= 499:
                category = "http_rejection"
    elif (
        category == "unknown"
        and diagnostic["stage"] in _RESPONSE_STAGES
        and isinstance(selected, (ValueError, TypeError, KeyError, AttributeError))
    ):
        category = "malformed_response"

    diagnostic["category"] = category
    diagnostic["uncertain"] = category != "http_rejection"
    diagnostic["summary"] = _SUMMARIES[category][:MAX_SUMMARY_LENGTH]

    if (
        type(elapsed_ms) in (int, float)
        and 0 <= elapsed_ms <= _MAX_ELAPSED_MS
        and math.isfinite(elapsed_ms)
    ):
        diagnostic["elapsed_ms"] = round(elapsed_ms, 3)
    count = _integer(byte_count, 0, _MAX_BYTE_COUNT)
    if count is not None:
        diagnostic["byte_count"] = count
    return diagnostic
