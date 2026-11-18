"""Consented, local acoustic personalization. Never authentication or approval.

Every model window and complete-recording reference must score >= .70 raw
cosine against a pooled owner prototype. Windows must match their own recording
reference (>= .80); main enrollment retains its >= .82 consistency checks.
Each enrollment clip needs >= 6 voiced seconds. These are engineering rules,
not calibrated probabilities. No playback/liveness detector is claimed.
"""
from __future__ import annotations

import base64
import contextlib
import copy
import hashlib
import json
import math
import os
from pathlib import Path
import queue
import re
import subprocess
import threading
import time
import uuid

from companion import ConflictError
from relay import protect
from voice_diagnostics import VoiceDiagnostics


ROOT = Path(__file__).resolve().parent
SAMPLE_RATE = 16000
DIMENSIONS = 256
MIN_SPEECH_SECONDS = 3
SHORT_UTTERANCE_POLICY = "short-utterance-v1"
MIN_SHORT_SPEECH_SECONDS = .3
MAX_SHORT_AUDIO_SECONDS = 4
ENROLLMENT_SECONDS = 12
ENROLLMENT_MIN_SPEECH = 6
REQUIRED_SAMPLES = 3
MAX_AUDIO_SECONDS = 30
MAX_PCM_BYTES = SAMPLE_RATE * 2 * MAX_AUDIO_SECONDS
MAX_WINDOWS = 16
MAX_PROFILE_BYTES = 1024 * 1024
COSINE_THRESHOLD = .70
MATCHING_POLICY = "pooled-owner-v1"
ENROLLMENT_CONSISTENCY = .82
WINDOW_CONSISTENCY = .80
MIN_RMS = .005
MAX_CLIPPED_FRACTION = .005
COVERAGE_TOLERANCE = .02
ENROLLMENT_TTL = 600
TEST_TTL = 180
MAX_SAMPLE_ATTEMPTS = 9
RUNTIME_CACHE_SECONDS = 5
CASES = ("owner", "other", "media", "overlap", "playback")
METADATA_KEY = "owner_voice_metadata"
POLICY_VERSION = "owner-voice-local-v2"
CONSENT_TEXT = (
    "I consent to enrolling and matching my voice locally on this computer. "
    "Only my accepted voice templates are saved, encrypted with Windows DPAPI; "
    "enrollment and matching use no new paid service. Normal speech accepted "
    "after matching still uses the existing AWS transcription, Bedrock, and "
    "voice services and their existing costs. This is acoustic personalization, "
    "not secure authentication, proof of a live speaker, or approval to act."
)


class OwnerVoiceError(ValueError):
    """A bounded, non-sensitive input or local-runtime error."""


class SpeakerWorkerError(OwnerVoiceError):
    pass


def _json(data):
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("Duplicate JSON field.")
            result[key] = value
        return result

    def invalid_constant(_):
        raise ValueError("Nonfinite JSON number.")

    return json.loads(data, object_pairs_hook=unique, parse_constant=invalid_constant)


class SpeakerWorker:
    """Fixed offline subprocess; bounded input, output, pending calls and waits."""

    MAX_PENDING = 2
    MAX_REQUEST_BYTES = 4 * ((MAX_PCM_BYTES + 2) // 3) + 2048
    MAX_RESPONSE_BYTES = 512 * 1024

    def __init__(self, *, timeout=45):
        if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not 0 < timeout <= 60:
            raise ValueError("Use a worker timeout between zero and 60 seconds.")
        self.timeout = timeout
        self.lock = threading.RLock()
        self.process = None
        self.pending = {}
        self.outgoing = None

    @staticmethod
    def _check_launch_paths():
        runtime = ROOT / ".speaker-runtime"
        executable = runtime / "Scripts" / "python.exe"
        script = ROOT / "speaker_worker.py"
        for path in (runtime, runtime / "Scripts", executable, script):
            if path.is_symlink() or getattr(path, "is_junction", lambda: False)():
                raise SpeakerWorkerError("The fixed local speaker runtime is unavailable.")
        if not executable.is_file() or not script.is_file():
            raise SpeakerWorkerError("The fixed local speaker runtime is unavailable.")

    def _start(self):
        if self.process is not None and self.process.poll() is None:
            return
        self._check_launch_paths()
        process = subprocess.Popen(
            [str(ROOT / ".speaker-runtime" / "Scripts" / "python.exe"),
             "-I", "-B", "-u", str(ROOT / "speaker_worker.py")],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            cwd=ROOT, shell=False, close_fds=True,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        )
        outgoing = queue.Queue(maxsize=self.MAX_PENDING)
        self.process, self.outgoing = process, outgoing
        threading.Thread(target=self._read, args=(process,), daemon=True).start()
        threading.Thread(target=self._write, args=(process, outgoing), daemon=True).start()

    def _fail(self, process, message):
        with self.lock:
            if self.process is process:
                self.process = None
                try:
                    self.outgoing.put_nowait(None)
                except queue.Full:
                    pass
            for owner_process, channel in self.pending.values():
                if owner_process is process:
                    try:
                        channel.put_nowait({"ok": False, "error": message})
                    except queue.Full:
                        pass
        try:
            if process.poll() is None:
                process.kill()
            process.wait(timeout=2)
        except (OSError, subprocess.TimeoutExpired):
            pass

    def _read(self, process):
        try:
            while True:
                line = process.stdout.readline(self.MAX_RESPONSE_BYTES + 1)
                if not line or len(line) > self.MAX_RESPONSE_BYTES or not line.endswith(b"\n"):
                    break
                data = _json(line)
                if (not isinstance(data, dict) or not isinstance(data.get("id"), str)
                        or type(data.get("ok")) is not bool):
                    break
                with self.lock:
                    pending = self.pending.get(data["id"])
                    if pending is None or pending[0] is not process:
                        break
                    try:
                        pending[1].put_nowait(data)
                    except queue.Full:
                        break
        except (OSError, ValueError, TypeError):
            pass
        finally:
            self._fail(process, "The local speaker worker stopped or returned invalid data.")
            try:
                process.stdout.close()
            except OSError:
                pass

    def _write(self, process, outgoing):
        try:
            while True:
                request = outgoing.get()
                if request is None or process.poll() is not None:
                    break
                process.stdin.write(request)
                process.stdin.flush()
        except (OSError, ValueError):
            self._fail(process, "The local speaker worker connection ended.")
        finally:
            try:
                process.stdin.close()
            except OSError:
                pass

    def call(self, command, payload):
        if (command not in ("status", "analyze", "analyze_greeting", "analyze_short") or not isinstance(payload, dict)
                or (command == "status" and payload)
                or (command in ("analyze", "analyze_greeting", "analyze_short") and (
                    set(payload) != {"audio", "sample_rate"}
                    or type(payload["sample_rate"]) is not int or payload["sample_rate"] != SAMPLE_RATE
                    or not isinstance(payload["audio"], str)
                    or len(payload["audio"]) > self.MAX_REQUEST_BYTES - 2048))):
            raise SpeakerWorkerError("Invalid local speaker request.")
        identity = uuid.uuid4().hex
        try:
            encoded = json.dumps({"id": identity, "command": command, **payload},
                                 allow_nan=False, separators=(",", ":")).encode() + b"\n"
        except (ValueError, TypeError, UnicodeError):
            raise SpeakerWorkerError("Invalid local speaker request.") from None
        if len(encoded) > self.MAX_REQUEST_BYTES:
            raise SpeakerWorkerError("The local speaker request exceeds its size limit.")
        channel = queue.Queue(maxsize=1)
        with self.lock:
            if len(self.pending) >= self.MAX_PENDING:
                raise SpeakerWorkerError("The local speaker worker is busy.")
            try:
                self._start()
            except (OSError, ValueError):
                raise SpeakerWorkerError("The fixed local speaker runtime could not start.") from None
            process = self.process
            self.pending[identity] = (process, channel)
            self.outgoing.put_nowait(encoded)
        try:
            result = channel.get(timeout=self.timeout)
            if result.get("ok") is not True or not isinstance(result.get("value"), dict):
                raise SpeakerWorkerError("Local speaker analysis failed.")
            return result["value"]
        except queue.Empty:
            self._fail(process, "Local speaker analysis timed out.")
            raise SpeakerWorkerError("Local speaker analysis timed out.") from None
        finally:
            with self.lock:
                self.pending.pop(identity, None)

    def close(self):
        with self.lock:
            process = self.process
        if process is not None:
            self._fail(process, "The local speaker worker was closed.")


def _pending_validation():
    return dict.fromkeys(CASES, "pending")


def _number(value, minimum, maximum):
    return (type(value) in (int, float) and math.isfinite(value)
            and minimum <= value <= maximum)


def _vector(value):
    if not isinstance(value, list) or len(value) != DIMENSIONS:
        raise ValueError("Invalid embedding.")
    if not all(_number(item, -1e6, 1e6) for item in value):
        raise ValueError("Invalid embedding.")
    norm = math.hypot(*value)
    if not 1e-8 <= norm <= 1e6:
        raise ValueError("Invalid embedding.")
    return tuple(item / norm for item in value)


def _cosine(left, right):
    return max(-1., min(1., math.fsum(a * b for a, b in zip(left, right))))


def _consistent(vectors, threshold):
    return all(_cosine(vector, other) >= threshold
               for index, vector in enumerate(vectors) for other in vectors[:index])


def _prototype(templates):
    """One normalized representation of an independently recorded sample set."""
    if not templates:
        raise ValueError("Missing voice references.")
    return _vector([math.fsum(vector[index] for vector in templates) / len(templates)
                    for index in range(DIMENSIONS)])


def _held_out_scores(templates):
    """Compare each recording with a prototype that excludes that recording."""
    if len(templates) < 3:
        raise ValueError("Three independent references are required.")
    return tuple(_cosine(vector, _prototype(templates[:index] + templates[index + 1:]))
                 for index, vector in enumerate(templates))


class OwnerVoice:
    def __init__(self, store, *, worker=None, clock=time.time, protect_fn=protect):
        self.store, self.clock, self.protect_fn = store, clock, protect_fn
        self.diagnostics=VoiceDiagnostics(store,clock=clock,protect_fn=protect_fn)
        self.worker = worker if worker is not None else SpeakerWorker()
        self.directory = Path(store.directory).absolute()
        self.profile_path = self.directory / "owner-voice-profile.dpapi"
        self.lock = threading.RLock()
        self.analysis_gate = threading.BoundedSemaphore(1)
        self.analyzing = threading.Event()
        self.closed = threading.Event()
        self.runtime_probe_gate = threading.Lock()
        self.runtime_probe_thread = None
        self.sessions, self.active = {}, None
        self.profile_cache = None
        from greeting_profile import GreetingProfile
        self.greetings = GreetingProfile(self)
        self.runtime = {"ready": False, "error": None,
                        "model_key": None, "binding": None, "checked_at": -math.inf}
        # Retain the owner's saved choice. Setup already persists disabled state,
        # and automatic admission still requires a fresh, matching runtime probe.
        with self._state():
            pass

    @staticmethod
    def _default(owner, revision=0):
        return {
            "owner": owner, "revision": revision, "profile_id": None, "profile_digest": None,
            "profile_binding": None, "model_key": None, "runtime_binding": None,
            "runtime_available": None,
            "samples": 0, "enabled": False, "validation": _pending_validation(),
            "other_check_deferred": False,
            "storage_failed": False,
        }

    @staticmethod
    def _owner(db):
        try:
            paired = {row["key"]: _json(row["value"]) for row in db.execute(
                "SELECT key,value FROM settings WHERE key IN ('owner_id','owner_chat_id')"
            )}
            owner, chat = paired.get("owner_id"), paired.get("owner_chat_id")
        except (ValueError, TypeError):
            return None
        return owner if type(owner) is int and owner > 0 and type(chat) is int and chat == owner else None

    def _clear_sessions(self):
        self.sessions.clear()
        self.active = None

    @contextlib.contextmanager
    def _state(self, runtime=None, *, include_db=False, commit_rejections=True):
        failure = None
        with self.lock:
            with self.store.db() as db:
                db.execute("BEGIN IMMEDIATE")
                owner = self._owner(db)
                row = db.execute("SELECT value FROM settings WHERE key=?", (METADATA_KEY,)).fetchone()
                try:
                    state = _json(row[0]) if row else self._default(owner)
                    # Metadata predating the readiness/consent split retains its
                    # enrolled template, completed checks, and saved preference.
                    if isinstance(state, dict):
                        missing = set(self._default(owner)) - set(state)
                        if missing <= {"runtime_available", "other_check_deferred"}:
                            state.setdefault("runtime_available", None)
                            state.setdefault("other_check_deferred", False)
                    if (not isinstance(state, dict) or set(state) != set(self._default(owner))
                            or type(state["revision"]) is not int or state["revision"] < 0
                            or type(state["enabled"]) is not bool
                            or type(state["other_check_deferred"]) is not bool
                            or (state["runtime_available"] is not None and type(state["runtime_available"]) is not bool)
                            or not isinstance(state["validation"], dict)
                            or set(state["validation"]) != set(CASES)
                            or any(value not in ("pending", "passed", "failed") for value in state["validation"].values())):
                        raise ValueError("Invalid metadata.")
                except (ValueError, TypeError):
                    state = self._default(owner)
                    self._clear_sessions()
                if state["owner"] != owner:
                    state = self._default(owner, state["revision"] + 1)
                    self._clear_sessions()
                    self.profile_cache = None
                # A closing worker can finish with an unavailable result. That
                # process-local shutdown must not revoke a saved profile or a
                # replacement host's enablement.
                if runtime is not None and not self.closed.is_set():
                    self.runtime = runtime
                    if runtime["ready"]:
                        if runtime["binding"] != state["runtime_binding"]:
                            if state["runtime_binding"] is not None or state["profile_id"] or self.active:
                                state["revision"] += 1
                                state["enabled"] = False
                                state["validation"] = _pending_validation()
                                state["other_check_deferred"] = False
                                self._clear_sessions()
                        state["runtime_binding"] = runtime["binding"]
                        state["runtime_available"] = True
                        policy_row=db.execute(
                            "SELECT value FROM settings WHERE key='owner_voice_decision_binding'").fetchone()
                        try:
                            previous_policy=_json(policy_row[0]) if policy_row else None
                        except (TypeError,ValueError):
                            previous_policy="invalid"
                        decision_binding=runtime.get("decision_binding")
                        if previous_policy!=decision_binding:
                            # A decision-policy update invalidates validation and
                            # in-flight permissions, but not compatible original-
                            # waveform voice templates. No re-enrollment shortcut
                            # is used for a changed model or feature extractor.
                            if state["profile_id"] or self.active:
                                state["revision"]+=1
                            state["enabled"]=False
                            state["validation"]=_pending_validation()
                            state["other_check_deferred"]=False
                            self._clear_sessions()
                            db.execute("INSERT OR REPLACE INTO settings(key,value) VALUES(?,?)",
                                       ("owner_voice_decision_binding",json.dumps(decision_binding)))
                    else:
                        # Readiness loss invalidates captures/proofs, not consent.
                        # Keep the last confirmed binding so recovery must match
                        # that exact validated model before listening can resume.
                        if state["runtime_available"] is not False and (state["profile_id"] or self.active):
                            state["revision"] += 1
                        state["runtime_available"] = False
                        self._clear_sessions()
                now = self.clock()
                expired = [identity for identity, item in self.sessions.items() if item["expires"] <= now]
                if self.active in expired:
                    state["revision"] += 1
                    state["enabled"] = False
                    self.active = None
                for identity in expired:
                    self.sessions.pop(identity, None)
                templates = self._read_profile(state)
                if state["profile_id"] and templates is None:
                    if not state["storage_failed"]:
                        state["revision"] += 1
                    state["storage_failed"] = True
                    state["enabled"] = False
                    state["validation"] = _pending_validation()
                    state["other_check_deferred"] = False
                    self._clear_sessions()
                try:
                    yield (state, templates, db) if include_db else (state, templates)
                except (OwnerVoiceError, ConflictError) as error:
                    if not commit_rejections:
                        raise
                    # Revocations detected while rejecting a stale mutation still commit.
                    failure = error
                encoded = json.dumps(state, allow_nan=False, separators=(",", ":"))
                if row is None or encoded != row[0]:
                    db.execute("INSERT OR REPLACE INTO settings(key,value) VALUES(?,?)",
                               (METADATA_KEY, encoded))
        if failure is not None:
            raise failure

    def _safe_path(self, path):
        if (self.directory.resolve() != self.directory
                or path.parent != self.directory or path.resolve().parent != self.directory
                or path.is_symlink() or getattr(path, "is_junction", lambda: False)()):
            raise OwnerVoiceError("The fixed protected profile path is unavailable.")

    def _read_profile(self, state):
        if not state["profile_id"]:
            return None
        try:
            self._safe_path(self.profile_path)
            with self.profile_path.open("rb") as stream:
                encrypted = stream.read(MAX_PROFILE_BYTES + 1)
            digest = hashlib.sha256(encrypted).hexdigest()
            if not 0 < len(encrypted) <= MAX_PROFILE_BYTES or digest != state["profile_digest"]:
                return None
            cache_key = (digest, state["owner"], state["profile_id"], state["profile_binding"])
            if self.profile_cache and self.profile_cache[0] == cache_key:
                return self.profile_cache[1]
            raw = self.protect_fn(encrypted, decrypt=True)
            if not isinstance(raw, bytes) or len(raw) > MAX_PROFILE_BYTES:
                return None
            value = _json(raw)
            if (not isinstance(value, dict) or value.get("version") != POLICY_VERSION
                    or value.get("owner") != state["owner"] or value.get("profile_id") != state["profile_id"]
                    or value.get("binding") != state["profile_binding"]
                    or value.get("model_key") != state["model_key"]
                    or value.get("samples") != REQUIRED_SAMPLES
                    or not isinstance(value.get("templates"), list)
                    or len(value["templates"]) != REQUIRED_SAMPLES):
                return None
            templates = tuple(_vector(vector) for vector in value["templates"])
            if not _consistent(templates, ENROLLMENT_CONSISTENCY):
                return None
            self.profile_cache = (cache_key, templates)
            return templates
        except Exception:
            # Never expose DPAPI errors, decrypted data, or supplied vectors.
            return None

    def _write_profile(self, encrypted):
        self._safe_path(self.profile_path)
        temporary = self.directory / (".owner-voice-profile." + uuid.uuid4().hex + ".tmp")
        self._safe_path(temporary)
        try:
            with temporary.open("xb") as stream:
                stream.write(encrypted)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.profile_path)
        finally:
            self._safe_path(temporary)
            temporary.unlink(missing_ok=True)

    def _unavailable_runtime(self):
        return {"ready": False, "error": "The local speaker runtime is unavailable.",
                "model_key": None, "binding": None, "checked_at": self.clock()}

    def _runtime_status(self):
        unavailable = self._unavailable_runtime()
        if self.closed.is_set():
            return unavailable
        try:
            value = self.worker.call("status", {})
            if (not isinstance(value, dict) or value.get("ready") is not True
                    or not isinstance(value.get("model_key"), str)
                    or not re.fullmatch(r"[A-Za-z0-9_.:/@+-]{1,160}", value["model_key"])
                    or value.get("sample_rate", SAMPLE_RATE) != SAMPLE_RATE
                    or value.get("embedding_dimensions", DIMENSIONS) != DIMENSIONS):
                return unavailable
            binding = {key: value.get(key) for key in (
                "model_key", "model_hashes", "preprocess_version", "runtime_versions", "analysis_policy",
            )}
            binding["owner_voice_policy"] = [POLICY_VERSION, COSINE_THRESHOLD,
                                             ENROLLMENT_CONSISTENCY, WINDOW_CONSISTENCY]
            encoded = json.dumps(binding, sort_keys=True, allow_nan=False, separators=(",", ":")).encode()
            if len(encoded) > 32768:
                return unavailable
            feature_binding=hashlib.sha256(encoded).hexdigest()
            decision=value.get("decision_policy")
            decision_binding=None
            if decision is not None:
                if (not isinstance(decision,dict) or not isinstance(decision.get("version"),str)
                        or not 1<=len(decision["version"])<=160):
                    return unavailable
                encoded_decision=json.dumps([feature_binding,decision],sort_keys=True,
                                            allow_nan=False,separators=(",",":")).encode()
                if len(encoded_decision)>32768:
                    return unavailable
                decision_binding=hashlib.sha256(encoded_decision).hexdigest()
            return {"ready": True, "error": None, "model_key": value["model_key"],
                    "binding": feature_binding, "checked_at": self.clock(),
                    "decision_binding":decision_binding,
                    "decision_policy_version":decision.get("version") if decision else None,
                    "preprocess_version":value.get("preprocess_version")}
        except Exception:
            return unavailable

    def _runtime_fresh(self):
        return 0 <= self.clock() - self.runtime["checked_at"] < RUNTIME_CACHE_SECONDS

    def _schedule_runtime_probe(self):
        if (self.closed.is_set() or self._runtime_fresh() or self.analyzing.is_set()
                or not self.runtime_probe_gate.acquire(blocking=False)):
            return

        def probe():
            try:
                runtime = self._runtime_status()
                if not self.closed.is_set():
                    with self._state(runtime):
                        pass
            except Exception:
                # Status polling must remain usable during shutdown/storage faults.
                pass
            finally:
                self.runtime_probe_gate.release()

        self.runtime_probe_thread = threading.Thread(target=probe, daemon=True)
        self.runtime_probe_thread.start()

    def _current_profile(self, state, templates):
        return bool(state["owner"] and templates and not state["storage_failed"]
                    and state["profile_binding"] == state["runtime_binding"]
                    and not self.active)

    def _validated_profile(self, state, templates):
        return bool(self._current_profile(state, templates)
                    and all(value == "passed" for value in state["validation"].values()))

    def _other_check_deferrable(self, state, templates):
        return bool(self._current_profile(state, templates)
                    and state["validation"]["other"] == "pending"
                    and all(state["validation"][case] == "passed" for case in CASES if case != "other"))

    def _admitted_profile(self, state, templates):
        # Deferral is an explicit owner choice, never a synthetic fifth pass.
        # A failed check cannot be deferred, and matching every utterance is unchanged.
        return self._validated_profile(state, templates) or bool(
            state["other_check_deferred"] and self._other_check_deferrable(state, templates))

    def _eligible(self, state, templates, *, require_fresh=True):
        return bool(not self.closed.is_set() and self._admitted_profile(state, templates)
                    and state["runtime_available"] is True and self.runtime["ready"]
                    and (not require_fresh or self._runtime_fresh())
                    and state["profile_binding"] == self.runtime["binding"])

    def _status(self, state, templates, *, db=None):
        validated = self._validated_profile(state, templates)
        eligible = self._eligible(state, templates, require_fresh=False)
        return {
            "runtime": {
                "ready": self.runtime["ready"] and state["runtime_available"] is True and not self.closed.is_set(),
                "error": self.runtime["error"], "model_key": self.runtime["model_key"],
                "stale": not self._runtime_fresh(),
                "refreshing": self.runtime_probe_gate.locked() and not self.closed.is_set(),
                "preprocess_version":self.runtime.get("preprocess_version"),
                "decision_policy_version":self.runtime.get("decision_policy_version"),
            },
            "profile": {
                "revision": state["revision"], "enrolled": templates is not None,
                "enabled": bool(state["enabled"] and self._admitted_profile(state, templates)),
                "validated": validated,
                "other_check_deferred": bool(state["other_check_deferred"]
                                             and self._other_check_deferrable(state, templates)),
                "samples": state["samples"] if templates else 0,
                "validation": dict(state["validation"]),
            },
            "can_enable": eligible and validated and not state["enabled"],
            "can_defer_other_check": bool(not state["enabled"] and self._other_check_deferrable(state, templates)
                and self._eligible({**state, "other_check_deferred": True}, templates, require_fresh=False)),
            "consent_text": CONSENT_TEXT,
            "min_speech_seconds": MIN_SPEECH_SECONDS, "enrollment_seconds": ENROLLMENT_SECONDS,
            "short_utterance": {"version": SHORT_UTTERANCE_POLICY,
                                "min_speech_seconds": MIN_SHORT_SPEECH_SECONDS,
                                "max_audio_seconds": MAX_SHORT_AUDIO_SECONDS},
            "matching_policy": MATCHING_POLICY,
            "required_samples": REQUIRED_SAMPLES,
            "greeting": self.greetings.status(state, db),
        }

    def status(self):
        self._schedule_runtime_probe()
        with self._state(include_db=True) as (state, templates, db):
            result=self._status(state, templates, db=db)
            result["capture_active"]=self.active is not None
            result["analysis_active"]=self.analyzing.is_set()
            owner=state["owner"]
            result["last_checks"]={}
            result["owner_comparisons"]={}
            policy_row=db.execute("SELECT value FROM settings WHERE key='owner_voice_decision_binding'").fetchone()
            try:
                decision_binding=_json(policy_row[0]) if policy_row else None
            except (TypeError,ValueError):
                decision_binding="invalid"
            keys=("owner_voice_last_validation_check",*(
                "owner_voice_last_validation_check:"+case for case in CASES),
                "owner_voice_last_validation_check:owner:standard",
                "owner_voice_last_validation_check:owner:unprocessed")
            rows=db.execute("SELECT key,value FROM settings WHERE key IN (?,?,?,?,?,?,?,?) ORDER BY key",keys).fetchall()
            for row in rows:
                if len(row["value"])>16384:
                    continue
                try:
                    saved=json.loads(row["value"])
                    check=saved["result"]
                    if (owner and saved["owner"]==owner and saved["profile_id"]==state["profile_id"]
                            and saved["profile_binding"]==state["profile_binding"]
                            and saved.get("decision_binding")==decision_binding
                            and type(check["profile_revision"]) is int
                            and check["profile_revision"]<=state["revision"]
                            and _number(check["checked_at"],0,1e12)
                            and check["case"] in CASES and type(check["passed"]) is bool
                            and state["validation"][check["case"]]!="pending"):
                        public={key:check[key] for key in (
                            "case","passed","decision","reason","detail","profile_revision",
                            "checked_at","diagnostics")}
                        mode = check.get("input_mode", "standard")
                        public["input_mode"] = mode if mode in ("standard", "unprocessed") else "standard"
                        # Comparison history is informational. Only the latest
                        # labelled check controls validation and enablement.
                        if check["case"]=="owner":
                            previous=result["owner_comparisons"].get(public["input_mode"])
                            if previous is None or public["profile_revision"]>previous["profile_revision"]:
                                result["owner_comparisons"][public["input_mode"]]=public
                        if state["validation"][check["case"]]==("passed" if check["passed"] else "failed"):
                            previous=result["last_checks"].get(check["case"])
                            if previous is None or public["profile_revision"]>previous["profile_revision"]:
                                result["last_checks"][check["case"]]=public
                except (ValueError,KeyError,TypeError):
                    pass
            result["last_check"]=max(result["last_checks"].values(),
                                     key=lambda check:check["checked_at"],default=None)
        try:
            result["diagnostic"]=self.diagnostics.status(owner)
        except PermissionError:
            result["diagnostic"]={"state":"off"}
        except (ValueError,OSError):
            result["diagnostic"]={"state":"needs_review"}
        return result

    def revision(self):
        with self._state() as (state, _):
            return state["revision"]

    def automatic_allowed(self):
        status = self.status()
        return status["profile"]["enabled"] and status["runtime"]["ready"]

    def admit_auto(self, expected_revision, reserve):
        """Atomically reserve local speech work; the caller performs ASR after return.

        The caller holds its capture lock. ``reserve(db)`` must only reserve the
        existing local budget/run row, and must never do provider or network I/O.
        This is the only parent callback invoked under an OwnerVoice state lock.
        """
        if not callable(reserve):
            raise OwnerVoiceError("A local speech reservation callback is required.")
        rejection, result = None, None
        with self._state(include_db=True, commit_rejections=False) as (state, templates, db):
            try:
                self._expect(state, expected_revision)
                if not state["enabled"] or not self._eligible(state, templates):
                    raise OwnerVoiceError("The current owner voice profile cannot admit automatic speech.")
            except (OwnerVoiceError, ConflictError) as error:
                # Persist a pairing/profile revocation even when admission is
                # rejected. No reservation has run at this point.
                rejection = error
            else:
                # Callback exceptions still escape the transaction and roll
                # back any partial reservation.
                result = reserve(db)
        if rejection is not None:
            raise rejection
        return result

    def _expect(self, state, revision):
        if self.closed.is_set():
            raise OwnerVoiceError("This owner voice service has been closed.")
        if type(revision) is not int or revision != state["revision"]:
            raise ConflictError("The owner voice profile changed. Refresh before continuing.")
        if state["owner"] is None:
            raise OwnerVoiceError("Pair the owner before changing the voice profile.")

    @staticmethod
    def _pcm(pcm_bytes, sample_rate=SAMPLE_RATE):
        if (type(sample_rate) is not int or sample_rate != SAMPLE_RATE
                or not isinstance(pcm_bytes, bytes) or not 0 < len(pcm_bytes) <= MAX_PCM_BYTES
                or len(pcm_bytes) % 2):
            raise OwnerVoiceError("Use at most 30 seconds of mono signed 16-bit little-endian audio at 16 kHz.")
        return hashlib.sha256(pcm_bytes).hexdigest()

    @staticmethod
    def _key(key):
        if not isinstance(key, str) or not re.fullmatch(r"[A-Za-z0-9_.:-]{1,128}", key):
            raise OwnerVoiceError("A stable sample key is required.")

    def _session(self, identity, state, kind):
        item = self.sessions.get(identity) if isinstance(identity, str) else None
        if (self.closed.is_set() or not item or item["kind"] != kind or item["owner"] != state["owner"]
                or item["revision"] != state["revision"]
                or item["profile_id"] != state["profile_id"]
                or item["binding"] != self.runtime["binding"] or item["expires"] <= self.clock()):
            raise ConflictError("This voice capture expired, was cancelled, or belongs to an older profile.")
        return item

    def _start_session(self, state, kind, case=None):
        if self.active:
            raise OwnerVoiceError("Finish or cancel the current owner voice capture first.")
        if not self.runtime["ready"]:
            raise OwnerVoiceError("The local speaker runtime is unavailable.")
        self._clear_sessions()
        identity = uuid.uuid4().hex
        previous = {"enabled": state["enabled"], "validation": dict(state["validation"]),
                    "other_check_deferred": state["other_check_deferred"]}
        state["enabled"] = False
        # Unfinished setup is provisional. Keep completed checks and the
        # owner's deferral choice through cancellation, timeout or restart.
        # The active-session guard and enabled=False still block admission.
        state["revision"] += 1
        item = {
            "id": identity, "kind": kind, "case": case, "owner": state["owner"],
            "revision": state["revision"], "profile_id": state["profile_id"],
            "binding": self.runtime["binding"], "previous": previous,
            "expires": self.clock() + (ENROLLMENT_TTL if kind in ("enrollment", "greeting") else TEST_TTL),
            "attempts": {}, "samples": [], "done": False,
            "started_at": self.clock(),
        }
        self.sessions[identity], self.active = item, identity
        return item

    @staticmethod
    def _enrollment(item):
        return {"id": item["id"], "collected": len(item["samples"]), "required": REQUIRED_SAMPLES}

    def enroll_start(self, consent, expected_revision):
        if consent is not True:
            raise OwnerVoiceError("Explicit consent is required before owner voice enrollment.")
        runtime = self._runtime_status()
        with self._state(runtime) as (state, templates):
            self._expect(state, expected_revision)
            item = self._start_session(state, "enrollment")
            return {"status": self._status(state, templates), "enrollment": self._enrollment(item)}

    def _reserve(self, item, key, digest):
        self._key(key)
        previous = item["attempts"].get(key)
        if previous:
            if previous["digest"] != digest:
                raise ConflictError("A sample key cannot be reused for different audio.")
            if previous["result"] is None:
                raise ConflictError("This sample is already being analyzed.")
            return copy.deepcopy(previous["result"])
        if len(item["attempts"]) >= MAX_SAMPLE_ATTEMPTS:
            raise OwnerVoiceError("This capture reached its sample-attempt limit. Start a new capture.")
        if any(attempt["result"] is None for attempt in item["attempts"].values()):
            raise ConflictError("Finish the current sample before submitting another.")
        if any(attempt["digest"] == digest for attempt in item["attempts"].values()):
            raise OwnerVoiceError("Record a new clip; the same audio cannot be submitted again under another key.")
        item["attempts"][key] = {"digest": digest, "result": None}
        return None

    def _analyze(self, pcm_bytes, *, greeting_only=False, short_utterance=False):
        if not self.analysis_gate.acquire(blocking=False):
            return None, self.runtime, "busy"
        self.analyzing.set()
        try:
            value = self.worker.call("analyze_short" if short_utterance else
                                     "analyze_greeting" if greeting_only else "analyze", {
                "audio": base64.b64encode(pcm_bytes).decode("ascii"), "sample_rate": SAMPLE_RATE,
            })
            runtime = self._runtime_status()
            return value, runtime, None
        except Exception:
            # Do not hide an analysis failure behind an immediate successful
            # restart/probe: first invalidate proofs spanning the interruption.
            return None, self._unavailable_runtime(), "runtime_unavailable"
        finally:
            self.analyzing.clear()
            self.analysis_gate.release()

    @staticmethod
    def _evidence(value, pcm_bytes, model_key, minimum):
        base = {"valid_speech": False, "vectors": (), "reference": None,
                "reason": "invalid_analysis", "decision": "unavailable"}
        if (not isinstance(value, dict) or value.get("model_key") != model_key
                or not _number(value.get("duration"), 0, MAX_AUDIO_SECONDS)
                or abs(value["duration"] - len(pcm_bytes) / (SAMPLE_RATE * 2)) > COVERAGE_TOLERANCE
                or not _number(value.get("voiced_seconds"), 0, value["duration"] + COVERAGE_TOLERANCE)
                or not _number(value.get("overlap_seconds"), 0, value["voiced_seconds"] + COVERAGE_TOLERANCE)
                or type(value.get("multiple_speakers")) is not bool
                or not isinstance(value.get("quality"), dict)
                or not _number(value["quality"].get("rms"), 0, 1)
                or not _number(value["quality"].get("clipped_fraction"), 0, 1)
                or not isinstance(value.get("windows"), list) or len(value["windows"]) > MAX_WINDOWS):
            return base
        base.update(duration=value["duration"], voiced_seconds=value["voiced_seconds"])
        if value["voiced_seconds"] < minimum:
            return {**base, "decision": "unknown", "reason": "insufficient_speech"}
        if value["quality"]["rms"] < MIN_RMS or value["quality"]["clipped_fraction"] > MAX_CLIPPED_FRACTION:
            return {**base, "decision": "unknown", "reason": "poor_quality"}
        if value["overlap_seconds"] > 0 or value["multiple_speakers"]:
            return {**base, "decision": "overlap", "reason": "multiple_or_overlapping_speakers", "valid_speech": True}
        analysis_reason=value.get("analysis_reason")
        if analysis_reason not in (None,"clean_speech","ambiguous_speech","poor_quality",
                                   "insufficient_speech","incomplete_speech_coverage",
                                   "multiple_or_overlapping_speakers"):
            return {**base,"reason":"invalid_analysis"}
        if analysis_reason in ("ambiguous_speech","poor_quality","insufficient_speech","incomplete_speech_coverage"):
            return {**base,"decision":"unknown","reason":analysis_reason}
        if analysis_reason=="multiple_or_overlapping_speakers":
            return {**base,"reason":"invalid_analysis"}
        vectors, covered, previous_end = [], 0., 0.
        try:
            for window in value["windows"]:
                if (not isinstance(window, dict) or not _number(window.get("start"), previous_end, value["duration"])
                        or not _number(window.get("end"), window["start"], value["duration"] + COVERAGE_TOLERANCE)
                        or window["end"] <= window["start"]
                        or not _number(window.get("voiced_seconds"), min(MIN_SPEECH_SECONDS, minimum),
                                       window["end"] - window["start"] + COVERAGE_TOLERANCE)):
                    raise ValueError("Invalid window.")
                vectors.append(_vector(window.get("embedding")))
                covered += window["voiced_seconds"]
                previous_end = window["end"]
        except (ValueError, TypeError, KeyError):
            return {**base, "reason": "invalid_embedding_or_window"}
        if not vectors or abs(covered - value["voiced_seconds"]) > COVERAGE_TOLERANCE:
            return {**base, "decision": "unknown", "reason": "incomplete_speech_coverage"}
        try:
            reference=_vector(value.get("clip_embedding"))
        except (TypeError,ValueError):
            return {**base,"reason":"invalid_recording_reference"}
        return {**base, "valid_speech": True, "vectors": tuple(vectors), "reference":reference,
                "reason": "clean_speech", "decision": "unknown"}

    def enroll_sample(self, enrollment_id, sample_key, pcm_bytes, sample_rate=SAMPLE_RATE):
        digest = self._pcm(pcm_bytes, sample_rate)
        runtime = self._runtime_status()
        with self._state(runtime) as (state, templates):
            item = self._session(enrollment_id, state, "enrollment")
            cached = self._reserve(item, sample_key, digest)
            if cached is not None:
                return {"status": self._status(state, templates), "enrollment": self._enrollment(item), **cached}
            if len(item["samples"]) >= REQUIRED_SAMPLES:
                item["attempts"].pop(sample_key, None)
                raise OwnerVoiceError("Three clips are already collected. Commit or cancel this enrollment.")
            binding, model_key = item["binding"], self.runtime["model_key"]
        value, runtime, error = self._analyze(pcm_bytes)
        evidence = self._evidence(value, pcm_bytes, model_key, ENROLLMENT_MIN_SPEECH)
        with self._state(runtime,include_db=True) as (state, templates, db):
            item = self._session(enrollment_id, state, "enrollment")
            if binding != self.runtime["binding"]:
                raise ConflictError("The speaker model changed during enrollment.")
            reference=evidence["reference"]
            previous=tuple(vector for sample in item["samples"] for vector in sample)
            coherent=reference is not None and all(
                _cosine(vector,reference)>=ENROLLMENT_CONSISTENCY for vector in evidence["vectors"])
            agrees=reference is not None and _consistent((*previous,reference),ENROLLMENT_CONSISTENCY)
            accepted = (error is None and evidence["reason"] == "clean_speech"
                        and coherent and agrees)
            reason = error or evidence["reason"]
            failure_stage=None
            if not accepted and reason == "clean_speech":
                reason = "inconsistent_speaker"
                failure_stage="window_to_recording" if not coherent else "between_recordings"
            # Return only fixed, non-sensitive explanations. Do not expose PCM,
            # embeddings, similarity scores, model exceptions or profile data.
            detail = {
                "clean_speech": "Accepted a clean, consistent owner-labelled clip.",
                "insufficient_speech": "Not enough clear speech was detected. Speak for at least six seconds within the 12-second recording, then try again.",
                "poor_quality": "The recording is too quiet or clipped. Check your microphone level and record again in a quiet place.",
                "multiple_or_overlapping_speakers": "The local check flagged possible mixed speech. This can be a false detection; retry with only your voice audible.",
                "ambiguous_speech": "There was enough speech, but the local check could not confidently classify parts of it. Try this clip again; the diagnostic counts are saved locally.",
                "inconsistent_speaker": (
                    "Parts of this recording did not match its complete voice reference. No profile was changed."
                    if failure_stage=="window_to_recording" else
                    "This recording did not match the earlier accepted recording closely enough. Keep the same microphone and your natural voice."),
                "incomplete_speech_coverage": "The local check could not analyze all the speech clearly. Record again with at least six seconds of clear speech from you alone.",
                "invalid_analysis": "The local speaker returned an incomplete analysis. Refresh status before recording again.",
                "invalid_embedding_or_window": "The local speaker returned incomplete speech evidence. Refresh status before recording again.",
                "invalid_recording_reference": "The local speaker did not return a valid complete-recording reference. Refresh status before recording again.",
                "busy": "The local speaker analyzer is busy. Wait for the current check to finish, then record this clip again.",
            }.get(reason, "The local speaker check could not accept this clip. Refresh status before recording again.")
            if accepted:
                item["samples"].append((reference,))
            diagnostics = {
                "duration_seconds": round(len(pcm_bytes)/(SAMPLE_RATE*2),2),
                "speech_seconds": round(value["voiced_seconds"],2) if isinstance(value,dict)
                    and _number(value.get("voiced_seconds"),0,MAX_AUDIO_SECONDS) else None,
                "overlap_seconds": round(value["overlap_seconds"],2) if isinstance(value,dict)
                    and _number(value.get("overlap_seconds"),0,MAX_AUDIO_SECONDS) else None,
                "uncertain_seconds": round(value["uncertain_seconds"],2) if isinstance(value,dict)
                    and _number(value.get("uncertain_seconds"),0,MAX_AUDIO_SECONDS) else None,
                "analysis_windows": len(value["windows"]) if isinstance(value,dict)
                    and isinstance(value.get("windows"),list) and len(value["windows"])<=MAX_WINDOWS else None,
                "failure_stage": failure_stage,
                "accepted_recordings":len(item["samples"]),
                "reference_strategy":"complete_recording",
            }
            # Only the latest explicit enrollment check is retained. No PCM,
            # embedding, transcript, similarity score or background log is saved.
            diagnostic_record={
                "owner":state["owner"],"checked_at":self.clock(),"accepted":accepted,
                "reason":reason,"runtime_binding":self.runtime["binding"],**diagnostics}
            db.execute("INSERT OR REPLACE INTO settings(key,value) VALUES(?,?)",
                       ("owner_voice_last_enrollment_check",json.dumps(diagnostic_record,allow_nan=False)))
            result = {"accepted": accepted, "reason": reason, "detail": detail,"diagnostics":diagnostics}
            diagnostic=self.diagnostics.capture_locked(db,state["owner"],pcm_bytes,result)
            if diagnostic["state"]!="off":
                result["diagnostic"]=diagnostic
                if diagnostic["state"]=="stored":
                    result["detail"]+=" A diagnostic copy was saved encrypted on this PC for the approved investigation."
                elif diagnostic["state"]=="not_saved":
                    result["detail"]+=" The diagnostic copy could not be saved."
                elif diagnostic["state"]=="expired":
                    result["detail"]+=" The one-time diagnostic permission expired; no recording was saved."
            item["attempts"][sample_key]["result"] = result
            return {"status": self._status(state, templates), "enrollment": self._enrollment(item), **result}

    def enroll_commit(self, enrollment_id, expected_revision):
        runtime = self._runtime_status()
        with self._state(runtime) as (state, _):
            self._expect(state, expected_revision)
            item = self._session(enrollment_id, state, "enrollment")
            if len(item["samples"]) != REQUIRED_SAMPLES or any(
                attempt["result"] is None for attempt in item["attempts"].values()
            ):
                raise OwnerVoiceError("Collect three accepted owner voice clips before committing.")
            templates = tuple(vector for sample in item["samples"] for vector in sample)
            profile_id = uuid.uuid4().hex
            profile = {"version": POLICY_VERSION, "owner": state["owner"], "profile_id": profile_id,
                       "binding": item["binding"], "model_key": self.runtime["model_key"],
                       "samples": REQUIRED_SAMPLES, "templates": templates}
        try:
            encrypted = self.protect_fn(json.dumps(profile, allow_nan=False, separators=(",", ":")).encode())
            if not isinstance(encrypted, bytes) or not 0 < len(encrypted) <= MAX_PROFILE_BYTES:
                raise ValueError("Invalid protected data.")
        except Exception:
            raise OwnerVoiceError("Windows could not encrypt the owner template. The previous profile was preserved.") from None
        runtime = self._runtime_status()
        with self.lock:
            previous, attempted_write = None, False
            try:
                with self._state(runtime) as (state, _):
                    self._expect(state, expected_revision)
                    self._session(enrollment_id, state, "enrollment")
                    self._safe_path(self.profile_path)
                    if self.profile_path.exists():
                        with self.profile_path.open("rb") as stream:
                            previous = stream.read(MAX_PROFILE_BYTES + 1)
                        if len(previous) > MAX_PROFILE_BYTES:
                            raise OwnerVoiceError("The previous protected profile exceeds its size limit.")
                    attempted_write = True
                    self._write_profile(encrypted)
                    state.update(profile_id=profile_id, profile_digest=hashlib.sha256(encrypted).hexdigest(),
                                 profile_binding=profile["binding"], model_key=profile["model_key"],
                                 samples=REQUIRED_SAMPLES, enabled=False, validation=_pending_validation(),
                                 other_check_deferred=False,
                                 storage_failed=False, revision=state["revision"] + 1)
                    result = {"status": self._status(state, templates), "committed": True}
            except Exception as error:
                if attempted_write:
                    try:
                        if previous is None:
                            self._safe_path(self.profile_path)
                            self.profile_path.unlink(missing_ok=True)
                        else:
                            self._write_profile(previous)
                    except (OSError, OwnerVoiceError):
                        # A file/metadata mismatch is always unusable on the next read.
                        pass
                self.profile_cache = None
                if isinstance(error, (OwnerVoiceError, ConflictError)):
                    raise
                raise OwnerVoiceError("The encrypted profile commit failed; automatic handling remains disabled.") from None
            self._clear_sessions()
            self.profile_cache = None
            return result

    def _cancel(self, identity, kind):
        if not isinstance(identity, str) or len(identity) > 128:
            raise OwnerVoiceError("Choose an existing owner voice capture.")
        with self._state() as (state, templates):
            if identity not in self.sessions:
                return {"status": self._status(state, templates), "cancelled": False}
            item = self._session(identity, state, kind)
            if item["done"] or self.active != identity:
                return {"status": self._status(state, templates), "cancelled": False}
            state["validation"] = dict(item["previous"]["validation"])
            state["other_check_deferred"] = item["previous"]["other_check_deferred"]
            restore = item["previous"]["enabled"]
            self._clear_sessions()
            state["revision"] += 1
            state["enabled"] = bool(restore and self._eligible(state, templates, require_fresh=False))
            return {"status": self._status(state, templates), "cancelled": True}

    def enroll_cancel(self, enrollment_id):
        return self._cancel(enrollment_id, "enrollment")

    def test_start(self, case, expected_revision, input_mode="standard"):
        if case not in CASES:
            raise OwnerVoiceError("Choose one of the five explicit owner-labelled voice checks.")
        if input_mode not in ("standard", "unprocessed") or (input_mode == "unprocessed" and case != "owner"):
            raise OwnerVoiceError("Unprocessed input is available only for the manual owner comparison.")
        runtime = self._runtime_status()
        with self._state(runtime) as (state, templates):
            self._expect(state, expected_revision)
            if not templates or state["profile_binding"] != self.runtime["binding"]:
                raise OwnerVoiceError("Enroll an owner profile with the current local model first.")
            item = self._start_session(state, "test", case)
            item["input_mode"] = input_mode
            return {"status": self._status(state, templates),
                    "test": {"id": item["id"], "case": case, "input_mode": input_mode}}

    @staticmethod
    def _result(decision, reason, revision, **extra):
        return {"decision": decision, "reason": reason, "profile_revision": revision, **extra}

    def _score(self, evidence, templates, revision):
        extra = {key: evidence[key] for key in ("duration", "voiced_seconds") if key in evidence}
        if evidence["reason"] != "clean_speech":
            return self._result(evidence["decision"], evidence["reason"], revision, **extra)
        vectors = evidence["vectors"]
        reference=evidence["reference"]
        if reference is None:
            return self._result("overlap", "inconsistent_speakers", revision, **extra)
        within=min(_cosine(vector,reference) for vector in vectors)
        matching={"within_recording_min":round(within,6),"within_recording_threshold":WINDOW_CONSISTENCY}
        if within<WINDOW_CONSISTENCY:
            return self._result("overlap", "inconsistent_speakers", revision,
                                failure_stage="window_to_recording",matching=matching,**extra)
        try:
            prototype = _prototype(templates)
        except (ValueError, TypeError, IndexError):
            return self._result("unavailable", "invalid_profile", revision, **extra)
        reference_score=_cosine(reference,prototype)
        window_score=min(_cosine(vector,prototype) for vector in vectors)
        score=min(reference_score,window_score)
        matching.update(reference_similarity_min=round(reference_score,6),
                        window_similarity_min=round(window_score,6),
                        reference_scores=[round(_cosine(reference, template), 6) for template in templates])
        failure_stage=("recording_to_profile" if reference_score<COSINE_THRESHOLD
                       else "window_to_profile" if window_score<COSINE_THRESHOLD else None)
        return self._result("owner" if score >= COSINE_THRESHOLD else "unknown",
                            "matched" if score >= COSINE_THRESHOLD else "speaker_mismatch",
                            revision, similarity=round(score, 6), threshold=COSINE_THRESHOLD,
                            window_count=len(vectors), failure_stage=failure_stage,
                            matching=matching, matching_policy=MATCHING_POLICY,
                            reference_kind="main_voice", **extra)

    @staticmethod
    def _check_diagnostics(value, pcm_bytes, result):
        values=value if isinstance(value,dict) else {}
        diagnostics={"duration_seconds":round(len(pcm_bytes)/(SAMPLE_RATE*2),2)}
        for source,target in (("voiced_seconds","speech_seconds"),
                              ("overlap_seconds","overlap_seconds"),
                              ("uncertain_seconds","uncertain_seconds")):
            number=values.get(source)
            diagnostics[target]=round(number,2) if _number(number,0,MAX_AUDIO_SECONDS) else None
        windows=values.get("windows")
        diagnostics["analysis_windows"]=len(windows) if isinstance(windows,list) and len(windows)<=MAX_WINDOWS else None
        quality=values.get("quality")
        if isinstance(quality,dict):
            for source,target in (("rms","input_rms"),("clipped_fraction","clipped_fraction")):
                number=quality.get(source)
                if _number(number,0,1):
                    diagnostics[target]=round(number,6)
        segmentation=values.get("segmentation")
        if isinstance(segmentation,dict):
            diagnostics["segmentation"]={}
            for key in ("strong_overlap","distinct_speaker_slots"):
                if type(segmentation.get(key)) is bool:
                    diagnostics["segmentation"][key]=segmentation[key]
            maximum=segmentation.get("maximum_overlap_posterior")
            if _number(maximum,0,1):
                diagnostics["segmentation"]["maximum_overlap_posterior"]=round(maximum,6)
        review=values.get("echo_review")
        if isinstance(review,dict):
            diagnostics["echo_review"]={}
            for key in ("attempted","resolved"):
                if type(review.get(key)) is bool:
                    diagnostics["echo_review"][key]=review[key]
            for key in ("raw_overlap_seconds","raw_uncertain_seconds"):
                if _number(review.get(key),0,MAX_AUDIO_SECONDS):
                    diagnostics["echo_review"][key]=round(review[key],2)
        for key in ("similarity","threshold"):
            number=result.get(key)
            if _number(number,-1,1):
                diagnostics[key]=round(number,6)
        matching=result.get("matching")
        if isinstance(matching,dict):
            diagnostics["matching"]={key:round(matching[key],6) for key in (
                "within_recording_min","within_recording_threshold",
                "reference_similarity_min","window_similarity_min")
                if _number(matching.get(key),-1,1)}
            scores=matching.get("reference_scores")
            if (isinstance(scores,list) and len(scores)<=REQUIRED_SAMPLES
                    and all(_number(score,-1,1) for score in scores)):
                diagnostics["matching"]["reference_scores"]=[round(score,6) for score in scores]
        if result.get("matching_policy") == MATCHING_POLICY:
            diagnostics["matching_policy"]=MATCHING_POLICY
        if result.get("reference_kind") in ("main_voice","short_voice","short_greeting"):
            diagnostics["reference_kind"]=result["reference_kind"]
        groups=result.get("profile_scores")
        if isinstance(groups,dict):
            diagnostics["profile_scores"]={key:round(groups[key],6)
                for key in ("main_voice","short_voice") if _number(groups.get(key),-1,1)}
        if result.get("failure_stage") in ("window_to_recording","recording_to_profile","window_to_profile"):
            diagnostics["failure_stage"]=result["failure_stage"]
        return diagnostics

    @staticmethod
    def _check_detail(case, passed, result, diagnostics):
        if passed:
            return ("Your voice matched the saved profile." if case=="owner" else
                    "The recorded speech was ignored, as expected for this check.")
        if case!="owner" and result["decision"]=="owner":
            return "This audio matched your voice profile, so this check did not pass. Automatic voice stays disabled."
        reason=result["reason"]
        if reason=="insufficient_speech" and diagnostics["speech_seconds"] is not None:
            detail=(f"Only {diagnostics['speech_seconds']:.2f} seconds of speech were detected. "
                    f"This check needs at least {MIN_SPEECH_SECONDS} seconds of clear speech.")
            if case in ("media","overlap","playback"):
                detail+=" The result is inconclusive. Use continuous spoken audio through speakers during this check."
        elif reason=="ambiguous_speech" and diagnostics["uncertain_seconds"]:
            detail=(("The audio was ignored. This validation check is inconclusive: " if case!="owner" else "")+
                    f"the local detector was uncertain about {diagnostics['uncertain_seconds']:.2f} "
                    "seconds of this recording, so speaker matching did not run.")
        elif reason=="speaker_mismatch" and diagnostics.get("failure_stage")=="window_to_profile":
            detail=("The complete recording matched your saved voice, but one or more shorter sections did not. "
                    "The whole check therefore did not pass.")
        elif case=="owner" and reason=="speaker_mismatch" and diagnostics.get("failure_stage")=="recording_to_profile":
            detail=("This recording passed the speech checks, but did not match your saved voice profile. "
                    "If you were the only speaker, re-enroll using this microphone.")
        else:
            detail={
                "speaker_mismatch":"This recording did not match your saved voice closely enough. Try the check again with the same microphone and your natural voice.",
                "poor_quality":"The recording was too quiet, clipped, or unclear for a reliable check.",
                "multiple_or_overlapping_speakers":"The local detector flagged possible mixed speech. This can be a false detection; it does not prove another person was speaking.",
                "inconsistent_speakers":"Parts of this recording did not match one consistent voice closely enough.",
                "ambiguous_speech":"The local detector could not classify parts of this recording clearly enough to make a voice match.",
                "incomplete_speech_coverage":"The local detector could not analyze all the speech in this recording.",
                "runtime_unavailable":"The local voice checker was unavailable. Refresh its status before trying again.",
                "busy":"The local voice checker was busy. Let the current check finish before trying again.",
            }.get(reason,"The local voice checker could not complete this recording's analysis.")
        return detail+" Your saved voice profile is unchanged; automatic voice stays disabled."

    def test_finish(self, test_id, sample_key, pcm_bytes, sample_rate=SAMPLE_RATE):
        digest = self._pcm(pcm_bytes, sample_rate)
        runtime = self._runtime_status()
        with self._state(runtime) as (state, templates):
            item = self._session(test_id, state, "test")
            cached = self._reserve(item, sample_key, digest)
            if cached is not None:
                return {"status": self._status(state, templates), "result": cached}
            if item["done"]:
                item["attempts"].pop(sample_key, None)
                raise ConflictError("This test already consumed its clip. Start a new labelled test.")
            model_key = state["model_key"]
        value, runtime, error = self._analyze(pcm_bytes)
        evidence = self._evidence(value, pcm_bytes, model_key, MIN_SPEECH_SECONDS)
        with self._state(runtime,include_db=True) as (state, templates, db):
            item = self._session(test_id, state, "test")
            result = self._score(evidence, templates, state["revision"])
            if error:
                result = self._result("unavailable", error, state["revision"])
            passed = bool(evidence["valid_speech"] and not error and (
                result["decision"] == "owner" if item["case"] == "owner"
                else result["decision"] in ("unknown", "overlap")
            ))
            state["validation"][item["case"]] = "passed" if passed else "failed"
            if item["case"] == "other":
                # A completed other-speaker check supersedes its deferral.
                # Rechecking the owner or media does not revoke that choice.
                state["other_check_deferred"] = False
            state["enabled"] = False
            state["revision"] += 1
            self.active = None
            item["done"], item["revision"] = True, state["revision"]
            diagnostics=self._check_diagnostics(value,pcm_bytes,result)
            public = {**result, "case": item["case"], "passed": passed,
                      "input_mode": item.get("input_mode", "standard"),
                      "profile_revision": state["revision"],
                      "checked_at":self.clock(),"diagnostics":diagnostics,
                      "detail":self._check_detail(item["case"],passed,result,diagnostics)}
            if item["case"]=="owner":
                diagnostic=self.diagnostics.capture_locked(db,state["owner"],pcm_bytes,
                    {"accepted":passed,"reason":result["reason"],"diagnostics":diagnostics},
                    purpose="owner_check",started_at=item.get("started_at"))
                if diagnostic["state"]!="off":
                    public["diagnostic"]=diagnostic
                    if diagnostic["state"]=="stored":
                        public["detail"]+=" One diagnostic clip was saved encrypted on this PC under your one-time consent."
                    elif diagnostic["state"]=="not_saved":
                        public["detail"]+=" The diagnostic copy could not be saved; no unencrypted copy was kept."
                    elif diagnostic["state"]=="expired":
                        public["detail"]+=" The one-time diagnostic consent expired; no recording was kept."
                    elif diagnostic["state"]=="needs_review":
                        public["detail"]+=" An earlier diagnostic file needs cleanup review; this recording was not saved."
            # Retain one bounded explanation per labelled check, never audio or
            # templates. Another check must not erase the owner failure reason.
            encoded=json.dumps({"owner":state["owner"],"profile_id":state["profile_id"],
                                "profile_binding":state["profile_binding"],
                                "decision_binding":self.runtime.get("decision_binding"),
                                "result":public},allow_nan=False)
            if item["case"]=="owner":
                # Preserve an existing result from before per-input summaries
                # were introduced. Keep two bounded records, never audio.
                previous=db.execute("SELECT value FROM settings WHERE key=?",
                    ("owner_voice_last_validation_check:owner",)).fetchone()
                if previous:
                    try:
                        older=json.loads(previous[0])
                        old_mode=older["result"].get("input_mode","standard")
                        if old_mode in ("standard","unprocessed"):
                            db.execute("INSERT OR REPLACE INTO settings(key,value) VALUES(?,?)",
                                ("owner_voice_last_validation_check:owner:"+old_mode,previous[0]))
                    except (ValueError,KeyError,TypeError):
                        pass
                db.execute("INSERT OR REPLACE INTO settings(key,value) VALUES(?,?)",
                    ("owner_voice_last_validation_check:owner:"+public["input_mode"],encoded))
            db.executemany("INSERT OR REPLACE INTO settings(key,value) VALUES(?,?)",(
                ("owner_voice_last_validation_check",encoded),
                ("owner_voice_last_validation_check:"+item["case"],encoded),
            ))
            item["attempts"][sample_key]["result"] = public
            return {"status": self._status(state, templates), "result": copy.deepcopy(public)}

    def test_cancel(self, test_id):
        return self._cancel(test_id, "test")

    def enable(self, expected_revision, consent, *, defer_other_check=False):
        if consent is not True:
            raise OwnerVoiceError("Explicit consent is required to enable acoustic personalization.")
        if type(defer_other_check) is not bool:
            raise OwnerVoiceError("Deferring the other-person check requires an explicit boolean choice.")
        runtime = self._runtime_status()
        with self._state(runtime) as (state, templates):
            self._expect(state, expected_revision)
            candidate = {**state, "other_check_deferred": defer_other_check}
            if not self._eligible(candidate, templates):
                raise OwnerVoiceError("All five real labelled checks must pass, or explicitly defer only the untested other-person check after the other four pass.")
            state["other_check_deferred"] = defer_other_check
            state["enabled"] = True
            state["revision"] += 1
            self._clear_sessions()
            return {"status": self._status(state, templates), "enabled": True}

    def disable(self, expected_revision):
        with self._state() as (state, templates):
            self._expect(state, expected_revision)
            state["enabled"] = False
            state["revision"] += 1
            self._clear_sessions()
            return {"status": self._status(state, templates), "enabled": False}

    def delete(self, expected_revision, confirm):
        if confirm is not True:
            raise OwnerVoiceError("Confirm deletion of the local owner voice profile.")
        with self._state(include_db=True) as (state, _, db):
            self._expect(state, expected_revision)
            binding, available = state["runtime_binding"], state["runtime_available"]
            replacement = self._default(state["owner"], state["revision"] + 1)
            replacement["runtime_binding"] = binding
            replacement["runtime_available"] = available
            state.update(replacement)
            self._clear_sessions()
            self.profile_cache = None
            db.execute("DELETE FROM settings WHERE key IN ('owner_voice_last_enrollment_check','owner_voice_last_validation_check') OR key LIKE 'owner_voice_last_validation_check:%'")
            db.execute("DELETE FROM settings WHERE key IN ('owner_voice_greeting_templates','owner_voice_last_greeting_check','owner_voice_short_profile_scope','owner_voice_short_profile_candidate')")
            self.diagnostics.clear_owner_locked(db,state["owner"])
            try:
                self._safe_path(self.profile_path)
                self.profile_path.unlink(missing_ok=True)
            except OSError:
                raise OwnerVoiceError("The profile was disabled, but its encrypted file could not be removed.") from None
            return {"status": self._status(state, None), "deleted": True}

    def verify(self, pcm_bytes, *, expected_revision=None, require_enabled=True):
        return self._verify(pcm_bytes, expected_revision=expected_revision, require_enabled=require_enabled)

    def verify_utterance(self, pcm_bytes, *, expected_revision=None, require_enabled=True):
        """Match speech against the owner's main and explicitly authorized short profiles."""
        short = isinstance(pcm_bytes, bytes) and len(pcm_bytes) < MIN_SPEECH_SECONDS * SAMPLE_RATE * 2
        if not short:
            result = self.verify(pcm_bytes, expected_revision=expected_revision, require_enabled=require_enabled)
            if (result["reason"] != "insufficient_speech" or not isinstance(pcm_bytes, bytes)
                    or len(pcm_bytes) > MAX_SHORT_AUDIO_SECONDS * SAMPLE_RATE * 2):
                return result
        result = self._verify(pcm_bytes, expected_revision=expected_revision, require_enabled=require_enabled,
                              short_utterance=True)
        return {**result, "short_utterance": True}

    def verify_greeting(self, pcm_bytes, *, expected_revision):
        """Short evidence can only nominate a greeting, never an instruction."""
        if not isinstance(pcm_bytes, bytes) or len(pcm_bytes) > 4 * SAMPLE_RATE * 2:
            return self._result("unknown", "invalid_audio", self.revision())
        result = self._verify(pcm_bytes, expected_revision=expected_revision, greeting_only=True)
        if result["decision"] == "owner":
            result = {**result, "decision": "greeting_candidate"}
        return result

    def _verify(self, pcm_bytes, *, expected_revision=None, require_enabled=True,
                greeting_only=False, short_utterance=False):
        minimum = MIN_SHORT_SPEECH_SECONDS if short_utterance else 0.6 if greeting_only else MIN_SPEECH_SECONDS
        # Unenabled scoring exists only inside test_finish, with a bound session.
        if require_enabled is not True:
            return self._result("unavailable", "explicit_test_required", self.revision())
        try:
            self._pcm(pcm_bytes)
        except OwnerVoiceError:
            return self._result("unknown", "invalid_audio", self.revision())
        if len(pcm_bytes) < minimum * SAMPLE_RATE * 2:
            with self._state() as (state, _):
                if expected_revision is not None and (
                    type(expected_revision) is not int or expected_revision != state["revision"]
                ):
                    return self._result("unavailable", "profile_changed", state["revision"])
                return self._result("unknown", "insufficient_speech", state["revision"])
        runtime = self._runtime_status()
        with self._state(runtime) as (state, templates):
            revision = state["revision"]
            if expected_revision is not None and (type(expected_revision) is not int or expected_revision != revision):
                return self._result("unavailable", "profile_changed", revision)
            if not state["enabled"] or not self._admitted_profile(state, templates):
                return self._result("unavailable", "profile_not_enabled", revision)
            if not self._eligible(state, templates):
                return self._result("unavailable", "runtime_unavailable", revision)
            owner, profile_id, binding = state["owner"], state["profile_id"], state["profile_binding"]
            model_key = state["model_key"]
        value, runtime, error = (self._analyze(pcm_bytes, short_utterance=True) if short_utterance
                                 else self._analyze(pcm_bytes, greeting_only=True) if greeting_only
                                 else self._analyze(pcm_bytes))
        evidence = self._evidence(value, pcm_bytes, model_key, minimum)
        if bool(isinstance(value, dict) and value.get("greeting_only")) != greeting_only:
            error = "invalid_analysis"
        if (short_utterance and (not isinstance(value, dict) or value.get("short_utterance") is not True
                                or value.get("short_utterance_policy_version") != SHORT_UTTERANCE_POLICY)
                or not short_utterance and isinstance(value, dict) and value.get("short_utterance") not in (None, False)):
            error = "invalid_analysis"
        with self._state(runtime) as (state, templates):
            if (state["revision"] != revision or state["owner"] != owner or state["profile_id"] != profile_id
                    or state["profile_binding"] != binding or not state["enabled"]
                    or not self._eligible(state, templates)):
                return self._result("unavailable", "profile_changed", state["revision"])
            if error:
                return self._result("unavailable", error, state["revision"])
            result = self._score(evidence, templates, revision)
            if short_utterance and result["reason"] == "speaker_mismatch":
                short_templates = self.greetings.read_for_voice(state)
                if short_templates:
                    main_score = result.get("similarity")
                    result = self.greetings.score(evidence, short_templates, revision)
                    result.update(reference_kind="short_voice", general_short_profile=True,
                                  profile_scores={"main_voice":main_score,
                                                  "short_voice":result.get("similarity")})
            if greeting_only and result["reason"] == "speaker_mismatch":
                from greeting_profile import ANALYSIS_POLICY
                greetings = self.greetings.read(state)
                if greetings and value.get("greeting_policy_version") == ANALYSIS_POLICY:
                    result = self.greetings.score(evidence, greetings, revision)
                elif not greetings:
                    result["reason"] = "short_greeting_setup_needed"
            # Aggregate local diagnostics only: no audio or voice vectors.
            return {**result, "diagnostics": self._check_diagnostics(value, pcm_bytes, result)}

    def close(self):
        with self.lock:
            if self.closed.is_set():
                return
            self.closed.set()
            # Capture start already persisted disabled state. A normal close
            # releases process resources without changing the owner's choice.
            self._clear_sessions()
            self.profile_cache = None
        self.worker.close()
        probe = self.runtime_probe_thread
        if probe and probe is not threading.current_thread():
            probe.join(timeout=2)
