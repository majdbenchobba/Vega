# Third-party notices

Optional model and runtime package notices are recorded in `speaker-models.json`
and `third-party/speaker-models`; provisioned user-local runtimes also retain
the notices extracted from their verified packages.

## NARA-WPE reference

The local echo-review implementation follows the WPE formulation and was
developed against the NARA-WPE reference implementation:

https://github.com/fgnt/nara_wpe

No NARA-WPE package or network service is loaded at runtime.

MIT License

Copyright (c) 2018 Communications Engineering Group, Paderborn University

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.

## ws

The read-only VS Code Agent Host adapter uses the `ws` package:

https://github.com/websockets/ws

MIT License

Copyright (c) 2011 Einar Otto Stangvik <einaros@gmail.com>
Copyright (c) 2013 Arnout Kazemier and contributors
Copyright (c) 2016 Luigi Pinca and contributors

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY, FITNESS
FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE AUTHORS OR
COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER LIABILITY, WHETHER
IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM, OUT OF OR IN
CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE SOFTWARE.

## Installed JavaScript dependencies

The dependency lockfile pins packages installed by `npm ci`. The direct
dependencies and their licenses are:

- `@github/copilot-sdk` 1.0.34 — MIT
- `entities` 8.1.0 — BSD-2-Clause
- `marked` 18.0.14 — MIT
- `ws` 8.23.0 — MIT
- `esbuild` 0.28.2 (build-time) — MIT

The source release vendors browser builds of `entities` and `marked`; their
license texts are included under `third-party`. Other npm packages are fetched
by `npm ci` and are not redistributed in this archive. The lockfile and
`THIRD_PARTY_SBOM.cdx.json` record the pinned and resolved dependency inventory.
Each package remains subject to its own license.

## Vendored browser assets

- ONNX Runtime Web 1.30.0 — MIT; the vendored WebAssembly runtime assets come
  from the `onnxruntime-web@1.30.0` npm release:
  `https://registry.npmjs.org/onnxruntime-web/-/onnxruntime-web-1.30.0.tgz`.
  The tarball was verified against npm's `sha512-q0y+JrrtukXSzsBWEMccVfqX25LRmosXHF+CaRJmg8pZClzcV7svNc4rKY3jL02Vb7QmRMDs1SigqR4CXAfKYQ==`
  integrity. The license text is at `public\vendor\onnxruntime\LICENSE.txt`.
- Silero VAD ONNX model (SHA-256
  `7ed98ddbad84ccac4cd0aeb3099049280713df825c610a8ed34543318f1b2c49`) — MIT.
  The license text is at `public\vendor\silero\LICENSE.txt`; it was copied from
  the Silero VAD repository's pinned commit
  `1e261b036686cd0017d500ee96acd1c4ba572a9d`.
- `marked` and `entities` browser builds are generated from the pinned npm
  packages; their license texts are included under `third-party`.

## Optional speech and speaker components

Vega does not bundle FFmpeg. Voice-note decoding uses a separately installed
FFmpeg executable, which is independently licensed by its distributor.

The optional speaker setup downloads pinned ONNX models and Python wheels only
after the user explicitly runs `setup_speaker_runtime.py --provision`. Model
licenses, package versions, hashes, source URLs, and notice files are recorded
in `speaker-models.json` and `third-party\speaker-models`. No speaker model
weights or downloaded Python runtime are included in this source release.

The base Python dependency `tzdata` 2025.2 is licensed under Apache-2.0. Its
license is supplied by the Python package distribution.
