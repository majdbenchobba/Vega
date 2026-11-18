"""Persistent, offline CPU audio-evidence worker. No enrollment or identity decisions.

One JSON object per stdin line; one response per stdout line:
  {"id":"1","command":"status"}
  {"id":"2","command":"analyze","sample_rate":16000,"audio":"<base64 s16le>"}

Launch using .speaker-runtime/Scripts/python.exe -I -B speaker_worker.py.
Only fixed, hash-verified local models are accepted. EOF exits the worker.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import importlib.metadata
import json
import math
import os
from pathlib import Path
import platform
import re
import struct
import sys

sys.dont_write_bytecode = True
# These are confined to this dedicated subprocess, set before importing NumPy/ORT.
for _thread_variable in ("OMP_NUM_THREADS", "OPENBLAS_NUM_THREADS", "MKL_NUM_THREADS",
                         "NUMEXPR_NUM_THREADS"):
    os.environ[_thread_variable] = "1"

ROOT = Path(__file__).resolve().parent
RUNTIME_DIR = ROOT / ".speaker-runtime"
MODEL_DIR = ROOT / ".speaker-models"
MODEL_KEY = "wespeaker-resnet34-lm-voxceleb2"
PREPROCESS_VERSION = "vega-speaker-fbank80-hamming-cmn-segmentation-v3"
SAMPLE_RATE = 16000
MAX_SECONDS = 30
MAX_PCM_BYTES = SAMPLE_RATE * MAX_SECONDS * 2
MAX_BASE64_CHARS = 4 * ((MAX_PCM_BYTES + 2) // 3)
MAX_LINE_BYTES = MAX_BASE64_CHARS + 2048
EMBEDDING_DIM = 256
MODEL_PINS = {
    "embedding": ("voxceleb_resnet34_LM.onnx", 26530309,
                  "7bb2f06e9df17cdf1ef14ee8a15ab08ed28e8d0ef5054ee135741560df2ec068"),
    "segmentation": ("pyannote-segmentation-3.0.onnx", 5992913,
                     "220ad67ca923bef2fa91f2390c786097bf305bceb5e261d4af67b38e938e1079"),
}
RUNTIME_PINS = {
    "onnxruntime": "1.30.0", "kaldi-native-fbank": "1.22.3", "numpy": "2.5.3",
    "flatbuffers": "25.12.19", "packaging": "26.3", "protobuf": "7.36.2",
}
POLICY = {
    "segmentation_window_samples": 160000,
    "segmentation_hop_samples": 80000,
    "segmentation_frames": 589,
    "segmentation_receptive_field_samples": 991,
    "segmentation_frame_step_samples": 270,
    "analysis_cell_samples": 160,
    "speech_probability": 0.5,
    "conditional_single_probability": 0.85,
    "speaker_slot_probability": 0.5,
    "overlap_probability": 0.1,
    "strong_overlap_probability": 0.5,
    "overlap_min_seconds": 0.1,
    "context_reliability_ratio": 0.5,
    "context_weighting": "hamming",
    "powerset_decoding": "argmax",
    "tail_context": "end_aligned",
    "speaker_evidence_seconds": 0.1,
    "min_voiced_seconds": 3,
    "target_max_window_voiced_seconds": 6,
    "embedding_audio": "contiguous",
    "clip_reference": "complete_recording",
    "min_rms": 0.005,
    "clipping_amplitude": 0.999,
    "max_clipped_fraction": 0.005,
    "stationary_tone_power_fraction": 0.98,
}
DECISION_POLICY = {
    "version": "echo-review-v1",
    "method": "delayed-weighted-linear-prediction",
    "fft_samples": 512, "hop_samples": 128, "taps": 12, "delay_frames": 3,
    "iterations": 3, "ridge_fraction": 1e-6, "max_restore_gain": 2,
    "raw_strong_overlap_veto": True, "raw_distinct_speaker_veto": True,
    "embedding_input": "original_audio_with_complete_speech_coverage",
}
# This affects only the greeting candidate, not enrollment or normal commands.
# A short clip placed against the model's left boundary can look like overlap.
GREETING_POLICY = {"version": "greeting-context-v3", "left_context_samples": SAMPLE_RATE,
                   "reference": "complete_voiced_envelope"}
SHORT_UTTERANCE_POLICY = {
    "version": "short-utterance-v1", "min_voiced_seconds": 0.3, "max_seconds": 4,
    "left_context_samples": SAMPLE_RATE, "reference": "complete_voiced_envelope",
}
SEGMENTATION_METADATA = {
    "num_speakers": "3", "powerset_max_classes": "2", "num_classes": "7",
    "sample_rate": "16000", "window_size": "160000",
    "receptive_field_size": "991", "receptive_field_shift": "270",
    "model_type": "pyannote-segmentation-3.0", "version": "1",
}
_GLOBAL_CPU_POOL_CONFIGURED = False


class WorkerError(Exception):
    """Bounded, non-sensitive error intended for the JSON protocol."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message

    def as_dict(self) -> dict:
        return {"code": self.code, "message": self.message}


def invalid_request(message: str) -> WorkerError:
    return WorkerError("invalid_request", message)


def reject_json_constant(value: str):
    raise invalid_request("Nonfinite JSON numbers are not supported.")


def unique_object(pairs: list[tuple]) -> dict:
    result = {}
    for key, value in pairs:
        if key in result:
            raise invalid_request("Duplicate JSON fields are not supported.")
        result[key] = value
    return result


def parse_json(data: bytes | str):
    try:
        text = data.decode("utf-8") if isinstance(data, bytes) else data
        # Check structure before invoking the C parser. Ignore punctuation inside
        # quoted strings, including escaped quotes and backslashes.
        depth = 0
        in_string = False
        escaped_at = -2
        for match in re.finditer(r'[\[\]{}"\\]', text):
            character, position = match.group(), match.start()
            if in_string:
                if escaped_at == position - 1:
                    continue
                if character == "\\":
                    escaped_at = position
                elif character == '"':
                    in_string = False
            elif character == '"':
                in_string = True
            elif character in "[{":
                depth += 1
                if depth > 16:
                    raise invalid_request("JSON nesting exceeds the request limit.")
            elif character in "]}":
                depth -= 1
        return json.loads(text, parse_constant=reject_json_constant,
                          object_pairs_hook=unique_object)
    except WorkerError:
        raise
    except (ValueError, UnicodeError, RecursionError):
        raise invalid_request("Expected a bounded UTF-8 JSON object.") from None


def valid_id(value) -> bool:
    return ((isinstance(value, str) and 0 < len(value) <= 128
             and all(ord(character) >= 32 for character in value))
            or (type(value) is int and abs(value) <= 9007199254740991))


def decode_request(message: dict) -> bytes | None:
    if not isinstance(message, dict) or not valid_id(message.get("id")):
        raise invalid_request("A short string or safe integer id is required.")
    if message.get("command") == "status":
        if set(message) != {"id", "command"}:
            raise invalid_request("Status accepts only id and command.")
        return None
    if message.get("command") not in ("analyze", "analyze_greeting", "analyze_short"):
        raise invalid_request("Supported commands are status, analyze, analyze_greeting and analyze_short.")
    if set(message) != {"id", "command", "audio", "sample_rate"}:
        raise invalid_request("Analyze accepts only id, command, audio and sample_rate.")
    if type(message["sample_rate"]) is not int or message["sample_rate"] != SAMPLE_RATE:
        raise invalid_request("Audio must be mono s16le PCM at 16000 Hz.")
    audio = message["audio"]
    if not isinstance(audio, str) or not 0 < len(audio) <= MAX_BASE64_CHARS:
        raise invalid_request("Audio is empty or exceeds the 30 second limit.")
    try:
        raw = base64.b64decode(audio, validate=True)
    except (ValueError, binascii.Error, UnicodeError):
        raise invalid_request("Audio must use canonical base64.") from None
    if (not 0 < len(raw) <= MAX_PCM_BYTES or len(raw) % 2
            or base64.b64encode(raw).decode("ascii") != audio):
        raise invalid_request("Audio must contain 1 to 480000 complete s16le samples.")
    if raw.startswith((b"RIFF", b"RIFX", b"RF64", b"OggS", b"fLaC", b"ID3",
                       b"\x1aE\xdf\xa3")):
        raise invalid_request("Container/compressed audio is unsupported; send raw s16le PCM.")
    return raw


def safe_local_model_path(filename: str) -> Path:
    if filename not in {pin[0] for pin in MODEL_PINS.values()}:
        raise WorkerError("model_integrity", "An unpinned model filename was requested.")
    path = MODEL_DIR / filename
    current = path
    while current != ROOT:
        if current.is_symlink() or (hasattr(current, "is_junction") and current.is_junction()):
            raise WorkerError("model_integrity", "Model symlinks and junctions are unsupported.")
        current = current.parent
    if not path.resolve().is_relative_to(MODEL_DIR):
        raise WorkerError("model_integrity", "Model directory containment check failed.")
    return path


def read_verified_model(role: str) -> tuple[bytes, tuple]:
    filename, expected_size, expected_hash = MODEL_PINS[role]
    path = safe_local_model_path(filename)
    try:
        with path.open("rb") as source:
            stat = os.fstat(source.fileno())
            if stat.st_size != expected_size:
                raise WorkerError("model_integrity", "A local model has the wrong size.")
            body = source.read(expected_size + 1)
    except OSError:
        raise WorkerError("model_unavailable", "Provision the pinned local model assets.") from None
    if len(body) != expected_size or hashlib.sha256(body).hexdigest() != expected_hash:
        raise WorkerError("model_integrity", "A local model failed its SHA-256 check.")
    # Session creation consumes these verified bytes, avoiding a hash/open race.
    return body, (stat.st_size, stat.st_mtime_ns, stat.st_ino)


def validate_manifest(manifest: dict) -> None:
    if (manifest.get("schema_version") != 1 or manifest.get("model_key") != MODEL_KEY
            or manifest.get("preprocess_version") != PREPROCESS_VERSION
            or set(manifest.get("models", {})) != set(MODEL_PINS)):
        raise WorkerError("model_configuration", "The speaker manifest is incompatible.")
    for role, (filename, size, digest) in MODEL_PINS.items():
        asset = manifest["models"][role]
        if (asset.get("filename"), asset.get("size"), asset.get("sha256")) != (
            filename, size, digest
        ):
            raise WorkerError("model_configuration", "The speaker manifest changed a model pin.")
    expected_audio = {
        "sample_rate": SAMPLE_RATE, "channels": 1, "encoding": "s16le",
        "max_seconds": MAX_SECONDS, "embedding_dimensions": EMBEDDING_DIM,
    }
    if manifest.get("audio") != expected_audio:
        raise WorkerError("model_configuration", "The audio contract needs a versioned update.")
    wheel_pins = {item.get("name"): item.get("version") for item in manifest.get("wheels", [])}
    if wheel_pins != RUNTIME_PINS or len(manifest.get("wheels", [])) != len(RUNTIME_PINS):
        raise WorkerError("model_configuration", "The manifest changed a runtime version.")
    expected_platform = {"provider": "CPUExecutionProvider", "intra_op_threads": 2,
                         "inter_op_threads": 1, "python": "3.13"}
    if any(manifest.get("platform", {}).get(name) != value
           for name, value in expected_platform.items()):
        raise WorkerError("model_configuration", "The CPU runtime contract is incompatible.")
    for name, value in POLICY.items():
        if manifest.get("analysis_policy", {}).get(name) != value:
            raise WorkerError("model_configuration", "The analysis policy needs a versioned update.")
    if manifest.get("short_utterance_policy") != SHORT_UTTERANCE_POLICY:
        raise WorkerError("model_configuration", "The short-utterance policy needs a versioned update.")
    if manifest.get("decision_policy") != DECISION_POLICY:
        raise WorkerError("model_configuration", "The decision policy needs a versioned update.")
    expected_preprocessing = {
        "pcm_float_scale": 32768, "num_mel_bins": 80, "frame_length_ms": 25,
        "frame_shift_ms": 10, "window_type": "hamming", "dither": 0,
        "snip_edges": True, "use_energy": False,
        "utterance_mean_normalization": True, "embedding_l2_normalization": True,
    }
    if any(manifest.get("preprocessing", {}).get(key) != value
           for key, value in expected_preprocessing.items()):
        raise WorkerError("model_configuration", "The preprocessing manifest is incompatible.")


def validate_session_metadata(embedding_session, segmentation_session) -> None:
    specifications = (
        (embedding_session, "feats", "embs", (None, None, 80), (None, 256)),
        (segmentation_session, "x", "y", (None, 1, None), (None, None, 7)),
    )
    for session, input_name, output_name, input_shape, output_shape in specifications:
        if session.get_providers() != ["CPUExecutionProvider"]:
            raise WorkerError("model_metadata", "Only the CPU execution provider is allowed.")
        inputs, outputs = session.get_inputs(), session.get_outputs()
        if len(inputs) != 1 or len(outputs) != 1:
            raise WorkerError("model_metadata", "Unexpected model input/output count.")
        for node, name, shape in ((inputs[0], input_name, input_shape),
                                  (outputs[0], output_name, output_shape)):
            if node.name != name or node.type != "tensor(float)" or len(node.shape) != len(shape):
                raise WorkerError("model_metadata", "Unexpected model tensor signature.")
            for actual, expected in zip(node.shape, shape):
                if expected is not None and actual != expected:
                    raise WorkerError("model_metadata", "Unexpected model tensor dimensions.")
                if expected is None and isinstance(actual, int) and actual != 1:
                    raise WorkerError("model_metadata", "Expected dynamic model dimensions.")
    metadata = segmentation_session.get_modelmeta().custom_metadata_map
    if any(metadata.get(key) != value for key, value in SEGMENTATION_METADATA.items()):
        raise WorkerError("model_metadata", "Segmentation metadata does not match its pinned export.")


def segmentation_starts(sample_count: int) -> list[int]:
    window = POLICY["segmentation_window_samples"]
    hop = POLICY["segmentation_hop_samples"]
    if sample_count <= window:
        return [0]
    last = sample_count - window
    starts = list(range(0, last + 1, hop))
    if starts[-1] != last:
        starts.append(last)
    return starts


def supported_overlap(np, weak, strong, lengths):
    """Require sustained weak evidence; retain even brief strong overlap."""
    result = np.zeros(weak.size, dtype=bool)
    edges = np.diff(np.concatenate(([False], weak, [False])).astype(np.int8))
    for start, end in zip(np.flatnonzero(edges == 1), np.flatnonzero(edges == -1)):
        if (strong[start:end].any() or int(lengths[start:end].sum())
                >= round(POLICY["overlap_min_seconds"] * SAMPLE_RATE)):
            result[start:end] = True
    return result


class SpeakerAnalyzer:
    """Audio evidence computation; sessions can be replaced by fakes in offline tests."""

    def __init__(self, numpy_module, feature_module, embedding_session, segmentation_session):
        self.np = numpy_module
        self.features = feature_module
        self.embedding_session = embedding_session
        self.segmentation_session = segmentation_session
        validate_session_metadata(embedding_session, segmentation_session)

    def validate_pcm(self, pcm):
        np = self.np
        x = np.asarray(pcm)
        if (x.ndim != 1 or not 0 < x.size <= SAMPLE_RATE * MAX_SECONDS
                or x.dtype.kind != "f" or not np.isfinite(x).all()
                or np.max(np.abs(x)) > 1):
            raise WorkerError("invalid_audio", "Expected bounded finite normalized mono PCM.")
        return np.ascontiguousarray(x, dtype=np.float32)

    def quality(self, pcm) -> dict:
        np = self.np
        rms = math.sqrt(float(np.mean(np.square(pcm, dtype=np.float64))))
        clipped = float(np.mean(np.abs(pcm) >= POLICY["clipping_amplitude"]))
        return {"rms": float(rms), "clipped_fraction": float(clipped)}

    def quality_admissible(self, pcm) -> bool:
        quality = self.quality(pcm)
        return (quality["rms"] >= POLICY["min_rms"]
                and quality["clipped_fraction"] <= POLICY["max_clipped_fraction"])

    def stationary_tone(self, pcm) -> bool:
        """Narrow quality veto for DC/pure test tones; never a media/liveness detector."""
        np = self.np
        if float(np.std(pcm, dtype=np.float64)) < 1e-5:
            return True
        # Use the complete waveform for subsecond sounds too. Otherwise the
        # loop would never inspect a short pure tone.
        # Speech/nonstationary material is not certified by passing this check.
        block = min(SAMPLE_RATE, pcm.size)
        for start in range(0, pcm.size - block + 1, block):
            frame = pcm[start:start + block].astype(np.float64)
            frame -= frame.mean()
            power = np.abs(np.fft.rfft(frame * np.hanning(frame.size))) ** 2
            total = float(power.sum())
            if total <= 1e-15:
                return False
            peak = int(power.argmax())
            concentrated = float(power[max(0, peak - 2):peak + 3].sum()) / total
            if concentrated < POLICY["stationary_tone_power_fraction"]:
                return False
        return True

    def _probabilities(self, context):
        np = self.np
        outputs = self.segmentation_session.run(["y"], {"x": context[None, None, :]})
        if not isinstance(outputs, (tuple, list)) or len(outputs) != 1:
            raise WorkerError("invalid_model_output", "Segmentation output count is invalid.")
        raw = np.asarray(outputs[0])
        if (raw.shape != (1, POLICY["segmentation_frames"], 7)
                or raw.dtype.kind != "f" or not np.isfinite(raw).all()):
            raise WorkerError("invalid_model_output", "Segmentation returned invalid values or shape.")
        logits = raw[0].astype(np.float64)
        maximum = logits.max(axis=1, keepdims=True)
        probabilities = np.exp(logits - maximum)
        denominator = probabilities.sum(axis=1, keepdims=True)
        log_sum = maximum + np.log(denominator)
        if not np.isfinite(log_sum).all() or np.max(np.abs(log_sum)) > 0.02:
            raise WorkerError("invalid_model_output", "Expected normalized segmentation log probabilities.")
        return probabilities / denominator

    def segment(self, pcm, details=None, *, minimum=3, left_context_samples=0) -> tuple:
        np = self.np
        cell = POLICY["analysis_cell_samples"]
        count = (pcm.size + cell - 1) // cell
        lengths = np.minimum(cell, pcm.size - np.arange(count) * cell).astype(np.int64)
        centers = np.arange(count) * cell + lengths / 2
        speech_sum = np.zeros(count, dtype=np.float64)
        overlap_sum = np.zeros(count, dtype=np.float64)
        single_sum = np.zeros(count, dtype=np.float64)
        weights = np.zeros(count, dtype=np.float64)
        strongest = np.zeros(count, dtype=np.float64)
        reliable_strong_overlap = np.zeros(count, dtype=bool)
        confident_single = np.zeros(count, dtype=bool)
        covered = np.zeros(count, dtype=bool)
        chunks = []
        multiple = False
        distinct_slots = False
        contexts = []
        hamming = np.hamming(POLICY["segmentation_frames"])
        for start in segmentation_starts(pcm.size):
            end = min(start + POLICY["segmentation_window_samples"], pcm.size)
            context = np.zeros(POLICY["segmentation_window_samples"], dtype=np.float32)
            left = min(left_context_samples, len(context) - (end - start))
            context[left:left + end - start] = pcm[start:end]
            probabilities = self._probabilities(context)
            indexes = np.flatnonzero((centers >= start) & (centers < end))
            frame_indexes = np.rint(
                (centers[indexes] - start + left - POLICY["segmentation_receptive_field_samples"] / 2)
                / POLICY["segmentation_frame_step_samples"]
            ).astype(np.int64)
            frame_indexes = np.clip(frame_indexes, 0, probabilities.shape[0] - 1)
            aligned = probabilities[frame_indexes]
            speech = 1.0 - aligned[:, 0]
            overlap = aligned[:, 4:7].sum(axis=1)
            # These seven outputs are mutually exclusive powerset classes.
            # Decode the winning class, as pyannote's Powerset converter does.
            # A single-speaker class need not exceed an arbitrary 85% cutoff.
            winner = aligned.argmax(axis=1)
            single_top = aligned[:, 1:4].max(axis=1)
            local_clean = (winner >= 1) & (winner <= 3)
            local_weight = hamming[frame_indexes]
            speech_sum[indexes] += local_weight * speech
            overlap_sum[indexes] += local_weight * overlap
            single_sum[indexes] += local_weight * single_top
            weights[indexes] += local_weight
            strongest[indexes] = np.maximum(strongest[indexes], local_weight)
            covered[indexes] = True
            contexts.append((indexes, winner, local_clean, local_weight, overlap, single_top))
        if not covered.all():
            raise WorkerError("invalid_model_output", "Segmentation failed to cover the complete clip.")
        speech_mean = speech_sum / weights
        overlap_mean = overlap_sum / weights
        # Once a cell is considered voiced, judge the dominant single-speaker
        # class relative to speech probability. Ordinary speech/silence
        # transitions must not become "unknown speaker" merely because silence
        # still carries posterior mass in an overlapping context.
        conditional_single = (single_sum / weights) / np.maximum(speech_mean,1e-12)
        for indexes, winner, local_clean, local_weight, overlap, single_top in contexts:
            # Do not let a low-context chunk edge overrule another chunk's
            # well-supported view of the same audio. No audio cells are dropped.
            reliable = local_weight >= strongest[indexes] * POLICY["context_reliability_ratio"]
            local_voice = local_clean & reliable & (single_top >= POLICY["speaker_slot_probability"])
            confident_single[indexes] |= local_voice
            distinct_slots |= np.unique(winner[local_voice]).size > 1
            reliable_strong_overlap[indexes] |= reliable & (
                overlap >= POLICY["strong_overlap_probability"])
            if local_voice.any():
                support = np.zeros(count, dtype=bool)
                support[indexes] = local_voice
                chunks.append(support)
        weak_overlap = overlap_mean >= POLICY["overlap_probability"]
        strong_overlap = ((overlap_mean >= POLICY["strong_overlap_probability"])
                          | reliable_strong_overlap)
        overlap_mask = supported_overlap(np, weak_overlap | strong_overlap, strong_overlap, lengths)
        voice = (speech_mean >= POLICY["speech_probability"]) | overlap_mask
        clean = (conditional_single >= POLICY["conditional_single_probability"]) & ~overlap_mask
        if int(lengths[confident_single & voice].sum()) < SAMPLE_RATE * minimum:
            clean[voice] = False
        multiple = bool(distinct_slots or overlap_mask.any())
        # Local slot labels are permutation-invariant. Link chunks using shared
        # confidently single-speaker *audio time*, never slot numbers across runs.
        # Disconnected evidence cannot establish one consistent speaker.
        if len(chunks) > 1:
            connected = {0}
            changed = True
            while changed:
                changed = False
                for other in range(len(chunks)):
                    if other in connected:
                        continue
                    if any(int(lengths[chunks[other] & chunks[known]].sum())
                           >= int(POLICY["speaker_evidence_seconds"] * SAMPLE_RATE)
                           for known in tuple(connected)):
                        connected.add(other)
                        changed = True
            if len(connected) != len(chunks):
                # Disconnected observations are uncertainty, not evidence that
                # the owner actually had another speaker in the room.
                clean[voice] = False
        if details is not None:
            details.update(
                strong_overlap=bool(strong_overlap.any()),
                distinct_speaker_slots=bool(distinct_slots),
                maximum_overlap_posterior=round(float(overlap_mean.max()),6),
            )
        return voice, overlap_mask, clean, lengths, bool(multiple)

    def reduce_late_echo(self, pcm):
        """Bounded, local WPE review signal. Never used as a saved voice template."""
        np = self.np
        size, hop = DECISION_POLICY["fft_samples"], DECISION_POLICY["hop_samples"]
        taps, delay = DECISION_POLICY["taps"], DECISION_POLICY["delay_frames"]
        padded = np.pad(pcm.astype(np.float64), (size // 2, size // 2 + hop))
        window = np.hanning(size)
        frames = np.lib.stride_tricks.sliding_window_view(padded, size)[::hop]
        observed = np.fft.rfft(frames * window, axis=-1).T.copy()
        estimate = observed.copy()
        past = np.zeros((observed.shape[0], taps, observed.shape[1]), dtype=np.complex128)
        for tap in range(taps):
            lag = delay + tap
            past[:, tap, lag:] = observed[:, :-lag]
        transpose = past.conj().swapaxes(-2, -1)
        eye = np.eye(taps)[None, :, :]
        for _ in range(DECISION_POLICY["iterations"]):
            power = np.abs(estimate) ** 2
            weights = 1 / np.maximum(power, max(1e-10, float(power.max()) * 1e-10))
            weighted = past * weights[:, None, :]
            covariance = weighted @ transpose
            correlation = weighted @ observed.conj()[:, :, None]
            floor = (np.trace(covariance, axis1=-2, axis2=-1).real / taps
                     * DECISION_POLICY["ridge_fraction"] + 1e-8)
            filters = np.linalg.solve(covariance + floor[:, None, None] * eye, correlation)
            estimate = observed - (filters.conj().swapaxes(-2, -1) @ past)[:, 0, :]
        reconstructed = np.fft.irfft(estimate.T, n=size, axis=-1)
        output, normalization = np.zeros(len(padded)), np.zeros(len(padded))
        for index, frame in enumerate(reconstructed):
            start = index * hop
            output[start:start + size] += frame * window
            normalization[start:start + size] += window ** 2
        output /= np.maximum(normalization, 1e-8)
        output = output[size // 2:size // 2 + len(pcm)]
        output_rms = float(np.sqrt(np.mean(output ** 2)))
        output *= min(DECISION_POLICY["max_restore_gain"],
                      self.quality(pcm)["rms"] / max(output_rms, 1e-8))
        output /= max(1, float(np.max(np.abs(output))))
        return self.validate_pcm(output)

    def embedding(self, pcm, *, minimum=3) -> list[float]:
        """Compute a normalized neural embedding; does not confer admissibility."""
        np = self.np
        x = self.validate_pcm(pcm)
        if x.size < SAMPLE_RATE * minimum:
            raise WorkerError("invalid_audio", "The recording is too short for this analysis mode.")
        opts = self.features.FbankOptions()
        opts.frame_opts.samp_freq = SAMPLE_RATE
        opts.frame_opts.frame_length_ms = 25
        opts.frame_opts.frame_shift_ms = 10
        opts.frame_opts.window_type = "hamming"
        opts.frame_opts.dither = 0
        opts.frame_opts.snip_edges = True
        opts.mel_opts.num_bins = 80
        opts.use_energy = False
        fb = self.features.OnlineFbank(opts)
        fb.accept_waveform(SAMPLE_RATE, (x * 32768.0).tolist())
        fb.input_finished()
        if fb.num_frames_ready < 2:
            raise WorkerError("invalid_model_output", "No usable feature frames were produced.")
        features = np.stack([fb.get_frame(i) for i in range(fb.num_frames_ready)]).astype(np.float32)
        if features.ndim != 2 or features.shape[1] != 80 or not np.isfinite(features).all():
            raise WorkerError("invalid_model_output", "Feature extraction returned invalid values.")
        features -= features.mean(axis=0, keepdims=True)
        outputs = self.embedding_session.run(["embs"], {"feats": features[None, ...]})
        if not isinstance(outputs, (list, tuple)) or len(outputs) != 1:
            raise WorkerError("invalid_model_output", "Embedding output count is invalid.")
        raw = np.asarray(outputs[0])
        if raw.shape != (1, EMBEDDING_DIM) or raw.dtype.kind != "f" or not np.isfinite(raw).all():
            raise WorkerError("invalid_model_output", "Embedding returned invalid values or dimensions.")
        vector = raw[0].astype(np.float64)
        norm = float(np.linalg.norm(vector))
        if not math.isfinite(norm) or norm <= 1e-12:
            raise WorkerError("invalid_model_output", "Embedding has no finite nonzero norm.")
        return [float(value) for value in vector / norm]

    def analyze(self, pcm, *, greeting_only=False, short_utterance=False) -> dict:
        np = self.np
        pcm = self.validate_pcm(pcm)
        segmentation = {}
        if greeting_only and short_utterance:
            raise WorkerError("invalid_audio", "Choose one short-speech analysis mode.")
        brief = greeting_only or short_utterance
        minimum = (SHORT_UTTERANCE_POLICY["min_voiced_seconds"] if short_utterance
                   else 0.6 if greeting_only else POLICY["min_voiced_seconds"])
        if brief and pcm.size > SAMPLE_RATE * SHORT_UTTERANCE_POLICY["max_seconds"]:
            raise WorkerError("invalid_audio", "Short-speech analysis is limited to four seconds.")
        voice, overlap, clean, lengths, multiple = (self.segment(
            pcm, segmentation, minimum=minimum,
            left_context_samples=GREETING_POLICY["left_context_samples"])
                                                   if brief else self.segment(pcm, segmentation))
        voiced_samples = int(lengths[voice].sum())
        result = {
            "model_key": MODEL_KEY,
            "duration": float(pcm.size / SAMPLE_RATE),
            "voiced_seconds": float(voiced_samples / SAMPLE_RATE),
            "overlap_seconds": float(int(lengths[overlap].sum()) / SAMPLE_RATE),
            "multiple_speakers": multiple,
            "quality": self.quality(pcm),
            "windows": [],
            "clip_embedding": None,
            "uncertain_seconds": float(int(lengths[voice & ~clean & ~overlap].sum()) / SAMPLE_RATE),
            "analysis_reason": "clean_speech",
            "segmentation": segmentation,
            "echo_review": {"attempted": False, "resolved": False},
        }
        # Never emit a clean subset that silently omits questionable speech.
        if greeting_only:
            result["greeting_only"] = True
            result["greeting_policy_version"] = GREETING_POLICY["version"]
        if short_utterance:
            result["short_utterance"] = True
            result["short_utterance_policy_version"] = SHORT_UTTERANCE_POLICY["version"]
        if voiced_samples < SAMPLE_RATE * minimum:
            result["analysis_reason"] = "insufficient_speech"
            return result
        if not self.quality_admissible(pcm) or self.stationary_tone(pcm):
            result["analysis_reason"] = "poor_quality"
            return result
        if (not brief and (multiple or np.any(voice & ~clean))
                and not segmentation["strong_overlap"]
                and not segmentation["distinct_speaker_slots"]):
            review = result["echo_review"]
            review.update(attempted=True, raw_overlap_seconds=result["overlap_seconds"],
                          raw_uncertain_seconds=result["uncertain_seconds"])
            reviewed_pcm = self.reduce_late_echo(pcm)
            try:
                review_voice, review_overlap, review_clean, _, review_multiple = self.segment(reviewed_pcm)
                resolved = bool(
                    self.quality_admissible(reviewed_pcm)
                    and not self.stationary_tone(reviewed_pcm)
                    and int(lengths[review_voice].sum()) >= SAMPLE_RATE * POLICY["min_voiced_seconds"]
                    and not review_multiple and not review_overlap.any()
                    and not np.any(review_voice & ~review_clean)
                )
                if resolved:
                    # Keep all originally detected speech in the original audio.
                    # Echo review cannot supply a cleaned subset or a replacement
                    # voice vector; every original window still has to match.
                    voice = voice | review_voice
                    overlap = np.zeros_like(overlap)
                    clean = np.ones_like(clean)
                    multiple = False
                    voiced_samples = int(lengths[voice].sum())
                    result.update(voiced_seconds=float(voiced_samples / SAMPLE_RATE),
                                  overlap_seconds=0., uncertain_seconds=0., multiple_speakers=False)
                review["resolved"] = resolved
            finally:
                reviewed_pcm.fill(0)
        if multiple or overlap.any():
            result["analysis_reason"] = "multiple_or_overlapping_speakers"
            return result
        if np.any(voice & ~clean):
            result["analysis_reason"] = "ambiguous_speech"
            return result
        voice_indexes = np.flatnonzero(voice)
        window_count = math.ceil(voiced_samples /
                                 (SAMPLE_RATE * POLICY["target_max_window_voiced_seconds"]))
        groups = np.array_split(voice_indexes, window_count)
        # A partial final 10 ms cell must not leave a group a few samples under
        # the hard three-second minimum. Six seconds is a target, not a cap.
        while window_count > 1 and any(
            int(lengths[group].sum()) < SAMPLE_RATE * POLICY["min_voiced_seconds"]
            for group in groups
        ):
            window_count -= 1
            groups = np.array_split(voice_indexes, window_count)
        windows = []
        cell = POLICY["analysis_cell_samples"]
        for group in groups:
            group_voiced = int(lengths[group].sum())
            if group_voiced < SAMPLE_RATE * minimum:
                result["analysis_reason"] = "incomplete_speech_coverage"
                return result
            start = int(group[0]) * cell
            end = min((int(group[-1]) + 1) * cell, pcm.size)
            # Preserve the natural waveform between the first and last voiced
            # cell. Cutting/gluing every 10 ms VAD cell changes phonetic content
            # and can make sections from one person appear less similar.
            waveform = pcm[start:end]
            # Retain the existing quality check on the admitted speech itself.
            # This subset is never the model's embedding input.
            mask = np.repeat(voice[int(group[0]):int(group[-1]) + 1], cell)[:end-start]
            voiced_waveform=waveform[mask]
            if voiced_waveform.size != group_voiced:
                raise WorkerError("invalid_model_output", "Embedding window coverage is inconsistent.")
            if not self.quality_admissible(voiced_waveform) or self.stationary_tone(voiced_waveform):
                result["analysis_reason"] = "poor_quality"
                return result
            windows.append({
                "start": float(start / SAMPLE_RATE), "end": float(end / SAMPLE_RATE),
                "voiced_seconds": float(group_voiced / SAMPLE_RATE),
                "embedding": self.embedding(waveform, minimum=minimum) if brief else self.embedding(waveform),
            })
        if sum(int(lengths[group].sum()) for group in groups) != voiced_samples:
            raise WorkerError("invalid_model_output", "Embedding windows omitted voiced samples.")
        result["windows"] = windows
        # Enrollment uses an utterance-level reference, as speaker extraction
        # does. Short windows remain independent checks against that reference
        # and the enrolled owner; no mixed/uncertain clip reaches this point.
        # Brief utterances are especially distorted by variable waiting time
        # around the speech. Their one window contains the original waveform
        # from the first to last voiced cell, including every intervening pause.
        # All speech still passes segmentation; no questionable subset is used.
        result["clip_embedding"] = (list(windows[0]["embedding"]) if brief
                                    else self.embedding(pcm))
        return result


class Runtime:
    def __init__(self):
        self.analyzer = None
        self.failed = None
        self.stamps = {}
        self.versions = {}

    def _load(self) -> SpeakerAnalyzer:
        global _GLOBAL_CPU_POOL_CONFIGURED
        if (sys.version_info[:2] != (3, 13) or platform.system() != "Windows"
                or struct.calcsize("P") != 8 or Path(sys.prefix).resolve() != RUNTIME_DIR
                or sys.prefix == sys.base_prefix):
            raise WorkerError("runtime_unavailable", "Launch with the dedicated CPython 3.13 runtime.")
        if RUNTIME_DIR.is_symlink() or RUNTIME_DIR.is_junction():
            raise WorkerError("runtime_unavailable", "The dedicated runtime must be a local directory.")
        try:
            manifest = parse_json((ROOT / "speaker-models.json").read_bytes())
            validate_manifest(manifest)
            self.versions = {name: importlib.metadata.version(name) for name in RUNTIME_PINS}
        except (OSError, importlib.metadata.PackageNotFoundError, ValueError):
            raise WorkerError("runtime_unavailable", "Provision the dedicated pinned runtime first.") from None
        if self.versions != RUNTIME_PINS:
            raise WorkerError("runtime_unavailable", "Runtime versions differ from the pinned manifest.")
        try:
            import numpy as np
            import onnxruntime as ort
            import kaldi_native_fbank as features
            if not _GLOBAL_CPU_POOL_CONFIGURED:
                # Both models share one pool: a per-session two-thread pool
                # would unnecessarily create two separate native worker pools.
                ort.set_global_thread_pool_sizes(intra_op_num_threads=2,
                                                inter_op_num_threads=1)
                _GLOBAL_CPU_POOL_CONFIGURED = True
            ort.disable_telemetry_events()
        except (ImportError, OSError):
            raise WorkerError("runtime_unavailable", "CPU libraries could not load; check local runtime prerequisites.") from None
        options = ort.SessionOptions()
        # Pool sizes are set globally above; ORT warns if per-session sizes are
        # also supplied when use_per_session_threads is false.
        options.use_per_session_threads = False
        options.execution_mode = ort.ExecutionMode.ORT_SEQUENTIAL
        options.log_severity_level = 3
        sessions = {}
        for role in MODEL_PINS:
            body, stamp = read_verified_model(role)
            try:
                sessions[role] = ort.InferenceSession(
                    body, sess_options=options, providers=["CPUExecutionProvider"]
                )
                sessions[role].disable_fallback()
            except Exception:
                raise WorkerError("model_unavailable", "A verified model could not load on the CPU runtime.") from None
            self.stamps[role] = stamp
        return SpeakerAnalyzer(np, features, sessions["embedding"], sessions["segmentation"])

    def get(self) -> SpeakerAnalyzer:
        if self.failed is not None:
            raise self.failed
        try:
            if self.analyzer is None:
                self.analyzer = self._load()
            for role, stamp in self.stamps.items():
                stat = safe_local_model_path(MODEL_PINS[role][0]).stat()
                if (stat.st_size, stat.st_mtime_ns, stat.st_ino) != stamp:
                    raise WorkerError("model_integrity", "A model changed after loading; restart after verification.")
        except WorkerError as error:
            self.analyzer = None
            self.failed = error
            raise
        except OSError:
            self.analyzer = None
            self.failed = WorkerError("model_integrity", "A loaded local model is no longer available.")
            raise self.failed from None
        return self.analyzer

    def status(self) -> dict:
        value = {
            "ready": False, "model_key": MODEL_KEY, "model_hashes": {},
            "expected_model_hashes": {role: pin[2] for role, pin in MODEL_PINS.items()},
            "preprocess_version": PREPROCESS_VERSION,
            "sample_rate": SAMPLE_RATE, "max_seconds": MAX_SECONDS,
            "embedding_dimensions": EMBEDDING_DIM,
            "provider": "CPUExecutionProvider", "intra_op_threads": 2, "inter_op_threads": 1,
            "thread_pool": "shared_global",
            "analysis_policy": dict(POLICY),
            "decision_policy": dict(DECISION_POLICY),
            "greeting_policy": dict(GREETING_POLICY),
            "short_utterance_policy": dict(SHORT_UTTERANCE_POLICY),
        }
        try:
            self.get()
        except WorkerError as error:
            value["error"] = error.as_dict()
        else:
            value.update({
                "ready": True, "model_hashes": dict(value["expected_model_hashes"]),
                "runtime_versions": dict(self.versions),
            })
        return value


def handle_message(message, runtime) -> dict:
    request_id = message.get("id") if isinstance(message, dict) else None
    if not valid_id(request_id):
        request_id = None
    try:
        raw = decode_request(message)
        if raw is None:
            value = runtime.status()
        else:
            analyzer = runtime.get()
            pcm = analyzer.np.frombuffer(raw, dtype="<i2").astype(analyzer.np.float32) / 32768.0
            value = (analyzer.analyze(pcm, greeting_only=True) if message["command"] == "analyze_greeting"
                     else analyzer.analyze(pcm, short_utterance=True) if message["command"] == "analyze_short"
                     else analyzer.analyze(pcm))
        # Validation before emitting prevents NaN/Infinity from crossing stdio.
        response = {"id": request_id, "ok": True, "value": value}
        json.dumps(response, allow_nan=False)
        return response
    except WorkerError as error:
        return {"id": request_id, "ok": False, "error": error.as_dict()}
    except Exception:
        return {"id": request_id, "ok": False,
                "error": {"code": "analysis_failed", "message": "Local audio analysis failed closed."}}


def bounded_lines(source):
    while True:
        line = source.readline(MAX_LINE_BYTES + 1)
        if not line:
            return
        if len(line) > MAX_LINE_BYTES:
            while line and not line.endswith(b"\n"):
                line = source.readline(MAX_LINE_BYTES + 1)
            yield None
        else:
            yield line


def serve(source, destination, runtime) -> None:
    for line in bounded_lines(source):
        try:
            if line is None:
                raise invalid_request("JSON line exceeds the request limit.")
            response = handle_message(parse_json(line), runtime)
        except WorkerError as error:
            response = {"id": None, "ok": False, "error": error.as_dict()}
        encoded = json.dumps(response, allow_nan=False, separators=(",", ":")) + "\n"
        try:
            destination.write(encoded.encode("utf-8"))
            destination.flush()
        except (BrokenPipeError, OSError):
            return


def offline_audit(event: str, arguments) -> None:
    if event.startswith("socket.") or event == "urllib.Request":
        raise PermissionError("The speaker worker has no network access.")


def main() -> int:
    # Runtime code has no HTTP client, downloader, model resolver, microphone,
    # output device, profile database, or external process invocation.
    sys.addaudithook(offline_audit)
    serve(sys.stdin.buffer, sys.stdout.buffer, Runtime())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
