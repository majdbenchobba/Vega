"""Bounded owner-authorized discovery and reads; no execution or persistent writes.

Main supplies the reviewed grant and chooses intent/project context. Bindings are
detached JSON snapshots, suitable for storing in Main's existing orchestration.
File text, filenames, and receipts are source data and never select further work.
"""
from __future__ import annotations

from collections import deque
from contextlib import contextmanager, ExitStack
import json
import os
from pathlib import Path
import re
import sqlite3
import stat
import time

from companion import ConflictError
from memory_policy import contains_secret
import project_files
from project_files import (
    DEVICE_NAME, EXCLUDED, MAX_BYTES, SENSITIVE_NAME, TEXT_EXTENSIONS,
    ProjectFiles, digest, file_identity, is_text_path, relative_path, text_from_bytes,
)
from routing_targets import dispatch_target


DEFAULT_POLICY_PATH = Path(__file__).absolute().with_name("owner-file-access.json")
MAX_VISITED = 2400
MAX_MATCHES = 80
MAX_FILES = 3
MAX_CHARS = 12000
SEARCH_SECONDS = 2.0
MAX_SCOPES = 64
MAX_PROJECTS = 256

# Apply the existing relative-path exclusions, plus credential stores that can
# occur above a granted folder or have ordinary-looking Windows filenames.
PRIVATE_COMPONENTS = EXCLUDED | {
    "appdata", "application data", "local settings", "programdata",
    ".config", ".local", ".mozilla", ".pki", ".gnupg", ".azure", ".kube",
    ".docker", ".npm", ".netrc", ".npmrc", ".pypirc", ".git-credentials",
    "microsoftedge", "user data", "profiles", "keychains", "keychain",
    "login data", "web data", "local state", "cookies", "logins.json",
    "key4.db", "cert9.db", "ntuser.dat", "usrclass.dat", "sam", "security",
    "system volume information", "$recycle.bin", "config.msi",
    "id_rsa", "id_dsa", "id_ecdsa", "id_ed25519", "authorized_keys",
}
PRIVATE_SUFFIXES = {".pem", ".key", ".pfx", ".p12", ".kdb", ".kdbx", ".keychain"}
BAD_CHARS = re.compile(r'[:*?"<>|\x00-\x1f\x7f\u202a-\u202e\u2066-\u2069]')
# Hidden/system, offline, and recall-on-open/data-access files are not admitted.
PRIVATE_ATTRIBUTES = 0x2 | 0x4 | 0x1000 | 0x40000 | 0x400000
STOP_WORDS = set(
    "a an and are as at be by can could do find for from give how i in is it local locate "
    "look me my of on open or our please read search show tell that the these "
    "this to using want what where which with would you your file files".split()
)


def _json_bytes(value):
    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True,
                          separators=(",", ":"), allow_nan=False).encode("utf-8")
    except (TypeError, ValueError, UnicodeError, RecursionError):
        raise ValueError("Use a bounded JSON file-access record.") from None


def _hash(value):
    return digest(_json_bytes(value))


def _object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate file-access policy fields are not allowed.")
        result[key] = value
    return result


def _text(value, maximum=2000):
    if (not isinstance(value, str) or not 1 <= len(value.strip()) <= maximum
            or contains_secret(value)):
        raise ValueError("Use a bounded, non-secret file request.")
    try:
        value.encode("utf-8")
    except UnicodeError:
        raise ValueError("Use valid UTF-8 request text.") from None
    if any(ord(char) < 32 and char not in "\r\n\t" for char in value):
        raise ValueError("Control characters are excluded.")
    return value.strip()


def _private_component(name):
    folded = name.casefold()
    return (folded in PRIVATE_COMPONENTS or SENSITIVE_NAME.search(name)
            or contains_secret(name) or Path(name).suffix.casefold() in PRIVATE_SUFFIXES)


def _absolute(value):
    """Validate lexical spelling before any normalization or filesystem access."""
    if not isinstance(value, str) or not value or len(value) > 4096:
        raise ValueError("A local absolute path is required.")
    if value.startswith(("\\\\", "//", "\\??\\", "\\Device\\")):
        raise ValueError("UNC and device paths are excluded.")
    if os.name == "nt":
        if not re.match(r"^[A-Za-z]:[\\/]", value):
            raise ValueError("A local drive path is required.")
        tail = value[3:]
    else:
        if not value.startswith("/") or "\\" in value:
            raise ValueError("A local absolute path is required.")
        tail = value[1:]
    parts = re.split(r"[\\/]", tail)
    if parts and parts[-1] == "":
        parts.pop()  # A single trailing directory separator is harmless.
    for part in parts:
        if (not part or part in {".", ".."} or part.endswith((" ", "."))
                or "~" in part or BAD_CHARS.search(part) or DEVICE_NAME.match(part)
                or _private_component(part)):
            raise ValueError("This local path is excluded from owner file access.")
    return Path(os.path.abspath(value))


def _relative(value, *, directory=False):
    value = relative_path(value, directory=directory)
    if any(_private_component(part) for part in value.split("/") if part):
        raise ValueError("This local path is excluded from owner file access.")
    return value


def _under(path, root):
    return path.is_relative_to(Path(root))


def _root_for(path, roots):
    candidates = [root for root in roots if _under(path, root["path"])]
    if not candidates:
        raise PermissionError("The path is outside the reviewed local roots.")
    return max(candidates, key=lambda root: len(Path(root["path"]).parts))


def _relative_to(path, root, *, directory=False):
    return _relative(path.relative_to(Path(root["path"])).as_posix()
                     if path != Path(root["path"]) else "", directory=directory)


def select_request_path(goal, path=None):
    """Pure, deterministic selection from Main's request, never from file text.

    An explicit owner-supplied path wins. Otherwise use the same current-clause
    binding as conversation admission; later source quotes cannot add targets.
    """
    goal = _text(goal)
    if path is not None:
        if not isinstance(path, str) or not path or len(path) > 4096:
            raise ValueError("Use one bounded local path.")
        return path
    from automatic_context import requested_path
    return requested_path(goal)


def _words(goal):
    return list(dict.fromkeys(word for word in re.findall(r"[^\W_]+", goal.casefold())
                              if len(word) >= 2 and word not in STOP_WORDS))[:24]


def _ordinary(info, *, directory=False):
    if (stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & (0x400 | PRIVATE_ATTRIBUTES)
            or (directory and not stat.S_ISDIR(info.st_mode))
            or (not directory and (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1))):
        raise ValueError("Only ordinary local files and folders are available.")


@contextmanager
def _directory(root, relative=""):
    """Reuse ProjectFiles' pinned Windows ancestry; also anchor POSIX traversal."""
    relative = _relative(relative, directory=True)
    files = ProjectFiles(root["path"], root["identity"])
    with files.open(relative, directory=True) as path:
        if os.name == "nt":
            yield path, None
            return
        # ProjectFiles already rejects links. Directory descriptors additionally
        # prevent a POSIX rename between that check and a later child open.
        with ExitStack() as stack:
            descriptor = None
            current = Path(path.anchor)
            for component in [path.anchor, *path.parts[1:]]:
                current = Path(component) if descriptor is None else current / component
                opened = os.open(component, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                                 dir_fd=descriptor)
                stack.callback(os.close, opened)
                descriptor = opened
                info = os.fstat(opened)
                if current == files.root and file_identity(info) != root["identity"]:
                    raise ConflictError("The authorized local folder was replaced.")
            yield path, descriptor


def _lstat(path, descriptor=None):
    return path.lstat() if descriptor is None else os.stat(path.name, dir_fd=descriptor, follow_symlinks=False)


@contextmanager
def _file(root, path):
    relative = _relative_to(path, root)
    parent = str(Path(relative).parent).replace("\\", "/")
    with _directory(root, "" if parent == "." else parent) as (_, descriptor):
        before = _lstat(path, descriptor)
        _ordinary(before)
        with ExitStack() as stack:
            if os.name == "nt":
                # This is exactly ProjectFiles' final-path, single-link handle.
                # It permits metadata for other suffixes without reading bytes.
                stream = stack.enter_context(project_files._windows_handle(path, directory=False))
            else:
                opened = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
                                 dir_fd=descriptor)
                stream = stack.enter_context(os.fdopen(opened, "rb"))
            info = os.fstat(stream.fileno())
            _ordinary(info)
            if file_identity(info) != file_identity(before):
                raise ConflictError("The local file was substituted before opening.")
            yield stream, info
            after = os.fstat(stream.fileno())
            named = _lstat(path, descriptor)
            _ordinary(after)
            _ordinary(named)
            if (file_identity(named) != file_identity(info)
                    or (file_identity(after), after.st_size, after.st_mtime_ns, after.st_ctime_ns)
                    != (file_identity(info), info.st_size, info.st_mtime_ns, info.st_ctime_ns)):
                raise ConflictError("The local file changed during its read.")


class OwnerFiles:
    def __init__(self, store, tasks, policy_path=None, policy=None, clock=time.time):
        self.store, self.tasks, self.clock = store, tasks, clock
        self.policy_path = Path(policy_path) if policy_path is not None else DEFAULT_POLICY_PATH
        # Retain the injected source so replacing grant values revokes bindings.
        # Each load below creates a detached JSON snapshot.
        self.policy = policy

    def _owner(self, owner):
        if type(owner) is not int or owner <= 0:
            raise PermissionError("The paired owner is required.")
        path = Path(self.store.path).absolute()
        db = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=0.25)
        try:
            values = {key: json.loads(value) for key, value in db.execute(
                "SELECT key,value FROM settings WHERE key IN ('owner_id','owner_chat_id')")}
            if any(type(values.get(key)) is not int or values[key] != owner
                   for key in ("owner_id", "owner_chat_id")):
                raise PermissionError("The paired owner is required.")
            registered = {}
            if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' "
                          "AND name='project_tool_workspaces'").fetchone():
                db.row_factory = sqlite3.Row
                registered = {row["project"]: dict(row) for row in db.execute(
                    "SELECT project,thread_id,root,root_identity,revision "
                    "FROM project_tool_workspaces WHERE owner=?", (owner,))}
            return registered
        finally:
            db.close()

    def _policy(self):
        if self.policy is None:
            path = _absolute(str(self.policy_path))
            root = {"path": str(path.parent), "identity": ProjectFiles(path.parent).identity()}
            with _file(root, path) as (stream, info):
                if info.st_size > MAX_BYTES:
                    raise ValueError("The owner file policy is too large.")
                data = stream.read(MAX_BYTES + 1)
        else:
            data = _json_bytes(self.policy)
        text = text_from_bytes(data)
        try:
            policy = json.loads(text, object_pairs_hook=_object)
        except (UnicodeError, RecursionError, json.JSONDecodeError):
            raise ValueError("The owner file policy is invalid.") from None
        if type(policy) is not dict or set(policy) != {
            "version", "owner", "grant_id", "source", "roots", "search_roots", "projects",
        }:
            raise ValueError("A reviewed version 1 owner file policy is required.")
        if type(policy["version"]) is not int or policy["version"] != 1:
            raise ValueError("Unsupported owner file policy version.")
        if type(policy["owner"]) is not int or policy["owner"] <= 0:
            raise ValueError("The grant must name its paired owner.")
        _text(policy["grant_id"], 160)
        source = policy["source"]
        if (type(source) is not dict or set(source) != {"kind", "thread_id", "text"}
                or source["kind"] != "codex_owner_request"):
            raise ValueError("An explicit Codex owner request must source this grant.")
        _text(source["thread_id"], 160)
        _text(source["text"], 16000)
        return policy, digest(data)

    def _state(self, owner):
        registrations = self._owner(owner)
        policy, policy_hash = self._policy()
        if policy["owner"] != owner:
            raise PermissionError("This grant belongs to a different owner pairing.")
        if (type(policy["roots"]) is not list or not 1 <= len(policy["roots"]) <= MAX_SCOPES
                or type(policy["search_roots"]) is not list or len(policy["search_roots"]) > MAX_SCOPES
                or type(policy["projects"]) is not dict or len(policy["projects"]) > MAX_PROJECTS):
            raise ValueError("Use bounded reviewed roots and project mappings.")
        roots, seen = [], set()
        for item in policy["roots"]:
            if type(item) is not dict or set(item) != {"path", "identity"}:
                raise ValueError("Each root needs its reviewed path and identity.")
            path, identity = _absolute(item["path"]), _text(item["identity"], 160)
            key = os.path.normcase(str(path))
            if key in seen:
                raise ValueError("Duplicate authorized roots are not allowed.")
            seen.add(key)
            ProjectFiles(path, identity).identity()
            roots.append({"path": str(path), "identity": identity})

        def scope(value, expected=None):
            path = _absolute(value)
            root = _root_for(path, roots)
            relative = _relative_to(path, root, directory=True)
            if path == Path(path.anchor):
                raise ValueError("Drive roots permit direct reads, not automatic disk traversal.")
            with _directory(root, relative):
                info = path.lstat()
                _ordinary(info, directory=True)
                identity = ProjectFiles(path, expected).identity()
            return {"path": str(path), "identity": identity,
                    "grant_root": root["path"], "grant_identity": root["identity"]}

        search_roots = []
        for value in policy["search_roots"]:
            record = scope(value)
            if record not in search_roots:
                search_roots.append(record)
        priority = {"desktop": 0, "documents": 1, "downloads": 2}
        search_roots.sort(key=lambda root: (priority.get(Path(root["path"]).name.casefold(), 3),
                                            os.path.normcase(root["path"])))
        tasks = self.tasks() if callable(self.tasks) else self.tasks
        projects, unavailable_projects = {}, {}
        for alias, item in policy["projects"].items():
            _text(alias, 160)
            if type(item) is not dict or set(item) != {"root", "thread_id", "identity"}:
                raise ValueError("Each mapped project needs its root, thread and identity.")
            _text(item["identity"], 160)
            _text(item["thread_id"], 160)
            try:
                target = dispatch_target(tasks, alias)
            except (KeyError, TypeError, ValueError):
                raise ConflictError("A mapped project is no longer registered.") from None
            if target["thread_id"] != item["thread_id"]:
                raise ConflictError("A mapped project's thread changed.")
            path = _absolute(item["root"])
            _relative_to(path, _root_for(path, roots), directory=True)
            registration = registrations.get(alias)
            if registration and (registration["thread_id"] != item["thread_id"]
                                 or _absolute(registration["root"]) != path
                                 or registration["root_identity"] != item["identity"]):
                raise ConflictError("The registered project folder changed.")
            try:
                record = scope(item["root"], item["identity"])
            except (OSError, ValueError):
                # A moved/missing project must not disable unrelated reads.
                # Do not treat replacement identities, links, denied access or
                # malformed grants as ordinary missing folders.
                try:
                    path.lstat()
                except FileNotFoundError:
                    unavailable_projects[alias] = "Folder not found. Update its project connection."
                    continue
                raise
            projects[alias] = {**record, "thread_id": item["thread_id"],
                               "dispatch_alias": target["alias"], "registration": registration}
        return {"policy_hash": policy_hash, "grant_id": policy["grant_id"], "roots": roots,
                "search_roots": search_roots, "projects": projects,
                **({"unavailable_projects": unavailable_projects} if unavailable_projects else {}),
                "source": {"kind": "codex_owner_request", "thread_id": policy["source"]["thread_id"],
                           "sha256": digest(policy["source"]["text"].encode("utf-8"))}}

    def capabilities(self, owner):
        # Boot and unpaired callers get no paths and cannot inherit a live grant.
        result = {"available": False, "roots": [], "projects": {}, "search_roots": [], "unavailable_projects": {},
                  "supported_read": sorted(TEXT_EXTENSIONS), "metadata": "ordinary local files",
                  "can_write": False, "can_shell": False, "can_cloud": False, "authority": False,
                  "limits": {"seconds": SEARCH_SECONDS, "visited": MAX_VISITED,
                             "files": MAX_FILES, "characters": MAX_CHARS, "file_bytes": MAX_BYTES}}
        try:
            state = self._state(owner)
        except (ValueError, OSError, PermissionError, sqlite3.Error):
            result["reason"] = "The reviewed owner file grant is absent, invalid, or stale."
            return result
        result.update(available=True, roots=state["roots"], search_roots=state["search_roots"],
                      projects={alias: {"root": item["path"], "identity": item["identity"],
                                        "thread_id": item["thread_id"]}
                                for alias, item in state["projects"].items()},
                      unavailable_projects=state.get("unavailable_projects", {}),
                      policy_hash=state["policy_hash"], grant_id=state["grant_id"])
        return result

    @staticmethod
    def _scopes(state, project):
        scopes = [state["projects"][project]] if project is not None else state["search_roots"]
        result = []
        for item in scopes:
            record = {key: item[key] for key in ("path", "identity", "grant_root", "grant_identity")}
            if not any(root["path"] == record["path"] for root in result):
                result.append(record)
        return result

    def _resolve_path(self, state, project, requested, *, discovery=False):
        if requested is None:
            return None
        if requested.startswith(("/", "\\")) or re.match(r"^[A-Za-z]:", requested):
            return _absolute(requested)
        relative = _relative(requested)
        if project is not None and not discovery:
            return Path(state["projects"][project]["path"]) / relative
        # An unscoped basename has no unique base. Preserve it as a filename
        # query, including when files with that name exist in several roots.
        return None

    def _binding(self, owner, state, project, goal, path, filename_query=None):
        if project is not None and (not isinstance(project, str) or project not in state["projects"]):
            raise ValueError("This project has no reviewed owner file mapping.")
        mapped = state["projects"].get(project)
        file_root = _root_for(path, state["roots"]) if path is not None else None
        relative = _relative_to(path, file_root) if path is not None else None
        primary = mapped or file_root
        if filename_query is not None:
            filename_query = _relative(filename_query)
            if path is not None:
                raise ValueError("A request cannot mix a direct path and filename discovery.")
        body = {"version": 1, "owner": owner, "policy_hash": state["policy_hash"],
                "scope_hash": _hash(state), "grant_id": state["grant_id"],
                "project": project, "thread_id": mapped["thread_id"] if mapped else None,
                "root": primary["path"] if primary else None,
                "root_identity": primary["identity"] if primary else None,
                "file_root": file_root, "path": str(path) if path is not None else None,
                "relative_path": relative, "query": goal, "query_words": _words(filename_query or goal),
                "filename_query": filename_query,
                "search_roots": self._scopes(state, project), "source": state["source"]}
        return {**body, "binding_hash": _hash(body)}

    def bind(self, owner, project, goal, path=None):
        goal = _text(goal)
        state = self._state(owner)
        if isinstance(project, str) and project in state.get("unavailable_projects", {}):
            raise ValueError(state["unavailable_projects"][project])
        if project is not None and (not isinstance(project, str) or project not in state["projects"]):
            raise ValueError("This project has no reviewed owner file mapping.")
        requested = select_request_path(goal, path)
        discovery = path is None and bool(re.search(r"\b(?:find|search|locate|look for)\b", goal, re.I))
        direct = self._resolve_path(state, project, requested, discovery=discovery)
        filename_query = requested if requested is not None and direct is None else None
        result = self._binding(owner, state, project, goal, direct, filename_query)
        self.validate(owner, result)
        return result

    def validate(self, owner, binding):
        if type(binding) is not dict or len(_json_bytes(binding)) > MAX_BYTES:
            raise ValueError("A frozen owner file binding is required.")
        if type(binding.get("owner")) is not int or binding["owner"] != owner:
            raise PermissionError("This file binding belongs to a different owner.")
        state = self._state(owner)
        try:
            goal = _text(binding.get("query"))
            path = _absolute(binding["path"]) if binding.get("path") is not None else None
            expected = self._binding(owner, state, binding.get("project"), goal, path,
                                     binding.get("filename_query"))
        except (KeyError, TypeError, ValueError):
            raise ConflictError("The frozen file request is invalid or changed.") from None
        if _json_bytes(binding) != _json_bytes(expected):
            raise ConflictError("The file grant, request, or registered scope changed.")
        return expected

    def _discover(self, binding):
        candidates, visited, skipped, seen, reasons = [], 0, 0, set(), []
        scopes, words = binding["search_roots"], binding["query_words"]
        deadline, real_deadline = self.clock() + SEARCH_SECONDS, time.monotonic() + SEARCH_SECONDS

        def stop():
            if visited >= MAX_VISITED:
                return "visit_limit"
            if self.clock() >= deadline or time.monotonic() >= real_deadline:
                return "time_limit"
            if len(candidates) >= MAX_MATCHES:
                return "match_limit"
            return None

        if not words:
            return [], {"visited": 0, "skipped": 0, "reasons": ["no_filename_words"]}
        for priority, root in enumerate(scopes):
            pending = deque([""])
            while pending:
                reason = stop()
                if reason:
                    reasons.append(reason)
                    break
                relative = pending.popleft()
                parent = Path(root["path"]) / relative
                key = os.path.normcase(str(parent))
                if key in seen:
                    continue
                seen.add(key)
                try:
                    with _directory(root, relative) as (directory, descriptor):
                        with os.scandir(directory if descriptor is None else descriptor) as children:
                            while True:
                                reason = stop()
                                if reason:
                                    reasons.append(reason)
                                    break
                                try:
                                    child = next(children)
                                except StopIteration:
                                    break
                                visited += 1
                                try:
                                    child_relative = _relative(f"{relative}/{child.name}" if relative else child.name)
                                    path = directory / child.name
                                    info = _lstat(path, descriptor)
                                    is_directory = stat.S_ISDIR(info.st_mode)
                                    _ordinary(info, directory=is_directory)
                                    if is_directory:
                                        pending.append(child_relative)
                                        continue
                                    hits = [word for word in words if word in child.name.casefold()]
                                    filename_query = binding["filename_query"]
                                    if filename_query is not None:
                                        spelling = child_relative.casefold()
                                        query = filename_query.casefold()
                                        hits = [filename_query] if (spelling == query or
                                                                  spelling.endswith("/" + query)) else []
                                    if hits:
                                        candidates.append({"path": str(path), "relative_path": child_relative,
                                                           "bytes": info.st_size, "identity": file_identity(info),
                                                           "matched_words": hits, "root": root, "priority": priority})
                                except (OSError, ValueError):
                                    skipped += 1
                except ConflictError:
                    raise
                except (OSError, ValueError):
                    skipped += 1
            if reasons:
                break
        candidates.sort(key=lambda item: (item["priority"], -len(item["matched_words"]),
                                         not is_text_path(item["path"]), item["path"].casefold(), item["path"]))
        if skipped:
            reasons.append("excluded_or_unavailable_entries")
        return candidates, {"visited": visited, "skipped": skipped, "reasons": list(dict.fromkeys(reasons))}

    def _read(self, binding, root, path, limit, expected_identity=None):
        with _file(root, path) as (stream, info):
            if expected_identity and file_identity(info) != expected_identity:
                raise ConflictError("A discovered file was replaced before reading.")
            metadata = {"path": str(path), "identity": file_identity(info),
                        "bytes": info.st_size, "mtime_ns": info.st_mtime_ns}
            item = {**metadata, "text": None, "sha256": None, "content_read": False,
                    "status": "metadata_only", "truncated": False, "bytes_read": 0,
                    "total_chars": 0, "returned_chars": 0, "omitted_chars": 0}
            if not is_text_path(path):
                item["reason"] = "unsupported_text_type"
            elif info.st_size > MAX_BYTES:
                item["reason"] = "text_byte_limit"
                item["truncated"] = True
            else:
                data = stream.read(MAX_BYTES + 1)
                item["bytes_read"] = len(data)
                item["content_read"] = True
                if len(data) > MAX_BYTES:
                    raise ConflictError("The text file grew beyond its admitted bound.")
                try:
                    text = text_from_bytes(data)  # Filter the whole file before taking an excerpt.
                except ValueError:
                    try:
                        sensitive = contains_secret(data.decode("utf-8-sig"))
                    except UnicodeError:
                        sensitive = False
                    item["reason"] = "excluded_text" if sensitive else "binary_or_unsupported_encoding"
                    item["status"] = "excluded" if sensitive else "metadata_only"
                else:
                    item.update(text=text[:limit], sha256=digest(data), content_read=True, status="read",
                                total_chars=len(text), returned_chars=min(len(text), limit),
                                omitted_chars=max(0, len(text) - limit), truncated=len(text) > limit)
            receipt = {"operation": "local_file_read" if item["content_read"] else "local_file_metadata",
                       "state": item["status"], "path": str(path), "identity": metadata["identity"],
                       "sha256": item["sha256"], "metadata_sha256": _hash(metadata),
                       "bytes": info.st_size, "bytes_read": item["bytes_read"],
                       "returned_chars": item["returned_chars"], "truncated": item["truncated"],
                       "policy_hash": binding["policy_hash"], "grant_id": binding["grant_id"],
                       "observed_at": self.clock(), "authority": False}
            item["receipt"] = receipt
        return item

    def inspect_request(self, owner, binding, goal):
        binding = self.validate(owner, binding)
        if _text(goal) != binding["query"]:
            raise ConflictError("The goal differs from the frozen file request.")
        direct = binding["path"] is not None
        if direct:
            candidates = [{"path": binding["path"], "root": binding["file_root"]}]
            search = {"visited": 0, "skipped": 0, "reasons": []}
        else:
            candidates, search = self._discover(binding)
        # Freeze candidates before reading any source. Source text cannot add
        # paths, change the query/project, invoke tools, or grant authority.
        selected = candidates[:MAX_FILES]
        matches = [{key: value for key, value in item.items() if key not in {"root", "priority"}}
                   for item in candidates] if not direct else []
        files, receipts, remaining = [], [], MAX_CHARS
        reasons = list(search["reasons"])
        if len(candidates) > MAX_FILES:
            reasons.append("file_limit")
        for index, candidate in enumerate(selected):
            self.validate(owner, binding)
            path = Path(candidate["path"])
            try:
                item = self._read(binding, candidate["root"], path,
                                  remaining // (len(selected) - index), candidate.get("identity"))
            except ConflictError:
                raise
            except (OSError, ValueError):
                reasons.append("unavailable_file")
                receipts.append({"operation": "local_file_read", "state": "unavailable",
                                 "path": str(path), "sha256": None, "authority": False,
                                 "policy_hash": binding["policy_hash"], "observed_at": self.clock()})
                continue
            remaining -= item["returned_chars"]
            files.append(item)
            receipts.append(item["receipt"])
            if item["truncated"]:
                reasons.append("text_limit")
            if item["status"] == "excluded":
                reasons.append("excluded_text")
        # Re-pairing, editing grant bytes, replacing roots or rerouting a thread
        # during a read discards every staged result before Main can see it.
        self.validate(owner, binding)
        read_count = sum(item["status"] == "read" for item in files)
        metadata_count = sum(item["status"] == "metadata_only" for item in files)
        ambiguous = binding["filename_query"] is not None and len(candidates) > 1
        summary = f"Read {read_count} text file(s); returned metadata for {metadata_count} file(s)."
        if ambiguous:
            summary += f" Found {len(candidates)} matching paths; no unique file was selected."
        if not direct:
            summary += f" Bounded filename search visited {search['visited']} entries in the configured scopes."
        if reasons:
            summary += " Some results were limited, excluded, or unavailable."
        return {"summary": summary, "files": files, "matches": matches, "ambiguous": ambiguous,
                "truncated": bool(reasons), "truncation_reasons": list(dict.fromkeys(reasons)),
                "source_receipts": receipts, "grant_receipt": binding["source"],
                "search": {**search, "mode": "explicit_path" if direct else "filenames",
                           "roots": [root["path"] for root in binding["search_roots"]] if not direct else [],
                           "matched": len(matches), "selected": len(selected),
                           "max_visited": MAX_VISITED, "seconds": SEARCH_SECONDS, "exhaustive": False},
                "authority": False, "instructions_are_data": True}
