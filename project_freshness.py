"""Pure project-report freshness contract; a read or thread scan is not evidence."""
from __future__ import annotations

from datetime import datetime
import math
import time


FRESHNESS_VERSION = 1
STALE_AFTER_SECONDS = 30 * 60
FUTURE_TOLERANCE_SECONDS = 30


def timestamp_seconds(value):
    """Accept finite epoch seconds/milliseconds or explicitly zoned ISO times."""
    if value is None or isinstance(value, bool):
        return None
    try:
        if isinstance(value, str):
            value = value.strip()
            if not value:
                return None
            try:
                value = float(value)
            except ValueError:
                parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
                if parsed.tzinfo is None:
                    return None
                value = parsed.timestamp()
        value = float(value)
        if value >= 1e12:
            value /= 1000
        return value if math.isfinite(value) and value > 0 else None
    except (TypeError, ValueError, OverflowError):
        return None


def project_freshness(evidence_at=None, *, checked_at=None, error=None,
                      now=None, stale_after_seconds=STALE_AFTER_SECONDS):
    """Build the public contract without reading/writing any source or state.

    The caller supplies the original report/evidence timestamp. ``checked_at``
    means a successful check of that source, not a DB/API read or task scan.
    A missing successful source check is explicitly stale, even with a recent
    imported report. The last known evidence timestamp survives source errors.
    ``error`` must be safe, owner-facing text, never a raw provider exception.
    """
    now = time.time() if now is None else timestamp_seconds(now)
    if now is None:
        raise ValueError("A valid reference time is required.")
    threshold = timestamp_seconds(stale_after_seconds)
    if threshold is None or threshold > 7 * 24 * 60 * 60:
        raise ValueError("Use a positive bounded freshness interval.")
    evidence = timestamp_seconds(evidence_at)
    checked = timestamp_seconds(checked_at)
    message = str(error).strip()[:400] if error else None
    invalid_time = (
        (evidence_at is not None and evidence is None)
        or (checked_at is not None and checked is None)
        or (evidence is not None and evidence > now + FUTURE_TOLERANCE_SECONDS)
        or (checked is not None and checked > now + FUTURE_TOLERANCE_SECONDS)
    )
    if invalid_time:
        if evidence is not None and evidence > now + FUTURE_TOLERANCE_SECONDS:
            evidence = None
        if checked is not None and checked > now + FUTURE_TOLERANCE_SECONDS:
            checked = None
        message = message or "The project source has an invalid timestamp. Freshness could not be verified."
    if message:
        state = "error"
    elif evidence is None:
        state = "not_reported"
    elif checked is None or checked + FUTURE_TOLERANCE_SECONDS < evidence:
        state = "stale"
    elif now - evidence > threshold or now - checked > threshold:
        state = "stale"
    else:
        state = "fresh"
    return {
        "state": state,
        "evidence_at": evidence,
        "checked_at": checked,
        "stale_after_seconds": threshold,
        "error": message,
    }


def project_response(payload, *, now=None):
    """Add conservative compatibility metadata to already-authorized views.

    Core ingestion owns source checks and evidence provenance. Legacy views
    expose checklist_observed_at but no source-check result; they remain stale.
    Never derive report freshness from a generic observed_at/status timestamp.
    Existing freshness and all request/delivery receipt fields pass through.
    """
    now = time.time() if now is None else timestamp_seconds(now)
    if now is None:
        raise ValueError("A valid reference time is required.")
    result = dict(payload)
    result.setdefault("snapshot_at", now)
    result.setdefault("freshness_version", FRESHNESS_VERSION)

    def with_freshness(project):
        value = dict(project)
        if not isinstance(value.get("freshness"), dict):
            value["freshness"] = project_freshness(
                value.get("checklist_observed_at"), now=now)
        return value

    if isinstance(result.get("projects"), list):
        result["projects"] = [with_freshness(item) for item in result["projects"]]
    if isinstance(result.get("project"), dict):
        result["project"] = with_freshness(result["project"])
    return result
