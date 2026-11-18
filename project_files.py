"""Bounded local text-file access with Windows handle-based scope checks."""
from __future__ import annotations

from contextlib import contextmanager, ExitStack
import ctypes
import hashlib
import os
from pathlib import Path, PurePosixPath
import re
import stat
import time

from companion import ConflictError
from memory_policy import contains_secret


MAX_BYTES = 192 * 1024
MAX_ENTRIES = 160
MAX_VISITED = 2400
MAX_MATCHES = 80
MAX_SEARCH_BYTES = 8 * 1024 * 1024
TEXT_EXTENSIONS = {
    ".md", ".txt", ".py", ".js", ".mjs", ".cjs", ".ts", ".tsx", ".jsx",
    ".css", ".html", ".json", ".toml", ".yaml", ".yml", ".svg", ".csv",
    ".xml", ".ini", ".cfg", ".sql", ".rs", ".go", ".java", ".c", ".h",
}
EXCLUDED = {
    ".state", ".test-temp", ".git", ".svn", ".hg", ".aws", ".ssh", ".codex",
    "node_modules", "__pycache__", ".venv", "venv", "reference", "imports",
    "credentials", "secrets", "private", "dist", "build", "coverage",
}
PROTECTED_NAMES = {
    "settings.json", "tasks.json", "project-context.json", "speech-config.json",
    "agents.md", "skill.md", "coordinator.md", "owner_requirements.md",
    "project-report-catalog.json", "project-progress-sources.json",
    "owner-file-access.json",
    "codex-session-source.json",
}
SENSITIVE_NAME = re.compile(
    r"(?:^|[._ -])(?:credentials?|passwords?|secrets?|tokens?|private[-_ ]?keys?)(?:[._ -]|$)", re.I
)
DEVICE_NAME = re.compile(r"^(?:con|prn|aux|nul|com[0-9¹²³]|lpt[0-9¹²³])(?:\.|$)", re.I)


def digest(data):
    return hashlib.sha256(data).hexdigest()


def file_identity(info):
    return f"{info.st_dev}:{info.st_ino}"


def relative_path(value, *, directory=False):
    if not isinstance(value, str) or len(value) > 240:
        raise ValueError("Choose a relative path inside this project.")
    value = value.replace("\\", "/")
    if not value and directory:
        return ""
    parts = value.split("/")
    if (not value or value.startswith("/") or any(
        not part or part in (".", "..") or part.endswith((" ", "."))
        or re.search(r'[:*?"<>|\x00-\x1f\x7f\u202a-\u202e\u2066-\u2069]', part)
        or DEVICE_NAME.match(part) or part.startswith(".") or "~" in part
        or part.casefold() in EXCLUDED or SENSITIVE_NAME.search(part)
        for part in parts
    )):
        raise ValueError("This path is outside the available project files.")
    return PurePosixPath(*parts).as_posix()


def is_text_path(path):
    return Path(path).suffix.casefold() in TEXT_EXTENSIONS


def text_from_bytes(data):
    if len(data) > MAX_BYTES:
        raise ValueError("Choose a UTF-8 text file of at most 192 KB.")
    try:
        text = data.decode("utf-8-sig")
    except UnicodeError:
        raise ValueError("Only UTF-8 text files are available in this tool.") from None
    if "\x00" in text or any(ord(char) < 32 and char not in "\r\n\t" for char in text):
        raise ValueError("This file contains binary or control data.")
    if contains_secret(text):
        raise ValueError("This file contains credential-like content and is excluded from project tools.")
    return text.replace("\r\n", "\n")


def bytes_from_text(text, original):
    if not isinstance(text, str) or len(text) > MAX_BYTES or "\x00" in text:
        raise ValueError("Use a bounded UTF-8 text change.")
    # Browser textareas normalize CRLF. Preserve the original file's convention
    # and UTF-8 BOM; never silently rewrite every line just for line endings.
    source = original.decode("utf-8-sig")
    text = text.replace("\r\n", "\n")
    if "\r\n" in source and "\n" not in source.replace("\r\n", ""):
        text = text.replace("\n", "\r\n")
    try:
        data = (b"\xef\xbb\xbf" if original.startswith(b"\xef\xbb\xbf") else b"") + text.encode("utf-8")
    except UnicodeError:
        raise ValueError("Use valid UTF-8 text.") from None
    text_from_bytes(data)
    return data


if os.name == "nt":
    import msvcrt
    from ctypes import wintypes

    class _FileInfo(ctypes.Structure):
        _fields_ = [
            ("attributes", wintypes.DWORD), ("creation", wintypes.FILETIME),
            ("access", wintypes.FILETIME), ("write", wintypes.FILETIME),
            ("volume", wintypes.DWORD), ("size_high", wintypes.DWORD),
            ("size_low", wintypes.DWORD), ("links", wintypes.DWORD),
            ("index_high", wintypes.DWORD), ("index_low", wintypes.DWORD),
        ]

    _kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    _kernel.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                                   ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p]
    _kernel.CreateFileW.restype = wintypes.HANDLE
    _kernel.GetFileInformationByHandle.argtypes = [wintypes.HANDLE, ctypes.POINTER(_FileInfo)]
    _kernel.GetFileInformationByHandle.restype = wintypes.BOOL
    _kernel.GetFinalPathNameByHandleW.argtypes = [wintypes.HANDLE, wintypes.LPWSTR, wintypes.DWORD, wintypes.DWORD]
    _kernel.GetFinalPathNameByHandleW.restype = wintypes.DWORD
    _kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    _kernel.CloseHandle.restype = wintypes.BOOL

    @contextmanager
    def _windows_handle(path, *, directory, writing=False, appendable_read=False, replaceable_read=False):
        # FILE_LIST_DIRECTORY participates in sharing checks; an access=0
        # metadata handle does not prevent renaming its own directory. Include
        # FILE_READ_ATTRIBUTES for handle-based identity checks and omit
        # FILE_SHARE_DELETE to pin each ancestor before opening a child.
        # https://learn.microsoft.com/windows/win32/api/fileapi/nf-fileapi-createfilew
        access = 0x00000081 if directory else 0x80000000 | (0x40000000 if writing else 0)
        if appendable_read and (directory or writing):
            raise ValueError("Appendable sharing is only available for a read-only event stream.")
        share = 3 if directory or appendable_read else 0 if writing else 1
        if replaceable_read:
            if directory or writing or not appendable_read:
                raise ValueError("Replaceable sharing requires a read-only snapshot stream.")
            share |= 4
        flags = 0x00200000 | (0x02000000 if directory else 0)  # OPEN_REPARSE_POINT / BACKUP_SEMANTICS
        handle = _kernel.CreateFileW(str(path), access, share, None, 3, flags, None)
        if handle == ctypes.c_void_p(-1).value:
            raise ValueError("This project file is unavailable or open for writing. Close its editor and refresh.")
        try:
            info = _FileInfo()
            final = ctypes.create_unicode_buffer(32768)
            count = _kernel.GetFinalPathNameByHandleW(handle, final, len(final), 0)
            if not _kernel.GetFileInformationByHandle(handle, ctypes.byref(info)) or not 0 < count < len(final):
                raise ValueError("The project file identity could not be verified.")
            final_path = final.value
            if final_path.startswith("\\\\?\\"):
                final_path = final_path[4:]
            if (info.attributes & 0x400 or bool(info.attributes & 0x10) != directory
                    or os.path.normcase(final_path) != os.path.normcase(str(path))
                    or (not directory and info.links != 1)):
                raise ValueError("Links, reparse points and substituted project paths are excluded.")
            if directory:
                descriptor = msvcrt.open_osfhandle(handle, os.O_BINARY | os.O_RDONLY)
                handle = None  # The descriptor owns the handle from here.
                try:
                    # Use Python's identity representation, including wider
                    # Windows file IDs, exactly as the stored path-stat IDs do.
                    yield os.fstat(descriptor)
                finally:
                    os.close(descriptor)
            else:
                descriptor = msvcrt.open_osfhandle(handle, os.O_BINARY | (os.O_RDWR if writing else os.O_RDONLY))
                handle = None  # fdopen owns the OS handle from here.
                with os.fdopen(descriptor, "r+b" if writing else "rb") as stream:
                    yield stream
        finally:
            if handle is not None:
                _kernel.CloseHandle(handle)


class ProjectFiles:
    def __init__(self, root, expected_identity=None):
        root = Path(root)
        if not root.is_absolute() or str(root).startswith(("\\\\", "//")):
            raise ValueError("A registered local project folder is required.")
        self.root = Path(os.path.abspath(root))
        self.expected_identity = expected_identity

    @contextmanager
    def open(self, relative="", *, directory=False, writing=False):
        with self._open(relative, directory=directory, writing=writing) as (resource, _):
            yield resource

    @contextmanager
    def _open(self, relative="", *, directory=False, writing=False):
        relative = relative_path(relative, directory=directory)
        path = self.root / relative
        if not directory and not is_text_path(path):
            raise ValueError("Choose a supported project text file.")
        if writing and os.name != "nt":
            raise ValueError("Reviewed file writes require the Windows host.")
        with ExitStack() as stack:
            root_identity = None
            parent = path if directory else path.parent
            # Start at the drive root, rather than resolving through a possibly
            # substituted project root before checking its ancestry.
            current = Path(parent.anchor)
            directories = [current]
            for component in parent.parts[1:]:
                current = current / component
                directories.append(current)
            for current in directories:
                if os.name == "nt":
                    info = stack.enter_context(_windows_handle(current, directory=True))
                else:
                    info = current.lstat()
                    if not stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
                        raise ValueError("The project folder cannot traverse links or reparse points.")
                if current == self.root:
                    root_identity = file_identity(info)
                    if self.expected_identity and root_identity != self.expected_identity:
                        raise ConflictError("The registered project folder was replaced. Its old access no longer applies.")
            if directory:
                yield path, root_identity
            elif os.name == "nt":
                yield stack.enter_context(_windows_handle(path, directory=False, writing=writing)), root_identity
            else:
                descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
                stream = stack.enter_context(os.fdopen(descriptor, "rb"))
                info = os.fstat(stream.fileno())
                if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                    raise ValueError("Only regular, unlinked project files are available.")
                yield stream, root_identity

    def identity(self):
        with self._open(directory=True) as (_, root_identity):
            return root_identity

    def read(self, path):
        with self.open(path) as stream:
            info = os.fstat(stream.fileno())
            if info.st_size > MAX_BYTES:
                raise ValueError("Choose a UTF-8 text file of at most 192 KB.")
            data = stream.read(MAX_BYTES + 1)
            text = text_from_bytes(data)
            return {"data": data, "text": text, "sha256": digest(data), "identity": file_identity(info)}

    def exists(self, path):
        relative = relative_path(path)
        parent, name = PurePosixPath(relative).parent.as_posix(), PurePosixPath(relative).name
        parent = "" if parent == "." else parent
        with self.open(parent, directory=True) as directory:
            with os.scandir(directory) as children:
                return any(child.name.casefold() == name.casefold() for child in children)

    def inspect(self, path=""):
        entries, truncated = [], False
        with self.open(path, directory=True) as directory:
            with os.scandir(directory) as children:
                for visited, child in enumerate(children, 1):
                    if visited > MAX_VISITED or len(entries) >= MAX_ENTRIES:
                        truncated = True
                        break
                    relative = f"{path}/{child.name}" if path else child.name
                    try:
                        relative = relative_path(relative)
                        # Windows DirEntry.stat may report st_nlink/st_ino as
                        # zero. lstat supplies the identity/link count needed by
                        # this admission check without following the entry.
                        info = Path(child.path).lstat()
                        if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
                            continue
                        kind = "directory" if stat.S_ISDIR(info.st_mode) else "file"
                        if kind == "file" and (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1
                                              or not is_text_path(relative) or info.st_size > MAX_BYTES):
                            continue
                        entries.append({"name": child.name, "path": relative, "kind": kind, "size": info.st_size})
                    except (OSError, ValueError):
                        continue
        entries.sort(key=lambda item: (item["kind"] != "directory", item["name"].casefold()))
        return {"path": path, "entries": entries, "truncated": truncated}

    def search(self, query, path=""):
        if not isinstance(query, str) or not 1 <= len(query.strip()) <= 200 or "\n" in query:
            raise ValueError("Enter a literal search phrase of 1–200 characters.")
        if contains_secret(query):
            raise ValueError("Credential-like values cannot be searched with this tool.")
        path = relative_path(path, directory=True)
        pending, matches, scanned, visited, total = [path], [], 0, 0, 0
        truncated, deadline = False, time.monotonic() + 3
        needle = query.casefold()
        while pending:
            if visited >= MAX_VISITED or time.monotonic() > deadline or total >= MAX_SEARCH_BYTES:
                truncated = True
                break
            parent = pending.pop()
            try:
                listing = self.inspect(parent)
            except (OSError, ValueError):
                continue
            truncated |= listing["truncated"]
            for entry in listing["entries"]:
                visited += 1
                if visited > MAX_VISITED or time.monotonic() > deadline or total >= MAX_SEARCH_BYTES:
                    truncated = True
                    break
                if entry["kind"] == "directory":
                    pending.append(entry["path"])
                    continue
                try:
                    content = self.read(entry["path"])
                except (OSError, ValueError):
                    continue
                scanned += 1
                total += len(content["data"])
                for number, line in enumerate(content["text"].splitlines(), 1):
                    if needle in line.casefold():
                        matches.append({"path": entry["path"], "line": number, "text": line[:300]})
                        if len(matches) >= MAX_MATCHES:
                            return {"matches": matches, "files_scanned": scanned, "truncated": True}
        return {"matches": matches, "files_scanned": scanned, "truncated": truncated}

    @staticmethod
    def write_checked(stream, content):
        stream.seek(0)
        stream.write(content)
        stream.truncate()
        stream.flush()
        os.fsync(stream.fileno())
        stream.seek(0)
        if stream.read(MAX_BYTES + 1) != content:
            raise OSError("Written file verification failed.")
