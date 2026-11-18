"""Provision only Vega's dedicated .speaker-runtime and pinned local model files.

Explicit setup:
  py -3.13 -B setup_speaker_runtime.py --provision
Offline check (no downloads, installs, or model inference):
  py -3.13 -B setup_speaker_runtime.py --verify-only

No global pip, Microsoft installers, credentials, audio devices, or host changes.
The normal JSON-lines worker never imports this network-capable setup module.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import platform
import re
import struct
import subprocess
import sys
import tempfile
import time
import urllib.parse
import urllib.request
import venv
import zipfile

sys.dont_write_bytecode = True

ROOT = Path(__file__).resolve().parent
RUNTIME = ROOT / ".speaker-runtime"
MODELS = ROOT / ".speaker-models"
NOTICES = ROOT / "third-party" / "speaker-models"
MANIFEST = ROOT / "speaker-models.json"
REQUIREMENTS = ROOT / "speaker-runtime-requirements.txt"
OWNER_FILE = RUNTIME / "vega-speaker-runtime.json"
OWNER_KIND = "vega-dedicated-speaker-runtime-v1"
NETWORK_HOSTS = {"pypi.org", "files.pythonhosted.org", "huggingface.co",
                 "creativecommons.org", "raw.githubusercontent.com"}


class SetupError(RuntimeError):
    pass


def emit(stage: str, **values) -> None:
    print(json.dumps({"stage": stage, **values}, allow_nan=False), flush=True)


def sha256(path: Path) -> str:
    with path.open("rb") as source:
        return hashlib.file_digest(source, "sha256").hexdigest()


def scoped(path: Path) -> Path:
    """Check every write target, including existing junctions, before touching it."""
    path = path.absolute()
    allowed = (RUNTIME, MODELS, NOTICES)
    if not any(path == base or path.is_relative_to(base) for base in allowed):
        raise SetupError("Write target is outside the dedicated speaker directories.")
    if not path.resolve().is_relative_to(ROOT):
        raise SetupError("A speaker directory resolves outside standalone-host.")
    current = path
    while current != ROOT:
        if current.is_symlink() or (hasattr(current, "is_junction") and current.is_junction()):
            raise SetupError("Speaker setup does not follow symlinks or junctions.")
        current = current.parent
    return path


def write_json(path: Path, value) -> None:
    path = scoped(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, allow_nan=False) + "\n", encoding="utf-8")


def load_manifest() -> dict:
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    if manifest.get("schema_version") != 1 or set(manifest.get("models", {})) != {
        "embedding", "segmentation"
    }:
        raise SetupError("Unsupported speaker manifest.")
    artifacts = list(manifest["models"].values()) + manifest["wheels"] + manifest["notices"]
    for item in artifacts:
        filename = item.get("filename", "")
        if not filename or re.fullmatch(r"[A-Za-z0-9_.+-]+", filename) is None:
            raise SetupError("An artifact filename is invalid.")
        if type(item.get("size")) is not int or not 0 < item["size"] < 200_000_000:
            raise SetupError("An artifact size is invalid.")
        if re.fullmatch(r"[0-9a-f]{64}", item.get("sha256", "")) is None:
            raise SetupError("An artifact hash is invalid.")
        check_url(item["url"])
    wanted = {
        f'{item["name"]}=={item["version"]} --hash=sha256:{item["sha256"]}'
        for item in manifest["wheels"]
    }
    actual = {
        line.strip() for line in REQUIREMENTS.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    }
    if actual != wanted or len(actual) != 6:
        raise SetupError("Requirements and the six pinned wheels disagree.")
    return manifest


def check_url(url: str) -> None:
    parsed = urllib.parse.urlsplit(url)
    if (parsed.scheme != "https" or parsed.hostname not in NETWORK_HOSTS
            or parsed.username is not None or parsed.password is not None):
        raise SetupError("Unexpected artifact or metadata URL.")


def request(url: str):
    check_url(url)
    req = urllib.request.Request(url, headers={
        "User-Agent": "VegaSpeakerSetup/1.0",
        "Accept-Encoding": "identity",
    })
    # HF may redirect public downloads to its CDN. Hashes, sizes and TLS remain
    # mandatory; never record signed redirect URLs in logs or receipts.
    response = urllib.request.urlopen(req, timeout=60)
    if urllib.parse.urlsplit(response.geturl()).scheme != "https":
        response.close()
        raise SetupError("An artifact redirected to a non-HTTPS URL.")
    return response


def metadata_json(url: str) -> dict:
    with request(url) as response:
        body = response.read(4_000_001)
    if len(body) > 4_000_000:
        raise SetupError("Metadata response exceeds its bound.")
    return json.loads(body)


def independently_verify_metadata(manifest: dict, offline: bool) -> dict:
    cache = scoped(RUNTIME / "metadata" / "verified-metadata.json")
    manifest_hash = sha256(MANIFEST)
    if offline:
        if not cache.is_file():
            raise SetupError("Offline provisioning requires previously verified metadata.")
        evidence = json.loads(cache.read_text(encoding="utf-8"))
        if evidence.get("manifest_sha256") != manifest_hash:
            raise SetupError("Cached metadata belongs to a different manifest.")
        return evidence
    verified = []
    for item in manifest["wheels"]:
        url = f'https://pypi.org/pypi/{item["name"]}/{item["version"]}/json'
        meta = metadata_json(url)
        candidates = [asset for asset in meta["urls"] if asset["filename"] == item["filename"]]
        if len(candidates) != 1:
            raise SetupError(f'Pinned wheel is unavailable: {item["filename"]}')
        actual = candidates[0]
        if (actual["size"] != item["size"] or actual["digests"]["sha256"] != item["sha256"]
                or actual["url"] != item["url"] or actual.get("yanked")
                or meta["info"]["version"] != item["version"]):
            raise SetupError(f'Published wheel metadata differs: {item["filename"]}')
        verified.append({"filename": item["filename"], "metadata_url": url,
                         "sha256": item["sha256"], "size": item["size"],
                         "requires_dist": meta["info"].get("requires_dist")})
        emit("metadata_verified", filename=item["filename"])
    for item in manifest["models"].values():
        meta = metadata_json(item["metadata_url"])
        candidates = [asset for asset in meta["siblings"]
                      if asset["rfilename"] == item["source_filename"]]
        if len(candidates) != 1:
            raise SetupError(f'Pinned model is unavailable: {item["filename"]}')
        actual = candidates[0]
        if (meta["sha"] != item["revision"] or actual["size"] != item["size"]
                or actual["lfs"]["sha256"] != item["sha256"]):
            raise SetupError(f'Published model metadata differs: {item["filename"]}')
        verified.append({"filename": item["filename"], "metadata_url": item["metadata_url"],
                         "revision": item["revision"], "sha256": item["sha256"],
                         "size": item["size"]})
        emit("metadata_verified", filename=item["filename"])
    return {"manifest_sha256": manifest_hash, "verified_at_unix": time.time(),
            "artifacts": verified}


def verify_artifact(path: Path, item: dict) -> dict:
    path = scoped(path)
    if not path.is_file() or path.stat().st_size != item["size"]:
        raise SetupError(f'Artifact is missing or has the wrong size: {item["filename"]}')
    actual = sha256(path)
    if actual != item["sha256"]:
        raise SetupError(f'Artifact SHA-256 mismatch: {item["filename"]}')
    return {"filename": item["filename"], "sha256": actual,
            "size": item["size"], "url": item["url"]}


def acquire(item: dict, directory: Path, offline: bool) -> dict:
    destination = scoped(directory / item["filename"])
    directory = scoped(directory)
    directory.mkdir(parents=True, exist_ok=True)
    if destination.exists():
        # Corrupt existing files are preserved for inspection, never overwritten.
        result = verify_artifact(destination, item)
        emit("cached_hash_verified", filename=item["filename"])
        return result
    if offline:
        raise SetupError(f'Artifact is absent from the offline cache: {item["filename"]}')
    partial = scoped(destination.with_name(destination.name + ".part"))
    digest = hashlib.sha256()
    size = 0
    emit("downloading", filename=item["filename"], expected_bytes=item["size"])
    with request(item["url"]) as response, partial.open("wb") as output:
        while block := response.read(1024 * 1024):
            size += len(block)
            if size > item["size"]:
                raise SetupError(f'Download exceeded its pinned size: {item["filename"]}')
            digest.update(block)
            output.write(block)
    if size != item["size"] or digest.hexdigest() != item["sha256"]:
        raise SetupError(f'Download failed its pinned hash/size: {item["filename"]}')
    scoped(partial).replace(scoped(destination))
    emit("download_hash_verified", filename=item["filename"], bytes=size,
         sha256=digest.hexdigest())
    return verify_artifact(destination, item)


def ensure_owned_directory(manifest: dict) -> None:
    scoped(RUNTIME)
    if RUNTIME.exists():
        if not OWNER_FILE.is_file():
            raise SetupError("Existing .speaker-runtime is unowned; refusing to reuse it.")
        owner = json.loads(OWNER_FILE.read_text(encoding="utf-8"))
        if owner.get("kind") != OWNER_KIND or owner.get("root") != str(ROOT):
            raise SetupError("Existing .speaker-runtime does not match this setup.")
        if owner.get("manifest_sha256") != sha256(MANIFEST):
            # Documentation/license-only changes may resume an interrupted owned
            # setup. Binary pins must still match its original metadata receipt.
            old_path = scoped(RUNTIME / "metadata" / "verified-metadata.json")
            if not old_path.is_file():
                raise SetupError("Cannot reconcile the existing setup manifest.")
            old = json.loads(old_path.read_text(encoding="utf-8"))
            prior = {(item["filename"], item["size"], item["sha256"])
                     for item in old["artifacts"]}
            expected = {(item["filename"], item["size"], item["sha256"])
                        for item in manifest["wheels"] + list(manifest["models"].values())}
            if prior != expected:
                raise SetupError("Binary pins changed; refusing to reuse an existing runtime.")
            write_json(OWNER_FILE, {**owner, "manifest_sha256": sha256(MANIFEST)})
    else:
        RUNTIME.mkdir()
        write_json(OWNER_FILE, {"kind": OWNER_KIND, "root": str(ROOT),
                              "manifest_sha256": sha256(MANIFEST), "state": "provisioning"})
    for directory in (RUNTIME / "tmp", RUNTIME / "wheelhouse", MODELS, NOTICES):
        scoped(directory).mkdir(parents=True, exist_ok=True)


def local_environment() -> dict:
    env = {key: value for key, value in os.environ.items()
           if not key.upper().startswith(("PYTHON", "PIP_"))}
    env.update({
        "TEMP": str(RUNTIME / "tmp"), "TMP": str(RUNTIME / "tmp"),
        "PIP_CONFIG_FILE": os.devnull, "PIP_DISABLE_PIP_VERSION_CHECK": "1",
        "PIP_NO_CACHE_DIR": "1", "PYTHONDONTWRITEBYTECODE": "1",
    })
    return env


def run_local_python(arguments: list[str], timeout: int = 240) -> str:
    interpreter = scoped(RUNTIME / "Scripts" / "python.exe")
    result = subprocess.run(
        [str(interpreter), "-I", "-B", *arguments], cwd=RUNTIME,
        env=local_environment(), capture_output=True, text=True, encoding="utf-8",
        errors="replace", timeout=timeout,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    if result.returncode:
        # Pip is offline here. Bounded diagnostics are useful for missing DLLs.
        raise SetupError((result.stdout + "\n" + result.stderr)[-5000:].strip())
    return result.stdout.strip()


def installed_versions(manifest: dict) -> dict:
    names = [item["name"] for item in manifest["wheels"]]
    code = (
        "import importlib.metadata as m,json,sys;"
        f"assert sys.prefix=={str(RUNTIME)!r};"
        "assert sys.prefix!=sys.base_prefix;"
        f"print(json.dumps({{n:m.version(n) for n in {names!r}}}))"
    )
    actual = json.loads(run_local_python(["-c", code]))
    expected = {item["name"]: item["version"] for item in manifest["wheels"]}
    if actual != expected:
        raise SetupError("Installed distribution versions differ from the manifest.")
    return actual


def copy_runtime_notices(manifest: dict) -> list[dict]:
    records = []
    for wheel in manifest["wheels"]:
        wheel_path = RUNTIME / "wheelhouse" / wheel["filename"]
        verify_artifact(wheel_path, wheel)
        count = 0
        with zipfile.ZipFile(wheel_path) as archive:
            for info in archive.infolist():
                path = PurePosixPath(info.filename)
                basename = path.name.upper()
                if info.is_dir() or not any(word in basename for word in
                                           ("LICENSE", "LICENCE", "COPYING", "NOTICE")):
                    continue
                if path.is_absolute() or ".." in path.parts or info.file_size > 4_000_000:
                    raise SetupError("Unexpected license path/size in a verified wheel.")
                destination = scoped(
                    NOTICES / "runtime" / f'{wheel["name"]}-{wheel["version"]}'
                    / Path(*path.parts)
                )
                destination.parent.mkdir(parents=True, exist_ok=True)
                body = archive.read(info)
                destination.write_bytes(body)
                records.append({
                    "package": wheel["name"], "version": wheel["version"],
                    "wheel_sha256": wheel["sha256"], "source_path": info.filename,
                    "notice_path": str(destination.relative_to(NOTICES)).replace("\\", "/"),
                    "sha256": hashlib.sha256(body).hexdigest(),
                })
                count += 1
        if not count:
            alternatives = [item for item in manifest["notices"]
                            if item.get("package") == wheel["name"]
                            and item.get("version") == wheel["version"]]
            if len(alternatives) != 1:
                raise SetupError(f'No license notices found in {wheel["filename"]}')
            notice = alternatives[0]
            verify_artifact(NOTICES / notice["filename"], notice)
            records.append({
                "package": wheel["name"], "version": wheel["version"],
                "wheel_sha256": wheel["sha256"], "source_url": notice["url"],
                "notice_path": notice["filename"], "sha256": notice["sha256"],
                "reason": "Wheel omits license text; copied from pinned upstream source.",
            })
    write_json(NOTICES / "runtime-notices.json", records)
    return records


def verify_only(manifest: dict) -> None:
    if not OWNER_FILE.is_file():
        raise SetupError("Dedicated speaker runtime has not been provisioned.")
    owner = json.loads(OWNER_FILE.read_text(encoding="utf-8"))
    if owner.get("kind") != OWNER_KIND or owner.get("manifest_sha256") != sha256(MANIFEST):
        raise SetupError("Dedicated runtime ownership/manifest check failed.")
    evidence = []
    for item in manifest["wheels"]:
        evidence.append(verify_artifact(RUNTIME / "wheelhouse" / item["filename"], item))
    for item in manifest["models"].values():
        evidence.append(verify_artifact(MODELS / item["filename"], item))
    for item in manifest["notices"]:
        verify_artifact(NOTICES / item["filename"], item)
    emit("verified_offline", interpreter=str(RUNTIME / "Scripts" / "python.exe"),
         versions=installed_versions(manifest), artifacts=evidence)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--provision", action="store_true")
    mode.add_argument("--verify-only", action="store_true")
    parser.add_argument("--offline", action="store_true",
                        help="Use only a previously verified local download cache.")
    args = parser.parse_args()
    try:
        if (sys.version_info[:2] != (3, 13) or platform.system() != "Windows"
                or platform.machine().upper() not in {"AMD64", "X86_64"}
                or struct.calcsize("P") != 8):
            raise SetupError("Use ordinary CPython 3.13 on Windows x64.")
        manifest = load_manifest()
        if args.verify_only:
            verify_only(manifest)
            return 0
        # Metadata is independently checked BEFORE creating/installing the venv.
        evidence = independently_verify_metadata(manifest, args.offline)
        ensure_owned_directory(manifest)
        write_json(RUNTIME / "metadata" / "verified-metadata.json", evidence)
        receipts = []
        for item in manifest["wheels"]:
            receipts.append(acquire(item, RUNTIME / "wheelhouse", args.offline))
        for item in manifest["models"].values():
            receipts.append(acquire(item, MODELS, args.offline))
        for item in manifest["notices"]:
            acquire(item, NOTICES, args.offline)
        license_records = copy_runtime_notices(manifest)
        interpreter = scoped(RUNTIME / "Scripts" / "python.exe")
        if not interpreter.is_file():
            emit("creating_dedicated_venv", interpreter=str(interpreter))
            os.environ["TEMP"] = os.environ["TMP"] = str(RUNTIME / "tmp")
            tempfile.tempdir = str(RUNTIME / "tmp")
            venv.EnvBuilder(with_pip=True, system_site_packages=False,
                            clear=False, upgrade_deps=False).create(RUNTIME)
        emit("installing_verified_wheels", network=False, global_python=False)
        install_output = run_local_python([
            "-m", "pip", "--require-virtualenv", "install",
            "--no-index", "--no-deps", "--no-cache-dir", "--no-compile",
            "--only-binary=:all:", "--require-hashes",
            "--find-links", str(RUNTIME / "wheelhouse"), "-r", str(REQUIREMENTS),
        ])
        scoped(RUNTIME / "install-output.txt").write_text(install_output + "\n",
                                                         encoding="utf-8")
        versions = installed_versions(manifest)
        check_output = run_local_python(["-m", "pip", "check"])
        # Import smoke is local only. It diagnoses a missing VC++ runtime without
        # attempting to fix Windows or installing a Microsoft redistributable.
        import_output = run_local_python(["-c",
            "import onnxruntime as o,kaldi_native_fbank,numpy;"
            "o.disable_telemetry_events();"
            "assert 'CPUExecutionProvider' in o.get_available_providers();"
            "print('CPU imports available')"])
        receipt = {
            "ready_for_model_smoke": True, "interpreter": str(interpreter),
            "python": sys.version, "platform": platform.platform(),
            "manifest_sha256": sha256(MANIFEST),
            "requirements_sha256": sha256(REQUIREMENTS),
            "verified_at_unix": time.time(), "artifacts": receipts,
            "versions": versions, "pip_check": check_output,
            "import_check": import_output, "runtime_notice_count": len(license_records),
            "synthetic_or_owner_recognition_tested": False,
        }
        write_json(RUNTIME / "setup-receipt.json", receipt)
        write_json(OWNER_FILE, {"kind": OWNER_KIND, "root": str(ROOT),
                              "manifest_sha256": sha256(MANIFEST), "state": "installed"})
        emit("provisioned", interpreter=str(interpreter), versions=versions,
             pip_check=check_output, receipt=str(RUNTIME / "setup-receipt.json"))
        return 0
    except Exception as error:
        emit("blocked", error=type(error).__name__, message=str(error)[:5000])
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
