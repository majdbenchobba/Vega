# Vega local speaker evidence runtime

This directory contains model attribution, license copies, the integration
contract, and runtime dependency notices for the dedicated CPU worker. The worker
does not enroll people, load profiles, select an identity, apply an owner-match
threshold, capture audio, play audio, or decide whether a source has authority.

## Local provisioning

From `standalone-host`, explicitly run:

```powershell
py -3.13 -B setup_speaker_runtime.py --provision
```

Setup independently compares the exact Windows CPython 3.13 wheel metadata on
PyPI and the model revisions/LFS hashes on Hugging Face against
`speaker-models.json`. It then verifies every downloaded byte count and SHA-256
before installation. The Protobuf wheel uses CPython's compatible `cp310-abi3`
ABI; FlatBuffers and Packaging use universal Python wheels.

Setup creates a **new dedicated** `.speaker-runtime` venv with
`system_site_packages=False`. The wheelhouse, temporary directory, pip bootstrap,
installation, and receipts stay there. Model binaries live in `.speaker-models`.
Neither the host's requirements nor global Python packages are changed.
Preexisting unowned runtime directories are refused. Interrupted, owned setup
can resume only with the same binary pins. Corrupt final artifacts are preserved
and refused, not silently replaced. No recursive cleanup is performed.

Pip installs the complete hashed lock with `--no-index --no-deps --require-hashes
--only-binary=:all:` from the verified wheelhouse, with pip configuration disabled.
No accounts, tokens, cloud inference calls, or Microsoft installer are used.
If Windows lacks a required VC++ runtime, setup reports that blocker.

An already provisioned installation can be checked entirely offline:

```powershell
py -3.13 -B setup_speaker_runtime.py --verify-only
```

`--provision --offline` also supports rebuilding an owned installation from its
existing verified metadata and artifact cache. It cannot bootstrap without that
cache. Normal worker execution does not invoke setup or download anything.

Evidence locations:

- `.speaker-runtime/metadata/verified-metadata.json`: independent publisher checks.
- `.speaker-runtime/setup-receipt.json`: actual hashes, versions and `pip check`.
- `.speaker-runtime/install-output.txt`: bounded offline pip installation output.
- `.speaker-runtime/smoke-results.json`: actual synthetic-only CPU inference.
- `runtime-notices.json`: generated during explicit provisioning; it records
  provenance and hashes for the copied dependency notices.

## Worker launch and transport

The parent starts this interpreter and script as a hidden local subprocess:

```text
<Vega folder>\.speaker-runtime\Scripts\python.exe
  -I -B
<Vega folder>\speaker_worker.py
```

Send one UTF-8 JSON object per line on stdin. Read one JSON response per line
on stdout. Processing is sequential and persistent; EOF ends the process. The
parent owns timeouts, process cancellation, HTTP, source authority and playback
policy. Diagnostics from native libraries can appear on stderr.

Request IDs are either nonempty strings of at most 128 characters, without control
characters, or safe JSON integers (absolute value at most 9007199254740991).
Boolean IDs are rejected. Requests permit only the documented fields; there is
no model-path, provider, threshold, download, TTS-state or profile parameter.

The maximum PCM payload is **960,000 bytes / 480,000 samples / 30 seconds**.
The maximum base64 length is 1,280,000 characters. The JSON line bound is
1,282,048 bytes and nesting is limited to 16. Invalid/oversized lines produce an
error; the following line is still processed. Duplicate fields, nonfinite JSON,
noncanonical base64, partial samples and known container headers are refused.
Raw PCM has no channel/rate header: the parent must actually supply mono s16le
at 16 kHz. The worker cannot infer a mislabeled raw stream's original format.

### Analyze

Input:

```json
{"id":"clip-1","command":"analyze","sample_rate":16000,"audio":"<canonical base64 of mono s16le PCM>"}
```

Output type:

```typescript
type AnalysisResponse = {
  id: string | number;
  ok: true;
  value: {
    model_key: "wespeaker-resnet34-lm-voxceleb2";
    duration: number;
    voiced_seconds: number;
    overlap_seconds: number;
    multiple_speakers: boolean;
    quality: { rms: number; clipped_fraction: number };
    windows: Array<{
      start: number;
      end: number;
      voiced_seconds: number;
      embedding: number[]; // exactly 256 finite floats; unit L2 norm
    }>;
  };
};
```

All times are seconds relative to the complete input clip. `duration` is exactly
the PCM sample count divided by 16000. `rms` uses the whole clip, on normalized
float PCM in [-1, 1]; `clipped_fraction` counts samples whose absolute amplitude
is at least 0.999.

`voiced_seconds` is conservative possible-speech evidence on 10 ms cells,
including possible overlap. `overlap_seconds` is the union of cells where the
sum of Pyannote's three pair-class probabilities is at least 0.10, taking the
maximum across overlapping contexts. It is not a ground-truth duration.

`multiple_speakers=true` means detected overlap, more than one plausible local
speaker slot within any context (even a brief second slot), **or disconnected
context evidence that cannot establish one consistent speaker**. It is a
conservative veto, not a person count. Pyannote slot numbers are permutation
invariant; slot numbers from different contexts are never compared as identities.
Contexts link through at least 100 ms of shared, confidently single-speaker
audio time. Distant speech islands may therefore be withheld even for one person.

An empty `windows` array means there is **no admissible embedding evidence**.
The parent must not interpret it as a match, enrollment, silence certainty, or
authorization. `ok=true` means the analysis executed successfully, including
successful withholding of uncertain audio.

### Status

Input:

```json
{"id":"readiness","command":"status"}
```

The response envelope is `{id,ok:true,value:{...}}`. Status values include:

- `ready`: true only after the pinned runtime imports, both model hashes and
  actual ONNX metadata validate, and both CPU sessions load.
- `model_key`: `wespeaker-resnet34-lm-voxceleb2`.
- `preprocess_version`: `vega-speaker-fbank80-hamming-cmn-segmentation-v1`.
- `model_hashes`: verified loaded SHA-256 values under `embedding` and
  `segmentation`; **empty if readiness fails**.
- `expected_model_hashes`: compiled-in pins, also present when not ready.
- `sample_rate`: 16000; `max_seconds`: 30; `embedding_dimensions`: 256.
- `provider`: `CPUExecutionProvider`; `intra_op_threads`: 2;
  `inter_op_threads`: 1; `thread_pool`: `shared_global`.
- `runtime_versions`: the six pinned installed distributions when ready.
- `analysis_policy`: the exact versioned audio-evidence defaults.
- `error`: `{code,message}` when not ready.

The exact model hashes are:

```text
embedding:
7bb2f06e9df17cdf1ef14ee8a15ab08ed28e8d0ef5054ee135741560df2ec068

segmentation:
220ad67ca923bef2fa91f2390c786097bf305bceb5e261d4af67b38e938e1079
```

Hashing precedes session creation, and sessions consume the verified bytes,
avoiding a second path open. File size, modification time and inode are checked
on subsequent requests; changes fail closed until restart. The loaded sessions
continue to use their verified bytes. No filesystem watcher or network service
is started.

### Errors

```json
{"id":"clip-1","ok":false,"error":{"code":"invalid_request","message":"..."}}
```

Malformed input without a valid ID uses `id:null`. Error codes are:

- `invalid_request`: invalid JSON/envelope/base64/format/size/rate/fields.
- `invalid_audio`: invalid internal PCM dimensions, range or finite values.
- `runtime_unavailable`: wrong interpreter, missing/mismatched packages, or
  unavailable native runtime prerequisites.
- `model_configuration`: incompatible manifest/policy/preprocessing.
- `model_integrity`: unexpected model path, hash/size failure, or changed model.
- `model_unavailable`: missing assets or a verified model that cannot load.
- `model_metadata`: wrong tensor signature, segmentation metadata or provider.
- `invalid_model_output`: invalid shape, probability normalization, features,
  embedding dimensions, nonfinite values, zero vector, or coverage inconsistency.
- `analysis_failed`: sanitized unexpected failure; no partial windows are returned.

No exception text containing audio, local internals or credentials is echoed.

## Segmentation and embedding admission

Pyannote segmentation runs across the **whole clip**, in 10-second contexts with
a 5-second hop. Short/final contexts are zero padded to 160000 samples. Its
589 frame outputs use the model's 991-sample receptive field and 270-sample step
to map evidence to the clip's 10 ms analysis cells, including first/last cells.
Padded time contributes no reported duration.

Each context has seven log-probability classes: silence, three individual speaker
slots, and their three pairs. Output shape, finite values and normalization are
checked before probabilities are normalized for aggregation.

The current conservative engineering defaults, exposed in status/manifest, are:

- Possible speech at probability >=0.50; confidently single-speaker speech at
  individual-class probability >=0.85 and pair probability <0.05.
- Any possible overlap at pair probability >=0.10 vetoes all windows.
- Ambiguous voiced evidence in any contributing context vetoes all windows.
  Confident silence in one context does not override confident speech at a
  boundary in another context.
- At least **3.0 voiced seconds**, not simply three seconds of recording.
- Whole-clip and every-window RMS >=0.005, clipped fraction <=0.005.
- A narrow DC/stationary-sinusoid quality veto. Passing it does not establish
  human speech, source authenticity, or freedom from background playback.

If admitted, voiced cells are divided into balanced chronological groups with
roughly 3-6 voiced seconds each. Six seconds is a soft computational target;
groups are merged if a partial final cell would otherwise make one too short.
Every possible-voice cell belongs to a returned window. If any group is too
short, low quality, mixed or invalid, **all windows are withheld**, rather than
returning a clean prefix while omitting the rest of the clip.

Window `start/end` encompass the group's original positions, including gaps.
Only its admitted voiced PCM cells are concatenated for feature extraction.
An admissible response satisfies the sum of window voiced durations equals
the clip's reported voiced duration, subject to floating-point representation.
The parent's identity check must consider all returned windows.

WeSpeaker preprocessing follows the upstream model recipe: normalized PCM is
scaled by 32768 for Kaldi features; 80 mel bins; 25 ms frames / 10 ms shifts;
Hamming window; dither zero; `snip_edges=True`; no energy feature; utterance
feature mean normalization; 256-dimensional neural output; finite nonzero L2
normalization. Profile compatibility must include both the model hash and
preprocessing version.

Both neural sessions share a CPU pool with at most two intra-op compute threads
and one inter-op thread, with sequential execution. NumPy/BLAS thread caps are
one. ONNX telemetry is disabled before creating model sessions. The worker has
no download path, HTTP client, capture/playback API, or subprocess invocation;
a Python audit hook also rejects socket/urllib activity.

## Limits and tests

This is speaker **evidence** after consent-based acquisition. It is not identity
recognition before enrollment, anti-replay, a media detector, or proof of a live
person. A recording or synthesis resembling an enrolled speaker can match an
embedding. The parent owns enrollment consent, deletion/re-enrollment, thresholds,
source authority, own-TTS/playback gates and automatic replies/actions.

No real voice has been captured or analyzed for this implementation. The tests
use fake neural sessions and generated signals. The actual model smoke uses
synthetic silence and 440 Hz sine tones only. Its separately computed tone
embedding validates the neural API/shape, not admission or speaker recognition.
Do not treat smoke timings as voice-quality benchmarks or latency guarantees
under concurrent ASR/TTS load.

```powershell
.\.speaker-runtime\Scripts\python.exe -I -B test_speaker_worker.py
.\.speaker-runtime\Scripts\python.exe -I -B test_speaker_worker.py --smoke
```

The second command records model-load, segmentation, embedding-probe and
persistent-stdio timing/shape evidence in `.speaker-runtime/smoke-results.json`.
No test reads, deletes or retries cleanup of the two blocked `.test-temp` fixtures.

## Attribution and redistribution notices

**WeSpeaker ResNet34-LM weights:** WeSpeaker project contributors; ResNet34
r-vector after large-margin fine-tuning, trained on VoxCeleb2 Dev. The weights
are licensed **CC BY 4.0**, not the toolkit's Apache license. See
`WeSpeaker-MODEL-CARD.md` for the authors' model description and paper citations,
and `WeSpeaker-CC-BY-4.0.txt` for the license. Preserve attribution and the license
link, and state changes. Vega downloads the upstream ONNX file unchanged.

- Model/card revision:
  https://huggingface.co/Wespeaker/wespeaker-voxceleb-resnet34-LM/tree/f0c48c298fd835726c27956a5d617bad7115627e
- License: https://creativecommons.org/licenses/by/4.0/
- Published model licensing:
  https://github.com/wenet-e2e/wespeaker/blob/9fecd6cb4f47475d01761d87c826298dff4ef18c/docs/pretrained.md
- Feature recipe:
  https://github.com/wenet-e2e/wespeaker/blob/9fecd6cb4f47475d01761d87c826298dff4ef18c/wespeaker/bin/infer_onnx.py

**Pyannote segmentation 3.0:** Copyright (c) 2022 CNRS, **MIT**. The ONNX
conversion is published by Fangjun Kuang / k2-fsa; the conversion source credits
Xiaomi Corporation. Vega uses the FP32 conversion unchanged, with a local
filename of `pyannote-segmentation-3.0.onnx`. Preserve `Pyannote-MIT.txt`.

- Conversion revision:
  https://huggingface.co/csukuangfj/sherpa-onnx-pyannote-segmentation-3-0/tree/9403a6902bb58e3d5ae8c7e77c3422de279db2e0
- Conversion code:
  https://github.com/k2-fsa/sherpa-onnx/blob/v1.13.8/scripts/pyannote/segmentation/export-onnx.py
- Original model: https://huggingface.co/pyannote/segmentation-3.0
- Powerset interpretation:
  https://github.com/pyannote/pyannote-audio/blob/3.3.2/pyannote/audio/utils/powerset.py

**Runtime distributions:** ONNX Runtime 1.30.0 (Microsoft, MIT);
kaldi-native-fbank 1.22.3 (contributors including Xiaomi, Apache 2.0);
NumPy 2.5.3 (NumPy developers, BSD and bundled notices);
FlatBuffers 25.12.19 (Google, Apache 2.0);
Packaging 26.3 (PyPA, Apache 2.0 or BSD);
Protobuf 7.36.2 (Google, BSD).

License texts and bundled third-party notices are copied directly from verified
wheels into `runtime/` and indexed in `runtime-notices.json`. The FlatBuffers
wheel omits a license file; `FlatBuffers-25.12.19-APACHE-2.0.txt` comes from its
pinned upstream commit
`7e163021e59cca4f8e1e35a7c828b5c6b7915953`. Preserve these notices when
redistributing the runtime. The installed Python/pip environment also retains
its own distribution notices.

All download URLs, bytes, SHA-256 values, model revisions and the notice hashes
are recorded in `speaker-models.json`. The worker does not depend on or install
Sherpa's bundled TTS/eSpeak runtime.
