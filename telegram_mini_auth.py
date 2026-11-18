"""Offline authentication of Telegram.WebApp.initData using only the stdlib.

Protocol: https://core.telegram.org/bots/webapps#validating-data-received-via-the-mini-app
The bot-token HMAC covers every decoded field except ``hash``. In particular,
``signature`` remains in this check; only Telegram's separate third-party
Ed25519 procedure excludes both ``hash`` and ``signature``.
"""

import hashlib
import hmac
import json
import math
import re
import time
from urllib.parse import parse_qsl


__all__ = ["MiniAppAuthError", "verify_telegram_init_data"]

_MAX_RAW_BYTES = 16_384
_MAX_FIELDS = 64
_MAX_USER_ID = (1 << 52) - 1
_MAX_AUTH_DATE = (1 << 63) - 1
_MAX_JSON_DEPTH = 32
_BAD_PERCENT = re.compile(r"%(?![0-9a-fA-F]{2})")
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")
_HASH = re.compile(r"[0-9a-fA-F]{64}")
_AUTH_DATE = re.compile(r"(?:0|[1-9][0-9]{0,18})")
_BOT_TOKEN = re.compile(r"[\x21-\x7e]{1,256}")


class MiniAppAuthError(ValueError):
    """Authentication failed; verifier errors never contain supplied data."""


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError
        result[key] = value
    return result


def _reject_json_constant(_value):
    raise ValueError


def _validate_user_json(user):
    # Iteration makes our nesting bound independent of Python's recursion limit.
    pending = [(user, 1)]
    while pending:
        value, depth = pending.pop()
        if type(value) in (dict, list):
            if depth > _MAX_JSON_DEPTH:
                raise ValueError
            if type(value) is dict:
                for key in value:
                    key.encode("utf-8", errors="strict")
                children = value.values()
            else:
                children = value
            pending.extend((child, depth + 1) for child in children)
        elif type(value) is str:
            # json.loads accepts isolated surrogate escapes; UTF-8 does not.
            value.encode("utf-8", errors="strict")
        elif type(value) is float and not math.isfinite(value):
            # A syntactically numeric exponent such as 1e9999 can overflow.
            raise ValueError


def _seconds(value):
    # Exact built-in types reject bools and implicit/string numeric coercion.
    if type(value) not in (int, float) or not math.isfinite(value) or value < 0:
        raise ValueError
    return value


def _verify(raw, bot_token, owner_id, now, max_age, future_skew):
    if type(raw) is not str or not raw or len(raw) > _MAX_RAW_BYTES:
        raise ValueError
    raw_bytes = raw.encode("utf-8", errors="strict")
    if len(raw_bytes) > _MAX_RAW_BYTES or _BAD_PERCENT.search(raw):
        raise ValueError
    if type(bot_token) is not str or not _BOT_TOKEN.fullmatch(bot_token):
        raise ValueError
    if type(owner_id) is not int or not 1 <= owner_id <= _MAX_USER_ID:
        raise ValueError

    current = _seconds(time.time() if now is None else now)
    max_age = _seconds(max_age)
    future_skew = _seconds(future_skew)
    fields = _unique_object(parse_qsl(
        raw,
        keep_blank_values=True,
        strict_parsing=True,
        encoding="utf-8",
        errors="strict",
        max_num_fields=_MAX_FIELDS,
        separator="&",
    ))
    for key, value in fields.items():
        # Forbid ambiguous data-check-string separators, including in unknown
        # fields. JSON-escaped newlines inside profile strings remain valid.
        if not key or "=" in key or _CONTROL.search(key) or _CONTROL.search(value):
            raise ValueError

    supplied_hash = fields.pop("hash", None)
    if supplied_hash is None or not _HASH.fullmatch(supplied_hash):
        raise ValueError
    check_string = "\n".join(f"{key}={fields[key]}" for key in sorted(fields))
    secret = hmac.digest(b"WebAppData", bot_token.encode("ascii"), "sha256")
    expected = hmac.digest(secret, check_string.encode("utf-8"), "sha256")
    if not hmac.compare_digest(expected, bytes.fromhex(supplied_hash)):
        raise ValueError

    # Interpret claims only after their original decoded strings authenticate.
    auth_date_text = fields.get("auth_date", "")
    if not _AUTH_DATE.fullmatch(auth_date_text):
        raise ValueError
    auth_date = int(auth_date_text)
    if auth_date > _MAX_AUTH_DATE:
        raise ValueError
    age = current - auth_date
    if age > max_age or age < -future_skew:
        raise ValueError

    user = json.loads(
        fields.get("user", ""),
        object_pairs_hook=_unique_object,
        parse_constant=_reject_json_constant,
    )
    if type(user) is not dict:
        raise ValueError
    _validate_user_json(user)
    user_id = user.get("id")
    if type(user_id) is not int or not 1 <= user_id <= _MAX_USER_ID:
        raise ValueError
    if user.get("is_bot", False) is not False or user_id != owner_id:
        raise ValueError

    return {
        "user_id": user_id,
        "auth_date": auth_date,
        "query_id": fields.get("query_id"),
        "start_param": fields.get("start_param"),
        "init_data_sha256": hashlib.sha256(raw_bytes).hexdigest(),
    }


def verify_telegram_init_data(
    raw, bot_token, owner_id, *, now=None, max_age=900, future_skew=30
):
    """Return an authenticated minimal principal, or raise MiniAppAuthError.

    Supply the exact initData string and the *current server-side* bot token and
    paired owner ID on every request. Never derive these arguments from
    initDataUnsafe or client-supplied owner claims. No pairing state is cached.

    ``raw`` must be a nonempty UTF-8 str of at most 16,384 bytes and 64 query
    fields, including hash. Percent escapes and UTF-8 decoding are strict;
    duplicate decoded keys, blank keys, control characters, and '=' in keys are
    rejected. Tokens must be 1-256 printable ASCII characters without spaces.
    Both owner and user IDs must be positive int values with at most 52 bits;
    bools are invalid. The signed user must be an object with is_bot absent or
    exactly false. JSON duplicate keys, nonfinite numbers, and more than 32
    nested JSON containers (including the user object) are invalid.

    auth_date is a canonical nonnegative decimal integer fitting signed 64 bits.
    ``now`` defaults to time.time(); time arguments accept finite nonnegative
    int/float seconds, excluding bools. Freshness boundaries are inclusive:
    -future_skew <= now - auth_date <= max_age. Zero-length windows are allowed.

    The result contains only user_id/auth_date integers, query_id/start_param
    strings (None when absent, empty strings preserved), and a lowercase SHA-256
    hex digest of the exact raw UTF-8 input. That digest is a fingerprint, not
    single-use replay protection. No user profile or credential is returned.
    """
    try:
        return _verify(raw, bot_token, owner_id, now, max_age, future_skew)
    except (ValueError, TypeError, OverflowError, RecursionError):
        # Raise outside the handler so even __context__ retains no parser
        # exception containing input, a JSON document, or a credential.
        pass
    raise MiniAppAuthError("Invalid Telegram Mini App authentication.") from None
