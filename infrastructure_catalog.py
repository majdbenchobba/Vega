"""Load an optional machine-local infrastructure catalog."""

from copy import deepcopy
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent
PRIVATE_CATALOG = ROOT / "infrastructure_catalog.local.json"
CATALOG_KEYS = ("groups", "nodes", "edges", "backups", "sources", "resource_roles")


def _load_catalog():
    try:
        value = json.loads(PRIVATE_CATALOG.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {
            "version": 1,
            "audited_at": None,
            "groups": [],
            "nodes": [],
            "edges": [],
            "backups": [],
            "sources": {},
            "resource_roles": {},
        }
    if not isinstance(value, dict):
        raise ValueError("Invalid local infrastructure catalog.")
    value.setdefault("resource_roles", {})
    if (type(value.get("version")) is not int
            or any(not isinstance(value.get(key), expected)
                   for key, expected in (("groups", list), ("nodes", list), ("edges", list),
                                         ("backups", list), ("sources", dict)))
            or not isinstance(value.get("resource_roles"), dict)
            or (value.get("audited_at") is not None and not isinstance(value["audited_at"], str))):
        raise ValueError("Invalid local infrastructure catalog.")
    if any(not isinstance(key, str) or not key or not isinstance(role, str) or not role
           for key, role in value["resource_roles"].items()):
        raise ValueError("Invalid local infrastructure resource roles.")
    return value


_CATALOG = _load_catalog()
AUDITED_AT = _CATALOG["audited_at"]


def catalog():
    return deepcopy({key: _CATALOG[key] for key in ("version", *CATALOG_KEYS)})
