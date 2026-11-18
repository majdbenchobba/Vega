"""Separate a project's history identity from its current dispatch owner."""

import math
import time


def dispatch_target(tasks, alias):
    if alias not in tasks:
        raise ValueError("Unknown registered project.")
    seen, current = set(), alias
    while True:
        if current in seen:
            raise ValueError("Project routing contains a cycle.")
        seen.add(current)
        info = tasks.get(current)
        if not isinstance(info, dict):
            raise ValueError("The project routing owner is unavailable.")
        next_alias = info.get("routing_alias")
        if next_alias is None:
            return {"alias": current, "thread_id": info["thread_id"], "label": info["label"]}
        if not isinstance(next_alias, str) or next_alias not in tasks:
            raise ValueError("The project routing owner is unavailable.")
        current = next_alias


def chat_check(info, *, now=None):
    """A saved check applies only to the exact chat that was inspected."""
    now = time.time() if now is None else now
    check = info.get("chat_check")
    unchecked = {"state": "unchecked", "checked_at": None, "title": None}
    if not isinstance(check, dict) or check.get("thread_id") != info.get("thread_id"):
        return unchecked
    stamp = check.get("checked_at")
    if (type(stamp) not in (int, float) or not math.isfinite(stamp)
            or stamp <= 0 or stamp > now + 30 or check.get("state") not in ("found", "missing")):
        return unchecked
    state = check["state"]
    # A positive check is a dated observation, not continuous monitoring.
    # A known missing target remains blocked until it is explicitly rechecked.
    if state == "found" and now - stamp > 86400:
        state = "check_due"
    title = check.get("title")
    return {"state": state, "checked_at": stamp,
            "title": title if isinstance(title, str) and len(title) <= 200 else None}


def project_connection(tasks, alias, *, now=None):
    now = time.time() if now is None else now
    target = dispatch_target(tasks, alias)
    source = chat_check(tasks[alias], now=now)
    destination = chat_check(tasks[target["alias"]], now=now)
    can_route = destination["state"] != "missing"
    state = ("missing" if not can_route else "history_missing" if source["state"] == "missing"
             else "found" if source["state"] == destination["state"] == "found"
             else "check_due" if "check_due" in (source["state"], destination["state"]) else "unchecked")
    return {"state": state, "can_route": can_route, "source": source, "destination": destination,
            "via_project": target["alias"] if target["alias"] != alias else None}


def require_dispatchable(tasks, alias):
    target = dispatch_target(tasks, alias)
    if chat_check(tasks[target["alias"]])["state"] == "missing":
        raise ValueError("This project's chat was not found at the last check. Reconnect it before sending a new request.")
    return target
