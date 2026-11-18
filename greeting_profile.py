"""Encrypted short voice references with explicit, profile-bound usage scope."""
import base64
import contextlib
import hashlib
import json
import math
from itertools import combinations

from owner_voice import (
    COSINE_THRESHOLD, ENROLLMENT_CONSISTENCY, MATCHING_POLICY, MAX_PROFILE_BYTES,
    REQUIRED_SAMPLES, SAMPLE_RATE, WINDOW_CONSISTENCY, OwnerVoiceError,
    _consistent, _cosine, _prototype, _vector,
)

KEY = "owner_voice_greeting_templates"
CHECK_KEY = "owner_voice_last_greeting_check"
SCOPE_KEY = "owner_voice_short_profile_scope"
CANDIDATE_KEY = "owner_voice_short_profile_candidate"
REFERENCE_POLICY = "pooled-reference-coverage-v1"
VOICE_CONSENT_VERSION = "short-owner-voice-v1"
VOICE_CONSENT = "Use my encrypted short samples to recognize my voice in any short phrase. Discard recordings after analysis."
POLICY = "greeting-references-v1"
ANALYSIS_POLICY = "greeting-context-v3"
SETUP_POLICY = "three-clear-references-v1"
CONSENT = (
    "I consent to saving three short greetings as encrypted voice templates on this PC. "
    "These recordings stay local and are discarded after analysis. The templates only "
    "enable greeting replies; my existing voice profile and checks are kept."
)

class GreetingProfile:
    def __init__(self, voice):
        self.voice = voice

    def _binding(self, state):
        return {key: state[key] for key in ("owner", "profile_id", "profile_binding", "profile_digest")}

    def _can_setup(self, state):
        # Collecting references is setup, not permission to listen or act.
        # Enablement still requires the parent's completed checks and consent.
        return bool(not self.voice.closed.is_set() and not self.voice.active
                    and state["owner"] and state["profile_id"] and not state["storage_failed"]
                    and self.voice.runtime["ready"] and state["runtime_available"] is True
                    and state["profile_binding"] == state["runtime_binding"] == self.voice.runtime["binding"])

    def read(self, state, db=None):
        if not state["profile_id"] or state["storage_failed"]:
            return None
        try:
            with contextlib.nullcontext(db) if db is not None else self.voice.store.db() as connection:
                row = connection.execute("SELECT value FROM settings WHERE key=?", (KEY,)).fetchone()
            if not row or len(row[0]) > 2 * MAX_PROFILE_BYTES:
                return None
            encrypted = base64.b64decode(json.loads(row[0]), validate=True)
            raw = self.voice.protect_fn(encrypted, decrypt=True)
            if not isinstance(raw, bytes) or len(raw) > MAX_PROFILE_BYTES:
                return None
            value = json.loads(raw)
            if (value.get("policy") not in (POLICY, ANALYSIS_POLICY) or value.get("binding") != self._binding(state)
                    or not isinstance(value.get("templates"), list)
                    or len(value["templates"]) != REQUIRED_SAMPLES):
                return None
            templates = tuple(_vector(item) for item in value["templates"])
            # Preserve valid previously saved greetings. New reference takes
            # represent natural variation; registration does not compare them.
            if value["policy"] == ANALYSIS_POLICY and not _consistent(templates, ENROLLMENT_CONSISTENCY):
                return None
            return templates
        except Exception:
            return None

    def status(self, state, db=None):
        return {"enrolled": self.read(state, db) is not None, "seconds": 3,
                "setup_policy": SETUP_POLICY, "can_setup": self._can_setup(state),
                "matching_policy": MATCHING_POLICY,
                "reference_policy": REFERENCE_POLICY,
                "required_samples": REQUIRED_SAMPLES, "consent_text": CONSENT,
                "voice_consent_version": VOICE_CONSENT_VERSION,
                "voice_consent_text": VOICE_CONSENT,
                "general_voice": self.read_for_voice(state, db) is not None,
                "last_check": self._last_check(state, db)}

    @staticmethod
    def coverage(templates):
        """Check the representation actually used to score future speech.

        Leave-one-out scores are useful evaluation evidence, not an admission
        rule for a profile constructed from all three independently clear takes.
        No cutoff is lowered and no query is admitted by this check alone.
        """
        try:
            if not templates or len(templates) != REQUIRED_SAMPLES:
                return None
            center = _prototype(templates)
            scores = tuple(_cosine(template, center) for template in templates)
            return scores if min(scores) >= COSINE_THRESHOLD else None
        except (ValueError, TypeError, IndexError):
            return None

    def _scope_binding(self, state, db):
        row = db.execute("SELECT value FROM settings WHERE key=?", (KEY,)).fetchone()
        return {**self._binding(state),
                "references_digest": hashlib.sha256(row[0].encode()).hexdigest()} if row else None

    def read_for_voice(self, state, db=None):
        """Legacy greeting-only consent never authorizes ordinary speech."""
        try:
            with contextlib.nullcontext(db) if db is not None else self.voice.store.db() as connection:
                row = connection.execute("SELECT value FROM settings WHERE key=?", (SCOPE_KEY,)).fetchone()
                if not row or len(row[0]) > 4096:
                    return None
                scope = json.loads(row[0])
                if (not isinstance(scope, dict) or scope.get("consent_version") != VOICE_CONSENT_VERSION
                        or scope.get("binding") != self._scope_binding(state, connection)):
                    return None
                templates = self.read(state, connection)
                if self.coverage(templates) is not None:
                    return templates
        except (ValueError, TypeError, KeyError):
            pass
        return None

    def _authorize(self, state, db):
        db.execute("INSERT OR REPLACE INTO settings(key,value) VALUES(?,?)",
                   (SCOPE_KEY, json.dumps({"consent_version": VOICE_CONSENT_VERSION,
                                          "binding": self._scope_binding(state, db)})))

    def use_for_voice(self, consent, expected_revision, consent_version):
        if consent is not True or consent_version != VOICE_CONSENT_VERSION:
            raise OwnerVoiceError("Agree to use these samples for short voice recognition.")
        voice = self.voice
        runtime = voice._runtime_status()
        with voice._state(runtime, include_db=True) as (state, templates, db):
            voice._expect(state, expected_revision)
            if not templates or not self._can_setup(state):
                raise OwnerVoiceError("End the current setup before using saved samples.")
            references = self.read(state, db)
            consistent = self.coverage(references) is not None
            if not consistent:
                raise OwnerVoiceError("The saved short samples need review before use.")
            if self.read_for_voice(state, db) is None:
                self._authorize(state, db)
                state["revision"] += 1
            return {"status": voice._status(state, templates, db=db), "authorized": True}

    def _last_check(self, state, db=None):
        """One bounded diagnostic, never recording bytes or voice vectors."""
        try:
            with contextlib.nullcontext(db) if db is not None else self.voice.store.db() as connection:
                row = connection.execute("SELECT value FROM settings WHERE key=?", (CHECK_KEY,)).fetchone()
            if not row or len(row[0]) > 8192:
                return None
            saved = json.loads(row[0])
            if (saved["binding"] != self._binding(state) or saved["policy"] != POLICY
                    or type(saved["profile_revision"]) is not int
                    or saved["profile_revision"] > state["revision"]):
                return None
            result = saved["result"]
            if (type(result["accepted"]) is not bool
                    or not isinstance(result["reason"], str) or len(result["reason"]) > 80
                    or not isinstance(result["detail"], str) or len(result["detail"]) > 600
                    or type(result["clip"]) is not int or not 1 <= result["clip"] <= REQUIRED_SAMPLES
                    or type(result["checked_at"]) not in (int, float) or not math.isfinite(result["checked_at"])):
                return None
            public = {key: result[key] for key in ("accepted", "reason", "detail", "clip", "checked_at")}
            diagnostic = result.get("diagnostics", {})
            public["diagnostics"] = {
                key: diagnostic[key] for key in (
                    "duration_seconds", "speech_seconds", "overlap_seconds", "uncertain_seconds",
                    "input_rms", "clipped_fraction", "analysis_windows", "accepted_recordings",
                    "within_recording_min", "between_recordings_min", "consistency_threshold",
                    "candidate_recordings", "selected_pair_min", "reselected_recordings",
                ) if type(diagnostic.get(key)) in (int, float) and math.isfinite(diagnostic[key])
            }
            if diagnostic.get("failure_stage") in ("speech_detection", "window_to_recording", "between_recordings", "analysis"):
                public["diagnostics"]["failure_stage"] = diagnostic["failure_stage"]
            return public
        except (KeyError, TypeError, ValueError):
            return None

    def start(self, consent, expected_revision, consent_version=None):
        if consent is not True:
            raise OwnerVoiceError("Agree to local short-greeting enrollment before recording.")
        if consent_version not in (None, VOICE_CONSENT_VERSION):
            raise OwnerVoiceError("Refresh the short voice consent before recording.")
        voice = self.voice
        runtime = voice._runtime_status()
        with voice._state(runtime, include_db=True) as (state, templates, db):
            voice._expect(state, expected_revision)
            if not templates or not self._can_setup(state):
                raise OwnerVoiceError("Save a voice profile with the current local model before adding greetings.")
            item = voice._start_session(state, "greeting")
            item["general_voice_consent"] = consent_version == VOICE_CONSENT_VERSION
            db.execute("DELETE FROM settings WHERE key=?", (CHECK_KEY,))
            return {"status": voice._status(state, templates, db=db), "enrollment": voice._enrollment(item)}

    def sample(self, identity, sample_key, pcm, sample_rate):
        voice = self.voice
        digest = voice._pcm(pcm, sample_rate)
        if not .6 * SAMPLE_RATE * 2 <= len(pcm) <= 4 * SAMPLE_RATE * 2:
            raise OwnerVoiceError("Record one short greeting within four seconds.")
        runtime = voice._runtime_status()
        with voice._state(runtime) as (state, templates):
            item = voice._session(identity, state, "greeting")
            cached = voice._reserve(item, sample_key, digest)
            if cached is not None:
                return {"status": voice._status(state, templates),
                        "enrollment": voice._enrollment(item), **cached}
            if len(item["samples"]) >= REQUIRED_SAMPLES:
                item["attempts"].pop(sample_key, None)
                raise OwnerVoiceError("Save or cancel the three collected greetings.")
            model_key = state["model_key"]
        value, runtime, error = voice._analyze(pcm, greeting_only=True)
        evidence = voice._evidence(value, pcm, model_key, .6)
        with voice._state(runtime, include_db=True) as (state, templates, db):
            item = voice._session(identity, state, "greeting")
            valid = (not error and isinstance(value, dict) and value.get("greeting_only") is True
                     and value.get("greeting_policy_version") == ANALYSIS_POLICY
                     and evidence["reason"] == "clean_speech")
            reference = evidence["reference"]
            within = min((_cosine(vector, reference) for vector in evidence["vectors"]),
                         default=None) if valid and reference is not None else None
            between = min((_cosine(vector, reference) for vector in item["samples"]),
                          default=None) if within is not None else None
            coherent = within is not None and within >= WINDOW_CONSISTENCY
            clip = len(item["samples"]) + 1
            if coherent:
                item["samples"].append(reference)
            reason = "accepted" if coherent else error or (
                evidence["reason"] if evidence["reason"] != "clean_speech" else
                "invalid_analysis" if not valid else
                "inconsistent_recording")
            detail = {
                "accepted": "Reference recorded.",
                "insufficient_speech": "Too little speech. Wait for “Speak now”, then say one short phrase.",
                "poor_quality": "The microphone signal was too quiet or distorted. Check the input meter, then retry this clip.",
                "multiple_or_overlapping_speakers": "The detector flagged possible mixed speech. This can be a false detection; your voice was not compared with the earlier clip.",
                "ambiguous_speech": "The detector could not classify part of this greeting. Your voice was not compared with the earlier clip.",
                "incomplete_speech_coverage": "The detector could not analyze the whole greeting. Wait for “Speak now”, then retry this clip.",
                "inconsistent_recording": "Sections of this greeting did not agree. Wait for “Speak now” and use your natural voice.",
                "busy": "The local voice check was busy. Wait a moment, then retry this clip.",
                "runtime_unavailable": "The local voice check could not run. Refresh status before trying again.",
            }.get(reason, "The local voice check returned incomplete evidence. Refresh status before trying again.")
            if not coherent:
                detail += " Your saved profile is unchanged."
            selected_min = min((_cosine(left, right) for left, right in combinations(item["samples"], 2)), default=None)
            diagnostics = voice._check_diagnostics(value, pcm, {})
            diagnostics.update(
                accepted_recordings=len(item["samples"]),
                within_recording_min=None if within is None else round(within, 6),
                between_recordings_min=None if between is None else round(between, 6),
                candidate_recordings=len(item["samples"]),
                selected_pair_min=None if selected_min is None else round(selected_min, 6),
                failure_stage=None if coherent else (
                    "analysis" if error or reason.startswith("invalid_") else
                    "speech_detection" if not valid else
                    "window_to_recording"),
            )
            result = {
                "accepted": bool(coherent), "reason": reason, "detail": detail,
                "diagnostics": diagnostics,
            }
            saved = {"binding": self._binding(state), "policy": POLICY,
                     "profile_revision": state["revision"],
                     "result": {**result, "clip": clip, "checked_at": voice.clock()}}
            db.execute("INSERT OR REPLACE INTO settings(key,value) VALUES(?,?)",
                       (CHECK_KEY, json.dumps(saved, allow_nan=False)))
            item["attempts"][sample_key]["result"] = result
            return {"status": voice._status(state, templates, db=db),
                    "enrollment": voice._enrollment(item), **result}

    def commit(self, identity, expected_revision):
        voice = self.voice
        runtime = voice._runtime_status()
        with voice._state(runtime, include_db=True) as (state, templates, db):
            voice._expect(state, expected_revision)
            item = voice._session(identity, state, "greeting")
            if len(item["samples"]) != REQUIRED_SAMPLES or any(
                    attempt["result"] is None for attempt in item["attempts"].values()):
                raise OwnerVoiceError("Collect three accepted greetings before saving.")
            payload = {"policy": POLICY, "binding": self._binding(state), "templates": item["samples"]}
            try:
                encrypted = voice.protect_fn(json.dumps(payload, allow_nan=False).encode())
                if not isinstance(encrypted, bytes) or not 0 < len(encrypted) <= MAX_PROFILE_BYTES:
                    raise ValueError("Invalid protected output.")
            except Exception:
                raise OwnerVoiceError("The greetings could not be encrypted. No saved profile was changed.") from None
            encoded = json.dumps(base64.b64encode(encrypted).decode("ascii"))
            if self.coverage(item["samples"]) is None:
                # Preserve a usable saved profile instead of replacing it with
                # references that the actual pooled scorer cannot represent.
                # Keep one encrypted candidate for diagnosis; never use it for
                # recognition or attach the prior scope consent to it.
                retained = self.read_for_voice(state, db) is not None
                db.execute("INSERT OR REPLACE INTO settings(key,value) VALUES(?,?)",
                           (CANDIDATE_KEY, encoded))
                state["revision"] += 1
                voice._clear_sessions()
                state["enabled"] = bool(item["previous"]["enabled"] and voice._eligible(state, templates))
                return {"status": voice._status(state, templates, db=db), "committed": False,
                        "setup_finished": True, "needs_review": True, "retained_previous": retained}
            db.execute("INSERT OR REPLACE INTO settings(key,value) VALUES(?,?)", (KEY, encoded))
            db.execute("DELETE FROM settings WHERE key=?", (CANDIDATE_KEY,))
            # A replacement must carry its own scope consent. Never inherit a
            # prior authorization for different samples.
            db.execute("DELETE FROM settings WHERE key=?", (SCOPE_KEY,))
            if item.get("general_voice_consent"):
                self._authorize(state, db)
            state["revision"] += 1
            voice._clear_sessions()
            state["enabled"] = bool(item["previous"]["enabled"] and voice._eligible(state, templates))
            return {"status": voice._status(state, templates, db=db), "committed": True}

    def cancel(self, identity):
        return self.voice._cancel(identity, "greeting")

    def score(self, evidence, templates, revision):
        """Score the complete turn against one representative short profile."""
        coverage = self.coverage(templates)
        if coverage is None:
            return self.voice._result("unknown", "inconsistent_profile", revision)
        return {**self.voice._score(evidence, templates, revision),
                "reference_kind": "short_greeting", "reference_policy": REFERENCE_POLICY}

    def probe(self, identity, sample_key, pcm, sample_rate):
        """Keep one explicitly consented diagnostic, never an enrollment pass.

        The fresh paused session must start after this distinct consent. No
        recognition, template selection, check mutation or cloud call occurs.
        Analysis reads this bounded encrypted clip locally after the owner
        records four prompted greetings in the same microphone session.
        """
        voice = self.voice
        digest = voice._pcm(pcm, sample_rate)
        if not 11.5 * SAMPLE_RATE * 2 <= len(pcm) <= 12 * SAMPLE_RATE * 2:
            raise OwnerVoiceError("The diagnostic needs one complete 12-second recording.")
        runtime = voice._runtime_status()
        with voice._state(runtime, include_db=True) as (state, templates, db):
            item = voice._session(identity, state, "greeting")
            prior = item["attempts"].get(sample_key)
            if prior and prior["result"] is not None:
                if prior["result"].get("diagnostic_only") is not True:
                    raise OwnerVoiceError("That sample identity belongs to enrollment, not a diagnostic.")
                cached = voice._reserve(item, sample_key, digest)
                return {"status": voice._status(state, templates, db=db), **cached}
            grant = db.execute(
                "SELECT * FROM voice_diagnostics WHERE owner=? AND state='armed'",
                (state["owner"],)).fetchone()
            if (not grant or grant["purpose"] != "greeting_probe"
                    or voice.clock() > grant["deadline"] or item["started_at"] < grant["armed_at"]
                    or item["samples"] or item["attempts"]):
                raise OwnerVoiceError("Prepare a new one-time greeting diagnostic before recording.")
            voice._reserve(item, sample_key, digest)
            diagnostics = {"duration_seconds": len(pcm) / (SAMPLE_RATE * 2),
                           "profile_revision": state["revision"], "greeting_sections": 4}
            saved = voice.diagnostics.capture_locked(
                db, state["owner"], pcm,
                {"accepted": False, "reason": "greeting_diagnostic_only", "diagnostics": diagnostics},
                purpose="greeting_probe", started_at=item["started_at"])
            result = {"diagnostic": saved, "diagnostic_only": True}
            item["attempts"][sample_key]["result"] = result
            return {"status": voice._status(state, templates, db=db), **result}
