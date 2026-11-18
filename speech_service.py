"""Private AWS speech worker with real run receipts and exact route selection."""
from __future__ import annotations

import base64
import hashlib
import json
import os
from pathlib import Path
import queue
import re
import shutil
import sqlite3
import struct
import subprocess
import threading
import time
import uuid

from relay import redact
from companion import ConflictError

ROOT = Path(__file__).resolve().parent
ROUTE_FIELDS = ("transcriptionLanguage", "speechModelId", "voice", "awsRegion", "awsProfile")
TEST_PHRASE = "Hello. This is a Vega voice test."


def is_short_greeting(text):
    # Full match: an appended request must never inherit short-clip admission.
    return isinstance(text, str) and re.fullmatch(
        r"\s*(?:hi|high|hey|hello)[\s,]+vega[.!?,\s]*", text, re.IGNORECASE
    ) is not None


class SpeechError(RuntimeError):
    pass


class SpeechWorker:
    def __init__(self):
        self.process = None
        self.lock = threading.RLock()
        self.pending = {}

    def _read(self, process):
        try:
            while True:
                line = process.stdout.readline(18 * 1024 * 1024 + 1)
                if not line:
                    break
                if len(line) > 18 * 1024 * 1024:
                    break
                data = json.loads(line)
                with self.lock:
                    pending = self.pending.get(data.get("id"))
                if pending is not None and pending[0] is process:
                    pending[1].put(data)
        except (OSError, ValueError):
            pass
        finally:
            with self.lock:
                for owner_process, channel in self.pending.values():
                    if owner_process is process:
                        channel.put({"ok": False, "error": {"message": "The voice worker stopped. Start a new voice turn."}})

    def _start(self):
        if self.process and self.process.poll() is None:
            return
        executable = shutil.which("node.exe" if os.name == "nt" else "node")
        if not executable:
            raise SpeechError("The installed Node.js speech runtime is unavailable.")
        self.process = subprocess.Popen(
            [executable, "--disable-warning=ExperimentalWarning", str(ROOT / "speech-worker.mjs")],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            cwd=ROOT, shell=False, creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        )
        threading.Thread(target=self._read, args=(self.process,), daemon=True).start()

    def call(self, command, payload, config=None, *, timeout=175, request_id=None):
        identity = request_id or str(uuid.uuid4())
        channel = queue.Queue()
        with self.lock:
            self._start()
            self.pending[identity] = (self.process, channel)
            try:
                self.process.stdin.write(json.dumps({"id": identity, "command": command, "payload": payload, "config": config}).encode("utf-8") + b"\n")
                self.process.stdin.flush()
            except (OSError, BrokenPipeError):
                self.pending.pop(identity, None)
                raise SpeechError("The voice worker connection ended.") from None
        try:
            result = channel.get(timeout=timeout)
            if not result.get("ok"):
                raise SpeechError(redact(result.get("error", {}).get("message", "The voice operation failed.")))
            return result.get("value")
        except queue.Empty:
            raise SpeechError("The voice operation timed out. No alternate speech route was used.") from None
        finally:
            with self.lock:
                self.pending.pop(identity, None)

    def close(self):
        with self.lock:
            if self.process:
                try:
                    self.process.stdin.close()
                    self.process.wait(timeout=3)
                except (OSError, subprocess.TimeoutExpired):
                    self.process.kill()
                    self.process.wait(timeout=3)
                self.process.stdout.close()
                self.process = None


class SpeechService:
    def __init__(self, store, *, worker=None, owner_voice=None, clock=time.time):
        self.store, self.clock = store, clock
        self.worker = worker or SpeechWorker()
        self.owner_voice = owner_voice
        self.config_path = store.directory / "speech-config.json"
        self.lock = threading.RLock()
        self.captures = {}
        self.transcripts = {}
        self.route_proposals = {}
        self.route_tests = {}
        self._catalog = None
        self.last_voice_check = None
        with store.db() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS speech_runs (
                    id TEXT PRIMARY KEY, owner INTEGER NOT NULL, source TEXT NOT NULL,
                    command TEXT NOT NULL, route_hash TEXT NOT NULL,
                    state TEXT NOT NULL, started_at REAL NOT NULL,
                    finished_at REAL, error TEXT
                );
                CREATE TABLE IF NOT EXISTS voice_inbox (
                    update_id INTEGER PRIMARY KEY, message_id INTEGER NOT NULL,
                    owner INTEGER NOT NULL, file_id TEXT NOT NULL, duration INTEGER NOT NULL,
                    state TEXT NOT NULL, received_at REAL NOT NULL, error TEXT
                );
            """)
            db.execute("UPDATE speech_runs SET state='interrupted',error='Host stopped; speech not replayed' WHERE state IN ('running','streaming')")
            db.execute("UPDATE voice_inbox SET state='interrupted',error='Host stopped during transcription; not replayed' WHERE state='transcribing'")

    def config(self):
        if not self.config_path.exists():
            raise SpeechError("The existing speech route has not been migrated yet.")
        try:
            value = json.loads(self.config_path.read_text("utf-8"))
        except (OSError, ValueError):
            raise SpeechError("The saved speech route could not be read.") from None
        expected = {
            "providerId": "aws", "transcriptionModelId": "amazon-transcribe-streaming",
        }
        if (
            not isinstance(value, dict)
            or any(value.get(key) != expected_value for key, expected_value in expected.items())
            or value.get("speechModelId") not in ("amazon-polly-neural", "amazon-polly-generative")
            or value.get("transcriptionLanguage") not in ("en-US", "fr-FR", "it-IT", "ar-SA", "ar-AE", "auto")
            or not isinstance(value.get("awsRegion"), str) or not re.fullmatch(r"[a-z]{2}-[a-z]+-\d", value["awsRegion"])
            or not isinstance(value.get("awsProfile"), str) or not re.fullmatch(r"[\w@.+-]{1,128}", value["awsProfile"])
            or not isinstance(value.get("voice"), str) or not re.fullmatch(r"[A-Z][A-Za-z]{1,40}", value["voice"])
        ):
            raise SpeechError("The saved speech route is invalid. No substitute route was selected.")
        return {key: value.get(key) for key in ("version", "providerId", "transcriptionModelId", "transcriptionLanguage", "speechModelId", "voice", "autoSpeak", "awsRegion", "awsProfile")}

    @staticmethod
    def route_hash(config):
        return hashlib.sha256(json.dumps(config, sort_keys=True).encode()).hexdigest()

    def _capture_activity(self):
        """Expire unused local leases without cancelling an in-flight provider or analysis."""
        now = self.clock()
        with self.lock:
            expired = []
            for identity, item in self.captures.items():
                deadline = item.get("expires")
                if type(deadline) not in (int, float) or not deadline <= now:
                    continue
                # A started provider or a finishing local verifier can still
                # own resources. Status reads must not interrupt either.
                unused = item.get("started") is False
                local = item.get("buffered") is True and item.get("provider_started") is False
                if item.get("finishing") is False and (unused or local):
                    expired.append(identity)
            for identity in expired:
                item = self.captures.pop(identity)
                if isinstance(item.get("audio"), bytearray):
                    item["audio"].clear()
            return {
                "live_capture_active": bool(self.captures),
                "expired_capture_count": sum(
                    type(item.get("expires")) in (int, float) and item["expires"] <= now
                    for item in self.captures.values()),
                "capture_status_version": 2,
            }

    def status(self):
        try:
            config = self.config()
            saved = self.store.get_many(("speech_error", "speech_verified_route"))
            error = saved.get("speech_error")
            ready = saved.get("speech_verified_route") == self.route_hash(config)
            with self.lock:
                recent = self.last_voice_check
                if recent and (recent["owner"] != self.store.get("owner_id")
                               or recent["route"] != self.route_hash(config)):
                    recent = None
                check = {key: value for key, value in recent.items()
                         if key not in ("owner", "route")} if recent else None
            return {"configured": True, "verified": ready, "route": config, "revision": self.route_hash(config), "route_controls_available": True, "error": error, **self._capture_activity(),
                    "last_voice_check": check}
        except SpeechError as error:
            return {"configured": False, "verified": False, "route": None, "revision": None, "route_controls_available": True, "error": str(error), **self._capture_activity()}

    def _owner(self):
        owner = self.store.get("owner_id")
        if type(owner) is not int:
            raise SpeechError("Pair the owner before changing voice settings.")
        return owner

    def catalog(self):
        if self._catalog is None:
            self._catalog = self.worker.call("voice_catalog", {}, timeout=15)
        return self._catalog

    def route_info(self):
        self._owner()
        return {"speech": self.status(), "catalog": self.catalog(), "testPhrase": TEST_PHRASE}

    def _check_revision(self, expected, config=None):
        config = config or self.config()
        if not isinstance(expected, str) or expected != self.route_hash(config):
            raise ConflictError("The saved voice route changed. Reload it and review the current values.")
        return config

    def propose_config(self, values, expected_revision):
        owner = self._owner()
        current = self._check_revision(expected_revision)
        if not isinstance(values, dict) or set(values) != set(ROUTE_FIELDS):
            raise SpeechError("Review the complete voice route form.")
        if any(not isinstance(value, str) or not value.strip() or "\0" in value for value in values.values()):
            raise SpeechError("Choose a value for each voice route field.")
        candidate = {**current, **{key: value.strip() for key, value in values.items()}}
        catalog = self.catalog()
        model = next((entry for entry in catalog["speechModels"] if entry["modelId"] == candidate["speechModelId"]), None)
        if not model or candidate["voice"] not in model["voices"]:
            raise SpeechError("Choose a voice supported by the selected speech model.")
        if candidate["transcriptionLanguage"] not in catalog["languages"]:
            raise SpeechError("Choose a supported transcription language.")
        if not re.fullmatch(r"[a-z]{2}-[a-z]+-\d", candidate["awsRegion"]):
            raise SpeechError("Enter a valid AWS region.")
        if model["regions"] and candidate["awsRegion"] not in model["regions"]:
            raise SpeechError("That speech model is not available in the selected region.")
        if not re.fullmatch(r"[\w@.+-]{1,128}", candidate["awsProfile"]):
            raise SpeechError("Enter an existing AWS CLI profile name.")
        self.worker.call("validate_route", {}, candidate, timeout=15)
        with self.lock:
            self._check_revision(expected_revision)
            if owner != self._owner():
                raise ConflictError("The owner changed before this route could be reviewed.")
            self.route_proposals = {key: item for key, item in self.route_proposals.items() if item["expires"] > self.clock()}
            if len(self.route_proposals) >= 20:
                raise SpeechError("Finish or close an existing route review before opening another.")
            identity = str(uuid.uuid4())
            self.route_proposals[identity] = {
                "owner": owner, "before_revision": expected_revision,
                "candidate": dict(candidate), "expires": self.clock() + 300,
            }
        return {
            "proposal_id": identity, "before": dict(current), "after": dict(candidate),
            "changes": [{"field": key, "before": current[key], "after": candidate[key]} for key in ROUTE_FIELDS if current[key] != candidate[key]],
            "expires_at": int((self.clock() + 300) * 1000),
        }

    def discard_proposal(self, identity):
        with self.lock:
            item = self.route_proposals.get(identity)
            if item and item["owner"] == self._owner():
                self.route_proposals.pop(identity, None)
        return {"discarded": True}

    def commit_config(self, identity):
        temporary = None
        with self.lock:
            proposal = self.route_proposals.get(identity)
            if not proposal or proposal["owner"] != self._owner() or proposal["expires"] <= self.clock():
                raise ConflictError("This route review expired or was closed. Review the form again.")
            self._check_revision(proposal["before_revision"])
            candidate = dict(proposal["candidate"])
            if self.route_hash(candidate) == proposal["before_revision"]:
                self.route_proposals.pop(identity, None)
                return {"saved": False, "unchanged": True, "speech": self.status()}
            try:
                temporary = self.config_path.with_name("speech-config." + uuid.uuid4().hex + ".tmp")
                with temporary.open("x", encoding="utf-8") as stream:
                    json.dump(candidate, stream, ensure_ascii=False, indent=2)
                self._check_revision(proposal["before_revision"])
                if proposal["owner"] != self._owner():
                    raise ConflictError("The owner changed during this route review.")
                temporary.replace(self.config_path)
                temporary = None
            except OSError:
                raise SpeechError("The reviewed route could not be saved. The previous route was preserved.") from None
            finally:
                if temporary:
                    temporary.unlink(missing_ok=True)
            captures = list(self.captures)
            tests = [key for key, item in self.route_tests.items() if item["state"] in ("prepared", "running")]
            self.captures.clear()
            self.transcripts.clear()
            self.route_proposals.clear()
            for key in tests:
                self.route_tests[key]["state"] = "cancelled"
        active_ids = set(captures + tests)
        try:
            with self.store.db() as db:
                db.execute("BEGIN IMMEDIATE")
                active_ids.update(row["id"] for row in db.execute(
                    "SELECT id FROM speech_runs WHERE owner=? AND route_hash=? AND state IN ('running','streaming')",
                    (proposal["owner"], proposal["before_revision"]),
                ))
                db.execute(
                    "UPDATE speech_runs SET state='cancelled',finished_at=? WHERE owner=? AND route_hash=? AND state IN ('running','streaming')",
                    (self.clock(), proposal["owner"], proposal["before_revision"]),
                )
        except sqlite3.Error:
            pass  # Every late result still revalidates the route before use.
        for key in active_ids:
            try:
                self.worker.call("cancel", {"id": key}, timeout=10)
            except SpeechError:
                pass  # Changed route checks still discard every old callback.
        try:
            self.store.set("speech_error", None)
        except sqlite3.Error:
            pass  # Clearing a cached diagnostic cannot roll back an approved file commit.
        return {
            "saved": True,
            "speech": {"configured": True, "verified": False, "route": candidate,
                       "revision": self.route_hash(candidate), "error": None, "live_capture_active": False},
        }

    def prepare_test(self, kind, expected_revision, preference_revision):
        if kind not in ("connection", "voice"):
            raise SpeechError("Choose a connection check or a voice sample.")
        config = self._check_revision(expected_revision)
        owner = self._owner()
        with self.lock:
            self.route_tests = {key: item for key, item in self.route_tests.items() if item["state"] == "running" or item["expires"] > self.clock()}
            if any(item["state"] in ("prepared", "running") for item in self.route_tests.values()):
                raise SpeechError("Stop the current voice test before starting another.")
            while len(self.route_tests) >= 40:
                self.route_tests.pop(next(iter(self.route_tests)))
            identity = str(uuid.uuid4())
            self.route_tests[identity] = {
                "owner": owner, "config": dict(config), "preference_revision": preference_revision,
                "kind": kind, "state": "prepared", "expires": self.clock() + 180,
            }
        return {"test_id": identity, "kind": kind, "revision": expected_revision}

    def cancel_test(self, identity):
        with self.lock:
            item = self.route_tests.get(identity)
            if not item or item["owner"] != self._owner():
                raise SpeechError("This voice test is unavailable.")
            item["state"] = "cancelled"
        self.worker.call("cancel", {"id": identity}, timeout=10)
        with self.store.db() as db:
            db.execute("UPDATE speech_runs SET state='cancelled',finished_at=? WHERE id=? AND source='owner-route-test' AND state='running'", (self.clock(), identity))
        return {"cancelled": True}

    def execute_test(self, identity, get_preference_revision):
        with self.lock:
            item = self.route_tests.get(identity)
            if not item or item["owner"] != self._owner():
                raise SpeechError("This voice test is unavailable.")
            if item["state"] == "cancelled":
                return {"cancelled": True}
            if item["state"] != "prepared" or item["expires"] <= self.clock():
                raise SpeechError("This voice test ended or expired. Start a new test.")
            self._check_revision(self.route_hash(item["config"]))
            if item["preference_revision"] != get_preference_revision():
                raise ConflictError("Voice preferences changed. Start a new test.")
            item["state"] = "running"
            item["expires"] = self.clock() + 180
        command = "probe" if item["kind"] == "connection" else "synthesize"
        try:
            result = self.run(command, {} if command == "probe" else {"text": TEST_PHRASE},
                              source="owner-route-test", config=item["config"], request_id=identity)
            with self.lock:
                if item["state"] == "cancelled":
                    return {"cancelled": True}
                self._check_revision(self.route_hash(item["config"]))
                if item["owner"] != self._owner() or item["preference_revision"] != get_preference_revision():
                    raise ConflictError("Voice access changed. The test result was discarded.")
                item["state"] = "completed"
            if command == "probe":
                return {"kind": "connection", "test_id": identity, "credentials_available": True,
                        "credential_source": result.get("credentialSource"), "revision": self.route_hash(item["config"])}
            audio = self._decode_audio(result)
            return {"kind": "voice", "test_id": identity, "audio": base64.b64encode(audio).decode(),
                    "mime": "audio/mpeg", "autoplay": False, "revision": self.route_hash(item["config"])}
        except (SpeechError, ConflictError):
            with self.lock:
                if item["state"] == "cancelled":
                    return {"cancelled": True}
                item["state"] = "failed"
            raise

    def run(self, command, payload, *, source, config=None, request_id=None, admission=None):
        config = config or self.config()
        owner = self.store.get("owner_id")
        if type(owner) is not int:
            raise SpeechError("Pair the owner before starting voice.")
        identity = request_id or str(uuid.uuid4())
        now = self.clock()
        def reserve(db):
            if owner != self.store.get("owner_id") or config != self.config():
                raise SpeechError("The voice owner or route changed before admission.")
            count = db.execute(
                "SELECT count(*) FROM speech_runs WHERE owner=? AND started_at>? AND command IN ('synthesize','transcribe','live_begin')",
                (owner, now - 3600),
            ).fetchone()[0]
            if command in ("synthesize", "transcribe", "live_begin") and count >= 120:
                raise SpeechError("The hourly voice limit was reached. Text conversation still works.")
            db.execute("INSERT INTO speech_runs VALUES(?,?,?,?,?,'running',?,NULL,NULL)", (identity, owner, source, command, self.route_hash(config), now))
            return identity
        if admission is None:
            with self.store.db() as db:
                db.execute("BEGIN IMMEDIATE")
                reserve(db)
        else:
            admission(reserve)
        try:
            if config != self.config() or owner != self.store.get("owner_id"):
                raise SpeechError("The voice route or owner changed before this operation. Start a new voice turn.")
            value = self.worker.call(command, payload, config, request_id=identity)
            if config != self.config() or owner != self.store.get("owner_id"):
                raise SpeechError("The voice route or owner changed during this operation. Its result was discarded.")
            with self.store.db() as db:
                db.execute(
                    "UPDATE speech_runs SET state=?,finished_at=? WHERE id=? AND state='running'",
                    ("streaming" if command == "live_begin" else "completed", None if command == "live_begin" else self.clock(), identity),
                )
            self.store.set("speech_error", None)
            return value
        except (SpeechError, OSError, ValueError) as error:
            safe = redact(str(error))
            with self.store.db() as db:
                db.execute("UPDATE speech_runs SET state='failed',finished_at=?,error=? WHERE id=? AND state='running'", (self.clock(), safe, identity))
            self.store.set("speech_error", safe)
            raise SpeechError(safe) from None

    def synthesize(self, text, *, source="local"):
        if not isinstance(text, str) or not text.strip() or len(text) > 16000:
            raise SpeechError("Choose a spoken reply of at most 16,000 characters.")
        result = self.run("synthesize", {"text": text}, source=source)
        return self._decode_audio(result)

    @staticmethod
    def _decode_audio(result):
        try:
            audio = base64.b64decode(result["audio"], validate=True)
        except (ValueError, KeyError, TypeError):
            raise SpeechError("The speech provider returned invalid audio.") from None
        if not 0 < len(audio) <= 8 * 1024 * 1024:
            raise SpeechError("The spoken reply exceeds the playback limit.")
        return audio

    def transcribe(self, audio, *, source="telegram"):
        if not isinstance(audio, bytes) or not 4 <= len(audio) <= 12 * 1024 * 1024:
            raise SpeechError("The voice note exceeds the 12 MB limit.")
        result = self.run("transcribe", {"audio": base64.b64encode(audio).decode()}, source=source)
        transcript = result.get("text")
        if not isinstance(transcript, str) or not transcript.strip() or len(transcript) > 12000:
            raise SpeechError("No usable words were received. Please send a shorter voice note or type your message.")
        return transcript.strip()

    def transcribe_owner_note(self, audio, *, expected_revision, admission_check=None):
        """Verify all of an explicit Telegram voice note locally before ASR."""
        if self.owner_voice is None or not self.owner_voice.automatic_allowed():
            return {"ignored": True, "reason": "Set up and validate your voice in Vega, or type your message."}
        if not isinstance(audio, bytes) or not 4 <= len(audio) <= 12 * 1024 * 1024:
            raise SpeechError("The voice note exceeds the 12 MB limit.")
        executable = shutil.which("ffmpeg")
        if executable is None:
            bundled = ROOT / "node_modules" / "ffmpeg-static" / ("ffmpeg.exe" if os.name == "nt" else "ffmpeg")
            executable = str(bundled) if bundled.is_file() else None
        if executable is None:
            raise SpeechError("Local voice-note decoding is unavailable. Text conversation still works.")
        try:
            decoded = subprocess.run(
                [executable, "-hide_banner", "-loglevel", "error", "-nostdin",
                 "-protocol_whitelist", "pipe", "-f", "ogg", "-i", "pipe:0",
                 "-ac", "1", "-ar", "16000", "-t", "121", "-f", "s16le", "pipe:1"],
                input=audio, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, shell=False,
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0, timeout=25,
            )
        except (OSError, subprocess.TimeoutExpired):
            raise SpeechError("The voice note could not be decoded locally.") from None
        pcm = decoded.stdout
        if decoded.returncode or not pcm or len(pcm) > 120 * 16000 * 2 or len(pcm) % 2:
            raise SpeechError("Use a valid voice note of at most two minutes.")
        chunk, step = 30 * 16000 * 2, 27 * 16000 * 2
        for start in range(0, len(pcm), step):
            segment = pcm[start:start + chunk]
            decision = self.owner_voice.verify_utterance(segment, expected_revision=expected_revision)
            if decision.get("decision") != "owner":
                return {"ignored": True, "reason": "I could not confirm one consistent owner voice. Nothing was submitted as your instruction."}
            if start + chunk >= len(pcm):
                break
        def admit(reserve):
            def checked(db):
                if admission_check is not None:
                    admission_check(db)
                return reserve(db)
            return self.owner_voice.admit_auto(expected_revision, checked)
        result = self.run("transcribe", {"audio": base64.b64encode(audio).decode()},
                          source="owner-verified-telegram", admission=admit)
        if not self.owner_voice.automatic_allowed() or self.owner_voice.revision() != expected_revision:
            return {"ignored": True, "reason": "The voice profile changed. Please start a new voice turn or type."}
        transcript = result.get("text")
        if not isinstance(transcript, str) or not transcript.strip() or len(transcript) > 12000:
            raise SpeechError("No usable words were received. Please type or send a clearer recording.")
        return {"text": transcript.strip(), "origin": "speaker_matched", "speaker_revision": expected_revision}

    def prepare_capture(self, context_revision, preference_revision, context=None, *, mode="automatic"):
        if mode not in ("automatic", "review"):
            raise SpeechError("Choose automatic listening or an explicitly reviewed recording.")
        buffered = mode == "automatic" and self.owner_voice is not None
        speaker_revision = None
        if buffered:
            if not self.owner_voice.automatic_allowed():
                raise SpeechError("Set up and validate your voice before automatic conversation. You can still type or review a recording.")
            speaker_revision = self.owner_voice.revision()
        config = self.config()
        identity = str(uuid.uuid4())
        for expired in [key for key, item in list(self.captures.items()) if item["expires"] <= self.clock()]:
            self.stream("cancel", {"capture_id": expired}, context_revision, preference_revision)
        with self.lock:
            if any(item["started"] for item in self.captures.values()):
                raise SpeechError("Another voice recording is active. Pause it first.")
            self.captures = {identity: {
                "owner": self.store.get("owner_id"), "expires": self.clock() + 120,
                "config": config, "context_revision": context_revision,
                "preference_revision": preference_revision, "started": False,
                "context": context, "mode": mode, "buffered": buffered,
                "speaker_revision": speaker_revision, "provider_started": False,
                "audio": bytearray(), "sequence": 0, "finishing": False,
                "origin": "manual_review" if mode == "review" else "legacy_unverified",
            }}
        return {"captureId": identity, "expiresAt": int((self.clock() + 120) * 1000),
                "buffered": buffered}

    def finish_local_capture(self, payload, pcm, context_revision, preference_revision):
        """Claim one prepared automatic recording; retries cannot create turns."""
        identity = payload.get("capture_id")
        if not isinstance(identity, str) or not pcm or len(pcm) % 2 or len(pcm) > 30 * 16000 * 2:
            raise SpeechError("Use a prepared recording of at most 30 seconds.")
        item = self.capture(identity, context_revision, preference_revision)
        with self.lock:
            if (self.captures.get(identity) is not item or not item.get("buffered")
                    or item["mode"] != "automatic" or item["started"] and not item.get("local_upload")
                    or item["finishing"] or item["audio"]):
                raise SpeechError("This automatic recording was already used or changed.")
            item["started"] = True
            item["local_upload"] = False
            item["audio"].extend(pcm)
        return self._buffered_stream("live_finish", {"sequence": 0}, item, identity,
                                     context_revision, preference_revision)

    def accept_transcript(self, identity, text, preference_revision, *, with_provenance=False):
        with self.lock:
            receipt = self.transcripts.pop(identity, None)
        if (
            not receipt or receipt["text"] != text
            or receipt["owner"] != self.store.get("owner_id")
            or receipt["preference_revision"] != preference_revision
            or receipt["config"] != self.config() or self.clock() >= receipt["expires"]
        ):
            raise SpeechError("The voice transcript or its saved context changed. Start a new voice turn.")
        if receipt.get("origin") == "speaker_matched" and (
            self.owner_voice is None or not self.owner_voice.automatic_allowed()
            or self.owner_voice.revision() != receipt.get("speaker_revision")
        ):
            raise SpeechError("Your voice profile changed before this message. Start a new turn or type it.")
        if with_provenance:
            return {"context": receipt["context"], "origin": receipt.get("origin", "legacy_unverified"),
                    "speaker_revision": receipt.get("speaker_revision"),
                    "greeting_only": receipt.get("greeting_only", False)}
        return receipt["context"]

    def capture(self, identity, context_revision, preference_revision):
        with self.lock:
            item = self.captures.get(identity)
            invalid = (
                not item or item["owner"] != self.store.get("owner_id") or item["config"] != self.config()
                or item["context_revision"] != context_revision or item["preference_revision"] != preference_revision
                or self.clock() >= item["expires"]
            )
            if not invalid:
                return item
            self.captures.pop(identity, None)
        if item and (not item.get("buffered") or item.get("provider_started")):
            try:
                self.worker.call("cancel", {"id": identity}, timeout=10)
            except SpeechError:
                pass
            with self.store.db() as db:
                db.execute("UPDATE speech_runs SET state='cancelled',finished_at=? WHERE id=? AND state='streaming'", (self.clock(), identity))
        raise SpeechError("This capture, context, or voice preference changed. Start a new recording.")

    def _save_transcript(self, item, value):
        transcript = value.get("text")
        if not isinstance(transcript, str) or len(transcript) > 12000:
            raise SpeechError("The live transcript is invalid or exceeds its limit.")
        receipt = str(uuid.uuid4())
        with self.lock:
            self.transcripts = {key: data for key, data in self.transcripts.items() if data["expires"] > self.clock()}
            if len(self.transcripts) >= 20:
                self.transcripts.pop(next(iter(self.transcripts)))
            saved = {key: data for key, data in item.items() if key != "audio"}
            self.transcripts[receipt] = {**saved, "text": transcript, "expires": self.clock() + 120}
        return {**value, "receipt": receipt, "origin": item.get("origin")}

    @staticmethod
    def _pcm_wave(pcm):
        return (b"RIFF" + struct.pack("<I", len(pcm) + 36) + b"WAVEfmt "
                + struct.pack("<IHHIIHH", 16, 1, 1, 16000, 32000, 2, 16)
                + b"data" + struct.pack("<I", len(pcm)) + pcm)

    def _buffered_stream(self, command, payload, item, identity, context_revision, preference_revision):
        if command == "live_begin":
            with self.lock:
                if self.captures.get(identity) is not item or item["started"]:
                    raise SpeechError("This recording was already started.")
                item["started"] = True
                item["local_upload"] = payload.get("local_upload") is True
            return {"buffered_locally": True}
        if not item["started"]:
            raise SpeechError("Begin the prepared recording first.")
        if command == "live_push":
            if item.get("local_upload"):
                raise SpeechError("This recording uses one complete local upload.")
            encoded = payload.get("audio")
            if not isinstance(encoded, str) or len(encoded) > 9000:
                raise SpeechError("Invalid local audio frame.")
            try:
                audio = base64.b64decode(encoded, validate=True)
            except ValueError:
                raise SpeechError("Invalid local audio frame.") from None
            if not audio or len(audio) > 6400 or len(audio) % 2:
                raise SpeechError("Invalid local audio frame.")
            with self.lock:
                if (self.captures.get(identity) is not item or item["finishing"]
                        or type(payload.get("sequence")) is not int or payload["sequence"] != item["sequence"]):
                    raise SpeechError("The local recording sequence changed.")
                if len(item["audio"]) + len(audio) > 30 * 16000 * 2:
                    raise SpeechError("Use automatic voice turns of at most 30 seconds.")
                item["audio"].extend(audio)
                item["sequence"] += 1
            return {"buffered_locally": True}
        if command != "live_finish":
            raise SpeechError("Unsupported local recording operation.")
        with self.lock:
            if (self.captures.get(identity) is not item or item["finishing"]
                    or type(payload.get("sequence")) is not int or payload["sequence"] != item["sequence"]):
                raise SpeechError("The local recording sequence changed.")
            item["finishing"] = True
            pcm = bytes(item["audio"])
            item["audio"].clear()
        try:
            started = time.perf_counter()
            result = self.owner_voice.verify_utterance(pcm, expected_revision=item["speaker_revision"])
            short_utterance = result.get("short_utterance") is True
            greeting_only = False
            greeting_attempted = False
            if (not result.get("general_short_profile") and len(pcm) <= 4 * 16000 * 2 and (result.get("reason") == "insufficient_speech"
                    or result.get("short_utterance") is True and result.get("reason") == "speaker_mismatch")):
                greeting_attempted = True
                candidate = self.owner_voice.verify_greeting(pcm, expected_revision=item["speaker_revision"])
                if candidate.get("decision") == "greeting_candidate":
                    result = {**candidate, "decision": "owner"}
                    greeting_only = True
                else:
                    result = candidate
            self.capture(identity, context_revision, preference_revision)
            check = {
                "owner": item["owner"], "route": self.route_hash(item["config"]),
                "checked_at": self.clock(), "profile_revision": item["speaker_revision"],
                "recording_seconds": round(len(pcm) / 32000, 3),
                "matching_seconds": round(time.perf_counter() - started, 3),
                "greeting_attempted": greeting_attempted,
                "short_utterance": short_utterance,
                "decision": result.get("decision"), "reason": result.get("reason"),
                "diagnostics": result.get("diagnostics"), "phase": "matching",
            }
            with self.lock:
                self.last_voice_check = check
            if result.get("decision") != "owner":
                with self.lock:
                    check["phase"] = "ignored"
                return {"ignored": True, "text": "", "reason": result.get("reason", "speaker_unconfirmed")}
            item["origin"] = "speaker_matched"
            wave = self._pcm_wave(pcm)
            def admit(reserve):
                # Capture cancellation and profile/budget admission are ordered;
                # no locks are retained while AWS processes the admitted audio.
                with self.lock:
                    self.capture(identity, context_revision, preference_revision)
                    def reserved(db):
                        result = reserve(db)
                        item["provider_started"] = True
                        return result
                    return self.owner_voice.admit_auto(item["speaker_revision"], reserved)
            transcribe_started = time.perf_counter()
            try:
                value = self.run(
                    "transcribe", {"audio": base64.b64encode(wave).decode(), "buffered": True},
                    source="owner-verified-live", config=item["config"], request_id=identity, admission=admit,
                )
            except (SpeechError, OSError, ValueError):
                with self.lock:
                    check.update(phase="transcription_failed", transcription_seconds=round(
                        time.perf_counter() - transcribe_started, 3))
                raise
            self.capture(identity, context_revision, preference_revision)
            with self.lock:
                check.update(phase="transcribed", transcription_seconds=round(
                    time.perf_counter() - transcribe_started, 3))
            if not self.owner_voice.automatic_allowed() or self.owner_voice.revision() != item["speaker_revision"]:
                return {"ignored": True, "text": "", "reason": "voice_profile_changed"}
            if greeting_only:
                if not is_short_greeting(value.get("text")):
                    with self.lock:
                        check.update(phase="ignored", reason="short_greeting_only")
                    return {"ignored": True, "text": "", "reason": "short_greeting_only"}
                item["greeting_only"] = True
            elif short_utterance and is_short_greeting(value.get("text")):
                # A main-profile match can still use the immediate greeting
                # reply, without a model call or project action.
                item["greeting_only"] = True
            return self._save_transcript(item, value)
        finally:
            with self.lock:
                self.captures.pop(identity, None)
                item["audio"].clear()

    def stream(self, command, payload, context_revision, preference_revision):
        identity = payload.get("capture_id")
        if not isinstance(identity, str):
            raise SpeechError("A prepared recording is required.")
        if command == "cancel":
            with self.lock:
                item = self.captures.pop(identity, None)
                if item and item.get("buffered"):
                    item["audio"].clear()
            if not item or not item.get("buffered") or item.get("provider_started"):
                self.worker.call("cancel", {"id": identity}, timeout=10)
            with self.store.db() as db:
                db.execute("UPDATE speech_runs SET state='cancelled',finished_at=? WHERE id=? AND state='streaming'", (self.clock(), identity))
            return {}
        item = self.capture(identity, context_revision, preference_revision)
        if item.get("buffered"):
            return self._buffered_stream(command, payload, item, identity, context_revision, preference_revision)
        if command == "live_begin":
            with self.lock:
                if self.captures.get(identity) is not item or item["started"]:
                    raise SpeechError("This recording was already started.")
                item["started"] = True
            result = self.run(command, payload, source="live", config=item["config"], request_id=identity)
            self.capture(identity, context_revision, preference_revision)
            return result
        if not item["started"]:
            raise SpeechError("Begin the prepared recording first.")
        try:
            value = self.worker.call(command, payload, item["config"])
            self.capture(identity, context_revision, preference_revision)
            if command == "live_finish":
                with self.store.db() as db:
                    db.execute("UPDATE speech_runs SET state='completed',finished_at=? WHERE id=? AND state='streaming'", (self.clock(), identity))
                value = self._save_transcript(item, value)
            return value
        except (SpeechError, OSError, ValueError) as error:
            self.worker.call("cancel", {"id": identity}, timeout=10)
            with self.store.db() as db:
                db.execute("UPDATE speech_runs SET state='failed',finished_at=?,error=? WHERE id=? AND state='streaming'", (self.clock(), redact(str(error)), identity))
            raise
        finally:
            if command == "live_finish":
                with self.lock:
                    self.captures.pop(identity, None)
